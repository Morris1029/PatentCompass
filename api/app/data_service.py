"""CNIPA spreadsheet ingestion, reproducible retrieval and statistical analysis."""

import hashlib
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import openpyxl
from sqlalchemy import Text, cast, exists, func, or_, select

from app.config import get_settings
from app.db import session_scope
from app.errors import AppError
from app.files import resolve_file
from app.model_provider import embed
from app.models import (AnalysisResult, Chart, DatasetVersion, Evidence, PatentApplication,
                        PatentEmbedding, PatentPublication, Project, ResearchRun, ScopeVersion, StoredFile, new_id)


HEADERS = {
    "申请号": "application_number", "公开（公告）号": "publication_numbers", "申请日": "application_date",
    "公开（公告）日": "publication_dates", "IPC分类号": "ipc_codes", "申请（专利权）人": "applicants",
    "发明人": "inventors", "发明名称": "title", "优先权号": "priority_numbers", "优先权日": "priority_dates",
    "文献类型": "document_type", "申请人所在国（省）": "countries", "摘要": "abstract",
}
REQUIRED = {"application_number", "publication_numbers", "application_date", "publication_dates", "title", "abstract"}
COUNTING_METHOD = {"unit": "application", "applicants": "full_count", "ipc": "multi_label_full_count",
                   "year": "application_date", "coverage": "imported_sample_only"}
KEYWORD_EXPANSIONS = {
    "无人机": ["unmanned aerial vehicle", "uav", "drone"],
    "自主": ["autonomous", "automatic"], "降落": ["landing", "land"],
    "起飞": ["takeoff", "take-off"], "避障": ["obstacle avoidance", "collision avoidance"],
    "导航": ["navigation", "navigating"], "电池": ["battery", "batteries"],
    "控制": ["control"], "通信": ["communication"], "路径规划": ["path planning", "route planning"],
}


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def split_values(value) -> list[str]:
    return [part.strip() for part in re.split(r"[;；\n\r]+", _text(value)) if part.strip()]


