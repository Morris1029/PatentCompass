"""Report read projections preserve immutable versions and existing API contracts."""
import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from app.main import create_app
from app.models import Project, Report, ReportVersion, ResearchVersion
from app.report_service import generate_report
from test_reports import report_analysis


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


@pytest.fixture
def versions(db_session, project, context_factory, report_analysis):
    generated = generate_report(context_factory(project, "generate_report", {"analysis_id": report_analysis.id}))
    original = db_session.get(ReportVersion, generated["report_version_id"])
    original.created_at = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
    original.content = {**original.content, "source": {**original.content["source"], "read_test": "FROZEN_SOURCE"}}
    result = [original]
    for number in range(2, 6):
        context = context_factory(project, "revise_report", {})
        content = deepcopy(original.content)
        content["title"] = f"报告 V{number}"
        content["blocks"][1]["text"] = f"版本 {number} 的正文。"
        version = ReportVersion(report_id=original.report_id, number=number, parent_id=result[-1].id,
                                research_id=original.research_id,
                                run_id=context.run_id, analysis_id=original.analysis_id,
                                scope_version_id=original.scope_version_id, dataset_version_ids=original.dataset_version_ids,
                                content=content, chart_ids=original.chart_ids, instruction="修改正文",
                                summary=f"修改记录 {number}", created_at=original.created_at)
        db_session.add(version)
        db_session.flush()
        result.append(version)
    report = db_session.get(Report, original.report_id)
    report.version_counter, report.current_version_id = 5, result[2].id
    db_session.get(ResearchVersion, original.research_id).current_revision_id = result[2].id
    db_session.flush()
    return result


@contextmanager
def capture_version_reads(db_session):
    connection = db_session.connection()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FROM report_versions" in statement:
            statements.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", capture)


def _cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def test_summary_list_projects_metadata_and_pages_tied_timestamps_in_sql(client, db_session, versions):
    report_id = versions[0].report_id
    expected = sorted(versions, key=lambda version: (version.created_at, version.id), reverse=True)
    found, cursor = [], None
    with capture_version_reads(db_session) as statements:
        for _ in range(3):
            response = client.get(f"/api/reports/{report_id}/versions", params={
                "summary": "true", "limit": 2, **({"cursor": cursor} if cursor else {})})
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["total"] == 5 and page["returned_count"] == len(page["items"])
            assert page["current_version_id"] == versions[2].id
            assert all(set(item) == {"id", "created_at", "number", "parent_id", "summary", "is_current",
                       "research_id", "research_number", "main_conversation_id", "source_conversation_id",
                       "source_message_id", "change_kind", "change_count", "change_summary", "restored_from_id", "conversation_id"}
                       for item in page["items"])
            found.extend(page["items"])
            cursor = page["next_cursor"]
    assert [item["id"] for item in found] == [version.id for version in expected]
    assert cursor is None and len({item["id"] for item in found}) == 5
    assert [item["id"] for item in found if item["is_current"]] == [versions[2].id]
    assert statements and all("report_versions.content" not in sql for sql, _ in statements)
    pages = [(sql, parameters) for sql, parameters in statements if "ORDER BY" in sql]
    assert len(pages) == 3 and all("LIMIT" in sql and 3 in parameters.values() for sql, parameters in pages)
    assert all("<" in sql for sql, _ in pages[1:])


def test_legacy_full_list_and_cursors_remain_compatible(client, db_session, versions):
    report_id = versions[0].report_id
    ordered = sorted(versions, key=lambda version: (version.created_at, version.id), reverse=True)
    legacy_cursor = _cursor({"created_at": ordered[1].created_at.isoformat(), "id": ordered[1].id})
    full = client.get(f"/api/reports/{report_id}/versions", params={"limit": 2}).json()
    assert full["next_cursor"] == legacy_cursor
    assert full["total"] == 5 and full["returned_count"] == 2
    assert full["items"][0]["content"] == ordered[0].content
    assert full["items"][0]["analysis_id"] == ordered[0].analysis_id
    assert "run_id" in full["items"][0] and "instruction" in full["items"][0]
    for summary in (False, True):
        response = client.get(f"/api/reports/{report_id}/versions", params={
            "summary": str(summary).lower(), "limit": 2, "cursor": legacy_cursor})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [version.id for version in ordered[2:4]]


