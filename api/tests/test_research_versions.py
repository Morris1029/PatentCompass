from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.errors import AppError
from app.jobs import Cancelled
from app.models import Message, Project, Report, ReportVersion, ResearchRun, ResearchVersion, ScopeVersion
from app.report_service import generate_report, publish_candidate, restore_report, revise_report
from app.research_service import (
    context_for, create_draft, ensure_conversation, ensure_for_report, for_conversation, set_context,
)
from test_reports import report_analysis


def _publish(ctx, candidate):
    with session_scope() as session:
        return publish_candidate(session, ctx, candidate)


def _deferred(context_factory, project, kind, payload):
    ctx = context_factory(project, kind, payload)
    ctx.defer_report_publish = True
    return ctx


def _first(context_factory, project, analysis):
    return generate_report(context_factory(project, "generate_report", {"analysis_id": analysis.id}))


def _edit(context_factory, project, record, text, *, deferred=False):
    payload = {"research_id": record["research_id"], "report_id": record["report_id"],
               "base_version_id": record["report_version_id"],
               "expected_current_version_id": record["report_version_id"],
               "operations": [{"op": "set_title", "text": text}]}
    ctx = context_factory(project, "revise_report", payload)
    ctx.defer_report_publish = deferred
    return ctx, revise_report(ctx)


def test_first_candidate_does_not_publish_or_allocate_until_success(db_session, project, context_factory, report_analysis):
    ctx = _deferred(context_factory, project, "generate_report", {"analysis_id": report_analysis.id})
    candidate = generate_report(ctx)
    assert "_report_candidate" in candidate and "report_version_id" not in candidate
    db_session.expire_all()
    report = db_session.scalar(select(Report).where(Report.project_id == project.id))
    research = db_session.get(ResearchVersion, candidate["_report_candidate"]["research_id"])
    assert (report.current_version_id, report.version_counter, report.research_counter) == (None, 0, 0)
    assert (research.status, research.number, research.current_revision_id) == ("draft", None, None)
    result = _publish(ctx, candidate)
    db_session.expire_all()
    research = db_session.get(ResearchVersion, result["research_id"])
    assert (research.status, research.number) == ("published", 1)
    assert research.initial_revision_id == research.current_revision_id == result["report_version_id"]
    assert result["change_kind"] == "generate" and result["change_count"] == 0


def test_cancelled_candidate_cannot_create_successful_history(db_session, project, context_factory, report_analysis):
    ctx = _deferred(context_factory, project, "generate_report", {"analysis_id": report_analysis.id})
    candidate = generate_report(ctx)
    db_session.get(ResearchRun, ctx.run_id).cancellation_requested = True
    db_session.flush()
    with pytest.raises(Cancelled):
        _publish(ctx, candidate)
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(ReportVersion).where(
        ReportVersion.report_id == candidate["_report_candidate"]["report_id"])) == 0
    research = db_session.get(ResearchVersion, candidate["_report_candidate"]["research_id"])
    assert research.number is None and research.status == "draft"


def test_retry_operation_publishes_exactly_one_revision(db_session, project, context_factory, report_analysis):
    original = _deferred(context_factory, project, "generate_report", {"analysis_id": report_analysis.id})
    candidate = generate_report(original)
    first = _publish(original, candidate)
    retry = _deferred(context_factory, project, "generate_report", original.payload)
    db_session.get(ResearchRun, retry.run_id).retry_of_id = original.run_id
    db_session.flush()
    second = _publish(retry, candidate)
    assert second["report_version_id"] == first["report_version_id"]
    db_session.expire_all()
    assert db_session.get(Report, first["report_id"]).version_counter == 1
    assert db_session.get(ReportVersion, first["report_version_id"]).operation_id == original.run_id


