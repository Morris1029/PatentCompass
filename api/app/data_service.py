"""CNIPA spreadsheet ingestion, reproducible retrieval and statistical analysis."""

import hashlib
from datetime import date

from sqlalchemy import Text, cast, exists, func, or_, select

from app.config import get_settings
from app.db import session_scope
from app.errors import AppError
from app.files import resolve_file
from app.model_provider import embed
from app.models import (AnalysisResult, Chart, DatasetVersion, Evidence, PatentApplication,
                        PatentEmbedding, PatentPublication, Project, ResearchRun, ScopeVersion, StoredFile, new_id)


from patentcompass_core.parsing import HEADERS, REQUIRED, _date, _priorities, _text, parse_workbook, split_values
from patentcompass_core.charts import build_chart_options
from patentcompass_core.statistics import COUNTING_METHOD, compute_statistics
from patentcompass_core.retrieval import (KEYWORD_EXPANSIONS, keyword_groups as _keyword_groups,
                                        keyword_score, reciprocal_rank_fusion, text_hash as _text_hash)


def import_dataset(ctx) -> dict:
    version_id = ctx.payload["dataset_version_id"]

    def parse():
        with session_scope() as session:
            version = session.get(DatasetVersion, version_id)
            source = session.get(StoredFile, version.source_file_id)
            path = resolve_file(source.key)
            if hashlib.sha256(path.read_bytes()).hexdigest() != source.sha256:
                raise AppError(409, "SOURCE_FILE_CHANGED", "原始文件校验失败")
        return parse_workbook(path)

    parsed = ctx.step("parse_workbook", parse)
    ctx.check_cancelled()

    def save():
        with session_scope() as session:
            version = session.scalar(select(DatasetVersion).where(DatasetVersion.id == version_id).with_for_update())
            if version.status == "ready":
                return {"dataset_version_id": version_id, **version.quality}
            version.quality = parsed["quality"]
            version.status = parsed["quality"]["status"]
            if version.status == "ready":
                for parsed_row in parsed["applications"]:
                    ctx.check_cancelled()
                    row = dict(parsed_row)
                    publications = row.pop("publications")
                    row["application_date"] = _date(row["application_date"])
                    application = PatentApplication(id=new_id(), dataset_version_id=version_id, **row)
                    session.add(application)
                    session.flush()
                    for publication in publications:
                        session.add(PatentPublication(application_id=application.id, dataset_version_id=version_id,
                                                      **{**publication, "publication_date": _date(publication["publication_date"])}))
            ctx.guard(session)
            return {"dataset_version_id": version_id, **version.quality}

    result = ctx.step("save_dataset", save)
    if result["status"] != "ready":
        raise AppError(422, "DATASET_QUALITY_FAILED", "导入数据包含异常，请查看质量说明", result)
    ctx.emit("dataset.ready", {"dataset_version_id": version_id, "application_count": result["application_count"]})
    return result


def _filtered_statement(dataset_ids: list[str], filters: dict):
    statement = select(PatentApplication).where(PatentApplication.dataset_version_id.in_(dataset_ids))
    if "application_ids" in filters:
        statement = statement.where(PatentApplication.id.in_(filters["application_ids"]))
    if filters.get("start"):
        statement = statement.where(PatentApplication.application_date >= date(int(filters["start"]), 1, 1))
    if filters.get("end"):
        statement = statement.where(PatentApplication.application_date <= date(int(filters["end"]), 12, 31))
    if filters.get("application_start"):
        statement = statement.where(PatentApplication.application_date >= _date(filters["application_start"]))
    if filters.get("application_end"):
        statement = statement.where(PatentApplication.application_date <= _date(filters["application_end"]))
    for name, column in (("applicant", PatentApplication.applicants), ("ipc", PatentApplication.ipc_codes)):
        if filters.get(name):
            statement = statement.where(cast(column, Text).ilike(f"%{filters[name]}%"))
    if filters.get("application_number"):
        statement = statement.where(PatentApplication.application_number.ilike(f"%{filters['application_number']}%"))
    if filters.get("publication_number"):
        statement = statement.where(exists(select(PatentPublication.id).where(
            PatentPublication.application_id == PatentApplication.id,
            PatentPublication.publication_number.ilike(f"%{filters['publication_number']}%"))))
    if filters.get("q"):
        for variants in _keyword_groups(filters["q"]):
            statement = statement.where(or_(*[or_(PatentApplication.title.ilike(f"%{term}%"),
                                                   PatentApplication.abstract.ilike(f"%{term}%")) for term in variants]))
    return statement


