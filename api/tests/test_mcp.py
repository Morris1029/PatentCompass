import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import socket
import time
from types import SimpleNamespace
from uuid import uuid4

from mcp import Client
from pydantic import BaseModel, ConfigDict
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
import uvicorn

from app import mcp_server, tool_service
from app.errors import AppError


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str


@dataclass
class Spec:
    input_model: type[BaseModel]
    description: str
    asynchronous: bool


@pytest.fixture
def controlled_mcp(monkeypatch):
    import app

    sessions, calls, cached = [], [], {}

    @contextmanager
    def session_scope():
        value = object()
        sessions.append((value, threading.get_ident()))
        yield value

    def invoke(session, name, payload, key):
        calls.append((session, name, payload, key))
        if payload["q"] == "forbidden":
            raise AppError(404, "NOT_FOUND", "记录不存在")
        if payload["q"] == "unavailable":
            raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置模型")
        return {"name": name, "query": payload["q"], "run_id": "run" if key else None,
                "_response_items": ["private provider state"], "created_at": datetime(2026, 10, 26, tzinfo=timezone.utc)}, 202 if key else 200

    service = SimpleNamespace(CAPABILITIES={
        "search_patents": Spec(SearchInput, "Search fixed patent samples", False),
        "analyze_patents": Spec(SearchInput, "Submit analysis", True)}, invoke=invoke)
    monkeypatch.setattr(app, "capability_service", service, raising=False)
    monkeypatch.setattr(tool_service, "capability_service", service)
    monkeypatch.setattr(mcp_server, "session_scope", session_scope)
    monkeypatch.setattr(tool_service.task_service, "get_run", lambda session, identifier, view="full": {"run_id": identifier, "status": "running"})
    monkeypatch.setattr(tool_service.task_service, "get_run_events", lambda session, identifier, after, limit: {
        "run_id": identifier, "after": after, "limit": limit, "items": []})
    monkeypatch.setattr(tool_service.task_service, "cancel_run", lambda session, identifier: {"run_id": identifier, "status": "cancelling"})
    monkeypatch.setattr(tool_service.task_service, "retry_run", lambda session, identifier: {"run_id": "retry", "retry_of_id": identifier, "status": "queued"})

    def idempotent(session, operation, key, payload, callback, status_code=200):
        if (operation, key) not in cached:
            cached[(operation, key)] = callback(), status_code
        return cached[(operation, key)]

    monkeypatch.setattr(tool_service, "idempotent", idempotent)
    server, application = mcp_server.create_mcp()
    return server, application, sessions, calls, cached


def test_mcp_discovers_typed_capabilities_and_calls_shared_services_in_own_thread(controlled_mcp):
    server, _, sessions, calls, _ = controlled_mcp
    caller_thread = threading.get_ident()

    async def verify():
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            from app.agent_service import TOOLS
            assert set(tools) == {"search_patents", "analyze_patents", "get_run", "get_run_events", "cancel_run", "retry_run",
                                  "list_agent_skills", "load_agent_skill", "read_agent_reference"} | set(TOOLS)
            assert tools["search_patents"].input_schema["properties"]["request"]["$ref"].endswith("SearchInput")
            assert "idempotency_key" in tools["analyze_patents"].input_schema["required"]
            assert "idempotency_key" not in tools["search_patents"].input_schema["properties"]
            result = await client.call_tool("search_patents", {"request": {"q": "landing"}})
            assert not result.is_error
            assert result.structured_content["query"] == "landing"
            assert "_response_items" not in result.structured_content
            assert result.structured_content["created_at"].startswith("2026-10-26")
            result = await client.call_tool("analyze_patents", {"request": {"q": "landing"}, "idempotency_key": "analysis-one"})
            assert result.structured_content["run_id"] == "run"

    asyncio.run(verify())
    assert calls[0][2:] == ({"q": "landing"}, None)
    assert calls[1][2:] == ({"q": "landing"}, "analysis-one")
    assert all(thread != caller_thread for _, thread in sessions)
    assert calls[0][0] is not calls[1][0]


