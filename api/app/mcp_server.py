"""MCP protocol adapter over the shared capability and task services."""

import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from app.db import session_scope
from app.errors import AppError
from app.resource_service import idempotent, public_value
from app import task_service


logger = logging.getLogger(__name__)
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200)]


def _result(value, *, error=False):
    value = jsonable_encoder(public_value(value))
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                          structured_content=value, is_error=error)


def _execute(callback):
    try:
        # Synchronous SDK tools execute in worker threads. Sessions belong to
        # this invocation and are never passed from an HTTP request thread.
        with session_scope() as session:
            value = callback(session)
        return _result(value)
    except AppError as error:
        return _result({"error": {"code": error.code, "message": error.message,
                                  "status_code": error.status_code, "details": error.details}}, error=True)
    except Exception:
        logger.exception("MCP tool execution failed")
        return _result({"error": {"code": "INTERNAL_ERROR", "message": "服务暂时无法完成请求",
                                  "status_code": 500, "details": None}}, error=True)


def _capability_tool(service, name, spec):
    input_model = spec.input_model
    if spec.asynchronous:
        def execute(request: input_model, idempotency_key: IdempotencyKey) -> CallToolResult:
            return _execute(lambda session: service.invoke(session, name, request.model_dump(mode="json"),
                                                          idempotency_key)[0])
    else:
        def execute(request: input_model) -> CallToolResult:
            return _execute(lambda session: service.invoke(session, name, request.model_dump(mode="json"), None)[0])
    return execute


def create_mcp():
    from app import capability_service

    server = MCPServer("PatentCompass", version="0.1.0",
        instructions="使用已授权的专利样本和固定版本。耗时操作返回任务标识；通过任务工具查询状态及产物。")
    for name, spec in capability_service.CAPABILITIES.items():
        server.add_tool(_capability_tool(capability_service, name, spec), name=name, description=spec.description,
                        annotations=ToolAnnotations(read_only_hint=not spec.asynchronous,
                                                    idempotent_hint=True, open_world_hint=False))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    def get_run(run_id: UUID) -> CallToolResult:
        """读取同一持久任务的状态、进度、结果、错误和待确认问题。"""
        return _execute(lambda session: task_service.get_run(session, str(run_id)))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    def get_run_events(run_id: UUID, after: Annotated[int, Field(ge=0)] = 0,
                       limit: Annotated[int, Field(ge=1, le=200)] = 200) -> CallToolResult:
        """按序号读取任务事件；断线后可继续查询同一任务。"""
        return _execute(lambda session: task_service.get_run_events(session, str(run_id), after, limit))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False))
    def cancel_run(run_id: UUID, idempotency_key: IdempotencyKey) -> CallToolResult:
        """请求取消任务并返回实际状态；请求已接受不等于执行已停止。"""
        identifier = str(run_id)
        return _execute(lambda session: idempotent(session, f"POST /api/runs/{identifier}/cancel", idempotency_key,
            {}, lambda: task_service.cancel_run(session, identifier))[0])

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False))
    def retry_run(run_id: UUID, idempotency_key: IdempotencyKey) -> CallToolResult:
        """为失败任务创建关联重试，保留原数据和范围；同一幂等标识不重复创建。"""
        identifier = str(run_id)
        return _execute(lambda session: idempotent(session, f"POST /api/runs/{identifier}/retry", idempotency_key,
            {}, lambda: task_service.retry_run(session, identifier), status_code=202)[0])

    application = server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)
    return server, application
