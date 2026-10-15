"""Exact, user-confirmed publication through a dedicated host gh process.

Preparation has only read-only GitHub operations. The API writes an immutable
confirmation/outbox before this worker can Fork, Push or create a Draft PR.
"""

from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from urllib.parse import urlencode, quote
from sqlalchemy import select
from app.audit import AuditService
from app.config import Settings
from app.executions import ExecutionAttemptService
from app.jobs import JobService
from app.models import (
    Job,
    PublishIntent,
    DraftPullRequest,
    PublishConfirmation,
    ReviewRun,
)
from app.provenance import canonical_json, content_hash
from app.publish_intents import (
    publish_intent_payload,
    draft_pull_request_payload,
    publish_confirmation_payload,
)
from app.reviews import ReviewRunService
from app.security import ensure_no_sensitive_data
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench, WorkbenchError
from app.workbench_worker import WorkbenchJobWorker

PREPARE = "publication_prepare"
WRITE = "publication_write"
ACTIONS = ["create_or_reuse_fork", "push_exact_branch", "create_draft_pr"]
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")


async def bounded_process(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: str | None = None,
    stdin: bytes | None = None,
    limit: int = 4_000_000,
    timeout: int = 120,
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=(
            asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def read(stream):
        output = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > limit:
                raise WorkbenchError("发布命令输出超过上限")

    async def feed():
        if stdin is not None:
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()

    try:
        async with asyncio.timeout(timeout):
            output, _, _ = await asyncio.gather(
                read(process.stdout), read(process.stderr), feed()
            )
            code = await process.wait()
            return code, output
    except BaseException:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise


class PublicationTransport(Protocol):
    async def api(
        self, method: str, path: str, body: dict | None = None, *, missing: bool = False
    ): ...
    async def push(
        self, directory: str, fork: str, branch: str, head_sha: str
    ) -> None: ...


class GhPublicationTransport:
    def __init__(self) -> None:
        # gh can use the host keychain/config; children never inherit application credentials.
        self.env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "TMPDIR")
            if k in os.environ
        }
        self.env.update(
            {
                "GH_PROMPT_DISABLED": "1",
                "GH_HOST": "github.com",
                "NO_COLOR": "1",
                "GH_PAGER": "cat",
            }
        )

    async def api(
        self, method: str, path: str, body: dict | None = None, *, missing: bool = False
    ):
        if method not in {"GET", "POST"} or not re.fullmatch(
            r"(?:user|repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./%?=&:+-]*)?)",
            path,
        ):
            raise WorkbenchError("不允许的 GitHub 请求")
        if method == "POST" and not re.fullmatch(
            r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:forks|pulls)", path
        ):
            raise WorkbenchError("Publisher 只允许创建 Fork 或 Draft PR")
        if (
            method == "POST"
            and path.endswith("/pulls")
            and (not body or body.get("draft") is not True)
        ):
            raise WorkbenchError("Publisher 只允许 Draft PR")
        return await self._request(method, path, body, missing=missing)

    async def cla_comment(self, repository: str, number: int, *, authorization: dict):
        """The sole comment capability: fixed, explicitly consented CLA text."""
        from app.cla import SIGNATURE, ACTION
        if (not REPO.fullmatch(repository) or type(number) is not int or number < 1
            or authorization.get("action") != ACTION
            or authorization.get("repository") != repository
            or authorization.get("number") != number
            or authorization.get("comment") != SIGNATURE
            or authorization.get("actor_id") != "local-user"
            or not re.fullmatch(r"[0-9a-f]{64}", authorization.get("authorization_hash", ""))):
            raise WorkbenchError("CLA 评论缺少精确授权", code="cla_unauthorized")
        return await self._request("POST", f"repos/{repository}/issues/{number}/comments", {"body": SIGNATURE})

    async def pr_text(self, repository: str, number: int, *, authorization: dict) -> dict:
        """Narrow metadata capability; generic API calls still prohibit PATCH."""
        from app.pr_updates import ACTION
        title, body = authorization.get("title"), authorization.get("body")
        if (not REPO.fullmatch(repository) or type(number) is not int or number < 1
            or authorization.get("action") != ACTION or authorization.get("actor_id") != "local-user"
            or authorization.get("repository") != repository or authorization.get("number") != number
            or not re.fullmatch(r"[a-f0-9]{64}", authorization.get("authorization_hash", ""))
            or not isinstance(title, str) or not 1 <= len(title) <= 200
            or not isinstance(body, str) or not 1 <= len(body) <= 7600):
            raise WorkbenchError("PR 文案更新缺少精确授权", code="pr_update_unauthorized")
        from app.security import ensure_no_sensitive_data
        ensure_no_sensitive_data({"title": title, "body": body}, context="PR update")
        return await self._request("PATCH", f"repos/{repository}/pulls/{number}",
            {"title": title, "body": body}, rejection_codes=(400, 401, 403, 404, 410, 422))

    async def pr_ready(self, repository: str, number: int, *, authorization: dict) -> dict:
        """Fixed GraphQL capability; callers cannot supply a mutation or query."""
        from app.pr_lifecycle import ACTION, READY_QUERY
        if (not REPO.fullmatch(repository) or type(number) is not int or number < 1
            or authorization.get('action') != ACTION or authorization.get('actor_id') != 'local-user'
            or authorization.get('repository') != repository or authorization.get('number') != number
            or not re.fullmatch('[a-f0-9]{64}', authorization.get('authorization_hash', ''))
            or not re.fullmatch(r'[A-Za-z0-9_=-]{8,256}', authorization.get('node_id', ''))
            or not SHA.fullmatch(authorization.get('head_sha', ''))):
            raise WorkbenchError('Ready for review 缺少精确授权', code='pr_ready_unauthorized')
        result = await self._request('POST', 'graphql', {'query': READY_QUERY, 'variables': {'input': {
            'pullRequestId': authorization['node_id'], 'clientMutationId': authorization['authorization_hash']}}},
            rejection_codes=(400,401,403,404,410,422), rejection_code='pr_ready_rejected')
        value = result.get('data', {}).get('markPullRequestReadyForReview') if isinstance(result, dict) and isinstance(result.get('data'), dict) else None
        pr = value.get('pullRequest') if isinstance(value, dict) else None
        if (not isinstance(result, dict) or result.get('errors') or not isinstance(pr, dict) or pr.get('id') != authorization['node_id']
            or pr.get('number') != number or pr.get('url') != f'https://github.com/{repository}/pull/{number}'
            or pr.get('headRefOid') != authorization['head_sha'] or pr.get('isDraft') is not False):
            raise WorkbenchError('GitHub 尚未确认转为审核的结果，请只读跟进核对', code='pr_ready_outcome_unknown')
        return pr

    async def _request(self, method: str, path: str, body: dict | None = None, *, missing: bool = False,
                       rejection_codes: tuple[int, ...] = (), rejection_code: str = "pr_update_rejected"):

        argv = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--include",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            path,
        ]
        if body is not None:
            argv.extend(["--input", "-"])
        code, raw = await bounded_process(
            argv,
            env=self.env,
            stdin=None if body is None else canonical_json(body).encode(),
        )
        normalized = raw.replace(b"\r\n", b"\n")
        header, _, content = normalized.partition(b"\n\n")
        match = re.match(rb"HTTP/[^ ]+ (\d+)", header)
        status = int(match[1]) if match else 0
        if missing and status == 404:
            return None
        if status in rejection_codes:
            raise WorkbenchError("GitHub 已拒绝本次操作，请检查权限或仓库要求后重新准备预览", code=rejection_code)
        if code != 0 or not 200 <= status < 300:
            raise WorkbenchError(
                "GitHub 请求失败或结果不明确；请检查发布任务后重试核对"
            )
        try:
            return json.loads(content)
        except (ValueError, UnicodeError):
            raise WorkbenchError("GitHub 返回了无效数据") from None

    async def push(self, directory: str, fork: str, branch: str, head_sha: str) -> None:
        if (
            not REPO.fullmatch(fork)
            or not SHA.fullmatch(head_sha)
            or not re.fullmatch(r"contribos/issue-[0-9]+-[a-f0-9-]+", branch)
        ):
            raise WorkbenchError("发布目标无效")
        code, raw = await bounded_process(
            ["gh", "auth", "token", "--hostname", "github.com"],
            env=self.env,
            limit=8192,
        )
        if code != 0 or not raw.strip():
            raise WorkbenchError("Publisher 的 gh 登录不可用")
        token = raw.strip().decode()
        with tempfile.TemporaryDirectory(prefix="contribos-push-env-") as empty_home:
            env = git_environment(empty_home)
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic "
                    + base64.b64encode(("x-access-token:" + token).encode()).decode(),
                }
            )
            code, _ = await bounded_process(
                git_argv()
                + [
                    "push",
                    "--porcelain",
                    "--no-verify",
                    "--recurse-submodules=no",
                    f"https://github.com/{fork}.git",
                    f"{head_sha}:refs/heads/{branch}",
                ],
                env=env,
                cwd=directory,
            )
        if code:
            raise WorkbenchError("Push 结果不明确；重试时将先核对远端分支")


