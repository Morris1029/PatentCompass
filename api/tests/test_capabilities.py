"""Shared capability contracts, isolated tasks and HTTP/MCP transport parity."""

import asyncio
from datetime import date
import hashlib
from io import BytesIO
import json
from uuid import uuid4

from docx import Document
from fastapi.testclient import TestClient
from mcp import Client
import pytest
from sqlalchemy import func, select

from app import capability_service, jobs, model_provider, task_service
from app.config import get_settings
from app.errors import AppError
from app.files import store_bytes
from app.main import create_app
from app.mcp_server import create_mcp
from app.models import (AnalysisResult, Conversation, Dataset, DatasetVersion, Message, PatentApplication,
                        PatentEmbedding, PatentPublication, Project, Report, ReportVersion, ResearchRun, ResearchVersion,
                        RunStep, StoredFile)
from patentcompass_core.statistics import compute_statistics


@pytest.fixture(autouse=True)
def no_heartbeat_thread(monkeypatch):
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


@pytest.fixture
def records():
    return [{"id": str(uuid4()), "application_number": "CNTEST1", "application_date": "2024-01-02",
             "title": "UAV autonomous landing", "abstract": "A camera supports autonomous landing on a moving platform.",
             "applicants": ["A", "B"], "ipc_codes": ["B64", "G08"], "countries": ["CN"],
             "source_sheet": "patents", "source_row": 2,
             "publications": [{"publication_number": "CNTEST1A"}, {"publication_number": "CNTEST1B"}]},
            {"id": str(uuid4()), "application_number": "CNTEST2", "application_date": "2023-02-03",
             "title": "Drone battery", "abstract": "Battery charging for drones.", "applicants": ["A"],
             "ipc_codes": ["B64"], "countries": ["US"], "source_sheet": "patents", "source_row": 3,
             "publications": [{"publication_number": "CNTEST2A"}]}]


def _side_effect_counts(session):
    return {model.__tablename__: session.scalar(select(func.count()).select_from(model)) for model in
            (Project, Conversation, Message, AnalysisResult, Report, ResearchVersion, ReportVersion)}


def _invoke(client, name, payload, key=None):
    return client.post(f"/api/capabilities/{name}/invoke", json=payload,
                       headers={"Idempotency-Key": key} if key else {})


def _complete(client, task):
    jobs.execute_run(task["run_id"])
    value = client.get(f"/api/runs/{task['run_id']}").json()
    assert value["status"] == "succeeded", value
    return value


@pytest.fixture
def dataset(db_session, records):
    content = b"controlled source"
    source = store_bytes(db_session, content, "patents.xlsx", "raw")
    dataset = Dataset(owner_id=get_settings().dev_owner_id, name="Capability source",
                      source_sha256=hashlib.sha256(content).hexdigest())
    db_session.add(dataset)
    db_session.flush()
    version = DatasetVersion(dataset_id=dataset.id, source_file_id=source.id, status="ready")
    db_session.add(version)
    db_session.flush()
    rows = []
    for record in records:
        values = {key: value for key, value in record.items() if key != "publications"}
        values.update(dataset_version_id=version.id, application_date=date.fromisoformat(record["application_date"]))
        row = PatentApplication(**values)
        db_session.add(row)
        db_session.flush()
        rows.append(row)
        for publication in record["publications"]:
            db_session.add(PatentPublication(application_id=row.id, dataset_version_id=version.id,
                publication_number=publication["publication_number"], publication_date=date(2025, 1, 1), kind_code="A"))
    db_session.flush()
    return dataset, version, rows


def test_directory_exposes_sixteen_typed_capabilities(client):
    response = client.get("/api/capabilities")
    assert response.status_code == 200, response.text
    items = {item["name"]: item for item in response.json()["items"]}
    assert set(items) == set(capability_service.CAPABILITIES) and len(items) == 16
    assert items["compute_statistics"]["execution"] == "inline"
    assert items["export_docx"]["execution"] == "task"
    assert all(item["input_schema"]["type"] == "object" for item in items.values())


@pytest.mark.parametrize("payload", [{}, {"records": [], "dataset_version_ids": [str(uuid4())]},
    {"records": [{"title": "landing"}], "dataset_version_ids": [str(uuid4())]},
    {"records": [{"id": "same"}, {"id": "same"}]}, {"dataset_version_ids": ["not-a-uuid"]}])
