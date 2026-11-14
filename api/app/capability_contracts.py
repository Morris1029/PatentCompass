"""Typed capability inputs used by both HTTP and MCP."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patentcompass_core.contracts import ChartSnapshot, PatentRecord, ReportDocument, ReportOperation


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PatentInput(Input):
    records: list[PatentRecord] | None = Field(default=None, min_length=1)
    dataset_version_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=50)
    scope: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def one_source(self):
        from patentcompass_core.retrieval import validate_filters
        self.scope = validate_filters(self.scope)
        if (self.records is None) == (self.dataset_version_ids is None):
            raise ValueError("records 与 dataset_version_ids 必须且只能提供一项")
        if self.records and len({row.id for row in self.records}) != len(self.records):
            raise ValueError("输入记录的 id 不可重复")
        return self


class SearchInput(PatentInput):
    q: str = Field(default="", max_length=500)
    queries: list[str] | None = Field(default=None, min_length=1, max_length=5)
    mode: Literal["field", "keywords", "semantic", "hybrid"] = "keywords"
    filters: dict = Field(default_factory=dict)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def exclusive_query(self):
        from patentcompass_core.retrieval import validate_filters
        self.filters = validate_filters(self.filters)
        if self.queries and self.q:
            raise ValueError("q 与 queries 互斥")
        if self.queries and self.mode not in {"keywords", "field"}:
            raise ValueError("queries 仅适用于关键词检索")
        return self


class PatentReadInput(Input):
    patent_id: UUID


class MaterialsInput(Input):
    project_id: UUID
    q: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=100)


class AnalysisInput(Input):
    analysis_id: UUID
    view: Literal["overview", "technology", "landscape", "opportunities", "roadmap", "geography"] = "overview"


class EvidenceInput(Input):
    evidence_id: UUID


class ReportVersionInput(Input):
    report_version_id: UUID


class FileInput(Input):
    file_id: UUID


class ReportBuildInput(PatentInput):
    title: str = Field(default="专利样本研究报告", min_length=1, max_length=300)
    report_mode: Literal["statistical", "ai"] = "statistical"
    analysis: dict | None = None


class ReportReviseInput(Input):
    content: ReportDocument
    operations: list[ReportOperation] = Field(default_factory=list, max_length=100)
    charts: list[ChartSnapshot] = Field(default_factory=list, max_length=100)
    block_id: str | None = None
    instruction: str | None = Field(default=None, min_length=1, max_length=10000)

    @model_validator(mode="after")
    def one_edit(self):
        if bool(self.operations) == bool(self.instruction):
            raise ValueError("operations 与 instruction 必须且只能提供一项")
        if self.instruction and not self.block_id:
            raise ValueError("自然语言修改需要明确 block_id")
        for operation in self.operations:
            if operation.op in {"insert_chart", "update_chart"}:
                matches = [chart for chart in self.charts if chart.id == str(operation.chart_id)
                           and (operation.chart_version_id is None or chart.version_id == str(operation.chart_version_id))]
                if len(matches) != 1:
                    raise ValueError("图表操作需要唯一且明确的冻结图表版本")
        return self


class ChartInput(Input):
    title: str = Field(default="专利统计", max_length=300)
    rows: list[dict] = Field(default_factory=list, max_length=10000)
    kind: str = Field(default="bar", max_length=50)


class RenderInput(Input):
    option: dict
    filename: str = Field(default="chart.png", max_length=150)


class ExportInput(Input):
    content: ReportDocument
    filename: str = Field(default="patent-report.docx", max_length=150)
    version_number: int = Field(default=1, ge=1)


class RunInput(Input):
    run_id: UUID


class EventsInput(RunInput):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=200)
