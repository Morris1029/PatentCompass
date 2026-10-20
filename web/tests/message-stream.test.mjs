import test from "node:test";
import assert from "node:assert/strict";
import { EMPTY_MESSAGE_STREAM, updateMessageStream } from "../src/lib/message-stream.ts";
import { reportOrderChanged, sameReportValue } from "../src/lib/report-diff.ts";

test("stream handles emoji offsets, repeated frames and newer snapshots without duplicating text", () => {
  let state = updateMessageStream(EMPTY_MESSAGE_STREAM, { type: "message.started", seq: 1, payload: { stream_id: "a" } });
  state = updateMessageStream(state, { type: "message.delta", seq: 2, payload: { stream_id: "a", offset: 0, delta: "你好🧭" } });
  state = updateMessageStream(state, { type: "message.delta", seq: 3, payload: { stream_id: "a", offset: 3, delta: "，继续" } });
  state = updateMessageStream(state, { type: "message.delta", seq: 3, payload: { stream_id: "a", offset: 3, delta: "，继续" } });
  assert.equal(state.content, "你好🧭，继续");
  state = updateMessageStream(state, { type: "snapshot", seq: 4, payload: { stream_id: "a", content: "你好🧭，继续回答", status: "streaming" } });
  const newer = state;
  state = updateMessageStream(state, { type: "snapshot", seq: 2, payload: { stream_id: "a", content: "你好🧭", status: "streaming" } });
  assert.deepEqual(state, newer);
});

test("reset removes a tool candidate and a completed response remains recoverable after reconnect", () => {
  let state = updateMessageStream(EMPTY_MESSAGE_STREAM, { type: "snapshot", seq: 3, payload: { stream_id: "candidate", content: "先看看", status: "streaming" } });
  state = updateMessageStream(state, { type: "message.reset", seq: 4, payload: { stream_id: "candidate" } });
  assert.equal(state.content, "");
  state = updateMessageStream(state, { type: "message.completed", seq: 8, payload: { stream_id: "answer", content: "**完整回答**", message_id: "message" } });
  assert.equal(state.content, "**完整回答**");
  assert.equal(state.status, "completed");
  assert.equal(state.message_id, "message");
  assert.equal(updateMessageStream(state, { type: "snapshot", seq: 8, payload: { stream_id: "answer", status: "failed" } }).status, "failed");
});

test("a missing delta waits for the durable snapshot instead of appending a broken fragment", () => {
  assert.deepEqual(updateMessageStream(EMPTY_MESSAGE_STREAM, { type: "message.delta", seq: 4, payload: { stream_id: "a", offset: 20, delta: "尾段" } }), EMPTY_MESSAGE_STREAM);
});

test("report comparisons ignore object key order and insertion positions, but detect relative moves", () => {
  assert.equal(sameReportValue({ text: "内容", layout: { width: 50, align: "left" } }, { layout: { align: "left", width: 50 }, text: "内容" }), true);
  assert.equal(reportOrderChanged([{ id: "a" }, { id: "b" }], [{ id: "new" }, { id: "a" }, { id: "b" }]), false);
  assert.equal(reportOrderChanged([{ id: "a" }, { id: "b" }], [{ id: "b" }, { id: "a" }]), true);
});
