"""External reasoning drives the original project, evidence and version lifecycle."""

import asyncio
from copy import deepcopy
from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient
from mcp import Client
import pytest
from sqlalchemy import func, select

from app import agent_service as service, jobs, model_provider, task_service
from app.config import get_settings
from app.errors import AppError
from app.files import store_bytes
from app.main import create_app
from app.mcp_server import create_mcp
from app.models import (AnalysisResult, Dataset, DatasetVersion, PatentApplication,
                        PatentPublication, Project, ReportVersion)
from patentcompass_agent import PROMPTS, read_prompt


@pytest.fixture(autouse=True)
def no_generation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Portable workflows must use the host agent for generation")
    monkeypatch.setattr(model_provider, "chat", forbidden)
    monkeypatch.setattr(model_provider, "stream_chat", forbidden)
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)


@pytest.fixture
def source(db_session, project):
    file = store_bytes(db_session, b"portable controlled sample", "source.xlsx", "raw")
    dataset = Dataset(owner_id=get_settings().dev_owner_id, name="Portable sample", source_sha256=str(uuid4()))
    db_session.add(dataset)
    db_session.flush()
    version = DatasetVersion(dataset_id=dataset.id, source_file_id=file.id, status="ready")
    db_session.add(version)
    db_session.flush()
    for index in range(2):
        row = PatentApplication(dataset_version_id=version.id, application_number=f"EXAMPLE-{index}",
            application_date=date(2024, 1, 1), title="Camera landing",
            abstract="A camera supports autonomous landing on a moving platform.",
            applicants=["A", "B"], ipc_codes=["G01"], countries=["CN"], source_sheet="Sample", source_row=index + 2)
        db_session.add(row)
        db_session.flush()
        for suffix in range(index + 1):
            db_session.add(PatentPublication(application_id=row.id, dataset_version_id=version.id,
                publication_number=f"EXAMPLE-{index}-{suffix}A", publication_date=date(2025, 1, 1), kind_code="A"))
    project.dataset_version_id = version.id
    db_session.flush()
    return project


def call(session, name, request, key=None):
    return service.invoke(session, name, request, key or str(uuid4()))


def context(session, project, research=None):
    return call(session, "get_agent_context", {"project_id": project.id,
        **({"research_id": research} if research else {})})["context"]


def test_semantic_receipts_identify_saved_batches_and_next_missing_index(db_session, source):
    task = start(db_session, source)
    request = {"run_id": task["run_id"], "batch_index": 0}
    batch = call(db_session, "get_semantic_batch", request)
    assert batch["batch_status"] == "pending" and batch["next_batch_index"] == 0
    receipt = call(db_session, "submit_semantic_batch", {**request, "records": extraction(batch)})
    assert receipt["batch_status"] == "completed" and receipt["completed_batches"] == [0]
    assert receipt["next_batch_index"] is None and receipt["processed_count"] == receipt["total_count"] == 2
    restored = call(db_session, "get_semantic_batch", request)
    assert restored["batch_status"] == "completed" and restored["next_batch_index"] is None
    from app import tool_service
    brief = tool_service.invoke(db_session, "get_run", {"run_id": task["run_id"], "view": "progress"})
    full = tool_service.invoke(db_session, "get_run", {"run_id": task["run_id"]})
    assert brief["semantic_progress"]["processed_count"] == 2
    assert all("result" not in item for item in brief["steps"])
    assert any("result" in item for item in full["steps"])


def start(session, project, research=None):
    ctx = context(session, project, research)
    return call(session, "start_semantic_analysis", {"project_id": project.id,
        "research_id": research, "expected_revision": ctx["revision"],
        "agent_model": "controlled-host-model", "skill_version": "1"})


def extraction(batch):
    tags = {"elements": ("摄像头", "camera"), "methods": ("自主降落", "autonomous landing"),
            "scenarios": ("移动平台", "moving platform")}
    return [{"id": row["id"], **{dimension: [{"label": label, "quote": quote,
        "field": "abstract", "confidence": .9}] for dimension, (label, quote) in tags.items()}}
        for row in batch["records"]]


