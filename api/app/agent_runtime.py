"""Resumable model/skill/tool loop for the application's actual conversation entry."""

from copy import deepcopy
import hashlib
import json

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import select

from app import tool_service
from app.chat_streaming import stream_response
from app.config import get_settings
from app.conversation_execution import require_execution, retrieval_details
from app.db import session_scope
from app.errors import AppError
from app.models import (Chart, Evidence, Message, PatentApplication,
                        Project, Report, ReportVersion, ResearchRun, ScopeVersion)
from app.resource_service import owned, public_value
from patentcompass_agent import files, list_skills, read_system_prompt


_HIDDEN = {"classify_patents", "build_embeddings", "build_report", "revise_report_content",
           "create_agent_project", "create_agent_research", "update_agent_scope", "bind_agent_dataset",
           "adopt_agent_research", "list_projects", "parse_patent_workbook", "parse_document", "render_chart", "export_docx"}
_ARTIFACTS = {"analysis_id", "report_id", "report_version_id", "research_id", "chart_id", "chart_version_id", "file_id", "download_url"}
_PROGRESS = _ARTIFACTS | {"run_id", "status", "batch_index", "batch_count", "batch_status", "next_batch_index", "processed_count", "total_count", "completed_batches", "semantic_progress", "dimension", "theme_count", "review"}


def _save(ctx, state):
    with session_scope() as session:
        run = ctx.guard(session)
        run.payload = {**run.payload, "agent_runtime": deepcopy(state)}
        ctx.payload = dict(run.payload)


def _context(ctx):
    with session_scope() as session:
        run = ctx.guard(session)
        project = session.get(Project, ctx.project_id)
        scope = session.get(ScopeVersion, ctx.scope_version_id) if ctx.scope_version_id else None
        message = session.get(Message, ctx.payload["message_id"])
        recent = session.scalars(select(Message).where(Message.conversation_id == ctx.conversation_id,
            Message.created_at <= message.created_at).order_by(Message.created_at.desc(), Message.id.desc()).limit(12)).all()
        from app.materials_service import read_materials
        materials = read_materials(session, message.attachments, ctx.project_id)
        serialized = json.dumps(materials, ensure_ascii=False)
        if len(serialized) > 60000:
            materials = {"preview": serialized[:60000], "truncated": True,
                         "sources": [{"material_id": item["material_id"], "name": item["name"]} for item in materials]}
        from app.agent_service import context_assets
        target_id = run.payload.get("reference_version_id") or run.payload.get("plan_report_version_id")
        if not target_id:
            report = session.scalar(select(Report).where(Report.project_id == project.id))
            target_id = report.current_version_id if report else None
        assets = context_assets(session, project.id, {"analysis_id": run.payload.get("analysis_id")}, target_id)
        return {**assets, "run_id": ctx.run_id, "project_id": ctx.project_id, "project_name": project.name,
            "host_model": get_settings().llm_model,
            "research_id": run.payload.get("research_id"), "scope_version_id": run.scope_version_id,
            "dataset_version_ids": list(run.dataset_version_ids), "scope": scope.content if scope else {},
            "analysis_id": run.payload.get("analysis_id"), "report_version_id": run.payload.get("reference_version_id"),
            "plan": run.payload.get("plan"), "request_action": run.kind, "semantic_mode": run.payload.get("semantic_mode"),
            "reference": message.reference, "materials": materials,
            "messages": [{"role": row.role, "content": (row.content if row.id == message.id else row.content[:6000])
                          or ("已发送附件，正文见资料上下文。" if row.attachments else "")} for row in reversed(recent)]}


def definitions():
    from app.orchestration import Plan
    result = [spec.definition(name) for name, spec in tool_service.catalogue().items() if name not in _HIDDEN]
    result.append(tool_service.Tool(Plan,
        "Prepare the current request and preserve the original scope, research assignment and execution confirmation. Does not perform analysis or produce a report.",
        False, "runtime").definition("prepare_agent_action"))
    return result


