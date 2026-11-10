"""Synthetic credential material only. Never opens the real Gateway or Provider."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.providers import gateway_storage as storage
from app.providers.gateway_storage import GatewayStorageMaintenance as Maintenance, PersistentGatewaySecretStore as Store, GatewayStorageError
from app.providers.gateway_http import create_model_gateway_app
from app.providers.model_secrets import GatewaySecretStore, management_auth
from app.provenance import canonical_json, content_hash
from tests.test_model_settings import envelope, configure


SECRET = "synthetic-credential-only"
KEY = bytes(range(32))
REAL_EPHEMERAL = storage._is_ephemeral
NATIVE_CREATE_CONNECTION = socket.create_connection


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    # No production switch permits ephemeral roots. Only tests patch this predicate.
    monkeypatch.setattr(storage, "_is_ephemeral", lambda path: False)
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden in synthetic Gateway storage test")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def initialized(tmp_path):
    root = tmp_path / "vault"
    pointer = Maintenance.initialize(root, intent="new-install")
    return root, pointer


def enrolled(root, pointer):
    with Store(root, pointer["store_id"]) as store:
        grant = store.grant("connection-one")
        sealed = envelope(store, grant, SECRET)
        result = store.save("connection-one", sealed)
        assert store.save("connection-one", sealed) == result
        return result["credential_ref"]


def generation(root, pointer):
    return root / "generations" / pointer["generation"]


def test_explicit_init_enrollment_and_reopen_preserve_material(tmp_path):
    root, p = initialized(tmp_path)
    ref = enrolled(root, p)
    before = {n: (generation(root, p) / n).read_bytes() for n in storage.FILES}
    with Store(root, p["store_id"]) as store:
        assert store.read(ref) == SECRET
        assert store.storage_readiness()["ready"]
        assert store.credential_readiness(ref)["credential_ready"]
    assert before == {n: (generation(root, p) / n).read_bytes() for n in storage.FILES}
    with pytest.raises(GatewayStorageError, match="Credential storage"):
        Maintenance.initialize(root, intent="reconfigure-new-store")


def test_open_missing_never_creates_files(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(GatewayStorageError): Store(root, "a" * 32)
    assert not root.exists()


@pytest.mark.parametrize("name", storage.FILES)
def test_missing_material_after_startup_blocks_without_recreation(tmp_path, name):
    root, p = initialized(tmp_path)
    ref = enrolled(root, p)
    with Store(root, p["store_id"]) as store:
        target = generation(root, p) / name
        target.unlink()
        with pytest.raises(GatewayStorageError): store.read(ref)
        assert not target.exists()
    with pytest.raises(GatewayStorageError): Store(root, p["store_id"])
    assert not target.exists()


@pytest.mark.parametrize("case", ["zero", "credentials", "grants", "column", "corrupt", "version"])
def test_bad_schema_or_database_rejected(tmp_path, case):
    root, p = initialized(tmp_path)
    target = generation(root, p) / "credentials.sqlite3"
    if case in {"zero", "corrupt"}: target.write_bytes(b"" if case == "zero" else b"not sqlite")
    else:
        with sqlite3.connect(target) as db:
            if case in {"credentials", "grants"}: db.execute("DROP TABLE " + case)
            elif case == "column": db.execute("ALTER TABLE credentials ADD COLUMN surprise TEXT")
            else: db.execute("PRAGMA user_version=2")
    before = target.read_bytes()
    with pytest.raises(GatewayStorageError): Store(root, p["store_id"])
    assert target.read_bytes() == before


@pytest.mark.parametrize("name", ["master.key", "envelope.pem"])
def test_wrong_generation_key_rejected_even_empty_database(tmp_path, name):
    root, p = initialized(tmp_path)
    other = tmp_path / "other"
    q = Maintenance.initialize(other, intent="new-install")
    target = generation(root, p) / name
    target.write_bytes((generation(other, q) / name).read_bytes())
    with pytest.raises(GatewayStorageError) as exc: Store(root, p["store_id"])
    assert exc.value.code == "gateway_storage_key_mismatch"


@pytest.mark.parametrize("name,mode", [("master.key", 0o644), ("credentials.sqlite3", 0), ("envelope.pem", 0o400)])
def test_actual_bad_permissions_fail_without_chmod(tmp_path, name, mode):
    root, p = initialized(tmp_path)
    path = generation(root, p) / name
    path.chmod(mode)
    with pytest.raises(GatewayStorageError) as exc: Store(root, p["store_id"])
    assert exc.value.code == "gateway_storage_permissions"
    assert path.stat().st_mode & 0o777 == mode


def test_owner_mismatch_is_rejected(tmp_path, monkeypatch):
    root, p = initialized(tmp_path)
    monkeypatch.setattr(storage.os, "geteuid", lambda: 123456)
    with pytest.raises(GatewayStorageError): Store(root, p["store_id"])


def test_symlinks_hardlinks_and_wrong_store_are_rejected(tmp_path):
    root, p = initialized(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(GatewayStorageError): Store(link, p["store_id"])
    with pytest.raises(GatewayStorageError): Store(root, "b" * 32)
    key = generation(root, p) / "master.key"
    os.link(key, tmp_path / "extra-key-link")
    with pytest.raises(GatewayStorageError): Store(root, p["store_id"])


def test_bad_paths_and_real_ephemeral_policy(tmp_path, monkeypatch):
    for path in ("", "relative/path", str(tmp_path / ".." / "escape")):
        with pytest.raises(GatewayStorageError): storage.persistent_root(path)
    monkeypatch.setattr(storage, "_is_ephemeral", REAL_EPHEMERAL)
    with pytest.raises(GatewayStorageError) as exc: storage.persistent_root("/tmp/unusable-private-store")
    assert exc.value.code == "gateway_storage_path_ephemeral"


def test_replaced_valid_database_and_parent_are_detected(tmp_path):
    root, p = initialized(tmp_path)
    ref = enrolled(root, p)
    with Store(root, p["store_id"]) as store:
        path = generation(root, p) / "credentials.sqlite3"
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes()); replacement.chmod(0o600)
        replacement.replace(path)
        with pytest.raises(GatewayStorageError): store.read(ref)
    with Store(root, p["store_id"]) as store:
        moved = tmp_path / "moved"
        root.rename(moved); root.symlink_to(moved, target_is_directory=True)
        with pytest.raises(GatewayStorageError): store.read(ref)


def test_maintenance_refuses_active_service_and_stale_cas(tmp_path):
    root, p = initialized(tmp_path)
    with Store(root, p["store_id"]):
        with pytest.raises(GatewayStorageError) as exc:
            Maintenance.backup(root, tmp_path / "backup", store_id=p["store_id"], expected_generation=p["generation"])
        assert exc.value.code == "gateway_storage_busy"
    with pytest.raises(GatewayStorageError):
        Maintenance.backup(root, tmp_path / "backup", store_id=p["store_id"], expected_generation="c" * 32)


def test_backup_restore_atomic_cutover_rollback_and_mutation_guard(tmp_path):
    root, p = initialized(tmp_path)
    ref = enrolled(root, p)
    backup = tmp_path / "backup"
    digest = Maintenance.backup(root, backup, store_id=p["store_id"], expected_generation=p["generation"])
    q = Maintenance.restore(root, backup, store_id=p["store_id"], expected_generation=p["generation"], snapshot_hash=digest)
    with Store(root, p["store_id"]) as store: assert store.read(ref) == SECRET
    r = Maintenance.rollback(root, store_id=p["store_id"], expected_generation=q["generation"])
    assert r["generation"] == p["generation"]
    q = Maintenance.restore(root, backup, store_id=p["store_id"], expected_generation=r["generation"], snapshot_hash=digest)
    with Store(root, p["store_id"]) as store: store.grant("new-management-write")
    with pytest.raises(GatewayStorageError) as exc:
        Maintenance.rollback(root, store_id=p["store_id"], expected_generation=q["generation"])
    assert exc.value.code == "gateway_storage_rollback_unsafe"


def test_corrupt_backup_cannot_switch_pointer(tmp_path):
    root, p = initialized(tmp_path)
    backup = tmp_path / "backup"
    digest = Maintenance.backup(root, backup, store_id=p["store_id"], expected_generation=p["generation"])
    (backup / "snapshot.json").write_text('{}')
    with pytest.raises(GatewayStorageError):
        Maintenance.restore(root, backup, store_id=p["store_id"], expected_generation=p["generation"], snapshot_hash=digest)
    assert json.loads((root / "current.json").read_text()) == p


def test_legacy_import_is_explicit_forward_copy_and_publisher_unchanged(tmp_path):
    source = tmp_path / "publisher-or-legacy"
    publisher = GatewaySecretStore(str(source))
    result = publisher.save("connection-one", envelope(publisher, publisher.grant("connection-one"), SECRET))
    ref = result["credential_ref"]
    before = {n: (source / n).read_bytes() for n in storage.FILES[:3]}
    digest = content_hash({n: hashlib.sha256(v).hexdigest() for n, v in before.items()})
    p = Maintenance.import_legacy(source, tmp_path / "imported", material_hash=digest, envelope_key_id=publisher.key_id)
    assert before == {n: (source / n).read_bytes() for n in storage.FILES[:3]}
    assert GatewaySecretStore(str(source)).read(ref) == SECRET
    with Store(tmp_path / "imported", p["store_id"]) as store: assert store.read(ref) == SECRET
    with sqlite3.connect(source / "credentials.sqlite3") as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        db.execute('DELETE FROM credentials'); db.execute('DELETE FROM grants')
    publisher.grant("new-publisher-connection")  # Original Publisher management writes still work.


def test_empty_legacy_trio_does_not_prove_recovery(tmp_path):
    source = tmp_path / "legacy"
    legacy = GatewaySecretStore(str(source))
    digest = content_hash({n: hashlib.sha256((source / n).read_bytes()).hexdigest() for n in storage.FILES[:3]})
    with pytest.raises(GatewayStorageError) as exc:
        Maintenance.import_legacy(source, tmp_path / "adopt", material_hash=digest, envelope_key_id=legacy.key_id)
    assert exc.value.code == "gateway_storage_legacy_pairing_unproven"
    assert not (tmp_path / "adopt").exists()


CHILD = """
import os,sys,socket
from app.providers import gateway_storage as s
s._is_ephemeral=lambda path: False
def forbidden(*a,**kw): raise AssertionError('No outbound network in synthetic child')
socket.socket.connect=forbidden
socket.socket.connect_ex=forbidden
socket.create_connection=forbidden
"""


def child(code, *args, wait=True):
    options = dict(env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path.cwd()), "PYTHONDONTWRITEBYTECODE": "1"},
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    command = [sys.executable, "-B", "-c", CHILD + code, *map(str, args)]
    return subprocess.run(command, timeout=30, **options) if wait else subprocess.Popen(command, **options)


@pytest.mark.parametrize("phase", ["generation_staged", "before_pointer", "after_pointer"])
def test_process_crash_during_cutover_keeps_complete_generation(tmp_path, phase):
    root, p = initialized(tmp_path)
    ref = enrolled(root, p)
    backup = tmp_path / "backup"
    digest = Maintenance.backup(root, backup, store_id=p["store_id"], expected_generation=p["generation"])
    result = child("""
