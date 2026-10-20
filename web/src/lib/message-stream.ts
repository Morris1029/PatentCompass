export type MessageStream = { stream_id: string; content: string; status: string; seq: number; message_id?: string };
export const EMPTY_MESSAGE_STREAM: MessageStream = { stream_id: "", content: "", status: "reset", seq: 0 };
type StreamUpdate = { type: string; seq: number; payload: Partial<MessageStream> & { delta?: string; offset?: number } };

export function updateMessageStream(current: MessageStream, update: StreamUpdate): MessageStream {
  const { type, seq, payload } = update;
  if (!payload.stream_id || seq < current.seq) return current;
  if (seq === current.seq) return payload.stream_id === current.stream_id && ["failed", "cancelled"].includes(payload.status || "") ? { ...current, status: payload.status! } : current;
  if (type === "message.started") return { stream_id: payload.stream_id, content: "", status: "streaming", seq };
  if (type === "message.reset") return { stream_id: payload.stream_id, content: "", status: "reset", seq };
  if (type === "message.delta") {
    const content = current.stream_id === payload.stream_id ? current.content : "";
    const length = Array.from(content).length;
    const offset = payload.offset || 0;
    if (offset > length) return current;
    return { stream_id: payload.stream_id, content: content + Array.from(payload.delta || "").slice(length - offset).join(""), status: "streaming", seq };
  }
  return { stream_id: payload.stream_id, content: payload.content || "", status: payload.status || "completed", seq, message_id: payload.message_id };
}
