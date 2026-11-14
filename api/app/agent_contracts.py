"""Explicit inputs for portable agents; no generated code or arbitrary task payloads."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.capability_contracts import Input
from patentcompass_core.contracts import ReportOperation


class Page(Input):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class ProjectRef(Input):
    project_id: UUID
    research_id: UUID | None = None


class ProjectCreate(Input):
    name: str = Field(min_length=1, max_length=300)
    scope: dict = Field(default_factory=dict)


class ScopeUpdate(ProjectRef):
    expected_scope_version_id: UUID
    changes: dict = Field(default_factory=dict)
    confirmed: bool


class DatasetBind(ProjectRef):
    dataset_version_id: UUID
    expected_revision: int = Field(ge=0)


class ResearchCreate(Input):
    project_id: UUID
    expected_adopted_version_id: UUID | None


class SemanticStart(ProjectRef):
    expected_revision: int = Field(ge=0)
    agent_model: str = Field(min_length=1, max_length=200)
    skill_version: str = Field(min_length=1, max_length=100)


class RunRef(Input):
    run_id: UUID


class BatchRead(RunRef):
    batch_index: int = Field(default=0, ge=0)


class Tag(Input):
    label: str = Field(min_length=1, max_length=80)
    field: Literal["title", "abstract"]
    quote: str = Field(min_length=5, max_length=300)
    confidence: float = Field(ge=0, le=1, strict=True)


class Extraction(Input):
    id: str = Field(min_length=1, max_length=30)
    elements: list[Tag] = Field(max_length=3)
    methods: list[Tag] = Field(max_length=3)
    scenarios: list[Tag] = Field(max_length=3)


class BatchSubmit(BatchRead):
    records: list[Extraction] = Field(min_length=1, max_length=20)


class TaxonomyRead(RunRef):
    dimension: Literal["elements", "methods", "scenarios"]


class Theme(Input):
    label: str = Field(min_length=1, max_length=80)
    members: list[str] = Field(min_length=1)


class TaxonomySubmit(TaxonomyRead):
    themes: list[Theme] = Field(max_length=16)


class ChartRead(Input):
    chart_id: UUID
    chart_version_id: UUID | None = None


class ProjectTask(ProjectRef):
    action: Literal["analyze", "generate_report", "revise_report", "restore_report", "export", "revise_chart"]
    expected_revision: int = Field(ge=0)
    analysis_id: UUID | None = None
    expected_current_version_id: UUID | None = None
    expected_adopted_version_id: UUID | None = None
    base_version_id: UUID | None = None
    report_version_id: UUID | None = None
    chart_id: UUID | None = None
    chart_mode: Literal["style", "data", "insight"] = "style"
    chart_option: dict = Field(default_factory=dict)
    chart_filters: dict = Field(default_factory=dict)
    interpretation: str | None = Field(default=None, min_length=1, max_length=40000)
    summary_text: str | None = Field(default=None, min_length=1, max_length=40000)
    operations: list[ReportOperation] = Field(default_factory=list, max_length=100)


class ResearchAdopt(Input):
    research_id: UUID
    expected_current_version_id: UUID | None