@pytest.mark.parametrize("query,code", [("forbidden", "NOT_FOUND"), ("unavailable", "MODEL_NOT_CONFIGURED")])
def test_mcp_returns_structured_business_errors(controlled_mcp, query, code):
    server = controlled_mcp[0]

    async def verify():
        async with Client(server) as client:
            result = await client.call_tool("search_patents", {"request": {"q": query}})
            assert result.is_error
            assert result.structured_content["error"]["code"] == code

    asyncio.run(verify())


def test_mcp_task_tools_share_canonical_operations_and_actual_cancel_state(controlled_mcp):
    server, _, _, _, cached = controlled_mcp
    identifier = str(uuid4())

    async def verify():
        async with Client(server) as client:
            current = await client.call_tool("get_run", {"run_id": identifier})
            assert current.structured_content == {"run_id": identifier, "status": "running"}
            events = await client.call_tool("get_run_events", {"run_id": identifier, "after": 4, "limit": 10})
            assert events.structured_content["after"] == 4
            cancelled = await client.call_tool("cancel_run", {"run_id": identifier, "idempotency_key": "cancel-one"})
            assert cancelled.structured_content["status"] == "cancelling"
            retried = await client.call_tool("retry_run", {"run_id": identifier, "idempotency_key": "retry-one"})
            same = await client.call_tool("retry_run", {"run_id": identifier, "idempotency_key": "retry-one"})
            assert retried.structured_content == same.structured_content

    asyncio.run(verify())
    assert cached[(f"POST /api/runs/{identifier}/cancel", "cancel-one")][1] == 200
    assert cached[(f"POST /api/runs/{identifier}/retry", "retry-one")][1] == 202


def test_mcp_rejects_invalid_tool_arguments_before_invoking_capability(controlled_mcp):
    server, _, _, calls, _ = controlled_mcp

    async def verify():
        async with Client(server) as client:
            for arguments in ({"request": {"q": "landing"}},
                              {"request": {"q": "landing"}, "idempotency_key": ""},
                              {"request": {"q": "landing", "owner_id": "someone-else"}, "idempotency_key": "key"}):
                result = await client.call_tool("analyze_patents", arguments)
                assert result.is_error

    asyncio.run(verify())
    assert not calls


def test_streamable_http_mount_lifespan_and_reconnect_keep_business_task_reference(controlled_mcp, monkeypatch):
    import app

    server, application, _, _, _ = controlled_mcp
    identifier = str(uuid4())
    saved = {}
    original = app.capability_service.invoke

    def submit(session, name, payload, key):
        result, status = original(session, name, payload, key)
        result["run_id"] = identifier
        saved[identifier] = {"run_id": identifier, "status": "queued", "query": payload["q"]}
        return result, status

    monkeypatch.setattr(app.capability_service, "invoke", submit)
    monkeypatch.setattr(tool_service.task_service, "get_run", lambda session, run_id, view="full": saved[run_id])

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    host = Starlette(routes=[Mount("/mcp", application)], lifespan=lifespan)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(host, log_level="error", lifespan="on"))
    thread = threading.Thread(target=running.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not running.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert running.started

        async def verify():
            url = f"http://127.0.0.1:{port}/mcp/"
            async with Client(url) as client:
                tools = await client.list_tools()
                assert "analyze_patents" in {tool.name for tool in tools.tools}
                submitted = await client.call_tool("analyze_patents", {
                    "request": {"q": "landing"}, "idempotency_key": "http-submission"})
                assert submitted.structured_content["run_id"] == identifier
                denied = await client.call_tool("search_patents", {"request": {"q": "forbidden"}})
                assert denied.is_error and denied.structured_content["error"]["code"] == "NOT_FOUND"
            async with Client(url) as reconnected:
                current = await reconnected.call_tool("get_run", {"run_id": identifier})
                assert current.structured_content == saved[identifier]

        asyncio.run(verify())
    finally:
        running.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()
