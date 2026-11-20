import secrets

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from contribos.config import Settings
from contribos.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, LocalAuth
from contribos.web.app import create_app

ORIGIN = 'http://127.0.0.1:8765'


@pytest.fixture
def web(tmp_path):
    token = secrets.token_urlsafe(32)
    now = [0.0]
    auth = LocalAuth(token, clock=lambda: now[0])
    app = create_app(Settings(home=tmp_path), auth=auth)
    @app.api_route('/api/protected', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    async def protected():
        return {'ok': True}
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, auth, token, now


def login(client, auth):
    code = auth.issue_startup_code()
    result = client.get('/desktop/bootstrap', params={'code': code}, follow_redirects=False)
    assert result.status_code == 303
    return code, result


def test_health_static_and_migrated_database(web):
    client, _, _, _ = web
    assert client.get('/api/health').json() == {'status': 'ok'}
    assert client.head('/api/health').status_code == 200
    assert client.get('/').status_code == 200
    assert 'ContribOS' in client.get('/static/index.html').text
    assert client.get('/static/styles.css').status_code == 200
    assert client.get('/static/../security.py').status_code == 404
    assert client.get('/static/.env').status_code == 404
    response = client.get('/')
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert (client.app.state.settings.database_path).is_file()


@pytest.mark.parametrize('method', ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@pytest.mark.parametrize('path', ['/api', '/api/', '/api/protected', '/api/not-implemented'])
def test_all_api_paths_require_identity(web, method, path):
    assert web[0].request(method, path).status_code == 401


def test_health_post_is_not_an_auth_bypass(web):
    assert web[0].post('/api/health').status_code == 401


def test_bootstrap_cookie_and_replay(web):
    client, auth, _, _ = web
    code, response = login(client, auth)
    assert response.headers['location'] == '/'
    cookies = response.headers.get_list('set-cookie')
    session = next(c for c in cookies if c.startswith(SESSION_COOKIE + '='))
    csrf = next(c for c in cookies if c.startswith(CSRF_COOKIE + '='))
    assert 'HttpOnly' in session and 'SameSite=strict' in session and 'Path=/' in session
    assert 'HttpOnly' not in csrf and 'SameSite=strict' in csrf
    assert code not in response.text
    assert client.get('/api/protected').status_code == 200
    replay = client.get('/desktop/bootstrap', params={'code': code})
    assert replay.status_code == 401 and code not in replay.text
    assert replay.headers['cache-control'] == 'no-store'


def test_expired_bootstrap(web):
    client, auth, _, now = web
    code = auth.issue_startup_code()
    now[0] = 60
    assert client.get('/desktop/bootstrap', params={'code': code}).status_code == 401
    assert client.get('/api/protected').status_code == 401


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_browser_origin_and_csrf(web, method):
    client, auth, _, _ = web
    login(client, auth)
    csrf = client.cookies[CSRF_COOKIE]
    assert client.request(method, '/api/protected').status_code == 403
    assert client.request(method, '/api/protected', headers={'Origin': ORIGIN}).json()['error']['code'] == 'csrf_rejected'
    headers = {'Origin': 'http://evil.example', CSRF_HEADER: csrf}
    assert client.request(method, '/api/protected', headers=headers).json()['error']['code'] == 'origin_rejected'
    headers['Origin'] = ORIGIN
    headers[CSRF_HEADER] = 'wrong'
    assert client.request(method, '/api/protected', headers=headers).status_code == 403
    headers[CSRF_HEADER] = csrf
    assert client.request(method, '/api/protected', headers=headers).status_code == 200
    client.cookies.set(CSRF_COOKIE, 'injected', domain='127.0.0.1', path='/')
    headers[CSRF_HEADER] = 'injected'
    assert client.request(method, '/api/protected', headers=headers).status_code == 403


def test_bearer_cli_and_browser_origin(web):
    client, auth, token, _ = web
    headers = {'Authorization': f'Bearer {token}'}
    assert client.get('/api/protected', headers=headers).status_code == 200
    assert client.post('/api/protected', headers=headers).status_code == 200
    headers['Origin'] = 'http://evil.example'
    assert client.post('/api/protected', headers=headers).status_code == 403
    headers['Origin'] = ORIGIN
    assert client.post('/api/protected', headers=headers).status_code == 403
    login(client, auth)
    headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
    assert client.post('/api/protected', headers=headers).status_code == 200
    headers['Authorization'] = 'Bearer invalid'
    assert client.get('/api/protected', headers=headers).status_code == 401


def test_host_rebinding_rejected(web):
    client = web[0]
    for host in ('evil.example', '127.0.0.1:9999'):
        assert client.get('/api/health', headers={'Host': host}).status_code == 400


def test_errors_never_echo_secrets(web, caplog, capsys):
    client, _, token, _ = web
    @client.app.get('/api/fail')
    async def fail():
        raise RuntimeError(token)
    @client.app.get('/api/invalid')
    async def invalid(number: int):
        return {'number': number}
    @client.app.get('/api/exception')
    async def exception():
        raise HTTPException(409, detail=token)
    headers = {'Authorization': f'Bearer {token}'}
    for path, status in [('/api/fail', 500), ('/api/invalid?number=' + token, 422), ('/api/exception', 409)]:
        response = client.get(path, headers=headers)
        assert response.status_code == status
        assert token not in response.text
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err + caplog.text


def test_doctor_endpoint_requires_auth_and_uses_same_checks(web, monkeypatch):
    import contribos.cli as cli
    client, _, token, _ = web
    monkeypatch.setattr(cli, 'doctor_report', lambda: [{'name': 'python', 'status': 'ok', 'detail': 'Python >=3.11'}])
    assert client.get('/api/doctor').status_code == 401
    response = client.get('/api/doctor', headers={'Authorization': f'Bearer {token}'})
    assert response.status_code == 200
    assert response.json()['checks'][0]['name'] == 'python'


@pytest.mark.parametrize('host_header', ['127.0.0.1', '127.0.0.1:80'])
def test_default_http_port_authority(tmp_path, host_header):
    auth = LocalAuth(secrets.token_urlsafe(32))
    app = create_app(Settings(home=tmp_path, port=80), auth=auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.get('/api/health', headers={'Host': host_header}).status_code == 200
