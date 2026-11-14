"""Read-only inspection of the shared agent configuration and owned task receipts."""

import base64
from datetime import datetime
import hashlib
import json
from uuid import UUID

from fastapi import Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, tuple_

from app.config import get_settings
from app.errors import AppError
from app.models import Project, ResearchRun, RunEvent
from app.resource_service import owned
from patentcompass_agent import files, list_skills, read_skill, read_system_prompt


STATES = {"queued", "running", "waiting_input", "cancelling", "cancelled", "succeeded", "failed"}
ARTIFACT_KEYS = {"analysis_id", "report_id", "report_version_id", "research_id", "chart_id", "chart_version_id", "file_id"}
EVENT_FIELDS = {
    "agent.started": {"version", "system_prompt_sha256", "skill_count", "model_name", "tools_sha256"},
    "agent.skill.loaded": {"name", "sha256"},
    "agent.tool.called": {"name", "turn", "call_id"},
    "agent.tool.completed": {"name", "turn", "call_id", "outcome", "error_code", "run_id", "status"} | ARTIFACT_KEYS,
    "input.required": {"kind", "title", "prompt"},
    "input.received": {"kind"},
    "plan.ready": {"action", "reason"},
    "activity.updated": {"id", "kind", "status", "phase", "processed_count", "total_count"} | ARTIFACT_KEYS,
    "artifact.ready": ARTIFACT_KEYS,
    "run.completed": {"status"},
    "run.failed": {"status"},
    "run.cancelled": {"status"},
    "step.updated": {"step", "status"},
}


def document(content, source):
    return {"content": content, "source": source, "sha256": hashlib.sha256(content.encode()).hexdigest()}


def configuration():
    from app.agent_runtime import definitions
    from app.tool_service import catalogue
    settings = get_settings()
    native = definitions()
    names = {item["function"]["name"] for item in native}
    tools = [{"name": name, "description": spec.description, "group": spec.group,
              "read_only": spec.read_only, "native": name in names,
              "parameters": spec.arguments.model_json_schema()} for name, spec in catalogue().items()]
    return {"identity": {"owner_id": settings.dev_owner_id, "display_name": "本地用户"},
        "model": {"name": settings.llm_model or None, "configured": settings.llm_enabled,
                  "embedding_name": settings.embedding_model or None, "embedding_configured": settings.embedding_enabled},
        "agent": {"name": "PatentCompass", "runtime_version": 1,
            "system_prompt": document(read_system_prompt(), "patentcompass_agent/SYSTEM_PROMPT.md"),
            "adapter_prompt": document(files("patentcompass_agent").joinpath("BUILTIN_RUNTIME.md").read_text(encoding="utf-8"),
                                       "patentcompass_agent/BUILTIN_RUNTIME.md"),
            "tools": native},
        "mcp": {"name": "PatentCompass", "transport": "Streamable HTTP", "endpoint": "/mcp/", "tools": tools},
        "skills": list_skills()}


def skill(name, reference=None):
    try:
        value = read_skill(name, reference)
    except ValueError as error:
        raise AppError(404, "SKILL_RESOURCE_NOT_FOUND", "技能或参考文件不存在") from error
    return {**value, **document(value["content"], f"patentcompass_agent/skills/{name}/{value['resource']}")}


def error_summary(error):
    if not isinstance(error, dict):
        return None
    message = str(error.get("message", ""))[:1000]
    for value in (get_settings().llm_api_key, get_settings().embedding_api_key):
        if value:
            message = message.replace(value, "[redacted]")
    # Provider errors can include URLs or connection details. Their raw messages
    # remain in the existing business task receipt, not in this overview.
    code = str(error.get("code", "TASK_FAILED"))
    return {"code": code, "message": message if code not in {"TASK_FAILED", "INTERNAL_ERROR"} else "任务执行失败，请查看原对话"}


def artifacts(value):
    result = {key: value[key] for key in ARTIFACT_KEYS if value.get(key)}
    if result.get("file_id"):
        result["download_url"] = f"/api/files/{result['file_id']}"
    return result


def run_summary(session, run):
    project = session.get(Project, run.project_id) if run.project_id else None
    state = run.payload.get("agent_runtime") or {}
    return {"id": run.id, "project_id": run.project_id, "project_name": project.name if project else None,
        "conversation_id": run.conversation_id, "kind": run.kind, "status": run.status,
        "created_at": run.created_at, "updated_at": run.updated_at,
        "runtime": "agent" if run.payload.get("agent_runtime_version") or state else "business",
        "model_name": state.get("model_name"), "skills": list(state.get("skills", {})),
        "action": (run.payload.get("plan") or {}).get("action"), "error": error_summary(run.error)}