def _prepare(ctx, arguments):
    from app.orchestration import Plan, _require_data_scope, prepare_execution, wait_for_input
    proposed = Plan.model_validate(arguments).model_dump(mode="json")
    requested = getattr(ctx, "kind", "auto")
    if requested in {"analyze", "generate_report"} and proposed["action"] != requested:
        raise AppError(409, "ACTION_NOT_AUTHORIZED", "请遵守原消息入口明确指定的业务操作")
    if (not ctx.payload.get("plan") and proposed["action"] == "revise_report" and not proposed["operations"]
            and not proposed["block_ids"] and not proposed["select_report_target"]):
        raise AppError(422, "REPORT_TARGET_REQUIRED", "请先明确修改操作；仅改标题使用 operations=[{op:set_title,text:目标标题}]。只有正文目标确实不明确时才设置 select_report_target=true。")
    plan = prepare_execution(ctx, proposed)
    if plan["action"] in {"analyze", "generate_report"}:
        _require_data_scope(ctx)
    if plan["action"] == "revise_chart" and plan["chart_mode"] == "data" and not ctx.payload.get("chart_data_confirmed"):
        wait_for_input(ctx, "chart_data_confirmation", "请确认图表局部筛选后重新计算", filters=plan["chart_filters"])
    context = _context(ctx)
    return {"plan": plan, "context": context,
        "answer_basis": {key: context[key] for key in ("scope_version_id", "dataset_version_ids", "scope", "analysis_id", "report_version_id")}}


def _required_skill(name, arguments):
    if name == "save_research_strategy":
        return "patent-research-strategy"
    if "semantic" in name:
        return "patent-semantic-analysis"
    if name == "start_agent_task":
        action = arguments.get("request", {}).get("action")
        if action in {"revise_report", "restore_report", "revise_chart"}:
            return "patent-result-revision"
        if action in {"generate_report", "export"}:
            return "patent-navigation-report"
    if name in {"search_patents", "search_materials", "get_patent", "get_evidence"}:
        return "patent-evidence-qa"
    return None


