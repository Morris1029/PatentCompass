import type { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
const METHODS_WITH_BODY = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function forward(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const base = process.env.PATENTCOMPASS_API_URL || "http://127.0.0.1:8000";
  const target = new URL(`/api/${path.map(encodeURIComponent).join("/")}`, base);
  target.search = request.nextUrl.search;
  const headers = new Headers(request.headers);
  for (const name of ["host", "connection", "transfer-encoding", "content-length"]) headers.delete(name);
  try {
    const init: RequestInit & { duplex?: "half" } = { method: request.method, headers, cache: "no-store", signal: request.signal };
    if (METHODS_WITH_BODY.has(request.method) && request.body) { init.body = request.body; init.duplex = "half"; }
    const response = await fetch(target, init);
    const outgoing = new Headers(response.headers);
    for (const name of ["content-encoding", "content-length", "transfer-encoding", "connection"]) outgoing.delete(name);
    outgoing.set("Cache-Control", "no-store");
    outgoing.set("X-Accel-Buffering", "no");
    return new Response(response.body, { status: response.status, headers: outgoing });
  } catch {
    return Response.json({ code: "API_UNAVAILABLE", message: "后端暂时无法连接，请检查本地 API 服务。" }, { status: 502 });
  }
}
export { forward as GET, forward as POST, forward as PUT, forward as PATCH, forward as DELETE, forward as HEAD };
