"""All vendor-specific commands, configuration isolation and error patterns."""

import os
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from contribos.agent.engine import AgentError
from contribos.config import Settings
from contribos.security import clean_environment

RETRYABLE = (r'rate.?limit', r'\b429\b', r'overload', r'\b503\b', r'temporar', r'timed? ?out', r'connection (?:reset|refused)', r'network', r'ECONNRESET')
AUTH_ERRORS = (r'authenticat', r'unauthori[sz]ed', r'not logged in', r'login required', r'\b401\b', r'invalid.*(?:key|token)')
QUOTA_ERRORS = (r'quota', r'insufficient.*(?:credit|balance)', r'usage.?limit', r'credit.*exhaust', r'billing', r'spending.?limit')
REGION_ERRORS = (
    r'unsupported[_ -](?:country|region|territory|location)',
    r'(?:country|region|territory|location)[^.!?]{0,90}(?:not supported|not available|unsupported|unavailable|restricted)',
    r'(?:not supported|not available|unavailable|cannot be used)[^.!?]{0,90}(?:country|region|territory|location)',
    r'geo(?:graphic)?[_ -]?(?:blocked|restricted|restriction)',
)


CODEX_CREDENTIAL_PROBE = r'''
import base64, json, os, pathlib, re, stat, sys, tomllib
home = pathlib.Path.home()
config_dir = pathlib.Path(os.environ["CODEX_HOME"])
config = tomllib.loads((config_dir / "config.toml").read_text())
auth = json.loads((config_dir / "auth.json").read_text())
assert auth == {"OPENAI_API_KEY": "proxy-managed"}
assert config["model_providers"]["sandboxd"]["experimental_bearer_token"] == "oai-oat01-proxy-managed"
# Docker Learn documents this exact, nonfunctional GitHub proxy sentinel.
github_sentinel = b"gho_" + b"sbxproxymanaged" + b"0" * 21
placeholders = {b"", b"proxy-managed", b"oai-oat01-proxy-managed", github_sentinel}
github_vars = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "CONTRIBOS_GITHUB_TOKEN"}
for key in github_vars:
    value = os.fsencode(os.getenv(key, ""))
    assert value in ({b""} if sys.argv[1] == "agent" else placeholders)
assert not os.getenv("SSH_AUTH_SOCK")
pattern = re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{24,}|"
                     rb"-----BEGIN (?P<pem>[A-Z ]*PRIVATE KEY)-----\s+(?:Proc-Type:[^\n]*\nDEK-Info:[^\n]*\n\s*)?"
                     rb"[A-Za-z0-9+/=\r\n]{32,}-----END (?P=pem)-----|"
                     rb"eyJ[A-Za-z0-9_-]{15,}[.]eyJ[A-Za-z0-9_-]{15,}[.][A-Za-z0-9_-]{15,}")

def check_secrets(data):
    # The generated VM hostname contains "sk-" in "ctask-" followed by a UUID.
    data = data.replace(os.fsencode(sys.argv[2]), b" ")
    for match in pattern.finditer(data):
        if match[0] == github_sentinel:
            continue
        if data[max(0, match.start() - 8):match.start()] == b"__oailb=" and match[0].startswith(b"eyJ"):
            # The load-balancer cookie has routing and expiry claims, without account identity.
            payload = match[0].split(b".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
            if set(claims) == {"host", "iss", "aud", "exp", "iat"}:
                continue
        raise ValueError()

for key, value in os.environ.items():
    data = os.fsencode(value)
    if key.upper().endswith(("_TOKEN", "_API_KEY", "_PASSWORD", "_SECRET_ACCESS_KEY")):
        assert data in placeholders
    check_secrets(data)

xdg = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
gh = pathlib.Path(os.environ.get("GH_CONFIG_DIR", xdg / "gh"))
known = {home / p for p in (".config/gh/hosts.yml", ".git-credentials", ".netrc", ".aws/credentials", ".npmrc",
                            ".pypirc", ".gitconfig", ".ssh/id_ed25519", ".ssh/id_rsa", ".docker/config.json")}
known.update((xdg / "git/credentials", xdg / "git/config", gh / "hosts.yml"))
for env in ("AWS_SHARED_CREDENTIALS_FILE", "NETRC", "NPM_CONFIG_USERCONFIG", "GIT_CONFIG_GLOBAL"):
    if os.getenv(env) and os.getenv(env) != os.devnull:
        known.add(pathlib.Path(os.environ[env]))
field = re.compile(rb"(?im)(?:^|[/:\s])(?:oauth_token|_authToken|_auth|password|_password|token|"
                   rb"aws_secret_access_key|aws_session_token)[ \t]*(?:[=:][ \t]*|[ \t]+)[\"']?([^\"'\s#;]+)")
for path in known:
    if not path.exists():
        continue
    info = path.stat()
    assert stat.S_ISREG(info.st_mode) and info.st_size <= 16_000_000
    data = path.read_bytes()
    check_secrets(data)
    for value in field.findall(data):
        assert value in placeholders or re.fullmatch(rb"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value)
    for value in re.findall(rb"https?://[^/\s@]*:([^/\s@]+)@", data):
        assert value in placeholders
    assert not re.search(rb"(?i)\bauthorization:\s*(?:basic|bearer)\s+\S+", data)
    if path.name == "config.json" and path.parent.name == ".docker":
        assert not any(entry.get("auth") or entry.get("identitytoken") for entry in json.loads(data).get("auths", {}).values())

for path in config_dir.rglob("*"):
    if not path.is_symlink() and path.is_file() and path.stat().st_size <= 16_000_000:
        check_secrets(path.read_bytes())
'''


