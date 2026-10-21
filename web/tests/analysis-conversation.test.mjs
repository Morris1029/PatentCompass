import test from "node:test";
import assert from "node:assert/strict";
import { startAnalysisConversation } from "../src/lib/analysis-conversation.ts";

test("analysis from an older research stays in that research main conversation", async () => {
  const calls = [];
  const target = { id: "project", research_id: "v1", main_conversation_id: "v1-main", conversation_id: "unrelated-chat", current_research_id: "v2" };
  const task = await startAnalysisConversation(target, false, "submission", async (...args) => {
    calls.push(args); return { run_id: "run", message_id: "message" };
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/conversations/v1-main/messages");
  assert.equal(calls[0][1].research_id, "v1");
  assert.equal(calls[0][1].action, "analyze");
  assert.equal(calls[0][1].semantic_mode, "off");
  assert.equal(task.conversation_id, "v1-main");
});

test("a missing main conversation is ensured before the full analysis request", async () => {
  const calls = [];
  await startAnalysisConversation({ id: "project", research_id: "v1" }, true, "submission", async (...args) => {
    calls.push(args); return calls.length === 1 ? { conversation_id: "main" } : { run_id: "run" };
  });
  assert.deepEqual(calls.map(call => call[0]), ["/api/research-versions/v1/conversation", "/api/conversations/main/messages"]);
  assert.equal(calls[1][1].semantic_mode, "full");
  assert.equal(calls[1][1].research_id, "v1");
  assert.deepEqual(calls.map(call => call[2]), ["submission", "submission"]);
});

test("analysis without research creates an ordinary conversation in the same project", async () => {
  const calls = [];
  const task = await startAnalysisConversation({ id: "project", main_conversation_id: "stale-main" }, false, "submission", async (...args) => {
    calls.push(args); return { run_id: "run", conversation_id: "ordinary" };
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/projects/project/conversations");
  assert.equal(calls[0][1].initial_message.action, "analyze");
  assert.equal(calls[0][1].initial_message.research_id, undefined);
  assert.equal(task.conversation_id, "ordinary");
});

test("retry after a lost conversation response reuses the same creation key", async () => {
  const keys = []; const requests = [];
  const submit = async (path, body, key) => {
    keys.push(key); requests.push([path, body]);
    if (keys.length === 1) throw new Error("response lost");
    return { run_id: "same-run", conversation_id: "same-conversation" };
  };
  await assert.rejects(startAnalysisConversation({ id: "project" }, true, "stable-key", submit));
  const task = await startAnalysisConversation({ id: "project" }, true, "stable-key", submit);
  assert.deepEqual(keys, ["stable-key", "stable-key"]);
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(task.run_id, "same-run");
});