def _scope_tool(ctx, state, name, arguments):
    """Bind shared contracts to this conversation's authorized fixed resources."""
    if name in _HIDDEN:
        raise AppError(422, "TOOL_NOT_AVAILABLE", "此对话不提供该工具")
    required = _required_skill(name, arguments)
    if required and required not in state["skills"]:
        raise AppError(422, "SKILL_REQUIRED", "请先加载对应技能", {"skill": required})
    if name not in {"load_agent_skill", "read_agent_reference", "list_agent_skills", "get_agent_context", "list_datasets"} and not ctx.payload.get("plan"):
        raise AppError(422, "AGENT_PREPARATION_REQUIRED", "请先准备本轮意图和业务上下文")
    request = arguments.get("request", arguments)
    if request.get("project_id") and str(request["project_id"]) != ctx.project_id:
        raise AppError(404, "PROJECT_NOT_FOUND", "工具目标不属于此对话")
    if request.get("research_id") and str(request["research_id"]) != ctx.payload.get("research_id"):
        raise AppError(409, "RESEARCH_TARGET_CHANGED", "请先通过本轮确认选择研究归属")
    if name in {"get_agent_context", "start_semantic_analysis", "start_agent_task", "save_research_strategy"} and ctx.payload.get("research_id"):
        if request.get("research_id") != ctx.payload["research_id"]:
            raise AppError(409, "RESEARCH_TARGET_REQUIRED", "请传入本轮已绑定的研究标识")
    if name in {"get_run", "get_run_events", "cancel_run", "retry_run", "get_semantic_batch",
                "submit_semantic_batch", "get_semantic_taxonomy", "submit_semantic_taxonomy", "finish_semantic_analysis"}:
        if str(request.get("run_id")) not in state["children"]:
            raise AppError(404, "RUN_NOT_FOUND", "任务不属于本轮执行")
    with session_scope() as session:
        ctx.guard(session)
        scope = session.get(ScopeVersion, ctx.scope_version_id)
        if name in {"start_semantic_analysis", "start_agent_task"} and request.get("action", "analyze") in {"analyze", "generate_report"}:
            from app.agent_service import _context as business_context
            _, _, current = business_context(session, ctx.project_id, ctx.payload.get("research_id"))
            if current["scope_version_id"] != ctx.scope_version_id or current["dataset_version_ids"] != ctx.dataset_version_ids:
                raise AppError(409, "AGENT_CONTEXT_CHANGED", "当前范围或数据已变化，请核对后重新确认")
        if name in {"search_patents", "compute_statistics"}:
            if request.get("records") is not None or sorted(request.get("dataset_version_ids") or []) != sorted(ctx.dataset_version_ids):
                raise AppError(409, "DATASET_SNAPSHOT_MISMATCH", "请采用本轮固定数据版本")
            supplied = request.get("scope", {})
            fixed = scope.content if scope else {}
            if any(fixed.get(key) != value for key, value in supplied.items()):
                raise AppError(409, "SCOPE_SNAPSHOT_FIXED", "检索不能替换固定研究范围")
            request["scope"] = dict(fixed)
        if name == "read_analysis" and request.get("analysis_id") != ctx.payload.get("analysis_id"):
            raise AppError(409, "ANALYSIS_SNAPSHOT_MISMATCH", "请读取本轮固定分析")
        if name == "get_patent":
            row = owned(session, PatentApplication, request["patent_id"])
            if row.dataset_version_id not in ctx.dataset_version_ids:
                raise AppError(404, "PATENT_NOT_FOUND", "专利不属于固定数据")
        if name == "get_evidence":
            row = owned(session, Evidence, request["evidence_id"])
            if row.analysis_id != ctx.payload.get("analysis_id"):
                raise AppError(404, "EVIDENCE_NOT_FOUND", "证据不属于固定分析")
        if name == "get_report_version":
            row = owned(session, ReportVersion, request["report_version_id"])
            if session.get(Report, row.report_id).project_id != ctx.project_id:
                raise AppError(404, "REPORT_NOT_FOUND", "报告不属于当前项目")
            frozen = ctx.payload.get("reference_version_id")
            if ctx.payload["plan"]["action"] == "chat" and frozen and row.id != frozen:
                raise AppError(409, "REPORT_SNAPSHOT_FIXED", "报告问答采用已固定版本")
        if name == "get_agent_chart":
            chart = owned(session, Chart, request["chart_id"])
            if chart.project_id != ctx.project_id:
                raise AppError(404, "CHART_NOT_FOUND", "图表不属于当前项目")
            if ctx.payload.get("plan", {}).get("action") == "chat" and ctx.payload.get("reference_version_id"):
                version = session.get(ReportVersion, ctx.payload["reference_version_id"])
                versions = {block["chart_id"]: block["chart_version_id"] for block in version.content["blocks"] if block.get("chart_id")}
                if chart.id not in versions or (request.get("chart_version_id") and request["chart_version_id"] != versions[chart.id]):
                    raise AppError(409, "CHART_SNAPSHOT_FIXED", "图表问答采用报告冻结版本")
                request["chart_version_id"] = versions[chart.id]
        if name == "start_agent_task":
            action = request.get("action")
            plan_action = ctx.payload["plan"]["action"]
            allowed = {plan_action} | ({"analyze"} if plan_action == "generate_report" else set())
            if action not in allowed:
                raise AppError(409, "ACTION_NOT_AUTHORIZED", "工具操作与本轮已确认目标不一致")
            expected = ctx.payload.get("target_version_id") or ctx.payload.get("plan_report_version_id")
            if action == "revise_report" and not request.get("base_version_id"):
                raise AppError(422, "REPORT_BASE_REQUIRED", "修改报告需要传入 base_version_id；expected_current_version_id 不能代替基础版本。",
                    {"base_version_id": expected, "expected_current_version_id": expected})
            if action == "revise_report" and request.get("base_version_id") != expected:
                raise AppError(409, "REPORT_TARGET_CHANGED", "修改目标必须是本轮确认的报告版本", {"base_version_id": expected})
            if action == "export" and not request.get("report_version_id"):
                raise AppError(422, "REPORT_VERSION_REQUIRED", "导出需要传入 report_version_id。", {"report_version_id": expected})
            approved_operations = ctx.payload["plan"].get("operations", [])
            if action == "revise_report" and approved_operations:
                from app.schemas import ReportOperation
                normalize = lambda values: [ReportOperation.model_validate(item).model_dump(mode="json", exclude_none=True) for item in values]
                if normalize(request.get("operations", [])) != normalize(approved_operations):
                    raise AppError(409, "REPORT_OPERATIONS_CHANGED", "结构化修改必须与本轮确认的操作一致")
            if action == "export" and request.get("report_version_id") != expected:
                raise AppError(409, "REPORT_TARGET_CHANGED", "导出目标必须是本轮固定的报告版本")
            if action == "revise_chart" and (request.get("chart_id") != ctx.payload["plan"].get("chart_id")
                    or request.get("base_version_id") != ctx.payload.get("chart_plan_version_id")
                    or request.get("chart_mode") != ctx.payload["plan"]["chart_mode"]):
                raise AppError(409, "CHART_TARGET_CHANGED", "图表修改目标必须与本轮确认一致")
            targets = ctx.payload.get("target_block_ids") or ctx.payload["plan"].get("block_ids", [])
            if action == "revise_report" and targets and any(op.get("block_id") and op["block_id"] not in targets for op in request.get("operations", [])):
                raise AppError(409, "REPORT_TARGET_CHANGED", "只能修改已确认的正文块")
    if name in {"search_patents", "search_materials"}:
        require_execution(ctx, [name])
    elif name == "start_semantic_analysis":
        if ctx.payload["plan"]["action"] not in {"analyze", "generate_report"}:
            raise AppError(409, "ACTION_NOT_AUTHORIZED", "本轮未要求重新分析")
        if ctx.payload.get("semantic_mode") == "off":
            raise AppError(409, "STATISTICAL_ONLY", "本轮明确采用统计分析，不额外执行语义分类")
        require_execution(ctx, ["analyze"])
    elif name == "start_agent_task":
        if request["action"] == "analyze" and ctx.payload.get("semantic_mode") == "full":
            raise AppError(409, "SEMANTIC_ANALYSIS_REQUIRED", "本轮要求全量语义分析，请采用语义批次工具")
        require_execution(ctx, [request["action"]])
    elif name == "save_research_strategy":
        if ctx.payload["plan"]["action"] != "analyze" or request["analysis_id"] != ctx.payload.get("analysis_id"):
            raise AppError(409, "STRATEGY_TARGET_MISMATCH", "请先确认完善当前分析的研究方案")
        require_execution(ctx, ["analyze"])


