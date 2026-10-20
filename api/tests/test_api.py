from io import BytesIO
from contextlib import contextmanager
from uuid import uuid4

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.data_service import HEADERS
from app.jobs import execute_run
from app.main import create_app
from app.models import Conversation, Project, ResearchRun


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


def source_bytes():
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(list(HEADERS))
    sheet.append(["WO2020CN12345", "WO2021000123A1;WO2021000123A9", "2020.01.02", "2021.01.07;2021.02.04",
                  "B64U10/13;G08G5/00", "ALPHA LTD; BETA LTD", "PERSON A", "UAV landing control",
                  "CN201910001234A 20190101", "2019.01.01", "", "CN", "An autonomous landing method."])
    sheet.append(["WO2024CN12346", "WO2025000124A1", "2024.01.02", "2025.01.09",
                  "B64U10/13", "ALPHA LTD", "PERSON B", "Unmanned aerial vehicle navigation",
                  "CN202310001234A 20230101", "2023.01.01", "", "CN", "An unmanned aerial vehicle navigation method."])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


@pytest.fixture
def api_dataset(client):
    project = client.post("/api/projects", json={"name": "API 样本项目", "scope": {"industry": "无人机", "start": 2020, "end": 2025}})
    assert project.status_code == 201, project.text
    project = project.json()
    imported = client.post("/api/datasets/imports", files={"file": ("sample.xlsx", source_bytes())},
                           data={"source_metadata": '{"query":"无人机","region":"中国","partial_results":true}'})
    assert imported.status_code == 202, imported.text
    imported = imported.json()
    not_ready = client.put(f"/api/projects/{project['id']}/datasets", json={"expected_revision": 0, "dataset_version_ids": [imported["dataset_version_id"]]})
    assert not_ready.status_code == 409
    execute_run(imported["run_id"])
    assert client.get(f"/api/runs/{imported['run_id']}").json()["status"] == "succeeded"
    bound = client.put(f"/api/projects/{project['id']}/datasets", json={"expected_revision": 0, "dataset_version_ids": [imported["dataset_version_id"]]})
    assert bound.status_code == 200, bound.text
    return {"project": bound.json(), "import": imported}


def confirm(client, project):
    result = client.patch(f"/api/projects/{project['id']}/scope", json={"expected_scope_version_id": project["scope_version_id"],
                          "changes": {}, "confirmed": True})
    assert result.status_code == 200, result.text
    return result.json()


def run(client, project_id, action, **payload):
    response = client.post(f"/api/projects/{project_id}/runs", json={"action": action, **payload})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    execute_run(run_id)
    status = client.get(f"/api/runs/{run_id}")
    assert status.status_code == 200, status.text
    return status.json()


def test_all_29_business_operations_have_openapi_contracts(client):
    document = client.get("/openapi.json").json()
    operations = [(path, method, operation) for path, methods in document["paths"].items() if path.startswith("/api/")
                  for method, operation in methods.items()]
    assert len(operations) == 44
    for path, method, operation in operations:
        assert "responses" in operation, (method, path)
        assert "422" in operation["responses"], (method, path)
    assert client.get("/health/live").json()["status"] == "ok"


def test_write_transaction_commits_before_response_starts(db_session, monkeypatch):
    from app import db
    order = []
    original = db.session_scope

    @contextmanager
    def tracked_session():
        with original() as session:
            yield session
        order.append("committed")

    monkeypatch.setattr(db, "session_scope", tracked_session)
    application = create_app(start_dispatcher=False)

    async def observed_app(scope, receive, send):
        async def observed_send(message):
            if message["type"] == "http.response.start":
                order.append("response_started")
            await send(message)
        await application(scope, receive, observed_send)

    with TestClient(observed_app) as client:
        response = client.post("/api/projects", json={"name": "Committed project"})
    assert response.status_code == 201
    assert order == ["committed", "response_started"]