s._checkpoint=lambda phase: os._exit(77) if phase==sys.argv[5] else None
s.GatewayStorageMaintenance.restore(sys.argv[1],sys.argv[2],store_id=sys.argv[3],expected_generation=sys.argv[4],snapshot_hash=sys.argv[6])
""", root, backup, p["store_id"], p["generation"], phase, digest)
    assert result.returncode == 77, result.stderr
    pointer = json.loads((root / "current.json").read_text())
    assert (pointer["generation"] == p["generation"]) == (phase != "after_pointer")
    with Store(root, p["store_id"]) as store: assert store.read(ref) == SECRET
    assert (generation(root, p) / "master.key").exists()


def test_real_hot_journal_recovered_on_copy_after_unclean_process_exit(tmp_path):
    root, p = initialized(tmp_path)
    ref = enrolled(root, p)
    path = generation(root, p) / "credentials.sqlite3"
    result = child("""
import sqlite3
from pathlib import Path
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=rw',uri=True)
db.execute('PRAGMA cache_size=2')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE credentials SET ciphertext=zeroblob(2000000)')
os._exit(78)
""", path)
    assert result.returncode == 78, result.stderr
    journal = Path(str(path) + "-journal")
    assert journal.exists() and journal.stat().st_size > 0
    before = (path.read_bytes(), journal.read_bytes())
    with pytest.raises(GatewayStorageError) as exc: Store(root, p["store_id"])
    assert exc.value.code == "gateway_storage_recovery_required"
    q = Maintenance.recover(root, store_id=p["store_id"], expected_generation=p["generation"])
    assert q["generation"] != p["generation"]
    with Store(root, p["store_id"]) as store: assert store.read(ref) == SECRET
    assert before == (path.read_bytes(), journal.read_bytes())


def test_real_gateway_process_restart_retains_credential_and_readiness(tmp_path, monkeypatch):
    root, p = initialized(tmp_path)
    ref = enrolled(root, p)
    from app.config import Settings
    from app.database import Database
    from app.model_settings import ModelSettingsService
    db = Database(f'sqlite:///{tmp_path/"fixture.db"}'); db.create_schema()
    with db.session() as session:
        profile = ModelSettingsService(session).profile(configure(session, credential_ref=ref).id)
    before = {n: (generation(root, p) / n).read_bytes() for n in storage.FILES}
    original_connect = socket.socket.connect
    original_create = socket.create_connection
    # Permit only the exact ephemeral loopback listener owned by each child.
    import _socket
    native_connect = _socket.socket.connect
    pids = []
    for _ in range(2):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        process = child("""
