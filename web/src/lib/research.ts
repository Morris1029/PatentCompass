export type ResearchRevision = { research_id?: string | null; research_number?: number | null; created_at?: string | null };

export function withResearchContext(project: Record<string, any>, research?: Record<string, any>, context = research?.context): Record<string, any> {
  if (!research) return project;
  return { ...project, ...context, research_id: research.id, research_number: research.number, main_conversation_id: research.main_conversation_id, current_report_version_id: research.current_revision_id ?? research.current_version_id ?? context?.current_report_version_id ?? null };
}

export type ResearchChoice = "new" | "existing";
export type ResearchAssignment = {
  intent: "continue" | "target" | "new" | "choose";
  reason?: string; topic?: string; recommended: ResearchChoice | null;
  existing_research_id?: string | null; existing_number?: number | null; existing_title?: string | null; existing_conversation_id?: string | null;
  new_uses_current_conversation?: boolean; allow_new: boolean; allow_existing: boolean;
};

export function researchAssignmentChoice(assignment?: ResearchAssignment, selected?: ResearchChoice): ResearchChoice | undefined {
  if (!assignment) return undefined;
  const allowed = (choice?: ResearchChoice | null) => choice === "new" ? assignment.intent !== "target" && assignment.allow_new : choice === "existing" && assignment.allow_existing;
  if (assignment.intent === "target") return assignment.allow_existing ? "existing" : undefined;
  if (allowed(selected)) return selected;
  if (assignment.intent === "choose") return undefined;
  return allowed(assignment.recommended) ? assignment.recommended! : undefined;
}

export function researchLabel(revision: ResearchRevision): string {
  return typeof revision.research_number === "number" && revision.research_number > 0 ? `V${revision.research_number}` : "未编号研究";
}

export function revisionLabel(revision: ResearchRevision): string {
  const date = revision.created_at ? new Date(revision.created_at) : undefined;
  const time = date && !Number.isNaN(date.getTime()) ? date.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }) : "修改记录";
  return `${researchLabel(revision)} · ${time}`;
}

export function reportComparisonLabel(current: ResearchRevision, previous: ResearchRevision) {
  const crossResearch = !!current.research_id && !!previous.research_id && current.research_id !== previous.research_id;
  return { title: crossResearch ? "研究版本对比" : "修改记录对比", before: crossResearch ? researchLabel(previous) : revisionLabel(previous), after: crossResearch ? researchLabel(current) : revisionLabel(current) };
}

export function orderReportComparison<T extends ResearchRevision>(current: T, previous: T): { current: T; previous: T } {
  const crossResearch = !!current.research_id && !!previous.research_id && current.research_id !== previous.research_id;
  return crossResearch && typeof current.research_number === "number" && typeof previous.research_number === "number" && current.research_number < previous.research_number ? { current: previous, previous: current } : { current, previous };
}
