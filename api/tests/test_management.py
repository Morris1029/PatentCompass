import base64
from datetime import timedelta
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from app import agent_runtime, jobs, model_provider
from app.config import get_settings
from app.main import create_app
from app.models import Project, ResearchRun, RunEvent, utcnow
from patentcompass_agent import list_skills, read_skill, read_system_prompt


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


def add_run(session, project, **patch):
    row = ResearchRun(owner_id=project.owner_id, project_id=project.id, kind="auto", status="succeeded", **patch)
    session.add(row)
    session.flush()
    return row


def test_configuration_uses_actual_shared_loaders_and_never_returns_secrets(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "PRIVATE-TOKEN")
    monkeypatch.setattr(settings, "llm_base_url", "https://user:password@private.invalid/v1")
    data = client.get("/api/management/configuration").json()
    assert data["identity"] == {"owner_id": settings.dev_owner_id, "display_name": "本地用户"}
    assert data["agent"]["system_prompt"]["content"] == read_system_prompt()
    assert data["skills"] == list_skills()
    assert data["agent"]["tools"] == agent_runtime.definitions()
    from app.tool_service import catalogue
    assert len(data["mcp"]["tools"]) == len(catalogue()) == 40
    assert len(data["agent"]["tools"]) == 27
    assert "prepare_agent_action" not in {item["name"] for item in data["mcp"]["tools"]}
    assert not {"PRIVATE-TOKEN", "private.invalid", "password"}.intersection(json.dumps(data).split())
    assert "PRIVATE-TOKEN" not in json.dumps(data) and "private.invalid" not in json.dumps(data)
    path = "/api/management/skills/patent-evidence-qa"
    skill = client.get(path).json()
    assert skill["content"] == read_skill("patent-evidence-qa")["content"]
    ref = skill["references"][0]
    assert client.get(path, params={"reference": ref}).json()["content"] == read_skill("patent-evidence-qa", ref)["content"]
    assert client.get(path, params={"reference": "../../SYSTEM_PROMPT.md"}).status_code == 404
    assert client.get("/api/management/skills/unknown").status_code == 404
    assert client.post("/api/management/configuration", json={}).status_code == 405


