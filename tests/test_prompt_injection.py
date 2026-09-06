from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.providers import (
    ANALYSIS_POLICY_VERSION,
    INSPECTION_OUTPUT_SCHEMA,
    INSPECTION_SCHEMA_VERSION,
    INSPECT_PROMPT_VERSION,
    AnalysisBudget,
    AnalysisBudgetLedger,
    BudgetExceededError,
    CodexCLIAdapter,
    CodexExecInvocation,
    CodexExecResult,
    CollectingEventSink,
    FrozenEvidence,
    InspectRequest,
    ProviderIdentity,
    ProviderRunError,
)
from app.sandbox_worker.container import (
    ContainerIsolationPolicy,
    ReadOnlySnapshot,
)


FIXTURES_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "provider_prompt_injections.json"
)
IMAGE = "fixture/codex@sha256:" + "3" * 64
IDENTITY = ProviderIdentity(
    provider="codex_cli",
    adapter_version="codex-cli-0.146.0-alpha.3.1",
    model="fixture-model",
    model_version="fixture-model-v1",
)


def _fixtures() -> list[dict[str, str]]:
    value = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    assert all(
        isinstance(item, dict)
        and set(item) == {"id", "category", "content"}
        and all(isinstance(field, str) for field in item.values())
        for item in value
    )
    return value


FIXTURES = _fixtures()


class _RecordingRunner:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.invocations: list[CodexExecInvocation] = []
        self.payload = payload or {
            "observations": [],
            "cited_evidence_ids": [],
        }

    async def run(self, invocation: CodexExecInvocation) -> CodexExecResult:
        self.invocations.append(invocation)
        events = (
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(self.payload),
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                },
            },
        )
        return CodexExecResult(
            stdout="\n".join(json.dumps(event) for event in events),
            return_code=0,
            duration_ms=1,
        )


def _request(fixture: dict[str, str]) -> InspectRequest:
    return InspectRequest.create(
        request_id=f"prompt-injection-{fixture['id']}",
        correlation_id=f"prompt-injection-{fixture['id']}",
        snapshot_id=f"snapshot-{fixture['id']}",
        score_version_id=f"score-{fixture['id']}",
        evidence=(
            FrozenEvidence.capture(
                evidence_id=fixture["id"],
                kind="github_issue",
                source_uri=(
                    "github://fixture/repository/issues/"
                    + fixture["id"]
                ),
                content=fixture["content"],
            ),
        ),
        prompt_version=INSPECT_PROMPT_VERSION,
        policy_version=ANALYSIS_POLICY_VERSION,
        output_schema_version=INSPECTION_SCHEMA_VERSION,
    )


def _snapshot(
    tmp_path: Path,
    fixture: dict[str, str],
) -> ReadOnlySnapshot:
    root = tmp_path / "root"
    source = root / "snapshot"
    source.mkdir(parents=True)
    (source / "issue.txt").write_text(
        fixture["content"],
        encoding="utf-8",
    )
    return ReadOnlySnapshot.capture(
        snapshot_id=f"snapshot-{fixture['id']}",
        source=source,
        allowed_root=root,
    )


@pytest.mark.parametrize(
    "fixture",
    FIXTURES,
    ids=[item["category"] for item in FIXTURES],
)
def test_prompt_injection_is_data_and_cannot_reconfigure_provider_boundary(
    fixture: dict[str, str],
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    provider = CodexCLIAdapter(runner, identity=IDENTITY)
    request = _request(fixture)

    asyncio.run(provider.inspect(request, CollectingEventSink()))

    invocation = runner.invocations[0]
    lines = invocation.prompt.splitlines()
    untrusted_prefix = "CONTRIBOS_UNTRUSTED_FROZEN_INPUT_JSON="
    untrusted = json.loads(lines[2][len(untrusted_prefix) :])
    assert untrusted["evidence"][0]["content"] == fixture["content"]
    assert invocation.input_hash == request.input_hash
    assert invocation.model == "fixture-model"
    assert invocation.output_schema is not INSPECTION_OUTPUT_SCHEMA
    assert invocation.output_schema["properties"]["cited_evidence_ids"][
        "items"
    ]["enum"] == [fixture["id"]]
    assert invocation.output_schema["additionalProperties"] is False

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(INSPECTION_OUTPUT_SCHEMA),
        encoding="utf-8",
    )
    argv = ContainerIsolationPolicy().build_argv(
        docker_executable="docker",
        image=IMAGE,
        snapshot=_snapshot(tmp_path, fixture),
        schema_path=schema_path,
        container_name=(
            "contribos-codex-" + fixture["id"].replace("_", "-")
        ),
        entrypoint="codex",
        command=("exec", "-"),
    )
    joined = " ".join(argv)
    assert fixture["content"] not in joined
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--pull") + 1] == "never"
    assert "CODEX_API_KEY" not in joined
    assert "/var/run/docker.sock" not in joined
    assert "--privileged" not in argv


