import asyncio
import base64
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app import db, schemas
from app.config import get_settings
from app.errors import AppError
from app.files import resolve_file, store_bytes
from app.jobs import TERMINAL, create_run, dispatch_pending, emit_event
from app.models import (AnalysisResult, Chart, Conversation, Dataset, DatasetVersion, Evidence,
                        IdempotencyRequest, Message, PatentApplication, PatentPublication, Project,
                        Report, ReportVersion, ResearchRun, RunEvent, RunStep, ScopeVersion, StoredFile,
                        ChartVersion, DatasetSource, ImportConflict, Material, MaterialBlock, new_id, utcnow)

logger = logging.getLogger(__name__)


def session_dependency():
    with db.session_scope() as session:
        yield session


DB = Annotated[Session, Depends(session_dependency, scope="function")]


def record(row, exclude=()):
    return jsonable_encoder({column.key: getattr(row, column.key) for column in row.__table__.columns
                             if column.key not in exclude})


def owned(session, model, object_id, lock=False):
    try:
        object_id = str(UUID(str(object_id)))
    except (ValueError, TypeError, AttributeError):
        raise AppError(422, "INVALID_RESOURCE_ID", "资源标识无效")
    query = select(model).where(model.id == str(object_id))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.scalar(query)
    if not row:
        raise AppError(404, "NOT_FOUND", "记录不存在")
    if hasattr(row, "owner_id"):
        permitted = row.owner_id == get_settings().dev_owner_id
    elif isinstance(row, DatasetVersion):
        permitted = session.get(Dataset, row.dataset_id).owner_id == get_settings().dev_owner_id
    elif isinstance(row, ReportVersion):
        permitted = session.get(Project, session.get(Report, row.report_id).project_id).owner_id == get_settings().dev_owner_id
    elif isinstance(row, Message):
        permitted = session.get(Project, session.get(Conversation, row.conversation_id).project_id).owner_id == get_settings().dev_owner_id
    elif hasattr(row, "project_id"):
        permitted = session.get(Project, row.project_id).owner_id == get_settings().dev_owner_id
    else:
        permitted = False
    if not permitted:
        raise AppError(404, "NOT_FOUND", "记录不存在")
    return row


def paginate(rows, limit, cursor=None, reverse=False):
    ordered = sorted(rows, key=lambda row: (row.created_at, row.id), reverse=reverse)
    if cursor:
        try:
            cursor_data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            point = (cursor_data["created_at"], cursor_data["id"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise AppError(422, "INVALID_CURSOR", "分页游标无效")
        ordered = [row for row in ordered if ((row.created_at.isoformat(), row.id) < point if reverse
                                              else (row.created_at.isoformat(), row.id) > point)]
    selected = ordered[:limit]
    next_cursor = None
    if len(ordered) > limit:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"created_at": last.created_at.isoformat(),
                                                         "id": last.id}).encode()).decode()
    return {"items": [record(row) for row in selected], "next_cursor": next_cursor,
            "total": len(rows), "returned_count": len(selected)}


def ranked_page(found, limit, cursor):
    rows = sorted(found["items"], key=lambda row: (-found["scores"][row.id], row.id))
    if cursor:
        try:
            point = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            rank = (-float(point["score"]), str(UUID(point["id"])))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise AppError(422, "INVALID_CURSOR", "语义检索分页游标无效")
        rows = [row for row in rows if (-found["scores"][row.id], row.id) > rank]
    selected = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"score": found["scores"][last.id], "id": last.id}).encode()).decode()
    return {"items": [record(row) for row in selected], "next_cursor": next_cursor,
            "total": None, "returned_count": len(selected)}


def mutate(session, request, response, payload, callback, status_code=200):
    key = request.headers.get("Idempotency-Key") or request.state.request_id
    if len(key) > 200:
        raise AppError(422, "INVALID_IDEMPOTENCY_KEY", "幂等标识不能超过 200 字符")
    operation = request.method + " " + request.url.path
    owner = get_settings().dev_owner_id
    digest = hashlib.sha256(json.dumps(jsonable_encoder(payload), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": owner + operation + key})
    previous = session.scalar(select(IdempotencyRequest).where(IdempotencyRequest.owner_id == owner,
                              IdempotencyRequest.operation == operation, IdempotencyRequest.client_key == key))
    if previous:
        if previous.request_hash != digest:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "同一幂等标识不能提交不同内容")
        response.status_code = previous.status_code
        return previous.response
    value = jsonable_encoder(callback())
    session.add(IdempotencyRequest(owner_id=owner, operation=operation, client_key=key,
                                  request_hash=digest, response=value, status_code=status_code))
    response.status_code = status_code
    return value


def task_result(run, **extra):
    return {"run_id": run.id, "status": run.status, **extra}


def validate_scope(content):
    allowed = {"industry", "goal", "need", "focus", "start", "end", "type", "patent_type", "q", "applicant", "ipc",
               "application_start", "application_end", "source_region", "coverage"}
    if set(content) - allowed:
        raise AppError(422, "INVALID_SCOPE_FIELD", "研究范围含不支持的字段", sorted(set(content) - allowed))
    if content.get("type") or content.get("patent_type"):
        raise AppError(422, "PATENT_TYPE_UNAVAILABLE", "当前数据未提供可核验的专利类型字段，暂时不能按发明或实用新型筛选")
    for key in ("q", "applicant", "ipc"):
        value = content.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 1000):
            raise AppError(422, "INVALID_SCOPE_FILTER", "检索条件必须是长度不超过 1000 的文本", {"field": key})
    for key in ("start", "end"):
        value = content.get(key)
        if value not in (None, ""):
            try:
                content[key] = int(value)
            except (TypeError, ValueError):
                raise AppError(422, "INVALID_YEAR", "年份必须为整数")
            if not 1800 <= content[key] <= 2200:
                raise AppError(422, "INVALID_YEAR", "年份超出支持范围")
    if content.get("start") and content.get("end") and content["start"] > content["end"]:
        raise AppError(422, "INVALID_DATE_RANGE", "开始年份不能晚于结束年份")
    for key in ("application_start", "application_end"):
        if content.get(key):
            try:
                content[key] = date.fromisoformat(str(content[key])).isoformat()
            except ValueError:
                raise AppError(422, "INVALID_DATE", "日期需要使用 YYYY-MM-DD")
    if content.get("application_start") and content.get("application_end") and content["application_start"] > content["application_end"]:
        raise AppError(422, "INVALID_DATE_RANGE", "申请开始日期不能晚于结束日期")
    return content


