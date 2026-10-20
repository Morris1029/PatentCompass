from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectCreate(RequestModel):
    name: str = Field(min_length=1, max_length=300, validation_alias=AliasChoices("name", "title"))
    scope: dict[str, Any] = Field(default_factory=dict)


class ScopeUpdate(RequestModel):
    research_id: UUID | None = None
    expected_scope_version_id: UUID
    changes: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class DatasetBinding(RequestModel):
    research_id: UUID | None = None
    expected_revision: int = Field(ge=0)
    dataset_version_ids: list[UUID] = Field(min_length=1, max_length=1)


class MessageReference(RequestModel):
    report_version_id: UUID
    block_id: str | None = None
    block_ids: list[str] = Field(default_factory=list, max_length=100)
    text: str = Field(default="", max_length=20000)


class MessageCreate(RequestModel):
    research_id: UUID | None = None
    content: str = Field(default="", max_length=40000)
    reference: MessageReference | None = None
    action: Literal["chat", "analyze", "generate_report", "auto"] = "chat"
    attachments: list[UUID] = Field(default_factory=list, max_length=10)
    report_mode: Literal["statistical", "ai"] = "statistical"
    semantic_mode: Literal["off", "full"] = "off"

    @model_validator(mode="after")
    def meaningful(self):
        if not self.content and not self.attachments:
            raise ValueError("请输入文字或添加资料")
        return self


class ConversationCreate(RequestModel):
    initial_message: MessageCreate


class RunCreate(RequestModel):
    research_id: UUID | None = None
    action: Literal["analyze", "generate_report", "index"]
    analysis_id: UUID | None = None
    report_mode: Literal["statistical", "ai"] = "statistical"
    semantic_mode: Literal["off", "full"] = "off"


class RunInput(RequestModel):
    input_request_id: UUID
    expected_scope_version_id: UUID | None = None
    values: dict[str, Any]


class ReportOperation(RequestModel):
    op: Literal["set_title", "replace_text", "insert_block", "remove_block", "move_block", "append_roadmap", "insert_chart", "update_chart", "set_layout", "set_chart_layout"]
    block_id: str | None = None
    text: str | None = Field(default=None, max_length=40000)
    block: dict[str, Any] | None = None
    after_id: str | None = None
    chart_id: UUID | None = None
    chart_version_id: UUID | None = None
    layout: dict[str, Any] | None = None


class ReportRevision(RequestModel):
    research_id: UUID | None = None
    base_version_id: UUID
    expected_current_version_id: UUID
    block_id: str | None = Field(default=None, min_length=1)
    replacement_text: str | None = Field(default=None, max_length=40000)
    instruction: str | None = Field(default=None, min_length=1, max_length=10000)
    operations: list[ReportOperation] | None = Field(default=None, min_length=1, max_length=100)
    conversation_id: UUID | None = None

    @model_validator(mode="after")
    def one_edit(self):
        if self.operations is not None:
            if self.block_id or self.replacement_text is not None or self.instruction:
                raise ValueError("operations 与旧单块修改形式互斥")
            return self
        if not self.block_id:
            raise ValueError("单块修改需要 block_id")
        if (self.replacement_text is None) == (self.instruction is None):
            raise ValueError("replacement_text 与 instruction 必须且只能提供一项")
        return self


class ChartRevision(RequestModel):
    research_id: UUID | None = None
    base_version_id: UUID
    expected_current_version_id: UUID
    mode: Literal["style", "data", "insight"]
    option: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    instruction: str = Field(default="", max_length=10000)


class ClassificationCorrection(RequestModel):
    application_id: UUID
    dimension: Literal["elements", "methods", "scenarios"]
    theme_ids: list[str] = Field(max_length=20)


class AnalysisRevision(RequestModel):
    research_id: UUID | None = None
    base_analysis_id: UUID
    expected_current_analysis_id: UUID
    corrections: list[ClassificationCorrection] = Field(min_length=1, max_length=5200)


class MaterialResponse(BaseModel):
    model_config = ConfigDict(extra="allow", from_attributes=True)
    id: UUID
    project_id: UUID
    file_id: UUID
    status: str
    run_id: UUID | None
    version: int


class CurrentVersionUpdate(RequestModel):
    version_id: UUID
    expected_current_version_id: UUID


class ResearchCreate(RequestModel):
    base_revision_id: UUID


