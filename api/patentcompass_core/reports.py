"""Detached report construction and revision; no application state is required."""

from copy import deepcopy
import json
from uuid import NAMESPACE_URL, uuid5

from .contracts import CapabilityError


DEFAULT_REPORT_LAYOUT = {
    "template_version": "statistical-brief-v1", "paper_size": "A4",
    "page_width_inches": 8.27, "page_height_inches": 11.69,
    "margins_inches": {"top": 0.75, "bottom": 0.75, "left": 0.8, "right": 0.8},
    "font_family": "Noto Sans CJK SC", "body_font_pt": 10.5, "chart_width_inches": 6.5,
}


def build_report_content(snapshot, *, default_layout=None):
    """Build a detached report from explicit facts and frozen assets.

    Required snapshot keys: project_name, analysis_id, scope, summary, sources,
    charts, evidence, representatives. Optional views adds semantic sections.
    Sources contain file_name, quality, source_metadata; chart snapshots contain
    id, version_id, kind, data and option. No project or database is consulted.
    """
    summary = snapshot["summary"]
    blocks = []

    def add(key, kind, **values):
        block_id = str(uuid5(NAMESPACE_URL, f"patentcompass:{snapshot['analysis_id']}:{key}"))
        blocks.append({"id": block_id, "type": kind, **values})

    add("scope-heading", "heading", text="研究范围与数据说明", level=1)
    add("overview", "paragraph", text=(
        f"本报告统计当前确认范围内的 {summary.get('application_count', 0)} 条申请记录，"
        f"涉及 {summary.get('publication_count', 0)} 个公开标识及 "
        f"{summary.get('abstract_count', 0)} 份行级摘要。结论仅适用于已导入样本，不能代表全部检索结果。"))
    scope_labels = {"industry": "研究行业", "goal": "决策目标", "need": "应用需求", "focus": "关注对象",
                    "q": "统计关键词", "query": "检索词", "keyword": "检索词", "region": "地区",
                    "source_region": "来源地区说明", "coverage": "覆盖说明", "applicant": "申请人", "ipc": "IPC",
                    "start": "申请起始年份", "end": "申请结束年份",
                    "application_start": "申请起始日期", "application_end": "申请结束日期"}
    scope_values = {**snapshot["scope"], **(snapshot["scope"].get("filters") or {})}
    scope_description = "；".join(f"{label}：{scope_values[key]}" for key, label in scope_labels.items()
                                  if scope_values.get(key))
    add("scope", "paragraph", text="确认范围：" + (scope_description or "采用本次已确认范围内的全部已导入记录"))
    for index, source in enumerate(snapshot["sources"]):
        metadata, quality = source["source_metadata"], source["quality"]
        conditions = "；".join(f"{label}：{metadata[key]}" for key, label in (
            ("query", "原始检索式"), ("search_query", "原始检索式"), ("region", "原始地区筛选"))
                               if metadata.get(key))
        add(f"source-{index}", "paragraph", text=(
            f"原始文件：{source['file_name']}。{conditions + '。' if conditions else ''}"
            f"导入校验得到 {quality.get('application_count', 0)} 条有效申请、"
            f"{quality.get('publication_count', 0)} 个公开标识，"
            f"记录异常 {len(quality.get('errors', []))} 项。"))
        for warning_index, warning in enumerate(quality.get("warnings", [])):
            add(f"source-{index}-warning-{warning_index}", "paragraph", text=str(warning))
    add("counting", "paragraph", text=(
        "统计以申请记录为单位，共同申请人分别全计数，多 IPC 标签分别计数。"
        "因此申请人或 IPC 合计可超过申请总量。公开标识与申请记录分开统计。"))
    for key, heading, field, columns in (
        ("yearly", "年度申请分布", "yearly", ["年度", "申请数量"]),
        ("applicants", "主要申请人", "applicants", ["申请人", "申请数量"]),
        ("ipc", "IPC 技术分类", "ipc", ["IPC 分类", "申请数量"]),
    ):
        add(f"{key}-heading", "heading", text=heading, level=1)
        rows = summary.get(field, [])
        label = {"yearly": "year", "applicants": "name", "ipc": "code"}[key]
        limit = len(rows) if key == "yearly" else 15
        add(f"{key}-table", "table", columns=columns,
            rows=[[str(row.get(label, "")), str(row["count"])] for row in rows[:limit]],
            caption="全部入选记录统计" if limit == len(rows) else f"按数量展示前 {min(limit, len(rows))} 项")
        for chart in snapshot["charts"]:
            if chart["kind"] == key:
                add(f"{key}-chart", "chart", chart=deepcopy(chart),
                    chart_id=chart["id"], chart_version_id=chart["version_id"],
                    caption=chart["data"].get("title", heading),
                    layout={"width_percent": 100, "alignment": "center"})
    technology = snapshot.get("views", {}).get("technology", {})
    semantic_ready = technology.get("status") == "ready" and technology.get("data", {}).get("classification") == "semantic"
    if semantic_ready:
        data = technology["data"]
        coverage = data.get("coverage", {})
        add("semantic-heading", "heading", text="全量语义分类与技术结构", level=1)
        add("semantic-coverage", "paragraph", text=(
            f"已处理 {coverage.get('processed_count', 0)} 条入选申请，可靠归类 {coverage.get('classified_count', 0)} 条，"
            f"待复核 {coverage.get('review_count', 0)} 条。分类归属及数量来自固定分析版本，待复核记录仍保留在总体中。"))
        themes = {}
        for dimension, label in (("elements", "基础要素"), ("methods", "技术方法"), ("scenarios", "应用场景")):
            rows = data.get("taxonomy", {}).get(dimension, [])
            themes.update({item["id"]: item["label"] for item in rows})
            add(f"semantic-{dimension}-heading", "heading", text=label, level=2)
            add(f"semantic-{dimension}-table", "table", columns=[label, "涉及申请数"],
                rows=[[item["label"], str(item.get("count", 0))] for item in rows], caption="多标签分别计数")
            for chart in snapshot["charts"]:
                if chart["kind"] == dimension:
                    add(f"semantic-{dimension}-chart", "chart", chart=deepcopy(chart), chart_id=chart["id"],
                        chart_version_id=chart["version_id"], caption=label,
                        layout={"width_percent": 100, "alignment": "center"})
        landscape = snapshot.get("views", {}).get("landscape", {}).get("data", {})
        if landscape.get("subject_matrix"):
            add("matrix-heading", "heading", text="申请人与技术方法交叉统计", level=1)
            add("matrix-table", "table", columns=["申请人", "技术方法", "申请数"], rows=[
                [item["applicant"], themes.get(item["theme_id"], item["theme_id"]), str(item["count"])]
                for item in landscape["subject_matrix"]], caption="逐条申请归属计算；共同申请人全计数")
        opportunities = snapshot.get("views", {}).get("opportunities", {}).get("data", {}).get("items", [])
        if opportunities:
            originals = {item["application_id"]: item for item in snapshot.get("evidence", []) if item.get("application_id")}
            add("directions-heading", "heading", text="候选研究方向与风险关注", level=1)
            for item in opportunities:
                add(f"direction-{item['id']}", "heading", text=f"{item.get('priority', '')} {item['title']}", level=2,
                    direction_id=item["id"])
                add(f"direction-description-{item['id']}", "paragraph", text=item.get("description", ""), direction_id=item["id"])
                add(f"direction-support-{item['id']}", "paragraph", text=(
                    f"样本支持记录 {item.get('support_count', 0)} 条。{item.get('priority_reason', '')}"
                    "外部补检尚未执行，不能据此认定技术空白、可专利性或法律结论。"), direction_id=item["id"],
                    application_ids=item.get("supporting_application_ids", []))
                for index, application_id in enumerate(item.get("supporting_application_ids", [])[:3]):
                    evidence = originals.get(application_id)
                    if evidence:
                        original = evidence["content"]
                        quotes = original.get("semantic_evidence", [])
                        quote = next((part.get("quote") for part in quotes if part.get("theme_id") in
                                      {item.get("method_theme_id"), item.get("scenario_theme_id")}), original.get("abstract", "")[:280])
                        add(f"direction-evidence-{item['id']}-{index}", "paragraph", text=(
                            f"原文依据：{original.get('application_number', application_id)}；"
                            f"工作表 {original.get('source_sheet', '')}，第 {original.get('source_row', '')} 行。{quote}"),
                            direction_id=item["id"], application_id=application_id, evidence_id=evidence["id"])
                for index, comparison in enumerate(item.get("counterexample_search", {}).get("results", [])[:3]):
                    evidence = originals.get(comparison["application_id"])
                    number = evidence["content"].get("application_number") if evidence else comparison["application_id"]
                    add(f"direction-comparison-{item['id']}-{index}", "paragraph", text=(
                        f"本地相近记录（待人工核对）：{number}。{comparison.get('quote', '')}"),
                        direction_id=item["id"], application_id=comparison["application_id"],
                        evidence_id=evidence["id"] if evidence else None)
                for risk in item.get("risks", []):
                    add(f"risk-{risk['id']}", "paragraph", text=f"风险关注：{risk['title']}。{risk.get('reason', '')}",
                        risk_id=risk["id"], direction_id=item["id"])
        blocks.extend(_roadmap_blocks({"source": snapshot}, snapshot["analysis_id"], allow_unavailable=True))
        geography = snapshot.get("views", {}).get("geography", {}).get("data", {})
        if geography.get("country"):
            add("source-country-heading", "heading", text="申请人来源国家", level=1)
            add("source-country-table", "table", columns=["来源国家", "申请数"],
                rows=[[item["country"], str(item["count"])] for item in geography["country"]],
                caption="共同来源国家全计数；不等于保护地")
            add("source-country-limit", "paragraph", text="省市、有效保护地与同族视角缺少必要字段，当前不能生成对应结论。")
    add("limits-heading", "heading", text="可用结论与待补充资料", level=1)
    add("limits", "paragraph", text=(
        "本次提供年度、申请人和 IPC 描述性统计，以及原文摘要定位。"
        + ("语义分类覆盖全部入选记录；候选方向与阶段行动仅用于后续核查，不表示已证明的技术空白。" if semantic_ready else
           "IPC 分类不能直接替代语义技术路线。当前固定分析未形成语义分类和候选路线结论。")
        + "省市、有效保护地和同族视角缺少必要字段；仍需权利要求、法律状态及外部补检证据。"))
    add("representatives-heading", "heading", text="样本记录与原文证据附录", level=1)
    add("representatives-method", "paragraph", text=(
        "以下按申请日期从新到旧列出最多五条样本记录，仅用于追溯原文，不代表技术重要性排名。"
        "保留英文摘要原文，不据此推断授权或法律状态。"))
    for record in snapshot["representatives"]:
        key = record["application_id"]
        add(f"patent-{key}", "heading", level=2,
            text=f"{record['application_number']} {record['title']}")
        add(f"patent-source-{key}", "paragraph", text=(
            f"公开编号：{', '.join(record['publication_numbers'])}；申请人：{'; '.join(record['applicants'])}。"
            f"来源工作表：{record['source_sheet']}；原始行：{record['source_row']}；"
            f"数据版本：{record['dataset_version_id']}。"), application_id=key)
        add(f"abstract-{key}", "paragraph", text=record["abstract"], application_id=key)
    return {"title": f"{snapshot['project_name']} 专利样本统计简报", "blocks": blocks,
            "layout": deepcopy(DEFAULT_REPORT_LAYOUT if default_layout is None else default_layout),
            "source": {key: deepcopy(value) for key, value in snapshot.items()
                       if key not in {"charts", "evidence", "representatives", "project_name"}},
            "evidence_snapshot": deepcopy(snapshot["evidence"]),
            "representative_snapshot": deepcopy(snapshot["representatives"])}