def _date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for pattern in ("%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"无效日期: {text}")


def _priorities(number_value, date_value) -> list[dict]:
    numbers = split_values(number_value)
    dates = [_date(value).isoformat() for value in split_values(date_value)]
    priorities = []
    for index, value in enumerate(numbers):
        # CNIPA exports a date per number inside the number cell, while the date
        # column can contain only distinct dates; positional zipping would lose rows.
        match = re.fullmatch(r"(.+?)\s+(\d{8})", value)
        embedded_date = _date(match.group(2)).isoformat() if match else None
        if embedded_date and dates and embedded_date not in dates:
            raise ValueError("优先权号内日期与优先权日期列不一致")
        if embedded_date:
            priority_date = embedded_date
        elif len(dates) == len(numbers):
            priority_date = dates[index]
        elif len(dates) == 1:
            priority_date = dates[0]
        elif dates:
            raise ValueError("无法确定优先权号与日期的对应关系")
        else:
            priority_date = None
        priorities.append({"number": match.group(1) if match else value, "date": priority_date, "raw": value})
    return priorities


def parse_workbook(path: str | Path) -> dict:
    """Parse all non-empty rows. Any invalid row makes the version fail, never silently disappear."""
    applications, errors, sheets, missing_fields = [], [], [], Counter()
    seen_applications, seen_publications = set(), set()
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise AppError(422, "INVALID_WORKBOOK", "无法读取 Excel 文件") from exc
    try:
        for sheet in workbook:
            iterator = sheet.iter_rows(values_only=True)
            header = [_text(item) for item in next(iterator, [])]
            if not any(header):
                continue
            mapped = {HEADERS[name]: index for index, name in enumerate(header) if name in HEADERS}
            missing = sorted(REQUIRED - mapped.keys())
            sheets.append({"name": sheet.title, "headers": header, "missing_columns": missing})
            if missing:
                errors.append({"sheet": sheet.title, "row": 1, "code": "MISSING_COLUMNS", "fields": missing})
            for row_number, row in enumerate(iterator, 2):
                if not any(_text(value) for value in row):
                    continue
                raw = {header[index] or f"column_{index + 1}":
                       value.isoformat() if isinstance(value, (date, datetime)) else value
                       for index, value in enumerate(row) if index < len(header)}
                values = {key: row[index] if index < len(row) else None for key, index in mapped.items()}
                for field in HEADERS.values():
                    if not _text(values.get(field)):
                        missing_fields[field] += 1
                try:
                    if missing:
                        raise ValueError("缺少必要列")
                    number = re.sub(r"\s+", "", _text(values["application_number"])).upper()
                    if not number or number in seen_applications:
                        raise ValueError("申请号为空或重复")
                    publications = split_values(values["publication_numbers"])
                    publication_dates = split_values(values["publication_dates"])
                    if not publications or len(publications) != len(publication_dates):
                        raise ValueError("公开编号与公开日期数量不一致")
                    publication_rows = []
                    local_publications = set()
                    for public_number, public_date in zip(publications, publication_dates):
                        public_number = re.sub(r"\s+", "", public_number).upper()
                        match = re.fullmatch(r"[A-Z]{2}\d+[A-Z]\d?", public_number)
                        if not match or public_number in seen_publications or public_number in local_publications:
                            raise ValueError("公开编号无效或重复")
                        local_publications.add(public_number)
                        kind = re.search(r"([A-Z]\d?)$", public_number).group(1)
                        publication_rows.append({"publication_number": public_number,
                                                 "publication_date": _date(public_date).isoformat(), "kind_code": kind})
                    if not _text(values["title"]) or not _text(values["abstract"]):
                        raise ValueError("标题或摘要为空")
                    priorities = _priorities(values.get("priority_numbers"), values.get("priority_dates"))
                    application = {"application_number": number, "application_date": _date(values["application_date"]).isoformat(),
                                   "title": _text(values["title"]), "abstract": _text(values["abstract"]),
                                   "applicants": split_values(values.get("applicants")), "inventors": split_values(values.get("inventors")),
                                   "ipc_codes": list(dict.fromkeys(split_values(values.get("ipc_codes")))),
                                   "countries": split_values(values.get("countries")), "priorities": priorities,
                                   "raw_record": raw, "source_sheet": sheet.title, "source_row": row_number,
                                   "publications": publication_rows}
                    applications.append(application)
                    seen_applications.add(number)
                    seen_publications.update(local_publications)
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append({"sheet": sheet.title, "row": row_number, "code": "INVALID_ROW", "message": str(exc), "raw_record": raw})
    finally:
        workbook.close()
    if not sheets:
        errors.append({"code": "EMPTY_WORKBOOK", "message": "工作簿中没有有效工作表"})
    if not applications and not errors:
        errors.append({"code": "NO_RECORDS", "message": "工作簿中没有专利记录"})
    quality = {"status": "failed" if errors else "ready", "application_count": len(applications),
               "publication_count": len(seen_publications), "abstract_count": sum(bool(a["abstract"]) for a in applications),
               "source_row_count": len(applications) + sum(e.get("code") == "INVALID_ROW" for e in errors),
               "missing_fields": dict(missing_fields), "errors": errors, "sheets": sheets,
               "warnings": ["当前数据为部分导出样本，统计不代表检索结果全集", "摘要为申请记录级文本，不为每个公开版本重复补造摘要",
                            "优先权号内嵌日期逐项保留，优先权日期列可能仅列出不重复日期；多申请人和多国家字段保留全部值"],
               "unavailable_fields": ["claims", "full_text", "legal_status", "family", "detailed_address"],
               "publication_prefixes": dict(Counter(pub["publication_number"][:2] for a in applications for pub in a["publications"]))}
    return {"applications": applications, "quality": quality}


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


def _keyword_groups(query: str) -> list[list[str]]:
    groups = []
    remainder = query.lower()
    for term, variants in sorted(KEYWORD_EXPANSIONS.items(), key=lambda pair: -len(pair[0])):
        if term in remainder:
            groups.append([term, *variants])
            remainder = remainder.replace(term, " ")
    groups.extend([[word] for word in remainder.split() if word])
    return groups


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
            groups = _keyword_groups(query)
            def keyword_score(patent):
                title, abstract = patent.title.lower(), patent.abstract.lower()
                return sum(max((3 * title.count(term) + min(abstract.count(term), 5) for term in variants), default=0)
                           for variants in groups)
            keyword_items = sorted((p for p in candidates if p.id in keyword_ids), key=lambda p: (-keyword_score(p), p.id))
            # Reciprocal-rank fusion avoids comparing keyword counts with cosine scores.
            fused = defaultdict(float)
            for ranked in (items, keyword_items):
                for rank, patent in enumerate(ranked[:candidate_limit], 1):
                    fused[patent.id] += 1 / (60 + rank)
            ordered = sorted(fused, key=lambda patent_id: (-fused[patent_id], patent_id))[:candidate_limit]
            items = [by_id[patent_id] for patent_id in ordered]
            scores = {patent_id: fused[patent_id] for patent_id in ordered}
    else:
        items = list(session.scalars(statement.order_by(PatentApplication.application_date.desc(), PatentApplication.id)))
    return {"items": items, "scores": scores, "mode": mode, "candidate_count": len(items), "scope_application_count": total}


def _text_hash(patent) -> str:
    return hashlib.sha256(f"{patent.title}\n{patent.abstract}".encode()).hexdigest()


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
    labels = [str(row["label"]) for row in rows]
    values = [row["count"] for row in rows]
    horizontal = kind != "yearly"
    return {"animation": False, "backgroundColor": "#ffffff", "title": {"text": title, "left": "center"},
            "tooltip": {"trigger": "axis"}, "grid": {"left": "5%", "right": "7%", "bottom": "8%", "top": "15%", "containLabel": True},
            "xAxis": {"type": "value"} if horizontal else {"type": "category", "data": labels},
            "yAxis": {"type": "category", "data": labels, "inverse": True,
                      "axisLabel": {"width": 260, "overflow": "truncate"}} if horizontal else {"type": "value", "minInterval": 1},
            "series": [{"type": "bar", "data": values, "label": {"show": True, "position": "right" if horizontal else "top"},
                        "itemStyle": {"color": "#236b8e"}}]}


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
            publication_map = defaultdict(list)
            for publication in publications:
                publication_map[publication.application_id].append(publication.publication_number)
            years, applicants, ipc, countries = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
            for patent in patents:
                years[str(patent.application_date.year)].append(patent.id)
                for name in dict.fromkeys(patent.applicants):
                    applicants[name].append(patent.id)
                for code in dict.fromkeys(patent.ipc_codes):
                    ipc[code].append(patent.id)
                for country in dict.fromkeys(patent.countries):
                    countries[country].append(patent.id)
            yearly = [{"year": int(year), "count": len(ids)} for year, ids in sorted(years.items())]
            applicant_rows = [{"name": name, "count": len(ids)} for name, ids in sorted(applicants.items(), key=lambda pair: (-len(pair[1]), pair[0]))]
            ipc_rows = [{"code": code, "count": len(ids)} for code, ids in sorted(ipc.items(), key=lambda pair: (-len(pair[1]), pair[0]))]
            country_rows = [{"country": country, "count": len(ids)} for country, ids in
                            sorted(countries.items(), key=lambda pair: (-len(pair[1]), pair[0]))]
            versions = list(session.scalars(select(DatasetVersion).where(DatasetVersion.id.in_(ctx.dataset_version_ids))))
            summary = {"application_count": len(patents), "publication_count": len(publications),
                       "abstract_count": sum(bool(p.abstract) for p in patents), "yearly": yearly, "applicants": applicant_rows,
                       "ipc": ipc_rows, "country": country_rows, "quality": versions[0].quality if versions else {},
                       "source_metadata": versions[0].source_metadata if versions else {}, "counting_method": COUNTING_METHOD}
            representative = [{"application_id": p.id, "application_number": p.application_number,
                               "publication_numbers": sorted(publication_map[p.id]), "title": p.title, "abstract": p.abstract,
                               "source_sheet": p.source_sheet, "source_row": p.source_row, "dataset_version_id": p.dataset_version_id}
                              for p in patents[:10]]
            return {"summary": summary, "patent_ids": ids, "selections": {"yearly": dict(years), "applicants": dict(applicants), "ipc": dict(ipc), "country": dict(countries)},
                    "representative": representative}

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
