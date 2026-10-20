"""Editable scope confirmations and content-based report change summaries."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app import data_service, jobs, model_provider, report_service
from app.config import get_settings
from app.main import create_app
from app.models import Chart, Report, ReportVersion, ResearchRun, RunEvent, ScopeVersion, new_id
from test_orchestration import _message, _plan, _ready_data
from test_reports import report_analysis


@pytest.fixture
def client(db_session, monkeypatch):
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


def _status(client, run_id):
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _submit(client, pending, scope=None):
    values = {"confirmed": True}
    if scope is not None:
        values["scope"] = scope
    return client.post(f"/api/runs/{pending['id']}/inputs", json={
        "input_request_id": pending["input_request"]["id"],
        "expected_scope_version_id": pending["scope_version_id"], "values": values})


def _waiting(client, db_session, project, monkeypatch, kind):
    version = _ready_data(db_session, project)
    project.dataset_version_id = version.id
    scope = db_session.get(ScopeVersion, project.scope_version_id)
    scope.content = {"industry": "无人机", "goal": "原有目标", "q": "drone", "start": 2020}
    scope.confirmed = kind != "scope_confirmation"
    db_session.flush()
    _plan(monkeypatch, scope_changes={"goal": "计划目标", "start": 2021}
          if kind == "plan_confirmation" else {})
    sent = _message(client, project)
    jobs.execute_run(sent["run_id"])
    pending = _status(client, sent["run_id"])
    assert pending["input_request"]["kind"] == kind, pending
    return pending


@pytest.mark.parametrize("kind", ["scope_confirmation", "plan_confirmation", "execution_confirmation"])
def test_all_scope_confirmations_return_full_editable_values(client, db_session, project, monkeypatch, kind):
    pending = _waiting(client, db_session, project, monkeypatch, kind)
    scope = pending["input_request"]["scope"]
    assert scope["industry"] == "无人机" and scope["q"] == "drone"
    assert scope["goal"] == ("计划目标" if kind == "plan_confirmation" else "原有目标")
    assert scope["start"] == (2021 if kind == "plan_confirmation" else 2020)


def test_existing_execution_confirmation_gets_editable_scope_without_recreating_the_request(
        client, db_session, project, monkeypatch):
    pending = _waiting(client, db_session, project, monkeypatch, "execution_confirmation")
    run = db_session.get(ResearchRun, pending["id"])
    original_id = run.input_request["id"]
    run.input_request = {key: value for key, value in run.input_request.items()
                         if key not in {"scope", "scope_editable"}}
    db_session.flush()
    restored = _status(client, run.id)["input_request"]
    assert restored["id"] == original_id and restored["scope_editable"] is True
    assert restored["scope"]["industry"] == "无人机" and restored["scope"]["start"] == 2020


@pytest.mark.parametrize("kind", ["scope_confirmation", "plan_confirmation"])
def test_edited_scope_values_override_proposed_changes_and_normalize_years(
        client, db_session, project, monkeypatch, kind):
    pending = _waiting(client, db_session, project, monkeypatch, kind)
    edited = {**pending["input_request"]["scope"], "goal": "用户最终目标", "q": "", "start": "2022", "end": "2025"}
    accepted = _submit(client, pending, edited)
    assert accepted.status_code == 202, accepted.text
    db_session.expire_all()
    run = db_session.get(ResearchRun, pending["id"])
    adopted = db_session.get(ScopeVersion, run.scope_version_id)
    assert adopted.id != pending["scope_version_id"] and adopted.confirmed
    assert adopted.content == {**edited, "start": 2022, "end": 2025}
    assert db_session.get(type(project), project.id).scope_version_id == adopted.id
    assert run.status == "queued" and run.input_request is None


def test_plan_confirmation_without_edited_values_keeps_proposed_changes(
        client, db_session, project, monkeypatch):
    pending = _waiting(client, db_session, project, monkeypatch, "plan_confirmation")
    accepted = _submit(client, pending)
    assert accepted.status_code == 202, accepted.text
    db_session.expire_all()
    run = db_session.get(ResearchRun, pending["id"])
    adopted = db_session.get(ScopeVersion, run.scope_version_id)
    assert adopted.content["goal"] == "计划目标" and adopted.content["start"] == 2021
    assert adopted.content["industry"] == "无人机" and adopted.content["q"] == "drone"


@pytest.mark.parametrize("action", ["analyze", "generate_report"])
def test_execution_scope_edit_discards_old_analysis_and_executes_once_with_confirmed_scope(
        client, db_session, project, context_factory, report_analysis, monkeypatch, action):
    _plan(monkeypatch, action=action)
    observed = []
    analysis_id = new_id()

    def analyze(ctx):
        observed.append((ctx.scope_version_id, dict(ctx.payload)))
        return {"analysis_id": analysis_id, "application_count": 1}

    monkeypatch.setattr(jobs, "run_analysis", analyze)
    monkeypatch.setattr(report_service, "generate_report", lambda ctx: {
        "report_id": new_id(), "report_version_id": new_id(), "analysis_id": ctx.payload["analysis_id"]})
    sent = _message(client, project)
    jobs.execute_run(sent["run_id"])
    pending = _status(client, sent["run_id"])
    assert pending["input_request"]["kind"] == "execution_confirmation", pending
    edited = {**pending["input_request"]["scope"], "goal": "仅研究着陆", "q": "landing", "start": "2023"}
    accepted = _submit(client, pending, edited)
    assert accepted.status_code == 202, accepted.text
    db_session.expire_all()
    run = db_session.get(ResearchRun, sent["run_id"])
    assert run.scope_version_id != report_analysis.scope_version_id
    assert run.payload.get("analysis_id") is None
    confirmed_scope = run.scope_version_id
    assert db_session.get(ScopeVersion, confirmed_scope).content["start"] == 2023
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert len(observed) == 1 and observed[0][0] == confirmed_scope
    assert observed[0][1].get("analysis_id") is None
    assert len(list(db_session.scalars(select(RunEvent).where(
        RunEvent.run_id == sent["run_id"], RunEvent.event_type == "input.required")))) == 1


def test_search_uses_scope_edited_in_execution_confirmation_without_asking_again(
        client, db_session, project, report_analysis, monkeypatch):
    for field in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(get_settings(), field, "controlled")
    searches = []

    def model(messages, tools=None):
        if messages[-1]["role"] == "user":
            return {"content": None, "tool_calls": [{"id": "scope-search", "type": "function", "function": {
                "name": "search_patents", "arguments": json.dumps({"q": "landing"})}}]}
        return {"content": "当前范围未检索到相关样本。"}

    def search(session, supplied_project, filters):
        searches.append(filters)
        return {"items": [], "scope_application_count": 1}

    def stream(messages, tools=None, on_delta=None):
        response = model(messages, tools)
        if response.get("content") and on_delta:
            on_delta(response["content"])
        return response

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(model_provider, "stream_chat", stream)
    monkeypatch.setattr(data_service, "search_patents", search)
    response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
        "content": "检索着陆技术", "action": "chat"}})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    jobs.execute_run(run_id)
    pending = _status(client, run_id)
    assert pending["input_request"]["kind"] == "execution_confirmation"
    accepted = _submit(client, pending, {**pending["input_request"]["scope"], "q": "landing", "start": "2023"})
    assert accepted.status_code == 202, accepted.text
    jobs.execute_run(run_id)
    done = _status(client, run_id)
    assert done["status"] == "succeeded", done
    assert len(searches) == 1 and searches[0]["scope_version_id"] == done["scope_version_id"]
    assert searches[0]["scope_version_id"] != report_analysis.scope_version_id
    assert "application_ids" not in searches[0]
    assert len(list(db_session.scalars(select(RunEvent).where(
        RunEvent.run_id == run_id, RunEvent.event_type == "input.required")))) == 1


@pytest.mark.parametrize("kind", ["scope_confirmation", "plan_confirmation", "execution_confirmation"])
def test_invalid_edited_year_keeps_pending_confirmation_and_creates_no_scope(
        client, db_session, project, monkeypatch, kind):
    pending = _waiting(client, db_session, project, monkeypatch, kind)
    count = db_session.scalar(select(func.count()).select_from(ScopeVersion).where(ScopeVersion.project_id == project.id))
    response = _submit(client, pending, {**pending["input_request"]["scope"], "start": "not-a-year"})
    assert response.status_code == 422, response.text
    unchanged = _status(client, pending["id"])
    assert unchanged["status"] == "waiting_input"
    assert unchanged["input_request"]["id"] == pending["input_request"]["id"]
    db_session.expire_all()
    assert db_session.get(ResearchRun, pending["id"]).scope_version_id == pending["scope_version_id"]
    assert db_session.scalar(select(func.count()).select_from(ScopeVersion).where(ScopeVersion.project_id == project.id)) == count


@pytest.mark.parametrize("kind", ["scope_confirmation", "plan_confirmation", "execution_confirmation"])
def test_stale_edit_does_not_overwrite_concurrently_confirmed_scope(
        client, db_session, project, monkeypatch, kind):
    pending = _waiting(client, db_session, project, monkeypatch, kind)
    concurrent = client.patch(f"/api/projects/{project.id}/scope", json={
        "expected_scope_version_id": pending["scope_version_id"], "changes": {"goal": "并发确认目标"}, "confirmed": True})
    assert concurrent.status_code == 200, concurrent.text
    count = db_session.scalar(select(func.count()).select_from(ScopeVersion).where(ScopeVersion.project_id == project.id))
    response = _submit(client, pending, {**pending["input_request"]["scope"], "goal": "旧表单覆盖目标"})
    assert response.status_code in {202, 409}, response.text
    latest = _status(client, pending["id"])
    assert latest["status"] == "waiting_input"
    if response.status_code == 202:
        assert latest["input_request"]["id"] != pending["input_request"]["id"]
    current = client.get(f"/api/projects/{project.id}").json()
    assert current["scope"]["id"] == concurrent.json()["id"]
    assert current["scope"]["content"]["goal"] == "并发确认目标"
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(ScopeVersion).where(ScopeVersion.project_id == project.id)) == count


@pytest.mark.parametrize("action", ["revise_report", "revise_chart"])
def test_historical_report_and_chart_revision_scope_cannot_be_edited(
        client, db_session, project, context_factory, report_analysis, monkeypatch, action):
    first = report_service.generate_report(context_factory(project, "generate_report", {"analysis_id": report_analysis.id}))
    if action == "revise_report":
        _plan(monkeypatch, action=action, operations=[{"op": "set_title", "text": "新标题"}])
    else:
        chart = db_session.scalar(select(Chart).where(Chart.analysis_id == report_analysis.id))
        _plan(monkeypatch, action=action, chart_id=chart.id, chart_mode="insight")
    sent = _message(client, project, "修改既有成果")
    jobs.execute_run(sent["run_id"])
    pending = _status(client, sent["run_id"])
    assert pending["input_request"]["kind"] == "execution_confirmation", pending
    response = _submit(client, pending, {**pending["input_request"]["scope"], "goal": "不得改变历史范围"})
    assert response.status_code in {409, 422}, response.text
    assert _status(client, sent["run_id"])["status"] == "waiting_input"
    assert client.get(f"/api/projects/{project.id}").json()["scope_version_id"] == report_analysis.scope_version_id
    assert db_session.get(ReportVersion, first["report_version_id"]).scope_version_id == report_analysis.scope_version_id


def _saved_version(db_session, project, context_factory, analysis, report, content, number, parent=None):
    context = context_factory(project, "revise_report" if parent else "generate_report", {})
    version = ReportVersion(report_id=report.id, number=number, parent_id=parent.id if parent else None,
                            run_id=context.run_id, analysis_id=analysis.id, scope_version_id=analysis.scope_version_id,
                            dataset_version_ids=analysis.dataset_version_ids, content=content)
    db_session.add(version)
    db_session.flush()
    report.current_version_id = version.id
    report.version_counter = number
    run = db_session.get(ResearchRun, context.run_id)
    run.status = "succeeded"
    run.result = {"report_id": report.id, "report_version_id": version.id}
    db_session.flush()
    return version


def _content():
    return {"title": "原报告", "blocks": [
        {"id": "p1", "type": "paragraph", "text": "第一段"},
        {"id": "p2", "type": "paragraph", "text": "第二段"},
        {"id": "p3", "type": "paragraph", "text": "第三段"}],
        "layout": {"body_font_pt": 11, "page_width_inches": 8.3}, "source": {"summary": {"application_count": 1}}}


def test_initial_report_activity_is_marked_initial_without_claiming_modifications(
        client, db_session, project, context_factory, report_analysis):
    report = Report(project_id=project.id)
    db_session.add(report)
    db_session.flush()
    initial = _saved_version(db_session, project, context_factory, report_analysis, report, _content(), 1)
    activity = _status(client, initial.run_id)["activities"][0]
    assert activity["is_initial"] is True and activity["parent_id"] is None
    assert activity.get("change_count") in (None, 0)


@pytest.mark.parametrize("change,expected", [
    ("title", {"title": 1}), ("body", {"modified": 1}), ("added", {"added": 1}),
    ("removed", {"removed": 1}), ("order", {"order": 1}), ("layout", {"layout": 1}),
    ("zero", {}), ("mixed", {"title": 1, "added": 1, "removed": 1, "modified": 1, "order": 1, "layout": 1}),
])
def test_report_activity_counts_only_actual_content_changes_from_its_parent(
        client, db_session, project, context_factory, report_analysis, change, expected):
    report = Report(project_id=project.id)
    db_session.add(report)
    db_session.flush()
    original = _content()
    parent = _saved_version(db_session, project, context_factory, report_analysis, report, original, 1)
    revised = deepcopy(original)
    if change in {"title", "mixed"}:
        revised["title"] = "新报告"
    if change in {"body", "mixed"}:
        revised["blocks"][0]["text"] = "修订后的第一段"
    if change in {"removed", "mixed"}:
        revised["blocks"] = [block for block in revised["blocks"] if block["id"] != "p2"]
    if change in {"order", "mixed"}:
        revised["blocks"] = list(reversed(revised["blocks"]))
    if change in {"added", "mixed"}:
        revised["blocks"].append({"id": "p4", "type": "paragraph", "text": "新增一段"})
    if change in {"layout", "mixed"}:
        revised["layout"]["body_font_pt"] = 12
    if change == "zero":
        revised = {key: revised[key] for key in reversed(revised)}
        revised["blocks"] = [{key: block[key] for key in reversed(block)} for block in revised["blocks"]]
    child = _saved_version(db_session, project, context_factory, report_analysis, report, revised, 2, parent)
    activity = _status(client, child.run_id)["activities"][0]
    assert activity["is_initial"] is False and activity["parent_id"] == parent.id
    expected_summary = {key: expected.get(key, 0) for key in ("title", "added", "removed", "modified", "order", "layout")}
    assert activity["change_summary"] == expected_summary
    assert activity["change_count"] == sum(expected_summary.values())
