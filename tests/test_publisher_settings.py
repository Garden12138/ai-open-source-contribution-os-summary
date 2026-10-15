from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api import create_app
from app.audit import AuditService
from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.models import Job
from app.provenance import canonical_json
from app.providers.model_secrets import GatewaySecretStore, management_auth
from app.publisher_accounts import PROBE, account_record, account_status, append_account
from app.publisher_service import AccountProbeWorker, create_publisher_app, probe_github_account
from app.workbench import Workbench, WorkbenchError
from tests.test_model_settings import envelope

KEY = bytes.fromhex("ab" * 32)
TOKEN = "ghp_PublisherCanary1234567890123456789012"


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'business.db'}",
        artifact_root=str(tmp_path / "artifacts"), publisher_mode="gh", publisher_management_key=KEY)
    db = Database(settings.database_url)
    db.create_schema()
    vault = GatewaySecretStore(str(tmp_path / "private"))
    app = create_publisher_app(settings, db, vault, run_workers=False)
    with TestClient(app) as client:
        yield db, settings, vault, client


def manage(client, operation, **payload):
    body = canonical_json({"operation": operation, **payload}).encode()
    return client.post("/internal/settings", content=body,
        headers={"X-Publisher-Management": management_auth(KEY, body)})


def save(fixture, key="one", expected=None):
    db, settings, vault, client = fixture
    grant = manage(client, "grant", connection_id=key).json()
    sealed = envelope(vault, grant, TOKEN)
    response = manage(client, "save", connection_id=key, envelope=sealed,
                      key=key, expected_hash=expected)
    assert response.status_code == 200, response.text
    return response.json()["job_id"], sealed


async def good_probe(token):
    assert token == TOKEN
    return {"login": "contributor", "user_id": 123, "scopes": ["public_repo"]}


def drive(fixture, probe=good_probe):
    db, settings, vault, client = fixture
    worker = AccountProbeWorker(db, vault, worker_id="test-publisher", probe=probe)
    return asyncio.run(worker.run_once())


def test_private_encrypted_save_persistent_probe_and_idempotency(fixture, caplog):
    db, settings, vault, client = fixture
    job_id, sealed = save(fixture)
    replay = manage(client, "save", connection_id="one", envelope=sealed, key="one", expected_hash=None)
    assert replay.json()["job_id"] == job_id
    assert manage(client, "save", connection_id="different", envelope=sealed, key="one", expected_hash=None).status_code == 409
    pending = manage(client, "status").json()
    assert pending["state"] == "pending" and not pending["connected"]
    assert "credential_ref" not in pending
    assert drive(fixture).state == "succeeded"
    result = manage(client, "status").json()
    assert result["connected"] and result["login"] == "contributor"
    assert result["scopes"] == ["public_repo"]
    with db.session() as session:
        assert AuditService(session).verify().valid
        dumped = "\n".join(session.connection().connection.driver_connection.iterdump())
        assert TOKEN not in dumped and sealed["ciphertext"] not in dumped
        assert [j.kind for j in session.scalars(select(Job))] == [PROBE]
    assert TOKEN.encode() not in vault.path.read_bytes()
    assert TOKEN not in caplog.text
    assert GatewaySecretStore(str(vault.root)).read(account_reference(db)) == TOKEN


def account_reference(db):
    with db.session() as session:
        return account_record(session).payload["credential_ref"]


def test_management_auth_and_size_rejection(fixture):
    _, _, _, client = fixture
    body = b'{"operation":"status"}'
    assert client.post("/internal/settings", content=body).status_code == 403
    assert client.post("/internal/settings", content=body, headers={
        "X-Publisher-Management": management_auth(KEY, body, timestamp=1)}).status_code == 403
    assert client.post("/internal/settings", content=b"x" * 24001).status_code == 413
    assert manage(client, "arbitrary").status_code == 422


def test_stale_updates_pending_probes_and_unresolved_publication_block_switch(fixture):
    db, _, _, client = fixture
    job_id, _ = save(fixture)
    current = manage(client, "status").json()["account_hash"]
    assert manage(client, "disconnect", expected_hash=current, key="busy").status_code == 409
    assert drive(fixture).state == "succeeded"
    current = manage(client, "status").json()["account_hash"]
    assert manage(client, "disconnect", expected_hash="f" * 64, key="stale").status_code == 409
    with db.session() as session:
        job, _ = JobService(session).enqueue(kind="publication_write", idempotency_key="write", payload={})
        JobService(session).request_cancel(job.id)
    assert manage(client, "disconnect", expected_hash=current, key="unresolved").status_code == 409


