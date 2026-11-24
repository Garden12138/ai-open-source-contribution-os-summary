import subprocess
import sys

import pytest

from contribos.cli import main
from contribos.config import Settings


@pytest.fixture(autouse=True)
def isolated_cli_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTRIBOS_HOME", str(tmp_path))


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "contribos" in capsys.readouterr().out


def test_module_entry():
    result = subprocess.run([sys.executable, "-m", "contribos", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "2.0.0.dev0" in result.stdout


def test_local_data_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRIBOS_HOME", str(tmp_path))
    settings = Settings()
    assert settings.database_path == tmp_path / "contribos.db"
    assert settings.local_token_path == tmp_path / "local-token"
    assert (settings.host, settings.port) == ("127.0.0.1", 8765)


@pytest.mark.parametrize('host', ['0.0.0.0', '::', '::1', '127.0.0.2', '192.168.1.1', 'example.com', '127.0.0.1.evil.example'])
def test_start_rejects_any_other_bind_address(host, tmp_path, monkeypatch):
    monkeypatch.setenv('CONTRIBOS_HOME', str(tmp_path / 'unused'))
    with pytest.raises(SystemExit) as result:
        main(['serve', '--host', host])
    assert result.value.code == 2
    assert not (tmp_path / 'unused').exists()


def test_serve_prints_one_login_link_and_disables_access_logs(tmp_path, monkeypatch, capsys):
    import uvicorn
    from contribos.security import SecurityError
    monkeypatch.setenv('CONTRIBOS_HOME', str(tmp_path))
    invocation = {}
    def run(app, **kwargs):
        invocation.update(app=app, **kwargs)
    monkeypatch.setattr(uvicorn, 'run', run)
    assert main(['serve']) == 0
    output = capsys.readouterr().out
    assert output.count('/desktop/bootstrap?code=') == 1
    assert 'http://127.0.0.1:8765' in output
    assert invocation['access_log'] is False
    assert invocation['host'] == '127.0.0.1'
    code = output.strip().split('code=')[1]
    invocation['app'].state.auth.exchange_startup_code(code)
    with pytest.raises(SecurityError):
        invocation['app'].state.auth.exchange_startup_code(code)


@pytest.mark.parametrize('port', [0, -1, 65536])
def test_invalid_port_rejected(port):
    with pytest.raises(SystemExit) as result:
        main(['serve', '--port', str(port)])
    assert result.value.code == 2


def test_doctor_success_and_suppressed_auth_output(monkeypatch, capsys):
    import secrets
    from types import SimpleNamespace
    import contribos.cli as cli
    monkeypatch.setattr(cli, "agent_statuses", lambda: [])
    canary = secrets.token_urlsafe(32)
    monkeypatch.setenv('CONTRIBOS_GITHUB_TOKEN', canary)
    calls = []
    monkeypatch.setattr(cli.shutil, 'which', lambda name: '/tools/' + name)
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert 'CONTRIBOS_GITHUB_TOKEN' not in kwargs['env']
        if argv[0].endswith('/gh'):
            assert kwargs['stdout'] == subprocess.DEVNULL
            assert kwargs['stderr'] == subprocess.DEVNULL
            return SimpleNamespace(returncode=0, stdout=canary, stderr=canary)
        return SimpleNamespace(returncode=0, stdout='git version 2.50.0', stderr='')
    monkeypatch.setattr(cli.subprocess, 'run', run)
    assert main(['doctor']) == 0
    output = capsys.readouterr().out
    assert all(name + ': [ok]' in output for name in ('python', 'git', 'gh'))
    assert canary not in output
    assert calls[1][0] == ['/tools/gh', 'auth', 'status', '--hostname', 'github.com']
    assert all(options['timeout'] == 5 for _, options in calls)


def test_doctor_missing_tools(monkeypatch, capsys):
    import contribos.cli as cli
    monkeypatch.setattr(cli, "agent_statuses", lambda: [])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: None)
    assert main(['doctor']) == 1
    output = capsys.readouterr().out
    assert 'git: [missing]' in output and 'gh: [missing]' in output


def test_doctor_logged_out(monkeypatch, capsys):
    from types import SimpleNamespace
    import contribos.cli as cli
    monkeypatch.setattr(cli, "agent_statuses", lambda: [])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: name)
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr=''))
    assert main(['doctor']) == 1
    output = capsys.readouterr().out
    assert 'git: [error]' in output and 'gh: [not_logged_in]' in output
    assert 'gh auth login' in output


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired(['check'], 5), OSError('private diagnostic')])
def test_doctor_failures_are_safe(monkeypatch, capsys, error):
    import contribos.cli as cli
    monkeypatch.setattr(cli, "agent_statuses", lambda: [])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: name)
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(cli.subprocess, 'run', run)
    assert main(['doctor']) == 1
    output = capsys.readouterr().out
    assert 'private diagnostic' not in output
    assert 'git: [error]' in output and 'gh: [error]' in output
