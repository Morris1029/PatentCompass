from datetime import date, datetime, timezone
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Record, Base):
    __tablename__ = "projects"
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(300))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    scope_version_id: Mapped[str | None] = mapped_column(ForeignKey("scope_versions.id", use_alter=True, name="fk_project_scope"))
    dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id", use_alter=True, name="fk_project_dataset"))
    current_analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_results.id", use_alter=True, name="fk_project_analysis"))


class ScopeVersion(Record, Base):
    __tablename__ = "scope_versions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("project_id", "number"),)


class StoredFile(Record, Base):
    __tablename__ = "files"
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("research_runs.id", use_alter=True, name="fk_file_run"), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(150))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)


class Dataset(Record, Base):
    __tablename__ = "datasets"
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(300))
    source_sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("owner_id", "source_sha256"),)


class DatasetVersion(Record, Base):
    __tablename__ = "dataset_versions"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    source_file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    rule_version: Mapped[str] = mapped_column(String(50), default="cnipa-v1")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    quality: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    base_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    merge_fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)


class PatentApplication(Record, Base):
    __tablename__ = "patent_applications"
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    application_number: Mapped[str] = mapped_column(String(100))
    application_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str] = mapped_column(Text)
    applicants: Mapped[list] = mapped_column(JSONB, default=list)
    inventors: Mapped[list] = mapped_column(JSONB, default=list)
    ipc_codes: Mapped[list] = mapped_column(JSONB, default=list)
    countries: Mapped[list] = mapped_column(JSONB, default=list)
    priorities: Mapped[list] = mapped_column(JSONB, default=list)
    raw_record: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_sheet: Mapped[str] = mapped_column(String(100))
    source_row: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("dataset_version_id", "application_number"),)


class PatentPublication(Record, Base):
    __tablename__ = "patent_publications"
    application_id: Mapped[str] = mapped_column(ForeignKey("patent_applications.id"), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    publication_number: Mapped[str] = mapped_column(String(100))
    publication_date: Mapped[date] = mapped_column(Date)
    kind_code: Mapped[str] = mapped_column(String(10))
    __table_args__ = (UniqueConstraint("dataset_version_id", "publication_number"),)


class PatentEmbedding(Record, Base):
    __tablename__ = "patent_embeddings"
    application_id: Mapped[str] = mapped_column(ForeignKey("patent_applications.id"), index=True)
    model: Mapped[str] = mapped_column(String(200))
    dimension: Mapped[int] = mapped_column(Integer)
    text_sha256: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(Vector())
    __table_args__ = (UniqueConstraint("application_id", "model", "text_sha256"),)


class Conversation(Record, Base):
    __tablename__ = "conversations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))


class Message(Record, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("research_runs.id", use_alter=True, name="fk_message_run"), index=True)
    role: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    reference: Mapped[dict | None] = mapped_column(JSONB)
    attachments: Mapped[list] = mapped_column(JSONB, default=list)
    cards: Mapped[list] = mapped_column(JSONB, default=list)


class ResearchRun(Record, Base):
    __tablename__ = "research_runs"
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"))
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    scope_version_id: Mapped[str | None] = mapped_column(ForeignKey("scope_versions.id"))
    dataset_version_ids: Mapped[list] = mapped_column(JSONB, default=list)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[dict | None] = mapped_column(JSONB)
    input_request: Mapped[dict | None] = mapped_column(JSONB)
    retry_of_id: Mapped[str | None] = mapped_column(ForeignKey("research_runs.id"))
    lease_token: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_pending: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RunStep(Record, Base):
    __tablename__ = "run_steps"
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "key"),)


class RunEvent(Record, Base):
    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("run_id", "seq"),)


class IdempotencyRequest(Record, Base):
    __tablename__ = "idempotency_requests"
    owner_id: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(300))
    client_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSONB)
    status_code: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("owner_id", "operation", "client_key"),)


