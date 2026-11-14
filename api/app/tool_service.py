"""One typed tool contract and dispatcher for MCP and the built-in agent."""

from dataclasses import dataclass
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError, create_model

from app import agent_service, capability_service, task_service
from app.capability_contracts import EventsInput, Input, RunInput
from app.errors import AppError
from app.resource_service import idempotent
from patentcompass_agent import list_skills, read_skill


class SkillInput(Input):
    name: str = Field(min_length=1, max_length=100)


class ReferenceInput(SkillInput):
    reference: str = Field(min_length=1, max_length=150)


class WriteRunInput(RunInput):
    idempotency_key: str = Field(min_length=1, max_length=200)


class RunStatusInput(RunInput):
    view: Literal["full", "progress"] = "full"


@dataclass(frozen=True)
class Tool:
    arguments: type[Input]
    description: str
    read_only: bool
    group: str

    def definition(self, name):
        schema = self.arguments.model_json_schema()
        definitions = schema.pop("$defs", {})

        def expand(value):
            if isinstance(value, list):
                return [expand(item) for item in value]
            if isinstance(value, dict):
                if "$ref" in value:
                    target = definitions[value["$ref"].removeprefix("#/$defs/")]
                    return expand({**target, **{key: item for key, item in value.items() if key != "$ref"}})
                return {key: expand(item) for key, item in value.items()}
            return value

        return {"type": "function", "function": {"name": name, "description": self.description,
                "parameters": expand(schema)}}


def catalogue():
    result = {}
    for group, specs in (("capability", capability_service.CAPABILITIES), ("agent", agent_service.TOOLS)):
        for name, spec in specs.items():
            read_only = not spec.asynchronous if group == "capability" else spec.read_only
            fields = {"request": (spec.input_model, ...)}
            if not read_only:
                fields["idempotency_key"] = (str, Field(min_length=1, max_length=200))
            arguments = create_model(name + "Arguments", __config__=ConfigDict(extra="forbid"), **fields)
            result[name] = Tool(arguments, spec.description, read_only, group)
    for name, model, description, read_only in (
        ("get_run", RunStatusInput, "读取任务状态、结果、错误和待确认问题；轮询时使用 progress 视图，省略检查点原文并保留批次进度", True),
        ("get_run_events", EventsInput, "按序号读取任务事件", True),
        ("cancel_run", WriteRunInput, "请求取消任务，接受请求不等于执行已停止", False),
        ("retry_run", WriteRunInput, "创建关联重试任务，复用兼容检查点", False),
        ("list_agent_skills", Input, "发现同一套专业技能的名称与描述", True),
        ("load_agent_skill", SkillInput, "按名称读取完整 SKILL.md 及可用引用目录", True),
        ("read_agent_reference", ReferenceInput, "按需读取已命名技能的受控参考文件", True),
    ):
        result[name] = Tool(model, description, read_only, "task" if "run" in name else "skill")
    return result


def validate_arguments(name, arguments):
    spec = catalogue().get(name)
    if spec is None:
        raise AppError(422, "UNKNOWN_AGENT_TOOL", "未知工具")
    try:
        request = spec.arguments.model_validate(arguments)
    except ValidationError as error:
        raise AppError(422, "VALIDATION_ERROR", "工具参数无效", error.errors(include_context=False)) from error
    value = request.model_dump(mode="json")
    return spec, value


def invoke(session, name, arguments):
    spec, value = validate_arguments(name, arguments)
    if spec.group == "capability":
        return capability_service.invoke(session, name, value["request"], value.get("idempotency_key"))[0]
    if spec.group == "agent":
        return agent_service.invoke(session, name, value["request"], value.get("idempotency_key"))
    if name == "get_run":
        return task_service.get_run(session, value["run_id"], view=value["view"])
    if name == "get_run_events":
        return task_service.get_run_events(session, value["run_id"], value["after"], value["limit"])
    if name in {"cancel_run", "retry_run"}:
        action = "cancel" if name == "cancel_run" else "retry"
        callback = task_service.cancel_run if action == "cancel" else task_service.retry_run
        return idempotent(session, f"POST /api/runs/{value['run_id']}/{action}", value["idempotency_key"], {},
                          lambda: callback(session, value["run_id"]), 200 if action == "cancel" else 202)[0]
    if name == "list_agent_skills":
        return {"skills": list_skills()}
    try:
        return read_skill(value["name"], value.get("reference"))
    except ValueError as error:
        raise AppError(422, "SKILL_RESOURCE_NOT_FOUND", "技能或引用文件不存在") from error
