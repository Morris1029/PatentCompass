import test from "node:test";
import assert from "node:assert/strict";
import { managementQuery, activeRunInterval } from "../src/lib/management.ts";

test("management navigation and close preserve the complete report or conversation context", () => {
  for (const original of [
    "project=p&conversation=c&research=r&view=chat&message=m&anchor=a",
    "project=p&research=r&view=report&version=historical&diff=before&anchor=block&analysis_id=old&chart_id=chart&patent=patent",
  ]) {
    const opened = managementQuery(original, { management: "overview" });
    const detail = managementQuery(opened, { management: "runs", management_run: "task", management_project: "p" });
    assert.equal(managementQuery(detail, { management: null }), original);
    assert.equal(new URLSearchParams(detail).get("management_run"), "task");
  }
});

test("management is available without a selected project and refreshable from its URL", () => {
  const url = managementQuery("", { management: "skills", management_skill: "patent-evidence-qa" });
  assert.equal(new URLSearchParams(url).get("management"), "skills");
  assert.equal(new URLSearchParams(url).get("project"), null);
  assert.equal(managementQuery(url, { management: null }), "");
});

test("active task inspection polls every five seconds and stops after completion", () => {
  for (const status of ["queued", "running", "cancelling", "waiting_input"]) assert.equal(activeRunInterval({ status }), 5000);
  for (const status of ["succeeded", "failed", "cancelled"]) assert.equal(activeRunInterval({ status }), 0);
  assert.equal(activeRunInterval(), 0);
});
