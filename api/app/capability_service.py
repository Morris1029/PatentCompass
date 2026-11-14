"""One application entrypoint for standalone HTTP and MCP capabilities."""

from patentcompass_agent import prompt_version, read_prompt

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import shutil
from uuid import NAMESPACE_URL, uuid5

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import select

from app import capability_contracts as inputs
from app import model_provider
from app.config import get_settings
from app.db import session_scope
from app.errors import AppError
from app.files import resolve_file, store_bytes
from app.models import (AnalysisResult, DatasetVersion, Evidence, Material, MaterialBlock,
                        PatentApplication, PatentEmbedding, PatentPublication, Project,
                        ReportVersion, StoredFile)
from app.resource_service import idempotent, owned, public_value, record
from patentcompass_core.contracts import PatentRecord


@dataclass(frozen=True)
class Capability:
    input_model: type[inputs.Input]
    description: str
    asynchronous: bool = False


CAPABILITIES = {
    "parse_patent_workbook": Capability(inputs.FileInput, "解析上传的专利工作簿，保留原始行和质量说明", True),
    "parse_document": Capability(inputs.FileInput, "解析 Word 正文和表格，返回原文位置", True),
    "search_patents": Capability(inputs.SearchInput, "在明确的数据和范围中检索专利，返回原文与版本依据"),
    "get_patent": Capability(inputs.PatentReadInput, "读取已授权专利及公开记录"),
    "search_materials": Capability(inputs.MaterialsInput, "检索项目已解析资料，返回段落和表格定位"),
    "read_analysis": Capability(inputs.AnalysisInput, "读取明确分析版本的一个工作台视图"),
    "get_evidence": Capability(inputs.EvidenceInput, "读取明确版本的统计或原文证据"),
    "get_report_version": Capability(inputs.ReportVersionInput, "读取不可变报告快照"),
    "compute_statistics": Capability(inputs.PatentInput, "计算申请、公开、年度、申请人、IPC和来源国家统计"),
    "classify_patents": Capability(inputs.PatentInput, "逐条语义标注、核验原文并归并主题", True),
    "build_embeddings": Capability(inputs.PatentInput, "为输入专利生成向量，数据引用输入同时更新对应索引", True),
    "build_report": Capability(inputs.ReportBuildInput, "从明确的专利和分析依据构建独立报告内容，不发布项目版本", True),
    "revise_report_content": Capability(inputs.ReportReviseInput, "修改独立报告内容，不修改项目版本", True),
    "build_chart_options": Capability(inputs.ChartInput, "由明确的统计行构建 ECharts 配置"),
    "render_chart": Capability(inputs.RenderInput, "将图表配置渲染成 PNG 文件", True),
    "export_docx": Capability(inputs.ExportInput, "将给定报告快照导出为 Word 文件", True),
}


def catalogue():
    return {"version": "1", "items": [{"name": name, "description": spec.description,
        "execution": "task" if spec.asynchronous else "inline", "input_schema": spec.input_model.model_json_schema(),
        "output": "run_id/status" if spec.asynchronous else "structured_result"} for name, spec in CAPABILITIES.items()]}


def validate(name, payload):
    spec = CAPABILITIES.get(name)
    if not spec:
        raise AppError(404, "CAPABILITY_NOT_FOUND", "能力不存在")
    try:
        return spec.input_model.model_validate(payload)
    except ValidationError as error:
        raise AppError(422, "VALIDATION_ERROR", "能力参数无效", json.loads(error.json(include_url=False))) from error


def _patents(session, request):
    """Resolve and detach an explicit source once, before queueing expensive work."""
    from patentcompass_core.retrieval import filter_records
    if request.records is not None:
        patents = request.records
    else:
        versions = [owned(session, DatasetVersion, value) for value in request.dataset_version_ids]
        if any(version.status != "ready" for version in versions):
            raise AppError(409, "DATASET_NOT_READY", "数据版本尚未就绪")
        rows = session.scalars(select(PatentApplication).where(
            PatentApplication.dataset_version_id.in_([row.id for row in versions])).order_by(PatentApplication.id)).all()
        publications = {}
        for publication in session.scalars(select(PatentPublication).where(
                PatentPublication.dataset_version_id.in_([row.id for row in versions]))):
            publications.setdefault(publication.application_id, []).append(record(publication))
        patents = [PatentRecord.model_validate({**record(row), "publications": publications.get(row.id, [])}) for row in rows]
    return filter_records(patents, request.scope)


