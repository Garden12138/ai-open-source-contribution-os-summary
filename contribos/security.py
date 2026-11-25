"""Redaction and process-local authentication for the loopback HTTP server."""

import hashlib
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

SESSION_COOKIE = "contribos_session"
CSRF_COOKIE = "contribos_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
GITHUB_CREDENTIAL_VARS = frozenset({'CONTRIBOS_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
                                    'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'})

_PATTERNS = (
    (re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]+"), "[REDACTED]"),
    (re.compile(r"(?i)([\"']authorization[\"']\s*:\s*[\"'])[^\"'\r\n]*"), r"\1[REDACTED]"),
    (re.compile(r"(?im)(\bauthorization\s*[:=]\s*)[^\r\n]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/?#<>\"']*@"), r"\1[REDACTED]@"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clean_environment(source: Mapping[str, str], runtime_dir: Path) -> dict[str, str]:
    """Remove inherited credential/config overrides and isolate default credential files."""
    blocked = {'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'BASH_ENV', 'ENV',
               'ZDOTDIR', 'NODE_OPTIONS', 'PYTHONPATH', 'PYTHONHOME', 'CODEX_HOME', 'CODEX_CONFIG', 'CODEX_PATH',
               'DEFAULT_AUTH_REQUEST', 'MODEL_PROVIDER', 'INITIAL_AGENT_MODE', 'APP_SERVER_LOGS',
               'CURSOR_CONFIG_DIR', 'CODEX_REMOTE_AUTH_TOKEN', 'NETRC', 'CURL_HOME', 'DOCKER_CONFIG',
               'AWS_SHARED_CREDENTIALS_FILE', 'NPM_CONFIG_USERCONFIG', 'KUBECONFIG', 'GNUPGHOME'}
    env = {k: v for k, v in source.items() if k not in blocked and not k.upper().startswith(
        ('GIT_', 'GCM_', 'GH_', 'GITHUB_', 'CONTRIBOS_', 'LD_', 'DYLD_', 'OPENCODE_', 'OPENHANDS_',
         'LLM_', 'OPENAI_', 'LITELLM_', 'XDG_')) and not k.upper().endswith(
        ('_TOKEN', '_API_KEY', '_PASSWORD', '_SECRET_ACCESS_KEY'))}
    env.update(HOME=str(runtime_dir / 'home'), USERPROFILE=str(runtime_dir / 'home'),
        XDG_CONFIG_HOME=str(runtime_dir / 'home' / '.config'),
        GH_CONFIG_DIR=str(runtime_dir / 'gh'), GIT_CONFIG_GLOBAL=str(runtime_dir / 'gitconfig'),
        GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
        GIT_ASKPASS='/usr/bin/false', SSH_ASKPASS='/usr/bin/false', GIT_SSH_COMMAND='/usr/bin/false',
        GIT_CONFIG_COUNT='5', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='',
        GIT_CONFIG_KEY_1='core.hooksPath', GIT_CONFIG_VALUE_1=os.devnull,
        GIT_CONFIG_KEY_2='core.fsmonitor', GIT_CONFIG_VALUE_2='false',
        GIT_CONFIG_KEY_3='http.extraHeader', GIT_CONFIG_VALUE_3='',
        GIT_CONFIG_KEY_4='credential.interactive', GIT_CONFIG_VALUE_4='false')
    return env


def without_github_credentials(environment: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if key not in GITHUB_CREDENTIAL_VARS}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SecurityError(ValueError):
    """A safe error code; never includes submitted credentials or raw exceptions."""


def load_or_create_local_token(path: Path) -> str:
    """Create privately, reject symlinks, and repair permissions on existing tokens."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600)
        except FileExistsError:
            descriptor = os.open(path, os.O_RDONLY | flags)
            with os.fdopen(descriptor, "r", encoding="ascii") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise SecurityError("local_token_unavailable")
                os.fchmod(stream.fileno(), 0o600)
                token = stream.read(256).strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                    raise SecurityError("local_token_unavailable")
                return token
        else:
            token = secrets.token_urlsafe(32)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return token
    except (OSError, UnicodeError):
        raise SecurityError("local_token_unavailable") from None


@dataclass(frozen=True)
class SessionCookies:
    session: str = field(repr=False)
    csrf: str = field(repr=False)


@dataclass(frozen=True)
class _Session:
    csrf_hash: str
    expires_at: float


class LocalAuth:
    def __init__(
        self,
        local_token: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        session_ttl: float = 12 * 60 * 60,
    ) -> None:
        self._local_token_hash = _digest(local_token)
        self._clock = clock
        self._session_ttl = session_ttl
        self._startup: tuple[str, float] | None = None
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def issue_startup_code(self, code: str | None = None) -> str:
        code = secrets.token_urlsafe(32) if code is None else code
        with self._lock:
            self._startup = (_digest(code), self._clock() + 60)
        return code

    def exchange_startup_code(self, code: str) -> SessionCookies:
        with self._lock:
            now = self._clock()
            if (
                self._startup is None
                or now >= self._startup[1]
                or not secrets.compare_digest(_digest(code), self._startup[0])
            ):
                raise SecurityError("invalid_startup_code")
            self._startup = None
            self._sessions = {k: v for k, v in self._sessions.items() if v.expires_at > now}
            # Login links are issued locally; bound session storage even after many logins.
            if len(self._sessions) >= 64:
                del self._sessions[next(iter(self._sessions))]
            cookies = SessionCookies(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
            self._sessions[_digest(cookies.session)] = _Session(_digest(cookies.csrf), now + self._session_ttl)
            return cookies

    def valid_local_token(self, token: str) -> bool:
        return bool(token) and secrets.compare_digest(_digest(token), self._local_token_hash)

    def valid_session(self, cookie: str) -> bool:
        with self._lock:
            return self._session(cookie) is not None

    def _session(self, cookie: str) -> _Session | None:
        key = _digest(cookie)
        session = self._sessions.get(key)
        if session and session.expires_at <= self._clock():
            del self._sessions[key]
            return None
        return session

    def valid_csrf(self, session_cookie: str, csrf_cookie: str, csrf_header: str) -> bool:
        if not csrf_cookie or not csrf_header:
            return False
        with self._lock:
            session = self._session(session_cookie)
            return bool(
                session
                and secrets.compare_digest(_digest(csrf_cookie), _digest(csrf_header))
                and secrets.compare_digest(_digest(csrf_cookie), session.csrf_hash)
            )


def valid_origin(origin: str, expected: str) -> bool:
    """Accept only the configured HTTP origin, never credentials, paths or null."""
    try:
        parsed = urlsplit(origin)
        target = urlsplit(expected)
        return bool(
            origin
            and parsed.scheme in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and parsed.hostname == target.hostname
            and parsed.scheme == target.scheme
            and (parsed.port or (443 if parsed.scheme == "https" else 80))
            == (target.port or (443 if target.scheme == "https" else 80))
        )
    except ValueError:
        return False