def submit_all(session, task):
    for index in range(task["batch_count"]):
        batch = call(session, "get_semantic_batch", {"run_id": task["run_id"], "batch_index": index})
        call(session, "submit_semantic_batch", {"run_id": task["run_id"], "batch_index": index, "records": extraction(batch)})
    for dimension in ("elements", "methods", "scenarios"):
        labels = call(session, "get_semantic_taxonomy", {"run_id": task["run_id"], "dimension": dimension})["labels"]
        call(session, "submit_semantic_taxonomy", {"run_id": task["run_id"], "dimension": dimension,
            "themes": [{"label": dimension, "members": [item["id"] for item in labels]}] if labels else []})
    return call(session, "finish_semantic_analysis", {"run_id": task["run_id"]})


def complete(session, task):
    session.flush()
    jobs.execute_run(task["run_id"])
    session.expire_all()
    value = task_service.get_run(session, task["run_id"])
    assert value["status"] == "succeeded", value
    return value["result"]


def test_external_analysis_report_revision_chart_restore_and_http_reads(db_session, source):
    project = source
    research = call(db_session, "create_agent_research", {"project_id": project.id,
        "expected_adopted_version_id": None})["research_id"]
    task = start(db_session, project, research)
    submitted = submit_all(db_session, task)
    analysis = complete(db_session, submitted)
    saved = db_session.get(AnalysisResult, analysis["analysis_id"])
    assert saved.summary["application_count"] == 2
    assert saved.summary["publication_count"] == 3
    assert saved.summary["semantic"]["processed_count"] == 2
    assert saved.summary["semantic"]["model"] == "controlled-host-model"
    assert analysis["chart_ids"] and analysis["evidence_ids"]
    ctx = context(db_session, project, research)
    generated = call(db_session, "start_agent_task", {"project_id": project.id, "research_id": research,
        "expected_revision": ctx["revision"], "action": "generate_report", "analysis_id": saved.id,
        "expected_current_version_id": None, "summary_text": "样本包含两条申请，候选方向仍需补检。"})
    result = complete(db_session, generated)
    version = db_session.get(ReportVersion, result["report_version_id"])
    assert version.research_id == research and version.scope_version_id == saved.scope_version_id
    assert version.dataset_version_ids == saved.dataset_version_ids
    assert any(block.get("origin") == "agent" for block in version.content["blocks"])
    assert version.content["evidence_snapshot"]
    with pytest.raises(AppError) as error:
        call(db_session, "start_agent_task", {"project_id": project.id, "research_id": research,
            "expected_revision": context(db_session, project, research)["revision"], "action": "generate_report",
            "expected_current_version_id": version.id, "expected_adopted_version_id": None})
    assert error.value.code == "REPORT_VERSION_CONFLICT"
    with TestClient(create_app(start_dispatcher=False)) as client:
        assert client.get(f"/api/projects/{project.id}").json()["current_report_version_id"] == version.id
        response = client.get(f"/api/reports/{result['report_id']}/versions/{version.id}")
        assert response.status_code == 200
    edit = call(db_session, "start_agent_task", {"project_id": project.id, "research_id": research,
        "expected_revision": context(db_session, project, research)["revision"], "action": "revise_report",
        "base_version_id": version.id, "expected_current_version_id": version.id,
        "operations": [{"op": "set_title", "text": "外部 Agent 导航报告"}]})
    edited = complete(db_session, edit)
    assert db_session.get(ReportVersion, edited["report_version_id"]).content["title"] == "外部 Agent 导航报告"
    chart = call(db_session, "get_agent_chart", {"chart_id": analysis["chart_ids"][0]})
    chart_task = call(db_session, "start_agent_task", {"project_id": project.id, "research_id": research,
        "expected_revision": context(db_session, project, research)["revision"], "action": "revise_chart",
        "chart_id": chart["id"], "base_version_id": chart["version_id"],
        "expected_current_version_id": chart["version_id"], "chart_mode": "insight", "interpretation": "此图仅描述导入样本。"})
    chart_result = complete(db_session, chart_task)
    new_chart = call(db_session, "get_agent_chart", {"chart_id": chart["id"]})
    assert new_chart["version_id"] == chart_result["chart_version_id"]
    assert new_chart["data_sha256"] == chart["data_sha256"]
    assert new_chart["interpretation"]["text"] == "此图仅描述导入样本。"
    restore = call(db_session, "start_agent_task", {"project_id": project.id, "research_id": research,
        "expected_revision": context(db_session, project, research)["revision"], "action": "restore_report",
        "report_version_id": version.id, "expected_current_version_id": edited["report_version_id"],
        "expected_adopted_version_id": edited["report_version_id"]})
    restored = complete(db_session, restore)
    assert restored["report_version_id"] not in {version.id, edited["report_version_id"]}
    assert db_session.get(ReportVersion, restored["report_version_id"]).restored_from_id == version.id


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra"])
def test_batch_coverage_is_exact_before_checkpoint(db_session, source, mutation):
    task = start(db_session, source)
    batch = call(db_session, "get_semantic_batch", {"run_id": task["run_id"]})
    rows = extraction(batch)
    if mutation == "missing": rows.pop()
    if mutation == "duplicate": rows[1] = deepcopy(rows[0])
    if mutation == "extra": rows.append({**rows[0], "id": "unknown"})
    with pytest.raises(AppError, match="必须恰好"):
        call(db_session, "submit_semantic_batch", {"run_id": task["run_id"], "records": rows})
    assert not call(db_session, "get_semantic_batch", {"run_id": task["run_id"]})["completed_batches"]