@pytest.mark.parametrize("cursor", [
    "%%%", _cursor({}), _cursor([]),
    _cursor({"created_at": "invalid", "id": str(uuid4())}),
    _cursor({"created_at": "2026-10-20T01:00:00", "id": str(uuid4())}),
    _cursor({"created_at": "2026-10-20T01:00:00+00:00", "id": "not-a-uuid"}),
])
@pytest.mark.parametrize("summary", [False, True])
def test_invalid_version_cursor_is_rejected_before_sql_filtering(client, versions, cursor, summary):
    response = client.get(f"/api/reports/{versions[0].report_id}/versions", params={
        "summary": str(summary).lower(), "cursor": cursor})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"


def test_display_detail_projects_only_rendered_content_and_preserves_all_metadata(client, db_session, versions):
    version = versions[1]
    path = f"/api/reports/{version.report_id}/versions/{version.id}"
    full = client.get(path)
    assert full.status_code == 200, full.text
    complete = full.json()
    with capture_version_reads(db_session) as statements:
        response = client.get(path, params={"view": "display"})
    assert response.status_code == 200, response.text
    display = response.json()
    assert display["content"] == {key: complete["content"][key] for key in ("title", "layout", "blocks")}
    assert {key: value for key, value in display.items() if key != "content"} == {
        key: value for key, value in complete.items() if key != "content"}
    assert "FROZEN_SOURCE" not in response.text and "source" in complete["content"]
    assert len(statements) == 1
    sql, parameters = statements[0]
    selected = sql.split("FROM report_versions", 1)[0]
    assert not re.search(r"report_versions\.content\s*(?:,|AS\b|FROM\b)", selected)
    assert {value for key, value in parameters.items() if key.startswith("content_")} == {"title", "layout", "blocks"}
    assert client.get(path, params={"view": "full"}).json() == complete
    assert client.get(path, params={"view": "unsupported"}).status_code == 422


def _another_report(db_session, context_factory, template, owner):
    project = Project(owner_id=owner, name="读取权限边界")
    db_session.add(project)
    db_session.flush()
    report = Report(project_id=project.id, version_counter=1)
    db_session.add(report)
    db_session.flush()
    context = context_factory(project, "generate_report", {})
    version = ReportVersion(report_id=report.id, number=1, run_id=context.run_id, analysis_id=template.analysis_id,
                            scope_version_id=template.scope_version_id, dataset_version_ids=template.dataset_version_ids,
                            content=deepcopy(template.content), chart_ids=template.chart_ids)
    db_session.add(version)
    db_session.flush()
    report.current_version_id = version.id
    db_session.flush()
    return report, version


def test_project_ownership_and_report_membership_match_in_both_detail_modes(
        client, db_session, project, context_factory, versions):
    template = versions[0]
    own_report, own_version = _another_report(db_session, context_factory, template, project.owner_id)
    foreign_report, foreign_version = _another_report(db_session, context_factory, template, "another-owner")
    for view in ("full", "display"):
        wrong_report = client.get(f"/api/reports/{template.report_id}/versions/{own_version.id}", params={"view": view})
        assert wrong_report.status_code == 404 and wrong_report.json()["code"] == "REPORT_VERSION_NOT_FOUND"
        for report_id, version_id in ((template.report_id, foreign_version.id), (foreign_report.id, foreign_version.id),
                                      (template.report_id, str(uuid4()))):
            hidden = client.get(f"/api/reports/{report_id}/versions/{version_id}", params={"view": view})
            assert hidden.status_code == 404 and hidden.json()["code"] == "NOT_FOUND"
    for summary in (False, True):
        denied = client.get(f"/api/reports/{foreign_report.id}/versions", params={"summary": str(summary).lower()})
        assert denied.status_code == 404 and denied.json()["code"] == "NOT_FOUND"
    assert client.get(f"/api/reports/{own_report.id}/versions", params={"summary": "true"}).json()["total"] == 1


def test_display_reads_and_adoption_preserve_full_snapshot_and_parent_links(client, db_session, versions):
    version = versions[1]
    frozen, parent_id = deepcopy(version.content), version.parent_id
    path = f"/api/reports/{version.report_id}/versions/{version.id}"
    display = client.get(path, params={"view": "display"}).json()
    from app.jobs import execute_run
    switched = client.post(f"/api/research-versions/{version.research_id}/restore", json={
        "revision_id": versions[0].id, "expected_current_version_id": versions[2].id,
        "expected_adopted_version_id": versions[2].id})
    assert switched.status_code == 202, switched.text
    execute_run(switched.json()["run_id"])
    assert client.get(f"/api/runs/{switched.json()['run_id']}").json()["status"] == "succeeded"
    assert client.get(path, params={"view": "display"}).json() == display
    assert client.get(path).json()["content"] == frozen
    db_session.expire_all()
    saved = db_session.get(ReportVersion, version.id)
    assert saved.content == frozen and saved.parent_id == parent_id
