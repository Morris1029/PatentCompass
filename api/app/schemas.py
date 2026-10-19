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
    expected_scope_version_id: UUID
    changes: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class DatasetBinding(RequestModel):
    expected_revision: int = Field(ge=0)
    dataset_version_ids: list[UUID] = Field(min_length=1, max_length=1)


class MessageReference(RequestModel):
    report_version_id: UUID
    block_id: str | None = None
    text: str = Field(default="", max_length=20000)


class MessageCreate(RequestModel):
    content: str = Field(min_length=1, max_length=40000)
    reference: MessageReference | None = None
    action: Literal["chat", "analyze", "generate_report"] = "chat"


class ConversationCreate(RequestModel):
    initial_message: MessageCreate


class RunCreate(RequestModel):
    action: Literal["analyze", "generate_report", "index"]
    analysis_id: UUID | None = None
    report_mode: Literal["statistical", "ai"] = "statistical"


class RunInput(RequestModel):
    input_request_id: UUID
    expected_scope_version_id: UUID
    values: dict[str, Any]


class ReportRevision(RequestModel):
    base_version_id: UUID
    expected_current_version_id: UUID
    block_id: str = Field(min_length=1)
    replacement_text: str | None = Field(default=None, max_length=40000)
    instruction: str | None = Field(default=None, min_length=1, max_length=10000)

    @model_validator(mode="after")
    def one_edit(self):
        if (self.replacement_text is None) == (self.instruction is None):
            raise ValueError("replacement_text 与 instruction 必须且只能提供一项")
        return self


class CurrentVersionUpdate(RequestModel):
    version_id: UUID
    expected_current_version_id: UUID


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


class RunResponse(Resource):
    project_id: UUID | None
    kind: str
    status: str
    result: dict[str, Any]
    error: dict[str, Any] | None
    input_request: dict[str, Any] | None
    dataset_version_ids: list[str]
    scope_version_id: UUID | None


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


class HealthResponse(BaseModel):
    status: str
    database: bool | None = None
    redis: bool | None = None
    pgvector: bool | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
