"""Patent filtering and ranking shared by direct callers and database adapters."""

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date

from .contracts import CapabilityError, PatentRecord
from .parsing import _date


KEYWORD_EXPANSIONS = {
    "无人机": ["unmanned aerial vehicle", "uav", "drone"],
    "自主": ["autonomous", "automatic"], "降落": ["landing", "land"],
    "起飞": ["takeoff", "take-off"], "避障": ["obstacle avoidance", "collision avoidance"],
    "导航": ["navigation", "navigating"], "电池": ["battery", "batteries"],
    "控制": ["control"], "通信": ["communication"], "路径规划": ["path planning", "route planning"],
}


def keyword_groups(query: str) -> list[list[str]]:
    groups = []
    remainder = query.lower()
    for term, variants in sorted(KEYWORD_EXPANSIONS.items(), key=lambda pair: -len(pair[0])):
        if term in remainder:
            groups.append([term, *variants])
            remainder = remainder.replace(term, " ")
    groups.extend([[word] for word in remainder.split() if word])
    return groups


def text_hash(record) -> str:
    patent = PatentRecord.model_validate(record)
    return hashlib.sha256(f"{patent.title}\n{patent.abstract}".encode()).hexdigest()


def _contains(text, query):
    # Match the existing database ILIKE containment semantics, including SQL wildcards.
    pattern = re.escape(str(query)).replace("%", ".*").replace("_", ".")
    return re.search(pattern, str(text), re.IGNORECASE | re.DOTALL) is not None


def validate_filters(filters):
    filters = dict(filters or {})
    allowed = {"industry", "goal", "need", "focus", "source_region", "coverage", "type", "patent_type",
               "q", "applicant", "ipc", "application_number", "publication_number", "start", "end",
               "application_start", "application_end", "application_ids", "dataset_version_ids",
               "mode", "candidate_limit", "limit", "offset"}
    if set(filters) - allowed:
        raise CapabilityError(422, "INVALID_SCOPE_FIELD", "检索范围含不支持的字段", sorted(set(filters) - allowed))
    if filters.get("type") or filters.get("patent_type"):
        raise CapabilityError(422, "PATENT_TYPE_UNAVAILABLE", "当前数据无法按专利类型筛选")
    if "candidate_limit" in filters:
        try:
            filters["candidate_limit"] = int(filters["candidate_limit"])
        except (ValueError, TypeError):
            raise CapabilityError(422, "INVALID_CANDIDATE_LIMIT", "候选数量需要整数")
    for key in ("q", "applicant", "ipc", "application_number", "publication_number"):
        if filters.get(key) is not None and (not isinstance(filters[key], str) or len(filters[key]) > 1000):
            raise CapabilityError(422, "INVALID_SCOPE_FILTER", "检索条件需要不超过 1000 字符的文本")
    for key in ("start", "end"):
        if filters.get(key) not in (None, ""):
            try:
                filters[key] = int(filters[key])
            except (ValueError, TypeError):
                raise CapabilityError(422, "INVALID_YEAR", "年份必须为整数")
            if not 1800 <= filters[key] <= 2200:
                raise CapabilityError(422, "INVALID_YEAR", "年份超出支持范围")
    if filters.get("start") and filters.get("end") and filters["start"] > filters["end"]:
        raise CapabilityError(422, "INVALID_DATE_RANGE", "开始年份不能晚于结束年份")
    for key in ("application_start", "application_end"):
        if filters.get(key):
            try:
                filters[key] = _date(filters[key]).isoformat()
            except (ValueError, TypeError):
                raise CapabilityError(422, "INVALID_DATE", "申请日期无效")
    if filters.get("application_start") and filters.get("application_end") and filters["application_start"] > filters["application_end"]:
        raise CapabilityError(422, "INVALID_DATE_RANGE", "申请开始日期不能晚于结束日期")
    for key in ("application_ids", "dataset_version_ids"):
        if key in filters and (not isinstance(filters[key], list) or any(not isinstance(value, str) for value in filters[key])):
            raise CapabilityError(422, "INVALID_SCOPE_FILTER", "记录与数据版本引用需要列表")
    return filters