@pytest.mark.parametrize(
    "payload",
    (
        {
            "observations": [],
            "cited_evidence_ids": [],
            "shell_command": "perform external write",
        },
        {
            "observations": [
                {
                    "code": "secret",
                    "summary": "github_pat_outputcanary12345678",
                    "evidence_ids": ["secret_disclosure"],
                }
            ],
            "cited_evidence_ids": ["secret_disclosure"],
        },
        {
            "observations": [
                {
                    "code": "unknown",
                    "summary": "Cite invented evidence.",
                    "evidence_ids": ["not-frozen"],
                }
            ],
            "cited_evidence_ids": ["not-frozen"],
        },
    ),
)
def test_prompt_injection_cannot_bypass_schema_secret_or_citation_validation(
    payload: dict[str, object],
) -> None:
    fixture = next(
        item for item in FIXTURES if item["category"] == "schema_bypass"
    )
    provider = CodexCLIAdapter(
        _RecordingRunner(payload),
        identity=IDENTITY,
    )

    with pytest.raises(ProviderRunError, match="malformed or unsafe"):
        asyncio.run(
            provider.inspect(
                _request(fixture),
                CollectingEventSink(),
            )
        )


def test_prompt_injection_cannot_expand_external_budget_ledger() -> None:
    fixture = next(
        item for item in FIXTURES if item["category"] == "budget_bypass"
    )
    request = _request(fixture)
    ledger = AnalysisBudgetLedger(
        AnalysisBudget(
            max_candidates=1,
            max_model_invocations=2,
            max_input_tokens=100,
            max_output_tokens=100,
            max_estimated_cost_microusd=100,
            max_duration_ms=100,
            max_retries=0,
        )
    )
    ledger.consider_candidates((request.snapshot_id,))
    ledger.start_invocation(
        invocation_id="inspect",
        candidate_id=request.snapshot_id,
    )
    ledger.start_invocation(
        invocation_id="analyze",
        candidate_id=request.snapshot_id,
    )

    with pytest.raises(BudgetExceededError, match="invocation_limit"):
        ledger.start_invocation(
            invocation_id="injected-extra-call",
            candidate_id=request.snapshot_id,
        )
    assert ledger.snapshot.model_invocations == 2


_RUN_DOCKER = os.environ.get("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") == "1"


@pytest.mark.skipif(
    not _RUN_DOCKER,
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
def test_real_prompt_injection_fixture_cannot_cross_container_boundary(
    tmp_path: Path,
) -> None:
    image = os.environ.get("CONTRIBOS_CODEX_IMAGE", "")
    if not image:
        pytest.fail("CONTRIBOS_CODEX_IMAGE must contain a pinned image ID")
    root = tmp_path / "root"
    source = root / "snapshot"
    source.mkdir(parents=True)
    (source / "prompt-injections.json").write_text(
        json.dumps(FIXTURES),
        encoding="utf-8",
    )
    snapshot = ReadOnlySnapshot.capture(
        snapshot_id="prompt-injection-corpus",
        source=source,
        allowed_root=root,
    )
    policy = ContainerIsolationPolicy()
    name = f"contribos-codex-injection-{uuid4().hex[:12]}"
    argv = policy.build_argv(
        docker_executable="docker",
        image=image,
        snapshot=snapshot,
        schema_path=None,
        container_name=name,
        entrypoint="node",
        command=(
            "-e",
            (
                "const fs=require('node:fs');"
                "const path=require('node:path');"
                "const hostHome=process.argv[1];"
                "const fixtures=JSON.parse(fs.readFileSync("
                "'/workspace/prompt-injections.json','utf8'));"
                "if(fixtures.length!==6)process.exit(10);"
                "const denied=['GITHUB_TOKEN','OPENAI_API_KEY',"
                "'SSH_AUTH_SOCK','CODEX_API_KEY'];"
                "if(denied.some(key=>process.env[key]))process.exit(11);"
                "if(process.env.HOME===hostHome)process.exit(12);"
                "if(fs.existsSync(path.join(hostHome,'.ssh')))process.exit(13);"
                "if(fs.existsSync('/var/run/docker.sock'))process.exit(14);"
                "try{fs.writeFileSync('/workspace/escape','x');"
                "process.exit(15)}catch(error){}"
                "try{fs.writeFileSync('/rootfs-escape','x');"
                "process.exit(16)}catch(error){}"
                "require('node:dns').lookup('example.com',error=>"
                "process.exit(error?0:17))"
            ),
            os.environ.get("HOME", ""),
        ),
    )
    docker_environment = {
        name: value
        for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "HOME", "PATH")
        if (value := os.environ.get(name))
    }
    parent_environment = {
        **docker_environment,
        "GITHUB_TOKEN": "github_pat_promptcanary12345678",
        "OPENAI_API_KEY": "prompt-provider-canary",
        "SSH_AUTH_SOCK": "/tmp/contribos-prompt-ssh-canary",
    }
    try:
        result = subprocess.run(
            argv,
            env=parent_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "promptcanary" not in result.stdout + result.stderr
        assert "provider-canary" not in result.stdout + result.stderr
    finally:
        subprocess.run(
            ("docker", "rm", "--force", name),
            env=docker_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
