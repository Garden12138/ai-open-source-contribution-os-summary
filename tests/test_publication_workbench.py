from __future__ import annotations
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
import pytest
from sqlalchemy import select
from app.artifacts import ArtifactStore
from app.authorizations import UserAction
from app.jobs import JobService
from app.models import PublishIntent, DraftPullRequest, ReviewRun
from app.publication import (
    PublicationWorker,
    request_publication,
    confirm_publication,
    WorkbenchError,
)
from app.review_provider import NvidiaReviewJobWorker, ProviderReviewService
from app.workbench import Workbench
from tests.test_execution_pipeline import _complete_verified_execution
from tests.test_nvidia_workflows import ScriptedRunner, _identity

REVIEW = json.dumps(
    {
        "verdict": "pass",
        "reason_code": "verified",
        "findings": [
            {
                "severity": "info",
                "location": "README.md",
                "evidence": "Exact diff and tests verified",
                "recommendation": "Proceed to human acceptance",
                "verdict": "pass",
            }
        ],
    }
)


class FakeCheckout:
    calls = 0

    @asynccontextmanager
    async def prepare(self, intent, patch, inventory):
        self.calls += 1
        assert patch
        assert inventory["changed_paths"]
        assert intent["workspace_paths"] == inventory["workspace_paths"]
        from app.provenance import content_hash
        assert intent["workspace_scope_hash"] == content_hash(intent["workspace_paths"])
        yield "/unused-test-checkout", "d" * 40, "e" * 40


class FakeTransport:
    def __init__(self, repository):
        self.repository = repository
        self.calls = []
        self.fork = None
        self.ref = None
        self.prs = []
        self.timeout_after_pr = False
        self.unknown_without_pr = False
        self.base = "a" * 40
        self.documents = {}

    async def api(self, method, path, body=None, *, missing=False):
        self.calls.append((method, path, body))
        if "/contents/" in path:
            import base64, hashlib
            from urllib.parse import unquote
            filename = unquote(path.split("/contents/", 1)[1].split("?", 1)[0])
            if filename in self.documents:
                raw = self.documents[filename].encode()
                return {"type":"file", "path":filename, "encoding":"base64", "size":len(raw),
                        "sha":hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
                        "content":base64.b64encode(raw).decode()}
            prefix = filename + "/"
            entries = [{"name":name[len(prefix):], "type":"file"} for name in self.documents
                       if name.startswith(prefix) and "/" not in name[len(prefix):]]
            return entries or None
        if path == "repos/" + self.repository.split("/")[0] + "/.github":
            return None
        if path == "user":
            return {"login": "contributor", "id": 123}
        if "/commits/" in path:
            return {"sha": self.base}
        if path == "repos/" + self.repository:
            return {"default_branch": "main"}
        if method == "GET" and "/pulls?" in path:
            return self.prs
        if method == "POST" and path.endswith("/forks"):
            self.fork = {"parent": {"full_name": self.repository}}
            return self.fork
        if "/git/ref/heads/" in path:
            return self.ref
        if method == "POST" and path.endswith("/pulls"):
            if self.unknown_without_pr:
                raise WorkbenchError("unknown external state")
            owner, branch = body["head"].split(":", 1)
            value = {
                "number": 7,
                "html_url": f"https://github.com/{self.repository}/pull/7",
                "draft": body["draft"],
                "title": body["title"],
                "body": body["body"],
                "head": {
                    "sha": "d" * 40,
                    "ref": branch,
                    "repo": {"full_name": owner + "/" + self.repository.split("/")[1]},
                },
                "base": {"ref": body["base"], "repo": {"full_name": self.repository}},
            }
            self.prs.append(value)
            if self.timeout_after_pr:
                raise WorkbenchError("timeout after PR write")
            return value
        if path.startswith("repos/contributor/"):
            return self.fork
        raise AssertionError((method, path, body))

    async def push(self, directory, fork, branch, head_sha):
        self.calls.append(("PUSH", fork, head_sha))
        assert head_sha == "d" * 40
        self.ref = {"object": {"sha": head_sha}}


