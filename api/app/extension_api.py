"""Additive endpoints for materials and immutable research artefacts."""
from uuid import UUID
from pathlib import Path

from fastapi import File, Query, Request, Response, UploadFile
from sqlalchemy import func, select, text

from app import schemas
from app.errors import AppError
from app.files import store_bytes
from app.jobs import create_run
from app.models import (AnalysisResult, Chart, ChartVersion, Material, MaterialBlock,
                        Project, ResearchRun, StoredFile)


def register(app):
    from app.main import DB, mutate, owned, paginate, record, task_result

    def material_record(session, row):
        file = session.get(StoredFile, row.file_id)
        return {**record(row), "file_name": file.original_name,
                "download_url": f"/api/files/{file.id}", "sha256": file.sha256}

    @app.post("/api/projects/{id}/materials", response_model=schemas.TaskResponse, status_code=202, tags=["materials"])
    def upload_material(id: UUID, request: Request, response: Response, session: DB, file: UploadFile = File(...)):
        import hashlib
        content = file.file.read(20 * 1024 * 1024 + 1)
        if not file.filename or not file.filename.lower().endswith((".doc", ".docx")) or len(content) > 20 * 1024 * 1024:
            raise AppError(422, "INVALID_MATERIAL_FILE", "请上传不超过 20MB 的 Word 资料")
        digest = hashlib.sha256(content).hexdigest()

        def action():
            project = owned(session, Project, id)
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"material:{id}:{digest}"})
            existing = session.scalar(select(Material).join(StoredFile, Material.file_id == StoredFile.id).where(
                Material.project_id == project.id, StoredFile.sha256 == digest).order_by(Material.created_at).limit(1))
            if existing:
                previous = session.get(ResearchRun, existing.run_id)
                if existing.status != "ready" and previous and previous.status == "cancelled":
                    resumed = create_run(session, "parse_material", project, {"material_id": existing.id}, retry_of_id=previous.id)
                    existing.run_id, existing.status, existing.error = resumed.id, "pending", None
                return {"run_id": existing.run_id, "status": existing.status, "material_id": existing.id, "reused": True}
            source = store_bytes(session, content, file.filename, "material", project_id=project.id)
            filename = Path(file.filename).name
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                            {"key": f"material-version:{project.id}:{filename}"})
            number = session.scalar(select(func.max(Material.version)).join(StoredFile, Material.file_id == StoredFile.id)
                .where(Material.project_id == project.id, StoredFile.original_name == filename)) or 0
            material = Material(project_id=project.id, file_id=source.id, status="pending", version=number + 1)
            session.add(material)
            session.flush()
            run = create_run(session, "parse_material", project, {"material_id": material.id})
            material.run_id = run.id
            return task_result(run, material_id=material.id, file_id=source.id, reused=False)
        return mutate(session, request, response, {"sha256": digest}, action, 202)

    @app.get("/api/projects/{id}/materials", response_model=schemas.Page[schemas.MaterialResponse], tags=["materials"])
    def list_materials(id: UUID, session: DB, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        project = owned(session, Project, id)
        rows = session.scalars(select(Material).where(Material.project_id == project.id)).all()
        result = paginate(rows, limit, cursor, True)
        result["items"] = [material_record(session, session.get(Material, row["id"])) for row in result["items"]]
        return result

    @app.get("/api/materials/{id}", response_model=schemas.MaterialResponse, tags=["materials"])
    def material_detail(id: UUID, session: DB):
        material = owned(session, Material, id)
        blocks = session.scalars(select(MaterialBlock).where(MaterialBlock.material_id == material.id).order_by(MaterialBlock.ordinal)).all()
        return {**material_record(session, material), "blocks": [record(block) for block in blocks]}

    @app.post("/api/projects/{id}/analysis/revisions", response_model=schemas.TaskResponse, status_code=202, tags=["analysis"])
    def revise_analysis(id: UUID, body: schemas.AnalysisRevision, request: Request, response: Response, session: DB):
        def action():
            project = owned(session, Project, id, lock=True)
            base = owned(session, AnalysisResult, body.base_analysis_id)
            if base.project_id != project.id:
                raise AppError(404, "ANALYSIS_NOT_FOUND", "分析不属于当前项目")
            if project.current_analysis_id != str(body.expected_current_analysis_id) or project.current_analysis_id != base.id:
                raise AppError(409, "ANALYSIS_VERSION_CONFLICT", "采用分析已变化，请保留修改并刷新")
            run = create_run(session, "revise_analysis", project, body.model_dump(mode="json"))
            run.scope_version_id, run.dataset_version_ids = base.scope_version_id, list(base.dataset_version_ids)
            return task_result(run)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.get("/api/charts/{id}/versions", response_model=schemas.ChartVersionPage, tags=["analysis"])
    def chart_versions(id: UUID, session: DB, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        from app.chart_service import ensure_versions, snapshot
        chart = owned(session, Chart, id)
        ensure_versions(session, [chart])
        rows = session.scalars(select(ChartVersion).where(ChartVersion.chart_id == chart.id)).all()
        result = paginate(rows, limit, cursor, True)
        result["items"] = [{**record(chart), **snapshot(session, chart, row["id"]), "id": chart.id} for row in result["items"]]
        result["current_version_id"] = chart.version_id
        return result

    @app.post("/api/charts/{id}/revisions", response_model=schemas.TaskResponse, status_code=202, tags=["analysis"])
    def chart_revision(id: UUID, body: schemas.ChartRevision, request: Request, response: Response, session: DB):
        def action():
            from app.chart_service import snapshot
            chart = owned(session, Chart, id, lock=True)
            snapshot(session, chart, str(body.base_version_id))
            if chart.version_id != str(body.expected_current_version_id):
                raise AppError(409, "CHART_VERSION_CONFLICT", "图表采用版本已变化")
            project = owned(session, Project, chart.project_id)
            analysis = session.get(AnalysisResult, chart.analysis_id)
            run = create_run(session, "revise_chart", project, {**body.model_dump(mode="json"), "chart_id": chart.id})
            run.scope_version_id, run.dataset_version_ids = analysis.scope_version_id, list(analysis.dataset_version_ids)
            return task_result(run)
        return mutate(session, request, response, body.model_dump(mode="json"), action, 202)

    @app.put("/api/charts/{id}/current-version", response_model=schemas.ChartResponse, tags=["analysis"])
    def adopt_chart(id: UUID, body: schemas.CurrentVersionUpdate, request: Request, response: Response, session: DB):
        def action():
            from app.chart_service import adopt
            chart = owned(session, Chart, id, lock=True)
            result = adopt(session, chart, str(body.version_id), str(body.expected_current_version_id))
            return {**record(chart), **result}
        return mutate(session, request, response, body.model_dump(mode="json"), action)
