"""Publisher-only credential management and persistent read-only account probes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Protocol
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.models import Job
from app.provenance import content_hash
from app.providers.model_secrets import GatewaySecretStore, verify_management
from app.publisher_accounts import (PROBE, account_record, account_status,
    append_account, assert_account_changeable, require_account)
from app.publication import GhPublicationTransport, PublicationWorker
from app.cla import CLAWorker, PREPARE, WRITE, CHECK
from app.pr_updates import PRUpdateWorker
from app.pr_lifecycle import PRLifecycleWorker
from app.workbench import WorkbenchError
from app.workbench_worker import WorkbenchJobWorker


class AccountProbe(Protocol):
    async def __call__(self, token: str) -> dict[str, Any]: ...


async def probe_github_account(token: str) -> dict[str, Any]:
    """Only GET /user; no redirects, repository operations or raw error output."""
    async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
        async with client.stream("GET", "https://api.github.com/user", headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }) as response:
            if response.status_code != 200:
                raise WorkbenchError("GitHub 账号检测失败，请检查 Token 是否有效、过期或受组织策略限制")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise WorkbenchError("GitHub 账号响应不符合预期")
            value = json.loads(raw)
            scopes = {s.strip() for s in response.headers.get("X-OAuth-Scopes", "").split(",")}
    if not ({"repo", "public_repo"} & scopes):
        raise WorkbenchError("Token 缺少 public_repo 权限；当前连接方式支持用于公开仓库贡献的 Classic Token")
    login, identifier = value.get("login"), value.get("id")
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) or type(identifier) is not int:
        raise WorkbenchError("GitHub 账号响应不符合预期")
    return {"login": login, "user_id": identifier, "scopes": sorted(scopes & {"repo", "public_repo", "workflow"})}


class AccountProbeWorker(WorkbenchJobWorker):
    kinds = (PROBE,)

    def __init__(self, database: Database, vault: GatewaySecretStore, *,
                 worker_id: str, probe: AccountProbe = probe_github_account):
        super().__init__(database, worker_id=worker_id)
        self.vault, self.probe = vault, probe

    async def execute(self, job: Job) -> dict:
        with self.database.session() as session:
            self.check(session, job)
            current = account_record(session)
            if not current or current.payload.get("job_id") != job.id or current.payload["state"] != "pending":
                raise WorkbenchError("发布账号已更新，本次检测已失效")
            expected = current.event_hash
        token = self.vault.read(job.payload["credential_ref"])
        try:
            result = await self.probe(token)
        except WorkbenchError as exc:
            safe = {"GitHub 账号检测失败，请检查 Token 是否有效、过期或受组织策略限制",
                    "GitHub 账号响应不符合预期",
                    "Token 缺少 public_repo 权限；当前连接方式支持用于公开仓库贡献的 Classic Token"}
            raise WorkbenchError(str(exc) if str(exc) in safe else "GitHub 账号连接失败，请检查网络后重新检测") from None
        except Exception:
            raise WorkbenchError("GitHub 账号连接失败，请检查网络后重新检测") from None
        with self.database.session() as session:
            self.check(session, job)
            current = account_record(session)
            if not current or current.event_hash != expected:
                raise WorkbenchError("发布账号已更新，本次检测已失效")
            checked = datetime.now(timezone.utc).isoformat()
            # A typed, fixed result prevents arbitrary upstream fields/secrets
            # from entering the business journal, even with an injected probe.
            if (not re.fullmatch(r"[A-Za-z0-9-]{1,39}", result.get("login", ""))
                or type(result.get("user_id")) is not int
                or not set(result.get("scopes", [])) <= {"repo", "public_repo", "workflow"}
                or not set(result.get("scopes", [])) & {"repo", "public_repo"}):
                raise WorkbenchError("GitHub 账号与权限校验未通过")
            public = {"login": result["login"], "user_id": result["user_id"],
                      "scopes": result["scopes"], "checked_at": checked}
            append_account(session, {"state": "connected", **public,
                "credential_ref": job.payload["credential_ref"], "job_id": job.id},
                correlation_id=job.id, actor=self.worker_id)
            return self.finish(session, job, public)


class ManagedPublicationWorker(PublicationWorker):
    def __init__(self, database: Database, settings: Settings, vault: GatewaySecretStore, *, worker_id: str):
        super().__init__(database, settings, worker_id=worker_id)
        self.vault = vault

    async def execute(self, job: Job) -> dict:
        expected = job.payload.get("publisher_account_hash")
        if not expected:
            raise WorkbenchError("发布任务缺少账号绑定，请重新准备预览")
        with self.database.session() as session:
            account = require_account(session, expected)
            reference = account.payload["credential_ref"]
        self.transport = GhPublicationTransport()
        self.transport.env["GH_TOKEN"] = self.vault.read(reference)
        try:
            return await super().execute(job)
        finally:
            self.transport.env.pop("GH_TOKEN", None)


class ManagedCLAWorker(CLAWorker):
    def __init__(self, database: Database, settings: Settings, vault: GatewaySecretStore, *, worker_id: str):
        super().__init__(database, settings, worker_id=worker_id, transport=GhPublicationTransport())
        self.vault = vault

    async def execute(self, job: Job) -> dict:
        with self.database.session() as session:
            account = require_account(session, job.payload['publisher_account_hash'])
            reference = account.payload['credential_ref']
        self.transport = GhPublicationTransport()
        self.transport.env['GH_TOKEN'] = self.vault.read(reference)
        try:
            return await super().execute(job)
        finally:
            self.transport.env.pop('GH_TOKEN', None)


class ManagedPRUpdateWorker(PRUpdateWorker):
    def __init__(self, database: Database, settings: Settings, vault: GatewaySecretStore, *, worker_id: str):
        super().__init__(database, settings, worker_id=worker_id, transport=GhPublicationTransport())
        self.vault = vault

    async def execute(self, job: Job) -> dict:
        with self.database.session() as session:
            account = require_account(session, job.payload['publisher_account_hash'])
            reference = account.payload['credential_ref']
        self.transport = GhPublicationTransport()
        self.transport.env['GH_TOKEN'] = self.vault.read(reference)
        try:
            return await super().execute(job)
        finally:
            self.transport.env.pop('GH_TOKEN', None)


class ManagedPRLifecycleWorker(PRLifecycleWorker):
    def __init__(self, database: Database, settings: Settings, vault: GatewaySecretStore, *, worker_id: str):
        super().__init__(database, settings, worker_id=worker_id, transport=GhPublicationTransport())
        self.vault = vault

    async def execute(self, job: Job) -> dict:
        with self.database.session() as session:
            account = require_account(session, job.payload['publisher_account_hash'])
            reference = account.payload['credential_ref']
        self.transport = GhPublicationTransport()
        self.transport.env['GH_TOKEN'] = self.vault.read(reference)
        try:
            return await super().execute(job)
        finally:
            self.transport.env.pop('GH_TOKEN', None)


def create_publisher_app(settings: Settings, database: Database,
                         vault: GatewaySecretStore, *, run_workers: bool = True,
                         probe: AccountProbe = probe_github_account) -> FastAPI:
    account_worker = AccountProbeWorker(database, vault, worker_id="publisher-account-" + uuid4().hex, probe=probe)
    publisher = ManagedPublicationWorker(database, settings, vault, worker_id="publisher-managed-" + uuid4().hex)
    cla = ManagedCLAWorker(database, settings, vault, worker_id="publisher-cla-" + uuid4().hex)

    pr_updates = ManagedPRUpdateWorker(database, settings, vault, worker_id="publisher-text-" + uuid4().hex)

    lifecycle = ManagedPRLifecycleWorker(database, settings, vault, worker_id="publisher-lifecycle-" + uuid4().hex)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def loop():
            while True:
                # No account or key means no credential-bearing publication.
                try:
                    if settings.publisher_management_key:
                        with database.session() as session:
                            JobService(session).recover_expired(kinds=(PROBE, "publication_requirements", "publication_prepare", "publication_write", PREPARE, WRITE, CHECK, *PRUpdateWorker.kinds, *PRLifecycleWorker.kinds))
                        await account_worker.run_once()
                        await publisher.run_once()
                        await cla.run_once()
                        await pr_updates.run_once()
                        await lifecycle.run_once()
                except Exception as exc:
                    logging.getLogger(__name__).warning("Publisher worker iteration failed: %s", type(exc).__name__)
                await asyncio.sleep(1)
        task = asyncio.create_task(loop()) if run_workers else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="ContribOS Publisher", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/internal/settings")
    async def manage(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 24000:
                return JSONResponse(status_code=413, content={"detail": "配置请求过大"})
        try:
            if not settings.publisher_management_key:
                raise ValueError()
            verify_management(settings.publisher_management_key, bytes(body),
                              request.headers.get("X-Publisher-Management", ""))
        except ValueError:
            return JSONResponse(status_code=403, content={"detail": "Publisher 管理授权无效"})
        try:
            payload = json.loads(body)
            operation = payload["operation"]
            if operation == "grant":
                if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", payload["connection_id"]):
                    raise ValueError()
                return vault.grant(payload["connection_id"])
            with database.session() as session:
                if operation == "status":
                    return {"available": True, **account_status(session)}
                # Serialize settings changes against publication enqueue/confirm.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                key = payload["key"]
                if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", key):
                    raise ValueError()
                digest = content_hash(payload)
                old = session.scalar(select(Job).where(Job.kind == PROBE, Job.idempotency_key == "publisher-account:" + key))
                if old:
                    if old.payload["request_hash"] != digest:
                        raise WorkbenchError("重复请求内容不同")
                    return {"job_id": old.id}
                from app.models import AuditEvent
                replay = session.scalar(select(AuditEvent).where(
                    AuditEvent.event_type == "publisher.account.disconnected",
                    AuditEvent.correlation_id == "publisher-account:" + key))
                if replay:
                    if replay.payload["request_hash"] != digest:
                        raise WorkbenchError("重复请求内容不同")
                    return {"disconnected": True}
                current = account_record(session)
                if (current.event_hash if current else None) != payload.get("expected_hash"):
                    raise WorkbenchError("发布账号设置已变化，请刷新后重试")
                assert_account_changeable(session)
                if operation == "disconnect":
                    append_account(session, {"state": "disconnected"}, correlation_id="publisher-account:" + key)
                    from app.audit import AuditService
                    AuditService(session).prepare(event_type="publisher.account.disconnected", actor_type="local_user",
                        actor_id="local-user", correlation_id="publisher-account:" + key, payload={"request_hash": digest})
                    session.commit()
                    with vault.connect() as private:
                        private.execute("DELETE FROM credentials")
                    return {"disconnected": True}
                if operation == "save":
                    reference = vault.save(payload["connection_id"], payload["envelope"])["credential_ref"]
                    if not re.fullmatch(r"ghp_[A-Za-z0-9]{20,255}", vault.read(reference)):
                        raise WorkbenchError("请使用带 public_repo 权限的 GitHub Classic Token")
                elif operation == "test":
                    if not current or not current.payload.get("credential_ref"):
                        raise WorkbenchError("请先连接 GitHub 发布账号")
                    reference = current.payload["credential_ref"]
                else:
                    raise ValueError()
                job, _ = JobService(session).enqueue(kind=PROBE, idempotency_key="publisher-account:" + key,
                    payload={"credential_ref": reference, "request_hash": digest}, timeout_seconds=60,
                    max_attempts=3, commit=False)
                append_account(session, {"state": "pending", "credential_ref": reference, "job_id": job.id},
                               correlation_id=job.id)
                session.commit()
                return {"job_id": job.id}
        except WorkbenchError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        except Exception:
            return JSONResponse(status_code=422, content={"detail": "Publisher 配置请求无效，请刷新后重试"})
    return app