def ready(tmp_path, *, review_provider="nvidia_nim"):
    app, attempt_id, task_id, _ = _complete_verified_execution(tmp_path)
    settings = replace(app.state.settings, publisher_mode="gh")
    db = app.state.database
    identity = _identity(settings.review_model)
    if review_provider == "minimax":
        identity = replace(
            identity,
            provider="minimax",
            adapter_version="studio-model-v1",
            model="MiniMax-M3",
            model_version="f" * 64,
        )
    with db.session() as session:
        ProviderReviewService(
            session, artifacts=ArtifactStore(session, settings.artifact_root)
        ).enqueue(
            attempt_id,
            action=UserAction.START_REVIEW,
            actor_id="local-user",
            expected_provider=identity,
            idempotency_key="review",
        )
    job = asyncio.run(
        NvidiaReviewJobWorker(
            db,
            artifact_root=settings.artifact_root,
            runner=ScriptedRunner(REVIEW),
            identity=identity,
            worker_id="review",
        ).run_once()
    )
    assert job.state == "succeeded", job.error_message
    with db.session() as session:
        from app.executions import ExecutionAttemptService

        repository = (
            ExecutionAttemptService(session)
            .get_verified(attempt_id)
            .repository_full_name
        )
        wb = Workbench(session, settings.artifact_root)
        request_publication(
            wb, task_id, title="Improve behavior", body="Tested change", key="prepare"
        )
    transport = FakeTransport(repository)
    worker = PublicationWorker(
        db,
        settings,
        worker_id="publisher",
        transport=transport,
        checkout=FakeCheckout(),
    )
    return db, settings, task_id, transport, worker


def test_verified_minimax_review_can_prepare_real_draft_publication(tmp_path):
    db, _, task_id, _, _ = ready(tmp_path, review_provider="minimax")
    with db.session() as session:
        review = session.scalar(select(ReviewRun).where(ReviewRun.task_id == task_id))
        assert review is not None and review.reviewer_kind == "minimax"
    db.close()


def test_preparation_is_read_only_confirmation_publishes_exactly_once(tmp_path):
    db, settings, task_id, transport, worker = ready(tmp_path)
    job = asyncio.run(worker.run_once())
    assert job.state == "succeeded", job.error_message
    assert all(call[0] == "GET" for call in transport.calls)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        intent = wb.latest(task_id, "publication_prepared")
        with pytest.raises(WorkbenchError):
            confirm_publication(
                wb, task_id, intent_hash=intent.record_hash, nonce="a" * 64
            )
        write = confirm_publication(
            wb, task_id, intent_hash=intent.record_hash, nonce=intent.payload["nonce"]
        )
        assert write.payload["publish_intent_id"]
        assert wb.latest(task_id, "publication_confirmed").actor_id == "local-user"
        with pytest.raises(WorkbenchError):
            confirm_publication(
                wb,
                task_id,
                intent_hash=intent.record_hash,
                nonce=intent.payload["nonce"],
            )
    job = asyncio.run(worker.run_once())
    assert job.state == "succeeded", job.error_message
    with db.session() as session:
        intent = session.scalar(select(PublishIntent))
        draft = session.scalar(select(DraftPullRequest))
        assert intent.head_commit_sha == "d" * 40
        assert draft.provider == "github" and draft.number == 7
        assert draft.publish_intent_id == intent.id
    assert len(transport.prs) == 1
    assert asyncio.run(worker.run_once()) is None


@pytest.mark.parametrize("lost_response", [True, False])
def test_unknown_pr_write_reconciles_without_repeating_post(tmp_path, lost_response):
    db, settings, task_id, transport, worker = ready(tmp_path)
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        intent = wb.latest(task_id, "publication_prepared")
        write = confirm_publication(
            wb, task_id, intent_hash=intent.record_hash, nonce=intent.payload["nonce"]
        )
    transport.timeout_after_pr = lost_response
    transport.unknown_without_pr = not lost_response
    failed = asyncio.run(worker.run_once())
    assert failed.state == "failed", failed.error_message
    with db.session() as session:
        JobService(session).retry(write.id)
    result = asyncio.run(worker.run_once())
    assert result.state == (
        "succeeded" if lost_response else "failed"
    ), result.error_message
    assert (
        sum(
            method == "POST" and path.endswith("/pulls")
            for method, path, _ in transport.calls
        )
        == 1
    )
    assert len(transport.prs) == (1 if lost_response else 0)


def test_base_movement_after_confirmation_produces_zero_writes(tmp_path):
    db, settings, task_id, transport, worker = ready(tmp_path)
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        intent = wb.latest(task_id, "publication_prepared")
        confirm_publication(
            wb, task_id, intent_hash=intent.record_hash, nonce=intent.payload["nonce"]
        )
    transport.base = "f" * 40
    result = asyncio.run(worker.run_once())
    assert result.state == "failed"
    assert all(method == "GET" for method, _, _ in transport.calls)


