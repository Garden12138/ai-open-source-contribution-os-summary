"""Opt-in M2 test. Run from the host with a signed-in sbx and a user-owned Issue."""

import asyncio
import http.server
import os
import threading
import urllib.request
import uuid

import pytest
from acp.schema import TextContentBlock

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.engine import TaskContext
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.sandbox.sbx import SbxSandbox
from contribos.tasks.issue import parse_issue_url

OAUTH_PROBE = '''
import base64, json, os, pathlib, re, tomllib
home = pathlib.Path.home()
config_dir = pathlib.Path(os.environ["CODEX_HOME"])
config = tomllib.loads((config_dir / "config.toml").read_text())
auth = json.loads((config_dir / "auth.json").read_text())
assert auth == {"OPENAI_API_KEY": "proxy-managed"}
assert config["model_providers"]["sandboxd"]["experimental_bearer_token"] == "oai-oat01-proxy-managed"
assert os.environ.get("OPENAI_API_KEY", "") in {"", "proxy-managed"}
assert not any(os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"))
assert not any((home / p).exists() for p in (".config/gh/hosts.yml", ".git-credentials", ".netrc"))
pattern = re.compile(rb"(?<![A-Za-z0-9_-])(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|eyJ[A-Za-z0-9_-]{15,}[.]eyJ[A-Za-z0-9_-]{15,}[.][A-Za-z0-9_-]{15,})")
for path in config_dir.rglob("*"):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16_000_000:
        continue
    data = path.read_bytes()
    for match in pattern.finditer(data):
        if data[max(0, match.start() - 8):match.start()] == b"__oailb=":
            # ChatGPT's load-balancer cookie carries host routing and expiry only.
            claims = match[0].split(b".")[1]
            decoded = json.loads(base64.urlsafe_b64decode(claims + b"=" * (-len(claims) % 4)))
            if set(decoded) == {"host", "iss", "aud", "exp", "iat"}:
                continue
        raise RuntimeError("Unexpected credential-shaped value")
'''


class CanaryHandler(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:
        type(self).hits += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


async def verify_network(sandbox: SbxSandbox) -> None:
    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), CanaryHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            CanaryHandler.hits = 0
            port = server.server_port
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/', timeout=5) as response:
                assert response.status == 200
            default = ['curl', '-fsS', '--max-time', '5', '-o', '/dev/null']
            assert (await sandbox.exec([*default, 'https://pypi.org/simple/packaging/'])).returncode == 0
            for host in ('127.0.0.1', 'host.docker.internal'):
                assert (await sandbox.exec([*default, f'http://{host}:{port}/'])).returncode != 0
            assert CanaryHandler.hits == 1
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.sandbox
@pytest.mark.skipif(os.getenv('CONTRIBOS_IT_SBX') != '1', reason='opt-in sbx test')
def test_real_sbx_codex_bundle_and_resume(tmp_path):
    issue_url = os.getenv('CONTRIBOS_IT_ISSUE_URL')
    if not issue_url:
        pytest.skip('Set CONTRIBOS_IT_ISSUE_URL to a user-owned test Issue')
    issue = parse_issue_url(issue_url)
    profile = PROFILES['codex']
    sandbox = SbxSandbox(Settings(home=tmp_path), profile)
    task = TaskContext(uuid.uuid4().hex, issue.repo, issue.number, '', '', isolation='sbx',
                       instructions='Use the test checkout only. Changes are disposable and stay local.')
    workspace = None

    def create_engine() -> AcpEngine:
        return AcpEngine(lambda: sandbox.spawn_agent(profile), stop_process=sandbox.stop,
                         error_mapper=profile.classify_error)

    async def check():
        nonlocal workspace
        workspace = await sandbox.prepare(task)
        assert (await sandbox.exec(['cat', f'{workspace.context_dir}/TASK.md'])).stdout == task.instructions
        await verify_network(sandbox)

        engine = create_engine()
        try:
            session = await engine.ensure_session(task)
            result = await engine.prompt(session, [TextContentBlock(text=(
                'Create exactly one new text file named contribos-m2-smoke.txt in the current repository '
                'with the line "ContribOS sandbox check". Run a Python command to verify its contents. '
                'Do not modify any other files. Do not commit, push or publish.'))])
            assert result.stop_reason == 'end_turn'
            assert 'contribos-m2-smoke.txt' in await sandbox.diff()
        finally:
            await engine.close()

        assert (await sandbox.exec(['python3', '-c', OAUTH_PROBE])).returncode == 0
        bundle = f'{workspace.repo_dir}/smoke.bundle'
        for args in (
            ['add', '-A'],
            ['-c', 'user.name=ContribOS', '-c', 'user.email=smoke@example.invalid', 'commit', '-m', 'Local smoke'],
            ['bundle', 'create', bundle, 'HEAD', f'^{workspace.base_sha}'],
        ):
            assert (await sandbox.exec(['git', *args])).returncode == 0
        exported = await sandbox.export_file(bundle)
        try:
            assert exported.read_bytes().startswith((b'# v2 git bundle\n', b'# v3 git bundle\n'))
        finally:
            exported.unlink()

        await sandbox.stop()
        await sandbox.restore(task)
        assert (await sandbox.exec(['cat', 'contribos-m2-smoke.txt'])).stdout.strip() == 'ContribOS sandbox check'
        engine = create_engine()
        try:
            assert await engine.ensure_session(task) == session
            result = await engine.prompt(session, [TextContentBlock(text='Reply with a short status. Do not edit files.')])
            assert result.stop_reason == 'end_turn'
        finally:
            await engine.close()

    async def run():
        try:
            await asyncio.wait_for(check(), 600)
        finally:
            await sandbox.destroy()

    asyncio.run(run())