def test_owned_runs_filter_paginate_and_hide_child_and_private_payload(client, db_session, project):
    root = add_run(db_session, project, payload={"secret": "DO-NOT-EXPOSE"})
    later = add_run(db_session, project, created_at=utcnow() + timedelta(seconds=1))
    child = add_run(db_session, project, payload={"parent_run_id": root.id})
    other = Project(owner_id="another-user", name="private project")
    db_session.add(other)
    db_session.flush()
    private = add_run(db_session, other)
    first = client.get("/api/management/runs", params={"limit": 1}).json()
    assert first["total"] == 2 and first["items"][0]["id"] == later.id
    second = client.get("/api/management/runs", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["id"] == root.id and second["next_cursor"] is None
    assert "DO-NOT-EXPOSE" not in json.dumps(second)
    assert client.get("/api/management/runs", params={"project_id": project.id, "status": "running"}).json()["total"] == 0
    assert client.get("/api/management/runs", params={"cursor": "bad"}).status_code == 422
    invalid = base64.urlsafe_b64encode(json.dumps({"created_at": utcnow().isoformat(), "id": 123}).encode()).decode()
    assert client.get("/api/management/runs", params={"cursor": invalid}).status_code == 422
    assert client.get("/api/management/runs", params={"status": "invalid"}).status_code == 422
    assert client.get("/api/management/runs", params={"project_id": other.id}).status_code == 404
    assert client.get(f"/api/management/runs/{private.id}").status_code == 404
    assert client.get(f"/api/management/runs/{private.id}/events").status_code == 404
    data = client.get(f"/api/management/runs/{root.id}").json()
    assert data["snapshot"]["model_name"] is None and data["snapshot"]["system_prompt"] is None
    assert [item["id"] for item in data["children"]] == [child.id]


def test_snapshot_is_historical_and_event_projection_is_bounded(client, db_session, project, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "PRIVATE-TOKEN")
    run = add_run(db_session, project, payload={"agent_runtime": {"system_prompt": "historical prompt", "adapter": "historical adapter",
        "skills": {"saved-skill": {"content": "old skill", "references": {"references/old.md": "old reference"}}},
        "history": [{"provider": "DO-NOT-EXPOSE"}], "tools": [], "model_name": "historical-model"}},
        result={"file_id": "export-id", "download_url": "https://untrusted.invalid", "raw": "DO-NOT-EXPOSE"})
    run.status = "waiting_input"
    run.input_request = {"kind": "execution_confirmation", "prompt": "请确认本轮检索", "raw": "DO-NOT-EXPOSE"}
    for seq, event_type, payload in [(1, "message.delta", {"content": "DO-NOT-EXPOSE"}),
        (2, "agent.tool.called", {"name": "search_patents", "call_id": "call-1", "turn": 2, "raw": "DO-NOT-EXPOSE"}),
        (3, "agent.tool.completed", {"name": "search_patents", "call_id": "call-1", "outcome": "error", "error_code": "INVALID_TOOL_REQUEST"}),
        (4, "input.required", {"kind": "execution_confirmation", "secret": "PRIVATE-TOKEN"})]:
        db_session.add(RunEvent(run_id=run.id, seq=seq, event_type=event_type, payload=payload))
    db_session.flush()
    data = client.get(f"/api/management/runs/{run.id}").json()
    assert data["snapshot"]["system_prompt"]["content"] == "historical prompt"
    assert data["waiting_input"] == {"kind": "execution_confirmation", "title": "请确认本轮检索"}
    assert data["snapshot"]["skills"][0]["references"][0]["content"] == "old reference"
    assert data["artifacts"]["download_url"] == "/api/files/export-id"
    assert "DO-NOT-EXPOSE" not in json.dumps(data)
    first = client.get(f"/api/management/runs/{run.id}/events", params={"limit": 1}).json()
    assert first["has_more"] and first["items"][0]["seq"] == 2
    tail = client.get(f"/api/management/runs/{run.id}/events", params={"after": first["after"]}).json()
    assert len(tail["items"]) == 2 and not tail["has_more"]
    assert "DO-NOT-EXPOSE" not in json.dumps(first) and "PRIVATE-TOKEN" not in json.dumps(tail)


def test_new_runtime_records_model_tool_contract_and_correlated_receipts(client, db_session, project, monkeypatch):
    from test_agent_runtime import Director
    settings = get_settings()
    for key, value in {"llm_model": "controlled-host", "llm_base_url": "https://controlled.invalid", "llm_api_key": "test"}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)

    def script():
        yield "load_agent_skill", {"name": "patent-evidence-qa"}
        yield "prepare_agent_action", {"action": "chat", "reason": "解释当前能力，不检索数据"}
        yield "get_agent_context", {"request": {"project_id": project.id}}
        yield "load_agent_skill", {"name": "missing-skill"}
        return "当前可以按需加载技能并调用共享工具。"

    monkeypatch.setattr(model_provider, "stream_chat", Director(script()))
    response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {"content": "解释当前能力", "action": "auto"}})
    assert response.status_code == 202, response.text
    identifier = response.json()["run_id"]
    jobs.execute_run(identifier)
    data = client.get(f"/api/management/runs/{identifier}").json()
    assert data["status"] == "succeeded", data
    snapshot = data["snapshot"]
    assert snapshot["model_name"] == "controlled-host" and snapshot["tools"] == agent_runtime.definitions()
    assert snapshot["tools_sha256"] == hashlib.sha256(json.dumps(snapshot["tools"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    events = client.get(f"/api/management/runs/{identifier}/events").json()["items"]
    calls = [item["payload"] for item in events if item["event_type"] == "agent.tool.called"]
    returns = [item["payload"] for item in events if item["event_type"] == "agent.tool.completed"]
    assert {item["call_id"] for item in calls} == {item["call_id"] for item in returns}
    assert returns[-1]["outcome"] == "error" and returns[-1]["error_code"] == "SKILL_RESOURCE_NOT_FOUND"
