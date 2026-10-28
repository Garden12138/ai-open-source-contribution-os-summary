"""Record actual controlled Docker receipts; this is not a native journey.

Run from the repository root with the Docker acceptance environment in README.
This observer does not change tool inputs, return values, models or task code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.provenance import content_hash
from app.sandbox_worker.agent_tools import DockerAgentToolGateway


def main() -> int:
    receipts = []
    original = DockerAgentToolGateway.execute

    async def observe(self, spec, call, arguments, **kwargs):
        response = await original(self, spec, call, arguments, **kwargs)
        receipts.append({"binding": spec.binding.model_dump(),
            "call": call.model_dump(), "result": response["result"],
            "result_hash": content_hash(response["result"]),
            "snapshot_hash": content_hash(response["snapshot"]) if response["snapshot"] else None})
        return response

    DockerAgentToolGateway.execute = observe
    try:
        code = pytest.main(["tests/test_agent_v2_tools.py", "--disable-warnings",
            "--junitxml=/tmp/arv2-recorded-real-tools.xml"])
    finally:
        DockerAgentToolGateway.execute = original
        target = Path(os.environ.get("ARV2_RECEIPT_OUTPUT", "/tmp/arv2-real-tool-receipts.json"))
        target.write_text(json.dumps({"classification": "controlled_real_docker_tools",
            "counts_as_M5": False, "model_calls": 0, "operator_business_patches": 0,
            "receipts": receipts}, ensure_ascii=False, indent=2) + "\n")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
