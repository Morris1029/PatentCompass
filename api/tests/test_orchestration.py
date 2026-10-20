import json
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import model_provider
from app.jobs import execute_run
from app.main import create_app
from app.models import (Dataset, DatasetVersion, Message, PatentApplication, PatentPublication,
                        ResearchRun, RunEvent, ScopeVersion, StoredFile)


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


def _ready_data(db_session, project):
    source = StoredFile(owner_id=project.owner_id, kind="raw", key="raw/plan.xlsx", original_name="plan.xlsx",
                        sha256="d" * 64, size=1, media_type="application/octet-stream")
    dataset = Dataset(owner_id=project.owner_id, name="Plan source", source_sha256="d" * 64)
    db_session.add_all([source, dataset])
    db_session.flush()
    version = DatasetVersion(dataset_id=dataset.id, source_file_id=source.id, status="ready", quality={"application_count": 1},
                             source_metadata={"partial_results": True})
    db_session.add(version)
    db_session.flush()
    patent = PatentApplication(dataset_version_id=version.id, application_number="PCT/PLAN/1", application_date=date(2024, 1, 1),
                               title="Unmanned aerial vehicle", abstract="An unmanned aerial vehicle navigates autonomously.",
                               applicants=["Plan Corp"], ipc_codes=["B64U10/00"], countries=["CN"], source_sheet="Plan", source_row=2)
    db_session.add(patent)
    db_session.flush()
    db_session.add(PatentPublication(application_id=patent.id, dataset_version_id=version.id,
                   publication_number="WO2024000123A1", publication_date=date(2024, 1, 1), kind_code="A1"))
    db_session.flush()
    return version


def _message(client, project, text="请分析现有专利样本"):
    response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
                           "content": text, "action": "auto", "semantic_mode": "off"}})
    assert response.status_code == 202, response.text
    return response.json()


def _plan(monkeypatch, action="analyze", **values):
    calls = []

    def chat(messages, tools=None):
        calls.append(messages)
        return {"content": json.dumps({"action": action, "reason": "Controlled routing", **values})}

    monkeypatch.setattr(model_provider, "chat", chat)
    return calls


def _confirm_execution(client, task):
    pending = client.get(f"/api/runs/{task['run_id']}").json()
    assert pending["input_request"]["kind"] == "execution_confirmation", pending
    accepted = client.post(f"/api/runs/{task['run_id']}/inputs", json={
        "input_request_id": pending["input_request"]["id"], "values": {"confirmed": True}})
    assert accepted.status_code == 202, accepted.text
    execute_run(task["run_id"])


def test_auto_plan_survives_dataset_and_scope_clarification(client, db_session, project, monkeypatch):
    version = _ready_data(db_session, project)
    scope = db_session.get(ScopeVersion, project.scope_version_id)
    scope.confirmed = False
    db_session.flush()
    calls = _plan(monkeypatch)
    sent = _message(client, project)
    execute_run(sent["run_id"])
    waiting = client.get(f"/api/runs/{sent['run_id']}").json()
    assert waiting["status"] == "waiting_input"
    assert waiting["input_request"]["kind"] == "dataset_selection"
    db_session.expire_all()
    assert db_session.get(ResearchRun, sent["run_id"]).payload["plan"]["action"] == "analyze"
    response = client.post(f"/api/runs/{sent['run_id']}/inputs", json={"input_request_id": waiting["input_request"]["id"],
                           "values": {"dataset_version_id": version.id}})
    assert response.status_code == 202, response.text
    execute_run(sent["run_id"])
    waiting = client.get(f"/api/runs/{sent['run_id']}").json()
    assert waiting["input_request"]["kind"] == "scope_confirmation"
    response = client.post(f"/api/runs/{sent['run_id']}/inputs", json={"input_request_id": waiting["input_request"]["id"],
                          "expected_scope_version_id": waiting["input_request"]["scope_version_id"], "values": {"confirmed": True}})
    assert response.status_code == 202, response.text
    execute_run(sent["run_id"])
    _confirm_execution(client, sent)
    done = client.get(f"/api/runs/{sent['run_id']}").json()
    assert done["status"] == "succeeded", done
    assert done["result"]["application_count"] == 1
    assert len(calls) == 1
    messages = client.get(f"/api/conversations/{sent['conversation_id']}/messages").json()["items"]
    assert any(message.get("cards") for message in messages)
    assert db_session.scalar(select(RunEvent.id).where(RunEvent.run_id == sent["run_id"], RunEvent.event_type == "plan.ready"))


