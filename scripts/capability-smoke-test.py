"""Verify direct Python, real HTTP/worker and MCP capabilities on an actual workbook.

The default path runs parsing, statistics, keyword search, template reporting and Word
export without model usage. --ai additionally makes real small-sample model requests.
Existing projects and research versions are not created or updated by this script.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from zipfile import ZipFile

import httpx
from docx import Document
from mcp import Client


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from patentcompass_core.contracts import PatentRecord
from patentcompass_core.parsing import parse_workbook
from patentcompass_core.retrieval import search_patents
from patentcompass_core.statistics import compute_statistics


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


async def run(options):
    identity = "capabilities-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = options.output_root.resolve() / identity
    output.mkdir(parents=True)
    result = {"id": identity, "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "real_services": True, "real_model_requests": options.ai, "stages": {}, "runs": {}}

    def save():
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def passed(name, value):
        result["stages"][name] = {"status": "passed", "result": value}
        save()
        print("Passed " + name, flush=True)

    save()
    stage = "prepare"
    try:
        workbook = options.workbook.resolve()
        parsed = parse_workbook(workbook)
        check(parsed["quality"]["status"] == "ready", "Workbook failed its data-quality checks")
        records = [PatentRecord.model_validate(record).model_dump(mode="json")
                   for record in parsed["applications"][:options.sample_size]]
        check(records, "Workbook has no selected records")
        expected = compute_statistics(records)
        expected_search = search_patents(records, {"q": options.query, "mode": "keywords"})
        result["source"] = {"workbook": str(workbook), "sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
                            "workbook_count": parsed["quality"]["application_count"], "selected_count": len(records)}
        passed("python", {"summary": expected["summary"], "search_count": expected_search["candidate_count"]})

        with httpx.Client(base_url=options.base_url.rstrip("/"), timeout=options.request_timeout,
                          trust_env=False) as http:
            def request(method, path, *, status=200, key=None, **kwargs):
                headers = {"Idempotency-Key": key} if key else {}
                response = http.request(method, path, headers=headers, **kwargs)
                check(response.status_code == status,
                      f"{method} {path}: expected {status}, received {response.status_code}: {response.text[:400]}")
                return response.json()

            def invoke(name, payload, key=None):
                return request("POST", f"/api/capabilities/{name}/invoke", json=payload, key=key,
                               status=202 if key else 200)

            async def wait(task, name):
                deadline = time.monotonic() + options.timeout
                previous = None
                while time.monotonic() < deadline:
                    value = request("GET", f"/api/runs/{task['run_id']}")
                    result["runs"][name] = {"run_id": task["run_id"], "status": value["status"],
                                             "step_count": len(value["steps"])}
                    save()
                    if value["status"] != previous:
                        print(f"{name}: {value['status']}", flush=True)
                        previous = value["status"]
                    if value["status"] in {"succeeded", "failed", "cancelled", "waiting_input"}:
                        check(value["status"] == "succeeded", f"{name}: {value.get('error') or value.get('input_request') or value['status']}")
                        check(value["project_id"] is None and value["conversation_id"] is None,
                              f"{name}: standalone task acquired project or conversation state")
                        check("_response_items" not in json.dumps(value), f"{name}: private model state exposed")
                        return value
                    await asyncio.sleep(1)
                raise RuntimeError(f"{name}: timeout; task {task['run_id']} is retained")

            stage = "readiness"
            ready = request("GET", "/health/ready")
            check(ready["status"] == "ready", "API dependencies are not ready")
            if options.ai:
                check(ready["capabilities"].get("chat"), "--ai requires configured real Responses generation")
            projects_before = request("GET", "/api/projects", params={"limit": 200})
            catalogue = request("GET", "/api/capabilities")
            check(len(catalogue["items"]) == 16, "Expected sixteen registered capabilities")
            passed(stage, {"capability_count": len(catalogue["items"]), "ai_requested": options.ai})

            stage = "worker_parse"
            upload = request("POST", "/api/capabilities/files", status=201, key=identity + "-upload",
                             files={"file": (workbook.name, workbook.read_bytes())})
            parsed_task = invoke("parse_patent_workbook", {"file_id": upload["file_id"]}, identity + "-parse")
            parsed_run = await wait(parsed_task, stage)
            check(parsed_run["result"]["quality"] == parsed["quality"], "Worker and Python workbook facts differ")
            passed(stage, {"run_id": parsed_task["run_id"], "file_id": upload["file_id"],
                           "application_count": parsed_run["result"]["quality"]["application_count"]})

            stage = "http_facts"
            http_statistics = invoke("compute_statistics", {"records": records})
            check(http_statistics == expected, "Python and HTTP statistics differ")
            http_search = invoke("search_patents", {"records": records, "q": options.query})
            check(http_search["candidate_count"] == expected_search["candidate_count"], "Python and HTTP search counts differ")
            check({row["id"] for row in http_search["items"]} == {row["id"] for row in expected_search["items"]},
                  "Python and HTTP search records differ")
            passed(stage, {"application_count": expected["summary"]["application_count"],
                           "search_count": http_search["candidate_count"]})

            stage = "mcp"
            async with Client(options.mcp_url or options.base_url.rstrip("/") + "/mcp/",
                              read_timeout_seconds=options.request_timeout) as mcp:
                async def call(name, arguments):
                    response = await mcp.call_tool(name, arguments)
                    check(not response.is_error, f"MCP {name}: {response.structured_content or response.content}")
                    check(isinstance(response.structured_content, dict), f"MCP {name}: missing structured result")
                    return response.structured_content

                tools = await mcp.list_tools()
                names = {tool.name for tool in tools.tools}
                check({item["name"] for item in catalogue["items"]} <= names, "MCP and HTTP capability directories differ")
                mcp_statistics = await call("compute_statistics", {"request": {"records": records}})
                check(mcp_statistics == expected, "Python, HTTP and MCP statistics differ")
                mcp_search = await call("search_patents", {"request": {"records": records, "q": options.query}})
                check(mcp_search == http_search, "HTTP and MCP search results differ")
                passed(stage, {"tool_count": len(names), "statistics_and_search_match": True})

                stage = "report"
                report_input = {"records": records, "title": "专利能力独立验收报告", "report_mode": "statistical"}
                task = invoke("build_report", report_input, identity + "-report")
                repeated = await call("build_report", {"request": report_input, "idempotency_key": identity + "-report"})
                check(task["run_id"] == repeated["run_id"], "HTTP/MCP repeated submission created a second task")
                report = await wait(task, stage)
                content = report["result"]["content"]
                check(content["source"]["summary"] == expected["summary"], "Report changed the independently computed statistics")
                mcp_run = await call("get_run", {"run_id": task["run_id"]})
                check(mcp_run["result"] == report["result"] and mcp_run["status"] == "succeeded", "HTTP/MCP task results differ")
                passed(stage, {"run_id": task["run_id"], "application_count": content["source"]["summary"]["application_count"],
                               "cross_transport_idempotency": True})

                stage = "word_export"
                export_task = await call("export_docx", {"request": {"content": content, "filename": "capability-report.docx"},
                                                         "idempotency_key": identity + "-export"})
                exported = await wait(export_task, stage)
                download = http.get(exported["result"]["download_url"])
                check(download.status_code == 200, "Exported Word file cannot be downloaded")
                check(hashlib.sha256(download.content).hexdigest() == exported["result"]["sha256"], "Downloaded file hash differs")
                document = Document(BytesIO(download.content))
                check(content["title"] in "\n".join(paragraph.text for paragraph in document.paragraphs), "Word title differs from report snapshot")
                target = output / "capability-report.docx"
                target.write_bytes(download.content)
                events = await call("get_run_events", {"run_id": export_task["run_id"]})
                check(any(event["event_type"] == "run.completed" for event in events["items"]), "MCP completion event missing")
                passed(stage, {"run_id": export_task["run_id"], "file_id": exported["result"]["file_id"],
                               "path": str(target), "bytes": len(download.content)})

                if options.ai:
                    stage = "real_classification"
                    classified = await wait(invoke("classify_patents", {"records": records}, identity + "-classify"), stage)
                    classification = classified["result"]
                    check(classification["coverage"]["processed_count"] == len(records), "Real classification did not cover all selected records")
                    originals = {record["id"]: record for record in records}
                    for identifier, row in classification["assignments"].items():
                        for evidence in row["assignment"]["evidence"]:
                            check(evidence["quote"] in originals[identifier][evidence["field"]], "Real classification retained an ungrounded quote")
                    passed(stage, {"run_id": classified["id"], "coverage": classification["coverage"]})

                    stage = "real_ai_report"
                    ai_task = await call("build_report", {"request": {**report_input, "report_mode": "ai", "analysis": classification},
                                                         "idempotency_key": identity + "-ai-report"})
                    generated = await wait(ai_task, stage)
                    model_blocks = [block for block in generated["result"]["content"]["blocks"] if block.get("origin") == "model"]
                    check(len(model_blocks) == 1 and model_blocks[0]["text"].strip(), "Real AI report has no generated summary")
                    composed = generated["result"]["content"]
                    check(all(composed["source"]["summary"].get(key) == value for key, value in expected["summary"].items()),
                          "AI report changed fixed statistics")
                    check(any(block.get("text") == "全量语义分类与技术结构" for block in composed["blocks"]),
                          "Classification was lost when composing the report")
                    check(composed["evidence_snapshot"], "Composed report lost its evidence")
                    chart_count = sum(block["type"] == "chart" for block in composed["blocks"])
                    check(chart_count >= 3, "Composed report lost its charts")
                    passed(stage, {"run_id": generated["id"], "model_summary_characters": len(model_blocks[0]["text"]),
                                   "chart_count": chart_count, "evidence_count": len(composed["evidence_snapshot"])})
                    stage = "semantic_word_export"
                    exported = await wait(invoke("export_docx", {"content": composed}, identity + "-semantic-word"), stage)
                    document = http.get(exported["result"]["download_url"])
                    check(document.status_code == 200, "Semantic Word download failed")
                    with ZipFile(BytesIO(document.content)) as archive:
                        images = [name for name in archive.namelist() if name.startswith("word/media/")]
                    check(len(images) == chart_count, "Semantic Word images differ from its frozen charts")
                    (output / "semantic-report.docx").write_bytes(document.content)
                    passed(stage, {"run_id": exported["id"], "file_id": exported["result"]["file_id"], "images": len(images)})

            stage = "project_isolation"
            projects_after = request("GET", "/api/projects", params={"limit": 200})
            check(projects_before["total"] == projects_after["total"] and
                  {project["id"] for project in projects_before["items"]} == {project["id"] for project in projects_after["items"]},
                  "Project inventory changed during acceptance; check for concurrent writes or unintended side effects")
            passed(stage, {"project_count_unchanged": True})
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["stages"][stage] = {"status": "failed", "error": str(error), "code": getattr(error, "code", type(error).__name__)}
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        print(f"Capability acceptance {result['status']}: {output / 'result.json'}", flush=True)
    return result["status"] == "passed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mcp-url", help="Defaults to the API base URL plus /mcp/")
    parser.add_argument("--workbook", type=Path, default=ROOT / "database" / "patent-demo.xlsx")
    parser.add_argument("--sample-size", type=int, default=3, help="Maximum real records used after full-workbook parsing")
    parser.add_argument("--query", default="landing")
    parser.add_argument("--output-root", type=Path, default=ROOT / "storage" / "acceptance")
    parser.add_argument("--timeout", type=float, default=600, help="Maximum seconds per persistent task")
    parser.add_argument("--request-timeout", type=float, default=210)
    parser.add_argument("--ai", action="store_true", help="Make real Responses requests for classification and report synthesis")
    args = parser.parse_args()
    if not 1 <= args.sample_size <= 20:
        parser.error("--sample-size must be between 1 and 20")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if asyncio.run(run(args)) else 1)