def test_unverified_and_low_confidence_labels_become_review_not_statistics(db_session, source):
    task = start(db_session, source)
    batch = call(db_session, "get_semantic_batch", {"run_id": task["run_id"]})
    rows = extraction(batch)
    for row in rows:
        row["methods"][0]["quote"] = "fabricated original evidence"
        row["elements"][0]["confidence"] = .4
    result = call(db_session, "submit_semantic_batch", {"run_id": task["run_id"], "records": rows})
    assert len(result["review"]) == 2
    for dimension in ("elements", "methods"):
        assert not call(db_session, "get_semantic_taxonomy", {"run_id": task["run_id"], "dimension": dimension})["labels"]


def test_progress_idempotency_cancel_and_source_change(db_session, source):
    request = {"project_id": source.id, "expected_revision": 0, "agent_model": "host", "skill_version": "1"}
    task = call(db_session, "start_semantic_analysis", request, "same-start")
    assert call(db_session, "start_semantic_analysis", request, "same-start") == task
    with pytest.raises(AppError) as error:
        call(db_session, "start_semantic_analysis", {**request, "agent_model": "other"}, "same-start")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    batch = call(db_session, "get_semantic_batch", {"run_id": task["run_id"]})
    body = {"run_id": task["run_id"], "records": extraction(batch)}
    first = call(db_session, "submit_semantic_batch", body, "same-batch")
    assert call(db_session, "submit_semantic_batch", body, "same-batch") == first
    assert call(db_session, "get_semantic_batch", {"run_id": task["run_id"]})["completed_batches"] == [0]
    task_service.cancel_run(db_session, task["run_id"])
    with pytest.raises(AppError) as error:
        call(db_session, "submit_semantic_batch", body)
    assert error.value.code == "AGENT_RUN_CLOSED"
    second = start(db_session, source)
    row = db_session.scalar(select(PatentApplication).where(PatentApplication.dataset_version_id == source.dataset_version_id))
    row.abstract += " Changed."
    db_session.flush()
    with pytest.raises(AppError) as error:
        call(db_session, "get_semantic_batch", {"run_id": second["run_id"]})
    assert error.value.code == "AGENT_SOURCE_CHANGED"