def test_plan_range_change_requires_explicit_confirmation(client, db_session, project, monkeypatch):
    version = _ready_data(db_session, project)
    project.dataset_version_id = version.id
    db_session.flush()
    calls = _plan(monkeypatch, scope_changes={"start": 2024, "source_region": "中国"})
    sent = _message(client, project, "只分析2024年起的样本")
    execute_run(sent["run_id"])
    waiting = client.get(f"/api/runs/{sent['run_id']}").json()
    assert waiting["input_request"]["kind"] == "plan_confirmation"
    assert waiting["input_request"]["changes"] == {"start": 2024, "source_region": "中国"}
    rejected = client.post(f"/api/runs/{sent['run_id']}/inputs", json={"input_request_id": waiting["input_request"]["id"],
                           "expected_scope_version_id": project.scope_version_id, "values": {"confirmed": False}})
    assert rejected.status_code == 422
    accepted = client.post(f"/api/runs/{sent['run_id']}/inputs", json={"input_request_id": waiting["input_request"]["id"],
                           "expected_scope_version_id": project.scope_version_id, "values": {"confirmed": True}})
    assert accepted.status_code == 202, accepted.text
    execute_run(sent["run_id"])
    _confirm_execution(client, sent)
    done = client.get(f"/api/runs/{sent['run_id']}").json()
    assert done["status"] == "succeeded", done
    assert len(calls) == 1
    latest = client.get(f"/api/projects/{project.id}").json()
    assert latest["scope"]["content"]["start"] == 2024


def test_retry_keeps_persisted_plan_instead_of_rerouting(client, db_session, project, monkeypatch):
    from app import jobs
    version = _ready_data(db_session, project)
    project.dataset_version_id = version.id
    db_session.flush()
    calls = _plan(monkeypatch)
    original = jobs.run_analysis

    def fail(ctx):
        raise RuntimeError("Controlled transient failure")

    monkeypatch.setattr(jobs, "run_analysis", fail)
    sent = _message(client, project)
    execute_run(sent["run_id"])
    _confirm_execution(client, sent)
    assert client.get(f"/api/runs/{sent['run_id']}").json()["status"] == "failed"
    monkeypatch.setattr(jobs, "run_analysis", original)
    response = client.post(f"/api/runs/{sent['run_id']}/retry")
    assert response.status_code == 202, response.text
    retry_id = response.json()["run_id"]
    execute_run(retry_id)
    done = client.get(f"/api/runs/{retry_id}").json()
    assert done["status"] == "succeeded", done
    assert len(calls) == 1
    db_session.expire_all()
    retry = db_session.get(ResearchRun, retry_id)
    assert retry.retry_of_id == sent["run_id"]
    assert retry.payload["plan"]["action"] == "analyze"
    assert retry.dataset_version_ids == [version.id]


@pytest.mark.parametrize("plan", [
    {"action": "delete_all", "reason": "invalid tool"},
    {"action": "revise_chart", "reason": "invalid identifier", "chart_id": "not-a-uuid"},
])
def test_unconstrained_or_malformed_plan_does_not_execute(client, project, monkeypatch, plan):
    monkeypatch.setattr(model_provider, "chat", lambda messages: {"content": json.dumps(plan)})
    sent = _message(client, project, "修改图表")
    execute_run(sent["run_id"])
    result = client.get(f"/api/runs/{sent['run_id']}").json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESEARCH_PLAN"


def test_chart_target_cannot_cross_project(client, project, monkeypatch):
    _plan(monkeypatch, action="revise_chart", chart_id=str(uuid4()))
    sent = _message(client, project, "修改图表")
    execute_run(sent["run_id"])
    result = client.get(f"/api/runs/{sent['run_id']}").json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "CHART_TARGET_REQUIRED"


def test_report_request_reuses_existing_analysis_for_same_frozen_scope(client, db_session, project, context_factory, monkeypatch):
    from app import jobs, report_service
    version = _ready_data(db_session, project)
    project.dataset_version_id = version.id
    db_session.flush()
    existing = jobs.run_analysis(context_factory(project, "analyze", {}))
    _plan(monkeypatch, action="generate_report")

    def no_reanalysis(ctx):
        raise AssertionError("Existing current analysis should be reused")

    monkeypatch.setattr(jobs, "run_analysis", no_reanalysis)
    monkeypatch.setattr(report_service, "generate_report", lambda ctx: {"analysis_id": ctx.payload["analysis_id"], "kind": "report"})
    sent = _message(client, project, "根据现有研究生成导航报告")
    execute_run(sent["run_id"])
    _confirm_execution(client, sent)
    done = client.get(f"/api/runs/{sent['run_id']}").json()
    assert done["status"] == "succeeded", done
    assert done["result"]["analysis_id"] == existing["analysis_id"]
