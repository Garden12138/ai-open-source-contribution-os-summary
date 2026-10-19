"""Trusted fixed-URL downloader and offline extractor; neither runs project code."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4
from urllib.parse import SplitResult
from typing import Any

from app.native_resources import DOWNLOAD, binding, catalog
from app.provenance import content_hash
from app.sandbox_worker.dependencies import DockerDependencyRuntime, DependencyPreparationError
from app.workbench import Workbench, WorkbenchError
from app.workbench_worker import WorkbenchJobWorker


def checked_url(url: str, initial: str) -> "SplitResult":
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if (len(url) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in url) or "\\" in url
            or parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.fragment or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com"}
            or (parsed.hostname == "github.com" and url != initial)):
        raise ValueError("url")
    return parsed


def download_archive(spec: dict, destination: str) -> dict:
    """Pin each TLS connection to a checked global address; disable proxy inheritance."""
    import hashlib
    import http.client
    import ipaddress
    import socket
    import ssl
    import time
    from urllib.parse import urljoin
    deadline = time.monotonic() + spec["timeout_seconds"]
    url = spec["url"]
    for _ in range(5):
        parsed = checked_url(url, spec["url"])
        # The downloader uses IPv4 transport only. OrbStack may synthesize
        # benchmarking IPv6 answers even when the IPv4 route is usable. Validate
        # every address in the selected family and connect only to its pinned IP.
        answers = socket.getaddrinfo(parsed.hostname, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        if not answers or any(not ipaddress.ip_address(item[4][0]).is_global for item in answers):
            raise ValueError("url")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("timeout")
        conn = http.client.HTTPSConnection(parsed.hostname, 443, timeout=min(20, remaining), context=ssl.create_default_context())
        try:
            # Connect directly to the vetted address, retaining TLS hostname checks.
            sock = socket.create_connection((answers[0][4][0], 443), timeout=min(20, remaining))
            conn.sock = conn._context.wrap_socket(sock, server_hostname=parsed.hostname)
            target = parsed.path + ("?" + parsed.query if parsed.query else "")
            conn.request("GET", target, headers={"User-Agent": "ContribOS-fixed-resource/1", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.getheader("Location", ""))
                checked_url(url, spec["url"])
                continue
            if response.status != 200:
                raise ValueError("unavailable")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > spec["download_bytes"]):
                raise ValueError("size")
            digest, total = hashlib.sha256(), 0
            with open(destination, "xb") as handle:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > spec["download_bytes"]:
                        raise ValueError("size")
                    if time.monotonic() > deadline:
                        raise ValueError("timeout")
                    digest.update(chunk)
                    handle.write(chunk)
            if digest.hexdigest() != spec["sha256"]:
                raise ValueError("hash")
            return {"archive_sha256": digest.hexdigest(), "archive_bytes": total}
        finally:
            conn.close()
    raise ValueError("url")


def extract_archive(spec: dict, archive: str, destination: str) -> dict:
    import hashlib
    import os
    from pathlib import PurePosixPath
    import tarfile
    # Rehash at the offline boundary, before any archive parsing.
    digest = hashlib.sha256()
    with open(archive, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if os.path.getsize(archive) > spec["download_bytes"] or digest.hexdigest() != spec["sha256"]:
        raise ValueError("hash")
    seen, selected, total = set(), {}, 0
    reverse = {value: name for name, value in spec["members"].items()}
    with tarfile.open(archive, "r|xz") as bundle:
        for member in bundle:
            path = PurePosixPath(member.name)
            if (str(path) in seen or len(seen) >= 20000 or len(member.name) > 1000
                    or path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not (member.isdir() or member.isfile()) or member.size < 0):
                raise ValueError("archive")
            seen.add(str(path))
            total += member.size
            if total > spec["expanded_bytes"]:
                raise ValueError("size")
            if member.name in reverse:
                if not member.isfile() or not member.size:
                    raise ValueError("archive")
                name, digest, size = reverse[member.name], hashlib.sha256(), 0
                source = bundle.extractfile(member)
                target = os.path.join(destination, name)
                with source, open(target, "xb") as handle:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > member.size:
                            raise ValueError("size")
                        digest.update(chunk)
                        handle.write(chunk)
                if size != member.size:
                    raise ValueError("archive")
                os.chmod(target, 0o555)
                selected[name] = {"sha256": digest.hexdigest(), "size": size}
    if set(selected) != {"ffmpeg", "ffprobe"}:
        raise ValueError("archive")
    return selected


def validate_files(files: object) -> None:
    import re
    if not isinstance(files, dict) or set(files) != {"ffmpeg", "ffprobe"}:
        raise ValueError("files")
    for entry in files.values():
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or type(entry["size"]) is not int or not 0 < entry["size"] <= 512 * 1024**2):
            raise ValueError("files")
    if sum(entry["size"] for entry in files.values()) > 512 * 1024**2:
        raise ValueError("files")


def validate_resource_binding(value: object) -> None:
    import re
    if (not isinstance(value, dict) or set(value) != {"version", "receipt_hash", "resource_hash", "files"}
            or value["version"] != "native-resource-policy-v1"
            or any(not isinstance(value[field], str) or not re.fullmatch(r"[0-9a-f]{64}", value[field])
                   for field in ("receipt_hash", "resource_hash"))):
        raise ValueError("Native resource binding is invalid")
    validate_files(value["files"])


def verify_cache(path: Path, files: dict) -> None:
    import stat
    validate_files(files)
    if path.is_symlink() or not path.is_dir() or {p.name for p in path.iterdir()} != {"ffmpeg", "ffprobe"}:
        raise WorkbenchError("固定资源缓存不可用，请重新准备", code="native_resource_cache_invalid")
    for name, entry in files.items():
        file = path / name
        if (file.is_symlink() or not file.is_file() or file.stat().st_size != entry["size"]
                or stat.S_IMODE(file.stat().st_mode) != 0o555):
            raise WorkbenchError("固定资源缓存校验失败", code="native_resource_cache_invalid")
        with file.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != entry["sha256"]:
                raise WorkbenchError("固定资源缓存校验失败", code="native_resource_cache_invalid")


ERRORS = {"hash": "下载资源 SHA-256 不符，已拒绝使用；请核对项目固定资源。",
          "unavailable": "固定资源下载失败或该版本不存在；未替换版本，可重新核对后授权。",
          "url": "资源地址、跳转或网络地址未通过来源限制。",
          "size": "资源下载或展开大小超出授权上限。", "archive": "资源归档安全校验失败。",
          "timeout": "资源准备超时；已有方案保留。"}


class DockerNativeResourceRuntime:
    def __init__(self, *, docker_environment: dict[str, str]) -> None:
        self.docker = DockerDependencyRuntime(docker_environment=docker_environment)

    def argv(self, *, image: str, staging: str, spec: dict, offline: bool, name: str) -> tuple[str, ...]:
        import re
        if (not re.fullmatch(r"sha256:[0-9a-f]{64}", image) or not os.path.isabs(staging) or "," in staging
                or not re.fullmatch(r"contribos-native-[0-9a-f]{32}", name)
                or spec != catalog(spec["platform"].split("/")[1])):
            raise ValueError("resource runtime")
        functions = inspect.getsource(extract_archive) if offline else inspect.getsource(checked_url) + inspect.getsource(download_archive)
        call = 'extract_archive(spec, "/resource/archive.tar.xz", "/resource/files")' if offline else 'download_archive(spec, "/resource/archive.tar.xz")'
        script = functions + '\nimport json, sys, platform\nspec=json.loads(sys.argv[1])\ntry:\n'
        script += '    architecture={"aarch64":"arm64","x86_64":"amd64"}.get(platform.machine(), platform.machine())\n'
        script += '    if "linux/"+architecture != spec["platform"]: raise ValueError("platform")\n'
        script += f'    result={call}\n    print(json.dumps({{"result":result}}))\nexcept Exception as exc:\n    code=str(exc) if type(exc) is ValueError and str(exc) in {tuple(ERRORS) + ("platform",)!r} else "unavailable"\n    print(json.dumps({{"error":code}}))\n'
        return (self.docker.docker_executable, "run", "--rm", "--pull", "never", "--name", name,
                "--network", "none" if offline else "bridge", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true", "--pids-limit", "64", "--memory", "536870912",
                "--memory-swap", "536870912", "--cpus", "1", "--user", "65532:65532",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
                "--mount", f"type=bind,src={staging},dst=/resource", "--entrypoint", "python3", image,
                "-I", "-c", script, json.dumps(spec, separators=(",", ":")))

    async def prepare(self, *, root: Path, preview: dict, job_id: str, cache_receipt=None, on_container=None) -> dict:
        spec = preview["resource"]
        cache_root = root / "native-resources"
        cache_root.mkdir(exist_ok=True)
        resource_hash = content_hash(spec)
        # A cache hit is still independently consented and rehashed by the supervisor.
        target = cache_root / resource_hash
        if target.exists() and cache_receipt is not None:
            if cache_receipt["resource"] != spec or cache_receipt["archive_sha256"] != spec["sha256"]:
                raise WorkbenchError("资源缓存依据不匹配", code="native_resource_cache_invalid")
            try:
                verify_cache(target, cache_receipt["files"])
            except WorkbenchError:
                pass  # This fresh consent permits a verified replacement download.
            else:
                return {k: cache_receipt[k] for k in ("archive_sha256", "archive_bytes", "files")}
        with tempfile.TemporaryDirectory(prefix="native-staging-" + job_id + "-", dir=cache_root) as temporary:
            staging = Path(temporary)
            staging.chmod(0o777)
            (staging / "files").mkdir(mode=0o777)
            (staging / "files").chmod(0o777)
            results = []
            for offline in (False, True):
                name = "contribos-native-" + uuid4().hex
                if on_container:
                    on_container(name)
                try:
                    raw = await self.docker._run(self.argv(image=preview["binding"]["image"], staging=temporary,
                        spec=spec, offline=offline, name=name), container_name=name, timeout_seconds=spec["timeout_seconds"], max_output_bytes=4096)
                    value = json.loads(raw)
                except DependencyPreparationError:
                    raise WorkbenchError("资源下载或隔离准备未完成；请检查资源与执行服务后重新授权。", code="native_resource_failed") from None
                if "error" in value:
                    code = value["error"]
                    raise WorkbenchError(ERRORS.get(code, "资源与 Runner 平台不符。"), code="native_resource_" + code)
                results.append(value["result"])
            files = results[1]
            verify_cache(staging / "files", files)
            result = {**results[0], "files": files}
            if target.exists():
                try:
                    verify_cache(target, files)
                except WorkbenchError:
                    old = staging / "corrupt-cache"
                    if target.is_dir() and not target.is_symlink():
                        target.chmod(0o755)
                    os.rename(target, old)
                    if old.is_dir() and not old.is_symlink():
                        old.chmod(0o755)
                    os.rename(staging / "files", target)
            else:
                os.rename(staging / "files", target)
            target.chmod(0o555)
            return result


class NativeResourceWorker(WorkbenchJobWorker):
    kinds = (DOWNLOAD,)

    def __init__(self, database: Any, settings: Any, runtime: Any, *, worker_id: str) -> None:
        super().__init__(database, worker_id=worker_id)
        self.settings, self.runtime = settings, runtime

    def current(self, session: Any, job: Any) -> tuple[Workbench, Any]:
        self.check(session, job)
        wb = Workbench(session, self.settings.artifact_root)
        preview = wb.latest(job.payload["task_id"], "native_resource_prepared")
        consent = wb.latest(job.payload["task_id"], "native_resource_confirmed")
        if (not preview or not consent or preview.record_hash != job.payload["preview_hash"]
                or consent.payload["preview_hash"] != preview.record_hash or consent.actor_id != "local-user"
                or consent.job_id != job.id or preview.payload["binding"] != binding(wb, job.payload["task_id"])):
            raise WorkbenchError("资源下载授权或冻结依据已变化", code="native_resource_stale")
        if preview.payload["resource"] != catalog(preview.payload["resource"]["platform"].split("/")[1]):
            raise WorkbenchError("资源策略已变化，请重新授权", code="native_resource_stale")
        stop = wb.latest(job.payload["task_id"], "stop_requested")
        if stop and stop.sequence > consent.sequence:
            raise WorkbenchError("资源准备已停止", code="native_resource_stale")
        return wb, preview

    async def cleanup(self) -> None:
        """Reconcile killed workers using durable allocations, never broad Docker targets."""
        import re
        from sqlalchemy import select
        from app.models import Job, WorkbenchEvent
        from app.jobs import TERMINAL_STATES
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            for event in session.scalars(select(WorkbenchEvent).where(WorkbenchEvent.kind == "native_resource_container")):
                job = session.get(Job, event.job_id)
                if not job or job.state not in TERMINAL_STATES:
                    continue
                name = event.payload["name"]
                if not re.fullmatch(r"contribos-native-[0-9a-f]{32}", name):
                    raise WorkbenchError("资源清理记录无效", code="native_resource_invalid")
                if wb.latest(event.task_id, "native_resource_disposed") and any(
                        e.kind == "native_resource_disposed" and e.payload["allocation_hash"] == event.record_hash for e in wb.history(event.task_id)):
                    continue
                try:
                    await self.runtime.docker._command((self.runtime.docker.docker_executable, "rm", "-f", name), timeout_seconds=30)
                except DependencyPreparationError:
                    # --rm already removed completed containers; inspect proves absence.
                    names = await self.runtime.docker._command((self.runtime.docker.docker_executable, "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"), timeout_seconds=30)
                    if names.strip():
                        continue
                wb.append(event.task_id, "native_resource_disposed", {"allocation_hash": event.record_hash}, key="native-disposed:" + event.id)
            cache = Path(self.settings.artifact_root) / "native-resources"
            if cache.is_dir() and not cache.is_symlink():
                for path in cache.iterdir():
                    match = re.fullmatch(r"native-staging-([0-9a-f-]{36})-[A-Za-z0-9_]+", path.name)
                    if not match or path.is_symlink() or not path.is_dir():
                        continue
                    job = session.get(Job, match[1])
                    if job and job.kind == DOWNLOAD and job.state in TERMINAL_STATES:
                        shutil.rmtree(path)

    async def execute(self, job: Any) -> dict:
        with self.database.session() as session:
            wb, preview = self.current(session, job)
            payload = dict(preview.payload)
            from datetime import datetime, timezone
            if datetime.now(timezone.utc) >= datetime.fromisoformat(payload["expires_at"]):
                raise WorkbenchError("排队期间资源授权已过期，请重新授权", code="native_resource_expired")
            old = next((e for e in reversed(wb.history(job.payload["task_id"])) if e.kind == "native_resource_ready"
                        and e.payload["resource"] == payload["resource"]), None)
            cache_receipt = wb.read(old.payload["artifact_id"]) if old else None
        def allocated(name):
            with self.database.session() as session:
                wb, _ = self.current(session, job)
                wb.append(job.payload["task_id"], "native_resource_container", {"name": name}, key="native-container:" + name,
                          job_id=job.id)
        result = await self.runtime.prepare(root=Path(self.settings.artifact_root), preview=payload, job_id=job.id,
                                            cache_receipt=cache_receipt, on_container=allocated)
        validate_files(result["files"])
        if (result["archive_sha256"] != payload["resource"]["sha256"]
                or type(result["archive_bytes"]) is not int or not 0 < result["archive_bytes"] <= payload["resource"]["download_bytes"]):
            raise WorkbenchError("资源准备证据校验失败", code="native_resource_invalid")
        with self.database.session() as session:
            wb, preview = self.current(session, job)
            artifact = wb.artifact({"version": "native-resource-receipt-v1", **payload, **result})
            event = wb.append(job.payload["task_id"], "native_resource_ready", {"binding": payload["binding"],
                "resource": payload["resource"], "files": result["files"], "artifact_id": artifact,
                "preview_hash": preview.record_hash}, key="native-ready:" + job.id, job_id=job.id, commit=False)
            from app.conversation import VERSION as CONVERSATION_VERSION
            wb.append(job.payload["task_id"], "conversation_result", {"version": CONVERSATION_VERSION,
                      "text": "固定测试资源已下载并通过 SHA-256 校验。后续测试继续断网运行；请重新核对方案，再单独确认执行。"},
                      key="native-result:" + job.id, job_id=job.id, commit=False)
            return self.finish(session, job, {"receipt_hash": event.record_hash})
