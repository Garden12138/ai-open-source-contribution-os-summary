"""Native resources require exact human consent, bounded input and offline use."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.native_resources import ARCHIVE, DOWNLOAD, binding, catalog, confirm, dependency_binding, prepare, view
from app.provenance import content_hash
from app.planner import PlanningService
from app.schemas import PlanVersionCreateRequest
from app.sandbox_worker.native_resources import (
    DockerNativeResourceRuntime, NativeResourceWorker, checked_url, download_archive, extract_archive, verify_cache,
)
from app.workbench import WorkbenchError
from tests.test_workbench import seeded


def resource_task(tmp_path):
    db, settings, task, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = wb.append(task, "context_ready", {**context.payload, "archive_hash": ARCHIVE}, key="resource-context")
        wb.save(task, PlanVersionCreateRequest.model_validate(content.hash_payload()), context_hash=context.record_hash, key="resource-plan")
        session.commit()
    return db, settings, task


def receipt_files():
    return {name: {"sha256": hashlib.sha256(name.encode()).hexdigest(), "size": len(name)} for name in ("ffmpeg", "ffprobe")}


class FakeRuntime:
    def __init__(self, failure=False):
        self.calls, self.failure = [], failure

    async def prepare(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise WorkbenchError("固定资源不存在；未替换版本。", code="native_resource_unavailable")
        return {"archive_sha256": kwargs["preview"]["resource"]["sha256"], "archive_bytes": 1024, "files": receipt_files()}


def test_preview_does_not_download_consent_is_exact_one_time_and_survives_upgrade(tmp_path):
    db, settings, task = resource_task(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        preview = prepare(wb, task, key="preview", architecture="arm64")
        assert not wb.jobs(task) and not wb.latest(task, "execution_authorized")
        assert prepare(wb, task, key="preview", architecture="arm64") == preview
        assert view(wb, task)["preview"]["current"]
        with pytest.raises(WorkbenchError, match="单独授权"):
            confirm(wb, task, preview_hash=preview["record_hash"], nonce="0"*64, approved=True)
        job = confirm(wb, task, preview_hash=preview["record_hash"], nonce=preview["nonce"], approved=True)
        assert confirm(wb, task, preview_hash=preview["record_hash"], nonce=preview["nonce"], approved=True).id == job.id
        assert len(wb.jobs(task)) == 1 and job.max_attempts == 1
        assert view(wb, task)["preview"]["consumed"]
        assert not wb.latest(task, "execution_authorized")
    db.create_schema()
    runtime = FakeRuntime()
    done = asyncio.run(NativeResourceWorker(db, settings, runtime, worker_id="native").run_once())
    assert done.state == "succeeded" and len(runtime.calls) == 1
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert view(wb, task)["ready"]
        from app.conversation import conversation_view
        assert conversation_view(wb, task)["pending_count"] == 0
        assert conversation_view(wb, task)["pending_action"]["kind"] == "execute"
        assert dependency_binding(wb, task)["files"] == receipt_files()
        assert not wb.latest(task, "execution_authorized")
        assert wb.read(wb.latest(task, "native_resource_ready").payload["artifact_id"])["archive_sha256"] == catalog("arm64")["sha256"]


@pytest.mark.parametrize("change", ["context", "plan", "expired", "paused", "busy", "pending", "superseded"])
def test_confirmation_rejects_stale_or_unavailable_inputs_without_download(tmp_path, change):
    db, settings, task = resource_task(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        p = prepare(wb, task, key="preview", architecture="arm64")
        if change == "context":
            c = wb.latest(task, "context_ready")
            wb.append(task, "context_ready", {**c.payload, "base_sha": "c"*40}, key="new-context")
        elif change == "plan":
            old = wb.latest_plan(task)
            from app.plans import PlanVersionService
            body = PlanVersionCreateRequest.model_validate({**PlanVersionService._validated_content(old).hash_payload(), "goal": "new goal", "parent_version_id":old.id})
            wb.save(task, body, context_hash=wb.latest(task, "context_ready").record_hash, key="new-plan")
        elif change == "expired":
            p = {**p, "expires_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}
            event = wb.append(task, "native_resource_prepared", {k:v for k,v in p.items() if k != "record_hash"}, key="expired")
            p["record_hash"] = event.record_hash
        elif change == "paused": wb.append(task, "conversation_paused", {}, key="pause")
        elif change == "busy": wb.enqueue(task, "planning_turn", {}, key="busy")
        elif change == "pending":
            from app.conversation import receive_message
            receive_message(wb, task, text="补充要求", key="pending", settings=settings)
        elif change == "superseded": prepare(wb, task, key="new-preview", architecture="arm64")
        before = len(wb.jobs(task))
        with pytest.raises(WorkbenchError):
            confirm(wb, task, preview_hash=p["record_hash"], nonce=p["nonce"], approved=True)
        assert len(wb.jobs(task)) == before
        assert not wb.latest(task, "native_resource_confirmed")


@pytest.mark.parametrize("change", ["context", "stop", "cancel"])
def test_worker_rechecks_authorization_before_any_download(tmp_path, change):
    db, settings, task = resource_task(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        p = prepare(wb, task, key="p", architecture="arm64")
        j = confirm(wb, task, preview_hash=p["record_hash"], nonce=p["nonce"], approved=True)
        if change == "context":
            c=wb.latest(task, "context_ready")
            wb.append(task, "context_ready", {**c.payload,"base_sha":"c"*40},key="changed")
        elif change == "stop": wb.append(task, "stop_requested", {}, key="stop")
        else:
            from app.jobs import JobService
            JobService(session).request_cancel(j.id)
    runtime = FakeRuntime()
    asyncio.run(NativeResourceWorker(db, settings, runtime, worker_id="native").run_once())
    assert runtime.calls == []


def test_download_failure_keeps_blocker_and_fresh_consent_is_needed(tmp_path):
    db, settings, task = resource_task(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        p = prepare(wb, task, key="p", architecture="arm64")
        first = confirm(wb, task, preview_hash=p["record_hash"], nonce=p["nonce"], approved=True)
    runtime = FakeRuntime(failure=True)
    failed = asyncio.run(NativeResourceWorker(db, settings, runtime, worker_id="native").run_once())
    assert failed.state == "failed" and "不存在" in failed.error_message
    assert failed.error_code == "native_resource_unavailable"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert not view(wb, task)["ready"]
        assert confirm(wb, task, preview_hash=p["record_hash"], nonce=p["nonce"], approved=True).id == first.id
        assert len(runtime.calls) == 1
        second = prepare(wb, task, key="p2", architecture="arm64")
        assert view(wb, task)["status"] == "awaiting_consent"
        assert view(wb, task)["error"] is None
        assert len([j for j in wb.jobs(task) if j.state == "failed"]) == 1
        assert confirm(wb, task, preview_hash=second["record_hash"], nonce=second["nonce"], approved=True).id != first.id


def test_api_requires_auth_origin_and_forbids_arbitrary_resource_inputs(tmp_path):
    db, settings, task = resource_task(tmp_path)
    settings = replace(settings, local_access_token="resource-local-test")
    root = f"/api/v1/tasks/{task}/workbench"
    headers = {"X-ContribOS-Token": settings.local_access_token, "Idempotency-Key": "preview"}
    with TestClient(create_app(settings)) as client:
        assert client.post(root+"/native-resources/prepare").status_code == 401
        assert client.post(root+"/native-resources/prepare",headers={**headers,"Origin":"https://foreign.invalid"}).status_code == 403
        assert client.post(root+"/native-resources/prepare", json={"url":"https://evil.invalid"}, headers=headers).status_code == 422
        p = client.post(root+"/native-resources/prepare", json={}, headers=headers).json()
        body = {"preview_hash":p["record_hash"],"nonce":p["nonce"],"approved":True}
        assert client.post(root+"/native-resources/confirm",json={**body,"url":"https://evil.invalid"},headers=headers).status_code == 422
        assert client.post(root+"/native-resources/confirm",json={**body,"approved":False},headers=headers).status_code == 422
        result = client.post(root+"/native-resources/confirm",json=body,headers=headers)
        assert result.status_code == 202, result.text
        assert client.post(root+"/native-resources/confirm",json=body,headers=headers).json()["id"] == result.json()["id"]
        assert client.get(root).json()["native_resources"]["status"] == "queued"


@pytest.mark.parametrize("url", ["http://github.com/x", "https://github.com/another", "https://127.0.0.1/x",
    "https://release-assets.githubusercontent.com.evil.invalid/x", "https://a:b@release-assets.githubusercontent.com/x",
    "https://release-assets.githubusercontent.com:444/x", "https://release-assets.githubusercontent.com/x#frag",
    "https://release-assets.githubusercontent.com/x\nHeader:bad", "https://release-assets.githubusercontent.com\\@evil.invalid/x"])
def test_rejects_url_escape_and_credential_forwarding(url):
    with pytest.raises(ValueError): checked_url(url, catalog("arm64")["url"])


@pytest.mark.parametrize("case", ["ok", "synthetic_ipv6", "release_redirect", "private_dns", "mixed_dns", "redirect", "size_header", "size_stream", "hash", "missing"])
def test_pinned_tls_downloader_checks_dns_redirects_hash_and_stream_limits(tmp_path, monkeypatch, case):
    import http.client
    import socket
    import ssl
    data = b"fixture archive"
    spec = {**catalog("arm64"), "sha256":hashlib.sha256(data).hexdigest(), "download_bytes":100}
    requests, sockets, resolved = [], [], []
    ips = ["8.8.8.8"]
    if case == "private_dns": ips=["127.0.0.1"]
    elif case == "mixed_dns": ips.append("169.254.169.254")
    def resolve(host, port, *, family=socket.AF_UNSPEC, type=0):
        assert host in {"github.com","release-assets.githubusercontent.com"} and port == 443 and type == socket.SOCK_STREAM
        resolved.append(host)
        ipv4 = [(socket.AF_INET,socket.SOCK_STREAM,6,"",(ip,443)) for ip in ips]
        if case == "synthetic_ipv6" and family == socket.AF_UNSPEC:
            return [(socket.AF_INET6,socket.SOCK_STREAM,6,"",("2001:2::c",443,0,0)),*ipv4]
        assert family == socket.AF_INET
        return ipv4
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", lambda target,**k:sockets.append(target) or object())
    class Context:
        def wrap_socket(self,sock,server_hostname):
            assert server_hostname in {"github.com","release-assets.githubusercontent.com"}
            return sock
    monkeypatch.setattr(ssl,"create_default_context",Context)
    class Response:
        status = 302 if case == "redirect" else 404 if case == "missing" else 200
        def __init__(self,host):
            self.body=io.BytesIO(data)
            if case == "release_redirect": self.status=302 if host == "github.com" else 200
        def getheader(self,name,default=None):
            if name == "Location":
                return "https://release-assets.githubusercontent.com/asset?signature=ephemeral" if case == "release_redirect" else "https://169.254.169.254/credentials"
            return "101" if case == "size_header" else None
        def read(self,size): return self.body.read(size)
    class Connection:
        def __init__(self,*a,**k): self.host=a[0];self._context=k["context"]
        def request(self,method,path,headers): requests.append((method,path,headers))
        def getresponse(self): return Response(self.host)
        def close(self): pass
    monkeypatch.setattr(http.client,"HTTPSConnection",Connection)
    if case == "size_stream": spec["download_bytes"]=2
    elif case == "hash": spec["sha256"]="0"*64
    if case in {"ok", "synthetic_ipv6", "release_redirect"}:
        assert download_archive(spec,tmp_path/"archive")["archive_bytes"]==len(data)
        assert sockets == [("8.8.8.8",443)] * (2 if case == "release_redirect" else 1)
        assert set(requests[0][2]) == {"User-Agent","Accept-Encoding"}
        if case == "release_redirect":
            assert resolved == ["github.com","release-assets.githubusercontent.com"]
            assert set(requests[1][2]) == {"User-Agent","Accept-Encoding"}
    else:
        with pytest.raises(ValueError): download_archive(spec,tmp_path/"archive")
        if case in {"private_dns","mixed_dns"}: assert not sockets and not requests


def archive_fixture(tmp_path, members=None):
    spec = catalog("arm64")
    archive = tmp_path / "archive.tar.xz"
    with tarfile.open(archive, "w:xz") as handle:
        for name, kind, content in members or [(p,"file",n.encode()) for n,p in spec["members"].items()]:
            m = tarfile.TarInfo(name)
            if kind == "link": m.type=tarfile.SYMTYPE; m.linkname="/etc/passwd"
            elif kind == "hardlink": m.type=tarfile.LNKTYPE; m.linkname="/etc/passwd"
            else: m.size=len(content)
            handle.addfile(m, io.BytesIO(content))
    spec["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    output = tmp_path / "output"; output.mkdir()
    return spec, archive, output


def test_offline_extraction_hashes_only_exact_binaries_and_cache_detects_tampering(tmp_path):
    spec, archive, output = archive_fixture(tmp_path)
    files = extract_archive(spec, archive, output)
    assert files == receipt_files()
    verify_cache(output, files)
    (output/"ffmpeg").chmod(0o644); (output/"ffmpeg").write_bytes(b"xxxxxx")
    with pytest.raises(WorkbenchError): verify_cache(output, files)


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "duplicate", "link", "hardlink", "size", "hash", "missing"])
def test_rejects_malicious_or_incomplete_archives(tmp_path,bad):
    paths = catalog("arm64")["members"]
    members = [(p,"file",n.encode()) for n,p in paths.items()]
    if bad in {"../escape", "/absolute"}: members.append((bad,"file",b"bad"))
    elif bad == "duplicate": members.append(members[0])
    elif bad in {"link","hardlink"}: members.append(("link",bad,b""))
    elif bad == "missing": members.pop()
    spec, archive, output = archive_fixture(tmp_path,members)
    if bad == "hash": spec["sha256"]="0"*64
    elif bad == "size": spec["expanded_bytes"]=1
    with pytest.raises(ValueError): extract_archive(spec,archive,output)
    assert not (tmp_path/"escape").exists()


def test_downloader_and_extractor_docker_contract_has_no_project_credentials_or_execution(tmp_path):
    with pytest.raises(ValueError):
        DockerNativeResourceRuntime(docker_environment={"PATH":"/usr/bin","GITHUB_TOKEN":"canary-do-not-forward"})
    runtime = DockerNativeResourceRuntime(docker_environment={"PATH":"/usr/bin"})
    spec = catalog("arm64")
    for offline in (False, True):
        argv=runtime.argv(image="sha256:"+"a"*64,staging=str(tmp_path),spec=spec,offline=offline,name="contribos-native-"+"b"*32)
        assert argv[argv.index("--network")+1] == ("none" if offline else "bridge")
        assert "--read-only" in argv and "65532:65532" in argv and "no-new-privileges=true" in argv
        assert sum(x=="--mount" for x in argv) == 1
        assert "GITHUB_TOKEN" not in runtime.docker.docker_environment
        assert not any("/workspace" in x or "docker.sock" in x for x in argv)
        assert "subprocess" not in argv[-2]
    with pytest.raises(ValueError): runtime.argv(image="sha256:"+"a"*64,staging=str(tmp_path),spec={**spec,"url":"https://evil.invalid"},offline=False,name="contribos-native-"+"b"*32)


def test_resource_receipt_changes_environment_and_cannot_clear_other_failures(tmp_path):
    from app.execution_preflight import task_preflight, environment_inputs_hash
    from app.execution_environment import task_environment
    db, settings, task = resource_task(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        context=wb.latest(task,"context_ready")
        original=environment_inputs_hash(context,task_environment(wb,task))
        wb.append(task,"execution_environment_diagnostic",{"environment_inputs_hash":original,"result_hash":"a"*64,
            "diagnostic":{"version":"dependency-failure-v1","exit_code":1,"reason_code":"native_tool_unavailable","phase":"baseline_verify","packages":["ffmpeg"]}},key="native-fail")
        assert task_preflight(wb,task)["status"]=="blocked"
        p=prepare(wb,task,key="p",architecture="arm64")
        confirm(wb,task,preview_hash=p["record_hash"],nonce=p["nonce"],approved=True)
    asyncio.run(NativeResourceWorker(db,settings,FakeRuntime(),worker_id="native").run_once())
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert task_preflight(wb,task)["status"]=="ready"
        assert environment_inputs_hash(wb.latest(task,"context_ready"),task_environment(wb,task)) != original
        wb.append(task,"execution_environment_diagnostic",{"environment_inputs_hash":original,"result_hash":"a"*64,
            "diagnostic":{"version":"dependency-failure-v1","exit_code":1,"reason_code":"dependency_conflict","phase":"runtime_resolve","packages":["demo"]}},key="other-fail")
        assert task_preflight(wb,task)["status"]=="blocked"


def test_optional_native_plan_and_result_leave_legacy_hashes_unchanged():
    from app.sandbox_worker.dependencies import DependencyPreparationResult
    from app.sandbox_worker.specs import SandboxPolicy
    from app.sandbox_worker.dependencies import DependencyProxyPolicy
    from tests.test_sandbox_dependencies import _plan
    policy = SandboxPolicy()
    legacy = _plan(policy,DependencyProxyPolicy.for_ecosystem("python"))
    assert "native_resource" not in legacy.to_wire()
    original = DependencyPreparationResult.create(plan=legacy,inventory_hash="a"*64,file_count=1,total_bytes=10)
    assert "native_resource" not in original.hash_payload()
    assert DependencyPreparationResult.from_payload(original.to_wire()) == original
    workspace = replace(legacy,version="workspace-dependency-plan-v2",source_kind="explore")
    native={"version":"native-resource-policy-v1","receipt_hash":"b"*64,"resource_hash":"c"*64,"files":receipt_files()}
    changed = replace(workspace,native_resource=native)
    assert changed.plan_hash != workspace.plan_hash
    result = DependencyPreparationResult.create(plan=changed,inventory_hash="a"*64,file_count=1,total_bytes=10)
    assert result.hash_payload()["native_resource"] == native
    assert DependencyPreparationResult.from_payload(result.to_wire()) == result


def test_native_workspace_binding_cannot_be_added_without_signed_result_provenance():
    from tests.test_sandbox_verification import _implement_result, _dependency_execution, _spec, _workspace, _FakeVerifyRuntime
    from app.sandbox_worker.specs import SandboxPolicy, JobSpecSigner
    from app.sandbox_worker.verification import VerifyService, VerificationError
    policy = SandboxPolicy()
    implement = _implement_result(policy)
    dependency = _dependency_execution(implement,policy)
    signer = JobSpecSigner(key_id="native-test",signing_key=b"n"*32)
    native={"version":"native-resource-policy-v1","receipt_hash":"b"*64,"resource_hash":"c"*64,"files":receipt_files()}
    forged = replace(dependency, workspace=replace(dependency.workspace,native_resource=native))
    with pytest.raises(VerificationError,match="dependency provenance"):
        asyncio.run(VerifyService(signer=signer,policy=policy,runtime=_FakeVerifyRuntime()).run(
            signer.sign(_spec(implement,policy,dependency_result_hash=dependency.result.result_hash)),
            implement,_workspace(implement,policy),forged))


def test_cache_hit_needs_verified_receipt_and_always_rehashes_files(tmp_path):
    runtime = DockerNativeResourceRuntime(docker_environment={})
    root=tmp_path/"artifacts";root.mkdir()
    spec=catalog("arm64")
    p={"resource":spec,"binding":{"image":"sha256:"+"a"*64}}
    calls=[]
    async def run(argv,**kwargs):
        calls.append(argv)
        mount=argv[argv.index("--mount")+1]
        directory=Path(mount.split("src=",1)[1].split(",",1)[0])
        if argv[argv.index("--network")+1]=="none":
            for name in ("ffmpeg","ffprobe"):
                (directory/"files"/name).write_bytes(name.encode())
                (directory/"files"/name).chmod(0o555)
            value=receipt_files()
        else: value={"archive_sha256":spec["sha256"],"archive_bytes":1024}
        return json.dumps({"result":value}).encode()
    runtime.docker._run=run
    result=asyncio.run(runtime.prepare(root=root,preview=p,job_id="a"*36))
    assert len(calls)==2
    same=asyncio.run(runtime.prepare(root=root,preview=p,job_id="b"*36,cache_receipt={"resource":spec,**result}))
    assert same==result and len(calls)==2
    asyncio.run(runtime.prepare(root=root,preview=p,job_id="c"*36))
    assert len(calls)==4  # An orphan directory is never sufficient cache evidence.
    target=root/"native-resources"/content_hash(spec)/"ffmpeg"
    target.chmod(0o644);target.write_bytes(b"xxxxxx")
    repaired=asyncio.run(runtime.prepare(root=root,preview=p,job_id="d"*36,cache_receipt={"resource":spec,**result}))
    assert repaired==result and len(calls)==6
    verify_cache(target.parent,receipt_files())


def test_finished_download_cannot_publish_ready_after_context_changes(tmp_path):
    db,settings,task=resource_task(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        p=prepare(wb,task,key="p",architecture="arm64")
        confirm(wb,task,preview_hash=p["record_hash"],nonce=p["nonce"],approved=True)
    class StaleRuntime(FakeRuntime):
        async def prepare(self,**kwargs):
            result=await super().prepare(**kwargs)
            with db.session() as session:
                wb=PlanningService(session,settings.artifact_root)
                c=wb.latest(task,"context_ready")
                wb.append(task,"context_ready",{**c.payload,"base_sha":"c"*40},key="changed-during-download")
            return result
    assert asyncio.run(NativeResourceWorker(db,settings,StaleRuntime(),worker_id="native").run_once()).state=="failed"
    with db.session() as session:
        assert not PlanningService(session,settings.artifact_root).latest(task,"native_resource_ready")


def test_cleanup_reconciles_only_durable_terminal_allocations_and_staging(tmp_path):
    db,settings,task=resource_task(tmp_path)
    name="contribos-native-"+"a"*32
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        p=prepare(wb,task,key="p",architecture="arm64")
        job=confirm(wb,task,preview_hash=p["record_hash"],nonce=p["nonce"],approved=True)
        allocation=wb.append(task,"native_resource_container",{"name":name},key="allocation",job_id=job.id)
    runtime=FakeRuntime(failure=True)
    commands=[]
    class Docker:
        docker_executable="docker"
        async def _command(self,argv,**kwargs): commands.append(argv);return b""
    runtime.docker=Docker()
    worker=NativeResourceWorker(db,settings,runtime,worker_id="native")
    cache=Path(settings.artifact_root)/"native-resources";cache.mkdir()
    staging=cache/("native-staging-"+job.id+"-fixture");staging.mkdir();(staging/"partial").write_text("partial")
    unrelated=cache/"unrelated";unrelated.mkdir()
    asyncio.run(worker.cleanup())
    assert staging.exists() and not commands  # Queued work is never cleaned.
    assert asyncio.run(worker.run_once()).state=="failed"
    asyncio.run(worker.cleanup())
    assert commands==[("docker","rm","-f",name)] and not staging.exists() and unrelated.exists()
    asyncio.run(worker.cleanup())
    assert len(commands)==1
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert wb.latest(task,"native_resource_disposed").payload["allocation_hash"]==allocation.record_hash


def test_missing_version_narration_needs_explicit_append_only_recovery(tmp_path):
    from app.conversation import conversation_view, recover_readonly_results
    db,settings,task=resource_task(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        source=wb.append(task,"conversation_result",{"text":"旧版资源准备提示"},key="legacy-native-result")
        before=[(e.id,e.record_hash,e.payload_hash) for e in wb.history(task)]
        with pytest.raises(WorkbenchError,match="不同版本"):
            conversation_view(wb,task)
        assert recover_readonly_results(wb,task)==1
        assert recover_readonly_results(wb,task)==0
        assert [(e.id,e.record_hash,e.payload_hash) for e in wb.history(task)[:len(before)]]==before
        assert conversation_view(wb,task)["results"][0]["classification"]=="legacy_unverified"
        assert conversation_view(wb,task)["pending_count"]==0
        assert not wb.latest(task,"native_resource_confirmed") and not wb.latest(task,"execution_authorized")
        marker=wb.latest(task,"conversation_result_recovered")
        assert marker.payload["source_hash"]==source.record_hash
    db.create_schema()
    with db.session() as session:
        assert conversation_view(PlanningService(session,settings.artifact_root),task)["pending_action"]["kind"]=="native_resource"


@pytest.mark.parametrize("kind,payload,actor", [
    ("conversation_received",{"text":"不能恢复队列"},"system"),
    ("conversation_result",{"text":"未知版本","version":"future-v99"},"system"),
    ("conversation_result",{"text":"不能恢复动作","source_kind":"awaiting_acceptance"},"system"),
    ("conversation_result",{"text":"不能恢复收件状态","message_id":"other"},"system"),
    ("conversation_result",{"text":"不可信来源"},"local-user"),
])
def test_narration_recovery_never_recovers_unknown_versions_or_actions(tmp_path,kind,payload,actor):
    from app.conversation import pending_messages,recover_readonly_results
    db,settings,task=resource_task(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        wb.append(task,kind,payload,key="invalid-event",actor=actor)
        assert recover_readonly_results(wb,task)==0
        with pytest.raises(WorkbenchError,match="不同版本"): pending_messages(wb,task)