def test_patent_source_is_required_exclusive_and_typed(payload):
    with pytest.raises(AppError) as error:
        capability_service.validate("compute_statistics", payload)
    assert error.value.code == "VALIDATION_ERROR"


def test_unknown_name_and_invalid_search_options_fail_before_execution(client, records):
    assert _invoke(client, "unknown", {"records": records}).status_code == 404
    for changes in ({"q": "landing", "queries": ["landing"]}, {"mode": "sql"}, {"limit": 0},
                    {"q": "landing", "queries": ["other"]}, {"owner_id": "someone-else"}):
        assert _invoke(client, "search_patents", {"records": records, **changes}).status_code == 422


@pytest.mark.parametrize("scope", [{"start": "bad"}, {"application_start": "not-a-date"},
                                  {"start": 2025, "end": 2020}])
def test_invalid_scope_is_a_client_error(client, records, scope):
    response = _invoke(client, "compute_statistics", {"records": records, "scope": scope})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("rows", [[{"count": 1, "extra": "x"}], [{"label": "A", "count": True}],
                                 [{"label": "A", "count": float("inf")}], [{"label": "A"}]])
def test_chart_rows_require_labels_and_finite_non_boolean_counts(db_session, rows):
    with pytest.raises(AppError) as error:
        capability_service.invoke(db_session, "build_chart_options", {"rows": rows})
    assert error.value.status_code == 422


def test_statistics_report_and_word_export_need_no_project_or_model(client, db_session, records):
    before = _side_effect_counts(db_session)
    statistics = _invoke(client, "compute_statistics", {"records": records})
    assert statistics.status_code == 200, statistics.text
    assert statistics.json() == compute_statistics(records)
    request = {"records": records, "title": "Independent sample report"}
    task = _invoke(client, "build_report", request, "independent-report")
    assert task.status_code == 202, task.text
    same = _invoke(client, "build_report", request, "independent-report")
    assert same.json()["run_id"] == task.json()["run_id"]
    generated = _complete(client, task.json())
    assert generated["project_id"] is None and generated["conversation_id"] is None
    content = generated["result"]["content"]
    assert content["title"] == request["title"]
    assert content["source"]["summary"]["application_count"] == 2
    assert content["source"]["summary"]["publication_count"] == 3
    export = _invoke(client, "export_docx", {"content": content, "filename": "independent.docx"}, "independent-export")
    assert export.status_code == 202, export.text
    exported = _complete(client, export.json())
    file = db_session.get(StoredFile, exported["result"]["file_id"])
    assert file.project_id is None and file.run_id == exported["id"]
    downloaded = client.get(exported["result"]["download_url"])
    assert downloaded.status_code == 200 and downloaded.content.startswith(b"PK")
    document = Document(BytesIO(downloaded.content))
    assert content["title"] in "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert _side_effect_counts(db_session) == before


