import test from "node:test";
import assert from "node:assert/strict";
import { researchLabel, revisionLabel, reportComparisonLabel, withResearchContext, researchAssignmentChoice, orderReportComparison, requiresReportBlocks } from "../src/lib/research.ts";

test("cross research comparison is older on the left even when browsing V1 and choosing V2", () => {
  const v1 = { id: "v1-snapshot", research_id: "r1", research_number: 1 };
  const v2 = { id: "v2-snapshot", research_id: "r2", research_number: 2 };
  const ordered = orderReportComparison(v1, v2);
  assert.equal(ordered.previous, v1);
  assert.equal(ordered.current, v2);
  assert.deepEqual(reportComparisonLabel(ordered.current, ordered.previous), { title: "研究版本对比", before: "V1", after: "V2" });
  assert.equal(orderReportComparison(v2, v1).current, v2);
});

test("same research and unknown research comparisons retain the explicitly selected snapshot direction", () => {
  const current = { id: "chosen", research_id: "r1", research_number: 1 };
  const previous = { id: "parent", research_id: "r1", research_number: 1 };
  assert.deepEqual(orderReportComparison(current, previous), { current, previous });
  const unknown = { id: "legacy", number: 99 };
  assert.deepEqual(orderReportComparison(current, unknown), { current, previous: unknown });
});

test("research assignment follows the backend intent rather than changed keywords or years", () => {
  assert.equal(researchAssignmentChoice(undefined), undefined);
  const assignment = { intent: "new", recommended: "new", allow_new: true, allow_existing: true };
  assert.equal(researchAssignmentChoice(assignment), "new");
  assert.equal(researchAssignmentChoice(assignment, "existing"), "existing");
  assert.equal(researchAssignmentChoice({ ...assignment, intent: "continue", recommended: "existing" }), "existing");
});

test("an ambiguous assignment requires an explicit allowed choice even when a recommendation exists", () => {
  const assignment = { intent: "choose", recommended: "new", allow_new: true, allow_existing: true };
  assert.equal(researchAssignmentChoice(assignment), undefined);
  assert.equal(researchAssignmentChoice(assignment, "new"), "new");
  assert.equal(researchAssignmentChoice(assignment, "existing"), "existing");
  assert.equal(researchAssignmentChoice({ ...assignment, allow_existing: false }, "existing"), undefined);
});

test("an explicit report target stays with its research and cannot become a new research", () => {
  const assignment = { intent: "target", recommended: "existing", allow_new: false, allow_existing: true, existing_number: 1 };
  assert.equal(researchAssignmentChoice(assignment), "existing");
  assert.equal(researchAssignmentChoice(assignment, "new"), "existing");
});

test("a confirmed assignment remains fixed when updated scope requires another confirmation", () => {
  const assignment = { intent: "choose", recommended: "new", allow_new: true, allow_existing: false, fixed: true };
  assert.equal(researchAssignmentChoice(assignment), "new");
  assert.equal(researchAssignmentChoice(assignment, "existing"), "new");
});

test("editing an older research uses its own scope, dataset, analysis and head without changing the adopted project", () => {
  const project = { id: "project", current_research_id: "v2", current_report_version_id: "v2-head", scope_version_id: "scope2", dataset_version_id: "data2", current_analysis_id: "analysis2", revision: 12 };
  const research = { id: "v1", number: 1, current_revision_id: "v1-head", main_conversation_id: "main1", context: { scope_version_id: "scope1", scope: { content: { q: "无人机" } }, dataset_version_id: "data1", current_analysis_id: "analysis1", revision: 3 } };
  const scoped = withResearchContext(project, research);
  assert.equal(scoped.id, "project");
  assert.equal(scoped.research_id, "v1");
  assert.equal(scoped.current_report_version_id, "v1-head");
  assert.equal(scoped.current_research_id, "v2");
  assert.equal(scoped.scope_version_id, "scope1");
  assert.equal(scoped.dataset_version_id, "data1");
  assert.equal(scoped.current_analysis_id, "analysis1");
  assert.equal(scoped.revision, 3);
  assert.equal(project.current_report_version_id, "v2-head");
});

test("an unpublished research never inherits the adopted report head or an allocated display number", () => {
  const scoped = withResearchContext({ current_report_version_id: "v1-head", current_analysis_id: "old" }, { id: "draft", number: null, status: "draft", current_revision_id: null, context: { current_analysis_id: null } });
  assert.equal(scoped.current_report_version_id, null);
  assert.equal(scoped.current_analysis_id, null);
  assert.equal(researchLabel(scoped), "未编号研究");
});

test("research numbers alone determine the public V label", () => {
  assert.equal(researchLabel({ research_number: 2, number: 87 }), "V2");
  assert.equal(researchLabel({ number: 87 }), "未编号研究");
  assert.equal(researchLabel({ research_number: null }), "未编号研究");
});

test("modification labels use local time rather than the immutable revision number", () => {
  const label = revisionLabel({ research_number: 1, created_at: "2026-10-20T08:30:45" });
  assert.match(label, /^V1 · /);
  assert.match(label, /2026.*10.*20/);
  assert.match(label, /08:30:45/);
  assert.equal(revisionLabel({ research_number: 1 }), "V1 · 修改记录");
  assert.equal(revisionLabel({ research_number: 1, created_at: "invalid" }), "V1 · 修改记录");
});

test("the same research compares modification records using their times", () => {
  const result = reportComparisonLabel({ research_id: "same", research_number: 1, created_at: "2026-10-20T09:30:00" }, { research_id: "same", research_number: 1, created_at: "2026-10-20T08:30:00" });
  assert.equal(result.title, "修改记录对比");
  assert.match(result.before, /08:30:00/);
  assert.match(result.after, /09:30:00/);
});

test("different known research versions retain the V1 to V2 comparison", () => {
  assert.deepEqual(reportComparisonLabel({ research_id: "new", research_number: 2 }, { research_id: "old", research_number: 1 }), { title: "研究版本对比", before: "V1", after: "V2" });
  assert.equal(reportComparisonLabel({}, {}).title, "修改记录对比");
});


test("the combined execution card requires target blocks only when updating an existing report", () => {
  const request = { kind: "execution_confirmation", select_blocks: true, blocks: [{ id: "block1", text: "目标段落" }] };
  assert.equal(requiresReportBlocks(request, "existing"), true);
  assert.equal(requiresReportBlocks(request, "new"), false);
  assert.equal(requiresReportBlocks(request), false);
  assert.equal(requiresReportBlocks({ ...request, select_blocks: false }, "existing"), false);
  assert.equal(requiresReportBlocks({ kind: "report_target" }, undefined, false), true);
  assert.equal(requiresReportBlocks({ kind: "report_target" }, undefined, true), false);
});
