import { Suspense } from "react";
import Workspace from "@/components/workspace";
export default function Page() { return <Suspense fallback={<main className="empty-state">正在打开研究工作台…</main>}><Workspace /></Suspense>; }