def test_context_change_prevents_external_analysis_overwriting_new_scope(db_session, source):
    task = submit_all(db_session, start(db_session, source))
    call(db_session, "update_agent_scope", {"project_id": source.id, "expected_scope_version_id": source.scope_version_id,
        "changes": {"q": "other"}, "confirmed": True})
    db_session.flush()
    jobs.execute_run(task["run_id"])
    db_session.expire_all()
    state = task_service.get_run(db_session, task["run_id"])
    assert state["status"] == "failed" and state["error"]["code"] == "AGENT_CONTEXT_CHANGED"
    assert source.current_analysis_id is None


def test_mcp_exposes_agent_schema_and_shared_business_errors(db_session, source):
    server, _ = create_mcp()
    async def verify():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(service.TOOLS) <= set(tools)
            assert "idempotency_key" in tools["submit_semantic_batch"].input_schema["required"]
            assert tools["get_semantic_batch"].annotations.read_only_hint
            result = await client.call_tool("get_agent_context", {"request": {"project_id": source.id}})
            assert not result.is_error and result.structured_content["project"]["id"] == source.id
            failure = await client.call_tool("get_agent_context", {"request": {"project_id": str(uuid4())}})
            assert failure.is_error
    asyncio.run(verify())


def test_shared_prompt_package_has_valid_unicode_and_no_application_dependency():
    assert len(PROMPTS) == 10
    for name in PROMPTS:
        prompt = read_prompt(name)
        assert prompt.strip() and "\ufffd" not in prompt


def test_multiple_batches_resume_and_retry_reuse_persisted_analysis(db_session, source, monkeypatch):
    for index in range(20):
        db_session.add(PatentApplication(dataset_version_id=source.dataset_version_id,
            application_number=f"EXAMPLE-EXTRA-{index}", application_date=date(2024, 1, 1), title="Camera landing",
            abstract="A camera supports autonomous landing on a moving platform.",
            applicants=["A"], ipc_codes=["G01"], countries=["CN"], source_sheet="Sample", source_row=index + 4))
    db_session.flush()
    task = start(db_session, source)
    assert task["batch_count"] == 2 and task["total_count"] == 22
    queued = submit_all(db_session, task)
    original = jobs._outcome_message
    monkeypatch.setattr(jobs, "_outcome_message", lambda *args: (_ for _ in ()).throw(RuntimeError("interrupted after checkpoint")))
    jobs.execute_run(queued["run_id"])
    db_session.expire_all()
    failed = task_service.get_run(db_session, task["run_id"])
    assert failed["status"] == "failed"
    monkeypatch.setattr(jobs, "_outcome_message", original)
    retry = task_service.retry_run(db_session, task["run_id"])
    result = complete(db_session, retry)
    assert db_session.get(AnalysisResult, result["analysis_id"]).summary["semantic"]["processed_count"] == 22
    assert db_session.scalar(select(func.count()).select_from(AnalysisResult).where(AnalysisResult.project_id == source.id)) == 1


def test_taxonomy_rejects_unmapped_or_duplicate_short_ids(db_session, source):
    task = start(db_session, source)
    batch = call(db_session, "get_semantic_batch", {"run_id": task["run_id"]})
    call(db_session, "submit_semantic_batch", {"run_id": task["run_id"], "records": extraction(batch)})
    for members, missing, duplicates, unknown in (([], ["e0"], [], []), (["e0", "e0"], [], ["e0"], []),
                                                  (["unknown"], ["e0"], [], ["unknown"])):
        with pytest.raises(AppError) as error:
            call(db_session, "submit_semantic_taxonomy", {"run_id": task["run_id"], "dimension": "elements",
                "themes": [{"label": "Theme", "members": members}] if members else []})
        assert error.value.code == "TAXONOMY_COVERAGE_MISMATCH"
        assert error.value.details == {"missing_ids": missing, "duplicate_ids": duplicates, "unknown_ids": unknown}
    with pytest.raises(AppError) as error:
        call(db_session, "finish_semantic_analysis", {"run_id": task["run_id"]})
    assert error.value.code == "TAXONOMY_INCOMPLETE"