@dataclass(frozen=True)
class SbxProfile:
    workload: str
    kit: str
    auth_mode: str
    service: str
    domains: tuple[str, ...]
    credential_probe: str = ''
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    command: tuple[str, ...]
    detect: tuple[str, ...]
    version: tuple[str, ...]
    auth_check: tuple[str, ...]
    login_command: tuple[str, ...]
    install_hint: str
    config_isolation: str
    host_args: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    sandbox_unavailable_reason: str = ''
    isolation_warning: str = ''
    auth_check_reliable: bool = True
    version_stderr: bool = False
    auth_json_field: str | None = None
    retryable_patterns: tuple[str, ...] = RETRYABLE
    modes: tuple[str, str] = ('plan', 'build')
    sbx_agent: str | None = None
    sbx: SbxProfile | None = None

    def config_dir(self, settings: Settings) -> Path:
        return settings.home / 'agent-homes' / self.id

    def render(self, values: tuple[str, ...], settings: Settings) -> list[str]:
        command = [v.replace('{config_dir}', str(self.config_dir(settings))) for v in values]
        if command:
            command[0] = settings.tool_paths.get(command[0]) or command[0]
        return command

    def argv(self, settings: Settings) -> list[str]:
        return self.render(self.command + self.host_args, settings)

    def env(self, settings: Settings) -> dict[str, str]:
        return {k: v.replace('{config_dir}', str(self.config_dir(settings))) for k, v in self.environment.items()}

    def login_hint(self, settings: Settings) -> str:
        env = ' '.join(f'{k}={shlex.quote(v)}' for k, v in self.env(settings).items()
                       if k == 'HOME' or k.endswith(('_HOME', '_DIR', '_CREDENTIAL_STORE')))
        return (env + ' ' + shlex.join(self.render(self.login_command, settings))).strip()

    def classify_error(self, exc: Exception) -> AgentError:
        text = str(exc) + ' ' + json.dumps(getattr(exc, 'data', None), default=str)
        if any(re.search(pattern, text, re.I) for pattern in REGION_ERRORS):
            return AgentError('agent_region_unavailable', 'Agent service is unavailable in this region; check your network egress or proxy settings.')
        if any(re.search(p, text, re.I) for p in QUOTA_ERRORS):
            return AgentError('quota_exhausted', 'Agent quota exhausted; check the account before continuing.')
        if any(re.search(p, text, re.I) for p in AUTH_ERRORS):
            return AgentError('authentication_required', 'Agent login required; run contribos agents for login instructions.')
        if isinstance(exc, (ConnectionError, EOFError)):
            return AgentError('agent_disconnected', 'Agent disconnected; continue to reconnect.', retryable=True)
        if any(re.search(p, text, re.I) for p in self.retryable_patterns):
            return AgentError('agent_unavailable', 'Agent is temporarily unavailable; continue to retry.', retryable=True)
        return AgentError('agent_request_failed')