def _collect_child(ctx, state, child_id):
    with session_scope() as session:
        parent = ctx.guard(session)
        child = owned(session, ResearchRun, child_id)
        if child.project_id != ctx.project_id or child_id not in state["children"]:
            raise AppError(404, "RUN_NOT_FOUND", "任务不属于本轮执行")
        artifacts = {key: item for key, item in child.result.items() if key in _ARTIFACTS} if child.status == "succeeded" else {}
        changed = any(state["artifacts"].get(key) != value for key, value in artifacts.items())
        state["artifacts"].update(artifacts)
        if artifacts.get("analysis_id") and parent.payload.get("analysis_id") != artifacts["analysis_id"]:
            parent.payload = {**parent.payload, "analysis_id": artifacts["analysis_id"]}
            ctx.payload = dict(parent.payload)
        status = child.status
        action = child.payload.get("action") or {"agent_analysis": "analyze", "agent_strategy": "strategy"}.get(child.kind, child.kind)
    if changed:
        ctx.emit("artifact.ready", artifacts)
    ctx.emit("activity.updated", {"id": child_id, "kind": action, "status": status, **artifacts})


def _invoke(ctx, state, name, arguments, call_id):
    if name == "prepare_agent_action":
        if not state["skills"]:
            raise AppError(422, "SKILL_REQUIRED", "请先加载本轮专业技能")
        return _prepare(ctx, arguments)
    if name == "get_run":
        arguments = {"view": "progress", **arguments}
    _, arguments = tool_service.validate_arguments(name, arguments)
    _scope_tool(ctx, state, name, arguments)
    if name == "read_agent_reference" and arguments.get("name") not in state["skills"]:
        raise AppError(422, "SKILL_REQUIRED", "请先加载对应技能")
    if name in {"search_patents", "search_materials"}:
        ctx.emit("activity.updated", {"id": call_id, "kind": name, "status": "running",
                                      "details": retrieval_details(name, arguments.get("request", {}))})
    with session_scope() as session:
        ctx.guard(session)
        value = public_value(jsonable_encoder(tool_service.invoke(session, name, arguments)))
        child_id = arguments.get("run_id") if name in {"get_run", "get_run_events"} else value.get("run_id") if isinstance(value, dict) else None
        if child_id and name in {"start_semantic_analysis", "start_agent_task", "save_research_strategy", "retry_run"}:
            child = owned(session, ResearchRun, child_id, lock=True)
            child.payload = {**child.payload, "parent_run_id": ctx.run_id,
                "conversation_id": ctx.conversation_id, "source_message_id": ctx.payload["message_id"]}
            child.conversation_id = None
    if name in {"search_patents", "search_materials"}:
        ctx.emit("activity.updated", {"id": call_id, "kind": name, "status": "succeeded", "matched_count": value.get("matched_count", 0)})
        ctx.emit("evidence.ready", {"records": value.get("items", value.get("records", [])), "matched_count": value.get("matched_count", 0)})
    if child_id and name in {"start_semantic_analysis", "start_agent_task", "save_research_strategy", "retry_run"}:
        if child_id not in state["children"]:
            state["children"].append(child_id)
    if name == "start_semantic_analysis":
        ctx.emit("activity.updated", {"id": child_id, "kind": "analyze", "status": "running",
            "phase": "识别专利技术", "processed_count": 0, "total_count": value["total_count"]})
    if name in {"get_semantic_batch", "submit_semantic_batch"} and "processed_count" in value:
        progress = {"stage": "extract", "processed_count": value["processed_count"], "total_count": value["total_count"]}
        ctx.emit("analysis.progress", progress)
        ctx.emit("activity.updated", {"id": arguments["request"]["run_id"], "kind": "analyze", "status": "running",
            "phase": "识别专利技术", **{key: progress[key] for key in ("processed_count", "total_count")}})
    if name == "load_agent_skill":
        state["skills"][value["name"]] = {"content": value["content"], "references": {}}
        ctx.emit("agent.skill.loaded", {"name": value["name"], "sha256": hashlib.sha256(value["content"].encode()).hexdigest()})
    if name == "read_agent_reference":
        if value["name"] not in state["skills"]:
            raise AppError(422, "SKILL_REQUIRED", "请先加载对应技能")
        state["skills"][value["name"]]["references"][value["resource"]] = value["content"]
    if child_id and name in {"start_agent_task", "finish_semantic_analysis", "save_research_strategy", "retry_run"}:
        from app.jobs import execute_run
        action = arguments.get("request", {}).get("action", "analyze")
        ctx.emit("activity.updated", {"id": child_id, "kind": action, "status": "running"})
        execute_run(child_id)
    if child_id and child_id in state["children"] and name in {"start_agent_task", "finish_semantic_analysis", "save_research_strategy", "retry_run", "get_run", "get_run_events"}:
        _collect_child(ctx, state, child_id)
    return value


