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
        with tempfile.TemporaryDirectory(prefix="contribos-publish-") as directory:
            env = git_environment(directory)

            async def git(*args: str, data: bytes | None = None) -> bytes:
                code, output = await bounded_process(
                    git_argv() + list(args), env=env, cwd=directory, stdin=data
                )
                if code:
                    raise WorkbenchError("受控 Git checkout、补丁或提交验证失败")
                return output.strip()

            await git("init", "--quiet", "--template=")
            await git(
                "fetch",
                "--no-tags",
                "--depth=1",
                f"https://github.com/{intent['repository']}.git",
                intent["base_sha"],
            )
            await git("checkout", "--quiet", "--detach", intent["base_sha"])
            # Never open links from a remote repository; validate the complete base inventory first.
            self.verify_inventory(directory, inventory["baseline_inventory"])
            await git(
                "apply", "--check", "--index", "--whitespace=nowarn", "-", data=patch
            )
            await git("apply", "--index", "--whitespace=nowarn", "-", data=patch)
            self.verify_inventory(directory, inventory["result_inventory"])
            changed = (
                (await git("diff", "--cached", "--name-only", "-z", "--no-renames"))
                .decode()
                .split("\x00")
            )
            if set(filter(None, changed)) != set(inventory["changed_paths"]):
                raise WorkbenchError("Git 修改文件与已审查清单不一致")
            tree = (await git("write-tree")).decode()
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
            ).decode()
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
                if not entry or entry["kind"] != "file":
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
    if review.reviewer_kind != "nvidia_nim":
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
    wb: Workbench, task_id: str, *, title: str, body: str, key: str
) -> Job:
    wb.assert_idle(task_id)
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
    return wb.enqueue(
        task_id,
        PREPARE,
        {
            "review_id": review.id,
            "review_hash": review.record_hash,
            "title": title,
            "body": body,
        },
        key="prepare:" + key,
        actor="local-user",
    )


def confirm_publication(
    wb: Workbench, task_id: str, *, intent_hash: str, nonce: str
) -> Job:
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
        raise WorkbenchError("发布预览已过期，请重新准备")
    review = verified_review(wb, event.payload["review_id"])
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
    kinds = (PREPARE, WRITE)

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
        return (
            review,
            attempt,
            wb.artifacts.read_bytes(review.diff_hash),
            wb.read(inventory_id),
        )

    async def execute(self, job: Job) -> dict:
        if job.kind == PREPARE:
            return await self.prepare(job)
        return await self.publish(job)

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
        if base.get("sha") != review.base_commit_sha:
            raise WorkbenchError("上游提交已移动，请重新规划和验证")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        marker = "contribos:" + data["task_id"] + ":" + review.record_hash
        intent = {
            "repository": attempt.repository_full_name,
            "fork": f"{owner}/{attempt.repository_full_name.split('/')[1]}",
            "branch": f"contribos/issue-{issue_number}-{data['task_id']}",
            "base_branch": base_branch,
            "base_sha": review.base_commit_sha,
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
        if base.get("sha") != intent["base_sha"]:
            raise WorkbenchError("上游提交已变化，停止发布")
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
        if base.get("sha") != intent["base_sha"]:
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