def test_disconnect_invalidates_preview_and_removes_local_credentials_idempotently(fixture):
    db, _, vault, client = fixture
    save(fixture)
    drive(fixture)
    before = manage(client, "status").json()
    reference = account_reference(db)
    result = manage(client, "disconnect", expected_hash=before["account_hash"], key="disconnect")
    assert result.json() == {"disconnected": True}
    assert manage(client, "disconnect", expected_hash=before["account_hash"], key="disconnect").json() == result.json()
    assert not vault.has(reference)
    assert manage(client, "status").json()["state"] == "disconnected"
    with db.session() as session:
        from app.publisher_accounts import require_account
        with pytest.raises(WorkbenchError):
            require_account(session, before["account_hash"])


def test_failed_or_cancelled_probe_can_be_retested_without_secret_reentry(fixture, caplog):
    _, _, _, client = fixture
    save(fixture)
    async def broken(token):
        raise RuntimeError("upstream " + token)
    assert drive(fixture, broken).state == "failed"
    failed = manage(client, "status").json()
    assert failed["state"] == "verification_failed" and TOKEN not in json.dumps(failed)
    assert TOKEN not in caplog.text
    response = manage(client, "test", expected_hash=failed["account_hash"], key="retry")
    assert response.status_code == 200
    assert drive(fixture).state == "succeeded"


def test_cancel_and_late_result_cannot_activate_a_newer_account(fixture):
    db, _, _, client = fixture
    job_id, _ = save(fixture)
    with db.session() as session:
        JobService(session).request_cancel(job_id)
    old = manage(client, "status").json()
    assert old["state"] == "verification_failed"
    assert manage(client, "test", expected_hash=old["account_hash"], key="retry").status_code == 200
    async def late(token):
        with db.session() as session:
            append_account(session, {"state": "disconnected"}, correlation_id="forced-new-state")
            session.commit()
        return await good_probe(token)
    assert drive(fixture, late).state == "failed"
    assert manage(client, "status").json()["state"] == "disconnected"


def test_api_sealed_contract_auth_csrf_and_no_raw_secret_echo(fixture):
    db, settings, vault, service = fixture
    api = create_app(replace(settings, publisher_mode="none", local_access_token="test-local-token"))
    async def proxy(_settings, operation, payload):
        response = manage(service, operation, **payload)
        from fastapi import HTTPException
        if response.status_code != 200:
            raise HTTPException(response.status_code, "请求未完成")
        return response.json()
    api.state.publisher_manage = proxy
    with TestClient(api) as client:
        root = "/api/v1/publisher-settings"
        assert client.get(root).json()["available"]
        headers = {"X-ContribOS-Token": "test-local-token", "Idempotency-Key": "api"}
        assert client.post(root + "/credential-grants", json={"connection_id":"api"}).status_code == 401
        assert client.post(root + "/credential-grants", json={"connection_id":"api"},
                           headers={**headers, "Origin":"http://testserver"}).status_code == 403
        grant = client.post(root + "/credential-grants", json={"connection_id":"api"}, headers=headers).json()
        bad = client.post(root + "/credentials", json={"token":TOKEN}, headers=headers)
        assert bad.status_code == 422 and TOKEN not in bad.text
        accepted = client.post(root + "/credentials", json={"connection_id":"api", "expected_hash":None,
            "envelope":envelope(vault, grant, TOKEN)}, headers=headers)
        assert accepted.status_code == 200 and TOKEN not in accepted.text
        assert drive(fixture).state == "succeeded"
        assert client.get(root).json()["login"] == "contributor"


def test_github_probe_only_reads_user_and_checks_scope(monkeypatch):
    import app.publisher_service as module
    calls = []
    scope = ["public_repo"]
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"login":"contributor","id":123,"untrusted_field":TOKEN},
                              headers={"X-OAuth-Scopes":scope[0]})
    original = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(probe_github_account(TOKEN))
    assert TOKEN not in json.dumps(result)
    assert [(r.method, str(r.url)) for r in calls] == [("GET","https://api.github.com/user")]
    scope[0] = "read:user"
    with pytest.raises(WorkbenchError, match="public_repo"):
        asyncio.run(probe_github_account(TOKEN))