def git_environment(empty_home: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": empty_home,
        "XDG_CONFIG_HOME": empty_home,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",
    }


def git_argv() -> list[str]:
    return [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "http.followRedirects=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "fetch.recurseSubmodules=false",
    ]


class TrustedCheckout:
    @asynccontextmanager
    async def prepare(self, intent: dict, patch: bytes, inventory: dict):
        if not REPO.fullmatch(intent["repository"]) or not SHA.fullmatch(
            intent["base_sha"]
        ):
            raise WorkbenchError("仓库或提交标识无效")
        if hashlib.sha256(patch).hexdigest() != intent["diff_hash"]:
            raise WorkbenchError("补丁哈希不匹配")
        scope = tuple(inventory.get("workspace_paths", ()))
        from app.sandbox_worker.specs import _relative_path

        try:
            if len(scope) > 64 or len(set(scope)) != len(scope):
                raise ValueError("Invalid scope")
            for path in scope:
                _relative_path(path, name="publication workspace path")
        except (TypeError, ValueError) as exc:
            raise WorkbenchError("发布工作区范围无效") from exc
        if scope or "workspace_paths" in intent:
            if (
                intent.get("workspace_paths") != list(scope)
                or intent.get("workspace_scope_version") != "source-tree-scope-v1"
                or intent.get("workspace_scope_hash") != content_hash(list(scope))
            ):
                raise WorkbenchError("发布工作区范围与执行授权不一致")

        def selected(path: str) -> bool:
            return not scope or any(path == p or path.startswith(p + "/") for p in scope)

        if any(not selected(path) for path in inventory["changed_paths"]):
            raise WorkbenchError("发布修改超出已批准的工作区范围")
        from app.sandbox_worker.implementation import FileInventoryEntry

        try:
            before = [FileInventoryEntry.from_wire(e) for e in inventory["baseline_inventory"]]
            after = [FileInventoryEntry.from_wire(e) for e in inventory["result_inventory"]]
        except ValueError as exc:
            raise WorkbenchError("发布文件清单格式无效") from exc
        links = {e.path: e.target for e in before if e.kind == "symlink"}
        if (
            links != {e.path: e.target for e in after if e.kind == "symlink"}
            or set(links).intersection(inventory["changed_paths"])
        ):
            raise WorkbenchError("发布不接受新增、修改或删除符号链接")
        with tempfile.TemporaryDirectory(prefix="contribos-publish-") as directory:
            env = git_environment(directory)

            async def git(*args: str, data: bytes | None = None) -> bytes:
                code, output = await bounded_process(
                    git_argv() + list(args), env=env, cwd=directory, stdin=data
                )
                if code:
                    raise WorkbenchError("受控 Git checkout、补丁或提交验证失败")
                return output

            await git("init", "--quiet", "--template=")
            await git(
                "fetch",
                "--no-tags",
                "--depth=1",
                f"https://github.com/{intent['repository']}.git",
                intent["base_sha"],
            )
            # Keep the entire immutable base in the index, but materialize only
            # the authorized sandbox scope. Unrelated trees remain original Git
            # objects; they must never be mistaken for sandbox-generated files.
            await git("read-tree", intent["base_sha"])
            paths = []
            found_links = set()
            for record in (await git("ls-files", "--stage", "-z")).split(b"\x00"):
                if not record:
                    continue
                header, raw_path = record.split(b"\t", 1)
                path = raw_path.decode("utf-8")
                if not selected(path):
                    continue
                mode, oid, stage = header.split()
                if mode == b"120000" and stage == b"0":
                    # A pre-existing reviewed link is retained only as an index
                    # object. Never create it or follow its target on disk.
                    target = await git("cat-file", "blob", oid.decode("ascii"))
                    if path not in links or target != links[path].encode("utf-8"):
                        raise WorkbenchError("发布符号链接与已审查基线不一致")
                    found_links.add(path)
                    continue
                if mode not in {b"100644", b"100755"} or stage != b"0":
                    raise WorkbenchError("发布工作区不接受符号链接或子模块")
                paths.append(raw_path)
                if len(paths) > 20000:
                    raise WorkbenchError("发布 checkout 超过容量")
            if found_links != set(links):
                raise WorkbenchError("发布符号链接清单不完整")
            if paths:
                await git(
                    "checkout-index", "--index", "-z", "--stdin",
                    data=b"\x00".join(paths) + b"\x00",
                )
            # Validate every materialized file, including unchanged scoped files.
            self.verify_inventory(directory, [e.to_wire() for e in before if e.kind == "file"])
            await git(
                "apply", "--check", "--index", "--whitespace=nowarn", "-", data=patch
            )
            await git("apply", "--index", "--whitespace=nowarn", "-", data=patch)
            self.verify_inventory(directory, [e.to_wire() for e in after if e.kind == "file"])
            changed = (
                (await git(
                    "diff", "--cached", "--name-only", "-z", "--no-renames",
                    intent["base_sha"], "--",
                ))
                .decode()
                .split("\x00")
            )
            if set(filter(None, changed)) != set(inventory["changed_paths"]):
                raise WorkbenchError("Git 修改文件与已审查清单不一致")
            tree = (await git("write-tree")).decode().strip()
            metadata = intent["commit_metadata"]
            env.update(
                {
                    "GIT_AUTHOR_NAME": metadata["name"],
                    "GIT_COMMITTER_NAME": metadata["name"],
                    "GIT_AUTHOR_EMAIL": metadata["email"],
                    "GIT_COMMITTER_EMAIL": metadata["email"],
                    "GIT_AUTHOR_DATE": metadata["date"],
                    "GIT_COMMITTER_DATE": metadata["date"],
                }
            )
            head = (
                await git(
                    "commit-tree",
                    tree,
                    "-p",
                    intent["base_sha"],
                    data=metadata["message"].encode(),
                )
            ).decode().strip()
            if not SHA.fullmatch(head) or (
                intent.get("head_sha") and intent["head_sha"] != head
            ):
                raise WorkbenchError("发布提交已变化")
            yield directory, head, tree

    @staticmethod
    def verify_inventory(directory: str, expected: list[dict]) -> None:
        wanted = {entry["path"]: entry for entry in expected}
        actual = set()
        total = 0
        for base, dirs, files in os.walk(directory, followlinks=False):
            if Path(base) == Path(directory):
                dirs[:] = [d for d in dirs if d != ".git"]
            for name in [*dirs, *files]:
                path = Path(base) / name
                relative = path.relative_to(directory).as_posix()
                if path.is_symlink():
                    raise WorkbenchError("发布 checkout 不接受符号链接")
                if path.is_dir():
                    continue
                entry = wanted.get(relative)
                if not entry or entry["type"] != "file":
                    raise WorkbenchError("发布 checkout 存在额外文件")
                size = path.stat().st_size
                total += size
                if size > 256_000_000 or total > 512_000_000 or len(actual) >= 20000:
                    raise WorkbenchError("发布 checkout 超过容量")
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(65536), b""):
                        digest.update(chunk)
                if (
                    size != entry["size"]
                    or digest.hexdigest() != entry["sha256"]
                    or bool(path.stat().st_mode & 0o111) != entry["executable"]
                ):
                    raise WorkbenchError("发布文件与已审查产物不一致")
                actual.add(relative)
        if actual != set(wanted):
            raise WorkbenchError("发布文件清单不完整")