def _messages(ctx, state):
    context = _context(ctx)
    recent = context.pop("messages")
    messages = [{"role": "system", "content": state["system_prompt"]},
                {"role": "system", "content": state["adapter"]},
                {"role": "system", "content": "Skill catalogue: " + json.dumps(list_skills(), ensure_ascii=False)},
                {"role": "system", "content": "Current fixed context and progress: " + json.dumps(
                    {**context, "progress": state["progress"], "artifacts": state["artifacts"], "children": state["children"]}, ensure_ascii=False)}]
    for name, skill in state["skills"].items():
        messages.append({"role": "system", "content": f"Loaded skill {name}:\n{skill['content']}"})
        for reference, text in skill["references"].items():
            messages.append({"role": "system", "content": f"Skill reference {name}/{reference}. Apply these rules to the corresponding tool payload; submit the result through its business tool, rather than ending the task with raw JSON.\n{text}"})
    messages.extend(recent)
    for group in state["history"][-8:]:
        messages.extend(group)
    return messages


def _answer(ctx, state, response):
    content = response.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 40000:
        raise AppError(502, "INVALID_MODEL_RESPONSE", "模型没有返回有效回答")
    if not ctx.payload.get("plan"):
        raise AppError(422, "AGENT_PREPARATION_REQUIRED", "请先完成本轮准备")
    action = ctx.payload["plan"]["action"]
    expected = {"analyze": "analysis_id", "generate_report": "report_version_id", "revise_report": "report_version_id",
                "revise_chart": "chart_version_id", "export": "file_id", "restore_report": "report_version_id"}.get(action)
    if expected and not state["artifacts"].get(expected):
        raise AppError(422, "AGENT_RESULT_INCOMPLETE", "本轮要求的业务产物尚未完成，请继续调用工具并确认结果")
    with session_scope() as session:
        ctx.guard(session)
        message = session.scalar(select(Message).where(Message.run_id == ctx.run_id, Message.role == "assistant"))
        if not message:
            message = Message(conversation_id=ctx.conversation_id, run_id=ctx.run_id, role="assistant", content=content,
                reference={"report_version_id": ctx.payload.get("reference_version_id"), "analysis_id": ctx.payload.get("analysis_id"),
                    "scope_version_id": ctx.scope_version_id, "dataset_version_ids": ctx.dataset_version_ids,
                    "citations": state["citations"]},
                cards=[{"type": "artifact", **state["artifacts"]}] if state["artifacts"] else [])
            session.add(message)
            session.flush()
        from app.jobs import emit_event
        emit_event(session, ctx.run_id, "message.completed", {"stream_id": response.get("_stream_id", ctx.run_id + ":final"),
            "message_id": message.id, "content": content})
        return {"message_id": message.id, "conversation_id": ctx.conversation_id, **state["artifacts"]}


