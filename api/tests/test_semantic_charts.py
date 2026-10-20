import copy
import json
from datetime import date

import pytest
from sqlalchemy import func, select

from app import chart_service, model_provider, semantic_service
from app.config import get_settings
from app.errors import AppError
from app.models import (AnalysisResult, Chart, ChartVersion, Dataset, DatasetVersion, PatentApplication,
                        PatentPublication, ResearchRun, SemanticCache, SemanticMembership, StoredFile)


@pytest.fixture
def semantic_data(db_session, project):
    source = StoredFile(owner_id=project.owner_id, kind="upload", key="upload/semantic.xlsx", original_name="semantic.xlsx",
                        sha256="e" * 64, size=1, media_type="application/octet-stream")
    dataset = Dataset(owner_id=project.owner_id, name="Semantic fixture", source_sha256="e" * 64)
    db_session.add_all([source, dataset])
    db_session.flush()
    version = DatasetVersion(dataset_id=dataset.id, source_file_id=source.id, status="ready",
                             quality={"application_count": 2}, source_metadata={"partial_results": True})
    db_session.add(version)
    db_session.flush()
    project.dataset_version_id = version.id
    patents = []
    for index, (year, applicants, text) in enumerate([
        (2022, ["Alpha", "Beta", "Alpha"], "A visual sensor enables autonomous landing of an unmanned aerial vehicle."),
        (2024, ["Beta"], "A visual sensor enables autonomous landing using a different trajectory controller.")
    ]):
        patent = PatentApplication(dataset_version_id=version.id, application_number=f"PCT/SEM/{index}",
                   application_date=date(year, 1, 1), title="Unmanned aerial vehicle control", abstract=text,
                   applicants=applicants, ipc_codes=["B64U10/00", "G05D1/00"], countries=["CN", "CN", "JP"] if index else ["CN"],
                   source_sheet="Patents", source_row=index + 2)
        db_session.add(patent)
        db_session.flush()
        db_session.add(PatentPublication(application_id=patent.id, dataset_version_id=version.id,
                       publication_number=f"WO202400000{index}A1", publication_date=date(2024, 1, 1), kind_code="A1"))
        patents.append(patent)
    db_session.flush()
    return patents


@pytest.fixture
def semantic_model(monkeypatch):
    settings = get_settings()
    for key, value in {"llm_base_url": "https://controlled.invalid/v1", "llm_api_key": "controlled", "llm_model": "controlled-v1"}.items():
        monkeypatch.setattr(settings, key, value)
    calls = []

    def chat(messages, tools=None):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if "records" in payload:
            records = []
            for row in payload["records"]:
                tag = lambda label, quote: [{"label": label, "field": "abstract", "quote": quote, "confidence": .95}]
                records.append({"id": row["id"], "elements": tag("视觉传感器", "visual sensor"),
                                "methods": tag("自主控制", "autonomous landing"), "scenarios": tag("自主降落", "autonomous landing")})
            return {"content": json.dumps({"records": records}, ensure_ascii=False)}
        return {"content": json.dumps({dimension: [{"label": item["label"], "members": [item["id"]]} for item in labels]
                                        for dimension, labels in payload.items()}, ensure_ascii=False)}

    monkeypatch.setattr(model_provider, "chat", chat)
    return calls


def test_full_classification_counts_every_application_and_reuses_cache(db_session, project, context_factory, semantic_data, semantic_model):
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert (result["processed_count"], result["classified_count"], result["review_count"], result["cached_count"]) == (2, 2, 0, 0)
    assert len(semantic_model) == 4
    analysis = db_session.get(AnalysisResult, result["analysis_id"])
    assert analysis.summary["country"] == [{"country": "CN", "count": 2}, {"country": "JP", "count": 1}]
    matrix = analysis.views["landscape"]["data"]["subject_matrix"]
    assert {row["applicant"]: row["count"] for row in matrix} == {"Alpha": 1, "Beta": 2}
    assert sum(row["count"] for row in analysis.views["landscape"]["data"]["annual_theme"]) == 2
    assert db_session.scalar(select(func.count()).select_from(SemanticMembership).where(SemanticMembership.analysis_id == analysis.id)) == 2
    assert db_session.scalar(select(func.count()).select_from(ChartVersion)) == 9
    direction = analysis.views["opportunities"]["data"]["items"][0]
    assert direction["support_count"] == 2
    assert direction["counterexample_search"]["examined_count"] == 2
    assert direction["external_validation"]["status"] == "not_performed"
    assert analysis.views["roadmap"]["data"]["items"][0]["id"] == direction["id"]
    second = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert second["cached_count"] == 2
    assert second["taxonomy_version"] == result["taxonomy_version"]
    assert len(semantic_model) == 4
    assert db_session.scalar(select(func.count()).select_from(SemanticCache)) == 6