def verified_review(wb: Workbench, review_id: str) -> ReviewRun:
    reviews = ReviewRunService(wb.session, wb.artifacts)
    review = reviews.get_verified(review_id)
    current = ContributionTaskStateService(wb.session).current(review.task_id)
    plan = wb.latest_plan(review.task_id)
    if review.reviewer_kind not in {"nvidia_nim", "openai_compatible", "minimax"}:
        raise WorkbenchError("演示审查不能用于真实发布")
    if (
        current.to_state != "ready"
        or review.verdict != "pass"
        or reviews.is_stale(review.id)
        or plan.id != review.plan_version_id
    ):
        raise WorkbenchError("Review 已过期或未通过，请重新执行和验收")
    # Re-read exact artifacts so disk corruption cannot reuse a passing review.
    wb.artifacts.read_bytes(review.diff_hash)
    tests = json.loads(wb.artifacts.read_bytes(review.test_results_hash))
    if (
        not isinstance(tests, list)
        or not tests
        or any(t.get("outcome") != "passed" or t.get("exit_code") != 0 for t in tests)
    ):
        raise WorkbenchError("验证未全部通过，禁止发布")
    if any(
        f.get("severity") in {"high", "blocking"} or f.get("verdict") == "block"
        for f in review.findings
    ):
        raise WorkbenchError("独立审查仍有阻断问题")
    return review


