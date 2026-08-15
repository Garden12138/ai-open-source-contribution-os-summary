# Git safety policy

P5-T18 applies `sandbox-git-safety-v1` to every container that can observe an
untrusted repository. The policy is a required field of `SandboxPolicy`, so its
version is included in every policy hash and signed JobSpec. A caller cannot
remove or substitute it by requesting a weaker policy.

Explore and Implement materialize exact Git archives without copying `.git`
metadata or invoking Git, hooks, filters, LFS, or submodules. The same fixed
environment is still installed defensively on their containers. Dependency
preparation and Verify pass the complete exact environment into any child
package-manager or signed test process and fail closed if a required value is
missing.

## Fixed process configuration

System and user configuration are disabled with
`GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`. System attributes
are disabled, interactive prompts and askpass programs point to a fixed false
executable, SSH execution is disabled, optional locks are off, LFS smudge is
skipped, and repository discovery cannot cross the bounded workspace,
dependency, or temporary roots.

Git's command-scope `GIT_CONFIG_COUNT` configuration has higher priority than
repository-local configuration and fixes all of the following:

- `core.hooksPath=/dev/null`, no template hooks, and no fsmonitor command;
- no SSH command, Git proxy, askpass, user attributes, or user excludes;
- an empty credential-helper chain with interactive credentials disabled;
- `protocol.allow=never`, including file, SSH, Git, HTTP, and HTTPS transports;
- recursive fetch, submodule, and push behavior disabled;
- LFS process/smudge/clean commands empty and non-required;
- HTTP redirects disabled.

These restrictions do not grant authorization to run Git. Verify commands must
still be exact signed argv from the approved plan, run with `network=none`, and
operate on a read-only repository. Later trusted Draft PR publication uses a
different Publisher trust domain and must construct its own narrowly scoped Git
policy; it must not weaken this Sandbox Worker policy.

## Acceptance

Deterministic tests freeze the complete setting list, reject a missing or
changed value, and prove Explore, Implement, dependency preparation, and Verify
Docker argv all install the same configuration.

A real macOS arm64 + OrbStack Verify run creates malicious Git state only under
the container's bounded `/tmp` and proves:

- a forged global alias is ignored;
- a locally installed executable `pre-commit` hook does not run;
- `file://` cloning is rejected by the protocol policy;
- a repository-local credential helper is not invoked;
- a repository-local LFS process filter is not invoked;
- the original Implement workspace remains read-only and inventory-identical.

Native Linux amd64 + Docker Engine execution remains part of P5-G06.