def _response_text(response):
    content = response.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 40000:
        raise CapabilityError(502, "INVALID_MODEL_RESPONSE", "模型没有返回有效的报告文字")
    return content.strip()


def replace_block(content, block_id, replacement):
    content = deepcopy(content)
    block = next((block for block in content["blocks"] if block["id"] == block_id), None)
    if not block:
        raise CapabilityError(404, "BLOCK_NOT_FOUND", "指定正文块不存在")
    if block["type"] not in {"paragraph", "heading"}:
        raise CapabilityError(422, "BLOCK_TYPE_UNSUPPORTED", "首轮仅支持替换段落或标题文字")
    block["text"] = replacement
    return content


def _roadmap_blocks(content, namespace, allow_unavailable=False):
    view = content.get("source", {}).get("views", {}).get("roadmap", {})
    items = view.get("data", {}).get("items", [])
    if view.get("status") != "ready" or not items:
        if allow_unavailable:
            return []
        raise CapabilityError(409, "ROADMAP_UNAVAILABLE", "当前报告固定分析版本尚无可追加的布局与路线图，请先完成全量语义分析")
    blocks = []

    def add(key, kind, **values):
        blocks.append({"id": str(uuid5(NAMESPACE_URL, f"patentcompass:{namespace}:roadmap:{key}")),
                       "type": kind, **values})

    add("heading", "heading", text="布局与路线图", level=1, section="roadmap")
    for item in items:
        direction_id = item["id"]
        add(direction_id, "heading", text=f"{item.get('priority', '')} {item['title']}", level=2,
            direction_id=direction_id)
        add(direction_id + "-conditions", "paragraph", text="推进条件：" + "；".join(item.get("conditions", [])),
            direction_id=direction_id)
        for phase in item.get("phases", []):
            add(phase["id"], "heading", text=phase["title"], level=3, direction_id=direction_id, phase_id=phase["id"])
            for index, action in enumerate(phase.get("actions", [])):
                add(f"{phase['id']}-action-{index}", "paragraph", text=(
                    f"行动：{action['title']}；负责人：{action.get('owner', '待分配')}；"
                    f"状态：{'待外部验证' if action.get('status') == 'pending_external' else '待执行'}。"),
                    direction_id=direction_id, action_id=action.get("id"))
        add(direction_id + "-boundary", "paragraph", text="外部补检尚未执行；本节为样本核查计划，不能替代完整检索或法律判断。",
            direction_id=direction_id, application_ids=item.get("supporting_application_ids", []))
    return blocks