def request_publication(
    wb: Workbench, task_id: str, *, title: str, body: str, key: str,
    publisher_account_hash: str | None = None,
    document_only: bool = False, document_hash: str | None = None,
    template_path: str | None = None,
    commit: bool = True,
    user_request: str = "",
    conversation_message_id: str | None = None,
    renewal_source_hash: str | None = None,
) -> Job:
    kind = "publication_requirements" if document_only else PREPARE
    job_key = ("requirements:" if document_only else "prepare:") + key
    old = wb.session.scalar(select(Job).where(Job.kind == kind, Job.idempotency_key == job_key))
    if old:
        expected = {"task_id": task_id, "title": title, "body": body,
                    "publisher_account_hash": publisher_account_hash,
                    "document_hash": document_hash, "template_path": template_path,
                    "user_request": user_request or None, "conversation_message_id": conversation_message_id}
        expected["renewal_source_hash"] = renewal_source_hash
        if any(old.payload.get(k) != value for k, value in expected.items()):
            raise WorkbenchError("重复发布准备请求的内容不同")
        return old
    if publisher_account_hash:
        from app.publisher_accounts import require_account
        from app.cla import _lock
        _lock(wb)
        require_account(wb.session, publisher_account_hash)
    wb.assert_idle(task_id)
    if document_only and sum(j.kind == "publication_requirements" for j in wb.jobs(task_id)) >= 16:
        raise WorkbenchError("本任务已达到发布文案生成预算", code="publication_document_budget")
    if wb.latest(task_id, "publication_confirmed"):
        raise WorkbenchError("发布已经确认，只能继续核对原发布结果")
    review = wb.session.scalar(
        select(ReviewRun)
        .where(ReviewRun.task_id == task_id)
        .order_by(ReviewRun.created_at.desc())
        .limit(1)
    )
    if review is None:
        raise WorkbenchError("缺少独立审查")
    verified_review(wb, review.id)
    if document_hash:
        document = wb.latest(task_id, "publication_document_ready")
        if not document or document.record_hash != document_hash or document.payload["review_hash"] != review.record_hash:
            raise WorkbenchError("发布文案已过期，请重新生成", code="publication_document_stale")
    return wb.enqueue(
        task_id,
        kind,
        {
            "review_id": review.id,
            "review_hash": review.record_hash,
            "title": title,
            "body": body,
            **({"user_request": user_request} if user_request else {}),
            **({"conversation_message_id": conversation_message_id} if conversation_message_id else {}),
            **({"document_hash": document_hash} if document_hash else {}),
            **({"template_path": template_path} if template_path else {}),
            **({"publisher_account_hash": publisher_account_hash} if publisher_account_hash else {}),
            **({"renewal_source_hash": renewal_source_hash} if renewal_source_hash else {}),
        },
        key=job_key,
        actor="local-user",
        commit=commit,
    )


def renew_publication_preview(
    wb: Workbench, task_id: str, *, preview_hash: str, key: str,
    publisher_account_hash: str | None = None, commit: bool = True,
) -> Job:
    """Recheck the exact displayed draft without changing text or authorizing writes."""
    from app.cla import _lock
    from app.execution_acceptance import acceptance_view

    _lock(wb)
    old = wb.session.scalar(select(Job).where(Job.kind == PREPARE, Job.idempotency_key == "prepare:" + key))
    if old:
        if (old.payload.get("task_id") != task_id
                or old.payload.get("renewal_source_hash") != preview_hash
                or old.payload.get("publisher_account_hash") != publisher_account_hash):
            raise WorkbenchError("重复预览更新请求的内容不同")
        return old
    preview = wb.latest(task_id, "publication_prepared")
    if not preview or preview.record_hash != preview_hash:
        raise WorkbenchError("发布预览已变化，请查看最新预览后更新", code="publication_preview_stale")
    if not acceptance_view(wb, task_id).get("accepted"):
        raise WorkbenchError("请先确认代码、测试与审查结果")
    if preview.payload.get("publisher_account_hash") != publisher_account_hash:
        raise WorkbenchError("发布账号已变化，请重新准备发布预览", code="publication_account_stale")
    return request_publication(wb, task_id, title=preview.payload["title"], body=preview.payload["body"],
        document_hash=preview.payload.get("document_hash"), template_path=preview.payload.get("template_path"),
        publisher_account_hash=publisher_account_hash, renewal_source_hash=preview_hash, key=key, commit=commit)


def confirm_publication(
    wb: Workbench, task_id: str, *, intent_hash: str, nonce: str
) -> Job:
    from app.cla import _lock
    from app.conversation import require_settled_messages
    _lock(wb)
    require_settled_messages(wb, task_id)
    wb.assert_idle(task_id)
    event = wb.latest(task_id, "publication_prepared")
    if (
        event is None
        or event.record_hash != intent_hash
        or not secrets.compare_digest(event.payload["nonce"], nonce)
    ):
        raise WorkbenchError("发布内容或一次性确认无效")
    if wb.latest(task_id, "publication_confirmed"):
        raise WorkbenchError("发布已确认，请查看当前发布任务")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(
        event.payload["expires_at"]
    ):
        raise WorkbenchError("发布预览已过期，请重新准备", code="publication_preview_expired")
    review = verified_review(wb, event.payload["review_id"])
    account_hash = event.payload.get("publisher_account_hash")
    if account_hash:
        from app.publisher_accounts import require_account
        require_account(wb.session, account_hash)
    if review.record_hash != event.payload["review_hash"]:
        raise WorkbenchError("审查内容已变化")
    intent = materialize_intent(wb, event, review, datetime.now(timezone.utc))
    job, _ = JobService(wb.session).enqueue(
        kind=WRITE,
        idempotency_key="publish:" + event.id,
        payload={
            "task_id": task_id,
            "intent_id": event.id,
            "intent_hash": intent_hash,
            "publish_intent_id": intent.id,
            "publish_intent_hash": intent.record_hash,
            **({"publisher_account_hash": account_hash} if account_hash else {}),
        },
        max_attempts=3,
        timeout_seconds=600,
        commit=False,
    )
    wb.append(
        task_id,
        "publication_confirmed",
        {"intent_hash": intent_hash, "actions": ACTIONS},
        key="confirm:" + event.id,
        actor="local-user",
        job_id=job.id,
        commit=False,
    )
    AuditService(wb.session).prepare(
        event_type="publication.authorized",
        actor_type="local_user",
        actor_id="local-user",
        correlation_id=job.id,
        payload={"intent_hash": intent_hash, "actions": ACTIONS},
    )
    wb.session.commit()
    return job


