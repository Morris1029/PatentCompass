export const managementTabs = [
  { id: "overview", label: "总览" }, { id: "agent", label: "Agent" },
  { id: "mcp", label: "MCP" }, { id: "skills", label: "Skills" }, { id: "runs", label: "运行记录" },
];

export function managementQuery(search: string, changes: Record<string, string | null>) {
  const next = new URLSearchParams(search);
  if (changes.management === null) for (const key of [...next.keys()]) if (key === "management" || key.startsWith("management_")) next.delete(key);
  for (const [key, value] of Object.entries(changes)) value === null ? next.delete(key) : next.set(key, value);
  return next.toString();
}

export const activeRunInterval = (data?: { status?: string }) => data && ["queued", "running", "cancelling", "waiting_input"].includes(data.status || "") ? 5000 : 0;