def _source_file(session, file_id):
    source = owned(session, StoredFile, file_id)
    path = resolve_file(source.key)
    if not path.is_file():
        raise AppError(404, "FILE_NOT_FOUND", "原始文件不可用")
    if hashlib.sha256(path.read_bytes()).hexdigest() != source.sha256:
        raise AppError(409, "FILE_HASH_MISMATCH", "文件内容与已保存的引用不一致")
    return source, path


def _freeze(session, request):
    payload = request.model_dump(mode="json")
    if isinstance(request, inputs.PatentInput):
        rows = _patents(session, request)
        if not rows:
            raise AppError(422, "NO_SELECTED_PATENTS", "当前范围没有可处理的申请")
        payload["records"] = [row.model_dump(mode="json") for row in rows]
        payload["dataset_version_ids"] = None
    if isinstance(request, inputs.FileInput):
        _source_file(session, request.file_id)
    return payload


def _version_snapshots(session, version_ids):
    result = []
    for version_id in version_ids:
        version = owned(session, DatasetVersion, version_id)
        source = owned(session, StoredFile, version.source_file_id)
        result.append({"dataset_version_id": version.id, "source_file_id": source.id,
            "file_name": source.original_name, "sha256": source.sha256, "rule_version": version.rule_version,
            "quality": deepcopy(version.quality), "source_metadata": deepcopy(version.source_metadata)})
    return result


def _model_fingerprint(name, request):
    prefix = "embedding" if name == "build_embeddings" else "llm"
    uses_model = name in {"classify_patents", "build_embeddings"} or (
        name == "build_report" and request.report_mode == "ai") or (
        name == "revise_report_content" and request.instruction is not None)
    if not uses_model:
        return None
    settings = get_settings()
    value = {"protocol": "responses-v1" if prefix == "llm" else "embeddings-v1",
             "model": getattr(settings, prefix + "_model"), "base_url": getattr(settings, prefix + "_base_url"),
             "options": settings.llm_extra_body if prefix == "llm" else {"batch_size": settings.embedding_batch_size}}
    if prefix == "llm":
        names = {"classify_patents": ("semantic-extraction", "semantic-taxonomy"),
                 "build_report": ("report-summary",), "revise_report_content": ("report-rewrite",)}[name]
        versions = {key: prompt_version(key, "legacy") for key in names}
        if any(version != "legacy" for version in versions.values()):
            value["prompts"] = versions
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def invoke(session, name, payload, idempotency_key=None):
    from app.jobs import create_run
    request = validate(name, payload)
    if not CAPABILITIES[name].asynchronous:
        return public_value(_execute(session, name, request)), 200
    if not idempotency_key:
        raise AppError(422, "IDEMPOTENCY_KEY_REQUIRED", "提交任务需要 Idempotency-Key")

    def submit():
        frozen = _freeze(session, request)
        source_ids = [str(value) for value in (getattr(request, "dataset_version_ids", None) or [])]
        run = create_run(session, "capability", payload={"capability": name, "capability_version": 1,
                         "input": frozen, "source_dataset_version_ids": source_ids,
                         "source_versions": _version_snapshots(session, source_ids),
                         "model_fingerprint": _model_fingerprint(name, request)})
        run.dataset_version_ids = source_ids
        return {"run_id": run.id, "status": run.status}

    return idempotent(session, "capability:" + name, idempotency_key, request.model_dump(mode="json"), submit, 202)


def _renderer():
    from patentcompass_core.rendering import RendererConfig
    return RendererConfig(shutil.which("node") or "", Path(__file__).resolve().parents[1] / "chart-renderer" / "render.mjs")