def search_patents(session, project: Project, filters: dict | None = None) -> dict:
    filters = dict(filters or {})
    scope_id = filters.pop("scope_version_id", project.scope_version_id)
    scope = session.get(ScopeVersion, scope_id) if scope_id else None
    dataset_ids = filters.pop("dataset_version_ids", [project.dataset_version_id] if project.dataset_version_id else [])
    if scope and scope.project_id != project.id:
        raise AppError(409, "SCOPE_PROJECT_MISMATCH", "范围不属于该项目")
    mode = filters.pop("mode", "keywords" if filters.get("q") else "field")
    if mode not in {"field", "keywords", "semantic", "hybrid"}:
        raise AppError(422, "INVALID_SEARCH_MODE", "无效检索模式")
    scope_filters = dict(scope.content) if scope else {}
    statement = _filtered_statement(dataset_ids, scope_filters)
    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    # Explicit query filters add to the frozen project scope; they never replace it.
    query = filters.get("q", "")
    active_filters = dict(filters)
    if mode in {"semantic", "hybrid"}:
        active_filters.pop("q", None)
    matching = _filtered_statement(dataset_ids, active_filters)
    statement = statement.where(PatentApplication.id.in_(matching.with_only_columns(PatentApplication.id)))
    scores = {}
    if mode in {"semantic", "hybrid"}:
        if not get_settings().embedding_enabled:
            raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置向量模型")
        if not query.strip():
            raise AppError(422, "SEMANTIC_QUERY_REQUIRED", "语义检索需要输入检索内容")
        candidates = list(session.scalars(statement))
        model = get_settings().embedding_model
        indexed = {row.application_id: row for row in session.scalars(select(PatentEmbedding).where(
            PatentEmbedding.application_id.in_([p.id for p in candidates]), PatentEmbedding.model == model))}
        if any(p.id not in indexed or indexed[p.id].text_sha256 != _text_hash(p) for p in candidates):
            raise AppError(409, "EMBEDDING_INDEX_NOT_READY", "当前数据版本的语义索引尚未就绪，请先创建索引任务")
        vector = embed([query])[0]
        if any(e.dimension != len(vector) for e in indexed.values()):
            raise AppError(409, "EMBEDDING_DIMENSION_MISMATCH", "模型维度已变化，请使用新的模型标识重建索引")
        distance = PatentEmbedding.embedding.cosine_distance(vector)
        candidate_limit = max(1, min(int(filters.get("candidate_limit", 500)), 500))
        result = session.execute(select(PatentEmbedding.application_id, distance.label("distance")).where(
            PatentEmbedding.application_id.in_(indexed), PatentEmbedding.model == model).order_by(distance,
            PatentEmbedding.application_id).limit(candidate_limit)).all()
        by_id = {p.id: p for p in candidates}
        items = [by_id[row.application_id] for row in result]
        scores = {row.application_id: 1 - float(row.distance) for row in result}
        if mode == "hybrid":
            keyword_statement = _filtered_statement(dataset_ids, {**active_filters, "q": query})
            keyword_ids = set(session.scalars(keyword_statement.with_only_columns(PatentApplication.id)))
            keyword_items = sorted((p for p in candidates if p.id in keyword_ids), key=lambda p: (-keyword_score(p, query), p.id))
            ordered, scores = reciprocal_rank_fusion([[p.id for p in items], [p.id for p in keyword_items]], candidate_limit)
            items = [by_id[patent_id] for patent_id in ordered]
    else:
        items = list(session.scalars(statement.order_by(PatentApplication.application_date.desc(), PatentApplication.id)))
    return {"items": items, "scores": scores, "mode": mode, "candidate_count": len(items), "scope_application_count": total}


