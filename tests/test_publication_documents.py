from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.jobs import JobService
from app.models import Job, PublishIntent
from app.provenance import content_hash
from app.publication import request_publication, confirm_publication
from app.publication_documents import PublicationDocumentWorker
from app.publication_requirements import read_requirements, validate_document, requirements_hash, title_rules
from app.workbench import Workbench, WorkbenchError
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_publication_workbench import FakeTransport, ready

WORKFLOW = '''name: Semantic PR
jobs:
  main:
    steps:
      - uses: amannn/action-semantic-pull-request@pinned
        with:
          types: |
            fix
            docs
          scopes: |
            openai
            core
          requireScope: true
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
'''
TEMPLATE = '## Summary\n\n## Testing\n\n- [ ] I have checked the contribution guidelines.\n'
BODY = '## Summary\nFix streamed output accumulation.\n\n## Testing\nSandbox verification passed.\n\n- [ ] I have checked the contribution guidelines.\n'


def rules():
    transport = FakeTransport('fixture/repo')
    transport.documents = {'.github/workflows/pr.yml': WORKFLOW,
                           '.github/PULL_REQUEST_TEMPLATE.md': TEMPLATE,
                           'CONTRIBUTING.md': 'Use English and a scoped semantic title.\n'}
    result = asyncio.run(read_requirements(transport, transport.repository, transport.base, ['src/accumulator.py']))
    return transport, result


def test_reads_pinned_rules_template_and_package_guide_without_writes():
    transport, requirements = rules()
    assert requirements['hash'] == requirements_hash(requirements)
    assert requirements['title_rules'] == {'conventional': True, 'types':['fix','docs'],
        'scopes':['openai','core'], 'require_scope':True, 'manual':[]}
    assert all(method == 'GET' for method, _, _ in transport.calls)
    assert all(source['commit'] == 'a'*40 for source in requirements['sources'])
    assert any('/src/CONTRIBUTING.md?' in path for _,path,_ in transport.calls)
    validate_document('fix(openai): accumulate streamed content', BODY, requirements)
    advanced = json.loads(json.dumps(requirements))
    for source in advanced['sources']:
        source['commit'] = 'b'*40
    assert requirements_hash(advanced) == requirements['hash']
    advanced['sources'][0]['blob_sha'] = 'c'*40
    assert requirements_hash(advanced) != requirements['hash']


@pytest.mark.parametrize("file_count", [7, 16])
def test_complete_workflow_evidence_has_bounded_aggregate_reading(file_count):
    from app.publication_requirements import MAX_REQUIREMENTS_BYTES
    transport = FakeTransport('fixture/repo')
    transport.documents = {f'.github/workflows/ci-{i:02}.yml': '# bounded fixture\n' + ' ' * 44_000
                           for i in range(file_count)}
    # The actual title rule may be in the last workflow, never discard a tail
    # to fit the old aggregate limit or silently claim conventions are complete.
    transport.documents[f'.github/workflows/ci-{file_count-1:02}.yml'] += WORKFLOW
    if file_count == 16:
        with pytest.raises(WorkbenchError) as failure:
            asyncio.run(read_requirements(transport, transport.repository, transport.base, []))
        assert failure.value.code == 'publication_requirements_budget'
    else:
        value = asyncio.run(read_requirements(transport, transport.repository, transport.base, []))
        assert 256_000 < sum(len(s['content'].encode()) for s in value['sources']) <= MAX_REQUIREMENTS_BYTES
        assert len(value['sources']) == file_count
        assert value['title_rules']['require_scope']
        assert value['version'] == 'repository-publication-requirements-v2'
        old = {**value, 'version': 'repository-publication-requirements-v1'}
        assert requirements_hash(old) != value['hash']
    assert all(method == 'GET' for method, _, _ in transport.calls)


@pytest.mark.parametrize('size', [65_000, 256_001])
def test_workflow_file_reading_matches_repository_file_budget(size):
    transport = FakeTransport('fixture/repo')
    transport.documents = {'.github/workflows/pr.yml': WORKFLOW + ' ' * (size-len(WORKFLOW.encode()))}
    if size > 256_000:
        with pytest.raises(WorkbenchError) as failure:
            asyncio.run(read_requirements(transport, transport.repository, transport.base, []))
        assert failure.value.code == 'publication_requirements_budget'
    else:
        value = asyncio.run(read_requirements(transport, transport.repository, transport.base, []))
        assert len(value['sources'][0]['content'].encode()) == size
        assert value['title_rules']['require_scope']