def update_scope(session, project, expected_id, changes, confirmed):
    if project.scope_version_id != str(expected_id):
        raise AppError(409, "SCOPE_VERSION_CONFLICT", "范围已被更新，请刷新后重试")
    old = session.get(ScopeVersion, project.scope_version_id)
    content = validate_scope({**old.content, **changes})
    scope = ScopeVersion(project_id=project.id, number=old.number + 1, content=content, confirmed=confirmed)
    session.add(scope)
    session.flush()
    project.scope_version_id = scope.id
    project.revision += 1
    return scope


def check_reference(session, project, reference):
    if not reference:
        return
    version = owned(session, ReportVersion, reference["report_version_id"])
    if session.get(Report, version.report_id).project_id != project.id:
        raise AppError(404, "REFERENCE_NOT_FOUND", "引用不属于当前项目")
    if reference.get("block_id"):
        block = next((item for item in version.content.get("blocks", []) if item.get("id") == reference["block_id"]), None)
        if not block:
            raise AppError(404, "BLOCK_NOT_FOUND", "引用段落不存在")
        if reference.get("text") and reference["text"] not in str(block.get("text", "")):
            raise AppError(409, "REFERENCE_TEXT_MISMATCH", "引用原文与报告版本不匹配")
    if reference.get("block_ids"):
        selected = [item for item in version.content.get("blocks", []) if item.get("id") in reference["block_ids"]]
        if len(selected) != len(set(reference["block_ids"])):
            raise AppError(404, "BLOCK_NOT_FOUND", "引用正文块不存在")
        if reference.get("text") and " ".join(reference["text"].split()) not in " ".join(
                " ".join(str(item.get("text", "")) for item in selected).split()):
            raise AppError(409, "REFERENCE_TEXT_MISMATCH", "引用文字与报告快照不匹配")


def make_message(session, project, conversation, message):
    value = message.model_dump(mode="json")
    check_reference(session, project, value.get("reference"))
    for material_id in message.attachments:
        material = owned(session, Material, material_id)
        if material.project_id != project.id:
            raise AppError(404, "MATERIAL_NOT_FOUND", "资料不属于此项目")
        if material.status != "ready":
            raise AppError(409, "MATERIAL_NOT_READY", "请等待附件解析完成后发送", {"material_id": material.id})
    if message.action in {"analyze", "generate_report"} and not project.dataset_version_id:
        raise AppError(409, "DATASET_REQUIRED", "请先绑定就绪的数据版本")
    row = Message(conversation_id=conversation.id, role="user", content=message.content,
                  reference=value.get("reference"), attachments=value["attachments"])
    session.add(row)
    session.flush()
    run = create_run(session, message.action, project, {"message_id": row.id, "report_mode": message.report_mode,
                     "semantic_mode": message.semantic_mode, "attachments": value["attachments"]}, conversation.id)
    if message.action in {"chat", "auto"} and project.current_analysis_id:
        analysis = session.get(AnalysisResult, project.current_analysis_id)
        if analysis.scope_version_id == run.scope_version_id and analysis.dataset_version_ids == run.dataset_version_ids:
            run.payload = {**run.payload, "analysis_id": analysis.id}
    if message.reference:
        version = session.get(ReportVersion, str(message.reference.report_version_id))
        run.scope_version_id, run.dataset_version_ids = version.scope_version_id, list(version.dataset_version_ids)
        run.payload = {**run.payload, "reference_version_id": version.id, "analysis_id": version.analysis_id}
    row.run_id = run.id
    return task_result(run, conversation_id=conversation.id, message_id=row.id)