def _artifact(session, data, filename, ctx):
    from app.models import ResearchRun
    row, run_id, seen = None, ctx.run_id if ctx else None, set()
    while run_id and run_id not in seen:
        seen.add(run_id)
        row = session.scalar(select(StoredFile).where(StoredFile.run_id == run_id))
        if row:
            break
        run = session.get(ResearchRun, run_id)
        run_id = run.retry_of_id if run else None
    if row is None:
        row = store_bytes(session, data, Path(filename).name, "capability", run_id=ctx.run_id if ctx else None)
    return {"file_id": row.id, "filename": row.original_name, "media_type": row.media_type,
            "size": row.size, "sha256": row.sha256, "download_url": f"/api/files/{row.id}"}


def _execute(session, name, request, ctx=None):
    from patentcompass_core import charts, documents, parsing, rendering, reports, retrieval, semantics, statistics
    if name in {"parse_patent_workbook", "parse_document"}:
        source, path = _source_file(session, request.file_id)
        value = parsing.parse_workbook(path) if name == "parse_patent_workbook" else {"blocks": documents.parse_document(path)}
        return jsonable_encoder({**value, "file_id": source.id, "source_sha256": source.sha256})
    if name == "get_patent":
        patent = session.get(PatentApplication, str(request.patent_id))
        if not patent:
            raise AppError(404, "NOT_FOUND", "专利不存在")
        owned(session, DatasetVersion, patent.dataset_version_id)
        return {**record(patent), "publications": [record(row) for row in session.scalars(select(PatentPublication).where(
            PatentPublication.application_id == patent.id))]}
    if name == "search_materials":
        return search_materials(session, str(request.project_id), request.q, request.limit)
    if name == "read_analysis":
        return read_analysis(session, str(request.analysis_id), request.view)
    if name in {"get_evidence", "get_report_version"}:
        model, value = (Evidence, request.evidence_id) if name == "get_evidence" else (ReportVersion, request.report_version_id)
        return record(owned(session, model, value))
    if name == "build_chart_options":
        if any(not {"label", "count"} <= set(row) or isinstance(row.get("count"), bool)
               or not isinstance(row.get("count"), (int, float)) or not math.isfinite(row["count"]) for row in request.rows):
            raise AppError(422, "INVALID_CHART_ROWS", "图表行需要 label 和数值 count")
        return {"option": charts.build_chart_options(request.model_dump())}
    if name == "render_chart":
        data = rendering.render_chart(request.option, renderer=_renderer())
        return _artifact(session, data, Path(request.filename).stem + ".png", ctx)
    if name == "export_docx":
        data = rendering.build_docx(request.content.model_dump(), request.version_number, renderer=_renderer(),
                                    check_cancelled=ctx.check_cancelled if ctx else lambda: None)
        return _artifact(session, data, Path(request.filename).stem + ".docx", ctx)
    if name == "revise_report_content":
        content = reports.revise_report_content(request.content.model_dump(),
            [operation.model_dump(mode="json", exclude_none=True) for operation in request.operations],
            charts=[chart.model_dump(mode="json") for chart in request.charts],
            namespace=ctx.run_id if ctx else "standalone", instruction=request.instruction,
            block_id=request.block_id, generate_callable=model_provider.chat)
        return {"content": content}

    patents = _patents(session, request)
    if name == "search_patents":
        searches = request.queries or [request.q]
        combined, query_counts = {}, []
        for query in searches:
            kwargs = {}
            if request.mode in {"semantic", "hybrid"}:
                if not get_settings().embedding_enabled:
                    raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置向量模型")
                if request.dataset_version_ids:
                    indexed = {row.application_id: row for row in session.scalars(select(PatentEmbedding).where(
                        PatentEmbedding.application_id.in_([p.id for p in patents]), PatentEmbedding.model == get_settings().embedding_model))}
                    if any(p.id not in indexed or indexed[p.id].text_sha256 != retrieval.text_hash(p) for p in patents):
                        raise AppError(409, "EMBEDDING_INDEX_NOT_READY", "请先建立当前数据和模型的向量索引")
                    vectors = {key: row.embedding for key, row in indexed.items()}
                else:
                    vectors = dict(zip([p.id for p in patents], _embed_records(patents, ctx)))
                kwargs = {"embeddings": vectors, "query_vector": model_provider.embed([query])[0]}
            found = retrieval.search_patents(patents, {**request.filters, "q": query, "mode": request.mode}, **kwargs)
            query_counts.append({"query": query, "matched_count": found["candidate_count"]})
            combined.update({row["id"]: row for row in found["items"]})
        items = list(combined.values())
        return {**found, "items": items[request.offset:request.offset + request.limit], "matched_count": len(items),
                "candidate_count": len(items), "returned_count": len(items[request.offset:request.offset + request.limit]),
                "query_counts": query_counts, "next_offset": request.offset + request.limit if len(items) > request.offset + request.limit else None}
    if name == "compute_statistics":
        return statistics.compute_statistics(patents, versions=_version_snapshots(session, request.dataset_version_ids or []))
    if name == "classify_patents":
        return semantics.classify_patents(patents, model_provider.chat, hooks=ctx)
    if name == "build_embeddings":
        vectors = _embed_records(patents, ctx)
        source_ids = ctx.payload.get("source_dataset_version_ids", []) if ctx else []
        if source_ids:
            _save_embeddings(session, patents, vectors, source_ids)
        return {"model": get_settings().embedding_model, "indexed_count": len(patents),
                "embeddings": [{"application_id": p.id, "embedding": vector} for p, vector in zip(patents, vectors)]}
    if name == "build_report":
        versions = ctx.payload.get("source_versions", []) if ctx else _version_snapshots(session, request.dataset_version_ids or [])
        calculated = statistics.compute_statistics(patents, versions=versions)
        identity = str(uuid5(NAMESPACE_URL, json.dumps([p.model_dump(mode="json") for p in patents], sort_keys=True, ensure_ascii=False)))
        draft = semantics.analysis_draft(patents, request.analysis, versions=versions) if request.analysis is not None else None
        if draft:
            identity = draft["analysis_id"]
        representatives = [{"application_id": p.id, "application_number": p.application_number,
            "title": p.title, "abstract": p.abstract, "applicants": p.applicants,
            "publication_numbers": [value["publication_number"] for value in p.publications],
            "source_sheet": p.source_sheet, "source_row": p.source_row, "dataset_version_id": p.dataset_version_id}
            for p in sorted(patents, key=lambda p: (p.application_date, p.application_number), reverse=True)[:5]]
        snapshot = {"project_name": request.title, "analysis_id": identity, "scope": request.scope,
            "scope_version_id": None, "dataset_version_ids": sorted({p.dataset_version_id for p in patents if p.dataset_version_id}),
            "summary": draft["summary"] if draft else calculated["summary"], "views": draft["views"] if draft else {},
            "sources": versions, "charts": draft["charts"] if draft else charts.statistical_snapshots(calculated, identity),
            "evidence": draft["evidence"] if draft else [], "representatives": representatives}
        content = reports.build_report_content(snapshot)
        content["title"] = request.title
        if request.report_mode == "ai":
            def summarize():
                response = model_provider.chat([
                    {"role": "system", "content": read_prompt('report-summary')},
                    {"role": "user", "content": json.dumps({"summary": snapshot["summary"], "records": representatives}, ensure_ascii=False)}])
                return {"text": reports._response_text(response)}
            synthesis = ctx.step("capability_report_synthesis", summarize) if ctx else summarize()
            content["blocks"].insert(1, {"id": str(uuid5(NAMESPACE_URL, identity + ":ai")), "type": "paragraph",
                                         "text": synthesis["text"], "origin": "model"})
        return {"content": content}
    raise AppError(404, "CAPABILITY_NOT_FOUND", "能力不存在")