PROFILES = {
    'codex': AgentProfile(
        id='codex', name='Codex', command=('codex-acp',), detect=('codex-acp', '--version'),
        version=('codex-acp', '--version'), auth_check=('codex', 'login', 'status'), login_command=('codex', 'login'),
        install_hint='npm i -g @agentclientprotocol/codex-acp', config_isolation='Dedicated CODEX_HOME; sign in there separately.',
        environment={'CODEX_HOME': '{config_dir}', 'INITIAL_AGENT_MODE': 'workspace-write',
                     'CODEX_CONFIG': json.dumps({'sandbox_mode': 'workspace-write', 'approval_policy': 'on-request',
                                                 'features': {'apps': False, 'plugins': False}})},
        modes=('read-only', 'workspace-write'), sbx_agent='codex',
        sbx=SbxProfile(
            credential_probe=CODEX_CREDENTIAL_PROBE,
            workload='docker.io/sbx/codex@sha256:b0d764924b6b47965b862c63cb5c3326b9f57d6dbb9e40cfc483577ba461667a',
            kit='docker.io/sbx/codex-acp@sha256:97209faa5012af3f3012d8d66768e249883c90409ba2a5a9af4aa39299ca2b85',
            auth_mode='oauth · openai',
            service='openai',
            domains=('api.openai.com:443', 'openai.com:443', 'auth.openai.com:443', 'chatgpt.com:443',
                     'files.openai.com:443', 'releases.openai.com:443', 'dhi.io:80'),
            environment={'CODEX_CONFIG': json.dumps({
                'features': {'apps': False, 'plugins': False},
                'mcp_servers': {'mcp-gateway': {'enabled': False}},
            })})),
    'cursor': AgentProfile(
        id='cursor', name='Cursor', command=('cursor-agent', 'acp'), detect=('cursor-agent', 'acp', '--help'),
        version=('cursor-agent', '--version'), auth_check=('cursor-agent', 'status', '--format', 'json'),
        auth_json_field='isAuthenticated', login_command=('cursor-agent', 'login'),
        install_hint='Install Cursor CLI: https://cursor.com/docs/cli/installation',
        config_isolation='Dedicated HOME, configuration and file credential store.',
        environment={'HOME': '{config_dir}', 'USERPROFILE': '{config_dir}',
                     'XDG_CONFIG_HOME': '{config_dir}/.config', 'CURSOR_CONFIG_DIR': '{config_dir}',
                     'AGENT_CLI_CREDENTIAL_STORE': 'file'},
        host_args=('--sandbox', 'enabled'), sbx_agent='cursor',
        sandbox_unavailable_reason='官方 kit 尚待在 VM 内登录并验证取消与恢复。',
        isolation_warning='MCP/plugin isolation still needs live verification.'),
    'antigravity': AgentProfile(
        id='antigravity', name='Antigravity', sandbox_unavailable_reason='自建 kit 的 ACP 会话需要在 VM 内登录，推理和恢复待验收。当前仅支持本机直跑。',
        command=('agy', 'acp'), detect=('agy', 'acp', '--help'), version=('agy', '--version'),
        auth_check=('agy', 'models'), auth_check_reliable=False,
        version_stderr=True,
        login_command=('agy', 'acp', '--app-data-dir', 'contribos-antigravity', '--gemini-dir', '{config_dir}/gemini', '--login'),
        install_hint='Install Antigravity CLI: https://antigravity.google/docs/cli',
        config_isolation='Dedicated HOME and --gemini-dir; default customizations disabled.',
        environment={'HOME': '{config_dir}', 'USERPROFILE': '{config_dir}',
                     'XDG_CONFIG_HOME': '{config_dir}/.config'},
        # The language server expects an application directory name relative to the home directory.
        host_args=('--app-data-dir', 'contribos-antigravity', '--gemini-dir', '{config_dir}/gemini',
                   '--include-default-customizations=false', '--sandbox', '--log-file', '/dev/null'),
        isolation_warning='No reliable noninteractive login status command; verify login and personal MCP/plugin isolation in ACP acceptance.'),
}


@dataclass(frozen=True)
class AgentStatus:
    id: str
    status: str
    executable: str | None
    version: str
    login: str
    login_hint: str
    detail: str
    isolation_warning: str


def check_profile(profile: AgentProfile, settings: Settings) -> AgentStatus:
    executable = settings.tool(profile.command[0])
    login_hint = profile.login_hint(settings)
    if not executable:
        return AgentStatus(profile.id, 'missing', None, '', 'unknown', login_hint, profile.install_hint, profile.isolation_warning)
    # Checks never inherit token-bearing variables, and their diagnostics are discarded.
    env = clean_environment({k: os.environ[k] for k in ('PATH', 'TMPDIR', 'SYSTEMROOT') if k in os.environ},
                            settings.home / 'runtime' / 'profile-checks' / profile.id)
    env = settings.tool_environment({**env, **profile.env(settings)})
    version = ''
    login = 'unknown'
    status = 'error'
    try:
        result = subprocess.run(profile.render(profile.version, settings), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE if profile.version_stderr else subprocess.DEVNULL,
            text=True, timeout=5, check=False, env=env)
        if result.returncode != 0:
            raise OSError()
        version_output = result.stdout + (result.stderr if profile.version_stderr else '')
        match = re.search(r'\b\d+(?:\.\d+){1,3}(?:[-+][\w.-]+)?\b', version_output)
        version = match[0] if match else 'unknown'
        detected = subprocess.run(profile.render(profile.detect, settings), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False, env=env)
        if detected.returncode != 0:
            raise OSError()
        if profile.auth_check_reliable:
            result = subprocess.run(profile.render(profile.auth_check, settings), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if profile.auth_json_field else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False, env=env, text=True)
            authenticated = result.returncode == 0
            if profile.auth_json_field:
                authenticated = authenticated and json.loads(result.stdout).get(profile.auth_json_field) is True
            login = 'logged_in' if authenticated else 'not_logged_in'
            status = 'ok' if authenticated else 'not_logged_in'
        else:
            status = 'unknown'
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        pass
    detail = f'{executable}; {version}; login={login}. {profile.config_isolation} Login: {login_hint}'
    return AgentStatus(profile.id, status, executable, version, login, login_hint, detail, profile.isolation_warning)


def agent_statuses(settings: Settings | None = None) -> list[AgentStatus]:
    settings = settings or Settings()
    return [check_profile(profile, settings) for profile in PROFILES.values()]
