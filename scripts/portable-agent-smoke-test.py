"""Real MCP/worker/project/Word acceptance with explicitly controlled host reasoning.

Creates a labelled synthetic dataset and an isolated acceptance project; preserves
all existing projects. This does not call a generation model or install a client plugin.
"""

import argparse
import asyncio
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
from mcp import Client
from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
from patentcompass_core.parsing import HEADERS


async def run(base_url):
    identity = "portable-agent-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = ROOT / "storage" / "acceptance" / identity
    output.mkdir(parents=True)
    result = {"status": "running", "real_services": True, "host_reasoning": "controlled synthetic fixture",
              "generation_model_calls": False, "stages": {}}

    def save():
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(value, message):
        if not value:
            raise RuntimeError(message)

    save()
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False) as http, Client(
                base_url + "/mcp/", read_timeout_seconds=60) as mcp:
            async def call(name, request, key=None):
                args = {"request": request}
                if key:
                    args["idempotency_key"] = identity + ":" + key
                response = await mcp.call_tool(name, args)
                check(not response.is_error, f"{name}: {response.structured_content or response.content}")
                return response.structured_content

            async def wait(task):
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    response = await mcp.call_tool("get_run", {"run_id": task["run_id"]})
                    check(not response.is_error, "get_run failed")
                    state = response.structured_content
                    check(state["status"] not in {"failed", "cancelled", "waiting_input"}, str(state.get("error") or state["status"]))
                    if state["status"] == "succeeded":
                        result["stages"][task["run_id"]] = state["result"]
                        save()
                        return state["result"]
                    await asyncio.sleep(.5)
                raise RuntimeError("Task timeout; original run retained: " + task["run_id"])

            workbook = Workbook()
            sheet = workbook.active
            sheet.append(list(HEADERS))
            for index in range(3):
                values = {"application_number": f"CN{202400000001 + index}", "publication_numbers": f"CN{110000001 + index}A",
                    "application_date": "2024-01-01", "publication_dates": "2025-01-01", "ipc_codes": "G01C21/00",
                    "applicants": "Synthetic A;Synthetic B", "inventors": "Example Inventor", "title": "Camera landing",
                    "countries": "CN", "abstract": "A camera supports autonomous landing on a moving platform."}
                sheet.append([values.get(field, "") for field in HEADERS.values()])
            data = BytesIO()
            workbook.save(data)
            response = await http.post("/api/datasets/imports", files={"file": (identity + ".xlsx", data.getvalue())},
                data={"name": identity, "source_metadata": json.dumps({"coverage": "explicit synthetic acceptance fixture"})},
                headers={"Idempotency-Key": identity + ":import"})
            check(response.status_code == 202, response.text)
            imported = response.json()
            await wait(imported)
            project = await call("create_agent_project", {"name": identity, "scope": {"industry": "Synthetic camera landing"}}, "project")
            result["project_id"] = project["id"]
            await call("bind_agent_dataset", {"project_id": project["id"], "dataset_version_id": imported["dataset_version_id"], "expected_revision": 0}, "bind")
            await call("update_agent_scope", {"project_id": project["id"], "expected_scope_version_id": project["scope_version_id"],
                "confirmed": True, "changes": {}}, "scope")
            research = await call("create_agent_research", {"project_id": project["id"], "expected_adopted_version_id": None}, "research")
            ref = {"project_id": project["id"], "research_id": research["research_id"]}

            async def context():
                return (await call("get_agent_context", ref))["context"]

            task = await call("start_semantic_analysis", {**ref, "expected_revision": (await context())["revision"],
                "agent_model": "controlled-host-fixture", "skill_version": "1"}, "analysis")
            for index in range(task["batch_count"]):
                batch = await call("get_semantic_batch", {"run_id": task["run_id"], "batch_index": index})
                tags = {"elements": ("Camera", "camera"), "methods": ("Landing", "autonomous landing"),
                        "scenarios": ("Platform", "moving platform")}
                rows = [{"id": row["id"], **{dimension: [{"label": label, "field": "abstract", "quote": quote,
                    "confidence": .9}] for dimension, (label, quote) in tags.items()}} for row in batch["records"]]
                await call("submit_semantic_batch", {"run_id": task["run_id"], "batch_index": index, "records": rows}, "batch" + str(index))
            for dimension in ("elements", "methods", "scenarios"):
                labels = (await call("get_semantic_taxonomy", {"run_id": task["run_id"], "dimension": dimension}))["labels"]
                await call("submit_semantic_taxonomy", {"run_id": task["run_id"], "dimension": dimension,
                    "themes": [{"label": dimension, "members": [row["id"] for row in labels]}]}, "taxonomy-" + dimension)
            analysis = await wait(await call("finish_semantic_analysis", {"run_id": task["run_id"]}, "finish"))
            check(analysis["processed_count"] == 3 and analysis["classified_count"] == 3, "Coverage mismatch")
            print("Passed external semantics, evidence and charts", flush=True)
            generated = await wait(await call("start_agent_task", {**ref, "expected_revision": (await context())["revision"],
                "action": "generate_report", "analysis_id": analysis["analysis_id"], "expected_current_version_id": None,
                "summary_text": "This is a synthetic three-application acceptance sample; candidate directions need verification."}, "report"))
            edited = await wait(await call("start_agent_task", {**ref, "expected_revision": (await context())["revision"],
                "action": "revise_report", "base_version_id": generated["report_version_id"],
                "expected_current_version_id": generated["report_version_id"],
                "operations": [{"op": "set_title", "text": "Portable Agent Acceptance Report"}]}, "edit"))
            exported = await wait(await call("start_agent_task", {**ref, "expected_revision": (await context())["revision"],
                "action": "export", "report_version_id": edited["report_version_id"]}, "export"))
            response = await http.get(exported["download_url"])
            check(response.status_code == 200, "Word download failed")
            word_bytes = len(response.content)
            document = Document(BytesIO(response.content))
            check(any("Portable Agent Acceptance Report" in p.text for p in document.paragraphs), "Revised title missing from Word")
            with ZipFile(BytesIO(response.content)) as archive:
                image_count = len([name for name in archive.namelist() if name.startswith("word/media/")])
            report = await mcp.call_tool("get_report_version", {"request": {"report_version_id": edited["report_version_id"]}})
            check(not report.is_error, "Report snapshot unavailable")
            expected_charts = sum(block["type"] == "chart" for block in report.structured_content["content"]["blocks"])
            check(len(document.inline_shapes) == expected_charts and image_count > 0, "Frozen report charts missing from Word")
            (output / "report.docx").write_bytes(response.content)
            response = await http.get("/api/projects/" + project["id"])
            check(response.json()["current_report_version_id"] == edited["report_version_id"], "Original HTTP project view differs")
            result.update(status="passed", analysis_id=analysis["analysis_id"], report_version_id=edited["report_version_id"],
                chart_count=expected_charts, word_bytes=word_bytes, tool_count=len((await mcp.list_tools()).tools))
            print("Passed project report publication, revision, frozen Word and HTTP reads", flush=True)
            save()
    except BaseException as error:
        result.update(status="failed", error=str(error))
        save()
        raise
    print(str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    asyncio.run(run(parser.parse_args().base_url.rstrip("/")))