def _embed_records(patents, ctx=None):
    size = get_settings().embedding_batch_size
    result = []
    for offset in range(0, len(patents), size):
        batch = patents[offset:offset + size]
        if ctx:
            ctx.check_cancelled()
        call = lambda: {"vectors": model_provider.embed([f"{p.title}\n{p.abstract}" for p in batch])}
        result.extend((ctx.step(f"capability_embeddings_{offset}", call) if ctx else call())["vectors"])
    return result


def _save_embeddings(session, patents, vectors, source_ids):
    from sqlalchemy.dialects.postgresql import insert
    from app.models import new_id
    from patentcompass_core.retrieval import text_hash
    for patent, vector in zip(patents, vectors):
        if patent.dataset_version_id not in source_ids:
            raise AppError(409, "DATASET_SNAPSHOT_MISMATCH", "向量原文不属于提交时的数据版本")
        session.execute(insert(PatentEmbedding).values(id=new_id(), application_id=patent.id,
            model=get_settings().embedding_model, dimension=len(vector), text_sha256=text_hash(patent), embedding=vector)
            .on_conflict_do_nothing(index_elements=[PatentEmbedding.application_id, PatentEmbedding.model, PatentEmbedding.text_sha256]))


def execute_capability(ctx):
    """The ordinary durable executor hosts standalone work without project side effects."""
    name = ctx.payload["capability"]
    request = validate(name, ctx.payload["input"])
    if ctx.payload.get("model_fingerprint") and ctx.payload["model_fingerprint"] != _model_fingerprint(name, request):
        raise AppError(409, "MODEL_CONFIGURATION_CHANGED", "模型配置已变化，请按当前配置重新提交任务")

    def calculate():
        ctx.check_cancelled()
        with session_scope() as session:
            result = _execute(session, name, request, ctx)
            ctx.guard(session)
            return jsonable_encoder(result)

    return ctx.step(f"capability_{name}_v1", calculate)


