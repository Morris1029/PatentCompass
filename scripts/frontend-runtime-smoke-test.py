"""Real-model acceptance for the integrated app's read/write task routes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx


def confirmation_input(state, fixed):
    """Use this case's fixed target; never adopt a model recommendation."""
    question = state.get("input_request") or {}
    if not fixed or question.get("kind") != "execution_confirmation":
        raise RuntimeError(f"Unexpected input request retained for inspection: {question}")
    if (state["scope_version_id"] != fixed["scope_version_id"] or question.get("scope") != fixed["scope"]
            or sorted(state["dataset_version_ids"]) != sorted(fixed["dataset_version_ids"])):
        raise RuntimeError("Confirmation changed the fixed acceptance scope or dataset")
    actions = {item["kind"] for item in question.get("actions", [])}
    if not actions or not actions <= set(fixed["actions"]):
        raise RuntimeError("Confirmation requested an operation outside this acceptance case")
    values = {"confirmed": True}
    assignment = question.get("research_assignment")
    if assignment:
        choice = fixed.get("research_choice")
        if choice not in {"new", "existing"} or not assignment.get("allow_" + choice):
            raise RuntimeError("The fixed acceptance research choice is unavailable")
        if choice == "existing" and assignment.get("existing_research_id") != fixed["research_id"]:
            raise RuntimeError("Confirmation changed the fixed target research")
        values["research_choice"] = choice
    elif fixed.get("research_choice") and not state.get("research_id") == fixed.get("research_id"):
        raise RuntimeError("Report execution did not retain the fixed target research")
    if fixed.get("report_version_id"):
        for field in ("report_base_version_id", "target_expected_current_version_id"):
            if question.get(field) != fixed["report_version_id"]:
                raise RuntimeError("Confirmation changed the fixed report revision")
        if question.get("select_blocks"):
            raise RuntimeError("The title-only acceptance case must not guess a paragraph target")
    return {"input_request_id": question["id"], "expected_scope_version_id": fixed["scope_version_id"], "values": values}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--timeout", type=float, default=600, help="Maximum seconds per task, including explicit confirmations")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / "storage/acceptance" / ("frontend-runtime-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    folder.mkdir(parents=True)
    evidence = {"project_id": args.project_id, "base_url": args.base_url}
    client = httpx.Client(base_url=args.base_url, timeout=240, trust_env=False)

    def request(method, path, body=None):
        response = client.request(method, "/api" + path, json=body,
                                  headers={"Idempotency-Key": str(uuid4())} if method != "GET" else {})
        response.raise_for_status()
        return response.json()

    def persist():
        (folder / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def wait(task, fixed=None):
        deadline = time.monotonic() + args.timeout
        confirmed = set()
        while time.monotonic() < deadline:
            state = request("GET", f"/runs/{task['run_id']}")
            if state["status"] == "waiting_input":
                evidence.setdefault("pending_inputs", []).append(state)
                persist()
                body = confirmation_input(state, fixed)
                if body["input_request_id"] in confirmed:
                    raise RuntimeError("The same confirmation remained pending after submission")
                confirmed.add(body["input_request_id"])
                evidence.setdefault("confirmations", []).append({"run_id": task["run_id"], "input": body})
                persist()
                request("POST", f"/runs/{task['run_id']}/inputs", body)
                continue
            if state["status"] in {"succeeded", "failed", "cancelled"}:
                evidence.setdefault("runs", []).append(state)
                persist()
                assert state["status"] == "succeeded", state.get("error") or state.get("input_request")
                print(json.dumps({"run_id": state["id"], "kind": state["kind"], "status": state["status"]}), flush=True)
                return state
            time.sleep(2)
        raise TimeoutError(task["run_id"])

    try:
        project = request("GET", f"/projects/{args.project_id}")
        assert project["scope"]["confirmed"] and project["dataset_version_id"], "Prepare a confirmed, bound 520-record acceptance project first"
        evidence["initial_scope"] = project["scope"]
        evidence["dataset_version_ids"] = project["dataset_version_ids"]
        report_id, frozen_id = project.get("report_id"), project.get("current_report_version_id")
        frozen = request("GET", f"/reports/{report_id}/versions/{frozen_id}") if frozen_id else None
        view = request("GET", f"/projects/{args.project_id}/analysis?view=overview")
        charts = [request("GET", f"/charts/{key}") for key in view["data"]["chart_ids"]]
        chart = next(item for item in charts if item["kind"] == "yearly")
        revisions = []
        for mode, extra in [("style", {"option": {"title": {"text": "年度分布验收"}}}),
                            ("data", {"filters": {"q": "landing"}}),
                            ("insight", {"instruction": "用两句话解释统计口径和证据边界，不作法律结论。"})]:
            task = request("POST", f"/charts/{chart['id']}/revisions", {"base_version_id": chart["version_id"],
                "expected_current_version_id": chart["current_version_id"], "mode": mode, **extra})
            state = wait(task)
            updated = request("GET", f"/charts/{chart['id']}?version_id={state['result']['chart_version_id']}")
            if mode != "data":
                assert updated["data_sha256"] == chart["data_sha256"]
            else:
                independent = request("GET", f"/projects/{args.project_id}/patents?q=landing&mode=keywords")
                assert len(updated["provenance"]["application_ids"]) == independent["total"]
            if mode == "insight":
                assert updated["interpretation"]["text"]
            revisions.append({"mode": mode, "version_id": updated["version_id"], "data_sha256": updated["data_sha256"]})
            chart = updated
        evidence["chart_revisions"] = revisions
        if frozen:
            assert request("GET", f"/reports/{report_id}/versions/{frozen_id}")["content"] == frozen["content"]
        evidence["historical_report_unchanged"] = bool(frozen)
        semantic = request("GET", f"/projects/{args.project_id}/patents?mode=semantic&q=autonomous%20landing&limit=5")
        assert semantic["total"] is None and semantic["scope_application_count"] == 520
        assert semantic["returned_count"] == 5 and semantic["candidate_count"] <= 500
        evidence["semantic_search"] = {key: semantic[key] for key in ("mode", "total", "scope_application_count", "candidate_count", "returned_count")}
        for text, expected in [("请在当前已确认范围内重新计算全部入选样本的年度、申请人和 IPC 统计，并保存新的工作台分析结果。本次仅更新分析工作台，采用确定性统计，研究范围保持不变。", "analyze"),
                               ("请根据已有分析生成一份统计简报。", "generate_report"),
                               ("请把当前报告标题改为无人机样本导航验收报告。", "revise_report")]:
            project = request("GET", f"/projects/{args.project_id}")
            fixed = {"scope": project["scope"]["content"], "scope_version_id": project["scope_version_id"],
                     "dataset_version_ids": project["dataset_version_ids"], "actions": [expected]}
            body = {"content": text, "action": "auto", "semantic_mode": "off", "report_mode": "statistical"}
            if expected in {"generate_report", "revise_report"}:
                research_id = project["current_research_id"]
                fixed.update(research_choice="existing" if research_id else "new", research_id=research_id)
                if expected == "generate_report":
                    fixed["actions"] = ["analyze", "generate_report"]
                    if research_id:
                        context = request("GET", f"/research-versions/{research_id}")["context"]
                        fixed.update(scope=context["scope"]["content"], scope_version_id=context["scope_version_id"],
                                     dataset_version_ids=context["dataset_version_ids"])
                else:
                    assert project["current_report_version_id"], "The fixed title-edit target report is missing"
                    target = request("GET", f"/reports/{project['report_id']}/versions/{project['current_report_version_id']}")
                    fixed.update(report_version_id=target["id"], scope=target["content"]["source"]["scope"],
                                 scope_version_id=target["scope_version_id"], dataset_version_ids=target["dataset_version_ids"])
                    body["reference"] = {"report_version_id": target["id"]}
            task = request("POST", f"/projects/{args.project_id}/conversations", {"initial_message": body})
            state = wait(task, fixed)
            assert state["plan"]["action"] == expected, state["plan"]
            assert any(item["kind"] == expected and item["status"] == "succeeded" for item in state.get("activities", [])), state.get("activities")
            if expected == "revise_report":
                revised = request("GET", f"/reports/{project['report_id']}/versions/{state['result']['report_version_id']}")
                assert revised["content"]["title"] == "无人机样本导航验收报告"
                assert revised["research_id"] == research_id
        evidence["status"] = "passed"
        persist()
        print(str(folder / "result.json"), flush=True)
    except Exception as exc:
        evidence["status"], evidence["error"] = "failed", str(exc)
        persist()
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