async def _dispatcher(stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(dispatch_pending)
        except Exception:
            logger.exception("Task dispatcher temporarily unavailable")
        try:
            await asyncio.wait_for(stop.wait(), timeout=get_settings().dispatch_interval)
        except TimeoutError:
            pass


def create_app(start_dispatcher=True):
    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        task = asyncio.create_task(_dispatcher(stop)) if start_dispatcher else None
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="PatentCompass API", version="0.1.0", lifespan=lifespan,
                  responses={400: {"model": schemas.ErrorResponse}, 404: {"model": schemas.ErrorResponse},
                             409: {"model": schemas.ErrorResponse}, 422: {"model": schemas.ErrorResponse},
                             503: {"model": schemas.ErrorResponse}})

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID", "")[:100] or new_id()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message,
                            "details": jsonable_encoder(exc.details), "request_id": request.state.request_id})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": "请求参数无效",
                            "details": jsonable_encoder(exc.errors(), custom_encoder={ValueError: str}),
                            "request_id": request.state.request_id})

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"code": "HTTP_ERROR", "message": str(exc.detail),
                            "details": None, "request_id": request.state.request_id})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error("Request failed: %s", request.state.request_id, exc_info=exc)
        return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "message": "服务暂时无法完成请求",
                            "details": None, "request_id": request.state.request_id},
                            headers={"X-Request-ID": request.state.request_id})

    @app.get("/health/live", response_model=schemas.HealthResponse, tags=["health"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", response_model=schemas.HealthResponse, tags=["health"])
    def ready(response: Response):
        from redis import Redis
        database = vector = redis = False
        try:
            with db.engine.connect() as connection:
                database = connection.scalar(text("SELECT 1")) == 1
                vector = bool(connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname='vector'")))
            redis = bool(Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1).ping())
        except Exception:
            logger.warning("Readiness dependencies unavailable")
        if not (database and vector and redis):
            response.status_code = 503
        return {"status": "ready" if database and vector and redis else "unavailable", "database": database,
                "pgvector": vector, "redis": redis,
                "capabilities": {"chat": get_settings().llm_enabled, "embeddings": get_settings().embedding_enabled,
                                 "statistical_reports": True, "pdf_export": False}}

    @app.post("/api/projects", response_model=schemas.ProjectResponse, status_code=201, tags=["projects"])
    def create_project(body: schemas.ProjectCreate, request: Request, response: Response, session: DB):
        def action():
            scope_content = validate_scope(dict(body.scope))
            project = Project(owner_id=get_settings().dev_owner_id, name=body.name)
            session.add(project)
            session.flush()
            scope = ScopeVersion(project_id=project.id, number=1, confirmed=False, content=scope_content)
            session.add(scope)
            session.flush()
            project.scope_version_id = scope.id
            return record(project)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 201)

    @app.get("/api/projects", response_model=schemas.Page[schemas.ProjectResponse], tags=["projects"])
    def projects(session: DB, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        rows = session.scalars(select(Project).where(Project.owner_id == get_settings().dev_owner_id)).all()
        return paginate(rows, limit, cursor, reverse=True)

    @app.get("/api/projects/{id}", response_model=schemas.ProjectResponse, tags=["projects"])
    def project_detail(id: UUID, session: DB):
        project = owned(session, Project, id)
        report = session.scalar(select(Report).where(Report.project_id == project.id))
        scope = session.get(ScopeVersion, project.scope_version_id)
        active = session.scalars(select(ResearchRun).where(ResearchRun.project_id == project.id,
                                ResearchRun.status.not_in(TERMINAL)).order_by(ResearchRun.created_at)).all()
        last = session.scalar(select(Conversation).where(Conversation.project_id == project.id)
                              .order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(1))
        return {**record(project), "scope": record(scope),
                "dataset_version_ids": [project.dataset_version_id] if project.dataset_version_id else [],
                "report_id": report.id if report else None,
                "current_report_version_id": report.current_version_id if report else None,
                "active_runs": [{"id": item.id, "kind": item.kind, "status": item.status} for item in active],
                "last_conversation_id": last.id if last else None}

    @app.patch("/api/projects/{id}/scope", response_model=schemas.ScopeResponse, tags=["projects"])
    def scope_update(id: UUID, body: schemas.ScopeUpdate, request: Request, response: Response, session: DB):
        def action():
            project = owned(session, Project, id, lock=True)
            scope = update_scope(session, project, body.expected_scope_version_id, body.changes, body.confirmed)
            return {**record(scope), "project_revision": project.revision,
                    "analysis_status": "stale" if project.current_analysis_id else "not_generated"}
        return mutate(session, request, response, body.model_dump(mode="json"), action)

    @app.get("/api/datasets", response_model=schemas.Page[schemas.DatasetResponse], tags=["datasets"])
    def datasets(session: DB, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        rows = session.scalars(select(Dataset).where(Dataset.owner_id == get_settings().dev_owner_id)).all()
        result = paginate(rows, limit, cursor, reverse=True)
        for item in result["items"]:
            item["versions"] = [record(version) for version in session.scalars(select(DatasetVersion)
                                .where(DatasetVersion.dataset_id == item["id"]).order_by(DatasetVersion.created_at))]
        return result

    @app.post("/api/datasets/imports", response_model=schemas.TaskResponse, status_code=202, tags=["datasets"])
    def import_dataset(request: Request, response: Response, session: DB,
                       file: UploadFile | None = File(None), source_file_id: UUID | None = Form(None),
                       name: str = Form("无人机专利样本", min_length=1, max_length=300), source_metadata: str = Form("{}"),
                       base_dataset_version_id: UUID | None = Form(None)):
        if (file is None) == (source_file_id is None):
            raise AppError(422, "IMPORT_SOURCE_REQUIRED", "上传文件和 source_file_id 必须且只能提供一项")
        try:
            metadata = json.loads(source_metadata)
            if not isinstance(metadata, dict):
                raise ValueError()
        except ValueError:
            raise AppError(422, "INVALID_SOURCE_METADATA", "来源说明需要 JSON 对象")
        content = file.file.read(20 * 1024 * 1024 + 1) if file else None
        if file and (not file.filename or not file.filename.lower().endswith(".xlsx") or len(content) > 20 * 1024 * 1024):
            raise AppError(422, "INVALID_IMPORT_FILE", "请上传不超过 20MB 的 .xlsx 文件")
        payload = {"source_file_id": str(source_file_id) if source_file_id else None,
                   "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
                   "name": name, "source_metadata": metadata,
                   "base_dataset_version_id": str(base_dataset_version_id) if base_dataset_version_id else None}
        def action():
            source = owned(session, StoredFile, source_file_id) if source_file_id else None
            if source and source.kind != "raw":
                raise AppError(422, "INVALID_SOURCE_FILE", "只能从原始导入文件创建数据集")
            file_hash = source.sha256 if source else payload["sha256"]
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "import:" + file_hash})
            if base_dataset_version_id:
                base = owned(session, DatasetVersion, base_dataset_version_id)
                if base.status != "ready":
                    raise AppError(409, "BASE_DATASET_NOT_READY", "合并基础版本尚未就绪")
                fingerprint = hashlib.sha256(f"{base.id}:{file_hash}:cnipa-v1:merge-v1".encode()).hexdigest()
                version = session.scalar(select(DatasetVersion).where(DatasetVersion.merge_fingerprint == fingerprint))
                if version:
                    run = session.scalar(select(ResearchRun).where(ResearchRun.kind == "merge_import",
                        ResearchRun.payload["dataset_version_id"].astext == version.id).order_by(ResearchRun.created_at.desc()).limit(1))
                    if run.status == "cancelled":
                        run = create_run(session, "merge_import", payload=dict(run.payload), retry_of_id=run.id)
                    return task_result(run, dataset_id=base.dataset_id, dataset_version_id=version.id,
                                       source_file_id=version.source_file_id, reused=True)
                source = source or store_bytes(session, content, file.filename, "raw")
                version = DatasetVersion(dataset_id=base.dataset_id, source_file_id=source.id, status="pending",
                    base_version_id=base.id, merge_fingerprint=fingerprint,
                    source_metadata={**base.source_metadata, **metadata})
                session.add(version)
                session.flush()
                run = create_run(session, "merge_import", payload={"dataset_version_id": version.id,
                    "source_file_id": source.id, "base_dataset_version_id": base.id, "merge_fingerprint": fingerprint})
                return task_result(run, dataset_id=base.dataset_id, dataset_version_id=version.id,
                                   source_file_id=source.id, reused=False)
            dataset = session.scalar(select(Dataset).where(Dataset.owner_id == get_settings().dev_owner_id,
                                                          Dataset.source_sha256 == file_hash))
            if dataset:
                version = session.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id,
                     DatasetVersion.rule_version == "cnipa-v1", DatasetVersion.base_version_id.is_(None)))
                run = session.scalar(select(ResearchRun).where(ResearchRun.kind == "import",
                                     ResearchRun.payload["dataset_version_id"].astext == version.id)
                                     .order_by(ResearchRun.created_at.desc()).limit(1))
                if run.status == "cancelled":
                    run = create_run(session, "import", payload={"dataset_version_id": version.id}, retry_of_id=run.id)
                    if version.status != "ready":
                        version.status = "pending"
                        version.quality = {key: value for key, value in version.quality.items() if key != "error"}
                return task_result(run, dataset_id=dataset.id, dataset_version_id=version.id,
                                   source_file_id=version.source_file_id, reused=True)
            source = source or store_bytes(session, content, file.filename, "raw")
            dataset = Dataset(owner_id=get_settings().dev_owner_id, name=name, source_sha256=source.sha256)
            session.add(dataset)
            session.flush()
            version = DatasetVersion(dataset_id=dataset.id, source_file_id=source.id, rule_version="cnipa-v1",
                                     status="pending", source_metadata=metadata)
            session.add(version)
            session.flush()
            session.add(DatasetSource(version_id=version.id, file_id=source.id, details={"kind": "original"}))
            run = create_run(session, "import", payload={"dataset_version_id": version.id})
            return task_result(run, dataset_id=dataset.id, dataset_version_id=version.id, source_file_id=source.id, reused=False)
        return mutate(session, request, response, payload, action, 202)

    @app.get("/api/datasets/{id}/quality", response_model=schemas.QualityResponse, tags=["datasets"])
    def quality(id: UUID, session: DB, dataset_version_id: UUID):
        dataset = owned(session, Dataset, id)
        version = owned(session, DatasetVersion, dataset_version_id)
        if version.dataset_id != dataset.id:
            raise AppError(404, "VERSION_NOT_FOUND", "数据版本不属于此数据集")
        return {"dataset_id": dataset.id, "dataset_version_id": version.id, "status": version.status,
                "quality": version.quality, "source_metadata": version.source_metadata, "source_file_id": version.source_file_id,
                "base_version_id": version.base_version_id,
                "conflicts": [record(row) for row in session.scalars(select(ImportConflict).where(ImportConflict.version_id == version.id))],
                "sources": [record(row) for row in session.scalars(select(DatasetSource).where(DatasetSource.version_id == version.id))]}

    @app.put("/api/projects/{id}/datasets", response_model=schemas.ProjectResponse, tags=["datasets"])
    def bind_dataset(id: UUID, body: schemas.DatasetBinding, request: Request, response: Response, session: DB):
        def action():
            project = owned(session, Project, id, lock=True)
            if project.revision != body.expected_revision:
                raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已发生变化，请刷新后重试")
            version = owned(session, DatasetVersion, body.dataset_version_ids[0])
            if version.status != "ready":
                raise AppError(409, "DATASET_NOT_READY", "数据版本尚未通过质量检查")
            project.dataset_version_id = version.id
            project.revision += 1
            return record(project)
        return mutate(session, request, response, body.model_dump(mode="json"), action)

    @app.get("/api/projects/{id}/patents", response_model=schemas.PatentPage, tags=["patents"])
    def patents(id: UUID, session: DB, q: str = "", applicant: str | None = None, ipc: str | None = None,
                application_number: str | None = None, publication_number: str | None = None,
                start: int | None = None, end: int | None = None, application_start: date | None = None,
                application_end: date | None = None, mode: str = Query("field", pattern="^(field|keywords|semantic|hybrid)$"),
                limit: int = Query(50, ge=1, le=200), cursor: str | None = None, analysis_id: UUID | None = None,
                chart_id: UUID | None = None, chart_version_id: UUID | None = None, dimension_value: str | None = None):
        from app.data_service import search_patents
        project = owned(session, Project, id)
        validate_scope({"start": start, "end": end,
                        "application_start": application_start, "application_end": application_end})
        filters = {"q": q, "applicant": applicant, "ipc": ipc, "application_number": application_number,
                   "publication_number": publication_number, "start": start, "end": end,
                   "application_start": application_start.isoformat() if application_start else None,
                   "application_end": application_end.isoformat() if application_end else None,
                   "mode": mode, "limit": limit}
        analysis = None
        if analysis_id:
            analysis = owned(session, AnalysisResult, analysis_id)
            if analysis.project_id != project.id:
                raise AppError(404, "ANALYSIS_NOT_FOUND", "分析不属于此项目")
        if chart_id:
            from app.chart_service import snapshot
            chart = owned(session, Chart, chart_id)
            if chart.project_id != project.id:
                raise AppError(404, "CHART_NOT_FOUND", "图表或版本不属于此项目")
            selected_chart = snapshot(session, chart, str(chart_version_id) if chart_version_id else None)
            chart_analysis = session.get(AnalysisResult, chart.analysis_id)
            if analysis and analysis.id != chart_analysis.id:
                raise AppError(409, "SNAPSHOT_CONFLICT", "图表与指定分析版本不一致")
            analysis = chart_analysis
            if dimension_value is not None:
                if dimension_value not in selected_chart["selections"].get("values", {}):
                    raise AppError(422, "UNKNOWN_DIMENSION_VALUE", "图表中不存在此维度值")
                filters["application_ids"] = selected_chart["selections"]["values"][dimension_value]
        if analysis:
            filters.update(scope_version_id=analysis.scope_version_id, dataset_version_ids=analysis.dataset_version_ids)
            if "application_ids" not in filters:
                filters["application_ids"] = analysis.patent_ids
        found = search_patents(session, project, filters)
        page = (ranked_page(found, limit, cursor) if found["mode"] in {"semantic", "hybrid"}
                else paginate(found["items"], limit, cursor))
        page.update(mode=found["mode"], candidate_count=found["candidate_count"],
                    scope_application_count=found["scope_application_count"])
        for item in page["items"]:
            item["score"] = found.get("scores", {}).get(item["id"])
        return page

    @app.get("/api/projects/{id}/patents/{patent_id}", response_model=schemas.PatentResponse, tags=["patents"])
    def patent_detail(id: UUID, patent_id: UUID, session: DB, analysis_id: UUID | None = None):
        project = owned(session, Project, id)
        application = session.get(PatentApplication, str(patent_id))
        allowed_versions = [project.dataset_version_id]
        if analysis_id:
            analysis = owned(session, AnalysisResult, analysis_id)
            if analysis.project_id != project.id or str(patent_id) not in analysis.patent_ids:
                raise AppError(404, "PATENT_NOT_FOUND", "记录不在指定分析中")
            allowed_versions = analysis.dataset_version_ids
        if not application or application.dataset_version_id not in allowed_versions:
            raise AppError(404, "PATENT_NOT_FOUND", "专利不在此项目的数据范围内")
        publications = session.scalars(select(PatentPublication).where(PatentPublication.application_id == application.id)).all()
        return {**record(application), "publications": [record(item) for item in publications]}

    @app.post("/api/projects/{id}/conversations", response_model=schemas.TaskResponse, status_code=202, tags=["conversations"])
    def new_conversation(id: UUID, body: schemas.ConversationCreate, request: Request, response: Response, session: DB):
        def action():
            project = owned(session, Project, id)
            conversation = Conversation(project_id=project.id, title=body.initial_message.content[:80] or "资料研究")
            session.add(conversation)
            session.flush()
            return make_message(session, project, conversation, body.initial_message)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.get("/api/projects/{id}/conversations", response_model=schemas.Page[schemas.ConversationResponse], tags=["conversations"])
    def conversations(id: UUID, session: DB, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        project = owned(session, Project, id)
        return paginate(session.scalars(select(Conversation).where(Conversation.project_id == project.id)).all(), limit, cursor, True)

    @app.get("/api/conversations/{id}/messages", response_model=schemas.Page[schemas.MessageResponse], tags=["conversations"])
    def messages(id: UUID, session: DB, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        conversation = owned(session, Conversation, id)
        return paginate(session.scalars(select(Message).where(Message.conversation_id == conversation.id)).all(), limit, cursor)

    @app.post("/api/conversations/{id}/messages", response_model=schemas.TaskResponse, status_code=202, tags=["conversations"])
    def send_message(id: UUID, body: schemas.MessageCreate, request: Request, response: Response, session: DB):
        def action():
            conversation = owned(session, Conversation, id)
            return make_message(session, owned(session, Project, conversation.project_id), conversation, body)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.post("/api/projects/{id}/runs", response_model=schemas.TaskResponse, status_code=202, tags=["runs"])
    def start_run(id: UUID, body: schemas.RunCreate, request: Request, response: Response, session: DB):
        def action():
            project = owned(session, Project, id)
            if not project.dataset_version_id:
                raise AppError(409, "DATASET_REQUIRED", "请先绑定就绪数据")
            payload = body.model_dump(mode="json", exclude={"action"}, exclude_none=True)
            if body.analysis_id:
                analysis = owned(session, AnalysisResult, body.analysis_id)
                if analysis.project_id != project.id:
                    raise AppError(404, "ANALYSIS_NOT_FOUND", "分析不属于此项目")
            return task_result(create_run(session, body.action, project, payload))
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.get("/api/runs/{id}", response_model=schemas.RunResponse, tags=["runs"])
    def run_status(id: UUID, session: DB):
        from app.conversation_execution import activities_for_run
        from app.chat_streaming import stream_for_run
        run = owned(session, ResearchRun, id)
        steps = session.scalars(select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.created_at)).all()
        input_request = dict(run.input_request) if run.input_request else None
        if input_request and input_request["kind"] == "import_conflicts":
            input_request["conflicts"] = [record(row) for row in session.scalars(select(ImportConflict).where(
                ImportConflict.version_id == run.payload["dataset_version_id"], ImportConflict.resolution.is_(None)))]
        if input_request and input_request["kind"] in {"scope_confirmation", "plan_confirmation", "execution_confirmation"}:
            from app.conversation_execution import scope_editable_for_run
            scope = session.get(ScopeVersion, run.scope_version_id)
            input_request["scope"] = {**(scope.content if scope else {}), **input_request.get("scope", {}),
                                      **input_request.get("changes", {})}
            input_request["scope_editable"] = (scope_editable_for_run(run)
                                               if input_request["kind"] == "execution_confirmation" else True)
        return {**record(run, exclude=("lease_token", "payload")), "steps": [record(item) for item in steps],
                "input_request": input_request, "plan": run.payload.get("plan"),
                "origin_message_id": run.payload.get("message_id") or session.scalar(select(Message.id).where(
                    Message.run_id == run.id, Message.role == "user")), "activities": activities_for_run(session, run),
                "stream": stream_for_run(session, run)}

    @app.get("/api/runs/{id}/events", tags=["runs"], response_class=StreamingResponse,
             responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
    def run_events(id: UUID, session: DB, after: int = Query(0, ge=0), last_event_id: str | None = Header(None)):
        run = owned(session, ResearchRun, id)
        if last_event_id:
            try:
                value = int(last_event_id)
                if value < 0:
                    raise ValueError()
                after = max(after, value)
            except ValueError:
                raise AppError(422, "INVALID_EVENT_CURSOR", "Last-Event-ID 必须为非负序号")
        run_id = run.id
        session.commit()
        async def stream():
            nonlocal after
            def batch():
                with db.session_scope() as inner:
                    events = inner.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after)
                                           .order_by(RunEvent.seq).limit(200)).all()
                    state = inner.get(ResearchRun, run_id).status
                    return [record(event) for event in events], state
            while True:
                events, state = await asyncio.to_thread(batch)
                for event in events:
                    after = event["seq"]
                    yield f"id: {after}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if state in TERMINAL | {"waiting_input"} and len(events) < 200:
                    break
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{id}/inputs", response_model=schemas.TaskResponse, status_code=202, tags=["runs"])
    def submit_input(id: UUID, body: schemas.RunInput, request: Request, response: Response, session: DB):
        def action():
            initial = owned(session, ResearchRun, id)
            project = owned(session, Project, initial.project_id, lock=True) if initial.project_id else None
            run = owned(session, ResearchRun, id, lock=True)
            if run.status != "waiting_input" or not run.input_request or run.input_request["id"] != str(body.input_request_id):
                raise AppError(409, "INPUT_REQUEST_CONFLICT", "此确认请求已经失效")
            kind = run.input_request["kind"]
            submitted_scope = body.values.get("scope")
            if "scope" in body.values and not isinstance(submitted_scope, dict):
                raise AppError(422, "INVALID_SCOPE", "研究范围需要使用表格字段值")
            if kind == "execution_confirmation":
                from app.conversation_execution import execution_context, execution_request, scope_editable_for_run
                if body.values.get("confirmed") is not True:
                    raise AppError(422, "CONFIRMATION_REQUIRED", "请明确确认本轮操作")
                if run.input_request.get("context") != execution_context(session, run):
                    run.input_request = execution_request(session, run, run.input_request["actions"], changed=True)
                    emit_event(session, run.id, "input.required", run.input_request)
                    return task_result(run)
                if submitted_scope is not None:
                    existing_scope = session.get(ScopeVersion, run.scope_version_id)
                    confirmed_scope = validate_scope({**existing_scope.content, **submitted_scope})
                    if confirmed_scope != existing_scope.content:
                        if not scope_editable_for_run(run):
                            raise AppError(409, "SCOPE_SNAPSHOT_FIXED", "此操作采用报告的固定范围，请在生成新报告时调整范围")
                        scope = update_scope(session, project, body.expected_scope_version_id, confirmed_scope, True)
                        run.scope_version_id = scope.id
                        run.payload = {key: value for key, value in run.payload.items()
                                       if key not in {"analysis_id", "execution_authorization"}}
                run.payload = {**run.payload, "execution_authorization": {
                    "context": execution_context(session, run), "allowed_actions": run.input_request["allowed_actions"]}}
            elif kind == "import_conflicts":
                from app.merge_service import resolve_conflicts
                version = owned(session, DatasetVersion, run.payload["dataset_version_id"])
                resolve_conflicts(session, version.id, body.values.get("choices", []))
            elif kind == "dataset_selection":
                if project.revision != run.input_request.get("expected_project_revision", project.revision):
                    if body.values.get("confirm_project_change") is not True or body.values.get("expected_revision") != project.revision:
                        raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目数据或范围已变化，请核对最新状态后重新确认采用数据",
                                       {"revision": project.revision, "dataset_version_id": project.dataset_version_id})
                version = owned(session, DatasetVersion, body.values.get("dataset_version_id"))
                if version.status != "ready":
                    raise AppError(409, "DATASET_NOT_READY", "所选数据版本尚未就绪")
                project.dataset_version_id = version.id
                project.revision += 1
                run.dataset_version_ids = [version.id]
            elif kind in {"scope_confirmation", "plan_confirmation"}:
                if body.values.get("confirmed") is not True:
                    raise AppError(422, "CONFIRMATION_REQUIRED", "需要明确确认研究范围")
                if project.scope_version_id != run.scope_version_id and body.values.get("confirm_scope_change") is not True:
                    raise AppError(409, "SCOPE_VERSION_CONFLICT", "项目范围已变化，请核对最新范围后明确重新确认",
                                   {"scope_version_id": project.scope_version_id})
                proposed = run.input_request.get("changes", {}) if kind == "plan_confirmation" else run.input_request.get("scope", {})
                changes = {**proposed, **(submitted_scope or {})}
                scope = update_scope(session, project, body.expected_scope_version_id, changes, True)
                run.scope_version_id = scope.id
                if kind == "plan_confirmation":
                    run.payload = {**run.payload, "plan_confirmed": True,
                                   "plan": {**run.payload.get("plan", {}), "scope_changes": changes}}
                run.payload = {key: value for key, value in run.payload.items()
                               if key not in {"analysis_id", "execution_authorization"}}
            elif kind == "report_target":
                if body.values.get("confirmed") is not True:
                    raise AppError(422, "CONFIRMATION_REQUIRED", "请明确确认修改目标与报告采用版本")
                version = owned(session, ReportVersion, body.values.get("base_version_id"))
                report = owned(session, Report, version.report_id, lock=True)
                if report.project_id != project.id:
                    raise AppError(404, "REPORT_NOT_FOUND", "报告不属于此项目")
                if report.current_version_id != body.values.get("expected_current_version_id"):
                    raise AppError(409, "REPORT_VERSION_CONFLICT", "采用版本已经变化，请重新选择")
                targets = body.values.get("block_ids", [])
                has_operations = bool(run.payload.get("plan", {}).get("operations"))
                if not isinstance(targets, list) or (not targets and not has_operations) or any(
                    target not in {block["id"] for block in version.content["blocks"]} for target in targets):
                    raise AppError(422, "BLOCK_TARGET_REQUIRED", "请选择此版本内需要修改的正文块")
                report.current_version_id = version.id
                run.scope_version_id, run.dataset_version_ids = version.scope_version_id, list(version.dataset_version_ids)
                run.payload = {**run.payload, "target_version_id": version.id, "target_current_id": version.id,
                               "target_block_ids": targets}
            elif kind == "chart_data_confirmation":
                if body.values.get("confirmed") is not True:
                    raise AppError(422, "CONFIRMATION_REQUIRED", "需要明确确认图表筛选")
                run.payload = {**run.payload, "chart_data_confirmed": True}
            else:
                raise AppError(422, "UNKNOWN_INPUT_KIND", "不支持此确认类型")
            run.input_request, run.status, run.dispatch_pending = None, "queued", True
            emit_event(session, run.id, "step.updated", {"step": kind, "status": "succeeded"})
            return task_result(run, scope_version_id=run.scope_version_id)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.post("/api/runs/{id}/cancel", response_model=schemas.TaskResponse, tags=["runs"])
    def cancel_run(id: UUID, request: Request, response: Response, session: DB):
        def action():
            run = owned(session, ResearchRun, id, lock=True)
            if run.status not in TERMINAL:
                run.cancellation_requested = True
                run.status = "cancelling" if run.status in {"running", "cancelling"} else "cancelled"
                run.dispatch_pending = False
                if run.kind in {"import", "merge_import"} and run.status == "cancelled":
                    version = session.get(DatasetVersion, run.payload["dataset_version_id"])
                    if version.status != "ready":
                        version.status = "cancelled"
                if run.kind == "parse_material" and run.status == "cancelled":
                    material = session.get(Material, run.payload["material_id"])
                    if material and material.status != "ready":
                        material.status = "cancelled"
                emit_event(session, run.id, "step.updated" if run.status == "cancelling" else "run.completed", {"status": run.status})
            return task_result(run)
        return mutate(session, request, response, {}, action)

    @app.post("/api/runs/{id}/retry", response_model=schemas.TaskResponse, status_code=202, tags=["runs"])
    def retry_run(id: UUID, request: Request, response: Response, session: DB):
        def action():
            old = owned(session, ResearchRun, id, lock=True)
            if old.status != "failed":
                raise AppError(409, "RUN_NOT_FAILED", "只能重试失败任务")
            run = create_run(session, old.kind, payload=dict(old.payload), conversation_id=old.conversation_id, retry_of_id=old.id)
            run.project_id, run.scope_version_id, run.dataset_version_ids = old.project_id, old.scope_version_id, list(old.dataset_version_ids)
            origin = (session.get(Message, old.payload["message_id"]) if old.payload.get("message_id") else
                      session.scalar(select(Message).where(Message.run_id == old.id, Message.role == "user")))
            if origin and origin.role == "user" and origin.conversation_id == old.conversation_id:
                origin.run_id = run.id
                origin.cards = [card for card in origin.cards if card.get("type") != "task"]
            if old.kind == "parse_material":
                material = session.get(Material, old.payload["material_id"])
                material.run_id, material.status, material.error = run.id, "pending", None
            if old.kind in {"import", "merge_import"}:
                version = session.get(DatasetVersion, old.payload["dataset_version_id"])
                if version.status != "ready":
                    version.status = "pending"
            return task_result(run, retry_of_id=old.id)
        return mutate(session, request, response, {}, action, 202)

    @app.get("/api/projects/{id}/analysis", response_model=schemas.AnalysisResponse, tags=["analysis"])
    def analysis_view(id: UUID, session: DB, run_id: UUID | None = None, view: str = "overview", analysis_id: UUID | None = None):
        project = owned(session, Project, id)
        analysis_id = str(analysis_id) if analysis_id else project.current_analysis_id
        if run_id:
            run = owned(session, ResearchRun, run_id)
            if run.project_id != project.id:
                raise AppError(404, "RUN_NOT_FOUND", "任务不属于当前项目")
            analysis_id = run.result.get("analysis_id")
            if not analysis_id:
                return {"status": "not_generated", "run_status": run.status, "analysis_id": None, "data": None}
        if not analysis_id:
            return {"status": "not_generated", "analysis_id": None, "data": None}
        analysis = owned(session, AnalysisResult, analysis_id)
        if analysis.project_id != project.id:
            raise AppError(404, "ANALYSIS_NOT_FOUND", "分析不属于当前项目")
        selected = analysis.views.get(view)
        if selected is None:
            raise AppError(422, "UNKNOWN_ANALYSIS_VIEW", "不支持的分析视角", list(analysis.views))
        stale = analysis.scope_version_id != project.scope_version_id or analysis.dataset_version_ids != [project.dataset_version_id]
        return {"status": "stale" if stale else selected.get("status", "ready"), "analysis_id": analysis.id,
                "run_id": analysis.run_id, "scope_version_id": analysis.scope_version_id,
                "dataset_version_ids": analysis.dataset_version_ids, "data": selected,
                "summary": analysis.summary, "snapshot_status": "stale" if stale else "current"}

    @app.get("/api/charts/{id}", response_model=schemas.ChartResponse, tags=["analysis"])
    def chart_detail(id: UUID, session: DB, version_id: UUID | None = None):
        from app.chart_service import snapshot
        chart = owned(session, Chart, id)
        return {**record(chart), **snapshot(session, chart, str(version_id) if version_id else None), "id": chart.id}

    @app.get("/api/evidence/{id}", response_model=schemas.EvidenceResponse, tags=["analysis"])
    def evidence_detail(id: UUID, session: DB):
        return record(owned(session, Evidence, id))

    @app.get("/api/reports/{id}/versions", response_model=schemas.ReportVersionPage | schemas.ReportVersionSummaryPage,
             tags=["reports"])
    def report_versions(id: UUID, session: DB, limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                        summary: bool = False):
        report = owned(session, Report, id)
        columns = (ReportVersion.id, ReportVersion.number, ReportVersion.parent_id,
                   ReportVersion.created_at, ReportVersion.summary) if summary else tuple(ReportVersion.__table__.columns)
        query = select(*columns).where(ReportVersion.report_id == report.id)
        if cursor:
            try:
                point = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                created_at = datetime.fromisoformat(point["created_at"])
                identifier = str(UUID(point["id"]))
                if created_at.utcoffset() is None:
                    raise ValueError("Cursor timestamp requires a timezone")
            except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as exc:
                raise AppError(422, "INVALID_CURSOR", "分页游标无效") from exc
            query = query.where(tuple_(ReportVersion.created_at, ReportVersion.id) < (created_at, identifier))
        rows = session.execute(query.order_by(ReportVersion.created_at.desc(), ReportVersion.id.desc())
                               .limit(limit + 1)).mappings().all()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "created_at": last["created_at"].isoformat(), "id": last["id"]}).encode()).decode()
        for item in items:
            item["is_current"] = item["id"] == report.current_version_id
        total = session.scalar(select(func.count()).select_from(ReportVersion).where(ReportVersion.report_id == report.id))
        return {"items": items, "next_cursor": next_cursor, "total": total, "returned_count": len(items),
                "current_version_id": report.current_version_id}

    @app.get("/api/reports/{id}/versions/{version}", response_model=schemas.ReportVersionResponse, tags=["reports"])
    def report_version(id: UUID, version: UUID, session: DB, view: Literal["full", "display"] = "full"):
        report = owned(session, Report, id)
        if view == "display":
            metadata = [column for column in ReportVersion.__table__.columns if column.key != "content"]
            fields = ("title", "layout", "blocks")
            row = session.execute(select(*metadata, *(ReportVersion.content[key].label(f"display_{key}") for key in fields))
                                  .join(Report, ReportVersion.report_id == Report.id)
                                  .join(Project, Report.project_id == Project.id)
                                  .where(ReportVersion.id == str(version), Project.owner_id == get_settings().dev_owner_id)
                                  ).mappings().first()
            if not row:
                raise AppError(404, "NOT_FOUND", "记录不存在")
            if row["report_id"] != report.id:
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "报告版本不属于此报告")
            result = dict(row)
            result["content"] = {key: result.pop(f"display_{key}") for key in fields}
            return result
        row = owned(session, ReportVersion, version)
        if row.report_id != report.id:
            raise AppError(404, "REPORT_VERSION_NOT_FOUND", "报告版本不属于此报告")
        return record(row)

    @app.post("/api/reports/{id}/revisions", response_model=schemas.TaskResponse, status_code=202, tags=["reports"])
    def report_revision(id: UUID, body: schemas.ReportRevision, request: Request, response: Response, session: DB):
        def action():
            report = owned(session, Report, id)
            project = owned(session, Project, report.project_id)
            base = owned(session, ReportVersion, body.base_version_id)
            if base.report_id != report.id:
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "基础版本不属于当前报告")
            if report.current_version_id != str(body.expected_current_version_id) or report.current_version_id != base.id:
                raise AppError(409, "REPORT_VERSION_CONFLICT", "请先切回需要修改的基础版本")
            payload = {**body.model_dump(mode="json", exclude_none=True), "report_id": report.id}
            run = create_run(session, "revise_report", project, payload)
            if body.conversation_id:
                conversation = owned(session, Conversation, body.conversation_id)
                if conversation.project_id != project.id:
                    raise AppError(404, "CONVERSATION_NOT_FOUND", "对话不属于此项目")
                run.conversation_id = conversation.id
                labels = {"set_title": "修改标题", "replace_text": "修改文字", "insert_block": "添加内容",
                          "remove_block": "移除内容", "move_block": "调整顺序", "append_roadmap": "追加布局与路线图",
                          "insert_chart": "插入图表", "update_chart": "更新图表", "set_layout": "调整页面排版",
                          "set_chart_layout": "调整图表排版"}
                session.add(Message(conversation_id=conversation.id, run_id=run.id, role="user",
                    content=body.instruction or ("修改报告正文" if not body.operations else "更新报告：" + "、".join(labels[op.op] for op in body.operations)),
                    reference={"report_version_id": base.id, "block_id": body.block_id},
                    cards=[{"type": "task", "run_id": run.id, "kind": "revise_report"}]))
            run.scope_version_id, run.dataset_version_ids = base.scope_version_id, list(base.dataset_version_ids)
            return task_result(run)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.put("/api/reports/{id}/current-version", response_model=schemas.AdoptedVersionResponse, tags=["reports"])
    def adopt_version(id: UUID, body: schemas.CurrentVersionUpdate, request: Request, response: Response, session: DB):
        def action():
            report = owned(session, Report, id, lock=True)
            if report.current_version_id != str(body.expected_current_version_id):
                raise AppError(409, "REPORT_VERSION_CONFLICT", "当前采用版本已更新")
            version = owned(session, ReportVersion, body.version_id)
            if version.report_id != report.id:
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "版本不属于此报告")
            report.current_version_id = version.id
            return {"report_id": report.id, "current_version_id": version.id}
        return mutate(session, request, response, body.model_dump(mode="json"), action)

    @app.post("/api/reports/{id}/exports", response_model=schemas.TaskResponse, status_code=202, tags=["reports"])
    def export_report(id: UUID, body: schemas.ReportExport, request: Request, response: Response, session: DB):
        def action():
            report = owned(session, Report, id)
            version = owned(session, ReportVersion, body.report_version_id)
            if version.report_id != report.id:
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "版本不属于当前报告")
            run = create_run(session, "export", owned(session, Project, report.project_id),
                             {**body.model_dump(mode="json"), "report_id": report.id})
            run.scope_version_id, run.dataset_version_ids = version.scope_version_id, list(version.dataset_version_ids)
            return task_result(run)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.get("/api/files/{id}", tags=["files"], response_class=FileResponse)
    def download_file(id: UUID, session: DB):
        file = owned(session, StoredFile, id)
        path = resolve_file(file.key)
        if not path.is_file():
            raise AppError(404, "FILE_NOT_FOUND", "文件未生成或已不可用")
        return FileResponse(path, media_type=file.media_type, filename=file.original_name)

    from app.extension_api import register
    register(app)
    return app


app = create_app()
