"""Authenticated zero-Provider readiness; never grants or consumes model access."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from uuid import uuid4

import httpx
from app.provenance import canonical_json, content_hash
from app.providers.contracts import ProviderRunError
from app.providers.model_secrets import management_auth, verify_management


STORAGE_ERROR_CODES = frozenset("gateway_storage_" + s for s in (
    "unavailable", "missing", "permissions", "path_invalid", "path_ephemeral",
    "identity_changed", "identity_invalid", "schema_invalid", "database_invalid",
    "key_mismatch", "reference_missing", "recovery_required", "journal_unsupported", "integrity", "limit", "busy",
))


def readonly_key(management_key: bytes) -> bytes:
    return hmac.new(management_key, b"gateway-credential-readiness-v1", hashlib.sha256).digest()


def configured_key(settings: Any) -> bytes | None:
    return (settings.model_gateway_readiness_key or
            (readonly_key(settings.model_gateway_management_key) if settings.model_gateway_management_key else None))


def storage_failure(code: str = "gateway_storage_unavailable") -> ProviderRunError:
    return ProviderRunError("model_" + (code if code in STORAGE_ERROR_CODES else "gateway_storage_unavailable"),
                            "Credential storage is not ready; explicit configuration may be required", retryable=False)


def validate_proof(settings: Any, profile: dict, data: dict) -> None:
    """Recheck signed evidence at authorization, before any business mutation."""
    try:
        authenticated = {k: v for k, v in data.items() if k != "proof_auth"}
        verify_management(configured_key(settings), canonical_json(authenticated).encode(), data.get("proof_auth", ""))
        proof = {k: v for k, v in authenticated.items() if k != "proof_hash"}
        if (data.get("ready") is not True or data.get("credential_ready") is not True
                or data.get("version") != "gateway-storage-v1" or data.get("store_id") != settings.model_gateway_store_id
                or data.get("profile_hash") != content_hash(profile)
                or data.get("reference_hash") != hashlib.sha256(profile["connection"]["credential_ref"].encode()).hexdigest()
                or type(data.get("checked_at")) is not int or not 0 <= int(time.time()) - data["checked_at"] <= 5
                or content_hash(proof) != data.get("proof_hash")):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise storage_failure() from None


def execution_profiles(settings: Any, models: Any, task_id: str) -> dict[str, dict]:
    from app.model_settings import ConnectionInput, ProfileInput
    selected = models.task_profiles(task_id, bind=False)
    result = {}
    for stage in ("implementation", "review"):
        if selected.get(stage):
            result[stage] = models.profile(selected[stage])
        else:
            if getattr(settings, stage + "_provider") != "nvidia_nim":
                raise storage_failure()
            # Preserve the supported environment-backed legacy model, without
            # creating model-config rows or granting it a new credential ref.
            profile = ProfileInput(name="Legacy " + stage, connection_id="legacy-env-nvidia",
                model=getattr(settings, stage + "_model")).model_dump()
            profile["connection"] = ConnectionInput(name="Legacy NVIDIA", provider="nvidia_nim",
                base_url="https://integrate.api.nvidia.com/v1", credential_ref="env-nvidia").model_dump()
            result[stage] = profile
    return result


async def credential_readiness(settings: Any, profile: dict, *, client: httpx.AsyncClient | None = None) -> dict:
    key = configured_key(settings)
    if not key or not settings.model_gateway_store_id:
        raise storage_failure()
    payload = {"operation": "credential_readiness", "profile": profile,
               "store_id": settings.model_gateway_store_id, "nonce": uuid4().hex}
    body = canonical_json(payload).encode()
    owned = client is None
    client = client or httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False)
    try:
        before = int(time.time())
        response = await client.post(settings.model_gateway_base_url.removesuffix("/v1") + "/internal/readiness",
            content=body, headers={"Content-Type": "application/json",
                                  "X-Gateway-Readiness": management_auth(key, body)})
        data = response.json()
        if response.status_code != 200:
            raise storage_failure(data.get("error", {}).get("code", ""))
        validate_proof(settings, profile, data)
        reference_hash = hashlib.sha256(profile["connection"]["credential_ref"].encode()).hexdigest()
        authenticated = {k: v for k, v in data.items() if k != "proof_auth"}
        verify_management(key, canonical_json(authenticated).encode(), data.get("proof_auth", ""))
        proof = {k: v for k, v in authenticated.items() if k != "proof_hash"}
        if (data.get("version") != "gateway-storage-v1" or data.get("ready") is not True
                or data.get("credential_ready") is not True or data.get("store_id") != settings.model_gateway_store_id
                or data.get("profile_hash") != content_hash(profile) or data.get("reference_hash") != reference_hash
                or data.get("nonce") != payload["nonce"]
                or type(data.get("checked_at")) is not int or not before - 1 <= data["checked_at"] <= int(time.time()) + 1
                or content_hash(proof) != data.get("proof_hash")):
            raise storage_failure()
        return data
    except (httpx.TransportError, ValueError, KeyError, TypeError, AttributeError):
        raise storage_failure() from None
    finally:
        if owned:
            await client.aclose()
