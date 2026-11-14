"""Plain capability data and execution hooks; no application or storage imports."""

from datetime import date
from typing import Any, Callable, Literal, Protocol, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CapabilityError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code, self.code, self.message, self.details = status_code, code, message, details


class PatentRecord(BaseModel):
    model_config = ConfigDict(extra="allow", from_attributes=True)

    id: str = ""
    dataset_version_id: str | None = None
    application_number: str = ""
    application_date: date | None = None
    title: str = ""
    abstract: str = ""
    applicants: list[str] = Field(default_factory=list)
    inventors: list[str] = Field(default_factory=list)
    ipc_codes: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    priorities: list[dict] = Field(default_factory=list)
    raw_record: dict = Field(default_factory=dict)
    source_sheet: str = ""
    source_row: int = 0
    publications: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def stable_identity(self):
        if not self.id:
            self.id = str(uuid5(NAMESPACE_URL, "patent:" + ":".join(
                [self.dataset_version_id or "", self.application_number, self.title, self.abstract])))
        return self


class DataScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_version_ids: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)


class ReportOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    op: Literal["set_title", "replace_text", "insert_block", "remove_block", "move_block", "append_roadmap", "insert_chart", "update_chart", "set_layout", "set_chart_layout"]
    block_id: str | None = None
    text: str | None = Field(default=None, max_length=40000)
    block: dict[str, Any] | None = None
    after_id: str | None = None
    chart_id: UUID | None = None
    chart_version_id: UUID | None = None
    layout: dict[str, Any] | None = None


class ChartSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    version_id: str
    kind: str
    data: dict
    option: dict
    evidence_snapshot: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_identifiers(self):
        if any(not isinstance(item.get("id"), str) or not item["id"] for item in self.evidence_snapshot):
            raise ValueError("图表证据需要明确 id")
        return self


class ReportDocument(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str
    layout: dict = Field(default_factory=dict)
    blocks: list[dict] = Field(default_factory=list)
    source: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_blocks(self):
        identifiers = []
        for block in self.blocks:
            if not isinstance(block.get("id"), str) or not block["id"]:
                raise ValueError("报告正文块需要非空 id")
            identifiers.append(block["id"])
            kind = block.get("type")
            if kind in {"heading", "paragraph"}:
                if not isinstance(block.get("text"), str):
                    raise ValueError("报告文字块需要 text")
                if kind == "heading" and (type(block.get("level", 1)) is not int or not 1 <= block.get("level", 1) <= 6):
                    raise ValueError("标题级别需要 1 到 6 的整数")
            elif kind == "table":
                if not isinstance(block.get("columns"), list) or not block["columns"] or not isinstance(block.get("rows"), list):
                    raise ValueError("报告表格需要 columns 和 rows")
                if any(not isinstance(column, str) for column in block["columns"]) or any(
                        not isinstance(row, (list, dict)) for row in block["rows"]):
                    raise ValueError("表格列名需要文本，表格行需要列表或对象")
            elif kind == "chart":
                if not isinstance(block.get("chart"), dict) or not isinstance(block["chart"].get("option"), dict):
                    raise ValueError("报告图表需要冻结的 chart.option")
            else:
                raise ValueError("不支持的报告正文块类型")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("报告正文块 id 不可重复")
        return self


T = TypeVar("T")


class ExecutionHooks(Protocol):
    def step(self, key: str, callback: Callable[[], T]) -> T: ...
    def check_cancelled(self) -> None: ...
    def emit(self, event_type: str, payload: dict) -> None: ...


class InlineExecution:
    """Direct calls need neither a task queue nor persistent execution state."""

    def step(self, key, callback):
        return callback()

    def check_cancelled(self):
        pass

    def emit(self, event_type, payload):
        pass