def test_daily_noop_has_no_revision_or_counter_change(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    title = db_session.get(ReportVersion, first["report_version_id"]).content["title"]
    _, result = _edit(context_factory, project, first, title)
    assert result["unchanged"] is True
    assert result["report_version_id"] == first["report_version_id"]
    assert result["change_count"] == 0 and result["change_summary"] == {}
    db_session.expire_all()
    assert db_session.get(Report, first["report_id"]).version_counter == 1


@pytest.mark.parametrize("kind", ["generate_report", "restore_report"])
def test_publication_does_not_overwrite_newer_working_context(
        db_session, project, context_factory, report_analysis, kind):
    first = _first(context_factory, project, report_analysis)
    payload = {"research_id": first["research_id"], "analysis_id": report_analysis.id}
    if kind == "restore_report":
        payload.update(restore_revision_id=first["report_version_id"],
            expected_current_version_id=first["report_version_id"],
            expected_adopted_version_id=first["report_version_id"])
    ctx = _deferred(context_factory, project, kind, payload)
    candidate = generate_report(ctx) if kind == "generate_report" else restore_report(ctx)
    research = db_session.get(ResearchVersion, first["research_id"])
    set_context(db_session, research, analysis_id=None)
    db_session.flush()
    with pytest.raises(AppError) as failure:
        _publish(ctx, candidate)
    assert failure.value.code == "RESEARCH_CONTEXT_CONFLICT"
    db_session.expire_all()
    assert db_session.get(ResearchVersion, research.id).analysis_id is None
    assert db_session.get(Report, first["report_id"]).version_counter == 1


def test_competing_candidates_use_research_head_cas(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    left, left_candidate = _edit(context_factory, project, first, "Left", deferred=True)
    right, right_candidate = _edit(context_factory, project, first, "Right", deferred=True)
    winner = _publish(left, left_candidate)
    with pytest.raises(AppError) as failure:
        _publish(right, right_candidate)
    assert failure.value.code == "REPORT_VERSION_CONFLICT"
    db_session.expire_all()
    report = db_session.get(Report, first["report_id"])
    assert report.version_counter == 2 and report.current_version_id == winner["report_version_id"]


def test_new_round_gets_new_conversation_and_number_only_at_first_report(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    report = db_session.get(Report, first["report_id"])
    legacy = db_session.get(ResearchVersion, first["research_id"])
    main = ensure_conversation(db_session, legacy)
    draft = create_draft(db_session, report, first["report_version_id"])
    db_session.flush()
    assert draft.number is None and draft.current_revision_id is None
    assert draft.main_conversation_id != main.id
    assert for_conversation(db_session, main.id).id == legacy.id
    assert for_conversation(db_session, draft.main_conversation_id).id == draft.id
    assert context_for(db_session, draft)["analysis_id"] == report_analysis.id
    ctx = _deferred(context_factory, project, "generate_report", {"research_id": draft.id,
        "analysis_id": report_analysis.id})
    candidate = generate_report(ctx)
    db_session.expire_all()
    assert db_session.get(ResearchVersion, draft.id).number is None
    second = _publish(ctx, candidate)
    assert second["research_number"] == 2 and second["main_conversation_id"] == draft.main_conversation_id
    db_session.expire_all()
    assert db_session.get(Report, report.id).current_research_id == draft.id
    assert db_session.get(ReportVersion, second["report_version_id"]).parent_id is None
    assert db_session.get(ReportVersion, first["report_version_id"]).research_id == legacy.id


def test_editing_inactive_research_does_not_adopt_it(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    draft = create_draft(db_session, db_session.get(Report, first["report_id"]), first["report_version_id"])
    db_session.flush()
    second = generate_report(context_factory(project, "generate_report", {
        "research_id": draft.id, "analysis_id": report_analysis.id}))
    _, edited = _edit(context_factory, project, first, "Only V1 changes")
    assert edited["research_number"] == 1
    db_session.expire_all()
    report = db_session.get(Report, first["report_id"])
    assert report.current_research_id == draft.id and report.current_version_id == second["report_version_id"]
    assert db_session.get(ResearchVersion, first["research_id"]).current_revision_id == edited["report_version_id"]
    assert edited["change_summary"]["title"] == 1 and edited["change_count"] == 1


def test_cross_research_restore_records_even_identical_content_and_adopts(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    original_content = deepcopy(db_session.get(ReportVersion, first["report_version_id"]).content)
    draft = create_draft(db_session, db_session.get(Report, first["report_id"]), first["report_version_id"])
    db_session.flush()
    second = generate_report(context_factory(project, "generate_report", {
        "research_id": draft.id, "analysis_id": report_analysis.id}))
    ctx = _deferred(context_factory, project, "restore_report", {
        "research_id": first["research_id"], "restore_revision_id": first["report_version_id"],
        "expected_current_version_id": first["report_version_id"],
        "expected_adopted_version_id": second["report_version_id"]})
    restored = _publish(ctx, restore_report(ctx))
    assert restored["report_version_id"] != first["report_version_id"]
    assert restored["change_kind"] == "restore" and restored["change_count"] == 0
    assert restored["restored_from_id"] == first["report_version_id"]
    db_session.expire_all()
    report = db_session.get(Report, first["report_id"])
    version = db_session.get(ReportVersion, restored["report_version_id"])
    assert report.current_research_id == first["research_id"]
    assert report.current_version_id == version.id and version.content == original_content
    assert version.parent_id == first["report_version_id"]


def test_restore_checks_adopted_head_and_preserves_snapshot(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    frozen = deepcopy(db_session.get(ReportVersion, first["report_version_id"]).content)
    _, second = _edit(context_factory, project, first, "Changed")
    ctx = _deferred(context_factory, project, "restore_report", {
        "research_id": first["research_id"], "restore_revision_id": first["report_version_id"],
        "expected_current_version_id": second["report_version_id"],
        "expected_adopted_version_id": second["report_version_id"]})
    restored = _publish(ctx, restore_report(ctx))
    db_session.expire_all()
    assert db_session.get(ReportVersion, restored["report_version_id"]).content == frozen
    assert db_session.get(ReportVersion, first["report_version_id"]).content == frozen
    assert restored["change_summary"]["title"] == 1
    assert restored["parent_id"] == second["report_version_id"]


def test_edit_keeps_working_context_and_project_adopted_basis(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    research = db_session.get(ResearchVersion, first["research_id"])
    scope = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "新范围"})
    db_session.add(scope)
    db_session.flush()
    set_context(db_session, research, scope_id=scope.id, analysis_id=None)
    revision = research.revision
    db_session.flush()
    _, edited = _edit(context_factory, project, first, "Working scope stays selected")
    db_session.expire_all()
    research = db_session.get(ResearchVersion, research.id)
    adopted = db_session.get(Project, project.id)
    assert research.scope_version_id == scope.id and research.analysis_id is None
    assert research.revision == revision
    assert adopted.scope_version_id == report_analysis.scope_version_id
    assert adopted.current_analysis_id == report_analysis.id
    assert db_session.get(Report, first["report_id"]).current_version_id == edited["report_version_id"]


def test_source_metadata_points_to_original_request(db_session, project, context_factory, report_analysis):
    first = _first(context_factory, project, report_analysis)
    research = db_session.get(ResearchVersion, first["research_id"])
    main = ensure_conversation(db_session, research)
    from app.models import Conversation
    original = Conversation(project_id=project.id, title="普通对话")
    db_session.add(original)
    db_session.flush()
    message = Message(conversation_id=original.id, role="user", content="请修改标题")
    db_session.add(message)
    db_session.flush()
    ctx = context_factory(project, "revise_report", {"research_id": research.id,
        "report_id": first["report_id"], "base_version_id": first["report_version_id"],
        "expected_current_version_id": first["report_version_id"], "source_message_id": message.id,
        "operations": [{"op": "set_title", "text": "Transferred request"}]})
    ctx.conversation_id = main.id
    edited = revise_report(ctx)
    assert edited["source_message_id"] == message.id
    assert edited["source_conversation_id"] == original.id
    assert edited["conversation_id"] == main.id


def test_lazy_legacy_group_preserves_adopted_branch_and_all_snapshots(db_session, project, context_factory, report_analysis):
    contexts = [context_factory(project, "generate_report", {}) for _ in range(3)]
    report = Report(project_id=project.id, version_counter=3)
    db_session.add(report)
    db_session.flush()
    records = []
    for index, ctx in enumerate(contexts):
        row = ReportVersion(report_id=report.id, number=index + 1, run_id=ctx.run_id,
            parent_id=records[0].id if records else None, analysis_id=report_analysis.id,
            scope_version_id=report_analysis.scope_version_id, dataset_version_ids=report_analysis.dataset_version_ids,
            content={"title": str(index), "blocks": []})
        db_session.add(row)
        db_session.flush()
        records.append(row)
    report.current_version_id = records[1].id
    db_session.flush()
    frozen = [(row.id, row.number, row.parent_id, deepcopy(row.content)) for row in records]
    research = ensure_for_report(db_session, report)
    assert research.number == 1 and research.current_revision_id == records[1].id
    assert report.current_version_id == records[1].id and report.version_counter == 3
    assert all(row.research_id == research.id for row in records)
    assert frozen == [(row.id, row.number, row.parent_id, row.content) for row in records]