class ResearchAdopt(RequestModel):
    expected_current_version_id: UUID


class ResearchRestore(RequestModel):
    revision_id: UUID
    expected_current_version_id: UUID
    expected_adopted_version_id: UUID


class ReportExport(RequestModel):
    report_version_id: UUID
    format: Literal["docx"] = "docx"


class Resource(BaseModel):
    model_config = ConfigDict(extra="allow", from_attributes=True)
    id: UUID
    created_at: datetime


class ProjectResponse(Resource):
    name: str
    revision: int
    scope_version_id: UUID | None
    dataset_version_id: UUID | None
    current_analysis_id: UUID | None


class ScopeResponse(Resource):
    project_id: UUID
    number: int
    confirmed: bool
    content: dict[str, Any]


class DatasetResponse(Resource):
    name: str
    source_sha256: str
    versions: list[dict[str, Any]]


class QualityResponse(BaseModel):
    dataset_id: UUID
    dataset_version_id: UUID
    status: str
    quality: dict[str, Any]
    source_metadata: dict[str, Any]
    source_file_id: UUID
    base_version_id: UUID | None = None
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)


class RunResponse(Resource):
    project_id: UUID | None
    kind: str
    status: str
    result: dict[str, Any]
    error: dict[str, Any] | None
    input_request: dict[str, Any] | None
    dataset_version_ids: list[str]
    scope_version_id: UUID | None
    origin_message_id: UUID | None = None
    activities: list[dict[str, Any]] = Field(default_factory=list)
    stream: dict[str, Any] | None = None


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    run_id: UUID
    status: str


class ConversationResponse(Resource):
    project_id: UUID
    title: str


class MessageResponse(Resource):
    conversation_id: UUID
    run_id: UUID | None
    role: str
    content: str
    reference: dict[str, Any] | None
    attachments: list[str] = Field(default_factory=list)
    cards: list[dict[str, Any]] = Field(default_factory=list)


class PatentResponse(Resource):
    dataset_version_id: UUID
    application_number: str
    title: str
    abstract: str
    applicants: list[str]
    ipc_codes: list[str]


class ChartResponse(Resource):
    project_id: UUID
    analysis_id: UUID
    version_id: UUID
    kind: str
    option: dict[str, Any]
    data: dict[str, Any]
    provenance: dict[str, Any]


class EvidenceResponse(Resource):
    project_id: UUID
    kind: str
    content: dict[str, Any]


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str
    analysis_id: UUID | None = None
    data: dict[str, Any] | list[Any] | None = None


class ReportVersionResponse(Resource):
    research_id: UUID | None = None
    research_number: int | None = None
    main_conversation_id: UUID | None = None
    source_conversation_id: UUID | None = None
    source_message_id: UUID | None = None
    change_kind: str = "edit"
    change_count: int = 0
    change_summary: dict[str, Any] = Field(default_factory=dict)
    restored_from_id: UUID | None = None
    report_id: UUID
    number: int
    parent_id: UUID | None
    analysis_id: UUID
    scope_version_id: UUID
    dataset_version_ids: list[str]
    content: dict[str, Any]
    chart_ids: list[str]
    instruction: str
    summary: str


class ReportVersionSummary(Resource):
    research_id: UUID | None = None
    research_number: int | None = None
    main_conversation_id: UUID | None = None
    source_conversation_id: UUID | None = None
    source_message_id: UUID | None = None
    change_kind: str = "edit"
    change_count: int = 0
    change_summary: dict[str, Any] = Field(default_factory=dict)
    restored_from_id: UUID | None = None
    number: int
    parent_id: UUID | None
    summary: str
    is_current: bool


class AdoptedVersionResponse(BaseModel):
    report_id: UUID
    current_version_id: UUID


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: Any = None
    request_id: str


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None
    returned_count: int = 0


class PatentPage(Page[PatentResponse]):
    mode: str
    candidate_count: int
    scope_application_count: int


class ReportVersionPage(Page[ReportVersionResponse]):
    current_version_id: UUID | None


class ReportVersionSummaryPage(Page[ReportVersionSummary]):
    current_version_id: UUID | None


class ChartVersionPage(Page[ChartResponse]):
    current_version_id: UUID


class HealthResponse(BaseModel):
    status: str
    database: bool | None = None
    redis: bool | None = None
    pgvector: bool | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