class AdvancedUpstreamTransport(FakeTransport):
    """Full scope tree evidence; Compare's file list is deliberately absent."""
    failure = None

    async def api(self, method, path, body=None, *, missing=False):
        if "/compare/" in path:
            self.calls.append((method, path, body))
            return {"status": "diverged" if self.failure == "diverged" else "ahead",
                    "base_commit": {"sha": "a" * 40},
                    "merge_base_commit": {"sha": "b" * 40 if self.failure == "not_ancestor" else "a" * 40}}
        if "/git/commits/" in path:
            self.calls.append((method, path, body))
            commit = path.rsplit("/", 1)[1]
            return {"sha": commit, "tree": {"sha": ("1" if commit == "a" * 40 else "2") * 40}}
        if "/git/trees/" in path:
            self.calls.append((method, path, body))
            oid = path.rsplit("/", 1)[1]
            entries = [
                {"path":"LICENSE", "mode":"100644", "type":"blob", "sha":"3" * 40},
                {"path":"README.md", "mode":"100644", "type":"blob", "sha":"4" * 40},
                {"path":"python", "mode":"040000", "type":"tree", "sha":"5" * 40},
                {"path":".github", "mode":"040000", "type":"tree", "sha":("6" if oid == "1" * 40 else "7") * 40},
            ]
            if oid == "2" * 40:
                if self.failure == "workspace_change": entries[2]["sha"] = "8" * 40
                if self.failure == "mode_change": entries[0]["mode"] = "100755"
                if self.failure == "missing": entries = entries[1:]
                if self.failure == "malformed_sha": entries[2]["sha"] = "invalid"
                if self.failure == "duplicate": entries.append(dict(entries[0]))
            return {"sha": oid, "tree": entries, "truncated": self.failure == "truncated"}
        return await super().api(method, path, body, missing=missing)


def test_unrelated_advance_keeps_reviewed_base_and_binds_exact_target(tmp_path, monkeypatch):
    import app.workspace_scope as scopes
    db, settings, task_id, _, worker = ready(tmp_path)
    monkeypatch.setattr(scopes, "attempt_workspace_paths", lambda *_: ("LICENSE", "README.md", "python"))
    transport = AdvancedUpstreamTransport(worker.transport.repository)
    transport.base = "f" * 40
    worker.transport = transport
    assert asyncio.run(worker.run_once()).state == "succeeded"
    assert all(method == "GET" for method, _, _ in transport.calls)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        event = wb.latest(task_id, "publication_prepared")
        assert event.payload["base_sha"] == "a" * 40
        assert event.payload["target_base_sha"] == "f" * 40
        assert event.payload["upstream_check"]["status"] == "unchanged_workspace"
        assert len(event.payload["upstream_check"]["workspace_roots_hash"]) == 64
        confirm_publication(wb, task_id, intent_hash=event.record_hash, nonce=event.payload["nonce"])
    assert asyncio.run(worker.run_once()).state == "succeeded"
    assert len(transport.prs) == 1


@pytest.mark.parametrize("failure", ["workspace_change", "mode_change", "missing", "diverged",
                                    "not_ancestor", "truncated", "malformed_sha", "duplicate", "unscoped"])
def test_upstream_compatibility_fails_closed(failure):
    worker = object.__new__(PublicationWorker)
    worker.transport = AdvancedUpstreamTransport("fixture/repo")
    worker.transport.failure = failure
    with pytest.raises(WorkbenchError):
        asyncio.run(worker.upstream_binding("fixture/repo", "a" * 40, "f" * 40,
                    [] if failure == "unscoped" else ["LICENSE", "README.md", "python"]))
    assert all(method == "GET" for method, _, _ in worker.transport.calls)


def test_new_target_moves_after_confirmation_still_produces_zero_writes(tmp_path, monkeypatch):
    import app.workspace_scope as scopes
    db, settings, task_id, _, worker = ready(tmp_path)
    monkeypatch.setattr(scopes, "attempt_workspace_paths", lambda *_: ("python",))
    transport = AdvancedUpstreamTransport(worker.transport.repository)
    transport.base = "f" * 40
    worker.transport = transport
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        event = wb.latest(task_id, "publication_prepared")
        confirm_publication(wb, task_id, intent_hash=event.record_hash, nonce=event.payload["nonce"])
    transport.base = "b" * 40
    assert asyncio.run(worker.run_once()).state == "failed"
    assert all(method == "GET" for method, _, _ in transport.calls)