def test_api_research_report_and_version_contract(client, api_dataset):
    project, imported = api_dataset["project"], api_dataset["import"]
    project_id = project["id"]
    confirm(client, project)
    assert client.get("/api/datasets").json()["returned_count"] == 1
    quality = client.get(f"/api/datasets/{imported['dataset_id']}/quality", params={"dataset_version_id": imported["dataset_version_id"]})
    assert quality.status_code == 200
    assert quality.json()["quality"]["publication_count"] == 3
    assert quality.json()["source_metadata"]["region"] == "中国"
    first_page = client.get(f"/api/projects/{project_id}/patents", params={"limit": 1}).json()
    second_page = client.get(f"/api/projects/{project_id}/patents", params={"limit": 1, "cursor": first_page["next_cursor"]}).json()
    assert first_page["total"] == 2
    assert first_page["items"][0]["id"] != second_page["items"][0]["id"]
    assert second_page["next_cursor"] is None
    detail = client.get(f"/api/projects/{project_id}/patents/{first_page['items'][0]['id']}")
    assert detail.status_code == 200 and detail.json()["publications"]
    year = client.get(f"/api/projects/{project_id}/patents", params={"start": 2024, "end": 2024}).json()
    assert year["total"] == 1 and year["items"][0]["application_date"].startswith("2024")
    for invalid in ({"start": -1}, {"end": 10000}, {"start": 2025, "end": 2020},
                    {"application_start": "2025-01-01", "application_end": "2020-12-31"}):
        assert client.get(f"/api/projects/{project_id}/patents", params=invalid).status_code == 422

    conversation = client.post(f"/api/projects/{project_id}/conversations", json={"initial_message": {"content": "分析当前样本", "action": "analyze"}})
    assert conversation.status_code == 202, conversation.text
    conversation = conversation.json()
    execute_run(conversation["run_id"])
    waiting = client.get(f"/api/runs/{conversation['run_id']}").json()
    assert waiting["input_request"]["kind"] == "execution_confirmation"
    assert client.post(f"/api/runs/{conversation['run_id']}/inputs", json={
        "input_request_id": waiting["input_request"]["id"], "values": {"confirmed": True}}).status_code == 202
    execute_run(conversation["run_id"])
    status = client.get(f"/api/runs/{conversation['run_id']}").json()
    assert status["status"] == "succeeded", status
    analysis_id = status["result"]["analysis_id"]
    assert len(client.get(f"/api/projects/{project_id}/conversations").json()["items"]) == 1
    messages = client.get(f"/api/conversations/{conversation['conversation_id']}/messages").json()
    assert messages["items"][0]["content"] == "分析当前样本"
    assert client.get(f"/api/projects/{project_id}/analysis").json()["status"] == "ready"
    chart = client.get(f"/api/charts/{status['result']['chart_ids'][0]}").json()
    assert chart["option"] and chart["provenance"]["scope_version_id"]
    drilldown = client.get(f"/api/projects/{project_id}/patents", params={"chart_id": chart["id"], "dimension_value": chart["data"]["rows"][0]["value"]})
    assert drilldown.status_code == 200 and drilldown.json()["total"] > 0
    evidence = client.get(f"/api/evidence/{status['result']['evidence_ids'][0]}")
    assert evidence.status_code == 200
    events = client.get(f"/api/runs/{conversation['run_id']}/events")
    assert events.status_code == 200 and "event: run.completed" in events.text
    resume = client.get(f"/api/runs/{conversation['run_id']}/events", headers={"Last-Event-ID": str(status["event_seq"])})
    assert resume.text == ""

    generated = run(client, project_id, "generate_report", analysis_id=analysis_id)
    assert generated["status"] == "succeeded", generated
    report_id, version_id = generated["result"]["report_id"], generated["result"]["report_version_id"]
    original = client.get(f"/api/reports/{report_id}/versions/{version_id}").json()
    block = next(block for block in original["content"]["blocks"] if block["type"] == "paragraph")
    revision = client.post(f"/api/reports/{report_id}/revisions", json={"base_version_id": version_id,
        "expected_current_version_id": version_id, "block_id": block["id"], "replacement_text": "只适用于当前导出样本。"})
    assert revision.status_code == 202, revision.text
    execute_run(revision.json()["run_id"])
    revised = client.get(f"/api/runs/{revision.json()['run_id']}").json()
    assert revised["status"] == "succeeded", revised
    new_version_id = revised["result"]["report_version_id"]
    versions = client.get(f"/api/reports/{report_id}/versions").json()
    assert len(versions["items"]) == 2
    assert versions["current_version_id"] == new_version_id
    assert client.get(f"/api/reports/{report_id}/versions/{version_id}").json()["content"] == original["content"]
    historical = client.put(f"/api/reports/{report_id}/current-version", json={"version_id": version_id, "expected_current_version_id": new_version_id})
    assert historical.status_code == 409 and historical.json()["code"] == "RESTORE_REQUIRED"
    restored_task = client.post(f"/api/research-versions/{revised['result']['research_id']}/restore", json={
        "revision_id": version_id, "expected_current_version_id": new_version_id,
        "expected_adopted_version_id": new_version_id})
    assert restored_task.status_code == 202, restored_task.text
    execute_run(restored_task.json()["run_id"])
    restored_run = client.get(f"/api/runs/{restored_task.json()['run_id']}").json()
    assert restored_run["status"] == "succeeded", restored_run
    restored_id = restored_run["result"]["report_version_id"]
    assert restored_id != version_id
    assert client.get(f"/api/reports/{report_id}/versions/{restored_id}").json()["content"] == original["content"]
    stale = client.put(f"/api/reports/{report_id}/current-version", json={"version_id": new_version_id, "expected_current_version_id": new_version_id})
    assert stale.status_code == 409
    export = client.post(f"/api/reports/{report_id}/exports", json={"report_version_id": version_id, "format": "docx"})
    assert export.status_code == 202
    cancelled = client.post(f"/api/runs/{export.json()['run_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    source = client.get(f"/api/files/{imported['source_file_id']}")
    assert source.status_code == 200 and source.content.startswith(b"PK")
    restored = client.get(f"/api/projects/{project_id}").json()
    assert restored["current_report_version_id"] == restored_id and restored["last_conversation_id"] == conversation["conversation_id"]


def test_idempotency_pagination_and_scope_conflicts(client, db_session):
    headers = {"Idempotency-Key": "create-project", "X-Request-ID": "api-test"}
    first = client.post("/api/projects", json={"name": "first"}, headers=headers)
    repeated = client.post("/api/projects", json={"name": "first"}, headers=headers)
    assert first.status_code == repeated.status_code == 201
    assert first.json()["id"] == repeated.json()["id"]
    assert first.headers["X-Request-ID"] == "api-test"
    conflict = client.post("/api/projects", json={"name": "changed"}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    client.post("/api/projects", json={"name": "second"})
    page = client.get("/api/projects", params={"limit": 1}).json()
    following = client.get("/api/projects", params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert page["items"][0]["id"] != following["items"][0]["id"]
    assert client.get("/api/projects", params={"cursor": "invalid"}).status_code == 422
    project = first.json()
    updated = confirm(client, project)
    stale = client.patch(f"/api/projects/{project['id']}/scope", json={"expected_scope_version_id": project["scope_version_id"], "changes": {"start": 2022}})
    assert stale.status_code == 409 and stale.json()["code"] == "SCOPE_VERSION_CONFLICT"
    invalid = client.patch(f"/api/projects/{project['id']}/scope", json={"expected_scope_version_id": updated["id"], "changes": {"start": 2025, "end": 2020}})
    assert invalid.status_code == 422
    empty = client.post(f"/api/projects/{project['id']}/conversations", json={"initial_message": {"content": "   "}})
    assert empty.status_code == 422
    assert db_session.scalar(select(func.count(Conversation.id))) == 0


def test_waiting_scope_confirmation_retry_and_missing_models(client, api_dataset, monkeypatch):
    project = api_dataset["project"]
    monkeypatch.setattr(get_settings(), "llm_base_url", "")
    monkeypatch.setattr(get_settings(), "embedding_base_url", "")
    pending = run(client, project["id"], "analyze")
    assert pending["status"] == "waiting_input"
    inputs = {"input_request_id": pending["input_request"]["id"], "expected_scope_version_id": project["scope_version_id"], "values": {"confirmed": False}}
    assert client.post(f"/api/runs/{pending['id']}/inputs", json=inputs).status_code == 422
    inputs["values"]["confirmed"] = True
    accepted = client.post(f"/api/runs/{pending['id']}/inputs", json=inputs)
    assert accepted.status_code == 202, accepted.text
    assert client.post(f"/api/runs/{pending['id']}/inputs", json=inputs).status_code == 409
    execute_run(pending["id"])
    assert client.get(f"/api/runs/{pending['id']}").json()["status"] == "succeeded"
    assert client.post(f"/api/runs/{pending['id']}/retry").status_code == 409
    chat = client.post(f"/api/projects/{project['id']}/conversations", json={"initial_message": {"content": "分析无人机", "action": "chat"}}).json()
    execute_run(chat["run_id"])
    failed = client.get(f"/api/runs/{chat['run_id']}").json()
    assert failed["status"] == "failed" and failed["error"]["code"] == "MODEL_NOT_CONFIGURED"
    retried = client.post(f"/api/runs/{chat['run_id']}/retry")
    assert retried.status_code == 202 and retried.json()["run_id"] != chat["run_id"]
    sent = client.post(f"/api/conversations/{chat['conversation_id']}/messages", json={"content": "继续分析"}, headers={"Idempotency-Key": "same-message"})
    again = client.post(f"/api/conversations/{chat['conversation_id']}/messages", json={"content": "继续分析"}, headers={"Idempotency-Key": "same-message"})
    assert sent.status_code == again.status_code == 202 and sent.json()["message_id"] == again.json()["message_id"]
    for mode in ("semantic", "hybrid"):
        search = client.get(f"/api/projects/{project['id']}/patents", params={"q": "降落", "mode": mode})
        assert search.status_code == 503 and search.json()["code"] == "MODEL_NOT_CONFIGURED"


def test_cross_project_and_foreign_owner_references_are_rejected(client, api_dataset, db_session):
    project = api_dataset["project"]
    confirm(client, project)
    analyzed = run(client, project["id"], "analyze")
    generated = run(client, project["id"], "generate_report", analysis_id=analyzed["result"]["analysis_id"])
    version_id = generated["result"]["report_version_id"]
    other = client.post("/api/projects", json={"name": "另一个项目"}).json()
    assert client.get(f"/api/projects/{other['id']}/patents", params={"analysis_id": analyzed["result"]["analysis_id"]}).status_code == 404
    assert client.get(f"/api/projects/{other['id']}/patents", params={"chart_id": analyzed["result"]["chart_ids"][0]}).status_code == 404
    referenced = client.post(f"/api/projects/{other['id']}/conversations", json={"initial_message": {"content": "引用", "reference": {"report_version_id": version_id}}})
    assert referenced.status_code == 404 and referenced.json()["code"] == "REFERENCE_NOT_FOUND"
    foreign = Project(owner_id="another-user", name="不能访问")
    db_session.add(foreign)
    db_session.flush()
    assert client.get(f"/api/projects/{foreign.id}").status_code == 404
    assert foreign.id not in {item["id"] for item in client.get("/api/projects").json()["items"]}
    assert client.get(f"/api/files/{uuid4()}").status_code == 404
    assert client.post("/api/datasets/imports", data={"path": "C:/private/file.xlsx"}).status_code == 422
    oversized_name = client.post("/api/datasets/imports", files={"file": ("sample.xlsx", source_bytes())}, data={"name": "n" * 301})
    assert oversized_name.status_code == 422


def test_semantic_and_hybrid_preserve_relevance_across_api_pages(client, api_dataset, monkeypatch):
    from app import data_service
    project = api_dataset["project"]
    confirm(client, project)
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_base_url", "https://test.invalid/v1")
    monkeypatch.setattr(settings, "embedding_api_key", "controlled-key")
    monkeypatch.setattr(settings, "embedding_model", "controlled-api-test")
    monkeypatch.setattr(data_service, "embed", lambda texts: [[1., 0.] if "landing" in text.lower() else [0., 1.] for text in texts])
    indexed = run(client, project["id"], "index")
    assert indexed["status"] == "succeeded", indexed
    for mode in ("semantic", "hybrid"):
        first = client.get(f"/api/projects/{project['id']}/patents", params={"mode": mode, "q": "landing", "limit": 1})
        assert first.status_code == 200, first.text
        first = first.json()
        second = client.get(f"/api/projects/{project['id']}/patents", params={"mode": mode, "q": "landing", "limit": 1,
                                                                             "cursor": first["next_cursor"]}).json()
        assert first["items"][0]["application_number"] == "WO2020CN12345"
        assert second["items"][0]["application_number"] == "WO2024CN12346"
        assert first["items"][0]["score"] > second["items"][0]["score"]
        assert first["scope_application_count"] == first["candidate_count"] == 2
        assert first["total"] is None and second["next_cursor"] is None


def test_cancelled_import_can_resume_same_version_without_duplicate_patents(client, db_session):
    from app.models import DatasetVersion, PatentApplication, PatentPublication, StoredFile
    content = source_bytes()
    initial = client.post("/api/datasets/imports", files={"file": ("sample.xlsx", content)}).json()
    cancelled = client.post(f"/api/runs/{initial['run_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    execute_run(initial["run_id"])
    assert db_session.scalar(select(func.count(PatentApplication.id))) == 0
    resumed = client.post("/api/datasets/imports", files={"file": ("sample.xlsx", content)})
    assert resumed.status_code == 202, resumed.text
    resumed = resumed.json()
    assert resumed["dataset_version_id"] == initial["dataset_version_id"]
    assert resumed["run_id"] != initial["run_id"] and resumed["reused"] is True
    run_row = db_session.get(ResearchRun, resumed["run_id"])
    assert run_row.retry_of_id == initial["run_id"]
    execute_run(resumed["run_id"])
    state = client.get(f"/api/runs/{resumed['run_id']}").json()
    assert state["status"] == "succeeded", state
    execute_run(resumed["run_id"])
    assert db_session.scalar(select(func.count(PatentApplication.id))) == 2
    assert db_session.scalar(select(func.count(PatentPublication.id))) == 3
    assert db_session.scalar(select(func.count(DatasetVersion.id))) == 1
    assert db_session.scalar(select(func.count(StoredFile.id))) == 1


def test_invalid_scope_filter_types_never_create_project_or_bad_scope_version(client, db_session):
    from app.models import ScopeVersion
    invalid_filters = [{"q": ["无人机"]}, {"q": {"term": "无人机"}}, {"applicant": ["ALPHA LTD"]},
                       {"ipc": {"prefix": "B64U"}}, {"q": "x" * 1001}]
    for filters in invalid_filters:
        response = client.post("/api/projects", json={"name": "无效范围", "scope": filters})
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "INVALID_SCOPE_FILTER"
    assert db_session.scalar(select(func.count(Project.id))) == 0
    assert db_session.scalar(select(func.count(ScopeVersion.id))) == 0
    project = client.post("/api/projects", json={"name": "有效范围", "scope": {"q": "无人机"}}).json()
    for filters in invalid_filters:
        response = client.patch(f"/api/projects/{project['id']}/scope", json={"expected_scope_version_id": project["scope_version_id"], "changes": filters})
        assert response.status_code == 422, response.text
        restored = client.get(f"/api/projects/{project['id']}").json()
        assert restored["scope_version_id"] == project["scope_version_id"]
        assert restored["scope"]["content"] == {"q": "无人机"}
    assert db_session.scalar(select(func.count(ScopeVersion.id))) == 1
