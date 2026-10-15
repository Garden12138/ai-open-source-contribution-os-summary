"""Disposable offline Studio browser fixture. Run as python -m tests.studio_browser_server."""

from pathlib import Path
import tempfile
from dataclasses import replace
import uvicorn
from app.api import create_app
from app.planning import ContributionTaskService
from app.planner import PlanningService
from app.providers.model_secrets import GatewaySecretStore
from app.model_settings import ModelSettingsService
from app.schemas import PlanVersionCreateRequest
from tests.test_workbench import seeded, run_turn, result
from tests.test_product_experience import _seed_candidates


def main():
    root = Path(tempfile.mkdtemp(prefix="contribos-studio-browser-"))
    db, settings, task_id, content, context = seeded(root)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(
            task_id,
            text="请协助完善这个开源贡献。",
            parent_id=None,
            expected_hash=None,
            key="browser-start",
        )
    run_turn(db, settings, result(content))
    from app.contribution_workflow import stop_workflow
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task_id)
        wb.request(task_id, text="修改方案", parent_id=plan.id,
                   expected_hash=plan.record_hash, key="browser-cancelled")
        stop_workflow(wb, task_id, key="browser-stop")
    _seed_candidates(db)
    from tests.test_analysis_evidence import _scan
    _scan(db, count=3)
    app = create_app(
        replace(settings, model_gateway_management_key=bytes.fromhex("ef" * 32),
                publisher_management_key=bytes.fromhex("ab" * 32))
    )
    store = GatewaySecretStore(str(root / "private"))

    async def manage(_settings, operation, payload):
        if operation == "grant":
            return store.grant(payload["connection_id"])
        if operation == "save":
            return store.save(payload["connection_id"], payload["envelope"])
        return {"configured": store.has(payload["credential_ref"])}

    app.state.gateway_manage = manage
    # Account probes use a deterministic read-only fake, never real GitHub.
    import httpx
    from fastapi import HTTPException
    from app.provenance import canonical_json
    from app.providers.model_secrets import management_auth
    from app.publisher_service import create_publisher_app, AccountProbeWorker
    publisher_vault = GatewaySecretStore(str(root / "publisher-private"))
    publisher = create_publisher_app(replace(app.state.settings, publisher_mode="gh"), db, publisher_vault, run_workers=False)
    async def publisher_manage(current, operation, payload):
        body = canonical_json({"operation":operation, **payload}).encode()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=publisher), base_url="http://publisher") as client:
            response = await client.post("/internal/settings", content=body,
                headers={"X-Publisher-Management":management_auth(current.publisher_management_key, body)})
        if response.status_code != 200:
            raise HTTPException(response.status_code, "配置未完成，请刷新后重试")
        return response.json()
    app.state.publisher_manage = publisher_manage
    async def publisher_probe(_token):
        return {"login":"browser-contributor", "user_id":123, "scopes":["public_repo"]}
    import asyncio
    from contextlib import asynccontextmanager, suppress
    from app.task_erasure import TaskErasureWorker
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def fixture_lifespan(application):
        async with original_lifespan(application):
            eraser = TaskErasureWorker(application.state.database, settings.artifact_root, worker_id="fixture-eraser")
            account_worker = AccountProbeWorker(db, publisher_vault, worker_id="fixture-account", probe=publisher_probe)
            async def run_erasure():
                while True:
                    await eraser.run_once()
                    await account_worker.run_once()
                    await asyncio.sleep(0.1)
            worker = asyncio.create_task(run_erasure())
            try:
                yield
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
    app.router.lifespan_context = fixture_lifespan
    print(f"Fixture task: {task_id}; data: {root}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