def _validated_layout(change, *, chart=False):
    if not isinstance(change, dict) or not change:
        raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "请提供有效的排版设置")
    if chart:
        if set(change) - {"width_percent", "alignment"}:
            raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "图表排版仅支持宽度和对齐")
        if "width_percent" in change and change["width_percent"] not in {50, 100}:
            raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "图表宽度只能为 50 或 100")
        if "alignment" in change and change["alignment"] not in {"left", "center", "right"}:
            raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "无效的图表对齐方式")
        return deepcopy(change)
    allowed = {"paper_size", "page_width_inches", "page_height_inches", "margins_inches", "font_family", "body_font_pt", "chart_width_inches"}
    if set(change) - allowed:
        raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "报告排版包含不支持的设置")
    for key, value in change.items():
        if key == "paper_size" and value != "A4":
            raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "当前仅支持 A4 页面")
        if key == "font_family" and (not isinstance(value, str) or not value.strip() or len(value) > 100):
            raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "无效的字体名称")
        if key == "margins_inches":
            if not isinstance(value, dict) or set(value) - {"top", "bottom", "left", "right"}:
                raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "页边距设置无效")
            if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not 0.25 <= item <= 2 for item in value.values()):
                raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "页边距须在 0.25 到 2 英寸之间")
        bounds = {"page_width_inches": (6, 12), "page_height_inches": (8, 16), "body_font_pt": (8, 24), "chart_width_inches": (2, 10)}
        if key in bounds and (isinstance(value, bool) or not isinstance(value, (int, float)) or not bounds[key][0] <= value <= bounds[key][1]):
            raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "页面、字体或图表尺寸超出支持范围")
    return deepcopy(change)


