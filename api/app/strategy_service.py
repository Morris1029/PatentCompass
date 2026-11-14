"""Persist evidence-checked agent research proposals as a new analysis version."""

from copy import deepcopy
import hashlib
import unicodedata

from sqlalchemy import select

from app import agent_contracts
from app.db import session_scope
from app.errors import AppError
from app.models import AnalysisResult, Chart, Evidence, PatentApplication, new_id
from app.resource_service import owned


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def build_directions(session, analysis, proposals, agent_model, allow_mock=False):
    rows = session.scalars(select(PatentApplication).where(PatentApplication.id.in_(analysis.patent_ids))).all()
    patents = {row.id: row for row in rows}
    result = []
    for index, proposal in enumerate(proposals):
        if proposal.basis == "mock" and not allow_mock:
            raise AppError(422, "MOCK_NOT_AUTHORIZED", "模拟方案需要明确允许，并且不能计入真实专利证据")
        if proposal.basis == "evidence" and not proposal.evidence:
            raise AppError(422, "STRATEGY_EVIDENCE_REQUIRED", "真实候选方向至少需要一条原文证据")
        if proposal.basis == "mock" and proposal.evidence:
            raise AppError(422, "MOCK_EVIDENCE_CONFLICT", "模拟方案不能附带伪装为真实支持的证据")
        evidence = []
        for evidence_index, item in enumerate(proposal.evidence):
            row = patents.get(str(item.application_id))
            if not row or _text(item.quote) not in _text(f"{row.title}\n{row.abstract}"):
                label = row.application_number if row else str(item.application_id)
                raise AppError(422, "STRATEGY_EVIDENCE_MISMATCH",
                    f"方向 {index + 1} 的证据 {evidence_index + 1}（{label}）未匹配原文。请读取该申请，复制标题或摘要连续原文，保留括号、部件编号和标点。")
            evidence.append({"application_id": row.id, "application_number": row.application_number, "quote": item.quote})
        supported = sorted({item["application_id"] for item in evidence})
        identifier = "strategy-" + hashlib.sha256((proposal.title + str(index)).encode()).hexdigest()[:16]
        phases = [{"id": f"{identifier}-phase-{i + 1}", "title": phase.title,
                   "actions": [{"id": f"{identifier}-{i}-{j}", "title": title, "owner": "待分配", "status": "pending"}
                               for j, title in enumerate(phase.actions)],
                   "conditions": [phase.acceptance], "deliverable": phase.deliverable, "acceptance": phase.acceptance}
                  for i, phase in enumerate(proposal.phases)]
        result.append({"id": identifier, "title": proposal.title, "description": proposal.description,
            "priority": f"P{min(index + 1, 3)}", "priority_reason": "Agent 建议的核查顺序，尚未验证商业价值或可专利性。",
            "basis": proposal.basis, "provenance": {"kind": proposal.basis, "agent_model": agent_model,
                "source_analysis_id": analysis.id, "label": "模拟方案（不计入真实专利证据）" if proposal.basis == "mock" else "原文支持的研究建议"},
            "support_count": len(supported), "supporting_application_ids": supported, "evidence": evidence,
            "conditions": proposal.conditions,
            "risks": [{"id": f"{identifier}-risk-{i}", "title": text, "status": "pending_validation"} for i, text in enumerate(proposal.risks)],
            "phases": phases, "actions": [action for phase in phases for action in phase["actions"]],
            "counterexample_search": {"status": "not_performed", "results": [], "interpretation": "尚未执行针对本方案的反例检索。"},
            "external_validation": {"status": "not_performed", "reason": "待补充权利要求、法律状态、同族和外部检索；当前为研究建议。"}})
    return result