def test_expired_preview_cannot_authorize_write(tmp_path):
    from datetime import datetime, timedelta, timezone
    db, settings, task_id, _, worker = ready(tmp_path)
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        original = wb.latest(task_id, "publication_prepared")
        expired = wb.append(task_id, "publication_prepared", {
            **original.payload, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        }, key="expired-preview")
        with pytest.raises(WorkbenchError, match="预览已过期") as caught:
            confirm_publication(wb, task_id, intent_hash=expired.record_hash, nonce=expired.payload["nonce"])
        assert caught.value.code == "publication_preview_expired"
        assert wb.latest(task_id, "publication_confirmed") is None
        assert session.scalar(select(PublishIntent)) is None
        assert not any(j.kind == "publication_write" for j in wb.jobs(task_id))


def test_expired_preview_renews_with_new_nonce_and_preserved_review(tmp_path):
    from datetime import datetime, timedelta, timezone
    db, settings, task_id, _, worker = ready(tmp_path)
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        original = wb.latest(task_id, "publication_prepared")
        expired = wb.append(task_id, "publication_prepared", {
            **original.payload, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        }, key="expired-preview")
        request_publication(wb, task_id, title="Edited title", body="Edited body", key="renew-preview")
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        renewed = wb.latest(task_id, "publication_prepared")
        assert renewed.payload["nonce"] != expired.payload["nonce"]
        assert renewed.payload["review_hash"] == original.payload["review_hash"]
        assert renewed.payload["tests_hash"] == original.payload["tests_hash"]
        assert renewed.payload["title"] == "Edited title"
        assert renewed.payload["body"].startswith("Edited body\n")
        with pytest.raises(WorkbenchError):
            confirm_publication(wb, task_id, intent_hash=expired.record_hash, nonce=expired.payload["nonce"])
        assert session.scalar(select(PublishIntent)) is None
        assert not any(j.kind == "publication_write" for j in wb.jobs(task_id))


