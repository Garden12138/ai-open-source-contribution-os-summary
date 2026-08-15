# Reproducible execution evidence

P5-T19 introduces `sandbox-verify-result-v2` as the command-evidence record for
an execution attempt. It is content-hashed and binds the exact repository/base
commit and archive, approved PlanVersion, Implement result, optional dependency
result, Runner image digest, complete SandboxPolicy version/hash, unchanged
workspace inventory, full ordered signed commands, and overall verdict.

Each command record contains:

- the exact command ID, argv, working directory, and canonical command hash;
- status and exit code;
- UTC start/completion timestamps and monotonic duration;
- child-process user/system CPU time, maximum RSS, page faults, and voluntary
  and involuntary context switches;
- exact raw stdout/stderr byte counts and SHA-256 hashes;
- bounded sanitized stdout/stderr text plus truncation/omission flags.

The trusted fixed supervisor writes command output only to bounded no-exec
tmpfs. If combined output exceeds the SandboxPolicy log budget, it returns only
byte counts and hashes and marks both raw logs omitted; oversized raw content
does not cross into the result. Otherwise, base64 is only an internal
container-to-Worker transport. The Worker verifies decoded byte counts and
hashes, redacts credential-shaped values over the complete decoded text, and
only then truncates each persisted log to 16,384 characters. Raw base64 never
appears in `VerifyCommandEvidence`, `VerifyResult`, business persistence, or API
data.

Not-run commands contain null timestamps, zero resource usage, empty-output
hashes, and no logs. Malformed timestamps, negative usage, status/exit
contradictions, altered captures, incomplete omission evidence, command/order
mismatches, and credential-bearing sanitized output fail closed.

Explore and Implement results bind the same exact base, image, policy, and
immutable predecessor chain; they execute only fixed Worker supervisors, not
repository commands. `sandbox-implement-result-v2` additionally carries both
complete sorted file/symlink inventories, their canonical hashes, counts and
bytes, the exact changed-path set, a deterministic unified diff, and its raw
SHA-256.

Each Verify command also produces one
`normalized-test-result-v1` derived from — and required to equal — the complete
command evidence. It retains the command identity/hash, normalized outcome,
exit code, timestamps, duration, and stdout/stderr byte counts and hashes while
excluding presentation logs and platform-specific resource counters. The
ordered normalized list has its own canonical `test_results_hash`; altered,
missing, reordered, or independently invented test results fail closed.

P5-T21 will atomically finalize these hashed records into the content-addressed
artifact store before stage success.

Real macOS arm64 + OrbStack acceptance records five signed commands, including
Git-safety probes, with UTC times and resource evidence. A credential-shaped
canary generated only inside the container is absent from the final result and
appears solely as `[REDACTED]`; the raw Implement inventory remains unchanged.
Native Linux amd64 + Docker Engine acceptance remains P5-G06.