def persist_strategy(ctx):
    def persist():
        from app.agent_service import check_context
        from app.data_service import _analysis_result, _update_analysis_context
        with session_scope() as session:
            ctx.guard(session)
            check_context(session, ctx)
            base = owned(session, AnalysisResult, ctx.payload["analysis_id"])
            if base.project_id != ctx.project_id or base.scope_version_id != ctx.scope_version_id or base.dataset_version_ids != ctx.dataset_version_ids:
                raise AppError(409, "STRATEGY_CONTEXT_CHANGED", "方案的分析、范围和数据必须一致")
            proposals = [agent_contracts.StrategyDirection.model_validate(value) for value in ctx.payload["directions"]]
            items = build_directions(session, base, proposals, ctx.payload["agent_model"], ctx.payload.get("allow_mock", False))
            views = deepcopy(base.views)
            revised = AnalysisResult(id=new_id(), project_id=ctx.project_id, run_id=ctx.run_id,
                scope_version_id=base.scope_version_id, dataset_version_ids=list(base.dataset_version_ids),
                patent_ids=list(base.patent_ids), summary=deepcopy(base.summary), views={}, revision_of_id=base.id)
            session.add(revised)
            session.flush()
            chart_ids, evidence_ids = {}, {}
            for row in session.scalars(select(Chart).where(Chart.analysis_id == base.id)):
                copy = Chart(id=new_id(), project_id=ctx.project_id, analysis_id=revised.id, kind=row.kind,
                    option=deepcopy(row.option), data=deepcopy(row.data), provenance=deepcopy(row.provenance), selections=deepcopy(row.selections))
                session.add(copy)
                chart_ids[row.id] = copy.id
            for row in session.scalars(select(Evidence).where(Evidence.analysis_id == base.id)):
                content = deepcopy(row.content)
                if content.get("chart_id") in chart_ids:
                    content["chart_id"] = chart_ids[content["chart_id"]]
                copy = Evidence(id=new_id(), project_id=ctx.project_id, analysis_id=revised.id,
                    application_id=row.application_id, kind=row.kind, content=content)
                session.add(copy)
                evidence_ids[row.id] = copy.id
            for view in views.values():
                view["chart_ids"] = [chart_ids.get(value, value) for value in view.get("chart_ids", [])]
                view["evidence_ids"] = [evidence_ids.get(value, value) for value in view.get("evidence_ids", [])]
            strategy_evidence = []
            for item in items:
                row = Evidence(id=new_id(), project_id=ctx.project_id, analysis_id=revised.id, kind="research_strategy", content=item)
                session.add(row)
                strategy_evidence.append(row.id)
                for original in item["evidence"]:
                    patent = session.get(PatentApplication, original["application_id"])
                    row = Evidence(id=new_id(), project_id=ctx.project_id, analysis_id=revised.id,
                        application_id=patent.id, kind="patent", content={**original, "direction_id": item["id"], "title": patent.title,
                        "abstract": original["quote"], "source_sheet": patent.source_sheet, "source_row": patent.source_row,
                        "dataset_version_id": patent.dataset_version_id})
                    session.add(row)
                    strategy_evidence.append(row.id)
            for name in ("opportunities", "roadmap"):
                views[name] = {"status": "ready", "data": {"items": items, "reason": None,
                    "coverage": {"basis": "selected_original_evidence", "total_count": len(base.patent_ids),
                                 "evidence_count": len({value for item in items for value in item["supporting_application_ids"]}),
                                 "mock_count": sum(item["basis"] == "mock" for item in items)},
                    "boundary": "候选方案基于核验原文整理，不等于全量语义分类或已证实技术空白。"},
                    "chart_ids": [], "evidence_ids": strategy_evidence}
            overview = views.get("overview", {}).get("data")
            if overview is not None:
                overview.update(directions=items, risks=[risk for item in items for risk in item["risks"]],
                    actions=[action for item in items for action in item["actions"]])
            revised.views = views
            _update_analysis_context(session, ctx, revised, expected_analysis_id=base.id)
            ctx.guard(session)
            session.flush()
            return _analysis_result(session, revised)
    result = ctx.step("persist_research_strategy", persist)
    ctx.emit("analysis.ready", result)
    return result
