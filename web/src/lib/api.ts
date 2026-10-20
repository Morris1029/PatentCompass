"use client";
import useSWR, { mutate } from "swr";
import useSWRImmutable from "swr/immutable";

export type Json = Record<string, any>;
export type Page<T = Json> = { items: T[]; next_cursor?: string | null; total?: number; returned_count?: number; current_version_id?: string | null };
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) { super(message); }
}
export async function api<T = Json>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers, cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ message: `请求失败（${response.status}）` }));
    throw new ApiError(response.status, error.code || "HTTP_ERROR", error.message, error.details);
  }
  return response.json();
}
export const post = <T = Json>(path: string, body: unknown = {}, idempotencyKey?: string) => api<T>(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body), headers: { "Idempotency-Key": idempotencyKey || crypto.randomUUID() } });
export const put = <T = Json>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body), headers: { "Idempotency-Key": crypto.randomUUID() } });
export const patch = <T = Json>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body), headers: { "Idempotency-Key": crypto.randomUUID() } });
export function useApi<T = Json>(path: string | null, interval: number | ((data?: T) => number) = 0) {
  return useSWR<T>(path, api<T>, { refreshInterval: interval, revalidateOnFocus: true, errorRetryCount: 2 });
}
export function useReportVersion(reportId?: string, versionId?: string | null) {
  return useSWRImmutable<Json>(reportId && versionId ? `/api/reports/${reportId}/versions/${versionId}?view=display` : null, api<Json>, { errorRetryCount: 2 });
}
export function refresh() { return mutate(key => typeof key === "string" && key.startsWith("/api/") && !/^\/api\/reports\/[^/]+\/versions\/[^/?]+\?view=display$/.test(key)); }
export function describe(value: unknown): string {
  if (value === null || value === undefined) return "待确认";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(describe).join("；");
  if (typeof value === "object") return Object.entries(value).map(([key, val]) => `${key}：${describe(val)}`).join("\n");
  return String(value);
}
export const statusLabel: Record<string, string> = { queued: "等待执行", running: "正在执行", waiting_input: "等待确认", cancelling: "正在停止", cancelled: "已停止", succeeded: "已完成", failed: "执行失败", ready: "就绪", parsing: "解析中", pending: "待处理", interrupted: "中断后恢复" };
export const terminal = (status?: string) => ["succeeded", "failed", "cancelled"].includes(status || "");
