import concurrent.futures
import secrets
import stat

import pytest

from contribos.security import LocalAuth, SecurityError, load_or_create_local_token, redact, valid_origin


@pytest.mark.parametrize('prefix', ['ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_', 'sk-', 'sk-proj-', 'sk-svcacct-'])
def test_redact_token_patterns(prefix):
    canary = prefix + secrets.token_hex(24)
    assert redact('before ' + canary + ' after') == 'before [REDACTED] after'


@pytest.mark.parametrize('template', [
    'Authorization: Bearer {}\nnext line',
    'AUTHORIZATION = Basic {}\nnext line',
    '{{"Authorization": "Bearer {}"}}',
    "{{'authorization': 'Bearer {}'}}",
    'https://user:{}@example.com/path',
    'git+https://user:{}@example.com/path',
    'https://user:with@{}@example.com/path',
])
def test_redact_headers_and_url_credentials(template):
    canary = secrets.token_hex(24)
    output = redact(template.format(canary))
    assert canary not in output
    assert '[REDACTED]' in output


def test_redact_preserves_ordinary_text():
    text = 'GET https://github.com/owner/repo/issues/1\nno credentials here'
    assert redact(text) == text


def test_startup_code_one_use_wrong_code_and_expiry():
    now = [100.0]
    auth = LocalAuth(secrets.token_urlsafe(32), clock=lambda: now[0], session_ttl=120)
    code = auth.issue_startup_code()
    with pytest.raises(SecurityError, match='invalid_startup_code'):
        auth.exchange_startup_code('incorrect')
    cookies = auth.exchange_startup_code(code)
    assert auth.valid_session(cookies.session)
    assert not auth.valid_session('unknown')
    assert auth.valid_csrf(cookies.session, cookies.csrf, cookies.csrf)
    assert not auth.valid_csrf(cookies.session, 'injected', 'injected')
    assert not auth.valid_csrf(cookies.session, cookies.csrf, 'different')
    assert not auth.valid_csrf(cookies.session, '', '')
    with pytest.raises(SecurityError):
        auth.exchange_startup_code(code)
    code = auth.issue_startup_code()
    now[0] = 160
    with pytest.raises(SecurityError):
        auth.exchange_startup_code(code)
    now[0] = 220
    assert not auth.valid_session(cookies.session)


def test_reissued_code_invalidates_previous_code():
    auth = LocalAuth(secrets.token_urlsafe(32))
    old = auth.issue_startup_code()
    new = auth.issue_startup_code()
    with pytest.raises(SecurityError):
        auth.exchange_startup_code(old)
    auth.exchange_startup_code(new)


def test_concurrent_startup_exchange_has_one_winner():
    auth = LocalAuth(secrets.token_urlsafe(32))
    code = auth.issue_startup_code()
    def attempt(_):
        try:
            auth.exchange_startup_code(code)
            return True
        except SecurityError:
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1


def test_local_token_persists_with_private_permissions(tmp_path):
    path = tmp_path / 'private' / 'local-token'
    token = load_or_create_local_token(path)
    assert len(token) == 43
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o644)
    assert load_or_create_local_token(path) == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    auth = LocalAuth(token)
    assert auth.valid_local_token(token)
    assert not auth.valid_local_token('wrong')
    assert not auth.valid_local_token('含非 ASCII 的输入')


def test_local_token_rejects_symlink_and_malformed_file(tmp_path):
    target = tmp_path / 'target'
    target.write_text('malformed')
    link = tmp_path / 'link'
    link.symlink_to(target)
    for path in (link, target):
        with pytest.raises(SecurityError, match='^local_token_unavailable$'):
            load_or_create_local_token(path)
    assert target.read_text() == 'malformed'


@pytest.mark.parametrize('origin', [
    '', 'null', 'https://evil.example', 'http://127.0.0.1:8766',
    'https://127.0.0.1:8765', 'http://localhost:8765',
    'http://127.0.0.1:8765/path', 'http://user:pass@127.0.0.1:8765',
    'http://127.0.0.1:8765?query', 'http://127.0.0.1:8765#fragment',
    'http://127.0.0.1:invalid', 'http://[invalid',
])
def test_bad_origins(origin):
    assert not valid_origin(origin, 'http://127.0.0.1:8765')


def test_good_origin():
    assert valid_origin('http://127.0.0.1:8765', 'http://127.0.0.1:8765')
