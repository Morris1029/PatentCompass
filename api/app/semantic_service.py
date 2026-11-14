"""Full-population, evidence-checked semantic classification with reusable checkpoints."""

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed

from patentcompass_core.semantics import (
    BATCH_SIZE, DIMENSIONS, DIMENSION_LABELS, PROMPT_VERSION, REVIEW_LABELS, SCHEMA_VERSION,
    TAXONOMY_PROMPT_VERSION, _digest, analysis_draft, assemble_taxonomy, assign_themes as _assign,
    classification_source,
    candidate_directions as _directions, decode_response as _json_message, extract_records,
    group_dimension, taxonomy_labels, validate_extraction as _valid_extraction,
)
from patentcompass_core.statistics import compute_statistics

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app import model_provider
from app.chart_service import ensure_versions
from app.config import get_settings
from app.data_service import _analysis_result, _text_hash, _update_analysis_context, search_patents
from app.db import session_scope
from app.errors import AppError
from app.models import (AnalysisResult, Chart, DatasetVersion, Evidence, PatentApplication, PatentPublication,
                        Project, ResearchRun, SemanticCache, SemanticMembership, new_id)


def _cache_key(patent, model):
    return _digest({"text_sha256": _text_hash(patent), "model": model,
                    "model_options": getattr(get_settings(), "llm_extra_body", {}),
                    "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION})


def _extract_batch(ctx, patents, model):
    keys = {patent.id: _cache_key(patent, model) for patent in patents}
    with session_scope() as session:
        cached = {row.cache_key: row.extraction for row in session.scalars(select(SemanticCache).where(
            SemanticCache.cache_key.in_(keys.values())))}
    pending_by_key = {}
    for patent in patents:
        if keys[patent.id] not in cached:
            pending_by_key.setdefault(keys[patent.id], patent)
    pending = list(pending_by_key.values())
    if pending:
        by_id = extract_records(pending, model_provider.chat)
        with session_scope() as session:
            for patent in pending:
                extraction = _valid_extraction(patent, by_id[patent.id])
                key = keys[patent.id]
                saved = session.scalar(insert(SemanticCache).values(
                    id=new_id(), cache_key=key, text_sha256=_text_hash(patent), model=model,
                    prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION, extraction=extraction
                ).on_conflict_do_nothing(index_elements=[SemanticCache.cache_key]).returning(SemanticCache.extraction))
                cached[key] = saved if saved is not None else session.scalar(select(SemanticCache.extraction).where(SemanticCache.cache_key == key))
            ctx.guard(session)
    return {"records": {patent.id: {**cached[keys[patent.id]], "cache_key": keys[patent.id]} for patent in patents},
            "cached_count": len(patents) - len(pending)}


def _taxonomy(ctx, extractions):
    labels = taxonomy_labels(extractions)
    if not any(labels.values()):
        return {"version": _digest(labels), "themes": {dimension: [] for dimension in DIMENSIONS}, "mapping": {}}
    model = get_settings().llm_model
    labels_hash = _digest(labels)
    cache_key = _digest({"labels_hash": labels_hash, "model": model,
                        "model_options": getattr(get_settings(), "llm_extra_body", {}),
                        "prompt_version": TAXONOMY_PROMPT_VERSION, "schema_version": SCHEMA_VERSION})
    with session_scope() as session:
        cached = session.scalar(select(SemanticCache).where(SemanticCache.cache_key == cache_key))
        if cached:
            return cached.extraction
    dimension_keys = {dimension: _digest({"labels": values, "model": model, "dimension": dimension,
        "model_options": getattr(get_settings(), "llm_extra_body", {}), "prompt_version": TAXONOMY_PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION}) for dimension, values in labels.items()}
    with session_scope() as session:
        dimension_cache = {row.cache_key: row.extraction for row in session.scalars(select(SemanticCache).where(
            SemanticCache.cache_key.in_(dimension_keys.values())))}
    results = {dimension: dimension_cache[key] for dimension, key in dimension_keys.items() if key in dimension_cache}

    def request(dimension):
        return group_dimension(dimension, labels, model_provider.chat)

    errors = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(request, dimension): dimension for dimension in DIMENSIONS if dimension not in results}
        for future in as_completed(futures):
            dimension = futures[future]
            try:
                result = future.result()
            except AppError as exc:
                errors.append(exc)
                continue
            # Each successful dimension survives failures in the other dimensions. Writes stay on the caller thread.
            with session_scope() as session:
                key = dimension_keys[dimension]
                saved = session.scalar(insert(SemanticCache).values(id=new_id(), cache_key=key, text_sha256=_digest(labels[dimension]),
                        model=model, prompt_version=f"{TAXONOMY_PROMPT_VERSION}-{dimension}", schema_version=SCHEMA_VERSION,
                        extraction=result).on_conflict_do_nothing(index_elements=[SemanticCache.cache_key]).returning(SemanticCache.extraction))
                results[dimension] = saved if saved is not None else session.scalar(select(SemanticCache.extraction).where(SemanticCache.cache_key == key))
                ctx.guard(session)
            ctx.emit("analysis.progress", {"stage": "taxonomy", "completed_dimensions": len(results), "total_dimensions": 3,
                                           "dimension": dimension})
    if errors:
        raise errors[0]
    taxonomy = assemble_taxonomy(labels, results)
    with session_scope() as session:
        saved = session.scalar(insert(SemanticCache).values(id=new_id(), cache_key=cache_key, text_sha256=labels_hash,
                        model=model, prompt_version=TAXONOMY_PROMPT_VERSION, schema_version=SCHEMA_VERSION, extraction=taxonomy)
                        .on_conflict_do_nothing(index_elements=[SemanticCache.cache_key]).returning(SemanticCache.extraction))
        taxonomy = saved if saved is not None else session.scalar(select(SemanticCache.extraction).where(SemanticCache.cache_key == cache_key))
        ctx.guard(session)
    return taxonomy


def _statistics(patents, publications, versions):
    result = compute_statistics(patents, publications, versions)
    return result["summary"], result["selections"]


def _persist(ctx, patents, assignments, taxonomy, cached_count, revision_of_id=None):
    with session_scope() as session:
        run = session.get(ResearchRun, ctx.run_id)
        run_ids = [ctx.run_id] + ([run.retry_of_id] if run and run.retry_of_id else [])
        existing = session.scalar(select(AnalysisResult).where(AnalysisResult.run_id.in_(run_ids)))
        if existing:
            return {**_analysis_result(session, existing), **existing.summary.get("semantic", {})}
        publications = list(session.scalars(select(PatentPublication).where(PatentPublication.application_id.in_([p.id for p in patents]))))
        versions = list(session.scalars(select(DatasetVersion).where(DatasetVersion.id.in_(ctx.dataset_version_ids))))
        base = session.get(AnalysisResult, revision_of_id) if revision_of_id else None
        source_model = base.summary.get("semantic", {}).get("model", "") if base else get_settings().llm_model
        classification = {"source": classification_source(patents), "assignments": assignments, "taxonomy": taxonomy,
                          "coverage": {"cached_count": cached_count, "model": source_model}}
        draft = analysis_draft(patents, classification, versions, publications)
        coverage = draft["summary"]["semantic"]
        analysis = AnalysisResult(id=new_id(), project_id=ctx.project_id, run_id=ctx.run_id, scope_version_id=ctx.scope_version_id,
                                  dataset_version_ids=ctx.dataset_version_ids, patent_ids=draft["patent_ids"],
                                  summary=draft["summary"], revision_of_id=revision_of_id)
        session.add(analysis)
        session.flush()
        for application_id, row in assignments.items():
            session.add(SemanticMembership(analysis_id=analysis.id, application_id=application_id,
                                          taxonomy_version=taxonomy["version"], **row))
        charts, identifiers = [], {}
        for content in draft["charts"]:
            chart = Chart(id=new_id(), project_id=ctx.project_id, analysis_id=analysis.id, kind=content["kind"],
                          option=content["option"], data=content["data"], selections=content["selections"],
                          provenance={**content["provenance"], "scope_version_id": ctx.scope_version_id,
                                      "dataset_version_ids": ctx.dataset_version_ids})
            session.add(chart)
            charts.append(chart)
            identifiers[content["id"]] = chart.id
        ensure_versions(session, charts)
        for content in draft["evidence"]:
            evidence = Evidence(id=new_id(), project_id=ctx.project_id, analysis_id=analysis.id,
                                application_id=content["application_id"], kind=content["kind"], content=content["content"])
            session.add(evidence)
            identifiers[content["id"]] = evidence.id
        analysis.views = copy.deepcopy(draft["views"])
        for view in analysis.views.values():
            view["chart_ids"] = [identifiers[value] for value in view["chart_ids"]]
            view["evidence_ids"] = [identifiers[value] for value in view["evidence_ids"]]
        _update_analysis_context(session, ctx, analysis,
                                 ctx.payload["expected_current_analysis_id"] if revision_of_id else None)
        ctx.guard(session)
        session.flush()
        return {**_analysis_result(session, analysis), **coverage}


def analyze(ctx):
    settings = get_settings()
    if not settings.llm_enabled:
        raise AppError(503, "MODEL_NOT_CONFIGURED", "全量语义分析需要配置对话模型，可先运行统计分析")
    with session_scope() as session:
        project = session.get(Project, ctx.project_id)
        patents = search_patents(session, project, {"scope_version_id": ctx.scope_version_id,
                                                  "dataset_version_ids": ctx.dataset_version_ids})["items"]
    if not patents:
        raise AppError(422, "NO_SELECTED_PATENTS", "当前范围没有可分析的申请，请调整范围")
    extractions, cached_count = {}, 0
    for offset in range(0, len(patents), BATCH_SIZE):
        batch = patents[offset:offset + BATCH_SIZE]
        batch_fingerprint = _digest([_cache_key(patent, settings.llm_model) for patent in batch])[:16]
        ctx.check_cancelled()
        result = ctx.step(f"semantic_extract_{offset}_{batch_fingerprint}", lambda: _extract_batch(ctx, batch, settings.llm_model))
        extractions.update(result["records"])
        cached_count += result["cached_count"]
        ctx.emit("analysis.progress", {"processed_count": len(extractions), "total_count": len(patents),
                                       "cached_count": cached_count, "stage": "extract"})
    taxonomy = ctx.step("semantic_taxonomy_" + TAXONOMY_PROMPT_VERSION + "_" + _digest(extractions)[:16], lambda: _taxonomy(ctx, extractions))
    assignments = _assign(extractions, taxonomy)
    result = ctx.step("persist_semantic_analysis", lambda: _persist(ctx, patents, assignments, taxonomy, cached_count))
    ctx.emit("analysis.ready", result)
    return result


def revise_analysis(ctx):
    with session_scope() as session:
        base = session.get(AnalysisResult, ctx.payload["base_analysis_id"])
        if not base or base.project_id != ctx.project_id:
            raise AppError(404, "ANALYSIS_NOT_FOUND", "分析不存在")
        technology = base.views.get("technology", {}).get("data") or {}
        if technology.get("classification") != "semantic":
            raise AppError(409, "SEMANTIC_ANALYSIS_REQUIRED", "只有完成语义分类的分析才能纠正归属")
        themes = copy.deepcopy(technology["taxonomy"])
        taxonomy = {"version": technology["taxonomy_version"], "themes": themes, "mapping": {},
                    "unmapped_labels": copy.deepcopy(technology.get("unmapped_labels", {}))}
        memberships = list(session.scalars(select(SemanticMembership).where(SemanticMembership.analysis_id == base.id)))
        assignments = {row.application_id: {"status": row.status, "assignment": copy.deepcopy(row.assignment)} for row in memberships}
        patents = list(session.scalars(select(PatentApplication).where(PatentApplication.id.in_(base.patent_ids))
                                       .order_by(PatentApplication.application_date.desc(), PatentApplication.id)))
    # A correction pins its original data and taxonomy. It cannot silently incorporate a new project scope.
    ctx.scope_version_id, ctx.dataset_version_ids = base.scope_version_id, list(base.dataset_version_ids)
    for correction in ctx.payload["corrections"]:
        application_id, dimension = correction["application_id"], correction["dimension"]
        if application_id not in assignments or dimension not in DIMENSIONS:
            raise AppError(422, "INVALID_CLASSIFICATION_CORRECTION", "纠正目标不属于指定分析")
        known = {theme["id"] for theme in themes[dimension]}
        if set(correction["theme_ids"]) - known:
            raise AppError(422, "INVALID_THEME", "纠正主题不属于该分析的分类体系")
        row = assignments[application_id]
        row["assignment"][dimension] = list(dict.fromkeys(correction["theme_ids"]))
        row["assignment"]["review_reasons"] = [reason for reason in row["assignment"]["review_reasons"]
                                                    if not reason.startswith(dimension + ":")]
        if not correction["theme_ids"]:
            row["assignment"]["review_reasons"].append(dimension + ":user_unclassified")
        row["assignment"]["evidence"] = [evidence for evidence in row["assignment"]["evidence"] if evidence["dimension"] != dimension]
        row["assignment"].setdefault("corrections", []).append({"dimension": dimension, "theme_ids": correction["theme_ids"],
                                                                "source": "user", "base_analysis_id": base.id})
        row["status"] = "review" if row["assignment"]["review_reasons"] else "classified"
    result = ctx.step("persist_analysis_correction", lambda: _persist(ctx, patents, assignments, taxonomy,
                     base.summary.get("semantic", {}).get("cached_count", 0), revision_of_id=base.id))
    ctx.emit("analysis.ready", result)
    return result