def test_unverified_quotes_are_reviewed_not_counted_as_claimed_classification(db_session, project, context_factory, semantic_data, semantic_model, monkeypatch):
    original = model_provider.chat

    def invalid(messages, tools=None):
        message = original(messages, tools)
        payload = json.loads(message["content"])
        if "records" in payload:
            payload["records"][0]["methods"][0]["quote"] = "a fabricated original sentence"
        return {"content": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(model_provider, "chat", invalid)
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert result["processed_count"] == 2
    assert (result["classified_count"], result["review_count"]) == (1, 1)
    analysis = db_session.get(AnalysisResult, result["analysis_id"])
    assert len(analysis.views["technology"]["data"]["review_records"]) == 1
    assert sum(row["count"] for row in analysis.views["landscape"]["data"]["subject_matrix"]) in {1, 2}


def test_failed_batch_resume_reuses_completed_model_work(db_session, project, context_factory, semantic_data, semantic_model, monkeypatch):
    monkeypatch.setattr(semantic_service, "BATCH_SIZE", 1)
    original = model_provider.chat
    request_ids, fail = [], [True]

    def transient(messages):
        payload = json.loads(messages[-1]["content"])
        if "records" in payload:
            request_ids.append(payload["records"][0]["abstract"])
            if len(request_ids) == 2 and fail[0]:
                fail[0] = False
                raise AppError(502, "MODEL_REQUEST_FAILED", "Controlled transient failure")
        return original(messages)

    monkeypatch.setattr(model_provider, "chat", transient)
    context = context_factory(project, "analyze", {"semantic_mode": "full"})
    with pytest.raises(AppError):
        semantic_service.analyze(context)
    assert db_session.scalar(select(func.count()).select_from(SemanticCache)) == 1
    result = semantic_service.analyze(context)
    assert result["processed_count"] == 2
    assert request_ids == [semantic_data[1].abstract, semantic_data[0].abstract, semantic_data[0].abstract]


def test_missing_model_rows_are_repaired_without_fabricating_or_repeating_completed_rows(project, context_factory, semantic_data, semantic_model, monkeypatch):
    original = model_provider.chat
    requests = []

    def omission(messages):
        payload = json.loads(messages[-1]["content"])
        message = original(messages)
        if "records" in payload:
            requests.append(payload["records"])
            if len(requests) == 1:
                response = json.loads(message["content"])
                response["records"].pop()
                return {"content": json.dumps(response, ensure_ascii=False)}
        return message

    monkeypatch.setattr(model_provider, "chat", omission)
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert result["processed_count"] == result["classified_count"] == 2
    assert [len(request) for request in requests] == [2, 1]
    assert requests[1][0]["abstract"] == requests[0][1]["abstract"]


def test_model_options_and_source_text_invalidate_extraction_cache(project, context_factory, semantic_data, semantic_model, monkeypatch):
    first = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert first["cached_count"] == 0
    monkeypatch.setattr(get_settings(), "llm_extra_body", {"enable_thinking": True})
    changed = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert changed["cached_count"] == 0
    assert len(semantic_model) == 8
    before = semantic_service._cache_key(semantic_data[0], get_settings().llm_model)
    semantic_data[0].abstract += " A newly supplied exact source paragraph."
    assert semantic_service._cache_key(semantic_data[0], get_settings().llm_model) != before


def test_identical_original_text_is_extracted_once_but_both_applications_are_counted(db_session, project, context_factory, semantic_data, semantic_model):
    semantic_data[1].title = semantic_data[0].title
    semantic_data[1].abstract = semantic_data[0].abstract
    db_session.flush()
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert len(semantic_model[0]["records"]) == 1
    assert result["processed_count"] == result["classified_count"] == 2
    assert result["cached_count"] == 1
    assert db_session.scalar(select(func.count()).select_from(SemanticCache)) == 5
    assert db_session.scalar(select(func.count()).select_from(SemanticMembership)) == 2


def test_taxonomy_omissions_remain_explicit_review_records(project, context_factory, semantic_data, semantic_model, db_session, monkeypatch):
    original = model_provider.chat

    def omitted_mapping(messages):
        message = original(messages)
        payload = json.loads(message["content"])
        if "scenarios" in payload:
            payload["scenarios"] = []
        return {"content": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(model_provider, "chat", omitted_mapping)
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert result["processed_count"] == result["review_count"] == 2
    analysis = db_session.get(AnalysisResult, result["analysis_id"])
    technology = analysis.views["technology"]["data"]
    assert technology["unmapped_labels"]["scenarios"] == ["自主降落"]
    assert all("scenarios:unmapped" in row["reasons"] for row in technology["review_records"])
    assert all(row["descriptions"] for row in technology["review_records"])
    assert analysis.views["opportunities"]["data"]["items"] == []
    assert analysis.views["opportunities"]["data"]["reason"]


def test_failed_taxonomy_dimension_reuses_successful_dimension_caches(project, context_factory, semantic_data, semantic_model, db_session, monkeypatch):
    from collections import Counter
    original = model_provider.chat
    attempts, fail = [], [True]

    def transient_dimension(messages):
        payload = json.loads(messages[-1]["content"])
        if "records" not in payload:
            dimension = next(iter(payload))
            attempts.append(dimension)
            if dimension == "elements" and fail[0]:
                fail[0] = False
                raise AppError(502, "MODEL_REQUEST_FAILED", "Controlled dimension failure")
        return original(messages)

    monkeypatch.setattr(model_provider, "chat", transient_dimension)
    context = context_factory(project, "analyze", {"semantic_mode": "full"})
    with pytest.raises(AppError):
        semantic_service.analyze(context)
    assert db_session.scalar(select(func.count()).select_from(SemanticCache)) == 4
    result = semantic_service.analyze(context)
    assert result["processed_count"] == result["classified_count"] == 2
    assert Counter(attempts) == {"elements": 2, "methods": 1, "scenarios": 1}


def test_missing_model_and_missing_batch_record_never_return_success(project, context_factory, semantic_data, monkeypatch):
    with pytest.raises(AppError) as error:
        semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert error.value.code == "MODEL_NOT_CONFIGURED"
    for key in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(get_settings(), key, "controlled")
    monkeypatch.setattr(model_provider, "chat", lambda messages: {"content": '{"records":[]}'})
    with pytest.raises(AppError) as error:
        semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    assert error.value.code == "SEMANTIC_COVERAGE_MISMATCH"


def test_classification_correction_creates_new_analysis_and_preserves_original(db_session, project, context_factory, semantic_data, semantic_model, monkeypatch):
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    base = db_session.get(AnalysisResult, result["analysis_id"])
    frozen = copy.deepcopy(base.views)
    for key in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(get_settings(), key, "")
    revised = semantic_service.revise_analysis(context_factory(project, "revise_analysis", {
        "base_analysis_id": base.id, "expected_current_analysis_id": base.id,
        "corrections": [{"application_id": semantic_data[0].id, "dimension": "methods", "theme_ids": []}]}))
    db_session.expire_all()
    assert db_session.get(AnalysisResult, base.id).views == frozen
    updated = db_session.get(AnalysisResult, revised["analysis_id"])
    assert updated.revision_of_id == base.id
    assert updated.summary["semantic"]["model"] == "controlled-v1"
    matrix = updated.views["landscape"]["data"]["subject_matrix"]
    assert {row["applicant"]: row["count"] for row in matrix} == {"Beta": 1}
    assert revised["review_count"] == 1
    with pytest.raises(AppError) as error:
        semantic_service.revise_analysis(context_factory(project, "revise_analysis", {
            "base_analysis_id": base.id, "expected_current_analysis_id": base.id,
            "corrections": [{"application_id": semantic_data[1].id, "dimension": "methods", "theme_ids": []}]}))
    assert error.value.code == "ANALYSIS_VERSION_CONFLICT"


def test_chart_history_style_data_adoption_and_stale_rejection(db_session, project, context_factory, semantic_data, semantic_model):
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    chart = db_session.scalar(select(Chart).where(Chart.analysis_id == result["analysis_id"], Chart.kind == "yearly"))
    initial = chart_service.snapshot(db_session, chart)
    style_ctx = context_factory(project, "revise_chart", {"chart_id": chart.id, "base_version_id": initial["version_id"],
                   "expected_current_version_id": initial["version_id"], "mode": "style", "option": {"series": [{"type": "line", "itemStyle": {"color": "#0d7376"}}]}})
    style = chart_service.revise_chart(style_ctx)
    assert chart_service.revise_chart(style_ctx)["chart_version_id"] == style["chart_version_id"]
    db_session.expire_all()
    styled = chart_service.snapshot(db_session, chart)
    assert styled["number"] == 2
    assert styled["data_sha256"] == initial["data_sha256"]
    assert styled["option"]["series"][0]["type"] == "line"
    assert chart_service.snapshot(db_session, chart, initial["version_id"])["option"] == initial["option"]
    filtered = chart_service.revise_chart(context_factory(project, "revise_chart", {
        "chart_id": chart.id, "base_version_id": style["chart_version_id"], "expected_current_version_id": style["chart_version_id"],
        "mode": "data", "filters": {"start": 2024}}))
    db_session.expire_all()
    data_version = chart_service.snapshot(db_session, chart)
    assert data_version["data"]["rows"] == [{"value": "2024", "label": "2024", "count": 1}]
    assert data_version["provenance"]["application_ids"] == [semantic_data[1].id]
    restored = chart_service.revise_chart(context_factory(project, "revise_chart", {
        "chart_id": chart.id, "base_version_id": filtered["chart_version_id"], "expected_current_version_id": filtered["chart_version_id"],
        "mode": "data", "filters": {}}))
    db_session.expire_all()
    assert len(chart_service.snapshot(db_session, chart)["data"]["rows"]) == 2
    with pytest.raises(AppError) as error:
        chart_service.adopt(db_session, chart, initial["version_id"], filtered["chart_version_id"])
    assert error.value.code == "CHART_VERSION_CONFLICT"
    chart_service.adopt(db_session, chart, initial["version_id"], restored["chart_version_id"])
    assert chart.version_id == initial["version_id"]
    assert db_session.scalar(select(func.count()).select_from(ChartVersion).where(ChartVersion.chart_id == chart.id)) == 4


def test_chart_style_cannot_change_series_data(project, context_factory, semantic_data, semantic_model, db_session):
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    chart = db_session.scalar(select(Chart).where(Chart.analysis_id == result["analysis_id"], Chart.kind == "yearly"))
    with pytest.raises(AppError) as error:
        chart_service.revise_chart(context_factory(project, "revise_chart", {"chart_id": chart.id,
            "base_version_id": chart.version_id, "expected_current_version_id": chart.version_id,
            "mode": "style", "option": {"series": [{"data": [999]}]}}))
    assert error.value.code == "INVALID_CHART_STYLE"


def test_chart_insight_uses_frozen_version_evidence(project, context_factory, semantic_data, semantic_model, db_session, monkeypatch):
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    chart = db_session.scalar(select(Chart).where(Chart.analysis_id == result["analysis_id"], Chart.kind == "yearly"))
    base = chart_service.snapshot(db_session, chart)
    captured = []

    def insight(messages):
        captured.append(json.loads(messages[-1]["content"]))
        return {"content": "两个年份各有一条样本申请，不代表总体趋势。"}

    monkeypatch.setattr(model_provider, "chat", insight)
    result = chart_service.revise_chart(context_factory(project, "revise_chart", {"chart_id": chart.id,
        "base_version_id": base["version_id"], "expected_current_version_id": base["version_id"],
        "mode": "insight", "instruction": "解释年份分布"}))
    db_session.expire_all()
    version = chart_service.snapshot(db_session, chart)
    assert version["data_sha256"] == base["data_sha256"]
    assert version["interpretation"]["base_version_id"] == base["version_id"]
    assert len(captured[0]["evidence"]) == 2
    assert "option" not in captured[0]
    assert result["number"] == 2


def test_chart_insight_rejects_empty_tool_only_or_oversized_model_content(project, context_factory, semantic_data, semantic_model, db_session, monkeypatch):
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    chart = db_session.scalar(select(Chart).where(Chart.analysis_id == result["analysis_id"], Chart.kind == "yearly"))
    original_version = chart.version_id
    for content in (None, "  ", "x" * 40001):
        monkeypatch.setattr(model_provider, "chat", lambda messages, content=content: {
            "content": content, "tool_calls": [{"id": "unexpected", "type": "function", "function": {"name": "unused", "arguments": "{}"}}]})
        with pytest.raises(AppError) as error:
            chart_service.revise_chart(context_factory(project, "revise_chart", {"chart_id": chart.id,
                "base_version_id": original_version, "expected_current_version_id": original_version,
                "mode": "insight", "instruction": "解释年份分布"}))
        assert error.value.code == "INVALID_MODEL_RESPONSE"
    db_session.expire_all()
    assert chart.version_id == original_version
    assert db_session.scalar(select(func.count()).select_from(ChartVersion).where(ChartVersion.chart_id == chart.id)) == 1


@pytest.mark.parametrize("mode", ["semantic", "statistical"])
def test_analysis_retry_reuses_artifact_committed_before_checkpoint_completion(db_session, project, context_factory, semantic_data, semantic_model, monkeypatch, mode):
    from app import data_service
    analyze = semantic_service.analyze if mode == "semantic" else data_service.analyze
    key = "persist_semantic_analysis" if mode == "semantic" else "persist_analysis"
    first = context_factory(project, "analyze", {"semantic_mode": "full" if mode == "semantic" else "off"})
    original = first.step

    def gap(step_key, callback):
        if step_key == key:
            callback()
            raise AppError(500, "CONTROLLED_CHECKPOINT_GAP", "Committed before checkpoint completion")
        return original(step_key, callback)

    monkeypatch.setattr(first, "step", gap)
    with pytest.raises(AppError) as error:
        analyze(first)
    assert error.value.code == "CONTROLLED_CHECKPOINT_GAP"
    original_analysis = db_session.scalar(select(AnalysisResult).where(AnalysisResult.run_id == first.run_id))
    assert original_analysis
    retry = context_factory(project, "analyze", dict(first.payload))
    db_session.get(ResearchRun, retry.run_id).retry_of_id = first.run_id
    db_session.flush()
    result = analyze(retry)
    assert result["analysis_id"] == original_analysis.id
    assert db_session.scalar(select(func.count()).select_from(AnalysisResult)) == 1


def test_heatmap_versions_and_filters_preserve_joint_membership_counts(db_session, project, context_factory, semantic_data, semantic_model):
    result = semantic_service.analyze(context_factory(project, "analyze", {"semantic_mode": "full"}))
    charts = {row.kind: row for row in db_session.scalars(select(Chart).where(Chart.analysis_id == result["analysis_id"]))}
    matrix = charts["subject_matrix"]
    base = chart_service.snapshot(db_session, matrix)
    assert base["option"]["series"][0]["type"] == "heatmap"
    rows = base["data"]["rows"]
    assert {row["applicant"]: row["count"] for row in rows} == {"Alpha": 1, "Beta": 2}
    for row in rows:
        ids = base["selections"]["values"][row["value"]]
        assert len(ids) == row["count"]
        assert all(row["applicant"] in patent.applicants for patent in semantic_data if patent.id in ids)
    style = chart_service.revise_chart(context_factory(project, "revise_chart", {"chart_id": matrix.id,
        "base_version_id": base["version_id"], "expected_current_version_id": base["version_id"],
        "mode": "style", "option": {"visualMap": {"inRange": {"color": ["#fff", "#0d7376"]}}}}))
    db_session.expire_all()
    assert chart_service.snapshot(db_session, matrix)["option"]["visualMap"]["inRange"]["color"] == ["#fff", "#0d7376"]
    filtered = chart_service.revise_chart(context_factory(project, "revise_chart", {"chart_id": matrix.id,
        "base_version_id": style["chart_version_id"], "expected_current_version_id": style["chart_version_id"],
        "mode": "data", "filters": {"start": 2024}}))
    db_session.expire_all()
    version = chart_service.snapshot(db_session, matrix)
    assert version["number"] == 3
    assert {row["applicant"]: row["count"] for row in version["data"]["rows"]} == {"Beta": 1}
    assert chart_service.snapshot(db_session, matrix, base["version_id"])["data"] == base["data"]
    with pytest.raises(AppError):
        chart_service.revise_chart(context_factory(project, "revise_chart", {"chart_id": matrix.id,
            "base_version_id": filtered["chart_version_id"], "expected_current_version_id": filtered["chart_version_id"],
            "mode": "style", "option": {"series": [{"type": "bar"}]}}))
    annual = chart_service.snapshot(db_session, charts["annual_theme"])
    assert {row["year"]: row["count"] for row in annual["data"]["rows"]} == {2022: 1, 2024: 1}