def search_materials(session, project_id, query, limit=10):
    owned(session, Project, project_id)
    rows = session.scalars(select(MaterialBlock).join(Material).where(
        Material.project_id == project_id, Material.status == "ready", MaterialBlock.text.ilike(f"%{query}%"))).all()
    return {"matched_count": len(rows), "records": [{"id": row.id, "material_id": row.material_id,
        "text": row.text, "kind": row.kind, "location": row.location} for row in rows[:limit]]}


def read_analysis(session, analysis_id, view):
    analysis = owned(session, AnalysisResult, analysis_id)
    value = deepcopy(analysis.views.get(view, {}))
    value.pop("evidence_ids", None)
    value.pop("chart_ids", None)
    if isinstance(value.get("data"), dict):
        value["data"].pop("memberships", None)
        value["data"].pop("review_records", None)
    return {"analysis_id": analysis.id, "view": view, "result": value, "records": []}


def conversation_tool(session, name, filters, *, project_id, scope_version_id, dataset_version_ids, analysis_id=None):
    """Conversation tools supply frozen context to the same explicit read services."""
    if name == "read_analysis":
        if not analysis_id:
            return {"status": "unavailable", "reason": "此任务未绑定冻结分析", "records": []}
        return read_analysis(session, analysis_id, filters["view"])
    if name == "search_materials":
        return search_materials(session, project_id, filters["q"])
    from app import data_service
    project = owned(session, Project, project_id)
    query = {**{key: value for key, value in filters.items() if key != "queries"},
             "scope_version_id": scope_version_id, "dataset_version_ids": dataset_version_ids, "mode": "keywords"}
    if analysis_id:
        query["application_ids"] = owned(session, AnalysisResult, analysis_id).patent_ids
    records, counts = {}, []
    for search in filters.get("queries") or [filters.get("q", "")]:
        found = data_service.search_patents(session, project, {**query, "q": search})
        counts.append({"query": search, "matched_count": len(found["items"])})
        records.update({item.id: item for item in found["items"]})
    return {"matched_count": len(records), "query_counts": counts,
        "query_semantics": "单式空格AND，多式并集去重；零命中仅适用于实际检索式",
        "scope_application_count": found["scope_application_count"],
        "records": [{"id": item.id, "application_number": item.application_number, "title": item.title,
            "abstract": item.abstract, "source_sheet": item.source_sheet, "source_row": item.source_row,
            "dataset_version_id": item.dataset_version_id} for item in list(records.values())[:10]]}
