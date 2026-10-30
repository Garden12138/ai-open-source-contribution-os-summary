"""Offline boundary contracts; these do not claim a real DeepSeek delivery."""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.migrations.versions import MIGRATIONS
from app.model_settings import (
    ConnectionInput, ModelSettingsService, DEEPSEEK_BASE_URL, DEEPSEEK_MODELS,
    model_presets,
    task_identity, configure_bound_worker,
)
from app.providers.contracts import ProviderRunError
from app.providers.model_secrets import GatewaySecretStore
from app.providers.nvidia_nim import _completion, STRUCTURED_OUTPUT_TOOL_NAME
from app.providers.studio import (
    ProfileUpstream, profile_identity,
    DEEPSEEK_ADAPTER_VERSION, DEEPSEEK_PAIRED_ADAPTER_VERSION,
)
from tests.test_model_settings import CANARY, KEY, configure, envelope


def test_official_presets_and_endpoint_restrictions():
    presets = [p for p in model_presets() if p['provider'] == 'deepseek']
    assert [p['model'] for p in presets] == list(DEEPSEEK_MODELS)
    assert all(p['base_url'] == DEEPSEEK_BASE_URL for p in presets)
    assert CANARY not in json.dumps(presets)
    for url, ref in [('https://other.example.com', 'cred-' + '1' * 32), (DEEPSEEK_BASE_URL, None), (DEEPSEEK_BASE_URL, 'env-nvidia')]:
        with pytest.raises(ValueError, match='DeepSeek'):
            ConnectionInput(name='DeepSeek', provider='deepseek', base_url=url, credential_ref=ref)


@pytest.mark.parametrize('model', DEEPSEEK_MODELS)
@pytest.mark.parametrize('effort,expected', [(None, 'high'), ('low', 'low'), ('medium', 'high'), ('high', 'high')])
def test_thinking_requests_remain_bounded_and_do_not_force_tools(tmp_path, model, effort, expected):
    store = GatewaySecretStore(str(tmp_path / 'private'))
    grant = store.grant('deepseek-test')
    ref = store.save('deepseek-test', envelope(store, grant))['credential_ref']
    db = Database(f"sqlite:///{tmp_path / 'models.db'}")
    db.create_schema()
    with db.session() as session:
        row = configure(session, provider='deepseek', model=model, credential_ref=ref)
        profile = ModelSettingsService(session).profile(row.id)
    profile['reasoning_effort'] = effort
    captured = {}

    class Transport:
        async def request(self, base_url, suffix, **kwargs):
            assert kwargs['credential'] == CANARY
            captured.update(url=base_url, suffix=suffix, payload=kwargs['payload'])
            return {'choices': []}

    upstream = ProfileUpstream(profile, store, transport=Transport())
    payload = {'model': model, 'max_tokens': 700, 'messages': [{'role': 'user', 'content': 'Return JSON.'}], 'tools': [{'type': 'function', 'function': {'name': STRUCTURED_OUTPUT_TOOL_NAME}}], 'tool_choice': {'type': 'function', 'function': {'name': STRUCTURED_OUTPUT_TOOL_NAME}}, 'temperature': .8, '_contribos_profile': profile}
    asyncio.run(upstream.create_response(payload))
    request = captured['payload']
    assert captured['url'] == DEEPSEEK_BASE_URL and captured['suffix'] == '/chat/completions'
    assert request['max_tokens'] == 700 and request['thinking'] == {'type': 'enabled'}
    assert request['reasoning_effort'] == expected and request['tool_choice'] == 'auto'
    assert request['tools'] == payload['tools'] and request['messages'] == payload['messages']
    assert 'temperature' not in request and '_contribos_profile' not in request
    assert CANARY not in json.dumps(request)
    assert profile_identity(profile).adapter_version == DEEPSEEK_ADAPTER_VERSION
    assert profile_identity(profile, paired_analysis=True).adapter_version == DEEPSEEK_PAIRED_ADAPTER_VERSION
    db.close()


