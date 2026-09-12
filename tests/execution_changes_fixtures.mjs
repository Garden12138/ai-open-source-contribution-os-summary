// Offline display fixtures; these are not real sandbox acceptance evidence.
export const inventory = {
  changed_paths: ["app/中文 file.py", "empty.txt", "old.txt", "picture.bin", "script.sh"],
  baseline_inventory: [
    {path:"app/中文 file.py",size:30,sha256:"old",executable:false},
    {path:"old.txt",size:4,sha256:"old",executable:false},
    {path:"picture.bin",size:4,sha256:"old",executable:false},
    {path:"script.sh",size:4,sha256:"same",executable:false},
  ],
  result_inventory: [
    {path:"app/中文 file.py",size:60,sha256:"new",executable:false},
    {path:"empty.txt",size:0,sha256:"empty",executable:false},
    {path:"picture.bin",size:4,sha256:"new",executable:false},
    {path:"script.sh",size:4,sha256:"same",executable:true},
  ],
};
export const diff = `diff --git a/app/中文 file.py b/app/中文 file.py
--- a/app/中文 file.py
+++ b/app/中文 file.py
@@ -2,2 +2,3 @@
 unchanged
-old
+new
+<img src=x onerror=alert(1)>
@@ -20 +21 @@
-last
\\ No newline at end of file
+end
\\ No newline at end of file
diff --git a/empty.txt b/empty.txt
new file mode 100644
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
diff --git a/picture.bin b/picture.bin
Binary files a/picture.bin and b/picture.bin differ
diff --git a/script.sh b/script.sh
old mode 100644
new mode 100755
`;

export function executionDetail(id = "execution-fixture") {
  const entry = (role, artifactId) => ({role, artifact_id:artifactId,
    content_url:`/api/v1/executions/${id}/artifacts/${artifactId}`, size_bytes:100, media_type:role === "unified-diff" ? "text/x-diff" : "application/json"});
  return {id, attempt_number:1, repository_full_name:"fixture/repo", current_stage:{stage:"verify", status:"running"},
    stages:[], stage_runs:[{stage:"verify",stage_run_number:1,job:{state:"running"}}], reviews:[],
    artifact_manifests:[{stage:"implement", execution_attempt_id:id, manifest_hash:"c".repeat(64), entries:[
      entry("unified-diff", "a".repeat(64)), entry("file-inventory", "b".repeat(64)),
    ]}]};
}