def execute(ctx):
    if not get_settings().llm_enabled:
        raise AppError(503, "MODEL_NOT_CONFIGURED", "内置 Agent 需要配置生成模型；确定性统计接口仍可使用")
    state = deepcopy(ctx.payload.get("agent_runtime"))
    if state is None:
        system = read_system_prompt()
        state = {"system_prompt": system, "adapter": files("patentcompass_agent").joinpath("BUILTIN_RUNTIME.md").read_text(encoding="utf-8"),
                 "skills": {}, "history": [], "pending": None, "turn": 0, "children": [], "progress": {}, "artifacts": {}, "citations": []}
        state["model_name"] = get_settings().llm_model
        state["tools"] = definitions()
        state["tools_sha256"] = hashlib.sha256(json.dumps(state["tools"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        ctx.emit("agent.started", {"version": 1, "system_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
                                  "skill_count": len(list_skills()), "model_name": state["model_name"], "tools_sha256": state["tools_sha256"]})
        _save(ctx, state)
    else:
        with session_scope() as session:
            ctx.guard(session)
            for identifier in state["children"]:
                child = owned(session, ResearchRun, identifier, lock=True)
                if child.project_id != ctx.project_id:
                    raise AppError(404, "RUN_NOT_FOUND", "恢复任务不属于当前项目")
                if child.status not in {"succeeded", "failed", "cancelled"}:
                    child.payload = {**child.payload, "parent_run_id": ctx.run_id}
    while state["turn"] < 200:
        ctx.check_cancelled()
        if state["pending"] is None:
            response = stream_response(ctx, "agent_" + str(state["turn"]), _messages(ctx, state), tools=state.get("tools") or definitions())
            state["pending"] = {"response": response, "index": 0, "results": []}
            _save(ctx, state)
        pending = state["pending"]
        response = pending["response"]
        calls = response.get("tool_calls") or []
        if not calls:
            try:
                return _answer(ctx, state, response)
            except AppError as error:
                if error.code not in {"AGENT_RESULT_INCOMPLETE", "AGENT_PREPARATION_REQUIRED"}:
                    raise
                ctx.emit("message.reset", {"stream_id": response.get("_stream_id", ctx.run_id + ":invalid")})
                state["history"].append([{"role": "system", "content":
                    "运行时校验反馈，不是新的用户请求。上一条尝试未通过校验、未保存，不代表任务已经完成。"
                    + str(error) + "。请继续完成原始最新用户请求，先选择正确意图并调用所需工具；不要改答系统准备情况。"}])
                state["pending"] = None
                state["turn"] += 1
                _save(ctx, state)
                continue
        for index in range(pending["index"], len(calls)):
            call = calls[index]
            name = call.get("function", {}).get("name", "")
            receipt = {"name": name, "turn": state["turn"], "call_id": call["id"]}
            ctx.emit("agent.tool.called", receipt)
            try:
                if len(calls) > 1:
                    raise AppError(422, "AGENT_TOOL_LIMIT", "请每轮选择一个工具，避免未确认的并行操作")
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError()
                if "idempotency_key" in arguments:
                    if not isinstance(arguments["idempotency_key"], str) or not arguments["idempotency_key"]:
                        raise ValueError("idempotency_key must be a nonempty string")
                    digest = hashlib.sha256(arguments["idempotency_key"].encode()).hexdigest()[:20]
                    arguments["idempotency_key"] = f"{ctx.payload.get('operation_id', ctx.run_id)}:agent:{digest}"
                result = _invoke(ctx, state, name, arguments, call["id"])
            except (AppError, ValidationError, ValueError, KeyError) as error:
                result = {"error": {"code": getattr(error, "code", "INVALID_TOOL_REQUEST"),
                                    "message": str(error), "details": getattr(error, "details", None)}}
            result = jsonable_encoder(public_value(result))
            summary = {key: result[key] for key in (_ARTIFACTS | {"run_id", "status"}) - {"download_url"}
                       if isinstance(result, dict) and key in result}
            failed = isinstance(result, dict) and bool(result.get("error"))
            ctx.emit("agent.tool.completed", {**receipt, **summary, "outcome": "error" if failed else "returned",
                **({"error_code": result["error"].get("code", "TOOL_ERROR")} if failed else {})})
            pending["results"].append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            if isinstance(result, dict):
                state["progress"][name] = {key: item for key, item in result.items() if key in _PROGRESS or key == "error"}
                if name in {"search_patents", "search_materials"}:
                    for row in result.get("items", result.get("records", [])):
                        if row not in state["citations"]:
                            state["citations"].append(row)
            pending["index"] = index + 1
            _save(ctx, state)
        state["history"].append([{"role": "assistant", "content": response.get("content"), "tool_calls": calls,
            **{key: response[key] for key in ("_response_items", "_generation_protocol") if key in response}}, *pending["results"]])
        completed_name = calls[-1].get("function", {}).get("name")
        completed_result = json.loads(pending["results"][-1]["content"])
        if completed_name == "submit_semantic_batch" and "error" not in completed_result:
            # Completed raw batches and label payloads remain in backend checkpoints;
            # the next model round needs their receipts, not repeated patent text.
            state["history"] = [group for group in state["history"] if not any(
                call.get("function", {}).get("name") in {"get_semantic_batch", "submit_semantic_batch"}
                for call in group[0].get("tool_calls", []))]
        state["history"] = state["history"][-8:]
        state["pending"] = None
        state["turn"] += 1
        _save(ctx, state)
    raise AppError(422, "AGENT_STEP_LIMIT", "本轮达到执行步数上限，已完成任务和批次均已保留")
