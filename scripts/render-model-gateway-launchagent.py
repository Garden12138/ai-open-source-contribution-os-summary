"""Render an uninstalled macOS LaunchAgent. No launchctl or storage operations.

Output is written only to the explicitly supplied path. Key files and storage
must be separately provisioned/validated under a future deployment authorization.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import re


def configuration(workspace: Path, home: Path, store_id: str) -> dict:
    if not workspace.is_absolute() or not home.is_absolute() or not re.fullmatch(r"[a-f0-9]{32}", store_id):
        raise ValueError("Absolute workspace/home and an explicit store identity are required")
    private = home / "Library/Application Support/ContribOS"
    return {
        "Label": "local.contribos.model-gateway",
        "ProgramArguments": [str(workspace / ".venv/bin/python"), "-m", "app", "model-gateway", "--host", "127.0.0.1", "--port", "18091"],
        "WorkingDirectory": str(workspace),
        "EnvironmentVariables": {
            "MODEL_GATEWAY_SECRET_ROOT": str(private / "model-gateway"),
            "MODEL_GATEWAY_STORE_ID": store_id,
            "MODEL_GATEWAY_SIGNING_KEY_FILE": str(private / "gateway-runtime/signing.key"),
            "MODEL_GATEWAY_MANAGEMENT_KEY_FILE": str(private / "gateway-runtime/management.key"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "RunAtLoad": False, "KeepAlive": False, "Umask": 0o077,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--store-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = plistlib.dumps(configuration(args.workspace, args.home, args.store_id))
    with args.output.open("xb") as target:
        target.write(data)


if __name__ == "__main__":
    main()
