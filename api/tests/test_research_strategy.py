from copy import deepcopy

import pytest
from sqlalchemy import select

from app import agent_contracts, chart_service, data_service, strategy_service
from app.errors import AppError
from app.models import AnalysisResult, Chart, Evidence, PatentApplication
from patentcompass_core.reports import build_report_content
from test_portable_agent import source, call, complete, context, start, submit_all
from test_agent_runtime import client, _plan, _run_message


def proposal(patent):
    return {"title": "视觉辅助移动平台着陆", "description": "验证视觉观测与移动平台着陆控制的适配边界。",
        "evidence": [{"application_id": patent.id, "quote": patent.abstract}],
        "risks": ["摘要不足以判断权利要求保护范围"], "conditions": ["核对原文和完整权利要求"],
        "phases": [{"title": "证据核查", "actions": ["核对原文和申请主体"], "deliverable": "可追溯证据表", "acceptance": "每条证据可定位到原始行"},
                   {"title": "方案验证", "actions": ["比较视觉方案与现有控制方法"], "deliverable": "验证方案及补检清单", "acceptance": "形成待验证指标及失败条件"}]}


def test_original_agent_saves_strategy_without_reclassifying_or_changing_statistics(client, db_session, source, context_factory, monkeypatch):
    base_result = data_service.analyze(context_factory(source))
    base = db_session.get(AnalysisResult, base_result["analysis_id"])
    old_views, old_summary = deepcopy(base.views), deepcopy(base.summary)
    baseline_report = build_report_content({"project_name": source.name, "analysis_id": base.id, "scope": {}, "summary": base.summary,
        "views": base.views, "sources": [], "charts": [], "evidence": [], "representatives": []})
    assert baseline_report["blocks"]  # Statistical-only historical snapshots keep exporting before any strategy exists.
    patent = db_session.scalar(select(PatentApplication).where(PatentApplication.id.in_(base.patent_ids)))

    def script():
        yield "load_agent_skill", {"name": "patent-research-strategy"}
        yield "prepare_agent_action", _plan("analyze", semantic_mode="off", research_intent="continue")
        context = yield "get_agent_context", {"request": {"project_id": source.id}}
        task = yield "save_research_strategy", {"request": {"project_id": source.id,
            "expected_revision": context["context"]["revision"], "analysis_id": base.id,
            "agent_model": "controlled-host", "directions": [proposal(patent)]}, "idempotency_key": "strategy"}
        assert "error" not in task, task
        result = yield "get_run", {"run_id": task["run_id"]}
        assert result["status"] == "succeeded", result
        return "候选方向和阶段路线已保存，真实统计保持不变。"

    sent, state, director, confirmations = _run_message(client, source, None, "完善候选和路线图，不重新分类", monkeypatch, script())
    assert confirmations >= 1
    revised = db_session.get(AnalysisResult, state["result"]["analysis_id"])
    assert revised.id != base.id and revised.revision_of_id == base.id
    assert revised.summary == old_summary and base.views == old_views
    db_session.expire(revised)
    assert revised.views["opportunities"]["status"] == revised.views["roadmap"]["status"] == "ready"
    item = revised.views["roadmap"]["data"]["items"][0]
    assert item["support_count"] == 1 and len(item["phases"]) == 2
    assert revised.views["technology"]["data"]["classification"] == "IPC"
    originals = list(db_session.scalars(select(Chart).where(Chart.analysis_id == base.id)))
    copies = list(db_session.scalars(select(Chart).where(Chart.analysis_id == revised.id)))
    assert len(copies) == len(originals) > 0
    assert {row.kind: row.data for row in copies} == {row.kind: row.data for row in originals}
    evidence = [{"id": row.id, "application_id": row.application_id, "content": row.content} for row in db_session.scalars(select(Evidence).where(Evidence.analysis_id == revised.id))]
    content = build_report_content({"project_name": source.name, "analysis_id": revised.id, "scope": {}, "summary": revised.summary,
        "views": revised.views, "sources": [], "charts": [], "evidence": evidence, "representatives": []})
    text = "\n".join(block.get("text", "") for block in content["blocks"])
    for expected in ["候选研究方向", "布局与路线图", "阶段交付物", "验收条件", patent.application_number]:
        assert expected in text


def test_strategy_rejects_wrong_quotes_and_foreign_ids_and_marks_authorized_mock(db_session, source, context_factory):
    result = data_service.analyze(context_factory(source))
    analysis = db_session.get(AnalysisResult, result["analysis_id"])
    patent = db_session.scalar(select(PatentApplication).where(PatentApplication.id.in_(analysis.patent_ids)))
    invalid = proposal(patent)
    invalid["evidence"][0]["quote"] = "invented evidence that does not occur in the original"
    with pytest.raises(AppError, match="原文"):
        strategy_service.build_directions(db_session, analysis, [agent_contracts.StrategyDirection.model_validate(invalid)], "model")
    invalid["evidence"][0]["application_id"] = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(AppError):
        strategy_service.build_directions(db_session, analysis, [agent_contracts.StrategyDirection.model_validate(invalid)], "model")
    mock = proposal(patent)
    mock.update(basis="mock", evidence=[])
    parsed = agent_contracts.StrategyDirection.model_validate(mock)
    with pytest.raises(AppError) as error:
        strategy_service.build_directions(db_session, analysis, [parsed], "model")
    assert error.value.code == "MOCK_NOT_AUTHORIZED"
    saved = strategy_service.build_directions(db_session, analysis, [parsed], "model", allow_mock=True)[0]
    assert saved["support_count"] == 0 and saved["supporting_application_ids"] == []
    assert "模拟" in saved["provenance"]["label"]


def test_strategy_preserves_semantic_memberships_for_chart_data_edits(db_session, source):
    task = start(db_session, source)
    analysis = complete(db_session, submit_all(db_session, task))
    base = db_session.get(AnalysisResult, analysis["analysis_id"])
    patent = db_session.get(PatentApplication, base.patent_ids[0])
    task = call(db_session, "save_research_strategy", {"project_id": source.id,
        "expected_revision": context(db_session, source)["revision"], "analysis_id": base.id,
        "agent_model": "controlled-host", "directions": [proposal(patent)]})
    saved = complete(db_session, task)
    chart = db_session.scalar(select(Chart).where(Chart.analysis_id == saved["analysis_id"], Chart.kind == "methods"))
    snapshot = chart_service.snapshot(db_session, chart)
    recomputed = chart_service._recount(db_session, chart, snapshot, {})
    assert recomputed["data"]["rows"] == snapshot["data"]["rows"]
    assert recomputed["data"]["rows"][0]["count"] == 2