from argparse import Namespace
from app.config import Settings
from app.cli import run_model_gateway
run_model_gateway(Settings(model_gateway_secret_root=sys.argv[1],model_gateway_store_id=sys.argv[2],model_gateway_signing_key=bytes(range(32)),model_gateway_management_key=bytes(range(32))),Namespace(host='127.0.0.1',port=int(sys.argv[3])))
""", root, p["store_id"], port, wait=False)
        pids.append(process.pid)
        def local_only(sock, address):
            if address == ('127.0.0.1', port): return native_connect(sock, address)
            return original_connect(sock, address)
        monkeypatch.setattr(socket.socket, 'connect', local_only)
        def local_create(address, *args, **kwargs):
            if address == ('127.0.0.1', port): return NATIVE_CREATE_CONNECTION(address, *args, **kwargs)
            return original_create(address, *args, **kwargs)
        monkeypatch.setattr(socket, 'create_connection', local_create)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=1) as client:
                for attempt in range(100):
                    try:
                        if client.get('/health').status_code == 200: break
                    except httpx.TransportError: pass
                    assert process.poll() is None
                    time.sleep(0.05)
                else: pytest.fail('Owned synthetic Gateway did not become live')
                settings = Settings(model_gateway_base_url=f'http://127.0.0.1:{port}/v1', model_gateway_store_id=p['store_id'], model_gateway_management_key=KEY)
                body=canonical_json({'operation':'credential_readiness','nonce':'a'*32,'store_id':p['store_id'],'profile':profile}).encode()
                from app.providers.gateway_readiness import readonly_key
                response=client.post('/internal/readiness',content=body,headers={'X-Gateway-Readiness':management_auth(readonly_key(KEY),body)})
                assert response.status_code == 200, response.text
                from app.providers.gateway_readiness import validate_proof
                validate_proof(settings,profile,response.json())
        finally:
            process.terminate()
            process.communicate(timeout=10)
            monkeypatch.setattr(socket.socket,'connect',original_connect)
            monkeypatch.setattr(socket,'create_connection',original_create)
    assert pids[0] != pids[1]
    assert before == {n: (generation(root,p)/n).read_bytes() for n in storage.FILES}
    db.close()


class NoProviderGateway:
    max_request_bytes = 24000
    def __init__(self, store): self.store = store; self.calls = 0
    async def create_response(self, **kwargs):
        self.store.storage_readiness()
        self.calls += 1
        raise AssertionError("Provider is forbidden")


def test_authenticated_credential_readiness_is_readonly_and_not_http_liveness(tmp_path):
    root, p = initialized(tmp_path); ref = enrolled(root, p)
    from app.config import Settings
    from app.database import Database
    from app.model_settings import ModelSettingsService
    from app.providers.gateway_readiness import credential_readiness
    db=Database(f'sqlite:///{tmp_path/"model.db"}'); db.create_schema()
    with db.session() as session: profile=ModelSettingsService(session).profile(configure(session,credential_ref=ref).id)
    settings=Settings(model_gateway_store_id=p['store_id'],model_gateway_management_key=KEY,model_gateway_base_url='http://fixture.invalid/v1')
    with Store(root,p['store_id']) as store:
        gateway=NoProviderGateway(store); app=create_model_gateway_app(gateway,secret_store=store,management_key=KEY)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False)) as client:
                assert (await client.post('http://fixture.invalid/internal/readiness',json={})).status_code==403
                proof=await credential_readiness(settings,profile,client=client)
                assert proof['credential_ready']
                (generation(root,p)/'master.key').unlink()
                assert (await client.get('http://fixture.invalid/health')).status_code==200
                from app.providers.contracts import ProviderRunError
                with pytest.raises(ProviderRunError) as exc: await credential_readiness(settings,profile,client=client)
                assert not exc.value.retryable
                response=await client.post('http://fixture.invalid/v1/chat/completions',json={})
                assert response.status_code==503
                assert str(root) not in response.text and ref not in response.text and SECRET not in response.text
        asyncio.run(run()); assert gateway.calls==0
    db.close()


@pytest.mark.parametrize('case',['expired','forged','wrong-profile','wrong-store'])
def test_readiness_proof_rejects_stale_forged_or_drifted_binding(tmp_path,case):
    from app.config import Settings
    from app.providers.gateway_readiness import validate_proof
    from app.providers.contracts import ProviderRunError
    settings=Settings(model_gateway_store_id='a'*32,model_gateway_management_key=KEY)
    profile={'connection':{'credential_ref':'cred-'+'1'*32}}
    proof={'version':storage.VERSION,'ready':True,'credential_ready':True,'store_id':'a'*32,
           'profile_hash':content_hash(profile),'reference_hash':hashlib.sha256(profile['connection']['credential_ref'].encode()).hexdigest(),
           'checked_at':int(time.time()),'nonce':'b'*32,'generation':'c'*32}
    if case=='expired': proof['checked_at']-=60
    if case=='wrong-profile': proof['profile_hash']='d'*64
    if case=='wrong-store': proof['store_id']='e'*32
    proof['proof_hash']=content_hash(proof)
    from app.providers.gateway_readiness import readonly_key
    proof['proof_auth']=management_auth(readonly_key(KEY),canonical_json(proof).encode())
    if case=='forged': proof['ready']=False
    with pytest.raises(ProviderRunError): validate_proof(settings,profile,proof)


@pytest.mark.parametrize('code,retryable',[
    ('gateway_storage_missing',False),('gateway_storage_key_mismatch',False),
    ('gateway_storage_schema_invalid',False),('gateway_storage_database_invalid',False),
    ('gateway_storage_permissions',False),('gateway_upstream_failure',True),('gateway_internal_error',True)])
def test_both_consumers_classify_deterministic_storage_separately(code,retryable):
    from types import SimpleNamespace
    from app.providers.studio import StudioRunner
    from app.providers.nvidia_nim import NvidiaNimGatewayRunner
    from app.providers.contracts import ProviderRunError
    from tests.test_provider_gateway import _invocation
    broker=SimpleNamespace(issue=lambda invocation:SimpleNamespace(base_url='http://fixture.invalid/v1',token='synthetic'))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(503,json={'error':{'code':code,'message':'safe fixture'}}))) as client:
            with pytest.raises(ProviderRunError) as a:
                await StudioRunner({'timeout_seconds':10},broker=broker,client=client)._post_data(broker.issue(None),{})
            with pytest.raises(ProviderRunError) as b:
                await NvidiaNimGatewayRunner(broker=broker,client=client).complete(_invocation())
            assert a.value.retryable is b.value.retryable is retryable
    asyncio.run(exercise())


@pytest.mark.parametrize('pre_dispatch',[True,False])
def test_actual_agent_journal_storage_failure_never_spends_provider_retry(tmp_path,pre_dispatch):
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.models import Job
    from app.agent_v2.adapter import StructuredModelAdapter
    from app.agent_v2.journal import AgentJournal
    from app.workbench import Workbench
    from app.providers.studio import StudioRunner
    from app.model_settings import ProfileInput
    from tests.test_agent_v2_loop import fixture
    from tests.test_nvidia_workflows import _identity
    db,settings,task,attempt,_,_,worker,_,_,sid=fixture(tmp_path)
    root,p=initialized(tmp_path)
    profile={**ProfileInput(name='fixture',connection_id='fixture',model=settings.implementation_model).model_dump(),
             'connection':{'provider':'openai_compatible','base_url':'https://models.example.com/v1','credential_ref':'cred-'+'1'*32}}
    broker=SimpleNamespace(issue=lambda invocation:SimpleNamespace(base_url='http://fixture.invalid/v1',token='synthetic'))
    with Store(root,p['store_id']) as store:
        gateway=NoProviderGateway(store); app=create_model_gateway_app(gateway,secret_store=store,management_key=KEY)
        (generation(root,p)/'credentials.sqlite3').unlink()
        if pre_dispatch:
            worker.settings=replace(settings,model_gateway_signing_key=KEY,model_gateway_management_key=KEY,
                model_gateway_store_id=p['store_id'],model_gateway_base_url='http://fixture.invalid/v1')
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False)) as client:
                runner=StudioRunner(profile,broker=broker,client=client)
                worker.adapter_factory=lambda session,spec:StructuredModelAdapter(runner,_identity(settings.implementation_model),attempt)
                assert (await worker.run_once()).state=='succeeded'
                assert await worker.run_once() is None
        asyncio.run(exercise())
        assert gateway.calls==0
    with db.session() as session:
        journal=AgentJournal(Workbench(session,settings.artifact_root),task,sid)
        cp=journal.checkpoint()
        assert cp.state=='blocked' and cp.usage.get('provider_retries',0)==0
        assert cp.usage.get('model_calls',0)==(0 if pre_dispatch else 1)
        assert not list(session.scalars(select(Job).where(Job.state.in_(['queued','leased','running']))))
        starts=[e for e in journal.events() if e.kind=='agent_v2_model_started']
        rejects=[e for e in journal.events() if e.kind=='agent_v2_model_rejected']
        assert len(starts)==len(rejects)==(0 if pre_dispatch else 1)
        if not pre_dispatch:
            receipt=rejects[0].payload['provider_failure_receipt']
            assert receipt['reservation_retained'] and receipt['reported_usage'] is None
            assert cp.usage['output_tokens']==4096 and cp.usage['input_tokens']>0
    db.close()


def test_domain_authorization_without_readiness_never_mutates_business(tmp_path):
    from app.contribution_workflow import authorize_execution
    from app.workbench import Workbench,WorkbenchError
    from tests.test_agent_v2_orchestrator import prepared
    db,settings,task,_,_=prepared(tmp_path)
    settings=replace(settings,model_gateway_signing_key=KEY,model_gateway_management_key=KEY,model_gateway_store_id='a'*32)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root); plan=wb.latest_plan(task)
        before=[(e.id,e.record_hash) for e in wb.history(task)]
        with pytest.raises(WorkbenchError) as exc:
            authorize_execution(wb,task,plan_id=plan.id,plan_hash=plan.record_hash,key='must-not-authorize',settings=settings)
        assert exc.value.code=='model_gateway_storage_unavailable'
        session.commit()
        assert [(e.id,e.record_hash) for e in wb.history(task)]==before
        assert not any(e.idempotency_key=='authorize:must-not-authorize' for e in wb.history(task))
    db.close()


def test_macos_launcher_matches_persistent_default_and_contains_only_key_paths(tmp_path):
    import importlib.util
    import plistlib
    from unittest.mock import patch
    from app.config import Settings
    spec=importlib.util.spec_from_file_location('render_gateway',Path('scripts/render-model-gateway-launchagent.py'))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    home=Path('/Users/synthetic-owner')
    data=module.configuration(Path('/opt/contribos'),home,'a'*32)
    with patch('app.config.sys.platform','darwin'),patch('app.config.Path.home',return_value=home):
        assert Settings().model_gateway_secret_root==data['EnvironmentVariables']['MODEL_GATEWAY_SECRET_ROOT']
    assert data['ProgramArguments'][-1]=='18091'
    assert data['RunAtLoad'] is data['KeepAlive'] is False
    assert data['EnvironmentVariables']['MODEL_GATEWAY_STORE_ID']=='a'*32
    assert all('/tmp' not in str(v) for v in data.values())
    assert 'MODEL_GATEWAY_SIGNING_KEY' not in data['EnvironmentVariables']
    assert plistlib.loads(plistlib.dumps(data))==data


def test_host_key_files_must_be_private_and_single_source(tmp_path,monkeypatch):
    from app.config import _hex_key_env
    path=tmp_path/'runtime.key'; path.write_text(KEY.hex()); path.chmod(0o600)
    monkeypatch.delenv('MODEL_GATEWAY_MANAGEMENT_KEY',raising=False)
    monkeypatch.setenv('MODEL_GATEWAY_MANAGEMENT_KEY_FILE',str(path))
    assert _hex_key_env('MODEL_GATEWAY_MANAGEMENT_KEY')==KEY
    path.chmod(0o644)
    with pytest.raises(GatewayStorageError): _hex_key_env('MODEL_GATEWAY_MANAGEMENT_KEY')
    path.chmod(0o600)
    monkeypatch.setenv('MODEL_GATEWAY_MANAGEMENT_KEY','fixture-conflict')
    with pytest.raises(ValueError): _hex_key_env('MODEL_GATEWAY_MANAGEMENT_KEY')
    monkeypatch.delenv('MODEL_GATEWAY_MANAGEMENT_KEY')
    monkeypatch.delenv('MODEL_GATEWAY_MANAGEMENT_KEY_FILE')
    monkeypatch.setenv('MODEL_GATEWAY_READINESS_KEY_FILE',str(tmp_path/'must-not-read.key'))
    from app.cli import main
    from app.config import Settings
    from app.workbench import WorkbenchError
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:pytest.fail('Publisher must reject before reading model configuration')))
    with pytest.raises(WorkbenchError): main(['publisher-worker','--once'])


def test_readonly_readiness_capability_cannot_issue_management_grants(tmp_path):
    from app.providers.gateway_readiness import readonly_key
    root,p=initialized(tmp_path)
    with Store(root,p['store_id']) as store:
        gateway=NoProviderGateway(store)
        app=create_model_gateway_app(gateway,secret_store=store,management_key=KEY)
        with TestClient(app) as client:
            body=canonical_json({'operation':'grant','connection_id':'forbidden'}).encode()
            response=client.post('/internal/settings',content=body,
                headers={'X-Gateway-Management':management_auth(readonly_key(KEY),body)})
            assert response.status_code==403
            body=canonical_json({'operation':'storage_readiness','nonce':'a'*32,'store_id':p['store_id']}).encode()
            assert client.post('/internal/readiness',content=body,
                headers={'X-Gateway-Readiness':management_auth(KEY,body)}).status_code==403
            response=client.post('/internal/readiness',content=body,
                headers={'X-Gateway-Readiness':management_auth(readonly_key(KEY),body)})
            assert response.status_code==200 and response.json()['ready']
        with store._open('ro') as db: assert db.execute('SELECT count(*) FROM grants').fetchone()[0]==0
        assert gateway.calls==0


def test_writable_parent_and_broad_root_permissions_are_not_silently_repaired(tmp_path):
    root,p=initialized(tmp_path)
    root.chmod(0o755)
    with pytest.raises(GatewayStorageError): Store(root,p['store_id'])
    assert root.stat().st_mode & 0o777 == 0o755
    root.chmod(0o700); tmp_path.chmod(0o777)
    try:
        with pytest.raises(GatewayStorageError) as exc: Store(root,p['store_id'])
        assert exc.value.code=='gateway_storage_path_invalid'
    finally: tmp_path.chmod(0o700)


def test_crash_before_initial_pointer_never_reinitializes_partial_material(tmp_path):
    root=tmp_path/'partial'
    result=child("""
s._checkpoint=lambda phase: os._exit(79) if phase=='before_pointer' else None
s.GatewayStorageMaintenance.initialize(sys.argv[1],intent='new-install')
""",root)
    assert result.returncode==79
    assert not (root/'current.json').exists()
    before={str(f.relative_to(root)):f.read_bytes() for f in root.rglob('*') if f.is_file()}
    with pytest.raises(GatewayStorageError): Store(root,'a'*32)
    with pytest.raises(GatewayStorageError): Maintenance.initialize(root,intent='new-install')
    assert before=={str(f.relative_to(root)):f.read_bytes() for f in root.rglob('*') if f.is_file()}


def test_oversized_database_is_rejected_before_sqlite_scan(tmp_path):
    root,p=initialized(tmp_path)
    path=generation(root,p)/'credentials.sqlite3'
    with path.open('r+b') as target: target.truncate(32_000_001)
    with pytest.raises(GatewayStorageError) as exc: Store(root,p['store_id'])
    assert exc.value.code=='gateway_storage_limit'
