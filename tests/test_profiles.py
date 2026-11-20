import subprocess
import json
from types import SimpleNamespace

import pytest

from contribos.agent.engine import AgentError
from contribos.agent.profiles import PROFILES, AgentStatus, check_profile
from contribos.config import Settings


@pytest.mark.parametrize('name', PROFILES)
def test_commands_config_isolation_and_safe_checks(name, tmp_path, monkeypatch):
    import contribos.agent.profiles as module
    profile = PROFILES[name]
    settings = Settings(home=tmp_path)
    monkeypatch.setenv('GITHUB_TOKEN', 'unused')
    monkeypatch.setattr(module.shutil, 'which', lambda command: '/tools/' + command)
    calls = []
    def execute(argv, **kwargs):
        assert 'GITHUB_TOKEN' not in kwargs['env']
        assert kwargs['stderr'] in {subprocess.DEVNULL, subprocess.PIPE}
        calls.append((argv, kwargs))
        output = json.dumps({'isAuthenticated': True}) if 'status' in argv and '--format' in argv else '1.2.3'
        return SimpleNamespace(returncode=0, stdout=output, stderr='1.2.3')
    monkeypatch.setattr(module.subprocess, 'run', execute)
    result = check_profile(profile, settings)
    assert result.status == ('ok' if profile.auth_check_reliable else 'unknown')
    assert str(tmp_path / 'agent-homes' / name) in profile.login_hint(settings)
    assert 'dangerously' not in ' '.join(profile.argv(settings))
    if name == 'codex':
        assert 'workspace-write' in profile.env(settings)['CODEX_CONFIG']
    if profile.auth_check_reliable:
        assert calls[-1][1]['stdout'] == (subprocess.PIPE if profile.auth_json_field else subprocess.DEVNULL)


@pytest.mark.parametrize('message,code,retryable', [
    ('Rate limit 429', 'agent_unavailable', True), ('network timeout', 'agent_unavailable', True),
    ('quota exhausted 429', 'quota_exhausted', False), ('Authentication required', 'authentication_required', False),
    ('usage limit exceeded', 'quota_exhausted', False), ('invalid API key', 'authentication_required', False),
    ('other error', 'agent_request_failed', False),
])
def test_profile_error_mapping(message, code, retryable):
    for profile in PROFILES.values():
        result = profile.classify_error(RuntimeError(message))
        assert isinstance(result, AgentError)
        assert result.code == code and result.retryable == retryable
        assert message not in result.message


def test_agents_and_doctor_share_statuses(monkeypatch, capsys):
    import contribos.cli as cli
    status = AgentStatus('codex', 'not_logged_in', '/tools/codex-acp', '1', 'not_logged_in',
        'CODEX_HOME=/dedicated codex login', 'Dedicated config', 'Verify plugins')
    monkeypatch.setattr(cli, 'agent_statuses', lambda: [status])
    assert cli.main(['agents']) == 1
    output = capsys.readouterr().out
    assert 'codex: [not_logged_in]' in output and status.login_hint in output and 'Verify plugins' in output
    monkeypatch.setattr(cli.shutil, 'which', lambda name: None)
    assert cli.doctor_checks()[-1].name == 'codex'


def test_missing_and_failed_checks(tmp_path, monkeypatch):
    import contribos.agent.profiles as module
    profile = PROFILES['codex']
    monkeypatch.setattr(module.shutil, 'which', lambda command: None)
    assert 'npm i -g' in check_profile(profile, Settings(home=tmp_path)).detail
    monkeypatch.setattr(module.shutil, 'which', lambda command: command)
    def fail(*args, **kwargs):
        raise OSError('private failure')
    monkeypatch.setattr(module.subprocess, 'run', fail)
    result = check_profile(profile, Settings(home=tmp_path))
    assert result.status == 'error' and 'private failure' not in result.detail


def test_cursor_exit_zero_is_not_proof_of_login(tmp_path, monkeypatch):
    import contribos.agent.profiles as module
    monkeypatch.setattr(module.shutil, 'which', lambda command: command)
    def execute(argv, **kwargs):
        output = json.dumps({'isAuthenticated': False}) if 'status' in argv else '2026.11.13'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    monkeypatch.setattr(module.subprocess, 'run', execute)
    status = check_profile(PROFILES['cursor'], Settings(home=tmp_path))
    assert status.status == 'not_logged_in'


def test_nested_acp_errors_are_classified_without_exposing_details():
    from acp import RequestError
    error = PROFILES['codex'].classify_error(RequestError(-32603, 'Internal error', {'details': '429 rate limit'}))
    assert error.code == 'agent_unavailable' and error.retryable
    assert '429' not in error.message


def test_codex_starts_in_workspace_mode_with_user_approval(tmp_path):
    profile = PROFILES['codex']
    env = profile.env(Settings(home=tmp_path))
    assert env['INITIAL_AGENT_MODE'] == 'workspace-write'
    assert json.loads(env['CODEX_CONFIG'])['approval_policy'] == 'on-request'
    assert profile.modes[1] == 'workspace-write'


def test_codex_profile_disables_account_connectors_and_plugins(tmp_path):
    env = PROFILES['codex'].env(Settings(home=tmp_path))
    assert json.loads(env['CODEX_CONFIG'])['features'] == {'apps': False, 'plugins': False}


def test_cursor_uses_same_isolated_home_for_status_launch_and_login(tmp_path, monkeypatch):
    import contribos.agent.profiles as module

    settings = Settings(home=tmp_path)
    profile = PROFILES['cursor']
    directory = str(profile.config_dir(settings))
    assert profile.env(settings)['HOME'] == directory
    assert f'HOME={directory}' in profile.login_hint(settings)
    assert 'AGENT_CLI_CREDENTIAL_STORE=file' in profile.login_hint(settings)

    monkeypatch.setattr(module.shutil, 'which', lambda command: command)

    def execute(argv, **kwargs):
        assert kwargs['env']['HOME'] == directory
        assert kwargs['env']['CURSOR_CONFIG_DIR'] == directory
        assert kwargs['env']['AGENT_CLI_CREDENTIAL_STORE'] == 'file'
        stdout = json.dumps({'isAuthenticated': True}) if 'status' in argv else '2026.11.19'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    monkeypatch.setattr(module.subprocess, 'run', execute)
    assert check_profile(profile, settings).login == 'logged_in'


def test_antigravity_login_and_acp_share_isolated_directories(tmp_path):
    settings = Settings(home=tmp_path)
    profile = PROFILES['antigravity']
    launch = profile.argv(settings)
    login = profile.render(profile.login_command, settings)
    for flag in ('--app-data-dir', '--gemini-dir'):
        assert launch[launch.index(flag) + 1] == login[login.index(flag) + 1]
    assert '/' not in launch[launch.index('--app-data-dir') + 1]
    assert profile.env(settings)['HOME'] == str(profile.config_dir(settings))
    assert '--sandbox' in launch
