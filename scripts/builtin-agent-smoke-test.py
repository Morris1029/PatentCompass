"""Real-model acceptance through the original frontend conversation HTTP entry.

Creates an isolated project over an explicitly selected small, ready dataset.
Uses no MCP calls or controlled model doubles. Confirmations are checked against
the fixed scope, data and intended actions before they are submitted.
"""

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import time
from uuid import uuid4

from docx import Document
import httpx


def run(options):
    root = Path(__file__).resolve().parents[1]
    identity = "builtin-agent-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = root / "storage" / "acceptance" / identity
    output.mkdir(parents=True)
    evidence = {"status": "running", "base_url": options.base_url, "real_model": True,
                "entry": "original conversation HTTP API through frontend proxy", "phases": []}
    if options.resume:
        previous_file = Path(options.resume).resolve()
        evidence = json.loads(previous_file.read_text(encoding="utf-8"))
        output = previous_file.parent
        check_dataset = evidence.get("dataset_version_id")
        if check_dataset != options.dataset_version:
            raise ValueError("Resume must retain the exact dataset version")
        evidence.setdefault("previous_errors", []).append(evidence.pop("error", "interrupted"))
        evidence["status"] = "running"

    def save():
        (output / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(condition, message):
        if not condition:
            raise RuntimeError(message)

    save()
    with httpx.Client(base_url=options.base_url, timeout=45, trust_env=False) as client:
        def request(method, path, body=None):
            response = client.request(method, "/api" + path, json=body,
                headers={"Idempotency-Key": str(uuid4())} if method != "GET" else {})
            check(response.status_code in {200, 201, 202}, f"{method} {path}: {response.status_code}: {response.text[:500]}")
            return response.json()

        try:
            datasets = request("GET", "/datasets?limit=200")
            version = next((v for item in datasets["items"] for v in item.get("versions", [])
                            if v["id"] == options.dataset_version), None)
            check(version and version["status"] == "ready", "Select a ready dataset version")
            count = version["quality"]["application_count"]
            check(0 < count <= options.max_records, "Dataset exceeds the explicit acceptance record budget")
            evidence["dataset_version_id"], evidence["sample_count"] = version["id"], count
            if options.resume:
                project = request("GET", "/projects/" + evidence["project_id"])
            else:
                project = request("POST", "/projects", {"name": identity, "scope": {"industry": "Camera landing acceptance sample",
                    "coverage": "Explicit synthetic acceptance fixture; no industry-wide claims"}})
                evidence["project_id"] = project["id"]
                request("PUT", f"/projects/{project['id']}/datasets", {"expected_revision": project["revision"], "dataset_version_ids": [version["id"]]})
                request("PATCH", f"/projects/{project['id']}/scope", {"expected_scope_version_id": project["scope_version_id"], "changes": {}, "confirmed": True})
            project = request("GET", "/projects/" + project["id"])
            fixed_scope = project["scope"]["content"]
            evidence["scope"] = project["scope"]
            save()
            conversation = None
            phases = [
                ("analysis", f"请仅对已绑定的{count}条样本完成全量语义分析，整理基础要素、技术方法、应用场景和原文证据，保存分析结果。这一步不要生成报告。", {"analyze"}, None),
                ("qa", "请检索原文说明摄像头在自主降落中的作用，引用实际申请号。只回答问题，不重新分析或生成报告。", {"search_patents", "search_materials"}, None),
                ("report", "请基于刚才保存的语义分析，生成一份新的独立专利导航报告。无需重新分类，只采用已绑定样本，保留图表、证据和样本局限。", {"analyze", "generate_report"}, "new"),
                ("revision", "仅将这份报告标题改为“统一 Agent 实际对话验收报告”，正文、统计和图表保持原样。", {"revise_report"}, "existing"),
                ("export", "请把当前这份报告的最新版本导出为 Word，并返回实际下载链接。", {"export"}, None),
            ]
            for index, (name, text, allowed, choice) in enumerate(phases):
                body = {"content": text, "action": "auto", "semantic_mode": "full"}
                if index < len(evidence["phases"]):
                    phase = evidence["phases"][index]
                    check(phase["name"] == name, "Resume phase order mismatch")
                    if phase.get("verified") and options.restart_phase != name:
                        conversation = phase["state"]["result"]["conversation_id"]
                        continue
                    state = request("GET", "/runs/" + phase["run_id"])
                    if options.restart_phase == name:
                        if state["status"] not in {"succeeded", "failed", "cancelled"}:
                            request("POST", "/runs/" + phase["run_id"] + "/cancel")
                        previous_attempts = phase.get("previous_attempts", [])
                        previous_attempts.append({key: value for key, value in phase.items() if key != "previous_attempts"})
                        replacement = request("POST", f"/conversations/{conversation}/messages", body)
                        phase = {"name": name, "request": body, "run_id": replacement["run_id"],
                                 "confirmations": [], "previous_attempts": previous_attempts}
                        evidence["phases"][index] = phase
                        state = request("GET", "/runs/" + phase["run_id"])
                    if state["status"] == "failed":
                        phase.setdefault("run_ids", [phase["run_id"]])
                        retried = request("POST", "/runs/" + phase["run_id"] + "/retry")
                        phase["run_id"] = retried["run_id"]
                        phase["run_ids"].append(retried["run_id"])
                    sent = {"run_id": phase["run_id"]}
                else:
                    sent = (request("POST", f"/projects/{project['id']}/conversations", {"initial_message": body}) if conversation is None
                            else request("POST", f"/conversations/{conversation}/messages", body))
                    phase = {"name": name, "request": body, "run_id": sent["run_id"], "confirmations": []}
                    evidence["phases"].append(phase)
                save()
                deadline, previous = time.monotonic() + options.timeout, None
                while time.monotonic() < deadline:
                    state = request("GET", "/runs/" + sent["run_id"])
                    phase["state"] = state
                    if state["status"] != previous:
                        print(name + ": " + state["status"], flush=True)
                        previous = state["status"]
                    if state["status"] == "waiting_input":
                        question = state["input_request"]
                        check(question["kind"] == "execution_confirmation", "Unexpected input retained: " + str(question))
                        check(question.get("scope") == fixed_scope, "Confirmation changed the fixed scope")
                        check(state["dataset_version_ids"] == [version["id"]], "Confirmation changed the fixed dataset")
                        actions = {item["kind"] for item in question["actions"]}
                        check(actions and actions <= allowed, "Confirmation requested an unexpected operation: " + str(actions))
                        values = {"confirmed": True}
                        if question.get("research_assignment"):
                            check(choice and question["research_assignment"].get("allow_" + choice), "Fixed research choice unavailable")
                            values["research_choice"] = choice
                        check(question["id"] not in [item["input_request_id"] for item in phase["confirmations"]], "Repeated confirmation")
                        answer = {"input_request_id": question["id"], "expected_scope_version_id": state["scope_version_id"], "values": values}
                        phase["confirmations"].append(answer)
                        save()
                        request("POST", "/runs/" + sent["run_id"] + "/inputs", answer)
                    elif state["status"] in {"succeeded", "failed", "cancelled"}:
                        save()
                        check(state["status"] == "succeeded", name + " failed: " + str(state.get("error")))
                        break
                    save()
                    time.sleep(1)
                else:
                    raise TimeoutError("Original run remains available: " + sent["run_id"])
                expected_action = {"analysis": "analyze", "qa": "chat", "report": "generate_report",
                                   "revision": "revise_report", "export": "export"}[name]
                check(state.get("plan", {}).get("action") == expected_action, "Agent action did not match the fixed user request")
                events = []
                for identifier in phase.get("run_ids", [sent["run_id"]]):
                    response = client.get("/api/runs/" + identifier + "/events")
                    events.extend(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: "))
                phase["events"] = events
                check(any(event["event_type"] == "agent.started" for event in events), "Built-in runtime was not used")
                check(any(event["event_type"] == "agent.skill.loaded" for event in events), "No actual Skill loaded")
                phase["tools"] = [event["payload"]["name"] for event in events if event["event_type"] == "agent.tool.called"]
                check("prepare_agent_action" in phase["tools"], "Original preparation was bypassed")
                conversation = state["result"]["conversation_id"]
                phase["messages"] = request("GET", f"/conversations/{conversation}/messages")
                phase["verified"] = True
                save()
                print("Passed " + name + ": " + ", ".join(phase["tools"]), flush=True)
            result = evidence["phases"][-1]["state"]["result"]
            generated = evidence["phases"][2]["state"]["result"]
            revised = evidence["phases"][3]["state"]["result"]
            before = request("GET", f"/reports/{generated['report_id']}/versions/{generated['report_version_id']}")["content"]
            after = request("GET", f"/reports/{revised['report_id']}/versions/{revised['report_version_id']}")["content"]
            check(before["blocks"] == after["blocks"] and before["source"] == after["source"]
                  and before.get("evidence_snapshot") == after.get("evidence_snapshot"), "Title-only edit changed report content or evidence")
            evidence["title_only_preserved_content"] = True
            check(result.get("file_id") and result.get("download_url"), "No actual Word export")
            response = client.get(result["download_url"])
            check(response.status_code == 200, "Word download failed")
            document = Document(BytesIO(response.content))
            check(any("统一 Agent 实际对话验收报告" in item.text for item in document.paragraphs), "Revised title missing")
            check(len(document.inline_shapes) > 0, "Report charts missing")
            (output / "report.docx").write_bytes(response.content)
            evidence.update(status="passed", conversation_id=conversation, file_id=result["file_id"],
                chart_count=len(document.inline_shapes), word_bytes=len(response.content))
            save()
        except BaseException as error:
            evidence.update(status="failed", error=str(error))
            save()
            raise
    print(str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--max-records", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--resume", help="Resume an existing result.json without replacing its original runs")
    parser.add_argument("--restart-phase", choices=["analysis", "qa", "report", "revision", "export"],
                        help="Replace a verified failing phase with a new original message, retaining its failed attempt")
    run(parser.parse_args())
