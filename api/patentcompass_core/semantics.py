"""Evidence-checked semantic classification over detached patent records."""

from patentcompass_agent import read_prompt

import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from .charts import build_chart_options, data_hash, heatmap_snapshot
from .contracts import CapabilityError, InlineExecution, PatentRecord
from .retrieval import text_hash
from .statistics import COUNTING_METHOD, compute_statistics


DIMENSIONS = ("elements", "methods", "scenarios")
DIMENSION_LABELS = {"elements": "基础要素", "methods": "技术方法", "scenarios": "应用场景"}
REVIEW_LABELS = {"invalid_format": "模型返回格式需要复核", "invalid_item": "标签格式需要复核",
                 "unverified_evidence": "引文未能与原文逐字对应，已排除该标签", "low_confidence": "归属置信度不足，已排除该标签",
                 "insufficient_evidence": "原文依据不足，尚未确定归属", "unmapped": "尚未归入分类体系",
                 "user_unclassified": "已由用户标记为暂不归类"}
PROMPT_VERSION = "patent-extraction-v1"
TAXONOMY_PROMPT_VERSION = "taxonomy-split-v3"
SCHEMA_VERSION = "semantic-v1"
BATCH_SIZE = 20


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()



def decode_response(message):
    try:
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ValueError("Missing JSON content")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError) as exc:
        raise CapabilityError(502, "SEMANTIC_RESPONSE_INVALID", "分类模型返回格式无效，可重试并复用已完成批次") from exc



def validate_extraction(patent, row):
    patent = PatentRecord.model_validate(patent)
    result = {key: [] for key in DIMENSIONS}
    reasons = []
    for dimension in DIMENSIONS:
        values = row.get(dimension, [])
        if not isinstance(values, list):
            reasons.append(f"{dimension}:invalid_format")
            continue
        for item in values[:3]:
            if not isinstance(item, dict):
                reasons.append(f"{dimension}:invalid_item")
                continue
            label, quote, field = item.get("label", ""), item.get("quote", ""), item.get("field", "abstract")
            confidence = item.get("confidence", 0)
            if (not isinstance(label, str) or not label.strip() or len(label) > 80 or
                    not isinstance(quote, str) or len(quote.strip()) < 5 or len(quote) > 300 or
                    not isinstance(field, str) or field not in {"title", "abstract"} or quote not in getattr(patent, field) or
                    not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1):
                reasons.append(f"{dimension}:unverified_evidence")
                continue
            if confidence < .65:
                reasons.append(f"{dimension}:low_confidence")
                continue
            result[dimension].append({"label": label.strip(), "quote": quote, "field": field,
                                       "confidence": confidence})
        if not result[dimension]:
            reasons.append(f"{dimension}:insufficient_evidence")
    result["review_reasons"] = list(dict.fromkeys(reasons))
    return result



def _request_extraction(chunk, generate_callable):
    completed = {}
    # Short request-local identifiers avoid UUID transcription mistakes. Only this map can resolve resource IDs.
    local = {f"p{index}": patent for index, patent in enumerate(chunk)}
    for _ in range(3):
        remaining = {key: patent for key, patent in local.items() if patent.id not in completed}
        if not remaining:
            break
        sources = [{"id": key, "title": patent.title, "abstract": patent.abstract} for key, patent in remaining.items()]
        message = generate_callable([
            {"role": "system", "content": (
                read_prompt('semantic-extraction'))},
            {"role": "user", "content": json.dumps({"records": sources}, ensure_ascii=False)}])
        records = decode_response(message).get("records")
        if not isinstance(records, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in records):
            raise CapabilityError(502, "SEMANTIC_RESPONSE_INVALID", "分类响应缺少逐条记录")
        occurrences = Counter(row["id"] for row in records)
        for row in records:
            key = row["id"]
            if key in remaining and occurrences[key] == 1:
                completed[remaining[key].id] = {**row, "id": remaining[key].id}
    if set(completed) != {patent.id for patent in chunk}:
        raise CapabilityError(502, "SEMANTIC_COVERAGE_MISMATCH", "分类响应未覆盖本批全部申请，可重试")
    return list(completed.values())