def test_regrouping_reuses_frozen_extractions_and_review_reasons_without_generation(db_session, source):
    from app.models import ResearchRun, RunStep
    task = start(db_session, source)
    batch = call(db_session, "get_semantic_batch", {"run_id": task["run_id"]})
    records = extraction(batch)
    records[0]["methods"].append({"label": "invalid extra", "quote": "not in original", "field": "abstract", "confidence": .9})
    call(db_session, "submit_semantic_batch", {"run_id": task["run_id"], "records": records})

    def finish(identifier):
        for dimension in ("elements", "methods", "scenarios"):
            labels = call(db_session, "get_semantic_taxonomy", {"run_id": identifier, "dimension": dimension})["labels"]
            call(db_session, "submit_semantic_taxonomy", {"run_id": identifier, "dimension": dimension,
                "themes": [{"label": dimension, "members": [item["id"] for item in labels]}]})
        return complete(db_session, call(db_session, "finish_semantic_analysis", {"run_id": identifier}))

    original = finish(task["run_id"])
    request = {"project_id": source.id, "expected_revision": context(db_session, source)["revision"],
        "agent_model": "review-host", "skill_version": "1", "reuse_run_id": task["run_id"]}
    revised = call(db_session, "start_semantic_analysis", request)
    assert revised["processed_count"] == 2 and revised["next_batch_index"] is None
    old = db_session.scalar(select(RunStep).where(RunStep.run_id == task["run_id"], RunStep.key == "agent_batch_0"))
    new = db_session.scalar(select(RunStep).where(RunStep.run_id == revised["run_id"], RunStep.key == "agent_batch_0"))
    assert old.result == new.result and any(row["review_reasons"] for row in new.result.values())
    saved = finish(revised["run_id"])
    coverage = db_session.get(AnalysisResult, saved["analysis_id"]).summary["semantic"]
    assert coverage["review_count"] == 1 and coverage["cached_count"] == 2
    assert coverage["agent"]["extraction_source_run_id"] == task["run_id"]
    assert db_session.get(AnalysisResult, original["analysis_id"]).summary["semantic"]["review_count"] == 1
    request["expected_revision"] = context(db_session, source)["revision"]
    db_session.get(ResearchRun, task["run_id"]).owner_id = "other-owner"
    db_session.flush()
    with pytest.raises(AppError) as error:
        call(db_session, "start_semantic_analysis", request)
    assert error.value.status_code == 404
    db_session.get(ResearchRun, task["run_id"]).owner_id = get_settings().dev_owner_id
    patent = db_session.scalar(select(PatentApplication).where(PatentApplication.dataset_version_id == source.dataset_version_id))
    patent.abstract += " changed"
    db_session.flush()
    with pytest.raises(AppError) as error:
        call(db_session, "start_semantic_analysis", request)
    assert error.value.code == "SEMANTIC_REUSE_MISMATCH"


def test_ownership_and_stale_context_checks_precede_task_creation(db_session, source):
    foreign = Project(owner_id="other-owner", name="Other")
    db_session.add(foreign)
    db_session.flush()
    with pytest.raises(AppError) as error:
        call(db_session, "get_agent_context", {"project_id": foreign.id})
    assert error.value.status_code == 404
    with pytest.raises(AppError) as error:
        call(db_session, "start_semantic_analysis", {"project_id": source.id, "expected_revision": 999,
            "agent_model": "host", "skill_version": "1"})
    assert error.value.code == "AGENT_CONTEXT_CHANGED"


def test_prompt_edits_invalidate_semantic_cache_without_changing_python_versions(monkeypatch):
    import patentcompass_agent as agent
    from app.semantic_service import _cache_key
    from patentcompass_core.contracts import PatentRecord
    row = PatentRecord(id="example", title="Camera", abstract="Camera landing")
    original = agent.read_prompt
    before = _cache_key(row, "host")
    assert agent.prompt_version("semantic-extraction", "v1") == "v1"
    monkeypatch.setattr(agent, "read_prompt", lambda name: original(name) + "\nChanged method.")
    assert _cache_key(row, "host") != before
    assert agent.prompt_version("semantic-extraction", "v1").startswith("v1-")
