export type WorkspaceIconName = "overview" | "analysis" | "layout" | "data" | "report" | "scope" | "chat" | "check" | "clock";

export default function WorkspaceIcon({ name, size = 22 }: { name: WorkspaceIconName; size?: number }) {
  const paths = {
    overview: <><path d="m3 10 9-7 9 7M5 9v12h14V9" /><path d="M9 21v-8h6v8" /></>,
    analysis: <><path d="M3 21h18M5 17v-5h4v5M11 17V8h4v9M17 17V3h4v14" /></>,
    layout: <><circle cx="12" cy="5" r="3" /><circle cx="5" cy="19" r="3" /><circle cx="19" cy="19" r="3" /><path d="M12 8v4m0 0-7 4m7-4 7 4" /></>,
    data: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
    report: <><path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M9 12h6m-6 4h6" /></>,
    scope: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="2" fill="currentColor" /><circle cx="15" cy="17" r="2" fill="currentColor" /></>,
    chat: <path d="M21 11a9 9 0 0 1-9 9H4l-2 2V11a9 9 0 0 1 19 0ZM7 9h10M7 13h7" />,
    check: <path d="m5 12 4 4L19 6" />,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
