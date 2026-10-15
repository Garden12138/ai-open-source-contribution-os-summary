"""Human-consented CLA Assistant signatures through isolated Publisher only.

No model participates. The only write is the fixed signature comment on an
already published, verified Draft PR. Append-only intent/consent/outbox receipts
keep uncertain writes in reconciliation rather than retrying the POST.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from typing import Protocol
from urllib.parse import quote, urlsplit

from sqlalchemy import select

from app.audit import AuditService
from app.jobs import JobService
from app.models import DraftPullRequest, Job
from app.provenance import content_hash
from app.publisher_accounts import require_account
from app.publish_intents import PublishIntentService, draft_pull_request_payload
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench, WorkbenchError
from app.workbench_worker import WorkbenchJobWorker

SIGNATURE = 'I have read the CLA Document and I hereby sign the CLA'
ACTION = 'submit_cla_signature_comment'
PREPARE, WRITE, CHECK = 'cla_prepare', 'cla_write', 'cla_check'
POLICY = 'cla-assistant-signature-v1'
SUPPORTED_ACTION = '5bfe123a0731f017d9c29550976bf724fe0870f5'


class CLATransport(Protocol):
    async def api(self, method: str, path: str, body: dict | None = None, *, missing: bool = False): ...
    async def cla_comment(self, repository: str, number: int, *, authorization: dict): ...


def _lock(wb: Workbench) -> None:
    connection = wb.session.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def published_target(wb: Workbench, task_id: str) -> dict:
    completed = wb.latest(task_id, 'publication_completed')
    if not completed:
        raise WorkbenchError('CLA 签署只支持本任务已发布的 Draft PR', code='cla_no_published_pr')
    draft = wb.session.get(DraftPullRequest, completed.payload['draft_pull_request_id'])
    if not draft or draft.task_id != task_id or draft.provider != 'github':
        raise WorkbenchError('Draft PR 来源无法确认', code='cla_provenance_invalid')
    fields = draft_pull_request_payload(**{k: getattr(draft, k) for k in (
        'publish_intent_id', 'task_id', 'provider', 'number', 'html_url', 'head_branch',
        'base_commit_sha', 'head_commit_sha', 'diff_hash', 'review_record_hash')})
    intent = PublishIntentService(wb.session).get_verified(draft.publish_intent_id)
    if (content_hash(fields) != draft.record_hash or completed.payload['publish_intent_id'] != intent.id
        or intent.task_id != task_id or draft.head_commit_sha != intent.head_commit_sha
        or draft.diff_hash != intent.diff_hash or draft.review_record_hash != intent.review_record_hash
        or draft.base_commit_sha != intent.base_commit_sha or draft.head_branch != intent.head_branch
        or draft.html_url != f'https://github.com/{intent.upstream_repository}/pull/{draft.number}'):
        raise WorkbenchError('发布来源校验失败', code='cla_provenance_invalid')
    return {'repository': intent.upstream_repository, 'number': draft.number, 'pr_url': draft.html_url,
            'head_sha': draft.head_commit_sha, 'head_branch': draft.head_branch,
            'base_sha': draft.base_commit_sha, 'diff_hash': intent.diff_hash,
            'tests_hash': intent.test_results_hash, 'review_hash': intent.review_record_hash,
            'publication_hash': completed.record_hash, 'draft_hash': draft.record_hash}


def unresolved_signature(wb: Workbench, task_id: str) -> bool:
    events = wb.history(task_id)
    submitted = {e.payload.get('authorization_hash') for e in events if e.kind == 'cla_submitted'}
    return any(e.kind == 'cla_write_started' and e.payload['authorization_hash'] not in submitted for e in events)


def request_cla(wb: Workbench, task_id: str, *, account_hash: str, key: str, check: bool = False, commit: bool = True) -> Job:
    _lock(wb)
    kind = CHECK if check else PREPARE
    job_key = kind + ':' + key
    old = wb.session.scalar(select(Job).where(Job.kind == kind, Job.idempotency_key == job_key))
    if old:
        if old.payload.get('task_id') != task_id or old.payload.get('publisher_account_hash') != account_hash:
            raise WorkbenchError('重复 CLA 请求的内容不同')
        return old
    require_account(wb.session, account_hash)
    wb.assert_idle(task_id)
    target = published_target(wb, task_id)
    if not check and unresolved_signature(wb, task_id):
        raise WorkbenchError('上次签署结果不明确，请先核对 CLA 结果；不会重复提交评论', code='cla_outcome_unknown')
    if sum(j.kind in {PREPARE, CHECK} for j in wb.jobs(task_id)) >= 64:
        raise WorkbenchError('本任务已达到 CLA 核对预算', code='cla_budget')
    return wb.enqueue(task_id, kind, {'publisher_account_hash': account_hash, 'target': target},
                      key=job_key, actor='local-user', timeout_seconds=300, commit=commit)


def consent_statement(p: dict) -> str:
    return (f"我已阅读并同意上述 CLA 协议（SHA-256：{p['agreement_hash']}），"
            f"授权 GitHub 账号 {p['login']} 向 {p['pr_url']} 提交所展示的完整签署评论。")


def confirm_cla(wb: Workbench, task_id: str, *, preview_hash: str, nonce: str,
                agreement_hash: str, acknowledgement: str, read_and_agree: bool) -> Job:
    _lock(wb)
    from app.conversation import require_settled_messages
    require_settled_messages(wb, task_id)
    wb.assert_idle(task_id)
    preview = wb.latest(task_id, 'cla_prepared')
    if not preview or preview.record_hash != preview_hash:
        raise WorkbenchError('CLA 协议预览已变化，请重新读取', code='cla_preview_stale')
    p = preview.payload
    if (not read_and_agree or acknowledgement != consent_statement(p)
        or agreement_hash != p['agreement_hash'] or not secrets.compare_digest(p['nonce'], nonce)):
        raise WorkbenchError('请明确阅读并同意当前协议，并确认完整签署授权', code='cla_consent_required')
    if datetime.now(timezone.utc) >= datetime.fromisoformat(p['expires_at']):
        raise WorkbenchError('CLA 预览已过期，请重新读取协议', code='cla_preview_expired')
    if any(e.kind == 'cla_confirmed' and e.payload['preview_hash'] == preview_hash for e in wb.history(task_id)):
        raise WorkbenchError('此 CLA 授权已使用，请核对签署结果', code='cla_consent_consumed')
    if p['status'] == 'passed' or p.get('existing_comment'):
        raise WorkbenchError('已发现签署记录或 CLA 已通过，请核对结果', code='cla_already_submitted')
    require_account(wb.session, p['publisher_account_hash'])
    if published_target(wb, task_id) != p['target'] or unresolved_signature(wb, task_id):
        raise WorkbenchError('PR 或签署状态已变化，请重新核对', code='cla_preview_stale')
    if hashlib.sha256(wb.artifacts.read_bytes(p['agreement_artifact'])).hexdigest() != p['agreement_hash']:
        raise WorkbenchError('CLA 协议产物校验失败', code='cla_agreement_invalid')
    job, _ = JobService(wb.session).enqueue(kind=WRITE, idempotency_key='cla-sign:' + preview.id,
        payload={'task_id': task_id, 'publisher_account_hash': p['publisher_account_hash'],
                 'preview_hash': preview_hash}, max_attempts=3, timeout_seconds=300, commit=False)
    event = wb.append(task_id, 'cla_confirmed', {'preview_hash': preview_hash, 'agreement_hash': agreement_hash,
        'acknowledgement': acknowledgement, 'action': ACTION}, key='cla-consent:' + preview.id,
        actor='local-user', job_id=job.id, commit=False)
    AuditService(wb.session).prepare(event_type='cla.authorized', actor_type='local_user', actor_id='local-user',
        correlation_id=job.id, payload={'consent_hash': event.record_hash, 'preview_hash': preview_hash,
                                      'agreement_hash': agreement_hash, 'action': ACTION})
    wb.session.commit()
    return job


def cla_view(wb: Workbench, task_id: str) -> dict:
    preview = wb.latest(task_id, 'cla_prepared')
    confirmed = wb.latest(task_id, 'cla_confirmed')
    submitted = wb.latest(task_id, 'cla_submitted')
    observed = wb.latest(task_id, 'cla_status_observed')
    result = {'preview': None, 'confirmed': confirmed.payload if confirmed else None,
              'submitted': submitted.payload if submitted else None, 'status': observed.payload if observed else None,
              'unresolved': unresolved_signature(wb, task_id)}
    if preview:
        p = preview.payload
        raw = wb.artifacts.read_bytes(p['agreement_artifact'])
        if hashlib.sha256(raw).hexdigest() != p['agreement_hash']:
            raise WorkbenchError('CLA 协议产物校验失败', code='cla_agreement_invalid')
        consumed = bool(confirmed and confirmed.payload['preview_hash'] == preview.record_hash)
        result['preview'] = {**p, 'record_hash': preview.record_hash, 'agreement_text': raw.decode(),
            'acknowledgement': consent_statement(p), 'consumed': consumed}
    return result


async def read_file(transport: CLATransport, repository: str, path: str, ref: str, *, missing: bool = False) -> tuple[str, str] | None:
    value = await transport.api('GET', f'repos/{repository}/contents/{quote(path, safe="/")}?ref={quote(ref, safe="")}', missing=missing)
    if value is None and missing:
        return None
    if (not isinstance(value, dict) or value.get('type') != 'file' or value.get('path') != path
        or value.get('encoding') != 'base64' or type(value.get('size')) is not int
        or not 0 < value['size'] <= 100000):
        raise WorkbenchError('CLA 文件类型或大小不支持，请到 GitHub 阅读', code='cla_agreement_invalid')
    try:
        raw = base64.b64decode(''.join(value['content'].split()), validate=True)
        text = raw.decode('utf-8')
    except (KeyError, ValueError, UnicodeError, AttributeError):
        raise WorkbenchError('CLA 文件读取失败', code='cla_agreement_invalid') from None
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if len(raw) != value['size'] or blob != value.get('sha'):
        raise WorkbenchError('CLA 文件哈希不一致', code='cla_agreement_invalid')
    ensure_no_sensitive_data(text, context='CLA document')
    return text, blob


async def agreement(transport: CLATransport, repository: str) -> dict:
    info = await transport.api('GET', 'repos/' + repository)
    branch = info.get('default_branch') if isinstance(info, dict) else None
    if not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', branch):
        raise WorkbenchError('CLA 默认分支无法核对', code='cla_unsupported')
    commit = await transport.api('GET', f'repos/{repository}/commits/{branch}')
    ref = commit.get('sha', '')
    if not re.fullmatch(r'[a-f0-9]{40}', ref):
        raise WorkbenchError('CLA 提交版本无效', code='cla_agreement_invalid')
    workflows = []
    for path in ('.github/workflows/cla.yaml', '.github/workflows/cla.yml'):
        value = await read_file(transport, repository, path, ref, missing=True)
        if value:
            workflows.append((path, *value))
    supported = []
    for path, text, blob in workflows:
        if (re.search(r'(?m)^\s*(?:-\s*)?uses:\s*contributor-assistant/github-action@' + SUPPORTED_ACTION + r'\b', text)
            and SIGNATURE in text and re.search(r'\bissue_comment\s*:', text)):
            urls = re.findall(r'(?m)^\s+path-to-document:\s*[\'\"]?(https://[^\s\'\"]+)[\'\"]?\s*$', text)
            if len(urls) == 1:
                url = urlsplit(urls[0])
                prefix = '/' + repository + '/blob/' + branch + '/'
                if (url.scheme == 'https' and url.netloc == 'github.com' and not url.query and not url.fragment
                    and url.path.startswith(prefix)):
                    document_path = url.path[len(prefix):]
                    if re.fullmatch(r'[A-Za-z0-9_./-]{1,300}', document_path) and '..' not in document_path.split('/'):
                        supported.append((path, blob, document_path, urls[0]))
    if len(supported) != 1:
        raise WorkbenchError('此仓库 CLA 需要外部页面或不支持的签署方式；请在 GitHub 按提示操作', code='cla_unsupported')
    workflow, workflow_blob, path, url = supported[0]
    text, blob = await read_file(transport, repository, path, ref)
    raw = text.encode()
    return {'agreement_text': text, 'agreement_hash': hashlib.sha256(raw).hexdigest(),
            'agreement_blob': blob, 'agreement_path': path, 'agreement_ref': ref,
            'agreement_url': f'https://github.com/{repository}/blob/{ref}/{path}',
            'canonical_agreement_url': url, 'workflow_path': workflow, 'workflow_blob': workflow_blob,
            'workflow_ref': ref, 'policy': POLICY}


async def existing_signature(transport: CLATransport, target: dict, login: str, user_id: int) -> dict | None:
    for page in range(1, 11):
        values = await transport.api('GET', f"repos/{target['repository']}/issues/{target['number']}/comments?per_page=100&page={page}")
        if not isinstance(values, list) or len(values) > 100:
            raise WorkbenchError('CLA 评论清单不完整', code='cla_remote_invalid')
        for value in values:
            user = value.get('user', {})
            if value.get('body') == SIGNATURE and user.get('login') == login and user.get('id') == user_id:
                return comment_receipt(value, target, login, user_id)
        if len(values) < 100:
            return None
    raise WorkbenchError('CLA 评论核对超过预算，停止签署', code='cla_budget')


def comment_receipt(value: dict, target: dict, login: str, user_id: int) -> dict:
    identifier = value.get('id')
    url = f"{target['pr_url']}#issuecomment-{identifier}"
    if (type(identifier) is not int or identifier < 1 or value.get('html_url') != url
        or value.get('body') != SIGNATURE or value.get('user', {}).get('login') != login
        or value.get('user', {}).get('id') != user_id):
        raise WorkbenchError('CLA 评论结果无法核对，请只读核对后重试', code='cla_outcome_unknown')
    return {'comment_id': identifier, 'comment_url': url}


async def check_status(transport: CLATransport, target: dict) -> dict:
    values = await transport.api('GET', f"repos/{target['repository']}/commits/{target['head_sha']}/check-runs?per_page=100")
    runs = values.get('check_runs') if isinstance(values, dict) else None
    if not isinstance(runs, list) or type(values.get('total_count')) is not int or values['total_count'] != len(runs) or len(runs) > 100:
        raise WorkbenchError('CLA 检查状态不完整，请稍后核对', code='cla_remote_invalid')
    matches = [r for r in runs if r.get('name') in {'CLAAssistant', 'CLA Assistant'} and r.get('app', {}).get('slug') == 'github-actions']
    current = max(matches, key=lambda r: r.get('id', 0)) if matches else None
    status = 'not_run' if current is None else ('pending' if current.get('status') != 'completed'
        else 'passed' if current.get('conclusion') == 'success' else 'failed')
    result = {'status': status, 'head_sha': target['head_sha'], 'observed_at': datetime.now(timezone.utc).isoformat()}
    if current:
        url = current.get('details_url', '')
        prefix = 'https://github.com/' + target['repository'] + '/actions/runs/'
        if isinstance(url, str) and url.startswith(prefix) and re.fullmatch(r'[0-9]+/job/[0-9]+', url[len(prefix):]):
            result['check_url'] = url
    return result


class CLAWorker(WorkbenchJobWorker):
    kinds = (PREPARE, WRITE, CHECK)

    def __init__(self, database, settings, *, worker_id: str, transport: CLATransport):
        super().__init__(database, worker_id=worker_id)
        self.settings, self.transport = settings, transport

    async def target(self, job: Job, *, strict: bool = True) -> tuple[dict, dict]:
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            _lock(wb)
            self.check(session, job)
            target = published_target(wb, job.payload['task_id'])
            account = require_account(session, job.payload['publisher_account_hash'])
            login, user_id = account.payload['login'], account.payload['user_id']
        user = await self.transport.api('GET', 'user')
        pr = await self.transport.api('GET', f"repos/{target['repository']}/pulls/{target['number']}")
        if (user.get('login') != login or user.get('id') != user_id or pr.get('number') != target['number']
            or pr.get('html_url') != target['pr_url']
            or (strict and (pr.get('state') != 'open' or pr.get('draft') is not True or pr.get('merged') is True
                or pr.get('head', {}).get('sha') != target['head_sha'] or pr.get('head', {}).get('ref') != target['head_branch']))
            or pr.get('base', {}).get('repo', {}).get('full_name') != target['repository']
            or pr.get('user', {}).get('login') != login or pr.get('user', {}).get('id') != user_id):
            raise WorkbenchError('PR、提交或发布账号已变化，CLA 签署已停止，请重新核对', code='cla_target_stale')
        return target, {'login': login, 'user_id': user_id}

    async def execute(self, job: Job) -> dict:
        if job.kind == WRITE:
            return await self.sign(job)
        target, user = await self.target(job, strict=job.kind != CHECK)
        if job.payload['target'] != target:
            raise WorkbenchError('CLA 目标已变化', code='cla_target_stale')
        existing = await existing_signature(self.transport, target, **user)
        try:
            observed = await check_status(self.transport, target)
        except WorkbenchError:
            if job.kind != CHECK or existing is None:
                raise
            # A verified comment receipt must survive a separate checks outage.
            observed = {'status':'unavailable', 'head_sha':target['head_sha'],
                        'observed_at':datetime.now(timezone.utc).isoformat()}
        document = await agreement(self.transport, target['repository']) if job.kind == PREPARE else None
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            _lock(wb)
            self.check(session, job)
            require_account(session, job.payload['publisher_account_hash'])
            if published_target(wb, job.payload['task_id']) != target:
                raise WorkbenchError('CLA 目标已变化', code='cla_target_stale')
            if document:
                raw = document.pop('agreement_text').encode()
                artifact = wb.artifacts.store_bytes(raw, media_type='text/plain', commit=False)
                p = {**document, **user, 'target': target, 'repository': target['repository'], 'pr_url': target['pr_url'],
                     'number': target['number'], 'comment': SIGNATURE, 'action': ACTION,
                     'publisher_account_hash': job.payload['publisher_account_hash'], 'agreement_artifact': artifact.id,
                     'nonce': secrets.token_hex(32), 'expires_at': (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(),
                     'status': observed['status'], 'existing_comment': existing}
                wb.append(job.payload['task_id'], 'cla_prepared', p, key='cla-preview:'+job.id, job_id=job.id, commit=False)
            if existing:
                authorization = wb.latest(job.payload['task_id'], 'cla_confirmed')
                authorized_preview = next((e for e in wb.history(job.payload['task_id'])
                    if authorization and e.kind == 'cla_prepared' and e.record_hash == authorization.payload['preview_hash']), None)
                if (authorized_preview and authorized_preview.payload['target'] == target
                    and authorized_preview.payload['publisher_account_hash'] == job.payload['publisher_account_hash']
                    and not any(e.kind=='cla_submitted' and e.payload['authorization_hash']==authorization.record_hash for e in wb.history(job.payload['task_id']))):
                    self.receipt(wb, job, authorization, existing)
            wb.append(job.payload['task_id'], 'cla_status_observed', {**observed, 'existing_comment': existing},
                      key='cla-status:'+job.id, job_id=job.id, commit=False)
            return self.finish(session, job, observed)

    def receipt(self, wb: Workbench, job: Job, authorization, receipt: dict) -> None:
        wb.append(job.payload['task_id'], 'cla_submitted', {**receipt,
            'authorization_hash': authorization.record_hash, 'preview_hash': authorization.payload['preview_hash']},
            key='cla-submitted:'+authorization.id, job_id=job.id, commit=False)
        AuditService(wb.session).prepare(event_type='cla.comment_observed', actor_type='publisher', actor_id=self.worker_id,
            correlation_id=job.id, payload={'authorization_hash':authorization.record_hash, **receipt})

    async def sign(self, job: Job) -> dict:
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            self.check(session, job)
            events = wb.history(job.payload['task_id'])
            preview = next((e for e in events if e.record_hash == job.payload['preview_hash'] and e.kind=='cla_prepared'), None)
            authorization = next((e for e in events if e.kind=='cla_confirmed' and e.job_id==job.id), None)
            if not preview or not authorization or authorization.actor_id != 'local-user' or authorization.payload['preview_hash'] != preview.record_hash:
                raise WorkbenchError('CLA 缺少本次明确授权', code='cla_unauthorized')
            p = preview.payload
            if hashlib.sha256(wb.artifacts.read_bytes(p['agreement_artifact'])).hexdigest() != p['agreement_hash']:
                raise WorkbenchError('CLA 协议产物校验失败', code='cla_agreement_invalid')
            if (p['policy'] != POLICY or p['action'] != ACTION or p['comment'] != SIGNATURE
                or p['publisher_account_hash'] != job.payload['publisher_account_hash']
                or authorization.payload['acknowledgement'] != consent_statement(p)
                or authorization.payload['agreement_hash'] != p['agreement_hash']):
                raise WorkbenchError('CLA 授权内容无效', code='cla_unauthorized')
            prior = next((e for e in events if e.kind=='cla_submitted' and e.payload['authorization_hash']==authorization.record_hash), None)
            if prior:
                return prior.payload
        target, user = await self.target(job)
        if target != p['target'] or user != {'login':p['login'],'user_id':p['user_id']}:
            raise WorkbenchError('CLA 确认目标已变化', code='cla_target_stale')
        existing = await existing_signature(self.transport, target, **user)
        if existing is None:
            with self.database.session() as session:
                wb = Workbench(session, self.settings.artifact_root)
                if any(e.kind=='cla_write_started' and e.payload['authorization_hash']==authorization.record_hash for e in wb.history(job.payload['task_id'])):
                    raise WorkbenchError('上次签署结果不明确，请继续核对；不会重复提交签署评论', code='cla_outcome_unknown')
            if datetime.now(timezone.utc) >= datetime.fromisoformat(p['expires_at']):
                raise WorkbenchError('CLA 授权在提交前已过期，请重新读取协议', code='cla_preview_expired')
            current = await agreement(self.transport, target['repository'])
            if any(current[k] != p[k] for k in ('agreement_hash','agreement_blob','agreement_path','workflow_blob','workflow_path','policy')):
                raise WorkbenchError('CLA 协议或签署方式已变化，请重新阅读并确认', code='cla_agreement_stale')
            await self.target(job)
            with self.database.session() as session:
                wb = Workbench(session, self.settings.artifact_root)
                _lock(wb)
                self.check(session, job)
                require_account(session, p['publisher_account_hash'])
                if wb.latest(job.payload['task_id'],'cla_prepared').record_hash != preview.record_hash:
                    raise WorkbenchError('CLA 预览已变化', code='cla_preview_stale')
                wb.append(job.payload['task_id'],'cla_write_started',{'authorization_hash':authorization.record_hash,
                    'preview_hash':preview.record_hash,'action':ACTION},key='cla-send:'+authorization.id,job_id=job.id)
            value = await self.transport.cla_comment(target['repository'], target['number'], authorization={
                'action':ACTION,'repository':target['repository'],'number':target['number'],'comment':SIGNATURE,
                'actor_id':'local-user','authorization_hash':authorization.record_hash})
            existing = comment_receipt(value, target, **user)
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            _lock(wb)
            self.check(session, job)
            self.receipt(wb, job, authorization, existing)
            return self.finish(session, job, {'submitted':True, **existing})