def extract_records(records, generate_callable):
    patents = [PatentRecord.model_validate(record) for record in records]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda chunk: _request_extraction(chunk, generate_callable),
                                     [patents[index:index + 10] for index in range(0, len(patents), 10)]))
    rows = [row for records in responses for row in records]
    by_id = {row.get("id"): row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != {patent.id for patent in patents}:
        raise CapabilityError(502, "SEMANTIC_COVERAGE_MISMATCH", "分类响应未覆盖本批全部申请，可重试")
    return by_id


def group_dimension(dimension, labels, generate_callable):
    ids = {f"{dimension[0]}{index}": label for index, label in enumerate(labels[dimension])}
    if not ids:
        return {"themes": [], "mapping": {}, "unmapped_labels": []}
    compact = {dimension: [{"id": key, "label": label} for key, label in ids.items()]}
    message = generate_callable([
        {"role": "system", "content": (
            read_prompt('semantic-taxonomy'))},
        {"role": "user", "content": json.dumps(compact, ensure_ascii=False)}])
    return validate_taxonomy_dimension(dimension, labels, decode_response(message))


def validate_taxonomy_dimension(dimension, labels, response):
    """Validate host-agent groups with the same rules as legacy generation."""
    ids = {f"{dimension[0]}{index}": label for index, label in enumerate(labels[dimension])}
    groups = response.get(dimension, [])
    if not isinstance(groups, list) or len(groups) > 20:
        raise CapabilityError(502, "TAXONOMY_INVALID", "分类体系格式无效")
    accepted, dimension_themes, dimension_mapping = set(), [], {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("label"), str) or not group["label"].strip():
            raise CapabilityError(502, "TAXONOMY_INVALID", "分类体系缺少主题名")
        members = group.get("members")
        if not isinstance(members, list) or any(not isinstance(member, str) for member in members):
            raise CapabilityError(502, "TAXONOMY_INVALID", "分类体系包含无效成员格式")
        members = [ids.get(member, member) for member in members]
        if (len(set(members)) != len(members) or set(members) - set(labels[dimension]) or accepted.intersection(members)):
            raise CapabilityError(502, "TAXONOMY_INVALID", "分类体系包含重复或无来源标签")
        label = group["label"].strip()
        theme_id = f"{dimension}-{_digest(label)[:12]}"
        if theme_id in {theme["id"] for theme in dimension_themes}:
            raise CapabilityError(502, "TAXONOMY_INVALID", "分类体系包含重复主题名")
        dimension_themes.append({"id": theme_id, "label": label, "members": members})
        for member in members:
            dimension_mapping[f"{dimension}:{member}"] = theme_id
        accepted.update(members)
    return {"themes": dimension_themes, "mapping": dimension_mapping,
            "unmapped_labels": sorted(set(labels[dimension]) - accepted)}



def taxonomy_labels(extractions):
    return {dimension: sorted({item["label"] for row in extractions.values() for item in row[dimension]})
            for dimension in DIMENSIONS}


def assemble_taxonomy(labels, results):
    themes = {dimension: results[dimension]["themes"] for dimension in DIMENSIONS}
    mapping = {key: value for dimension in DIMENSIONS for key, value in results[dimension]["mapping"].items()}
    unmapped = {dimension: results[dimension]["unmapped_labels"] for dimension in DIMENSIONS}
    return {"version": _digest({"themes": themes, "unmapped": unmapped}), "themes": themes, "mapping": mapping,
            "unmapped_labels": unmapped}


def assign_themes(extractions, taxonomy):
    assignments = {}
    for application_id, extraction in extractions.items():
        assignment = {dimension: [] for dimension in DIMENSIONS}
        assignment.update({"evidence": [], "review_reasons": list(extraction.get("review_reasons", [])),
                           "cache_key": extraction.get("cache_key")})
        for dimension in DIMENSIONS:
            for item in extraction[dimension]:
                theme_id = taxonomy["mapping"].get(f"{dimension}:{item['label']}")
                if not theme_id:
                    assignment["review_reasons"].append(f"{dimension}:unmapped")
                    continue
                if theme_id not in assignment[dimension]:
                    assignment[dimension].append(theme_id)
                assignment["evidence"].append({"dimension": dimension, "theme_id": theme_id, **item})
        assignments[application_id] = {"status": "review" if assignment["review_reasons"] else "classified",
                                       "assignment": assignment}
    return assignments



def candidate_directions(patents, assignments, taxonomy):
    patents = [PatentRecord.model_validate(patent) for patent in patents]
    names = {theme["id"]: theme["label"] for values in taxonomy["themes"].values() for theme in values}
    by_id = {patent.id: patent for patent in patents}
    pairs = defaultdict(list)
    for application_id, row in assignments.items():
        for method in row["assignment"]["methods"]:
            for scenario in row["assignment"]["scenarios"]:
                pairs[(method, scenario)].append(application_id)
    items = []
    for index, ((method, scenario), support) in enumerate(sorted(pairs.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:3]):
        direction_id = "direction-" + _digest([method, scenario])[:16]
        # Original English evidence supplies retrieval terms; translated labels do not pretend to match English abstracts.
        quotes = [evidence["quote"] for application_id in support for evidence in
                  assignments[application_id]["assignment"]["evidence"] if evidence["theme_id"] in {method, scenario}]
        words = Counter(word for quote in quotes for word in re.findall(r"[A-Za-z]{4,}", quote.lower())
                        if word not in {"that", "with", "from", "which", "there", "where", "said", "comprising", "method", "device", "system", "includes", "according",
                                        "unmanned", "aerial", "vehicle", "vehicles", "control", "based", "using", "provided", "present", "invention", "first", "second"})
        terms = [word for word, _ in words.most_common(8)]
        comparisons = []
        for patent in patents:
            if patent.id in support:
                continue
            text = f"{patent.title}\n{patent.abstract}".lower()
            matches = [word for word in terms if word in text]
            assignment = assignments[patent.id]["assignment"]
            shared = [dimension for dimension, theme in (("methods", method), ("scenarios", scenario)) if theme in assignment[dimension]]
            if matches or shared:
                comparisons.append((10 * len(shared) + len(matches), patent.id, matches, shared))
        comparison_rows = [{"application_id": application_id, "title": by_id[application_id].title,
                            "quote": by_id[application_id].abstract[:280], "matched_terms": matches,
                            "shared_semantic_dimensions": shared,
                            "status": "candidate_requires_review"}
                           for _, application_id, matches, shared in sorted(comparisons, key=lambda row: (-row[0], row[1]))[:5]]
        priority = f"P{index + 1}"
        conditions = ["逐条核对相近记录的差异点", "补充权利要求、法律状态及外部完整检索"]
        actions = [{"id": direction_id + "-review", "title": "核对样本与本地对照记录", "status": "pending", "owner": "待分配"},
                   {"id": direction_id + "-external", "title": "执行外部补检并验证可行性", "status": "pending_external", "owner": "待分配"}]
        items.append({"id": direction_id, "title": f"{names[method]} × {names[scenario]}", "priority": priority,
                      "method_theme_id": method, "scenario_theme_id": scenario,
                      "description": "根据样本中已出现的方法与场景组合整理的候选研究方向，尚未证明技术空白或可专利性。",
                      "support_count": len(support), "supporting_application_ids": sorted(support),
                      "priority_reason": "按样本内该组合的记录数安排核查顺序，不代表商业价值排序。",
                      "counterexample_search": {"status": "completed", "method": "local_semantic_facets_and_original_text_overlap",
                                                "examined_count": len(patents), "query_terms": terms,
                                                "results": comparison_rows, "interpretation": "相近记录仅供人工判断，不自动认定为反例或侵权证据。"},
                      "risks": [{"id": direction_id + "-risk", "title": "样本覆盖与权利要求尚不足",
                                 "reason": "当前为部分导出，且缺少权利要求、有效法律状态和完整同族信息。", "status": "pending_validation"}],
                      "conditions": conditions, "actions": actions,
                      "phases": [{"id": direction_id + "-phase-1", "title": "样本证据核查", "actions": [actions[0]], "conditions": [conditions[0]]},
                                 {"id": direction_id + "-phase-2", "title": "外部验证与方案比较", "actions": [actions[1]], "conditions": [conditions[1]]}],
                      "external_validation": {"status": "not_performed", "reason": "本轮未接外部专利库"}})
    return items



def classification_source(records):
    patents = [PatentRecord.model_validate(record) for record in records]
    return {"version": 1, "record_fingerprints": {
        patent.id: _digest(patent.model_dump(mode="json")) for patent in patents}}


def _validate_classification(patents, classification):
    def invalid(message):
        raise CapabilityError(422, "CLASSIFICATION_SOURCE_MISMATCH", message)

    if not isinstance(classification, dict) or classification.get("source") != classification_source(patents):
        invalid("分类结果缺少匹配的原始记录依据，请使用当前记录重新分类")
    if len({patent.id for patent in patents}) != len(patents):
        invalid("分类记录标识不能重复")
    if not isinstance(classification.get("coverage", {}), dict):
        invalid("分类覆盖说明格式无效")
    assignments, taxonomy = classification.get("assignments"), classification.get("taxonomy")
    if not isinstance(assignments, dict) or set(assignments) != {patent.id for patent in patents}:
        invalid("分类归属与当前记录集合不一致")
    if not isinstance(taxonomy, dict) or not isinstance(taxonomy.get("version"), str) or not isinstance(taxonomy.get("themes"), dict):
        invalid("分类结果缺少有效的主题体系")
    known = {}
    for dimension in DIMENSIONS:
        themes = taxonomy["themes"].get(dimension)
        if not isinstance(themes, list) or any(not isinstance(theme, dict) or not isinstance(theme.get("id"), str)
                or not isinstance(theme.get("label"), str) for theme in themes):
            invalid("分类主题格式无效")
        known[dimension] = {theme["id"] for theme in themes}
        if len(known[dimension]) != len(themes):
            invalid("分类主题标识重复")
    for patent in patents:
        row = assignments[patent.id]
        if not isinstance(row, dict) or row.get("status") not in ("classified", "review") or not isinstance(row.get("assignment"), dict):
            invalid("分类归属格式无效")
        assignment = row["assignment"]
        for dimension in DIMENSIONS:
            selected = assignment.get(dimension)
            if not isinstance(selected, list) or any(not isinstance(item, str) or item not in known[dimension] for item in selected):
                invalid("分类归属引用了无来源主题")
            if len(set(selected)) != len(selected):
                invalid("分类归属不能重复引用同一主题")
        if not isinstance(assignment.get("review_reasons"), list) or any(not isinstance(reason, str) for reason in assignment["review_reasons"]):
            invalid("分类复核说明格式无效")
        if row["status"] != ("review" if assignment["review_reasons"] else "classified"):
            invalid("分类状态与待复核说明不一致")
        if not isinstance(assignment.get("evidence"), list):
            invalid("分类结果缺少原文依据")
        for evidence in assignment["evidence"]:
            dimension = evidence.get("dimension") if isinstance(evidence, dict) else None
            if dimension not in DIMENSIONS or evidence.get("theme_id") not in assignment[dimension]:
                invalid("分类引文对应的归属无效")
            if not validate_extraction(patent, {dimension: [evidence]})[dimension]:
                invalid("分类引文与当前原文不一致或置信度不足")
    return assignments, taxonomy


def analysis_draft(records, classification, versions=None, publications=None):
    """Compose checked classification into report-ready views, charts and original evidence.

    IDs are stable for a detached draft. A database publisher assigns its own IDs
    to a saved analysis while retaining the same pure content and counting rules.
    """
    patents = [PatentRecord.model_validate(record) for record in records]
    assignments, taxonomy = _validate_classification(patents, classification)
    publications = list(publications) if publications is not None else None
    calculated = compute_statistics(patents, publications, versions)
    summary, selections = calculated["summary"], calculated["selections"]
    supplied_coverage = classification.get("coverage", {})
    coverage = {"processed_count": len(assignments),
                "classified_count": sum(row["status"] == "classified" for row in assignments.values()),
                "review_count": sum(row["status"] == "review" for row in assignments.values()),
                "cached_count": supplied_coverage.get("cached_count", 0), "total_count": len(patents),
                "taxonomy_version": taxonomy["version"], "model": supplied_coverage.get("model", ""),
                "counting_note": "按通过原文核验的各维度归属分别统计；待复核记录中已有可靠依据的维度仍计入，其余归属不计入。"}
    summary["semantic"] = coverage
    identity = _digest({"source": classification["source"], "taxonomy": taxonomy, "assignments": assignments})
    analysis_id = str(uuid5(NAMESPACE_URL, "patentcompass:analysis:" + identity))
    stable_id = lambda kind: str(uuid5(NAMESPACE_URL, f"{analysis_id}:{kind}"))
    themes = deepcopy(taxonomy["themes"])
    theme_selections = {dimension: defaultdict(list) for dimension in DIMENSIONS}
    annual, matrix = Counter(), Counter()
    for patent in patents:
        assignment = assignments[patent.id]["assignment"]
        for dimension in DIMENSIONS:
            for theme_id in dict.fromkeys(assignment[dimension]):
                theme_selections[dimension][theme_id].append(patent.id)
        for theme_id in dict.fromkeys(assignment["methods"]):
            annual[(patent.application_date.year, theme_id)] += 1
            for applicant in dict.fromkeys(patent.applicants):
                matrix[(applicant, theme_id)] += 1
    for dimension in DIMENSIONS:
        for theme in themes[dimension]:
            theme["count"] = len(theme_selections[dimension][theme["id"]])
    directions = candidate_directions(patents, assignments, taxonomy)
    provenance = {"scope_version_id": None,
                  "dataset_version_ids": sorted({patent.dataset_version_id for patent in patents if patent.dataset_version_id}),
                  "application_ids": calculated["patent_ids"], "counting_method": dict(COUNTING_METHOD),
                  "taxonomy_version": taxonomy["version"]}
    charts = {}
    specs = [("yearly", "year", "年度申请分布", "year"), ("applicants", "applicant", "主要申请人", "name"),
             ("ipc", "ipc", "IPC 技术分类", "code"), ("country", "country", "申请人来源国家", "country")]
    specs.extend((dimension, dimension, DIMENSION_LABELS[dimension], "id") for dimension in DIMENSIONS)
    for kind, dimension, title, key in specs:
        values = themes[kind] if kind in DIMENSIONS else summary[kind]
        rows = [{"value": str(row[key]), "label": row.get("label", str(row[key])), "count": row["count"]} for row in values]
        rows = rows if kind == "yearly" else sorted(rows, key=lambda row: (-row["count"], row["label"]))
        displayed = rows if kind == "yearly" else rows[:15]
        charts[kind] = {"kind": kind, "option": build_chart_options({"title": title, "rows": displayed, "kind": kind}),
                        "data": {"title": title, "dimension": dimension, "rows": displayed, "all_rows": rows, "unit": "件申请"},
                        "selections": {"dimension": dimension, "values": dict(theme_selections[kind]) if kind in DIMENSIONS else selections[kind]}}
    for dimension, title in (("annual_theme", "年度 × 技术方法"), ("subject_matrix", "主体 × 技术方法（前20申请人）")):
        charts[dimension] = {"kind": dimension, **heatmap_snapshot(dimension, title,
            [patent.model_dump(mode="json") for patent in patents], assignments, themes["methods"])}
    for kind, chart in charts.items():
        chart.update(id=stable_id("chart:" + kind), version_id=stable_id("chart:" + kind + ":1"),
                     provenance=deepcopy(provenance), number=1, data_sha256=data_hash(chart["data"]), interpretation={})
    publication_map = defaultdict(list)
    if publications is not None:
        for publication in publications:
            application_id = publication.get("application_id") if isinstance(publication, dict) else publication.application_id
            number = publication.get("publication_number") if isinstance(publication, dict) else publication.publication_number
            publication_map[application_id].append(number)
    else:
        for patent in patents:
            publication_map[patent.id] = [row["publication_number"] for row in patent.publications]
    evidence = [{"id": stable_id("evidence:" + patent.id), "application_id": patent.id, "kind": "patent",
                 "content": {"application_id": patent.id, "application_number": patent.application_number,
                    "publication_numbers": sorted(publication_map[patent.id]),
                    "title": patent.title, "abstract": patent.abstract, "source_sheet": patent.source_sheet,
                    "source_row": patent.source_row, "dataset_version_id": patent.dataset_version_id,
                    "semantic_evidence": deepcopy(assignments[patent.id]["assignment"]["evidence"])}}
                for patent in patents]
    evidence_ids = [item["id"] for item in evidence]

    def ready(data, kinds):
        return {"status": "ready", "data": data, "chart_ids": [charts[kind]["id"] for kind in kinds], "evidence_ids": list(evidence_ids)}

    reviews = [{"application_id": application_id, "reasons": row["assignment"]["review_reasons"],
                "descriptions": [DIMENSION_LABELS.get(reason.split(":")[0], "分类") + "：" +
                    REVIEW_LABELS.get(reason.split(":")[-1], "需要复核") for reason in row["assignment"]["review_reasons"]]}
               for application_id, row in assignments.items() if row["status"] == "review"]
    findings = [{"id": "finding-" + theme["id"], "text": f"{theme['label']}：样本内 {theme['count']} 条申请涉及该方法。",
                 "theme_id": theme["id"], "application_ids": theme_selections["methods"][theme["id"]]}
                for theme in sorted(themes["methods"], key=lambda theme: (-theme["count"], theme["id"]))[:4]]
    views = {
        "overview": ready({**summary, "findings": findings, "directions": directions,
            "risks": [risk for direction in directions for risk in direction["risks"]],
            "actions": [action for direction in directions for action in direction["actions"]]}, list(charts)),
        "technology": ready({"classification": "semantic", "taxonomy": themes, "taxonomy_version": taxonomy["version"],
            "unmapped_labels": taxonomy.get("unmapped_labels", {}), "coverage": coverage, "review_records": reviews,
            "memberships": [{"application_id": application_id, **row} for application_id, row in assignments.items()],
            "ipc": summary["ipc"]}, ["elements", "methods", "scenarios", "ipc"]),
        "landscape": ready({"yearly": summary["yearly"], "applicants": summary["applicants"], "theme_dimension": "methods",
            "annual_theme": [{"year": year, "theme_id": theme_id, "count": count} for (year, theme_id), count in sorted(annual.items())],
            "subject_matrix": [{"applicant": applicant, "theme_id": theme_id, "count": count} for (applicant, theme_id), count in sorted(matrix.items())]},
            ["yearly", "applicants", "methods", "annual_theme", "subject_matrix"]),
        "opportunities": ready({"items": directions, "reason": None if directions else "当前没有同时具备可靠方法与应用场景归属的组合，需先复核分类。"}, []),
        "roadmap": ready({"items": directions, "reason": None if directions else "尚未形成可靠候选方向，暂不能生成对应阶段行动。"}, []),
        "geography": ready({"country": summary["country"], "counting_method": "共同来源国家全计数，统计申请人来源，不等于保护地",
            "unavailable": {"province": "样本没有省市或详细地址", "protection": "缺少有效保护地及法律状态", "family": "没有完整同族数据"}}, ["country"]),
    }
    return {"analysis_id": analysis_id, "patent_ids": calculated["patent_ids"], "summary": summary,
            "views": deepcopy(views), "charts": list(charts.values()), "evidence": evidence}


def classify_patents(records, generate_callable, hooks=None):
    """Run the same extraction, evidence checks, taxonomy and directions without application state."""
    patents = [PatentRecord.model_validate(record) for record in records]
    if not patents:
        raise CapabilityError(422, "NO_SELECTED_PATENTS", "当前范围没有可分析的申请，请调整范围")
    if len({patent.id for patent in patents}) != len(patents):
        raise CapabilityError(422, "DUPLICATE_PATENT_ID", "待分类记录的标识不能重复")
    execution = hooks or InlineExecution()
    extractions = {}
    for offset in range(0, len(patents), BATCH_SIZE):
        execution.check_cancelled()
        batch = patents[offset:offset + BATCH_SIZE]
        fingerprint = _digest([text_hash(patent) for patent in batch])[:16]
        raw = execution.step(f"semantic_extract_{offset}_{fingerprint}", lambda: extract_records(batch, generate_callable))
        extractions.update({patent.id: validate_extraction(patent, raw[patent.id]) for patent in batch})
        execution.emit("analysis.progress", {"processed_count": len(extractions), "total_count": len(patents),
                                             "cached_count": 0, "stage": "extract"})
    labels = taxonomy_labels(extractions)
    if any(labels.values()):
        results = {}
        for dimension in DIMENSIONS:
            execution.check_cancelled()
            results[dimension] = execution.step(
                "taxonomy_" + dimension + "_" + _digest(labels[dimension])[:16],
                lambda dimension=dimension: group_dimension(dimension, labels, generate_callable))
            execution.emit("analysis.progress", {"stage": "taxonomy", "completed_dimensions": len(results),
                                                 "total_dimensions": 3, "dimension": dimension})
        taxonomy = assemble_taxonomy(labels, results)
    else:
        taxonomy = {"version": _digest(labels), "themes": {dimension: [] for dimension in DIMENSIONS}, "mapping": {}}
    assignments = assign_themes(extractions, taxonomy)
    coverage = {"processed_count": len(assignments),
                "classified_count": sum(row["status"] == "classified" for row in assignments.values()),
                "review_count": sum(row["status"] == "review" for row in assignments.values()),
                "total_count": len(patents), "taxonomy_version": taxonomy["version"]}
    return {"source": classification_source(patents), "extractions": extractions, "taxonomy": taxonomy, "assignments": assignments,
            "directions": candidate_directions(patents, assignments, taxonomy), "coverage": coverage}
