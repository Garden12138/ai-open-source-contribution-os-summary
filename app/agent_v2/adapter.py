"""Explicit structured-tool adapter over the existing credential-free gateway."""
from __future__ import annotations

import json
from typing import Any
from app.agent_v2.contracts import Decision
from app.providers.codex_cli import CodexExecInvocation
from app.providers.contracts import ProviderStage
from app.provenance import canonical_json, content_hash
from app.security import ensure_no_sensitive_data


class ModelProtocolError(ValueError):
    def __init__(self, content: str) -> None:
        super().__init__("agent_model_output_invalid")
        self.content = content


class StructuredModelAdapter:
    capability = "structured_tools"

    def __init__(self, runner: Any, identity: Any, attempt_id: str) -> None:
        self.runner, self.identity, self.attempt_id = runner, identity, attempt_id

    async def decide(self, prompt: dict, *, request_id: str, max_output_tokens: int) -> tuple[Decision, dict[str, int]]:
        text = canonical_json(prompt)
        invocation = CodexExecInvocation(
            stage=ProviderStage.CHANGE_SET, request_id=request_id,
            correlation_id=self.attempt_id, snapshot_id=self.attempt_id,
            input_hash=content_hash(prompt), prompt=text,
            output_schema=Decision.model_json_schema(), model=self.identity.model,
            output_token_limit=max_output_tokens,
        )
        completion = await self.runner.complete(invocation)
        ensure_no_sensitive_data(completion.content, context="v2 model output")
        # No prose extraction, guessed actions, hidden defaults or schema repair.
        try:
            decision = Decision.model_validate(json.loads(completion.content))
        except ValueError:
            raise ModelProtocolError(completion.content) from None
        return decision, {"input_tokens": completion.input_tokens,
                          "output_tokens": completion.output_tokens,
                          "duration_ms": completion.duration_ms}
