import test from "node:test";
import assert from "node:assert/strict";
import { conversationArtifacts } from "../src/lib/conversation-artifacts.ts";

test("repeated completion events produce one card per actual report version", () => {
  const activities = [
    { id: "child", status: "succeeded", report_version_id: "v1", report_id: "report" },
    { id: "poll", status: "succeeded", report_version_id: "v1" },
    { id: "generate_report", status: "succeeded", report_version_id: "v1" },
    { id: "revision", status: "succeeded", report_version_id: "v2" },
  ];
  const result = conversationArtifacts(activities, [{ type: "artifact", report_version_id: "v1" }]);
  assert.equal(result.reports.length, 2);
  assert.deepEqual(result.reports.map(item => item.report_version_id), ["v1", "v2"]);
  assert.equal(result.reports[0].report_id, "report");
});

test("a saved export link is retained when its report version already has an activity", () => {
  const result = conversationArtifacts([{ status: "succeeded", report_version_id: "v1" }], [
    { type: "artifact", report_version_id: "v1", file_id: "word", download_url: "/api/files/word" },
    { type: "task", run_id: "task" },
  ]);
  assert.equal(result.fallback.length, 1);
  assert.equal(result.reports.length, 1);
  assert.equal(result.reports[0].file_id, "word");
  assert.equal(result.reports[0].download_url, "/api/files/word");
});
