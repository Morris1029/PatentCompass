type Artifact = Record<string, any>;
const resourceKeys = ["report_version_id", "analysis_id", "chart_version_id", "file_id"];

export function conversationArtifacts(activities: Artifact[], cards: Artifact[]) {
  const fallback = cards.filter(card => card.type !== "task" && resourceKeys.some(key => card[key]) &&
    !resourceKeys.filter(key => card[key]).every(key => activities.some(activity => activity[key] === card[key])));
  const reports = new Map<string, Artifact>();
  for (const item of [...activities.filter(activity => activity.status === "succeeded"), ...fallback]) {
    if (item.report_version_id) reports.set(item.report_version_id, { ...reports.get(item.report_version_id), ...item });
  }
  return { fallback, reports: [...reports.values()] };
}