def build_embeddings(ctx) -> dict:
    settings = get_settings()
    if not settings.embedding_enabled:
        raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置向量模型")
    with session_scope() as session:
        patents = list(session.scalars(select(PatentApplication).where(
            PatentApplication.dataset_version_id.in_(ctx.dataset_version_ids)).order_by(PatentApplication.id)))
    model = settings.embedding_model
    batch_size = settings.embedding_batch_size
    # Checkpoints must not be reused for different model or batch boundaries.
    model_key = hashlib.sha256(model.encode()).hexdigest()[:12]
    for offset in range(0, len(patents), batch_size):
        batch = patents[offset:offset + batch_size]

        def persist():
            with session_scope() as session:
                pending = [p for p in batch if not session.scalar(select(PatentEmbedding.id).where(
                    PatentEmbedding.application_id == p.id, PatentEmbedding.model == model, PatentEmbedding.text_sha256 == _text_hash(p)))]
            vectors = embed([f"{p.title}\n{p.abstract}" for p in pending])
            with session_scope() as session:
                for patent, vector in zip(pending, vectors):
                    session.add(PatentEmbedding(application_id=patent.id, model=model, dimension=len(vector),
                                                text_sha256=_text_hash(patent), embedding=vector))
                ctx.guard(session)
            return {"count": len(batch), "model": model}

        ctx.check_cancelled()
        ctx.step(f"embedding_batch_{model_key}_{batch_size}_{offset}", persist)
    return {"indexed_count": len(patents), "model": model, "dataset_version_ids": ctx.dataset_version_ids}


def _chart_option(title: str, rows: list[dict], kind: str) -> dict:
    return build_chart_options({"title": title, "rows": rows, "kind": kind})


def _update_analysis_context(session, ctx, analysis, expected_analysis_id=None):
    research_id = ctx.payload.get("research_id")
    if research_id:
        from app import research_service
        from app.models import Report, ResearchVersion
        research = session.scalar(select(ResearchVersion).join(Report, ResearchVersion.report_id == Report.id).where(
            ResearchVersion.id == research_id, Report.project_id == ctx.project_id).with_for_update(of=ResearchVersion))
        if not research:
            raise AppError(404, "RESEARCH_NOT_FOUND", "研究版本不存在")
        current = research_service.context_for(session, research)
        if expected_analysis_id is not None and current.get("analysis_id") != expected_analysis_id:
            raise AppError(409, "ANALYSIS_VERSION_CONFLICT", "当前分析已变化，分类纠正未覆盖最新分析",
                           {"current_analysis_id": current.get("analysis_id")})
        if current["scope_version_id"] == ctx.scope_version_id and current["dataset_version_ids"] == ctx.dataset_version_ids:
            research_service.set_context(session, research, analysis_id=analysis.id)
        return
    project = session.scalar(select(Project).where(Project.id == ctx.project_id).with_for_update())
    if expected_analysis_id is not None and project.current_analysis_id != expected_analysis_id:
        raise AppError(409, "ANALYSIS_VERSION_CONFLICT", "当前分析已变化，分类纠正未覆盖最新分析",
                       {"current_analysis_id": project.current_analysis_id})
    if project.scope_version_id == ctx.scope_version_id and ctx.dataset_version_ids == [project.dataset_version_id]:
        project.current_analysis_id = analysis.id


