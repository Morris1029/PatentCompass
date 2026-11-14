"""Chart options and joint counts over explicit records and assignments."""

import copy
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from uuid import NAMESPACE_URL, uuid5

from .contracts import CapabilityError


def data_hash(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_chart_options(input: dict) -> dict:
    """Build a categorical bar chart from explicit title, rows and kind."""
    title, rows, kind = input["title"], input["rows"], input["kind"]
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


def statistical_snapshots(calculated, analysis_id):
    """Build the three report charts from the same counted rows, without storage."""
    result = []
    for kind, title, label in (("yearly", "年度申请分布", "year"),
                               ("applicants", "主要申请人", "name"), ("ipc", "IPC 技术分类", "code")):
        all_rows = [{"label": str(row[label]), "count": row["count"]} for row in calculated["summary"][kind]]
        rows = all_rows if kind == "yearly" else all_rows[:15]
        chart_id = str(uuid5(NAMESPACE_URL, f"patentcompass:{analysis_id}:{kind}"))
        result.append({"id": chart_id, "version_id": str(uuid5(NAMESPACE_URL, chart_id + ":1")), "kind": kind,
            "option": build_chart_options({"title": title, "rows": rows, "kind": kind}),
            "data": {"title": title, "rows": rows, "all_rows": all_rows, "unit": "件申请"},
            "provenance": {"application_ids": list(calculated["patent_ids"]),
                           "counting_method": calculated["summary"]["counting_method"]},
            "selections": {"dimension": kind, "values": calculated["selections"][kind]}})
    return result


STYLE_ROOTS = {"title", "legend", "grid", "color", "backgroundColor", "tooltip", "xAxis", "yAxis", "series", "visualMap"}
STYLE_KEYS = {"textStyle", "left", "right", "top", "bottom", "show", "color", "fontSize", "fontWeight",
              "fontFamily", "position", "rotate", "width", "height", "overflow", "align", "verticalAlign",
              "orient", "containLabel", "itemStyle", "label", "lineStyle", "areaStyle", "borderRadius",
              "borderWidth", "borderColor", "opacity", "type", "smooth", "symbol", "symbolSize", "inverse",
              "axisLabel", "axisLine", "axisTick", "splitLine", "trigger", "text", "subtext", "barWidth",
              "min", "max", "calculable", "inRange", "outOfRange", "splitNumber"}


def apply_chart_style(option, patch):
    if not isinstance(patch, dict) or set(patch) - STYLE_ROOTS:
        raise CapabilityError(422, "INVALID_CHART_STYLE", "图表样式只能修改支持的呈现属性")

    def merge(old, change, path):
        if isinstance(change, dict):
            if set(change) - STYLE_KEYS:
                raise CapabilityError(422, "INVALID_CHART_STYLE", "图表样式不能修改数据、映射或执行脚本")
            result = copy.deepcopy(old) if isinstance(old, dict) else {}
            for key, value in change.items():
                if key == "type" and path and path[0] == "series":
                    allowed = {"heatmap"} if isinstance(old, dict) and old.get("type") == "heatmap" else {"bar", "line", "scatter"}
                    if value not in allowed:
                        raise CapabilityError(422, "INVALID_CHART_STYLE", "不支持该图表类型")
                result[key] = merge(result.get(key), value, path + [key])
            return result
        if isinstance(change, list):
            if path and path[-1] == "color" and all(isinstance(item, str) for item in change):
                return change
            if not isinstance(old, list) or len(change) > len(old):
                raise CapabilityError(422, "INVALID_CHART_STYLE", "样式修改不能增加数据系列")
            return [merge(item, change[index], path) if index < len(change) else copy.deepcopy(item)
                    for index, item in enumerate(old)]
        if not isinstance(change, (str, int, float, bool, type(None))):
            raise CapabilityError(422, "INVALID_CHART_STYLE", "图表样式值无效")
        return change

    result = copy.deepcopy(option)
    for key, value in patch.items():
        result[key] = merge(result.get(key), value, [key])
    return result


def _year(value):
    return value.year if isinstance(value, date) else date.fromisoformat(value).year


def heatmap_snapshot(dimension, title, patents, assignments, theme_definitions):
    """Compute joint cells and exact drilldown sets, never cross marginal aggregates."""
    if dimension not in {"annual_theme", "subject_matrix"}:
        raise CapabilityError(422, "UNKNOWN_HEATMAP", "不支持的联合统计")
    cells, x_counts = defaultdict(list), Counter()
    labels = {theme["id"]: theme["label"] for theme in theme_definitions}
    for patent in patents:
        x_values = [str(_year(patent["application_date"]))] if dimension == "annual_theme" else list(dict.fromkeys(patent["applicants"]))
        methods = list(dict.fromkeys(assignments[patent["id"]]["assignment"]["methods"]))
        for x_value in x_values:
            x_counts[x_value] += 1
            for theme_id in methods:
                cells[(x_value, theme_id)].append(patent["id"])
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
