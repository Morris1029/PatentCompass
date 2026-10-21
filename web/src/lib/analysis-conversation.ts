type AnalysisTarget = { id: string; research_id?: string | null; main_conversation_id?: string | null };
type Submit = (path: string, body: unknown, key: string) => Promise<Record<string, any>>;

export async function startAnalysisConversation(project: AnalysisTarget, semantic: boolean, key: string, submit: Submit) {
  let conversationId = project.research_id ? project.main_conversation_id : null;
  if (project.research_id && !conversationId) {
    conversationId = (await submit(`/api/research-versions/${project.research_id}/conversation`, {}, key)).conversation_id;
  }
  const message = {
    content: semantic ? "请基于当前研究范围和数据完成完整语义分析。" : "请基于当前研究范围和数据完成统计分析，不做语义分类。",
    action: "analyze", semantic_mode: semantic ? "full" : "off",
    ...(project.research_id ? { research_id: project.research_id } : {}),
  };
  if (conversationId) {
    return { ...await submit(`/api/conversations/${conversationId}/messages`, message, key), conversation_id: conversationId };
  }
  return submit(`/api/projects/${project.id}/conversations`, { initial_message: message }, key);
}