def test_settings_account_rotation_invalidates_existing_publication_preview(tmp_path):
    from tests.test_publication_workbench import ready
    from app.publication import confirm_publication
    db, settings, task_id, transport, worker = ready(tmp_path)
    with db.session() as session:
        account = append_account(session, {"state":"connected", "credential_ref":"cred-" + "a" * 32,
            "login":"contributor"}, correlation_id="test-connect")
        session.commit()
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        preview = wb.latest(task_id, "publication_prepared")
        # Create a managed, exact account-bound preview from the verified fake
        # transport; no real network or external write occurs in this test.
        bound = wb.append(task_id, "publication_prepared", {**preview.payload, "publisher_account_hash":account.event_hash}, key="bound-preview")
        append_account(session, {"state":"disconnected"}, correlation_id="disconnect")
        session.commit()
        with pytest.raises(WorkbenchError, match="账号"):
            confirm_publication(wb, task_id, intent_hash=bound.record_hash, nonce=bound.payload["nonce"])
        assert wb.latest(task_id, "publication_confirmed") is None
        assert all(method == "GET" for method, _, _ in transport.calls)


def test_publisher_lease_recovery_is_scoped(fixture):
    db, _, _, _ = fixture
    stamp = datetime.now(timezone.utc)
    with db.session() as session:
        jobs = JobService(session)
        own, _ = jobs.enqueue(kind=PROBE, idempotency_key="own", payload={}, now=stamp)
        other, _ = jobs.enqueue(kind="sandbox_stage", idempotency_key="other", payload={}, now=stamp)
        jobs.lease_next(worker_id="one", kinds=(PROBE,), lease_seconds=1, now=stamp)
        jobs.lease_next(worker_id="two", kinds=("sandbox_stage",), lease_seconds=1, now=stamp)
        assert jobs.recover_expired(now=stamp + timedelta(seconds=2), kinds=(PROBE,)) == (own.id,)
        assert jobs.get(other.id).state == "leased"


def test_settings_backed_publisher_preserves_exact_preview_and_confirmation(tmp_path, monkeypatch):
    from tests.test_publication_workbench import ready, FakeCheckout
    from app.publication import request_publication, confirm_publication
    from app.publisher_service import ManagedPublicationWorker
    import app.publisher_service as module
    db, settings, task_id, transport, _ = ready(tmp_path)
    vault = GatewaySecretStore(str(tmp_path / "publisher-private"))
    grant = vault.grant("managed")
    reference = vault.save("managed", envelope(vault, grant, TOKEN))["credential_ref"]
    with db.session() as session:
        for job in session.scalars(select(Job).where(Job.kind == "publication_prepare")):
            JobService(session).request_cancel(job.id)
        account = append_account(session, {"state":"connected", "credential_ref":reference,
            "login":"contributor", "user_id":123, "scopes":["public_repo"]}, correlation_id="managed-connect")
        session.commit()
        wb = Workbench(session, settings.artifact_root)
        request_publication(wb, task_id, title="Improve behavior", body="Tested change", key="managed-prepare",
                            publisher_account_hash=account.event_hash)
    transport.env = {}
    monkeypatch.setattr(module, "GhPublicationTransport", lambda: transport)
    worker = ManagedPublicationWorker(db, settings, vault, worker_id="managed-test")
    worker.checkout = FakeCheckout()
    assert asyncio.run(worker.run_once()).state == "succeeded"
    assert "GH_TOKEN" not in transport.env
    assert all(method == "GET" for method, _, _ in transport.calls)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        intent = wb.latest(task_id, "publication_prepared")
        assert intent.payload["publisher_account_hash"] == account.event_hash
        assert wb.latest(task_id, "publication_confirmed") is None
        job = confirm_publication(wb, task_id, intent_hash=intent.record_hash, nonce=intent.payload["nonce"])
        assert job.payload["publisher_account_hash"] == account.event_hash
    assert asyncio.run(worker.run_once()).state == "succeeded"
    assert "GH_TOKEN" not in transport.env
    with db.session() as session:
        assert Workbench(session, settings.artifact_root).latest(task_id, "publication_completed")
        dumped = "\n".join(session.connection().connection.driver_connection.iterdump())
        assert TOKEN not in dumped


def test_management_key_bootstrap_preserves_other_env_and_is_idempotent(tmp_path):
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("ensure_publisher_key", "scripts/ensure-publisher-key.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / ".env"
    path.write_text("UNRELATED_SETTING=value\nPUBLISHER_MANAGEMENT_KEY=\n")
    module.ensure_key(path)
    first = path.read_text()
    module.ensure_key(path)
    assert path.read_text() == first and first.startswith("UNRELATED_SETTING=value\n")
    assert len(first.split("PUBLISHER_MANAGEMENT_KEY=")[1].strip()) == 64
    assert path.stat().st_mode & 0o077 == 0
