"""Persistent, cancellable connection probes without repository content."""

from __future__ import annotations
import json
import time
import httpx
from app.workbench_worker import WorkbenchJobWorker
from app.model_settings import job_profile
from app.provenance import content_hash
from app.providers.codex_cli import CodexExecInvocation
from app.providers.contracts import ProviderStage, ProviderRunError
from app.providers.studio import runner_for_profile


class ModelTestWorker(WorkbenchJobWorker):
    kinds = ("model_connection_test",)

    def __init__(self, database, settings, *, worker_id: str):
        super().__init__(database, worker_id=worker_id)
        self.settings = settings

    async def execute(self, job) -> dict:
        with self.database.session() as session:
            profile = job_profile(session, job.id)
        if not profile:
            raise ValueError("Missing frozen profile")
        runner = runner_for_profile(self.settings, profile)
        invocation = CodexExecInvocation(
            stage=ProviderStage.PLANNING,
            request_id=job.id,
            correlation_id=job.id,
            snapshot_id="connection-test",
            input_hash=content_hash(job.payload),
            model=profile["model"],
            prompt='Return {"ok": true}.',
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean", "const": True}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
        if job.payload.get("operation") == "models":
            if runner.broker is None:
                raise ValueError("Gateway is unavailable")
            credential = runner.broker.issue(invocation)
            async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
                response = await client.post(
                    credential.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + credential.token},
                    json={
                        "model": profile["model"],
                        "_contribos_profile": profile,
                        "_contribos_operation": "models",
                    },
                )
            if response.status_code != 200 or len(response.content) > 4_000_000:
                raise ProviderRunError(
                    "model_list_unavailable",
                    "无法读取模型列表，可以手动填写模型 ID",
                    retryable=False,
                )
            values = response.json().get("data", [])
            models = [
                v["id"]
                for v in values
                if isinstance(v, dict)
                and isinstance(v.get("id"), str)
                and len(v["id"]) <= 120
            ][:500]
            return {"models": models}
        started = time.monotonic()
        result = await runner.complete(invocation)
        if json.loads(result.content) != {"ok": True}:
            raise ProviderRunError(
                "model_structure_invalid", "模型未返回要求的结构化结果", retryable=False
            )
        return {
            "connected": True,
            "structured_output": True,
            "model": profile["model"],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "usage": None,
        }
