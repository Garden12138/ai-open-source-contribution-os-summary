import test from "node:test";
import assert from "node:assert/strict";
import {parseExecutionChanges, implementationArtifacts} from "../app/static/execution-changes.js";
import {diff, inventory, executionDetail} from "./execution_changes_fixtures.mjs";

test("multiple files and hunks retain exact line numbers, signs, paths and statuses", () => {
  const result = parseExecutionChanges(diff, inventory);
  assert.equal(result.malformed, false);
  assert.deepEqual(result.files.map(f => f.status), ["modified","added","deleted","modified","modified"]);
  const file = result.files[0];
  assert.equal(file.additions, 3); assert.equal(file.deletions, 2);
  assert.deepEqual(file.rows.filter(r => r.kind === "add").map(r => [r.oldLine,r.newLine]), [[null,3],[null,4],[null,21]]);
  assert.deepEqual(file.rows.filter(r => r.kind === "delete").map(r => [r.oldLine,r.newLine]), [[3,null],[20,null]]);
  assert.equal(file.rows.filter(r => r.kind === "note").length, 2);
  assert.equal(result.files[1].additions, 0); assert.equal(result.files[2].deletions, 1);
  assert.equal(result.files[3].additions, null); assert.equal(result.files[3].unavailable, true);
  assert.equal(result.files[4].modeChanged, true); assert.equal(result.files[4].additions, 0);
});
test("empty changes do not resemble a loading or parse failure", () => {
  assert.deepEqual(parseExecutionChanges("", {changed_paths:[], baseline_inventory:[], result_inventory:[]}).files, []);
  assert.throws(() => parseExecutionChanges("", {}));
});
test("invalid hunk counts and mismatched paths fall back to raw with no invented totals", () => {
  for (const broken of [diff.replace("-2,2 +2,3", "-2,2 +2,4"), diff.replace("--- a/", "--- wrong/"), diff + "garbage\n",
    diff.replace("@@ -20 +21 @@", "@@ -9007199254740992 +21 @@"),
    diff.replace("@@ -1 +0,0 @@\n-gone\n", "")]) {
    const result = parseExecutionChanges(broken, inventory);
    assert.equal(result.malformed, true);
    assert.equal(result.raw, broken);
    assert.ok(result.files.every(f => f.additions === null && f.deletions === null && !f.rows.length));
  }
});
test("header-like code remains source; CRLF code retains its carriage return", () => {
  const path = "source.ts";
  const inv = {changed_paths:[path],baseline_inventory:[],result_inventory:[{path,size:30,sha256:"new",executable:false}]};
  const patch = `diff --git a/${path} b/${path}\nnew file mode 100644\n--- /dev/null\n+++ b/${path}\n@@ -0,0 +1,3 @@\n+--- code\n++++ code\n+abc\r\n`;
  const result = parseExecutionChanges(patch, inv);
  assert.equal(result.malformed, false);
  assert.equal(result.files[0].additions, 3);
  assert.deepEqual(result.files[0].rows.slice(1).map(row => row.text), ["--- code","+++ code","abc\r"]);
});
test("never mix manifests, executions or arbitrary artifact URLs", () => {
  const detail = executionDetail();
  assert.ok(implementationArtifacts(detail).key.includes(detail.id));
  const bad = structuredClone(detail); bad.artifact_manifests[0].entries[0].content_url = "https://example.com";
  assert.throws(() => implementationArtifacts(bad));
  bad.artifact_manifests[0].execution_attempt_id = "different";
  assert.throws(() => implementationArtifacts(bad));
  const incomplete = structuredClone(detail); incomplete.artifact_manifests[0].entries.pop();
  assert.throws(() => implementationArtifacts(incomplete));
  assert.equal(implementationArtifacts({...detail,artifact_manifests:[]}), null);
});