@pytest.mark.parametrize("scoped", [False, True])
def test_trusted_checkout_reconstructs_real_commit_and_rejects_drift(
    tmp_path, monkeypatch, scoped
):
    import difflib
    import hashlib
    import app.publication as publication

    seed = tmp_path / "trusted-fixture"
    seed.mkdir()
    original = b"def value():\n    return 1\n"
    updated = b"def value():\n    return 2\n"
    main_path = "python/main.py" if scoped else "main.py"
    (seed / main_path).parent.mkdir(parents=True, exist_ok=True)
    (seed / main_path).write_bytes(original)
    if scoped:
        (seed / "python/unchanged.txt").write_bytes(b"unchanged\n")
        (seed / "python/CLAUDE.md").symlink_to("unchanged.txt")
        (seed / "javascript").mkdir()
        (seed / "javascript/unrelated.js").write_bytes(b"unchanged outside scope\n")
        (seed / "unrelated-link").symlink_to("javascript/unrelated.js")
    env = publication.git_environment(str(tmp_path))
    env.update(
        GIT_AUTHOR_NAME="Fixture",
        GIT_COMMITTER_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.test",
        GIT_COMMITTER_EMAIL="fixture@example.test",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
    )
    actual_process = publication.bounded_process

    async def git(*args, data=None):
        code, value = await actual_process(
            publication.git_argv() + list(args), cwd=str(seed), env=env, stdin=data
        )
        assert code == 0
        return value.strip().decode()

    async def setup():
        await git("init", "--quiet", "--template=")
        await git("add", "--all")
        tree = await git("write-tree")
        return await git("commit-tree", tree, data=b"Trusted fixture\n")

    base = asyncio.run(setup())

    async def offline_fetch(argv, **kwargs):
        if "fetch" in argv:
            argv = publication.git_argv() + [
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--no-tags",
                str(seed),
                argv[-1],
            ]
        return await actual_process(argv, **kwargs)

    monkeypatch.setattr(publication, "bounded_process", offline_fetch)
    patch = "".join(
        difflib.unified_diff(
            original.decode().splitlines(True),
            updated.decode().splitlines(True),
            fromfile="a/" + main_path,
            tofile="b/" + main_path,
        )
    ).encode()

    def entry(data, path=main_path):
        return {
            "path": path,
            "type": "file",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "executable": False,
        }

    inventory = {
        "baseline_inventory": [entry(original)],
        "result_inventory": [entry(updated)],
        "changed_paths": [main_path],
    }
    intent = {
        "repository": "fixture/repo",
        "base_sha": base,
        "diff_hash": hashlib.sha256(patch).hexdigest(),
        "commit_metadata": {
            "name": "Contributor",
            "email": "contributor@example.test",
            "date": "2026-01-02T00:00:00+00:00",
            "message": "Approved change\n",
        },
    }
    if scoped:
        from app.provenance import content_hash
        inventory["workspace_paths"] = ["python"]
        for key in ("baseline_inventory", "result_inventory"):
            inventory[key].append(entry(b"unchanged\n", "python/unchanged.txt"))
            inventory[key].append({"path":"python/CLAUDE.md", "type":"symlink", "target":"unchanged.txt"})
        intent.update(workspace_paths=["python"], workspace_scope_version="source-tree-scope-v1",
                      workspace_scope_hash=content_hash(["python"]))

    async def exercise():
        checkout = publication.TrustedCheckout()
        async with checkout.prepare(intent, patch, inventory) as (
            directory,
            head,
            tree,
        ):
            assert (Path(directory) / main_path).read_bytes() == updated
            assert len(head) == 40 and head != base
            if scoped:
                assert not (Path(directory) / "javascript").exists()
                assert not (Path(directory) / "unrelated-link").is_symlink()
                assert not (Path(directory) / "python/CLAUDE.md").is_symlink()
                async def inspect(*args):
                    code, output = await actual_process(publication.git_argv() + list(args),
                                                        cwd=directory, env=env)
                    assert code == 0
                    return output
                assert await inspect("rev-parse", f"{base}:javascript") == await inspect("rev-parse", f"{head}:javascript")
                assert await inspect("ls-tree", base, "unrelated-link") == await inspect("ls-tree", head, "unrelated-link")
                assert await inspect("ls-tree", base, "python/CLAUDE.md") == await inspect("ls-tree", head, "python/CLAUDE.md")
                assert (await inspect("diff-tree", "--no-commit-id", "--name-only", "-r", base, head)).decode().strip() == main_path
        async with checkout.prepare({**intent, "head_sha": head}, patch, inventory) as (
            _,
            same_head,
            same_tree,
        ):
            assert (same_head, same_tree) == (head, tree)
        with pytest.raises(WorkbenchError):
            async with checkout.prepare(
                intent, patch, {**inventory, "result_inventory": [entry(original)]}
            ):
                pass
        if scoped:
            # Omitted or altered scoped files must still fail, as must attempts
            # to widen the frozen scope or introduce unrelated changes.
            for broken_intent, broken_inventory in (
                (intent, {**inventory, "baseline_inventory": [entry(original)]}),
                (intent, {**inventory, "baseline_inventory": [entry(b"tampered"), inventory["baseline_inventory"][1]]}),
                ({**intent, "workspace_paths": ["javascript"]}, inventory),
                ({**intent, "workspace_scope_hash": "0" * 64}, inventory),
                (intent, {**inventory, "changed_paths": ["javascript/unrelated.js"]}),
                (intent, {**inventory, "result_inventory": inventory["result_inventory"][:-1]}),
                (intent, {**inventory, "result_inventory": [*inventory["result_inventory"][:-1], {"path":"python/CLAUDE.md", "type":"symlink", "target":"main.py"}]}),
                (intent, {**inventory, "changed_paths": [main_path, "python/CLAUDE.md"]}),
                ({**intent, "workspace_paths": ["../python"]}, {**inventory, "workspace_paths": ["../python"]}),
            ):
                with pytest.raises(WorkbenchError):
                    async with checkout.prepare(broken_intent, patch, broken_inventory):
                        pass
            # The patch itself cannot touch an unmaterialized path even if the
            # claimed changed-path list hides it.
            malicious_patch = patch + b"--- a/javascript/unrelated.js\n+++ b/javascript/unrelated.js\n@@ -1 +1 @@\n-unchanged outside scope\n+tampered\n"
            with pytest.raises(WorkbenchError):
                async with checkout.prepare({**intent, "diff_hash": hashlib.sha256(malicious_patch).hexdigest()}, malicious_patch, inventory):
                    pass
            (seed / "python/link").symlink_to("unchanged.txt")
            await git("add", "python/link")
            link_tree = await git("write-tree")
            link_base = await git("commit-tree", link_tree, "-p", base, data=b"Symlink fixture\n")
            with pytest.raises(WorkbenchError, match="符号链接"):
                async with checkout.prepare({**intent, "base_sha": link_base}, patch, inventory):
                    pass

    from pathlib import Path

    asyncio.run(exercise())