def test_async_submissions_require_keys_and_reject_changed_payloads(client, records):
    request = {"records": records}
    assert _invoke(client, "build_report", request).status_code == 422
    first = _invoke(client, "build_report", request, "same-key")
    assert first.status_code == 202
    conflict = _invoke(client, "build_report", {**request, "title": "Changed"}, "same-key")
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_generic_upload_and_word_parser_share_file_references_without_projects(client, db_session):
    before = _side_effect_counts(db_session)
    document = Document()
    document.add_paragraph("独立研究资料")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "来源证据"
    stream = BytesIO()
    document.save(stream)
    payload = stream.getvalue()
    headers = {"Idempotency-Key": "standalone-upload"}
    uploaded = client.post("/api/capabilities/files", files={"file": ("source.docx", payload)}, headers=headers)
    assert uploaded.status_code == 201, uploaded.text
    repeated = client.post("/api/capabilities/files", files={"file": ("source.docx", payload)}, headers=headers)
    assert repeated.json()["file_id"] == uploaded.json()["file_id"]
    task = _invoke(client, "parse_document", {"file_id": uploaded.json()["file_id"]}, "standalone-parse")
    assert task.status_code == 202, task.text
    parsed = _complete(client, task.json())["result"]
    assert parsed["source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert [block["text"] for block in parsed["blocks"]] == ["独立研究资料", "来源证据"]
    assert parsed["blocks"][0]["location"] == {"paragraph": 1}
    assert client.get(uploaded.json()["download_url"]).content == payload
    assert _invoke(client, "parse_document", {"path": "C:/private.docx"}, "arbitrary-path").status_code == 422
    assert _side_effect_counts(db_session) == before


def test_dataset_reference_checks_ownership_and_freezes_selected_records(client, db_session, dataset):
    owner, version, rows = dataset
    payload = {"dataset_version_ids": [version.id], "scope": {"start": 2024}}
    selected = _invoke(client, "compute_statistics", payload)
    assert selected.status_code == 200 and selected.json()["summary"]["application_count"] == 1
    task = _invoke(client, "build_report", payload, "frozen-dataset")
    assert task.status_code == 202, task.text
    run = db_session.get(ResearchRun, task.json()["run_id"])
    assert run.payload["source_dataset_version_ids"] == [version.id]
    assert run.payload["input"]["dataset_version_ids"] is None
    assert [record["id"] for record in run.payload["input"]["records"]] == [rows[0].id]
    original_title = rows[0].title
    rows[0].title = "MUTATED AFTER SUBMISSION"
    db_session.flush()
    completed = _complete(client, task.json())
    snapshot = completed["result"]["content"]["representative_snapshot"]
    assert len(snapshot) == 1 and snapshot[0]["title"] == original_title
    owner.owner_id = "other-owner"
    db_session.flush()
    assert _invoke(client, "compute_statistics", payload).status_code == 404
    assert _invoke(client, "get_patent", {"patent_id": rows[0].id}).status_code == 404


def test_not_ready_dataset_cannot_be_submitted(client, db_session, dataset):
    _, version, _ = dataset
    version.status = "pending"
    db_session.flush()
    response = _invoke(client, "build_report", {"dataset_version_ids": [version.id]}, "not-ready")
    assert response.status_code == 409 and response.json()["code"] == "DATASET_NOT_READY"
    assert db_session.scalar(select(func.count()).select_from(ResearchRun)) == 0


def test_embedding_task_updates_only_explicit_dataset_index(client, db_session, dataset, monkeypatch):
    _, version, rows = dataset
    before = _side_effect_counts(db_session)
    monkeypatch.setattr(get_settings(), "embedding_model", "capability-test")
    monkeypatch.setattr(get_settings(), "embedding_base_url", "https://controlled.invalid/v1")
    monkeypatch.setattr(get_settings(), "embedding_api_key", "controlled-key")
    monkeypatch.setattr(model_provider, "embed", lambda texts: [[1., 0.] if "landing" in text else [0., 1.] for text in texts])
    task = _invoke(client, "build_embeddings", {"dataset_version_ids": [version.id]}, "index-explicit-source")
    assert task.status_code == 202, task.text
    completed = _complete(client, task.json())
    assert completed["result"]["indexed_count"] == 2
    indexes = list(db_session.scalars(select(PatentEmbedding).where(PatentEmbedding.model == "capability-test")))
    assert {index.application_id for index in indexes} == {row.id for row in rows}
    search = _invoke(client, "search_patents", {"dataset_version_ids": [version.id], "q": "landing", "mode": "semantic"})
    assert search.status_code == 200, search.text
    assert search.json()["items"][0]["id"] == rows[0].id
    assert _side_effect_counts(db_session) == before


def test_http_and_mcp_use_one_idempotency_record_and_the_same_statistics(client, db_session, records):
    first = _invoke(client, "build_report", {"records": records}, "cross-protocol-key")
    assert first.status_code == 202, first.text
    server, _ = create_mcp()

    async def verify():
        async with Client(server) as mcp:
            tools = await mcp.list_tools()
            from app.agent_service import TOOLS
            assert len(tools.tools) == 23 + len(TOOLS)
            result = await mcp.call_tool("build_report", {"request": {"records": records},
                                                         "idempotency_key": "cross-protocol-key"})
            assert not result.is_error, result
            assert result.structured_content["run_id"] == first.json()["run_id"]
            statistics = await mcp.call_tool("compute_statistics", {"request": {"records": records}})
            assert not statistics.is_error
            assert statistics.structured_content == compute_statistics(records)

    asyncio.run(verify())
    assert db_session.scalar(select(func.count()).select_from(ResearchRun)) == 1


def test_standalone_cancel_and_retry_keep_projects_and_research_untouched(client, db_session, records):
    before = _side_effect_counts(db_session)
    cancelled_task = _invoke(client, "build_report", {"records": records}, "cancel-report").json()
    cancelled = client.post(f"/api/runs/{cancelled_task['run_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    jobs.execute_run(cancelled_task["run_id"])
    assert client.get(f"/api/runs/{cancelled_task['run_id']}").json()["status"] == "cancelled"
    assert client.post(f"/api/runs/{cancelled_task['run_id']}/retry").status_code == 409
    missing_model = _invoke(client, "classify_patents", {"records": records}, "no-model").json()
    jobs.execute_run(missing_model["run_id"])
    failed = client.get(f"/api/runs/{missing_model['run_id']}").json()
    assert failed["status"] == "failed" and failed["error"]["code"] == "MODEL_NOT_CONFIGURED"
    first = client.post(f"/api/runs/{missing_model['run_id']}/retry", headers={"Idempotency-Key": "retry-key"})
    assert first.status_code == 202, first.text
    server, _ = create_mcp()

    async def verify():
        async with Client(server) as mcp:
            result = await mcp.call_tool("retry_run", {"run_id": missing_model["run_id"], "idempotency_key": "retry-key"})
            assert not result.is_error and result.structured_content["run_id"] == first.json()["run_id"]
            cancelled = await mcp.call_tool("cancel_run", {"run_id": first.json()["run_id"], "idempotency_key": "cancel-retry"})
            assert not cancelled.is_error and cancelled.structured_content["status"] == "cancelled"

    asyncio.run(verify())
    retried = db_session.get(ResearchRun, first.json()["run_id"])
    assert retried.project_id is None and retried.scope_version_id is None and retried.conversation_id is None
    assert retried.payload["input"] == db_session.get(ResearchRun, missing_model["run_id"]).payload["input"]
    assert _side_effect_counts(db_session) == before


def test_raw_model_continuation_never_leaves_public_task_or_event_views(client, db_session, records):
    task = _invoke(client, "build_report", {"records": records}, "private-state").json()
    run = db_session.get(ResearchRun, task["run_id"])
    private = {"visible": "public", "nested": [{"_response_items": [{"encrypted_content": "PRIVATE_CONTINUATION"}],
                                                "_generation_protocol": "responses-v1"}]}
    run.status, run.result = "succeeded", private
    db_session.add(RunStep(run_id=run.id, key="private-checkpoint", status="succeeded", fingerprint="a" * 64, result=private))
    jobs.emit_event(db_session, run.id, "run.completed", private)
    db_session.flush()
    public_run = client.get(f"/api/runs/{run.id}")
    public_events = client.get(f"/api/runs/{run.id}/events")
    direct = task_service.get_run(db_session, run.id)
    server, _ = create_mcp()

    async def verify():
        async with Client(server) as mcp:
            result = await mcp.call_tool("get_run", {"run_id": run.id})
            events = await mcp.call_tool("get_run_events", {"run_id": run.id})
            assert not result.is_error and not events.is_error
            return result.structured_content, events.structured_content

    exposed = public_run.text + public_events.text + json.dumps([direct, asyncio.run(verify())])
    assert public_run.status_code == public_events.status_code == 200
    assert "PRIVATE_CONTINUATION" not in exposed and "_response_items" not in exposed
    assert "_generation_protocol" not in exposed and "public" in exposed


def test_queued_model_task_rejects_configuration_change(client, db_session, records, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_model", "first-model")
    task = _invoke(client, "classify_patents", {"records": records}, "frozen-model").json()
    monkeypatch.setattr(get_settings(), "llm_model", "different-model")
    monkeypatch.setattr(model_provider, "chat", lambda *args, **kwargs: pytest.fail("Changed model must not be called"))
    jobs.execute_run(task["run_id"])
    state = client.get(f"/api/runs/{task['run_id']}").json()
    assert state["status"] == "failed" and state["error"]["code"] == "MODEL_CONFIGURATION_CHANGED"


def test_retry_reuses_artifact_committed_before_checkpoint(client, db_session, records):
    from types import SimpleNamespace
    from app import capability_service
    task = _invoke(client, "build_report", {"records": records}, "artifact-lineage").json()
    run = db_session.get(ResearchRun, task["run_id"])
    first = capability_service._artifact(db_session, b"committed output", "report.docx", SimpleNamespace(run_id=run.id))
    run.status = "failed"
    db_session.flush()
    retried = client.post(f"/api/runs/{run.id}/retry").json()
    repeated = capability_service._artifact(db_session, b"recomputed output", "report.docx",
                                              SimpleNamespace(run_id=retried["run_id"]))
    assert repeated["file_id"] == first["file_id"]
    assert db_session.scalar(select(func.count()).select_from(StoredFile)) == 1


def test_material_retry_rebinds_the_material_task(client, db_session, project, context_factory):
    from app.models import Material
    context = context_factory(project, "parse_material")
    source = store_bytes(db_session, b"retained source", "source.docx", "raw", project_id=project.id)
    material = Material(project_id=project.id, file_id=source.id, run_id=context.run_id, status="failed")
    db_session.add(material)
    db_session.flush()
    old = db_session.get(ResearchRun, context.run_id)
    old.payload, old.status = {"material_id": material.id}, "failed"
    db_session.flush()
    response = client.post(f"/api/runs/{old.id}/retry")
    assert response.status_code == 202
    db_session.refresh(material)
    assert material.run_id == response.json()["run_id"] and material.status == "parsing"


def test_invalid_report_operation_is_rejected_before_queueing(client, db_session):
    response = _invoke(client, "revise_report_content", {
        "content": {"title": "Draft", "blocks": [{"id": "p", "type": "paragraph", "text": "Source"}]},
        "operations": [{}]}, "invalid-operation")
    assert response.status_code == 422
    assert db_session.scalar(select(func.count()).select_from(ResearchRun)) == 0


def test_classification_composes_into_independent_semantic_report(client, records, monkeypatch):
    def generate(messages, tools=None):
        value = json.loads(messages[-1]["content"])
        if "records" in value:
            result = {"records": [{"id": row["id"], **{dimension: [{"label": dimension, "field": "abstract",
                "quote": row["abstract"], "confidence": 0.9}] for dimension in ("elements", "methods", "scenarios")}}
                for row in value["records"]]}
        else:
            dimension = next(iter(value))
            result = {dimension: [{"label": dimension, "members": [row["id"] for row in value[dimension]]}]}
        return {"content": json.dumps(result)}

    monkeypatch.setattr(model_provider, "chat", generate)
    classified = _complete(client, _invoke(client, "classify_patents", {"records": records}, "compose-classify").json())
    task = _invoke(client, "build_report", {"records": records, "analysis": classified["result"]}, "compose-report")
    content = _complete(client, task.json())["result"]["content"]
    assert content["source"]["summary"]["semantic"]["processed_count"] == len(records)
    assert any(block.get("text") == "全量语义分类与技术结构" for block in content["blocks"])
    assert sum(block["type"] == "chart" for block in content["blocks"]) == 6
    assert len(content["evidence_snapshot"]) == len(records)
    changed = [{**records[0], "title": "Changed source"}, records[1]]
    rejected = _invoke(client, "build_report", {"records": changed, "analysis": classified["result"]}, "compose-stale-source")
    jobs.execute_run(rejected.json()["run_id"])
    state = client.get(f"/api/runs/{rejected.json()['run_id']}").json()
    assert state["status"] == "failed" and state["error"]["status_code"] == 422


def test_independent_chart_revision_requires_a_unique_frozen_snapshot(client):
    chart_id, first_version, second_version = str(uuid4()), str(uuid4()), str(uuid4())
    snapshot = {"id": chart_id, "version_id": first_version, "kind": "yearly",
                "data": {"title": "Explicit chart"}, "option": {"series": [{"type": "bar", "data": [2]}]}}
    payload = {"content": {"title": "Draft", "blocks": [{"id": "p", "type": "paragraph", "text": "Evidence"}]},
               "operations": [{"op": "insert_chart", "chart_id": chart_id, "after_id": "p"}],
               "charts": [snapshot, {**snapshot, "version_id": second_version}]}
    assert _invoke(client, "revise_report_content", payload, "ambiguous-chart").status_code == 422
    payload["operations"][0]["chart_version_id"] = first_version
    response = _invoke(client, "revise_report_content", payload, "explicit-chart")
    assert response.status_code == 202, response.text
    content = _complete(client, response.json())["result"]["content"]
    assert content["blocks"][-1]["chart_version_id"] == first_version
    assert content["blocks"][-1]["chart"]["option"] == snapshot["option"]