class AnalysisResult(Record, Base):
    __tablename__ = "analysis_results"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), unique=True)
    scope_version_id: Mapped[str] = mapped_column(ForeignKey("scope_versions.id"))
    dataset_version_ids: Mapped[list] = mapped_column(JSONB)
    patent_ids: Mapped[list] = mapped_column(JSONB)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    views: Mapped[dict] = mapped_column(JSONB, default=dict)
    revision_of_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_results.id"), index=True)


class Chart(Record, Base):
    __tablename__ = "charts"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_results.id"), index=True)
    version_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), default=new_id, unique=True)
    kind: Mapped[str] = mapped_column(String(60))
    option: Mapped[dict] = mapped_column(JSONB)
    data: Mapped[dict] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    selections: Mapped[dict] = mapped_column(JSONB, default=dict)


class Evidence(Record, Base):
    __tablename__ = "evidence_items"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_results.id"), index=True)
    application_id: Mapped[str | None] = mapped_column(ForeignKey("patent_applications.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    content: Mapped[dict] = mapped_column(JSONB)


class Report(Record, Base):
    __tablename__ = "reports"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), unique=True)
    current_version_id: Mapped[str | None] = mapped_column(ForeignKey("report_versions.id", use_alter=True, name="fk_report_current_version"))
    version_counter: Mapped[int] = mapped_column(Integer, default=0)


class ReportVersion(Record, Base):
    __tablename__ = "report_versions"
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("report_versions.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), unique=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_results.id"))
    scope_version_id: Mapped[str] = mapped_column(ForeignKey("scope_versions.id"))
    dataset_version_ids: Mapped[list] = mapped_column(JSONB)
    content: Mapped[dict] = mapped_column(JSONB)
    chart_ids: Mapped[list] = mapped_column(JSONB, default=list)
    instruction: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"))
    __table_args__ = (UniqueConstraint("report_id", "number"),)


Index("ix_run_dispatch", ResearchRun.dispatch_pending, ResearchRun.status)


class Material(Record, Base):
    __tablename__ = "materials"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("research_runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[dict | None] = mapped_column(JSONB)


class MaterialBlock(Record, Base):
    __tablename__ = "material_blocks"
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    location: Mapped[dict] = mapped_column(JSONB)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("material_id", "ordinal"),)


class DatasetSource(Record, Base):
    __tablename__ = "dataset_sources"
    version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    base_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("version_id", "file_id"),)


class ImportConflict(Record, Base):
    __tablename__ = "import_conflicts"
    version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    application_number: Mapped[str] = mapped_column(String(100))
    field: Mapped[str] = mapped_column(String(100))
    old_value: Mapped[object] = mapped_column(JSONB)
    new_value: Mapped[object] = mapped_column(JSONB)
    resolution: Mapped[str | None] = mapped_column(String(20))
    chosen_value: Mapped[object | None] = mapped_column(JSONB)
    source: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("version_id", "application_number", "field"),)


class SemanticCache(Record, Base):
    __tablename__ = "semantic_caches"
    cache_key: Mapped[str] = mapped_column(String(64), unique=True)
    text_sha256: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[str] = mapped_column(String(80))
    extraction: Mapped[dict] = mapped_column(JSONB)


class SemanticMembership(Record, Base):
    __tablename__ = "semantic_memberships"
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_results.id"), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("patent_applications.id"), index=True)
    taxonomy_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    assignment: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("analysis_id", "application_id"),)


class ChartVersion(Record, Base):
    __tablename__ = "chart_versions"
    chart_id: Mapped[str] = mapped_column(ForeignKey("charts.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    option: Mapped[dict] = mapped_column(JSONB)
    data: Mapped[dict] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    selections: Mapped[dict] = mapped_column(JSONB, default=dict)
    data_sha256: Mapped[str] = mapped_column(String(64))
    interpretation: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("chart_id", "number"),)
