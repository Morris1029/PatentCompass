"""Real-model acceptance for the integrated app's read/write task routes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / "storage/acceptance" / ("frontend-runtime-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    folder.mkdir(parents=True)
    evidence = {"project_id": args.project_id, "base_url": args.base_url}
    client = httpx.Client(base_url=args.base_url, timeout=240)

    def request(method, path, body=None):
        response = client.request(method, "/api" + path, json=body,
                                  headers={"Idempotency-Key": str(uuid4())} if method != "GET" else {})
        response.raise_for_status()
        return response.json()

    def persist():
        (folder / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def wait(task):
        for _ in range(180):
            state = request("GET", f"/runs/{task['run_id']}")
            if state["status"] in {"succeeded", "failed", "cancelled", "waiting_input"}:
                evidence.setdefault("runs", []).append(state)
                persist()
                assert state["status"] == "succeeded", state.get("error") or state.get("input_request")
                print(json.dumps({"run_id": state["id"], "kind": state["kind"], "status": state["status"]}), flush=True)
                return state
            time.sleep(2)
        raise TimeoutError(task["run_id"])

    try:
        project = request("GET", f"/projects/{args.project_id}")
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
                assert updated["data_sha256"] != chart["data_sha256"]
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
        for text, expected in [("请只对当前全部样本执行统计分析。", "analyze"),
                               ("请根据已有分析生成一份统计简报。", "generate_report"),
                               ("请把当前报告标题改为无人机样本导航验收报告。", "revise_report")]:
            task = request("POST", f"/projects/{args.project_id}/conversations", {"initial_message": {
                "content": text, "action": "auto", "semantic_mode": "off", "report_mode": "statistical"}})
            state = wait(task)
            assert state["plan"]["action"] == expected, state["plan"]
        evidence["status"] = "passed"
        persist()
        print(str(folder / "result.json"), flush=True)
    except Exception as exc:
        evidence["status"], evidence["error"] = "failed", str(exc)
        persist()
        raise


if __name__ == "__main__":
    main()