def runs(session, project_id=None, status=None, limit=20, cursor=None):
    if project_id:
        owned(session, Project, project_id)
    conditions = [ResearchRun.owner_id == get_settings().dev_owner_id,
                  ResearchRun.payload["parent_run_id"].astext.is_(None)]
    if project_id:
        conditions.append(ResearchRun.project_id == str(project_id))
    if status:
        if status not in STATES:
            raise AppError(422, "INVALID_RUN_STATUS", "任务状态无效")
        conditions.append(ResearchRun.status == status)
    total = session.scalar(select(func.count()).select_from(ResearchRun).where(*conditions))
    if cursor:
        try:
            point = json.loads(base64.urlsafe_b64decode(cursor))
            created_at = datetime.fromisoformat(point["created_at"])
            identifier = UUID(str(point["id"]))
            conditions.append(tuple_(ResearchRun.created_at, ResearchRun.id) <
                              tuple_(created_at, identifier))
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise AppError(422, "INVALID_CURSOR", "分页游标无效") from error
    rows = list(session.scalars(select(ResearchRun).where(*conditions)
        .order_by(ResearchRun.created_at.desc(), ResearchRun.id.desc()).limit(limit + 1)))
    selected = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"created_at": last.created_at.isoformat(), "id": last.id}).encode()).decode()
    return {"items": [run_summary(session, row) for row in selected], "total": total,
            "returned_count": len(selected), "next_cursor": next_cursor}


def run_detail(session, identifier):
    run = owned(session, ResearchRun, identifier)
    state = run.payload.get("agent_runtime") or {}
    snapshot = {"model_name": state.get("model_name"), "tools": state.get("tools"), "tools_sha256": state.get("tools_sha256"),
        "system_prompt": document(state["system_prompt"], "运行时快照") if state.get("system_prompt") else None,
        "adapter_prompt": document(state["adapter"], "运行时快照") if state.get("adapter") else None,
        "skills": [{"name": name, **document(value["content"], "运行时快照"),
                    "references": [{"name": ref, **document(text, "运行时快照")} for ref, text in value.get("references", {}).items()]}
                   for name, value in state.get("skills", {}).items()]}
    children = session.scalars(select(ResearchRun).where(ResearchRun.owner_id == get_settings().dev_owner_id,
        ResearchRun.payload["parent_run_id"].astext == run.id).order_by(ResearchRun.created_at)).all()
    plan = run.payload.get("plan") or {}
    return {**run_summary(session, run), "snapshot": snapshot,
        "plan": {key: plan[key] for key in ("action", "reason", "semantic_mode", "research_intent") if key in plan},
        "waiting_input": {"kind": run.input_request.get("kind"), "title": run.input_request.get("title") or run.input_request.get("prompt")}
                          if run.input_request and run.status == "waiting_input" else None,
        "artifacts": artifacts({**state.get("artifacts", {}), **run.result}),
        "children": [{**run_summary(session, child), "artifacts": artifacts(child.result)} for child in children]}


def events(session, identifier, after=0, limit=100):
    run = owned(session, ResearchRun, identifier)
    rows = list(session.scalars(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.seq > after,
        RunEvent.event_type.in_(EVENT_FIELDS)).order_by(RunEvent.seq).limit(limit + 1)))
    items = [{"seq": row.seq, "created_at": row.created_at, "event_type": row.event_type,
              "payload": {key: value for key, value in row.payload.items() if key in EVENT_FIELDS[row.event_type]}}
             for row in rows[:limit]]
    return {"items": items, "status": run.status, "after": items[-1]["seq"] if items else after, "has_more": len(rows) > limit}


def register(app):
    from app.main import DB

    @app.get("/api/management/configuration", tags=["management"])
    def get_configuration():
        return configuration()

    @app.get("/api/management/skills/{name}", tags=["management"])
    def get_skill(name: str, reference: str | None = Query(None, max_length=150)):
        return skill(name, reference)

    @app.get("/api/management/runs", tags=["management"])
    def get_runs(session: DB, project_id: UUID | None = None, status: str | None = None,
                 limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=1000)):
        return jsonable_encoder(runs(session, project_id, status, limit, cursor))

    @app.get("/api/management/runs/{id}", tags=["management"])
    def get_run(id: UUID, session: DB):
        return jsonable_encoder(run_detail(session, id))

    @app.get("/api/management/runs/{id}/events", tags=["management"])
    def get_events(id: UUID, session: DB, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        return jsonable_encoder(events(session, id, after, limit))