def test_api_profiles_support_all_stages_without_changing_existing_defaults(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.database.session() as session:
            old = configure(session)
            conn = ModelSettingsService(session).append('connection:deepseek', ConnectionInput(name='DeepSeek', provider='deepseek', base_url=DEEPSEEK_BASE_URL, credential_ref='cred-' + '2' * 32).model_dump(), None)
        for model in DEEPSEEK_MODELS:
            values = {'name': model, 'connection_id': conn.id, 'model': model, 'reasoning_effort': 'high'}
            for wrong in ({'structured_output': 'json_schema'}, {'temperature': .5}, {'model': 'deepseek-chat'}):
                assert client.post('/api/v1/model-profiles', json={'scope_id': 'invalid', 'profile': {**values, **wrong}}).status_code == 422
            saved = client.post('/api/v1/model-profiles', json={'scope_id': model, 'profile': values})
            assert saved.status_code == 200, saved.text
        state = client.get('/api/v1/model-settings').json()
        assert set(state['effective'].values()) == {old.id}
        result = client.put('/api/v1/model-settings', json={'expected_hash': state['version']['record_hash'], 'settings': {'default': saved.json()['id']}})
        assert result.status_code == 200, result.text
        assert set(client.get('/api/v1/model-settings').json()['effective'].values()) == {saved.json()['id']}
        meta = client.get('/api/v1/meta').json()
        assert all(meta[k] == 'deepseek' for k in ('analysis_provider', 'implementation_provider', 'review_provider'))


def test_execution_and_review_worker_identities_match_frozen_deepseek_binding(tmp_path):
    from types import SimpleNamespace
    from app.jobs import JobService
    db = Database(f"sqlite:///{tmp_path / 'bound.db'}")
    db.create_schema()
    settings = Settings(model_gateway_signing_key=KEY)
    with db.session() as session:
        row = configure(session, provider='deepseek', model=DEEPSEEK_MODELS[0])
        service = ModelSettingsService(session)
        service.task_profiles('test-task', bind=True)
        session.commit()
        expected = task_identity(session, settings, 'test-task', 'review')
        assert expected.adapter_version == DEEPSEEK_ADAPTER_VERSION
        job, _ = JobService(session).enqueue(kind='provider_review', payload={'task_id': 'test-task', 'expected_provider': expected.hash_payload()}, idempotency_key='bound-deepseek-review')
    worker = SimpleNamespace(database=db, runner=object(), identity=object())
    configure_bound_worker(worker, job.id, settings)
    assert worker.identity == expected
    assert worker.runner.profile['id'] == row.id
    db.close()


@pytest.mark.parametrize('finish', ['stop', 'tool_calls', 'length', 'content_filter', 'insufficient_system_resource', 'aborted', None])
def test_final_status_usage_and_private_reasoning(finish):
    data = {'choices': [{'finish_reason': finish, 'message': {'content': None, 'reasoning_content': 'PRIVATE_REASONING_SENTINEL', 'tool_calls': [{'type': 'function', 'function': {'name': STRUCTURED_OUTPUT_TOOL_NAME, 'arguments': '{"ok":true}'}}]}}], 'usage': {'prompt_tokens': 200, 'completion_tokens': 80, 'prompt_cache_hit_tokens': 150}}
    if finish in {'stop', 'tool_calls'}:
        content, usage = _completion(data, error_prefix='deepseek', provider_label='DeepSeek')
        assert json.loads(content) == {'ok': True}
        assert usage == {'input_tokens': 200, 'output_tokens': 80, 'cached_input_tokens': 150}
        assert 'PRIVATE_REASONING_SENTINEL' not in content
    else:
        with pytest.raises(ProviderRunError) as caught:
            _completion(data, error_prefix='deepseek', provider_label='DeepSeek')
        assert caught.value.usage.input_tokens == 200 and caught.value.usage.output_tokens == 80
    data.pop('usage')
    with pytest.raises(ProviderRunError) as caught:
        _completion(data, error_prefix='deepseek', provider_label='DeepSeek')
    assert caught.value.usage is None


def test_migration_from_0039_retains_review_rows_and_can_restore_backup(tmp_path, monkeypatch):
    from app.migrations import versions
    from tests.test_workbench import automatic_fixture, populate_round, drive_round
    from app.models import ReviewRun

    with monkeypatch.context() as patch:
        patch.setattr(versions, 'MIGRATIONS', tuple(m for m in MIGRATIONS if m.revision <= '0039_environment_contracts'))
        db, settings, tid, workers, code, review = automatic_fixture(tmp_path)
        populate_round(code, review)
        drive_round(db, settings, tid, workers)
        assert db.current_revision() == '0039_environment_contracts'
    backup = tmp_path / 'before-0040.db'
    with db.engine.connect() as c:
        raw = c.connection.driver_connection
        before = list(raw.execute('SELECT * FROM review_runs'))
        assert len(before) == 1
        target = sqlite3.connect(backup)
        raw.backup(target)
        target.close()
    report = db.create_schema()
    assert report.applied == ('0040_deepseek_reviews',)
    with db.engine.connect() as c:
        assert list(c.connection.driver_connection.execute('SELECT * FROM review_runs')) == before
        assert c.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        trigger = c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='review_runs_provenance_insert'").scalar_one()
        assert "'deepseek'" in trigger and 'invocation.provider_name = NEW.reviewer_kind' in trigger
        assert 'diff.artifact_id = NEW.diff_hash' in trigger
    with db.session() as session:
        row = session.query(ReviewRun).one()
        with pytest.raises(IntegrityError):
            session.execute(text('UPDATE review_runs SET reason_code=reason_code WHERE id=:id'), {'id': row.id})
    with sqlite3.connect(backup) as restored:
        assert list(restored.execute('SELECT * FROM review_runs')) == before
        assert restored.execute('SELECT MAX(revision) FROM _schema_migrations').fetchone()[0] == '0039_environment_contracts'
        assert restored.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()