def _new_block(raw, block_id):
    if not isinstance(raw, dict) or set(raw) - {"id", "type", "text", "level", "columns", "rows", "caption"}:
        raise CapabilityError(422, "INVALID_REPORT_BLOCK", "新增正文块格式无效")
    block = {**deepcopy(raw), "id": raw.get("id") or block_id}
    if not isinstance(block["id"], str) or not 1 <= len(block["id"]) <= 200:
        raise CapabilityError(422, "INVALID_REPORT_BLOCK", "正文块标识无效")
    if block.get("type") in {"paragraph", "heading"}:
        if not isinstance(block.get("text"), str) or len(block["text"]) > 40000:
            raise CapabilityError(422, "INVALID_REPORT_BLOCK", "正文块需要有效文字")
        if block["type"] == "heading" and block.get("level", 1) not in {1, 2, 3}:
            raise CapabilityError(422, "INVALID_REPORT_BLOCK", "章节标题层级只能为 1、2 或 3")
    elif block.get("type") == "table":
        columns, rows = block.get("columns"), block.get("rows")
        if (not isinstance(columns, list) or not 1 <= len(columns) <= 10 or
                any(not isinstance(column, str) for column in columns) or not isinstance(rows, list) or len(rows) > 5200 or
                any(not isinstance(row, list) or len(row) != len(columns) or
                    any(not isinstance(cell, (str, int, float)) for cell in row) for row in rows)):
            raise CapabilityError(422, "INVALID_REPORT_BLOCK", "表格列、行或单元格格式无效")
    else:
        raise CapabilityError(422, "INVALID_REPORT_BLOCK", "新增正文块仅支持标题、段落或表格；图表请使用插入图表操作")
    return block