def analyze(ctx) -> dict:
    def calculate():
        with session_scope() as session:
            project = session.get(Project, ctx.project_id)
            result = search_patents(session, project, {"scope_version_id": ctx.scope_version_id,
                                                     "dataset_version_ids": ctx.dataset_version_ids})
            patents = result["items"]
            ids = [patent.id for patent in patents]
            publications = list(session.scalars(select(PatentPublication).where(PatentPublication.application_id.in_(ids))))
            versions = list(session.scalars(select(DatasetVersion).where(DatasetVersion.id.in_(ctx.dataset_version_ids))))
            return compute_statistics(patents, publications, versions)

    calculated = ctx.step("calculate_statistics_v2_country", calculate)
    ctx.check_cancelled()

    def persist():
        with session_scope() as session:
            run = session.get(ResearchRun, ctx.run_id)
            run_ids = [ctx.run_id] + ([run.retry_of_id] if run and run.retry_of_id else [])
            existing = session.scalar(select(AnalysisResult).where(AnalysisResult.run_id.in_(run_ids)))
            if existing:
                return _analysis_result(session, existing)
            summary = calculated["summary"]
            analysis = AnalysisResult(id=new_id(), project_id=ctx.project_id, run_id=ctx.run_id,
                                      scope_version_id=ctx.scope_version_id, dataset_version_ids=ctx.dataset_version_ids,
                                      patent_ids=calculated["patent_ids"], summary=summary)
            session.add(analysis)
            session.flush()
            charts, evidence_ids = {}, []
            for kind, dimension, title, key in (("yearly", "year", "年度申请分布", "year"),
                                                ("applicants", "applicant", "主要申请人", "name"),
                                                ("ipc", "ipc", "IPC 技术分类", "code"),
                                                ("country", "country", "申请人来源国家", "country")):
                # Summary keeps every group; the rendered ranking shows the leading 15.
                rows = [{"value": str(row[key]), "label": str(row[key]), "count": row["count"]} for row in summary[kind]]
                displayed = rows if kind == "yearly" else rows[:15]
                chart = Chart(id=new_id(), project_id=ctx.project_id, analysis_id=analysis.id, kind=kind,
                              option=_chart_option(title, displayed, kind),
                              data={"title": title, "dimension": dimension, "rows": displayed, "all_rows": rows, "unit": "件申请"},
                              provenance={"scope_version_id": ctx.scope_version_id, "dataset_version_ids": ctx.dataset_version_ids,
                                          "application_ids": calculated["patent_ids"], "counting_method": COUNTING_METHOD},
                              selections={"dimension": dimension, "values": calculated["selections"][kind]})
                evidence = Evidence(id=new_id(), project_id=ctx.project_id, analysis_id=analysis.id, kind="statistic",
                                    content={"metric": kind, "rows": rows, "chart_id": chart.id,
                                             "application_count": summary["application_count"], "counting_method": COUNTING_METHOD,
                                             "dataset_version_ids": ctx.dataset_version_ids, "scope_version_id": ctx.scope_version_id})
                session.add_all([chart, evidence])
                charts[kind] = chart.id
                evidence_ids.append(evidence.id)
            for patent in calculated["representative"]:
                evidence = Evidence(id=new_id(), project_id=ctx.project_id, analysis_id=analysis.id,
                                    application_id=patent["application_id"], kind="patent", content=patent)
                session.add(evidence)
                evidence_ids.append(evidence.id)
            ready = lambda data, kinds: {"status": "ready", "data": data, "chart_ids": [charts[k] for k in kinds], "evidence_ids": evidence_ids}
            unavailable = lambda reason: {"status": "unavailable", "data": None, "chart_ids": [], "evidence_ids": [], "reason": reason}
            analysis.views = {
                "overview": ready(summary, list(charts)),
                "landscape": ready({"yearly": summary["yearly"], "applicants": summary["applicants"]}, ["yearly", "applicants"]),
                "technology": ready({"classification": "IPC", "ipc": summary["ipc"]}, ["ipc"]),
                "opportunities": unavailable("当前仅提供确定性样本统计；候选机会需要模型分析和外部补检"),
                "roadmap": unavailable("当前样本缺少权利要求与法律状态，尚未执行语义路线分析"),
                "geography": ready({"country": summary["country"],
                    "counting_method": "共同来源国家全计数，统计申请人来源，不等于保护地",
                    "unavailable": {"province": "样本没有省市或详细地址", "protection": "缺少有效保护地及法律状态",
                                    "family": "没有完整同族数据"}}, ["country"]),
            }
            _update_analysis_context(session, ctx, analysis)
            ctx.guard(session)
            session.flush()
            return _analysis_result(session, analysis)

    result = ctx.step("persist_analysis", persist)
    ctx.emit("analysis.ready", result)
    return result


def _analysis_result(session, analysis) -> dict:
    return {"analysis_id": analysis.id, "chart_ids": list(session.scalars(select(Chart.id).where(Chart.analysis_id == analysis.id))),
            "evidence_ids": list(session.scalars(select(Evidence.id).where(Evidence.analysis_id == analysis.id))),
            "application_count": analysis.summary["application_count"], "publication_count": analysis.summary["publication_count"]}
