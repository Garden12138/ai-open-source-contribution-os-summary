"""All vendor-specific commands, configuration isolation and error patterns."""

import os
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from contribos.agent.engine import AgentError
from contribos.config import OpenModel, Settings
from contribos.security import GITHUB_CREDENTIAL_VARS, clean_environment

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
    kit: str | None
    auth_mode: str | None
    service: str
    domains: tuple[str, ...]
    credential_probe: str = ''
    environment: dict[str, str] = field(default_factory=dict)
    install: tuple[str, ...] = ()
    command: tuple[str, ...] = ()


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
    accepted: bool = True
    open_model: bool = False

    def model_environment(self, model: OpenModel, directory: str, *, guest: bool) -> tuple[dict[str, str], dict[str, str]]:
        """Return launch environment and non-secret, profile-specific configuration files."""
        if not self.open_model:
            return {}, {}
        if not model.model:
            raise AgentError('model_not_configured', 'Select the provider and model in Settings.')
        base = 'https://api.deepseek.com/v1' if model.provider == 'deepseek' else (
            f'http://{"host.docker.internal" if guest else "127.0.0.1"}:{model.xcli_port}/v1')
        key = 'contribos-model-proxy' if guest else provider_key(model)
        if self.id == 'opencode':
            config = {'model': 'contribos/' + model.model, 'small_model': 'contribos/' + model.model,
                      'enabled_providers': ['contribos'], 'permission': 'ask',
                      'provider': {'contribos': {'npm': '@ai-sdk/openai-compatible', 'name': model.provider,
                          'options': {'baseURL': base, 'apiKey': '{env:CONTRIBOS_PROVIDER_KEY}'},
                          'models': {model.model: {'name': model.model}}}}}
            return {'OPENCODE_CONFIG_CONTENT': json.dumps(config), 'CONTRIBOS_PROVIDER_KEY': key}, {}
        # The 1.16 ACP subcommand reads AgentStore even when --override-with-envs is passed.
        spec = {'llm': {'model': 'openai/' + model.model, 'base_url': base, 'usage_id': 'agent'}, 'tools': []}
        return {'OPENAI_API_KEY': key, 'OPENHANDS_PERSISTENCE_DIR': directory}, {'agent_settings.json': json.dumps(spec)}

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
        return self.isolated_env(str(self.config_dir(settings)))

    def isolated_env(self, directory: str) -> dict[str, str]:
        return {k: v.replace('{config_dir}', directory) for k, v in self.environment.items()}

    def login_hint(self, settings: Settings) -> str:
        if self.open_model:
            return (f'Select a model in Settings. Dedicated home: {self.config_dir(settings)}. '
                    'DeepSeek uses DEEPSEEK_API_KEY in the backend environment.')
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


def provider_key(model: OpenModel) -> str:
    key = os.environ.get('DEEPSEEK_API_KEY' if model.provider == 'deepseek' else 'CONTRIBOS_XCLI_API_KEY', '')
    if model.provider == 'deepseek' and not key:
        raise AgentError('model_key_missing', 'Set DEEPSEEK_API_KEY for the backend process.')
    if key and (any(value in key for name in GITHUB_CREDENTIAL_VARS if (value := os.environ.get(name)))
                or re.search(r'gh[pousr]_|github_pat_', key) or not re.fullmatch(r'[!-~]{1,4096}', key)):
        raise AgentError('model_key_invalid', 'Check the model credential in the backend environment.')
    return key or 'contribos-model-proxy'


OPEN_HOME = {'HOME': '{config_dir}', 'USERPROFILE': '{config_dir}', 'XDG_CONFIG_HOME': '{config_dir}/.config',
             'XDG_DATA_HOME': '{config_dir}/.local/share', 'XDG_CACHE_HOME': '{config_dir}/.cache',
             'XDG_STATE_HOME': '{config_dir}/.local/state'}
OPEN_WARNING = '尚待真实模型完成 ACP 接入验收。可通过 contribos chat 显式测试。'
SHELL_KIT = 'docker.io/sbx/shell@sha256:a43a179b1c6b5100b59db6edc15b4a348f71e7d957f8d6f35368c3750525c9c6'
INSTALL_OPENCODE = '''set -eu
case "$(uname -m)" in aarch64) arch=arm64;; x86_64) arch=x64-baseline;; *) exit 1;; esac
name=opencode-linux-$arch
mkdir -p /home/agent/.local/bin
curl -fsSL "https://registry.npmjs.org/$name/-/$name-1.19.6.tgz" -o /tmp/contribos-opencode.tgz
tar -xOz -f /tmp/contribos-opencode.tgz package/bin/opencode > /home/agent/.local/bin/opencode
chmod 755 /home/agent/.local/bin/opencode
'''
OPENCODE_FLAGS = {'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
                  'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_SHARE': '1'}


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
    'opencode': AgentProfile(
        id='opencode', name='OpenCode', command=('opencode', 'acp'), detect=('opencode', 'acp', '--help'),
        version=('opencode', '--version'), auth_check=('opencode', 'auth', 'list'), login_command=('opencode',),
        auth_check_reliable=False, open_model=True, accepted=False, isolation_warning=OPEN_WARNING,
        install_hint='npm install -g opencode-ai@1', config_isolation='Dedicated HOME and XDG directories.',
        environment={**OPEN_HOME, **OPENCODE_FLAGS}, sbx_agent='shell',
        sbx=SbxProfile(workload=SHELL_KIT, kit=None, auth_mode=None, service='openai', domains=(),
                       install=('sh', '-c', INSTALL_OPENCODE), command=('/home/agent/.local/bin/opencode', 'acp'))),
    'openhands': AgentProfile(
        id='openhands', name='OpenHands', command=('openhands', 'acp'), detect=('openhands', 'acp', '--help'),
        version=('openhands', '--version'), auth_check=('openhands', 'acp', '--help'), login_command=('openhands',),
        auth_check_reliable=False, open_model=True, accepted=False, isolation_warning=OPEN_WARNING,
        install_hint='uv tool install openhands==1.16.0 --python 3.12',
        config_isolation='Dedicated HOME, XDG directories and per-workspace OpenHands persistence.',
        environment=OPEN_HOME, sbx_agent='shell',
        sbx=SbxProfile(workload=SHELL_KIT, kit=None, auth_mode=None, service='openai', domains=(),
                       install=('uv', 'tool', 'install', '--python', '3.12', 'openhands==1.16.0'),
                       command=('/home/agent/.local/bin/openhands', 'acp'))),
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
