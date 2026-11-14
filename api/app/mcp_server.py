"""MCP protocol adapter over the shared capability and task services."""

import json
import logging

from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from app.db import session_scope
from app.errors import AppError
from app.resource_service import public_value


logger = logging.getLogger(__name__)


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


def create_mcp():
    from inspect import Parameter, Signature
    from app import tool_service

    def bind(name, spec):
        def execute(**arguments) -> CallToolResult:
            return _execute(lambda session: tool_service.invoke(session, name, arguments))
        annotations = {key: field.rebuild_annotation() for key, field in spec.arguments.model_fields.items()}
        execute.__annotations__ = {**annotations, "return": CallToolResult}
        execute.__signature__ = Signature([Parameter(key, Parameter.POSITIONAL_OR_KEYWORD,
            annotation=annotations[key], default=Parameter.empty if field.is_required() else field.default)
            for key, field in spec.arguments.model_fields.items()], return_annotation=CallToolResult)
        return execute

    server = MCPServer("PatentCompass", version="0.1.0",
        instructions="Use authorized fixed versions. Discover skills when needed. Long operations return a run_id; inspect the run for actual completion.")
    for name, spec in tool_service.catalogue().items():
        server.add_tool(bind(name, spec), name=name, description=spec.description,
            annotations=ToolAnnotations(read_only_hint=spec.read_only, idempotent_hint=True, open_world_hint=False))
    application = server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)
    return server, application
