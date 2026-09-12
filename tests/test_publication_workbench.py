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

    async def api(self, method, path, body=None, *, missing=False):
        self.calls.append((method, path, body))
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


def test_trusted_checkout_reconstructs_real_commit_and_rejects_drift(
    tmp_path, monkeypatch
):
    import difflib
    import hashlib
    import app.publication as publication

    seed = tmp_path / "trusted-fixture"
    seed.mkdir()
    original = b"def value():\n    return 1\n"
    updated = b"def value():\n    return 2\n"
    (seed / "main.py").write_bytes(original)
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
        await git("add", "main.py")
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
                base,
            ]
        return await actual_process(argv, **kwargs)

    monkeypatch.setattr(publication, "bounded_process", offline_fetch)
    patch = "".join(
        difflib.unified_diff(
            original.decode().splitlines(True),
            updated.decode().splitlines(True),
            fromfile="a/main.py",
            tofile="b/main.py",
        )
    ).encode()

    def entry(data):
        return {
            "path": "main.py",
            "kind": "file",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "executable": False,
        }

    inventory = {
        "baseline_inventory": [entry(original)],
        "result_inventory": [entry(updated)],
        "changed_paths": ["main.py"],
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

    async def exercise():
        checkout = publication.TrustedCheckout()
        async with checkout.prepare(intent, patch, inventory) as (
            directory,
            head,
            tree,
        ):
            assert (Path(directory) / "main.py").read_bytes() == updated
            assert len(head) == 40 and head != base
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

    from pathlib import Path

    asyncio.run(exercise())
