"""Accept real configured models through PatentCompass HTTP APIs; never read keys.

Run with api/.venv/Scripts/python.exe scripts/model-smoke-test.py. Existing ready
520-record sample data is reused; an independent project and artifacts are kept.
"""

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from zipfile import ZipFile

from docx import Document
import httpx


ROOT = Path(__file__).resolve().parents[1]


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(args):
    identity = "models-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = args.output_root.resolve() / identity
    output.mkdir(parents=True)
    result = {"acceptance_id": identity, "base_url": args.base_url, "status": "running",
              "started_at": datetime.now(timezone.utc).isoformat(), "stages": {}, "runs": {}}
    if args.resume:
        previous_path = args.resume.resolve()
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        check(previous["base_url"].rstrip("/") == args.base_url.rstrip("/"), "Resume must use the same backend URL")
        check(previous.get("project_id"), "Resume requires a previously created acceptance project")
        result = {**previous, "acceptance_id": identity, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                  "resumed_from": str(previous_path)}
        result.pop("finished_at", None)
        # A resumed historical question can narrow the scope again; always restore it.
        result["stages"].pop("restore_scope", None)

    def save():
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def stage(name, callback, dependencies=()):
        if args.resume and result["stages"].get(name, {}).get("status") == "passed":
            print(f"Reusing passed stage {name}; no model request repeated", flush=True)
            return result["stages"][name].get("result")
        if any(result["stages"].get(dependency, {}).get("status") != "passed" for dependency in dependencies):
            result["stages"][name] = {"status": "skipped", "reason": "A prerequisite stage failed", "dependencies": dependencies}
            save()
            return None
        print(f"Starting {name}", flush=True)
        started = time.monotonic()
        try:
            value = callback()
            result["stages"][name] = {"status": "passed", "seconds": round(time.monotonic() - started, 2), "result": value}
            print(f"Passed {name}", flush=True)
            return value
        except Exception as exc:
            result["stages"][name] = {"status": "failed", "seconds": round(time.monotonic() - started, 2), "error": str(exc)}
            print(f"Failed {name}: {exc}", flush=True)
            return None
        finally:
            save()

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=args.request_timeout, trust_env=False) as client:
        def request(method, path, *, expected_status=200, **kwargs):
            headers = dict(kwargs.pop("headers", {}))
            if method in {"POST", "PATCH", "PUT"}:
                headers["Idempotency-Key"] = identity + "-" + uuid4().hex
            response = client.request(method, path, headers=headers, **kwargs)
            check(response.status_code == expected_status,
                  f"{method} {path}: expected {expected_status}, received {response.status_code}: {response.text[:1200]}")
            return response.json()

        def wait_run(run_id, name):
            deadline = time.monotonic() + args.timeout
            last_status, last_progress = None, 0
            while time.monotonic() < deadline:
                value = request("GET", f"/api/runs/{run_id}")
                result["runs"][name] = value
                completed = sum(step["status"] == "succeeded" for step in value["steps"])
                progress = (value["status"], completed)
                if progress != last_status or time.monotonic() - last_progress > 15:
                    print(f"{name}: {value['status']}, {completed} completed steps", flush=True)
                    last_status, last_progress = progress, time.monotonic()
                    save()
                if value["status"] in {"succeeded", "failed", "cancelled", "waiting_input"}:
                    check(value["status"] == "succeeded", f"{name}: {value['status']}: {value.get('error') or value.get('input_request')}")
                    return value
                time.sleep(1)
            raise RuntimeError(f"{name}: timed out after {args.timeout} seconds; run {run_id} is retained")

        def start_run(action, name, **payload):
            previous = result["runs"].get(name) if args.resume else None
            if previous and previous["status"] == "failed":
                task = request("POST", f"/api/runs/{previous['id']}/retry", expected_status=202)
                result.setdefault("retried_runs", []).append({"name": name, "previous_run_id": previous["id"], "run_id": task["run_id"]})
                save()
                return wait_run(task["run_id"], name)
            if previous:
                return wait_run(previous["id"], name)
            task = request("POST", f"/api/projects/{result['project_id']}/runs", expected_status=202,
                           json={"action": action, **payload})
            return wait_run(task["run_id"], name)

        def verify_events(run_id, name, required):
            response = client.get(f"/api/runs/{run_id}/events")
            check(response.status_code == 200, f"{name}: SSE replay failed")
            events = []
            for block in response.text.split("\n\n"):
                fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                if "data" in fields:
                    events.append({"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])})
            ids = [event["id"] for event in events]
            check(ids and ids == sorted(set(ids)), f"{name}: invalid persisted event sequence")
            check(set(required).issubset({event["event"] for event in events}), f"{name}: missing required SSE events")
            cursor = ids[len(ids) // 2]
            resumed = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)})
            check(resumed.status_code == 200, f"{name}: SSE continuation failed")
            resumed_ids = [int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")]
            check(resumed_ids == [value for value in ids if value > cursor], f"{name}: SSE continuation duplicated or omitted events")
            (output / f"{name}-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"event_count": len(events), "resumed_after": cursor, "resumed_count": len(resumed_ids)}

        def prepare():
            readiness = request("GET", "/health/ready")
            check(readiness["status"] == "ready", "Backend dependencies are not ready")
            check(readiness["capabilities"].get("chat") and readiness["capabilities"].get("embeddings"),
                  "Both chat and embedding configuration must be loaded by API")
            result["readiness"] = readiness
            selected, cursor = None, None
            while not selected:
                page = request("GET", "/api/datasets", params={"limit": 200, **({"cursor": cursor} if cursor else {})})
                for dataset in page["items"]:
                    for version in dataset["versions"]:
                        quality = version.get("quality", {})
                        if version["status"] == "ready" and (quality.get("application_count"), quality.get("publication_count"), quality.get("abstract_count")) == (520, 526, 520):
                            selected = {"dataset_id": dataset["id"], "dataset_version_id": version["id"], "quality": quality}
                            break
                    if selected:
                        break
                cursor = page["next_cursor"]
                if not cursor:
                    break
            check(selected is not None, "Import the original 520-record sample before model acceptance")
            result["dataset"] = selected
            project = request("POST", "/api/projects", expected_status=201, json={"name": f"无人机真实模型验收 {identity}",
                "scope": {"industry": "无人机", "source_region": "中国", "coverage": "部分检索结果"}})
            result["project_id"] = project["id"]
            request("PUT", f"/api/projects/{project['id']}/datasets", json={"expected_revision": project["revision"],
                    "dataset_version_ids": [selected["dataset_version_id"]]})
            scope = request("PATCH", f"/api/projects/{project['id']}/scope", json={"expected_scope_version_id": project["scope_version_id"],
                            "changes": {}, "confirmed": True})
            result["initial_scope_version_id"] = scope["id"]
            return {"project_id": project["id"], "dataset_version_id": selected["dataset_version_id"], "scope_version_id": scope["id"]}

        stage("prepare", prepare)

        def index():
            value = start_run("index", "embedding_index")
            check(value["result"]["indexed_count"] == 520, "Index did not cover all 520 application records")
            batches = [step for step in value["steps"] if step["key"].startswith("embedding_batch_")]
            check(batches and all(step["status"] == "succeeded" for step in batches)
                  and sum(step["result"]["count"] for step in batches) == 520, "Embedding batch checkpoints incomplete")
            return {**value["result"], "batch_checkpoints": len(batches), "sse": verify_events(value["id"], "index", ["run.completed"])}

        stage("embedding_index", index, ("prepare",))

        def search():
            queries = []
            for mode in ("semantic", "hybrid"):
                for q in ("无人机避障", "UAV obstacle avoidance"):
                    page = request("GET", f"/api/projects/{result['project_id']}/patents", params={"q": q, "mode": mode, "limit": 5})
                    check(page["mode"] == mode and page["scope_application_count"] == 520, "Semantic search scope or mode is wrong")
                    check(0 < page["candidate_count"] <= 500 and len(page["items"]) == 5, "Semantic candidate count is wrong")
                    scores = [item["score"] for item in page["items"]]
                    check(all(isinstance(score, (int, float)) for score in scores) and scores == sorted(scores, reverse=True),
                          "Ranked search lost similarity ordering")
                    next_page = request("GET", f"/api/projects/{result['project_id']}/patents", params={"q": q, "mode": mode, "limit": 5, "cursor": page["next_cursor"]})
                    check(not ({item["id"] for item in page["items"]} & {item["id"] for item in next_page["items"]}), "Ranked pagination repeated applications")
                    queries.append({"mode": mode, "query": q, "page": page, "next_page": next_page})
            return queries

        stage("semantic_and_hybrid_search", search, ("embedding_index",))

        def conversation():
            task = request("POST", f"/api/projects/{result['project_id']}/conversations", expected_status=202,
                json={"initial_message": {"content": "请调用 search_patents 工具，使用简短检索词 drone 查询本项目样本，依据实际返回记录用中文总结三项无人机技术内容，必须引用至少一个返回的申请编号，不能编造记录。"}})
            result["conversation_id"] = task["conversation_id"]
            value = wait_run(task["run_id"], "chat")
            steps = [step for step in value["steps"] if step["key"].startswith("chat_search_")]
            check(steps and all(step["status"] == "succeeded" for step in steps), "Model did not actually invoke search_patents")
            messages = request("GET", f"/api/conversations/{task['conversation_id']}/messages")["items"]
            answer = next(item for item in messages if item["id"] == value["result"]["message_id"])
            citations = (answer.get("reference") or {}).get("citations", [])
            check(answer["role"] == "assistant" and citations, "Assistant answer or persisted original-record citations missing")
            check(any(citation["application_number"] in answer["content"] for citation in citations), "Answer does not cite a returned application number")
            retrieved_ids = {record["id"] for step in steps for record in step["result"]["records"]}
            check({citation["id"] for citation in citations} <= retrieved_ids, "Saved citations differ from actual tool results")
            result["chat_messages"] = messages
            check(request("GET", f"/api/conversations/{task['conversation_id']}/messages")["items"] == messages, "Message refresh changed history")
            return {"answer": answer, "search_step_count": len(steps), "sse": verify_events(value["id"], "chat", ["evidence.ready", "message.delta", "message.completed", "run.completed"])}

        stage("tool_chat", conversation, ("prepare",))

        def generate():
            value = start_run("generate_report", "ai_report", report_mode="ai")
            check(any(step["key"] == "report_synthesis" and step["status"] == "succeeded" for step in value["steps"]), "Report skipped real model synthesis")
            result["report_id"] = value["result"]["report_id"]
            version = request("GET", f"/api/reports/{result['report_id']}/versions/{value['result']['report_version_id']}")
            generated = [block for block in version["content"]["blocks"] if block.get("origin") == "model"]
            check(len(generated) == 1 and generated[0]["text"].strip(), "AI-generated report block missing")
            check(version["number"] == 1 and version["content"]["source"]["summary"]["application_count"] == 520, "AI report snapshot differs from sample")
            result["v1"], result["model_block_id"] = version, generated[0]["id"]
            return {"report_id": result["report_id"], "version_id": version["id"], "model_block": generated[0]}

        stage("ai_report", generate, ("prepare",))

        def revise():
            v1 = result["v1"]
            task = request("POST", f"/api/reports/{result['report_id']}/revisions", expected_status=202, json={
                "base_version_id": v1["id"], "expected_current_version_id": v1["id"], "block_id": result["model_block_id"],
                "instruction": "请把这段改写成两到三句中文，开头写“样本观察：”，突出这是部分检索结果，保留原文中已有的数据边界和证据，不新增技术结论或法律判断。"})
            value = wait_run(task["run_id"], "natural_language_revision")
            check(any(step["key"] == "revision_text" and step["status"] == "succeeded" for step in value["steps"]), "Natural-language revision skipped model")
            version = request("GET", f"/api/reports/{result['report_id']}/versions/{value['result']['report_version_id']}")
            old = next(block for block in v1["content"]["blocks"] if block["id"] == result["model_block_id"])
            new = next(block for block in version["content"]["blocks"] if block["id"] == result["model_block_id"])
            check(version["number"] == 2 and version["parent_id"] == v1["id"] and new["text"] != old["text"], "Revision did not create changed version 2")
            check(request("GET", f"/api/reports/{result['report_id']}/versions/{v1['id']}") == v1, "AI revision overwrote immutable version 1")
            result["v2"] = version
            return {"version_id": version["id"], "old_text": old["text"], "new_text": new["text"]}

        stage("natural_language_revision", revise, ("ai_report",))

        def historical_question():
            project = request("GET", f"/api/projects/{result['project_id']}")
            scope = request("PATCH", f"/api/projects/{project['id']}/scope", json={"expected_scope_version_id": project["scope_version_id"],
                "changes": {"q": "model-acceptance-no-such-keyword-" + uuid4().hex}, "confirmed": True})
            current = request("GET", f"/api/projects/{project['id']}/patents")
            check(current["total"] == 0, "Current scope was not narrowed for historical snapshot verification")
            v1 = result["v1"]
            block = next(item for item in v1["content"]["blocks"] if item["type"] == "paragraph" and "520" in item.get("text", ""))
            body = {"content": "请只依据我引用的旧版报告解释：该历史报告统计了多少条申请、多少个公开标识，为什么不能代表全部检索结果？请明确给出这两个数量。不要使用当前项目的新范围替换旧报告数据。",
                    "reference": {"report_version_id": v1["id"], "block_id": block["id"], "text": block["text"]}}
            if result.get("conversation_id"):
                conversation_id = result["conversation_id"]
                task = request("POST", f"/api/conversations/{conversation_id}/messages", expected_status=202, json=body)
            else:
                task = request("POST", f"/api/projects/{project['id']}/conversations", expected_status=202, json={"initial_message": body})
                conversation_id = task["conversation_id"]
            value = wait_run(task["run_id"], "historical_v1_question")
            check(value["scope_version_id"] == v1["scope_version_id"] != scope["id"], "Historical question used current scope")
            messages = request("GET", f"/api/conversations/{conversation_id}/messages")["items"]
            answer = next(item for item in messages if item["id"] == value["result"]["message_id"])
            check(answer["reference"]["report_version_id"] == v1["id"] and "520" in answer["content"] and "526" in answer["content"],
                  "Historical answer did not retain V1 sample counts")
            check(request("GET", f"/api/reports/{result['report_id']}/versions/{v1['id']}") == v1, "Historical question changed version 1")
            return {"current_scope_count": current["total"], "current_scope_version_id": scope["id"], "answer": answer,
                    "sse": verify_events(value["id"], "historical-v1", ["message.delta", "message.completed", "run.completed"])}

        stage("historical_v1_question", historical_question, ("natural_language_revision",))

        def restore_scope():
            project = request("GET", f"/api/projects/{result['project_id']}")
            restored = project["scope"]
            if restored["content"].get("q"):
                restored = request("PATCH", f"/api/projects/{project['id']}/scope", json={
                    "expected_scope_version_id": project["scope_version_id"], "changes": {"q": None}, "confirmed": True})
                start_run("analyze", "restore_current_analysis")
            page = request("GET", f"/api/projects/{project['id']}/patents")
            check(page["total"] == 520, "Acceptance project did not return to the complete 520-record sample scope")
            return {"scope_version_id": restored["id"], "application_count": page["total"]}

        stage("restore_scope", restore_scope, ("prepare",))

        def export():
            version = result["v2"]
            task = request("POST", f"/api/reports/{result['report_id']}/exports", expected_status=202,
                           json={"report_version_id": version["id"], "format": "docx"})
            value = wait_run(task["run_id"], "export_ai_v2")
            response = client.get(f"/api/files/{value['result']['file_id']}")
            check(response.status_code == 200, "AI Word download failed")
            document = Document(BytesIO(response.content))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            block = next(item for item in version["content"]["blocks"] if item["id"] == result["model_block_id"])
            check(block["text"] in text, "Downloaded Word differs from revised AI block")
            with ZipFile(BytesIO(response.content)) as archive:
                images = [name for name in archive.namelist() if name.startswith("word/media/")]
                check(len(images) == 3, "AI report Word does not include all three charts")
            path = output / "patentcompass-ai-report-v2.docx"
            path.write_bytes(response.content)
            return {"path": str(path), "bytes": len(response.content), "image_count": len(images), "file_id": value["result"]["file_id"], "report_version_id": version["id"]}

        stage("word_export", export, ("natural_language_revision",))

    result["status"] = "passed" if all(stage["status"] == "passed" for stage in result["stages"].values()) else "failed"
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    print(f"Model acceptance {result['status']}: {output / 'result.json'}", flush=True)
    return result["status"] == "passed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output-root", type=Path, default=ROOT / "storage" / "acceptance")
    parser.add_argument("--timeout", type=float, default=1800, help="Maximum seconds per asynchronous model task")
    parser.add_argument("--request-timeout", type=float, default=210, help="Maximum seconds per HTTP request")
    parser.add_argument("--resume", type=Path, help="Resume retained result.json in a new output folder; passed stages are reused")
    sys.exit(0 if run(parser.parse_args()) else 1)