@pytest.mark.parametrize(('title','body','code'), [
    ('修复流式内容', BODY, 'publication_document_language'),
    ('fix(openai): accumulate streamed content', BODY+'\n测试通过', 'publication_document_language'),
    ('Improve streaming', BODY, 'publication_document_title'),
    ('feat(openai): add streaming', BODY, 'publication_document_title'),
    ('fix(unknown): improve streaming', BODY, 'publication_document_title'),
    ('fix: improve streaming', BODY, 'publication_document_title'),
    ('fix(openai): improve streaming', '## Summary\nA fix.', 'publication_document_template'),
    ('fix(openai): improve streaming', BODY+'\nTODO fill details', 'publication_document_incomplete'),
])
def test_invalid_public_document_fails_closed(title, body, code):
    _, requirements = rules()
    with pytest.raises(WorkbenchError) as error:
        validate_document(title, body, requirements)
    assert error.value.code == code


def test_custom_workflow_rules_are_explicit_manual_checks_not_executed():
    sources = [{'role':'workflow','path':'.github/workflows/pr.yml',
                'content':WORKFLOW.replace('requireScope: true', 'subjectPattern: "^(a+)+$"')}]
    result = title_rules(sources)
    assert result['conventional']
    assert result['manual'] == ['.github/workflows/pr.yml: custom validation']
    assert not title_rules([{'role':'workflow','path':'ci.yml','content':'name: Semantic PR'}])['conventional']


def test_template_choice_and_literal_multilingual_evidence():
    _, requirements = rules()
    requirements['sources'].append({**requirements['sources'][1], 'role':'template', 'path':'other.md', 'content':TEMPLATE})
    with pytest.raises(WorkbenchError, match='选择'):
        validate_document('fix(openai): accumulate content', BODY, requirements)
    validate_document('fix(openai): accumulate content', BODY+'\nFixture: `中文`', requirements,
                      '.github/PULL_REQUEST_TEMPLATE.md')


@pytest.mark.parametrize('damage', ['hash','symlink','truncated','canary'])
def test_untrusted_requirement_files_are_verified_bounded_and_redacted(damage):
    class Transport(FakeTransport):
        async def api(self, method, path, body=None, *, missing=False):
            value = await super().api(method, path, body, missing=missing)
            if '/contents/CONTRIBUTING.md?' in path:
                if damage == 'hash': value['sha'] = 'f'*40
                if damage == 'symlink': value['type'] = 'symlink'
                if damage == 'truncated': value['size'] = 256001
            return value
    transport = Transport('fixture/repo')
    canary = 'ghp_' + 'sensitiveCanary12345678901234567890'
    transport.documents['CONTRIBUTING.md'] = canary if damage == 'canary' else 'English contribution guide.'
    if damage == 'canary':
        value = asyncio.run(read_requirements(transport, transport.repository, transport.base, []))
        assert canary not in json.dumps(value)
        assert value['sources'][0]['redacted']
    else:
        with pytest.raises(WorkbenchError):
            asyncio.run(read_requirements(transport, transport.repository, transport.base, []))


def pipeline(tmp_path, output=None):
    db, settings, task_id, transport, publisher = ready(tmp_path)
    transport.documents = rules()[0].documents
    with db.session() as session:
        for job in session.scalars(select(Job).where(Job.kind == 'publication_prepare')):
            JobService(session).request_cancel(job.id)
        job = request_publication(Workbench(session, settings.artifact_root), task_id,
                                  title='', body='', document_only=True, key='english')
        assert request_publication(Workbench(session, settings.artifact_root), task_id,
                                  title='', body='', document_only=True, key='english').id == job.id
    assert asyncio.run(publisher.run_once()).state == 'succeeded'
    doc = output or {'title':'fix(openai): accumulate streamed content','body':BODY,
                     'template_path':'.github/PULL_REQUEST_TEMPLATE.md','notes':[]}
    runner = ScriptedRunner(json.dumps(doc))
    provider = PublicationDocumentWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='publication-writer')
    return db, settings, task_id, transport, publisher, provider, runner