def materialize_intent(wb: Workbench, event, review, now: datetime) -> PublishIntent:
    p = event.payload
    intent_fields = publish_intent_payload(
        review_run_id=review.id,
        execution_attempt_id=review.execution_attempt_id,
        task_id=review.task_id,
        actor_type="local_user",
        actor_id="local-user",
        upstream_repository=p["repository"],
        base_commit_sha=p["base_sha"],
        head_branch=p["branch"],
        head_commit_sha=p["head_sha"],
        diff_hash=p["diff_hash"],
        test_results_hash=p["tests_hash"],
        review_record_hash=p["review_hash"],
        title=p["title"],
        body=p["body"],
        allowed_actions=ACTIONS,
        confirmation_nonce=p["nonce"],
        expires_at=datetime.fromisoformat(p["expires_at"]),
    )
    fields = dict(intent_fields)
    fields["expires_at"] = datetime.fromisoformat(p["expires_at"])
    intent = PublishIntent(
        id=str(uuid4()),
        idempotency_key="real:" + event.id,
        **fields,
        record_hash=content_hash(intent_fields),
        created_at=now,
    )
    wb.session.add(intent)
    wb.session.flush()
    return intent


class PublicationWorker(WorkbenchJobWorker):
    kinds = ("publication_requirements", PREPARE, WRITE)

    def __init__(
        self,
        database,
        settings: Settings,
        *,
        worker_id: str,
        transport: PublicationTransport | None = None,
        checkout: TrustedCheckout | None = None,
    ):
        super().__init__(database, worker_id=worker_id)
        if settings.publisher_mode != "gh":
            raise WorkbenchError("真实 Publisher 未启用")
        self.settings = settings
        self.transport = transport or GhPublicationTransport()
        self.checkout = checkout or TrustedCheckout()

    def inputs(self, wb: Workbench, review_id: str):
        review = verified_review(wb, review_id)
        attempt = ExecutionAttemptService(wb.session).get_verified(
            review.execution_attempt_id
        )
        manifests = wb.artifacts.execution_manifests(attempt.id)
        from app.models import ExecutionStageVersion

        implement = wb.session.get(
            ExecutionStageVersion, review.implement_stage_version_id
        )
        manifest = next(
            m
            for m in manifests
            if m.stage == "implement" and m.result_hash == implement.result_hash
        )
        inventory_id = next(
            e.artifact_id for e in manifest.entries if e.role == "file-inventory"
        )
        from app.workspace_scope import attempt_workspace_paths

        inventory = {
            **wb.read(inventory_id),
            "workspace_paths": list(attempt_workspace_paths(wb.session, attempt)),
        }
        return (
            review,
            attempt,
            wb.artifacts.read_bytes(review.diff_hash),
            inventory,
        )

    async def execute(self, job: Job) -> dict:
        if job.kind == "publication_requirements":
            return await self.prepare_document(job)
        if job.kind == PREPARE:
            return await self.prepare(job)
        return await self.publish(job)

    async def prepare_document(self, job: Job) -> dict:
        from app.planner import frozen_inputs
        from app.plans import PlanVersionService
        from app.publication_requirements import read_requirements
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            review, attempt, patch, inventory = self.inputs(wb, job.payload["review_id"])
            if review.record_hash != job.payload["review_hash"]:
                raise WorkbenchError("发布文案依据已变化", code="publication_document_stale")
            inputs = frozen_inputs(session, job.payload["task_id"])
            plan = PlanVersionService._validated_content(wb.latest_plan(job.payload["task_id"])).hash_payload()
            tests = wb.read(review.test_results_hash)
            verification = wb.read(review.verify_result_hash)
        repository = await self.transport.api("GET", "repos/" + attempt.repository_full_name)
        branch = repository.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise WorkbenchError("上游分支不可用")
        base = await self.transport.api("GET", f"repos/{attempt.repository_full_name}/commits/{quote(branch, safe='')}")
        await self.upstream_binding(attempt.repository_full_name, review.base_commit_sha, base.get("sha"), inventory["workspace_paths"])
        requirements = await read_requirements(self.transport, attempt.repository_full_name, base["sha"], inventory["changed_paths"])
        evidence = {"requirements": requirements, "inputs": inputs, "approved_plan": plan,
                    "diff": patch.decode("utf-8"), "tests": tests, "verification": verification,
                    "user_request": job.payload.get("user_request", "")}
        ensure_no_sensitive_data(evidence, context="publication document evidence")
        with self.database.session() as session:
            self.check(session, job)
            wb = Workbench(session, self.settings.artifact_root)
            verified_review(wb, review.id)
            evidence_hash = wb.artifact(evidence)
            next_job = wb.enqueue(job.payload["task_id"], "publication_document", {
                "review_id": review.id, "review_hash": review.record_hash, "evidence_hash": evidence_hash,
                **({"conversation_message_id": job.payload["conversation_message_id"],
                    "publisher_account_hash": job.payload["publisher_account_hash"]}
                   if job.payload.get("conversation_message_id") else {}),
            }, key="document-job:" + job.id, commit=False)
            return self.finish(session, job, {"document_job_id": next_job.id, "next_job_id": next_job.id})

    async def upstream_binding(
        self, repository: str, reviewed_base: str, target: str, scope: list[str]
    ) -> dict:
        """Keep the reviewed base frozen; prove unrelated upstream advancement.

        This is not a rebase and never reuses evidence for changed source. Git
        tree identities cover every file/mode/link in each authorized subtree,
        without relying on a truncated changed-file listing from Compare.
        """
        def blocked() -> WorkbenchError:
            return WorkbenchError(
                "上游已修改已验证工作区或提交历史，请重新读取最新代码并确认方案，再在沙箱执行和验证。",
                code="publication_upstream_changed",
            )

        if not isinstance(target, str) or not SHA.fullmatch(target):
            raise WorkbenchError("无法核对上游提交，请重新准备预览", code="publication_upstream_unverified")
        binding = {
            "version": "publication-upstream-check-v1",
            "reviewed_base_sha": reviewed_base,
            "target_base_sha": target,
            "workspace_scope_hash": content_hash(scope),
            "status": "identical",
        }
        if target == reviewed_base:
            return binding
        if not scope:
            raise blocked()
        comparison = await self.transport.api(
            "GET", f"repos/{repository}/compare/{reviewed_base}...{target}"
        )
        if (
            comparison.get("status") != "ahead"
            or comparison.get("base_commit", {}).get("sha") != reviewed_base
            or comparison.get("merge_base_commit", {}).get("sha") != reviewed_base
        ):
            raise blocked()

        cache: dict[str, dict] = {}

        async def tree(oid: str) -> dict:
            if not isinstance(oid, str) or not SHA.fullmatch(oid):
                raise WorkbenchError("上游文件树标识无效", code="publication_upstream_unverified")
            if oid not in cache:
                if len(cache) >= 128:
                    raise WorkbenchError("上游工作区核对超过预算，请缩小范围后重新规划")
                raw = await self.transport.api("GET", f"repos/{repository}/git/trees/{oid}")
                entries = raw.get("tree")
                if (raw.get("sha") != oid or raw.get("truncated") is not False
                        or not isinstance(entries, list) or len(entries) > 20000):
                    raise WorkbenchError("上游文件树清单不完整", code="publication_upstream_unverified")
                values = {}
                for entry in entries:
                    name, digest = entry.get("path"), entry.get("sha")
                    if (not isinstance(name, str) or not name or name in {".", ".."}
                            or "/" in name or "\x00" in name or name in values
                            or not isinstance(digest, str) or not SHA.fullmatch(digest)
                            or (entry.get("mode"), entry.get("type")) not in {
                                ("040000", "tree"), ("100644", "blob"),
                                ("100755", "blob"), ("120000", "blob"), ("160000", "commit")}):
                        raise WorkbenchError("上游文件树格式无效", code="publication_upstream_unverified")
                    values[name] = {key: entry[key] for key in ("mode", "type", "sha")}
                cache[oid] = values
            return cache[oid]

        async def roots(commit: str) -> dict:
            raw = await self.transport.api("GET", f"repos/{repository}/git/commits/{commit}")
            if raw.get("sha") != commit:
                raise WorkbenchError("上游提交证据不一致", code="publication_upstream_unverified")
            root = raw.get("tree", {}).get("sha")
            values = {}
            for path in scope:
                oid, entry = root, None
                parts = path.split("/")
                for index, part in enumerate(parts):
                    entry = (await tree(oid)).get(part)
                    if entry is None or index == len(parts) - 1:
                        break
                    if entry["type"] != "tree":
                        entry = None
                        break
                    oid = entry["sha"]
                values[path] = entry
            return values

        before = await roots(reviewed_base)
        after = await roots(target)
        if before != after:
            raise blocked()
        return {**binding, "status": "unchanged_workspace", "workspace_roots_hash": content_hash(before)}

    async def prepare(self, job: Job) -> dict:
        data = job.payload
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            review, attempt, patch, inventory = self.inputs(wb, data["review_id"])
            if review.record_hash != data["review_hash"]:
                raise WorkbenchError("审查已变化")
            from app.planner import frozen_inputs

            issue_number = int(
                frozen_inputs(session, data["task_id"])["issue"].get("number", 0)
            )
        user = await self.transport.api("GET", "user")
        owner, user_id = user.get("login"), user.get("id")
        if (
            not isinstance(owner, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner)
            or not isinstance(user_id, int)
        ):
            raise WorkbenchError("发布账号无效")
        repository = await self.transport.api(
            "GET", "repos/" + attempt.repository_full_name
        )
        base_branch = repository.get("default_branch")
        if not isinstance(base_branch, str) or not base_branch:
            raise WorkbenchError("上游分支不可用")
        base = await self.transport.api(
            "GET",
            f"repos/{attempt.repository_full_name}/commits/{quote(base_branch, safe='')}",
        )
        upstream = await self.upstream_binding(
            attempt.repository_full_name, review.base_commit_sha, base.get("sha"),
            inventory["workspace_paths"],
        )
        from app.publication_requirements import read_requirements, validate_document
        requirements = await read_requirements(self.transport, attempt.repository_full_name, base["sha"], inventory["changed_paths"])
        if data.get("document_hash"):
            with self.database.session() as session:
                document = Workbench(session, self.settings.artifact_root).latest(data["task_id"], "publication_document_ready")
                if not document or document.record_hash != data["document_hash"] or document.payload["review_hash"] != review.record_hash or document.payload["requirements_hash"] != requirements["hash"]:
                    raise WorkbenchError("仓库规范或发布文案依据已变化，请重新生成英文草稿", code="publication_document_stale")
        validate_document(data["title"], data["body"], requirements, data.get("template_path"), issue_number=issue_number)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        marker = "contribos:" + data["task_id"] + ":" + review.record_hash
        intent = {
            **({"publisher_account_hash": data["publisher_account_hash"]}
               if data.get("publisher_account_hash") else {}),
            "repository": attempt.repository_full_name,
            "fork": f"{owner}/{attempt.repository_full_name.split('/')[1]}",
            "branch": f"contribos/issue-{issue_number}-{data['task_id']}",
            "base_branch": base_branch,
            "base_sha": review.base_commit_sha,
            "target_base_sha": upstream["target_base_sha"],
            "upstream_check": upstream,
            "requirements": requirements,
            "requirements_hash": requirements["hash"],
            "document_hash": data.get("document_hash"),
            "template_path": data.get("template_path"),
            "title": data["title"],
            "body": re.sub(
                r"\n*<!-- contribos:[a-f0-9-]+:[a-f0-9]{64} -->", "", data["body"]
            ).rstrip()
            + f"\n\n<!-- {marker} -->",
            "review_id": review.id,
            "review_hash": review.record_hash,
            "diff_hash": review.diff_hash,
            "tests_hash": review.test_results_hash,
            "plan_hash": review.plan_record_hash,
            "policy_hash": review.sandbox_policy_hash,
            "workspace_scope_version": "source-tree-scope-v1",
            "workspace_paths": inventory["workspace_paths"],
            "workspace_scope_hash": content_hash(inventory["workspace_paths"]),
            "commit_metadata": {
                "name": owner,
                "email": f"{user_id}+{owner}@users.noreply.github.com",
                "date": now.isoformat(),
                "message": data["title"] + "\n\n" + marker + "\n",
            },
            "actions": ACTIONS,
            "nonce": secrets.token_hex(32),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        }
        async with self.checkout.prepare(intent, patch, inventory) as (_, head, tree):
            intent.update(head_sha=head, tree_sha=tree)
        ensure_no_sensitive_data(intent, context="publication preview")
        with self.database.session() as session:
            self.check(session, job)
            wb = Workbench(session, self.settings.artifact_root)
            verified_review(wb, review.id)
            if data.get("publisher_account_hash"):
                from app.publisher_accounts import require_account
                require_account(session, data["publisher_account_hash"])
            event = wb.append(
                data["task_id"],
                "publication_prepared",
                intent,
                key="prepared:" + job.id,
                job_id=job.id,
                commit=False,
            )
            return self.finish(session, job, {"intent_hash": event.record_hash})

    async def publish(self, job: Job) -> dict:
        data = job.payload
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            event = next(
                (e for e in wb.history(data["task_id"]) if e.id == data["intent_id"]),
                None,
            )
            confirmation = wb.latest(data["task_id"], "publication_confirmed")
            if (
                event is None
                or event.kind != "publication_prepared"
                or event.record_hash != data["intent_hash"]
                or confirmation is None
                or confirmation.job_id != job.id
                or confirmation.payload["intent_hash"] != event.record_hash
            ):
                raise WorkbenchError("缺少此发布内容的一次性人工确认")
            existing = wb.latest(data["task_id"], "publication_completed")
            if existing:
                return existing.payload
            from app.publish_intents import PublishIntentService

            stored = PublishIntentService(session).get_verified(
                data["publish_intent_id"]
            )
            if stored.record_hash != data["publish_intent_hash"]:
                raise WorkbenchError("已确认的发布意图发生变化")
            intent = event.payload
            if intent.get("publisher_account_hash"):
                from app.publisher_accounts import require_account
                require_account(session, intent["publisher_account_hash"])
            if intent["actions"] != ACTIONS:
                raise WorkbenchError("发布动作无效")
            review, attempt, patch, inventory = self.inputs(wb, intent["review_id"])
        # Always reconcile an existing PR before considering another write.
        found = await self.find_pr(intent)
        if found:
            return self.record(job, event, review, found)
        base = await self.transport.api(
            "GET",
            f"repos/{intent['repository']}/commits/{quote(intent['base_branch'], safe='')}",
        )
        if base.get("sha") != intent.get("target_base_sha", intent["base_sha"]):
            raise WorkbenchError("上游提交已变化，停止发布；确认目标已过期，请先核对发布任务。")
        if intent.get("requirements_hash"):
            from app.publication_requirements import read_requirements
            current_requirements = await read_requirements(self.transport, intent["repository"], base["sha"], inventory["changed_paths"])
            if current_requirements["hash"] != intent["requirements_hash"]:
                raise WorkbenchError("仓库贡献规范已变化，停止发布；请核对原发布任务后重新准备", code="publication_requirements_stale")
        account = await self.transport.api("GET", "user")
        if account.get("login") != intent["fork"].split("/")[0]:
            raise WorkbenchError("Publisher 登录账号与确认的 Fork 不一致")
        fork = await self.transport.api("GET", "repos/" + intent["fork"], missing=True)
        if fork is None:
            self.action_started(job, event, "fork_write_started")
            await self.transport.api(
                "POST",
                f"repos/{intent['repository']}/forks",
                {"default_branch_only": True},
            )
            for _ in range(10):
                fork = await self.transport.api(
                    "GET", "repos/" + intent["fork"], missing=True
                )
                if fork:
                    break
                await asyncio.sleep(2)
        if not fork or fork.get("parent", {}).get("full_name") != intent["repository"]:
            raise WorkbenchError("Fork 尚未就绪或不属于已确认的上游")
        branch_path = f"repos/{intent['fork']}/git/ref/heads/{intent['branch']}"
        remote = await self.transport.api("GET", branch_path, missing=True)
        if (
            remote is not None
            and remote.get("object", {}).get("sha") != intent["head_sha"]
        ):
            raise WorkbenchError("目标远端分支已存在不同提交，禁止覆盖")
        if remote is None:
            async with self.checkout.prepare(intent, patch, inventory) as (
                directory,
                head,
                tree,
            ):
                if tree != intent["tree_sha"]:
                    raise WorkbenchError("发布文件树与预览不一致")
                self.action_started(job, event, "push_write_started")
                await self.transport.push(
                    directory, intent["fork"], intent["branch"], head
                )
            remote = await self.transport.api("GET", branch_path)
            if remote.get("object", {}).get("sha") != intent["head_sha"]:
                raise WorkbenchError("Push 后提交核对失败")
        base = await self.transport.api(
            "GET",
            f"repos/{intent['repository']}/commits/{quote(intent['base_branch'], safe='')}",
        )
        if base.get("sha") != intent.get("target_base_sha", intent["base_sha"]):
            raise WorkbenchError("上游提交已变化，保留远端分支并停止创建 PR")
        # A POST timeout may have created a PR. Persist its marker before the call;
        # retries perform only reconciliation if a prior POST has an unknown result.
        with self.database.session() as session:
            self.check(session, job)
            wb = Workbench(session, self.settings.artifact_root)
            sent = wb.latest(data["task_id"], "pr_write_started")
            if sent:
                raise WorkbenchError(
                    "上次 PR 请求结果不明确；远端尚未查到匹配 PR，请稍后重试核对"
                )
            verified_review(wb, review.id)
            wb.append(
                data["task_id"],
                "pr_write_started",
                {"intent_hash": event.record_hash},
                key="pr-write:" + event.id,
                job_id=job.id,
            )
        created = await self.transport.api(
            "POST",
            f"repos/{intent['repository']}/pulls",
            {
                "title": intent["title"],
                "body": intent["body"],
                "draft": True,
                "head": intent["fork"].split("/")[0] + ":" + intent["branch"],
                "base": intent["base_branch"],
                "maintainer_can_modify": False,
            },
        )
        self.validate_pr(intent, created)
        return self.record(job, event, review, created)

    def action_started(self, job: Job, event, kind: str) -> None:
        with self.database.session() as session:
            self.check(session, job)
            wb = Workbench(session, self.settings.artifact_root)
            verified_review(wb, event.payload["review_id"])
            wb.append(
                job.payload["task_id"],
                kind,
                {"intent_hash": event.record_hash},
                key=kind + ":" + event.id,
                job_id=job.id,
            )

    async def find_pr(self, intent: dict):
        query = urlencode(
            {
                "state": "all",
                "head": intent["fork"].split("/")[0] + ":" + intent["branch"],
                "base": intent["base_branch"],
                "per_page": 100,
            }
        )
        values = await self.transport.api(
            "GET", f"repos/{intent['repository']}/pulls?{query}"
        )
        if not isinstance(values, list) or len(values) > 1:
            raise WorkbenchError("远端 PR 状态不明确")
        if values:
            self.validate_pr(intent, values[0])
            return values[0]
        return None

    @staticmethod
    def validate_pr(intent: dict, value: dict) -> None:
        if (
            not value.get("draft")
            or value.get("title") != intent["title"]
            or value.get("body") != intent["body"]
            or value.get("head", {}).get("sha") != intent["head_sha"]
            or value.get("head", {}).get("ref") != intent["branch"]
            or value.get("head", {}).get("repo", {}).get("full_name") != intent["fork"]
            or value.get("base", {}).get("repo", {}).get("full_name")
            != intent["repository"]
            or value.get("base", {}).get("ref") != intent["base_branch"]
            or value.get("html_url")
            != f"https://github.com/{intent['repository']}/pull/{value.get('number')}"
        ):
            raise WorkbenchError("远端 PR 与人工确认的内容不同")

    def record(self, job: Job, event, review, remote: dict) -> dict:
        """Commit the real intent, result, lifecycle and audit atomically."""
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            existing = wb.latest(job.payload["task_id"], "publication_completed")
            if existing:
                return existing.payload
            p = event.payload
            now = datetime.now(timezone.utc)
            from app.publish_intents import PublishIntentService

            intent = PublishIntentService(session).get_verified(
                job.payload["publish_intent_id"]
            )
            if intent.record_hash != job.payload["publish_intent_hash"]:
                raise WorkbenchError("发布意图哈希已变化")
            states = ContributionTaskStateService(session)
            current = states.current(review.task_id)
            draft_state = states.prepare_transition(
                review.task_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state="draft_pr",
                reason_code="draft_pr_published",
            )
            fields = draft_pull_request_payload(
                publish_intent_id=intent.id,
                task_id=review.task_id,
                provider="github",
                number=remote["number"],
                html_url=remote["html_url"],
                head_branch=p["branch"],
                base_commit_sha=p["base_sha"],
                head_commit_sha=p["head_sha"],
                diff_hash=p["diff_hash"],
                review_record_hash=p["review_hash"],
            )
            draft = DraftPullRequest(
                id=str(uuid4()),
                **fields,
                record_hash=content_hash(fields),
                created_at=now,
            )
            session.add_all((draft_state, draft))
            session.flush()
            fields = publish_confirmation_payload(
                publish_intent_id=intent.id,
                draft_pull_request_id=draft.id,
                actor_type="local_user",
                actor_id="local-user",
                confirmation_nonce=p["nonce"],
                draft_pr_state_version_id=draft_state.id,
                draft_pr_state_record_hash=draft_state.record_hash,
            )
            session.add(
                PublishConfirmation(
                    id=str(uuid4()),
                    **fields,
                    record_hash=content_hash(fields),
                    created_at=now,
                )
            )
            result = {
                "html_url": remote["html_url"],
                "number": remote["number"],
                "intent_hash": event.record_hash,
                "publish_intent_id": intent.id,
                "draft_pull_request_id": draft.id,
            }
            wb.append(
                review.task_id,
                "publication_completed",
                result,
                key="published:" + event.id,
                job_id=job.id,
                commit=False,
            )
            AuditService(session).prepare(
                event_type="publication.completed",
                actor_type="local_user",
                actor_id="local-user",
                correlation_id=job.id,
                payload=result,
            )
            return self.finish(session, job, result)
