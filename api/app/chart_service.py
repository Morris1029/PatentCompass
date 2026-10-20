"""Immutable, independently adopted chart history and snapshot-based revisions."""

import copy
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import model_provider
from app.data_service import _chart_option, search_patents
from app.db import session_scope
from app.errors import AppError
from app.models import AnalysisResult, Chart, ChartVersion, PatentApplication, Project, ResearchRun, new_id


def data_hash(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def ensure_versions(session, charts):
    """Keep existing public version identifiers when first materializing history."""
    for chart in charts:
        session.flush()
        if not session.get(ChartVersion, chart.version_id):
            session.execute(insert(ChartVersion).values(id=chart.version_id, chart_id=chart.id, number=1,
                                     option=copy.deepcopy(chart.option), data=copy.deepcopy(chart.data),
                                     provenance=copy.deepcopy(chart.provenance), selections=copy.deepcopy(chart.selections),
                                     data_sha256=data_hash(chart.data), interpretation={})
                            .on_conflict_do_nothing(index_elements=[ChartVersion.id]))
    session.flush()


def snapshot(session, chart, version_id=None):
    ensure_versions(session, [chart])
    version = session.get(ChartVersion, version_id or chart.version_id)
    if not version or version.chart_id != chart.id:
        raise AppError(404, "CHART_VERSION_NOT_FOUND", "图表版本不存在")
    return {"id": chart.id, "chart_id": chart.id, "version_id": version.id,
            "chart_version_id": version.id, "number": version.number, "kind": chart.kind,
            "analysis_id": chart.analysis_id, "current_version_id": chart.version_id,
            "created_at": version.created_at.isoformat(),
            **{key: copy.deepcopy(getattr(version, key)) for key in
               ("option", "data", "provenance", "selections", "data_sha256", "interpretation")}}


def adopt(session, chart, version_id, expected_current_version_id):
    chart = session.scalar(select(Chart).where(Chart.id == chart.id).with_for_update()
                           .execution_options(populate_existing=True))
    ensure_versions(session, [chart])
    if chart.version_id != expected_current_version_id:
        raise AppError(409, "CHART_VERSION_CONFLICT", "图表采用版本已变化，请保留输入并查看最新版本",
                       {"current_version_id": chart.version_id})
    version = session.get(ChartVersion, version_id)
    if not version or version.chart_id != chart.id:
        raise AppError(404, "CHART_VERSION_NOT_FOUND", "图表版本不存在")
    chart.version_id = version.id
    for key in ("option", "data", "provenance", "selections"):
        setattr(chart, key, copy.deepcopy(getattr(version, key)))
    return snapshot(session, chart)


STYLE_ROOTS = {"title", "legend", "grid", "color", "backgroundColor", "tooltip", "xAxis", "yAxis", "series", "visualMap"}
STYLE_KEYS = {"textStyle", "left", "right", "top", "bottom", "show", "color", "fontSize", "fontWeight",
              "fontFamily", "position", "rotate", "width", "height", "overflow", "align", "verticalAlign",
              "orient", "containLabel", "itemStyle", "label", "lineStyle", "areaStyle", "borderRadius",
              "borderWidth", "borderColor", "opacity", "type", "smooth", "symbol", "symbolSize", "inverse",
              "axisLabel", "axisLine", "axisTick", "splitLine", "trigger", "text", "subtext", "barWidth",
              "min", "max", "calculable", "inRange", "outOfRange", "splitNumber"}


def _style_patch(option, patch):
    if not isinstance(patch, dict) or set(patch) - STYLE_ROOTS:
        raise AppError(422, "INVALID_CHART_STYLE", "图表样式只能修改支持的呈现属性")

    def merge(old, change, path):
        if isinstance(change, dict):
            if set(change) - STYLE_KEYS:
                raise AppError(422, "INVALID_CHART_STYLE", "图表样式不能修改数据、映射或执行脚本")
            result = copy.deepcopy(old) if isinstance(old, dict) else {}
            for key, value in change.items():
                if key == "type" and path and path[0] == "series":
                    allowed = {"heatmap"} if isinstance(old, dict) and old.get("type") == "heatmap" else {"bar", "line", "scatter"}
                    if value not in allowed:
                        raise AppError(422, "INVALID_CHART_STYLE", "不支持该图表类型")
                result[key] = merge(result.get(key), value, path + [key])
            return result
        if isinstance(change, list):
            if path and path[-1] == "color" and all(isinstance(item, str) for item in change):
                return change
            if not isinstance(old, list) or len(change) > len(old):
                raise AppError(422, "INVALID_CHART_STYLE", "样式修改不能增加数据系列")
            return [merge(item, change[index], path) if index < len(change) else copy.deepcopy(item)
                    for index, item in enumerate(old)]
        if not isinstance(change, (str, int, float, bool, type(None))):
            raise AppError(422, "INVALID_CHART_STYLE", "图表样式值无效")
        return change

    result = copy.deepcopy(option)
    for key, value in patch.items():
        result[key] = merge(result.get(key), value, [key])
    return result


DATA_FILTERS = {"q", "applicant", "ipc", "application_number", "publication_number", "application_start",
                "application_end", "start", "end", "theme_ids"}


class DataFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    q: str | None = Field(default=None, max_length=2000)
    applicant: str | None = Field(default=None, max_length=1000)
    ipc: str | None = Field(default=None, max_length=100)
    application_number: str | None = Field(default=None, max_length=100)
    publication_number: str | None = Field(default=None, max_length=100)
    application_start: date | None = None
    application_end: date | None = None
    start: int | None = Field(default=None, ge=1800, le=2200)
    end: int | None = Field(default=None, ge=1800, le=2200)
    theme_ids: list[str] | None = Field(default=None, max_length=100)


def heatmap_snapshot(dimension, title, patents, assignments, theme_definitions):
    """Compute joint cells and exact drilldown sets, never cross marginal aggregates."""
    if dimension not in {"annual_theme", "subject_matrix"}:
        raise AppError(422, "UNKNOWN_HEATMAP", "不支持的联合统计")
    cells, x_counts = defaultdict(list), Counter()
    labels = {theme["id"]: theme["label"] for theme in theme_definitions}
    for patent in patents:
        x_values = [str(patent.application_date.year)] if dimension == "annual_theme" else list(dict.fromkeys(patent.applicants))
        methods = list(dict.fromkeys(assignments[patent.id]["assignment"]["methods"]))
        for x_value in x_values:
            x_counts[x_value] += 1
            for theme_id in methods:
                cells[(x_value, theme_id)].append(patent.id)
    all_x = sorted(x_counts) if dimension == "annual_theme" else sorted(x_counts, key=lambda value: (-x_counts[value], value))
    shown_x = all_x if dimension == "annual_theme" else all_x[:20]
    y_counts = Counter()
    for (_, theme_id), ids in cells.items():
        y_counts[theme_id] += len(ids)
    y_values = sorted(labels, key=lambda theme_id: (-y_counts[theme_id], labels[theme_id]))
    x_indices, y_indices = {value: index for index, value in enumerate(shown_x)}, {value: index for index, value in enumerate(y_values)}
    all_rows, selections, series = [], {}, []
    for (x_value, theme_id), ids in sorted(cells.items()):
        key = dimension + "-" + data_hash([x_value, theme_id])[:20]
        row = {"value": key, "label": f"{x_value} / {labels[theme_id]}", "theme_id": theme_id, "count": len(ids),
               **({"year": int(x_value)} if dimension == "annual_theme" else {"applicant": x_value})}
        all_rows.append(row)
        selections[key] = ids
        if x_value in x_indices:
            series.append({"name": key, "value": [x_indices[x_value], y_indices[theme_id], len(ids)]})
    rows = [row for row in all_rows if (str(row["year"]) if dimension == "annual_theme" else row["applicant"]) in x_indices]
    option = {"animation": False, "backgroundColor": "#ffffff", "title": {"text": title, "left": "center", "textStyle": {"fontSize": 16}},
              "tooltip": {"position": "top"}, "grid": {"top": 60, "left": "3%", "right": "5%", "bottom": 95, "containLabel": True},
              "xAxis": {"type": "category", "data": shown_x, "axisLabel": {"rotate": 35, "width": 120, "overflow": "truncate"}},
              "yAxis": {"type": "category", "data": [labels[theme_id] for theme_id in y_values], "axisLabel": {"width": 140, "overflow": "truncate"}},
              "visualMap": {"min": 0, "max": max((row["count"] for row in rows), default=1), "calculable": True,
                            "orient": "horizontal", "left": "center", "bottom": 5, "inRange": {"color": ["#edf7f5", "#6eb7ac", "#0d7376"]}},
              "series": [{"type": "heatmap", "data": series, "label": {"show": len(rows) < 120}, "itemStyle": {"borderColor": "#ffffff", "borderWidth": 1}}]}
    return {"option": option, "data": {"title": title, "dimension": dimension, "rows": rows, "all_rows": all_rows,
            "x_labels": [{"value": value, "label": value} for value in shown_x],
            "y_labels": [{"value": theme_id, "label": labels[theme_id]} for theme_id in y_values],
            "display_limit": None if dimension == "annual_theme" else 20, "unit": "件申请"},
            "selections": {"dimension": dimension, "values": selections}}


def _recount(session, chart, base, filters):
    if not isinstance(filters, dict) or set(filters) - DATA_FILTERS:
        raise AppError(422, "INVALID_CHART_FILTER", "图表筛选包含不支持的条件")
    try:
        filters = DataFilters.model_validate(filters).model_dump(mode="json", exclude_none=True)
        if ((filters.get("start") and filters.get("end") and filters["start"] > filters["end"]) or
                (filters.get("application_start") and filters.get("application_end") and filters["application_start"] > filters["application_end"])):
            raise ValueError("Date order")
    except (ValidationError, ValueError) as exc:
        raise AppError(422, "INVALID_CHART_FILTER", "图表筛选的日期、年份或主题格式无效") from exc
    project = session.get(Project, chart.project_id)
    provenance = base["provenance"]
    original_ids = provenance.get("filter_base_application_ids", provenance.get("application_ids", []))
    analysis = session.get(AnalysisResult, chart.analysis_id)
    if filters.get("theme_ids"):
        from app.models import SemanticMembership
        requested = set(filters["theme_ids"])
        known = {theme["id"] for items in analysis.views.get("technology", {}).get("data", {}).get("taxonomy", {}).values()
                 for theme in items}
        if requested - known:
            raise AppError(422, "INVALID_THEME", "主题不属于图表原分析版本")
        memberships = session.scalars(select(SemanticMembership).where(SemanticMembership.analysis_id == analysis.id))
        original_ids = [row.application_id for row in memberships if row.application_id in original_ids and requested.intersection(
            theme for dimension in ("elements", "methods", "scenarios") for theme in row.assignment.get(dimension, []))]
    result = search_patents(session, project, {**{key: value for key, value in filters.items() if key != "theme_ids"},
                                             "scope_version_id": provenance.get("scope_version_id", analysis.scope_version_id),
                                             "dataset_version_ids": provenance.get("dataset_version_ids", analysis.dataset_version_ids),
                                             "application_ids": original_ids})
    patents = result["items"]
    by_value = defaultdict(list)
    dimension = base["data"]["dimension"]
    if dimension in {"annual_theme", "subject_matrix"}:
        from app.models import SemanticMembership
        assignments = {row.application_id: {"assignment": row.assignment} for row in session.scalars(select(SemanticMembership).where(
            SemanticMembership.analysis_id == analysis.id, SemanticMembership.application_id.in_([p.id for p in patents])))}
        computed = heatmap_snapshot(dimension, base["data"]["title"], patents, assignments,
                                   analysis.views["technology"]["data"]["taxonomy"]["methods"])
        return {**computed, "provenance": {**provenance, "application_ids": [p.id for p in patents], "filters": filters,
                "filter_base_application_ids": provenance.get("filter_base_application_ids", provenance.get("application_ids", []))}}
    if dimension in {"year", "applicant", "ipc", "country"}:
        for patent in patents:
            values = {"year": [str(patent.application_date.year)], "applicant": patent.applicants,
                      "ipc": patent.ipc_codes, "country": patent.countries}[dimension]
            for value in dict.fromkeys(values):
                by_value[str(value)].append(patent.id)
        names = {}
    elif dimension in {"elements", "methods", "scenarios"}:
        from app.models import SemanticMembership
        names = {theme["id"]: theme["label"] for theme in
                 analysis.views.get("technology", {}).get("data", {}).get("taxonomy", {}).get(dimension, [])}
        for row in session.scalars(select(SemanticMembership).where(SemanticMembership.analysis_id == analysis.id,
                                                                  SemanticMembership.application_id.in_([p.id for p in patents]))):
            for theme_id in dict.fromkeys(row.assignment.get(dimension, [])):
                by_value[theme_id].append(row.application_id)
    else:
        raise AppError(422, "UNSUPPORTED_CHART_DATA_EDIT", "该图表暂不支持局部数据筛选")
    ordered = sorted(by_value, key=lambda value: value if dimension == "year" else (-len(by_value[value]), value))
    rows = [{"value": value, "label": names.get(value, value), "count": len(by_value[value])} for value in ordered]
    displayed = rows if dimension == "year" else rows[:15]
    data = {**base["data"], "rows": displayed, "all_rows": rows}
    updated_provenance = {**provenance, "application_ids": [p.id for p in patents], "filters": filters,
                          "filter_base_application_ids": provenance.get("filter_base_application_ids", provenance.get("application_ids", []))}
    return {"data": data, "option": _chart_option(data["title"], displayed, "yearly" if dimension == "year" else chart.kind),
            "provenance": updated_provenance, "selections": {"dimension": dimension, "values": dict(by_value)}}


def revise_chart(ctx):
    payload = ctx.payload
    mode = payload.get("mode")
    if mode not in {"style", "data", "insight"}:
        raise AppError(422, "INVALID_CHART_MODE", "请选择样式、数据或解读修改")
    with session_scope() as session:
        chart = session.get(Chart, payload["chart_id"])
        if not chart or chart.project_id != ctx.project_id:
            raise AppError(404, "CHART_NOT_FOUND", "图表不存在")
        run = session.get(ResearchRun, ctx.run_id)
        completed_ids = [ctx.run_id] + ([run.retry_of_id] if run and run.retry_of_id else [])
        existing = session.scalar(select(ChartVersion).where(ChartVersion.chart_id == chart.id,
                                  ChartVersion.provenance["revision_run_id"].astext.in_(completed_ids)))
        if existing:
            return {"chart_id": chart.id, "chart_version_id": existing.id, "number": existing.number,
                    "current_version_id": chart.version_id, "mode": mode}
        base = snapshot(session, chart, payload["base_version_id"])
        if chart.version_id != payload["expected_current_version_id"]:
            raise AppError(409, "CHART_VERSION_CONFLICT", "图表采用版本已变化", {"current_version_id": chart.version_id})

    def calculate():
        result = {key: copy.deepcopy(base[key]) for key in ("option", "data", "provenance", "selections", "interpretation")}
        if mode == "style":
            result["option"] = _style_patch(base["option"], payload.get("option", {}))
        elif mode == "data":
            with session_scope() as session:
                result.update(_recount(session, session.get(Chart, chart.id), base, payload.get("filters", {})))
            result["interpretation"] = {}
        else:
            with session_scope() as session:
                patents = list(session.scalars(select(PatentApplication).where(
                    PatentApplication.id.in_(base["provenance"].get("application_ids", [])))
                    .order_by(PatentApplication.id).limit(8)))
                evidence = [{"application_id": p.id, "title": p.title, "abstract": p.abstract} for p in patents]
            message = model_provider.chat([
                {"role": "system", "content": "你负责解释冻结版本的专利样本图表。仅根据所给程序统计与原文说明，不能推导未提供的申请人×IPC关系、技术空白或法律结论。区分样本与全集，明确限制。证据不足时明确说明。"},
                {"role": "user", "content": json.dumps({"instruction": payload.get("instruction", "解释这张图表"),
                 "chart_data": base["data"], "evidence": evidence}, ensure_ascii=False)}])
            text = message.get("content")
            if not isinstance(text, str) or not text.strip() or len(text) > 40000:
                raise AppError(502, "INVALID_MODEL_RESPONSE", "模型没有返回有效的图表解读文字")
            result["interpretation"] = {"text": text.strip(), "base_version_id": base["version_id"],
                                         "evidence_application_ids": [p.id for p in patents],
                                         "data_sha256": base["data_sha256"]}
        return result

    computed = ctx.step(f"chart_{mode}_{base['version_id']}", calculate)

    def persist():
        with session_scope() as session:
            chart = session.scalar(select(Chart).where(Chart.id == payload["chart_id"]).with_for_update()
                                   .execution_options(populate_existing=True))
            existing = session.scalar(select(ChartVersion).where(ChartVersion.chart_id == chart.id,
                                      ChartVersion.provenance["revision_run_id"].astext == ctx.run_id))
            if existing:
                return {"chart_id": chart.id, "chart_version_id": existing.id, "number": existing.number,
                        "current_version_id": chart.version_id, "mode": mode}
            if chart.version_id != payload["expected_current_version_id"]:
                raise AppError(409, "CHART_VERSION_CONFLICT", "图表采用版本已变化，请重新确认",
                               {"current_version_id": chart.version_id})
            number = session.scalar(select(func.max(ChartVersion.number)).where(ChartVersion.chart_id == chart.id)) + 1
            computed["provenance"] = {**computed["provenance"], "revision_run_id": ctx.run_id}
            version = ChartVersion(id=new_id(), chart_id=chart.id, number=number, **computed,
                                   data_sha256=data_hash(computed["data"]))
            if mode != "data" and version.data_sha256 != base["data_sha256"]:
                raise AppError(409, "CHART_DATA_CHANGED", "呈现或解读修改不能改变图表数据")
            session.add(version)
            session.flush()
            adopted = adopt(session, chart, version.id, payload["expected_current_version_id"])
            ctx.guard(session)
            return {"chart_id": chart.id, "chart_version_id": version.id, "number": number,
                    "current_version_id": adopted["version_id"], "mode": mode}

    result = ctx.step("persist_chart_revision", persist)
    ctx.emit("chart.ready", result)
    return result