def test_english_document_vertical_slice_has_no_write_then_exact_confirmation(tmp_path):
    db, settings, task_id, transport, publisher, provider, runner = pipeline(tmp_path)
    assert asyncio.run(provider.run_once()).state == 'succeeded'
    assert all(method == 'GET' for method, _, _ in transport.calls)
    assert 'untrusted DATA' in runner.invocations[0].prompt
    assert 'GitHub CI' in runner.invocations[0].prompt
    assert not hasattr(provider, 'transport')
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        document = wb.latest(task_id, 'publication_document_ready')
        assert document.payload['requirements_hash'] == document.payload['requirements']['hash']
        assert wb.latest(task_id, 'publication_confirmed') is None
        assert session.scalar(select(PublishIntent)) is None
        request_publication(wb, task_id, title=document.payload['title'], body=document.payload['body'],
            document_hash=document.record_hash, template_path=document.payload['template_path'], key='english-preview')
    assert asyncio.run(publisher.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        preview = wb.latest(task_id, 'publication_prepared')
        assert preview.payload['document_hash'] == document.record_hash
        assert preview.payload['commit_metadata']['message'].startswith('fix(openai):')
        confirm_publication(wb, task_id, intent_hash=preview.record_hash, nonce=preview.payload['nonce'])
    assert asyncio.run(publisher.run_once()).state == 'succeeded'
    assert len(transport.prs) == 1
    assert transport.prs[0]['title'] == document.payload['title']


def test_generated_checkboxes_are_not_signed_and_chinese_output_is_rejected(tmp_path):
    db, settings, task_id, _, _, provider, _ = pipeline(tmp_path,
        {'title':'fix(openai): accumulate content','body':BODY.replace('[ ]','[x]'),
         'template_path':'.github/PULL_REQUEST_TEMPLATE.md'})
    assert asyncio.run(provider.run_once()).state == 'succeeded'
    with db.session() as session:
        body = Workbench(session, settings.artifact_root).latest(task_id,'publication_document_ready').payload['body']
        assert '[x]' not in body and '[ ]' in body
    db.close()


def test_bad_model_output_cannot_become_a_publishable_document(tmp_path):
    db, settings, task_id, transport, _, provider, _ = pipeline(tmp_path,
        {'title':'修复内容','body':BODY,'template_path':'.github/PULL_REQUEST_TEMPLATE.md'})
    job = asyncio.run(provider.run_once())
    assert job.state == 'failed' and job.error_code == 'publication_document_language'
    with db.session() as session:
        assert Workbench(session, settings.artifact_root).latest(task_id,'publication_document_ready') is None
    assert all(method == 'GET' for method, _, _ in transport.calls)


def test_changed_guidelines_invalidate_draft_and_produce_zero_writes(tmp_path):
    db, settings, task_id, transport, publisher, provider, _ = pipeline(tmp_path)
    assert asyncio.run(provider.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        doc = wb.latest(task_id,'publication_document_ready')
        request_publication(wb, task_id, title=doc.payload['title'], body=doc.payload['body'],
            document_hash=doc.record_hash, template_path=doc.payload['template_path'], key='stale-preview')
    transport.documents['CONTRIBUTING.md'] += '\nNew required format.'
    job = asyncio.run(publisher.run_once())
    assert job.state == 'failed' and job.error_code == 'publication_document_stale'
    assert all(method == 'GET' for method, _, _ in transport.calls)
    with db.session() as session:
        assert not Workbench(session, settings.artifact_root).latest(task_id,'publication_prepared')


def test_api_document_generation_requires_access_and_returns_persistent_job(tmp_path):
    db, settings, task_id, transport, publisher, provider, _ = pipeline(tmp_path)
    assert asyncio.run(provider.run_once()).state == 'succeeded'
    settings = replace(settings, local_access_token='local-test-access', implementation_provider='nvidia_nim')
    with TestClient(create_app(settings)) as client:
        route = f'/api/v1/tasks/{task_id}/workbench'
        assert client.post(route+'/publication/document', headers={'Idempotency-Key':'api-doc'}).status_code == 401
        headers = {'Idempotency-Key':'api-doc','X-ContribOS-Token':'local-test-access'}
        assert client.post(route+'/publication/document', headers={**headers,'Origin':'http://testserver'}).status_code == 403
        response = client.post(route+'/publication/document', headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()['kind'] == 'publication_requirements'
        assert response.json()['state'] == 'queued'
        assert client.post(route+'/publication/document',headers=headers).json()['id'] == response.json()['id']
        events = client.get(route).json()['events']
        assert any(event['kind'] == 'publication_document_ready' for event in events)


@pytest.mark.parametrize('claim', ['Signed-off-by: Someone <someone@example.com>', 'I hereby sign the CLA.', 'GitHub CI passed.'])
def test_generation_cannot_fabricate_legal_signoff_or_remote_ci(tmp_path, claim):
    db, settings, task_id, transport, _, provider, _ = pipeline(tmp_path,
        {'title':'fix(openai): accumulate content','body':BODY+'\n'+claim,
         'template_path':'.github/PULL_REQUEST_TEMPLATE.md'})
    job = asyncio.run(provider.run_once())
    assert job.state == 'failed' and job.error_code == 'publication_document_claims'
    with db.session() as session:
        assert not Workbench(session, settings.artifact_root).latest(task_id,'publication_document_ready')
    assert all(method == 'GET' for method, _, _ in transport.calls)


def test_guidelines_changing_after_confirmation_prevent_all_writes(tmp_path):
    db, settings, task_id, transport, publisher, provider, _ = pipeline(tmp_path)
    assert asyncio.run(provider.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        doc = wb.latest(task_id,'publication_document_ready')
        request_publication(wb, task_id, title=doc.payload['title'],body=doc.payload['body'],
            document_hash=doc.record_hash,template_path=doc.payload['template_path'],key='confirmed-preview')
    assert asyncio.run(publisher.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        preview = wb.latest(task_id,'publication_prepared')
        confirm_publication(wb, task_id, intent_hash=preview.record_hash,nonce=preview.payload['nonce'])
    transport.documents['CONTRIBUTING.md'] += '\nNew requirements.'
    job = asyncio.run(publisher.run_once())
    assert job.state == 'failed' and job.error_code == 'publication_requirements_stale'
    assert all(method == 'GET' for method, _, _ in transport.calls)


def test_cancelled_generation_produces_no_document_or_publication(tmp_path):
    db, settings, task_id, transport, _, provider, runner = pipeline(tmp_path)
    with db.session() as session:
        job = session.scalar(select(Job).where(Job.kind=='publication_document'))
        JobService(session).request_cancel(job.id)
    assert asyncio.run(provider.run_once()) is None
    assert runner.invocations == []
    with db.session() as session:
        assert not Workbench(session,settings.artifact_root).latest(task_id,'publication_document_ready')
    assert all(method == 'GET' for method, _, _ in transport.calls)


def test_organization_default_community_files_are_pinned_and_used():
    import base64, hashlib
    class OrganizationTransport(FakeTransport):
        async def api(self, method, path, body=None, *, missing=False):
            self.calls.append((method,path,body))
            if path == 'repos/fixture/.github':
                return {'default_branch':'main'}
            if path == 'repos/fixture/.github/commits/main':
                return {'sha':'b'*40}
            if path.startswith('repos/fixture/.github/contents/'):
                filename=path.split('/contents/',1)[1].split('?',1)[0]
                text={'CONTRIBUTING.md':'Use an English PR description.',
                      '.github/PULL_REQUEST_TEMPLATE.md':TEMPLATE}.get(filename)
                if text:
                    raw=text.encode()
                    return {'type':'file','path':filename,'encoding':'base64','size':len(raw),
                            'sha':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),
                            'content':base64.b64encode(raw).decode()}
                return None
            if '/contents/' in path:
                return None
            raise AssertionError(path)
    transport=OrganizationTransport('fixture/repo')
    value=asyncio.run(read_requirements(transport,'fixture/repo','a'*40,[]))
    assert len(value['sources'])==2
    assert all(source['repository']=='fixture/.github' and source['commit']=='b'*40 for source in value['sources'])
    validate_document('Improve streaming',BODY,value)
    assert all(method=='GET' for method,_,_ in transport.calls)


def test_changed_plan_cannot_receive_late_generated_document(tmp_path):
    from app.task_states import ContributionTaskStateService
    db, settings, task_id, transport, _, provider, runner = pipeline(tmp_path)
    with db.session() as session:
        states=ContributionTaskStateService(session)
        current=states.current(task_id)
        states.transition(task_id,to_state='planning',reason_code='user_replan',
            expected_sequence=current.sequence,expected_record_hash=current.record_hash)
    job=asyncio.run(provider.run_once())
    assert job.state=='failed'
    assert runner.invocations==[]
    with db.session() as session:
        assert not Workbench(session,settings.artifact_root).latest(task_id,'publication_document_ready')
    assert all(method=='GET' for method,_,_ in transport.calls)


def test_real_style_issue_first_line_and_manual_repo_requirements():
    transport = FakeTransport('fixture/repo')
    transport.documents['CONTRIBUTING'] = '''The first line of the description should contain a reference to the issue.
Please wait for explicit confirmation from a maintainer before starting work.
If the change affects emitted spans, include a screenshot showing the updated trace output.
'''
    result = asyncio.run(read_requirements(transport,'fixture/repo','a'*40,[]))
    assert result['body_rules']['issue_first_line']
    assert {c['code'] for c in result['repository_checks']} == {'issue_first_line','maintainer_confirmation','span_screenshot'}
    for body in ['## Summary\nFixes #3785', 'Fixes #1\n\n## Summary\nA focused fix.']:
        with pytest.raises(WorkbenchError) as error:
            validate_document('fix(openai): accumulate content',body,result,issue_number=3785)
        assert error.value.code=='publication_document_issue'
    validate_document('fix(openai): accumulate content','Fixes #3785\n\n## Summary\nA focused fix.',result,issue_number=3785)
