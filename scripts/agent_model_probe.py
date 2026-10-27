"""Bounded acceptance adapter, executed only inside the Provider container.

The host test supervisor sends prompts over stdin and receives sanitized model
results. Gateway signing material and provider credentials stay in their owning
services. This script never executes repository code or writes business state.
"""
from __future__ import annotations

import asyncio
import json
import sys

from app.config import Settings
from app.database import Database
from app.model_settings import ModelSettingsService
from app.providers.codex_cli import CodexExecInvocation
from app.providers.contracts import ProviderStage
from app.providers.studio import runner_for_profile
from app.security import ensure_no_sensitive_data


async def main() -> dict:
    request = json.loads(sys.stdin.buffer.read(1_000_001))
    ensure_no_sensitive_data(request, context="acceptance request")
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            service = ModelSettingsService(session)
            defaults = service.defaults()
            metadata = {stage: {"profile_id": defaults[stage],
                "identity": service.identity(defaults[stage]).hash_payload()}
                for stage in ("planning", "implementation", "review") if defaults.get(stage)}
            if request.get("operation") == "metadata":
                return metadata
            stage = request["stage"]
            if stage not in metadata or metadata[stage]["identity"] != request["expected_identity"]:
                raise ValueError("profile_changed")
            profile = service.profile(defaults[stage])
        value = request["invocation"]
        if len(value["prompt"].encode()) > 500_000:
            raise ValueError("prompt_limit")
        invocation = CodexExecInvocation(stage=ProviderStage(value["stage"]),
            request_id=value["request_id"], correlation_id=value["correlation_id"],
            snapshot_id=value["snapshot_id"], input_hash=value["input_hash"],
            prompt=value["prompt"], output_schema=value["output_schema"], model=value["model"],
            output_token_limit=min(16_384, value.get("output_token_limit") or 16_384))
        result = await runner_for_profile(settings, profile).complete(invocation)
        response = {"identity": metadata[stage]["identity"], "completion": {
            key: getattr(result, key) for key in ("content", "input_tokens", "cached_input_tokens", "output_tokens", "duration_ms")}}
        ensure_no_sensitive_data(response, context="acceptance response")
        return response
    finally:
        database.close()


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
    except Exception as exc:
        # No provider/subprocess/database exception text or prompt is printed.
        print(json.dumps({"error": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))
