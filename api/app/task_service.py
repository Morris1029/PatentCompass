"""Durable task operations, independent of the transport that invokes them."""

from sqlalchemy import select

from app.errors import AppError
from app.jobs import TERMINAL, create_run, emit_event
from app.models import (ImportConflict, Material, Message, ReportVersion,
                        ResearchRun, ResearchVersion, RunStep, ScopeVersion)
from app.resource_service import owned, public_value, record


def research_record(session, research):
    from app.research_service import context_for
    context = context_for(session, research)
    scope = session.get(ScopeVersion, context.get("scope_version_id")) if context.get("scope_version_id") else None
    head = session.execute(select(ReportVersion.content["title"].label("title"), ReportVersion.created_at)
                           .where(ReportVersion.id == research.current_revision_id)).first() if research.current_revision_id else None
    return {**record(research), "current_version_id": research.current_revision_id,
            "title": head.title if head else "新一轮研究", "updated_at": head.created_at if head else research.created_at,
            "context": {**context, "scope": record(scope) if scope else None,
                        "dataset_version_id": next(iter(context.get("dataset_version_ids", [])), None),
                        "current_analysis_id": context.get("analysis_id"), "revision": research.revision}}


def get_run(session, run_id):
    from app.conversation_execution import activities_for_run, scope_editable_for_run
    from app.chat_streaming import stream_for_run
    run = owned(session, ResearchRun, run_id)
    steps = session.scalars(select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.created_at)).all()
    request = dict(run.input_request) if run.input_request else None
    if request and request["kind"] == "import_conflicts":
        request["conflicts"] = [record(row) for row in session.scalars(select(ImportConflict).where(
            ImportConflict.version_id == run.payload["dataset_version_id"], ImportConflict.resolution.is_(None)))]
    if request and request["kind"] in {"scope_confirmation", "plan_confirmation", "execution_confirmation"}:
        scope = session.get(ScopeVersion, run.scope_version_id)
        request["scope"] = {**(scope.content if scope else {}), **request.get("scope", {}), **request.get("changes", {})}
        request["scope_editable"] = scope_editable_for_run(run) if request["kind"] == "execution_confirmation" else True
    research_id = run.payload.get("research_id")
    return public_value({**record(run, exclude=("lease_token", "payload")), "steps": [record(item) for item in steps],
        "research_id": research_id,
        "context": research_record(session, session.get(ResearchVersion, research_id))["context"] if research_id else None,
        "input_request": request, "plan": run.payload.get("plan"),
        "origin_message_id": run.payload.get("message_id") or session.scalar(select(Message.id).where(
            Message.run_id == run.id, Message.role == "user")),
        "activities": activities_for_run(session, run), "stream": stream_for_run(session, run)})


def get_run_events(session, run_id, after=0, limit=200):
    from app.models import RunEvent
    run = owned(session, ResearchRun, run_id)
    events = session.scalars(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.seq > after)
                            .order_by(RunEvent.seq).limit(limit)).all()
    return public_value({"run_id": run.id, "status": run.status, "items": [record(row) for row in events],
                         "after": events[-1].seq if events else after})


def cancel_run(session, run_id):
    run = owned(session, ResearchRun, run_id, lock=True)
    if run.status not in TERMINAL:
        run.cancellation_requested = True
        run.status = "cancelling" if run.status in {"running", "cancelling"} else "cancelled"
        run.dispatch_pending = False
        if run.kind in {"import", "merge_import"} and run.status == "cancelled":
            from app.models import DatasetVersion
            version = session.get(DatasetVersion, run.payload["dataset_version_id"])
            if version.status != "ready":
                version.status = "cancelled"
        if run.kind == "parse_material" and run.status == "cancelled":
            material = session.get(Material, run.payload["material_id"])
            material.status = "cancelled"
        emit_event(session, run.id, "step.updated" if run.status == "cancelling" else "run.completed", {"status": run.status})
        children = list(session.scalars(select(ResearchRun).where(
            ResearchRun.payload["parent_run_id"].astext == run.id, ResearchRun.owner_id == run.owner_id,
            ResearchRun.status.not_in(TERMINAL))))
        for child in children:
            cancel_run(session, child.id)
    return {"run_id": run.id, "status": run.status}


def retry_run(session, run_id):
    old = owned(session, ResearchRun, run_id, lock=True)
    if old.status != "failed":
        raise AppError(409, "RUN_NOT_FAILED", "只能重试失败任务")
    run = create_run(session, old.kind, payload=dict(old.payload), conversation_id=old.conversation_id, retry_of_id=old.id)
    run.project_id, run.scope_version_id, run.dataset_version_ids = old.project_id, old.scope_version_id, list(old.dataset_version_ids)
    # Rebind in the retry transaction, before the new worker can start or the
    # caller can cancel the queued retry. Completed children remain historical.
    children = session.scalars(select(ResearchRun).where(ResearchRun.owner_id == old.owner_id,
        ResearchRun.payload["parent_run_id"].astext == old.id, ResearchRun.status.not_in(TERMINAL)).with_for_update()).all()
    for child in children:
        child.payload = {**child.payload, "parent_run_id": run.id}
    origin = session.get(Message, old.payload["message_id"]) if old.payload.get("message_id") else session.scalar(
        select(Message).where(Message.run_id == old.id, Message.role == "user"))
    if origin and origin.role == "user" and origin.conversation_id == old.conversation_id:
        origin.run_id = run.id
        origin.cards = [card for card in origin.cards if card.get("type") != "task"]
    if old.kind == "parse_material":
        material = session.get(Material, old.payload["material_id"])
        material.run_id, material.status, material.error = run.id, "parsing", None
    if old.kind in {"import", "merge_import"}:
        from app.models import DatasetVersion
        version = session.get(DatasetVersion, old.payload["dataset_version_id"])
        if version.status != "ready":
            version.status = "pending"
    return {"run_id": run.id, "status": run.status, "retry_of_id": old.id}