def filter_records(records, filters=None, publications=None) -> list[PatentRecord]:
    filters = validate_filters(filters)
    patents = [PatentRecord.model_validate(row) for row in records]
    public_numbers = defaultdict(list)
    for row in publications or []:
        if isinstance(row, dict):
            public_numbers[row["application_id"]].append(row["publication_number"])
        else:
            public_numbers[row.application_id].append(row.publication_number)
    start = _date(filters["application_start"]) if filters.get("application_start") else None
    end = _date(filters["application_end"]) if filters.get("application_end") else None
    groups = keyword_groups(filters.get("q", ""))
    result = []
    for patent in patents:
        if "application_ids" in filters and patent.id not in filters["application_ids"]:
            continue
        if filters.get("dataset_version_ids") and patent.dataset_version_id not in filters["dataset_version_ids"]:
            continue
        if filters.get("start") and (not patent.application_date or patent.application_date.year < int(filters["start"])):
            continue
        if filters.get("end") and (not patent.application_date or patent.application_date.year > int(filters["end"])):
            continue
        if start and (not patent.application_date or patent.application_date < start):
            continue
        if end and (not patent.application_date or patent.application_date > end):
            continue
        if any(filters.get(key) and not _contains(json.dumps(getattr(patent, field), ensure_ascii=False), filters[key])
               for key, field in (("applicant", "applicants"), ("ipc", "ipc_codes"))):
            continue
        if filters.get("application_number") and not _contains(patent.application_number, filters["application_number"]):
            continue
        numbers = public_numbers[patent.id] + [row["publication_number"] for row in patent.publications]
        if filters.get("publication_number") and not any(_contains(number, filters["publication_number"]) for number in numbers):
            continue
        if not all(any(_contains(patent.title, term) or _contains(patent.abstract, term) for term in variants) for variants in groups):
            continue
        result.append(patent)
    return result


def keyword_score(record, query: str) -> int:
    patent = PatentRecord.model_validate(record)
    title, abstract = patent.title.lower(), patent.abstract.lower()
    return sum(max((3 * title.count(term) + min(abstract.count(term), 5) for term in variants), default=0)
               for variants in keyword_groups(query))


def reciprocal_rank_fusion(rankings, limit=500):
    fused = defaultdict(float)
    for ranked in rankings:
        for rank, record_id in enumerate(ranked[:limit], 1):
            fused[record_id] += 1 / (60 + rank)
    ordered = sorted(fused, key=lambda record_id: (-fused[record_id], record_id))[:limit]
    return ordered, {record_id: fused[record_id] for record_id in ordered}


def semantic_ranking(records, embeddings, query_vector, limit=500):
    patents = [PatentRecord.model_validate(row) for row in records]
    if not query_vector or not any(query_vector) or any(not math.isfinite(value) for value in query_vector):
        raise CapabilityError(422, "INVALID_QUERY_VECTOR", "查询向量需要有限的非零数值")
    norm = math.sqrt(sum(value * value for value in query_vector))
    scores = {}
    for patent in patents:
        vector = embeddings.get(patent.id)
        if vector is None:
            raise CapabilityError(409, "EMBEDDING_INDEX_NOT_READY", "当前数据版本的语义索引尚未就绪，请先创建索引任务")
        if len(vector) != len(query_vector):
            raise CapabilityError(409, "EMBEDDING_DIMENSION_MISMATCH", "模型维度已变化，请使用新的模型标识重建索引")
        if not any(vector) or any(not math.isfinite(value) for value in vector):
            raise CapabilityError(422, "INVALID_EMBEDDING", "专利向量需要有限的非零数值")
        denominator = norm * math.sqrt(sum(value * value for value in vector))
        scores[patent.id] = sum(a * b for a, b in zip(query_vector, vector)) / denominator
    ordered = sorted(patents, key=lambda patent: (-scores[patent.id], patent.id))[:limit]
    return ordered, {patent.id: scores[patent.id] for patent in ordered}


def search_patents(records, filters=None, *, scope_filters=None, publications=None, embeddings=None, query_vector=None):
    """Search supplied records without a database, model credentials, or application state."""
    filters = dict(filters or {})
    scoped = filter_records(records, scope_filters, publications)
    total = len(scoped)
    mode = filters.pop("mode", "keywords" if filters.get("q") else "field")
    if mode not in {"field", "keywords", "semantic", "hybrid"}:
        raise CapabilityError(422, "INVALID_SEARCH_MODE", "无效检索模式")
    query = filters.get("q", "")
    active = {key: value for key, value in filters.items() if key != "q"} if mode in {"semantic", "hybrid"} else filters
    candidates = filter_records(scoped, active, publications)
    scores = {}
    if mode in {"semantic", "hybrid"}:
        if not query.strip():
            raise CapabilityError(422, "SEMANTIC_QUERY_REQUIRED", "语义检索需要输入检索内容")
        limit = max(1, min(int(filters.get("candidate_limit", 500)), 500))
        items, scores = semantic_ranking(candidates, embeddings or {}, query_vector, limit)
        if mode == "hybrid":
            keywords = sorted(filter_records(candidates, {"q": query}, publications),
                              key=lambda patent: (-keyword_score(patent, query), patent.id))
            ordered, scores = reciprocal_rank_fusion([[p.id for p in items], [p.id for p in keywords]], limit)
            by_id = {patent.id: patent for patent in candidates}
            items = [by_id[record_id] for record_id in ordered]
    else:
        items = sorted(candidates, key=lambda patent: (-(patent.application_date or date.min).toordinal(), patent.id))
    return {"items": [patent.model_dump(mode="json") for patent in items], "scores": scores, "mode": mode,
            "candidate_count": len(items), "scope_application_count": total}
