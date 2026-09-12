"""Provision a private local management key without printing secret material."""

from __future__ import annotations

import os
from pathlib import Path
import secrets

from dotenv import dotenv_values


def main() -> None:
    path = Path(__file__).resolve().parents[2] / ".env"
    if path.is_symlink() or not path.is_file():
        raise SystemExit("Expected an existing regular repository .env file")
    values = dotenv_values(path)
    existing = values.get("MODEL_GATEWAY_MANAGEMENT_KEY")
    if existing:
        try:
            valid = len(bytes.fromhex(existing)) >= 32
        except ValueError:
            valid = False
        if not valid or existing == values.get("MODEL_GATEWAY_SIGNING_KEY"):
            raise SystemExit("Existing management key is invalid or reused; no change made")
        print("Independent management key already configured; unchanged")
        return
    if values.get("MODEL_GATEWAY_MANAGEMENT_KEY_FILE"):
        raise SystemExit("File-based management key already configured; no change made")
    # Credential provisioning is intentionally runtime-only: never put a generated
    # key into source patches, command arguments, tool output or application logs.
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "a") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write("\n# Studio API/Gateway-only management key. Keep private.\n")
        output.write("MODEL_GATEWAY_MANAGEMENT_KEY=" + secrets.token_hex(32) + "\n")
        output.flush()
        os.fsync(output.fileno())
    print("Independent management key provisioned; .env permissions 0600")


if __name__ == "__main__":
    main()