def revise_report_content(content, operations=None, *, namespace="revision", charts=None,
                          instruction=None, block_id=None, generate_callable=None, default_layout=None):
    """Apply atomic edits to detached content, using explicit frozen chart assets.

    Natural-language editing requires a block_id and generate_callable(messages)
    returning a message with content. Chart snapshots can include an optional
    evidence_snapshot list, which is merged into the report's frozen evidence.
    """
    result = deepcopy(content)
    if instruction is not None:
        block = next((item for item in result["blocks"] if item["id"] == block_id), None)
        if block is None:
            raise CapabilityError(404, "BLOCK_NOT_FOUND", "指定正文块不存在")
        if block["type"] not in {"paragraph", "heading"}:
            raise CapabilityError(422, "BLOCK_TYPE_UNSUPPORTED", "文字修改仅支持段落或标题")
        if generate_callable is None:
            raise CapabilityError(503, "MODEL_NOT_CONFIGURED", "自然语言修改需要显式提供生成能力")
        response = generate_callable([
            {"role": "system", "content": "只返回修改后的单个正文块文字。遵循指令，保留证据与数据边界，不虚构事实。沿用每项统计的原始总体和维度；申请人排名、IPC排名及年份分布是独立统计，未提供交叉统计时，不能写成某申请人主要涉及某IPC，也不能新增相关性或因果归因。压缩篇幅时优先保留样本范围和核心数量，不把省略的证据说成已全部保留。"},
            {"role": "user", "content": json.dumps({"instruction": instruction,
                "block": block, "source": result.get("source", {})}, ensure_ascii=False)},
        ])
        result = replace_block(result, block_id, _response_text(response))
    blocks = result["blocks"]

    def locate(block_id):
        block = next((item for item in blocks if item["id"] == block_id), None)
        if block is None:
            raise CapabilityError(404, "BLOCK_NOT_FOUND", "指定正文块不存在", {"block_id": block_id})
        return block

    def insert(block, after_id):
        if any(item["id"] == block["id"] for item in blocks):
            raise CapabilityError(422, "DUPLICATE_REPORT_BLOCK", "正文块标识重复")
        index = blocks.index(locate(after_id)) + 1 if after_id is not None else 0
        blocks.insert(index, block)

    def frozen_chart(operation):
        chart = next((item for item in (charts or []) if str(item["id"]) == str(operation.get("chart_id"))
                      and (not operation.get("chart_version_id") or
                           str(item["version_id"]) == str(operation["chart_version_id"]))), None)
        if chart is None:
            raise CapabilityError(404, "CHART_NOT_FOUND", "未提供指定图表的冻结快照")
        snapshot = deepcopy(chart)
        existing = {item["id"] for item in result.get("evidence_snapshot", [])}
        for evidence in snapshot.pop("evidence_snapshot", []):
            if evidence["id"] not in existing:
                result.setdefault("evidence_snapshot", []).append(deepcopy(evidence))
                existing.add(evidence["id"])
        return snapshot

    for index, operation in enumerate(operations or []):
        op = operation["op"]
        block_id = operation.get("block_id")
        new_id = str(uuid5(NAMESPACE_URL, f"patentcompass:revision:{namespace}:{index}"))
        if op == "set_title":
            text = operation.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 300:
                raise CapabilityError(422, "INVALID_REPORT_TITLE", "报告标题需要 1 到 300 个字符")
            result["title"] = text.strip()
        elif op == "replace_text":
            block = locate(block_id)
            if block["type"] not in {"heading", "paragraph"} or not isinstance(operation.get("text"), str):
                raise CapabilityError(422, "BLOCK_TYPE_UNSUPPORTED", "文字修改仅支持标题和段落")
            block["text"] = operation["text"]
        elif op == "insert_block":
            insert(_new_block(operation.get("block"), new_id), operation.get("after_id"))
        elif op == "remove_block":
            blocks.remove(locate(block_id))
        elif op == "move_block":
            block = locate(block_id)
            if operation.get("after_id") == block_id:
                raise CapabilityError(422, "INVALID_BLOCK_POSITION", "正文块不能移动到自身之后")
            if operation.get("after_id") is not None:
                locate(operation["after_id"])
            blocks.remove(block)
            insert(block, operation.get("after_id"))
        elif op == "append_roadmap":
            if any(block.get("section") == "roadmap" for block in blocks):
                raise CapabilityError(409, "ROADMAP_ALREADY_PRESENT", "当前报告已包含布局与路线图章节")
            blocks.extend(_roadmap_blocks(result, namespace))
        elif op in {"insert_chart", "update_chart"}:
            chart = frozen_chart(operation)
            if op == "insert_chart":
                block = {"id": new_id, "type": "chart", "layout": {"width_percent": 100, "alignment": "center"}}
            else:
                block = locate(block_id)
                if block["type"] != "chart":
                    raise CapabilityError(422, "BLOCK_TYPE_UNSUPPORTED", "此操作需要指定图表块")
            block.update({"chart": chart, "chart_id": chart["id"], "chart_version_id": chart["version_id"],
                          "caption": operation.get("text") or chart["data"].get("title", chart["kind"])})
            if operation.get("layout"):
                block["layout"] = {**block.get("layout", {}), **_validated_layout(operation["layout"], chart=True)}
            if op == "insert_chart":
                insert(block, operation.get("after_id"))
        elif op == "set_chart_layout":
            block = locate(block_id)
            if block["type"] != "chart":
                raise CapabilityError(422, "BLOCK_TYPE_UNSUPPORTED", "此操作需要指定图表块")
            block["layout"] = {**block.get("layout", {}), **_validated_layout(operation.get("layout"), chart=True)}
        elif op == "set_layout":
            layout = {**deepcopy(DEFAULT_REPORT_LAYOUT if default_layout is None else default_layout),
                      **result.get("layout", {})}
            change = _validated_layout(operation.get("layout"))
            if "margins_inches" in change:
                change["margins_inches"] = {**layout["margins_inches"], **change["margins_inches"]}
            layout.update(change)
            if layout["page_width_inches"] - layout["margins_inches"]["left"] - layout["margins_inches"]["right"] < 2:
                raise CapabilityError(422, "INVALID_REPORT_LAYOUT", "页面正文区域过窄")
            result["layout"] = layout
        else:
            raise CapabilityError(422, "INVALID_REPORT_OPERATION", "不支持的报告操作")
    if not blocks:
        raise CapabilityError(422, "EMPTY_REPORT", "报告至少需要保留一个正文块")
    return result
