"""Conversation-driven, explicitly consented title/body updates to published Draft PRs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import secrets
from typing import Any, Protocol

from sqlalchemy import select

from app.audit import AuditService
from app.cla import _lock, published_target
from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.models import Job, WorkbenchEvent
from app.provenance import content_hash
from app.publisher_accounts import require_account
from app.publication_requirements import read_requirements, requirements_hash, validate_document
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench, WorkbenchError
from app.workbench_worker import WorkbenchJobWorker

READ, DOCUMENT, WRITE, CHECK = 'pr_update_read', 'pr_update_document', 'pr_update_write', 'pr_update_check'
ACTION = 'update_draft_pr_title_body'


class PRUpdateTransport(Protocol):
    async def api(self, method: str, path: str, body: dict | None = None, *, missing: bool = False) -> Any: ...
    async def pr_text(self, repository: str, number: int, *, authorization: dict) -> dict: ...


def is_update_request(text: str) -> bool:
    """Only classify a request to PREPARE; never infer external-write consent."""
    return bool(re.search(r'PR|pull\s*request|标题|正文|文案', text, re.I)
        and re.search(r'更新|修改|改为|改成|重写|翻译|润色|update|rewrite|translate|edit', text, re.I)
        and not re.search(r'^(?:能否|可以|能不能|是否|怎么|如何|can\b|how\b)', text.strip(), re.I))


def unresolved_update(wb: Workbench, task_id: str) -> bool:
    events = wb.history(task_id)
    resolved = {e.payload['authorization_hash'] for e in events if e.kind in {'pr_update_completed','pr_update_rejected'}}
    return any(e.kind == 'pr_update_write_started' and e.payload['authorization_hash'] not in resolved for e in events)


def request_update(wb: Workbench, task_id: str, *, text: str, account_hash: str,
                   key: str, model_profile_id: str | None = None, check: bool = False, commit: bool = True) -> Job:
    _lock(wb)
    ensure_no_sensitive_data(text, context='PR update request')
    kind = CHECK if check else READ
    old = wb.session.scalar(select(Job).where(Job.kind == kind, Job.idempotency_key == kind+':'+key))
    if old:
        if (old.payload.get('task_id') != task_id or old.payload.get('text') != text
            or old.payload.get('publisher_account_hash') != account_hash):
            raise WorkbenchError('重复更新请求的内容不同', code='pr_update_conflict')
        return old
    require_account(wb.session, account_hash)
    wb.assert_idle(task_id)
    target = published_target(wb, task_id)
    from app.pr_lifecycle import unresolved_ready
    if (unresolved_update(wb, task_id) or unresolved_ready(wb, task_id)) and not check:
        raise WorkbenchError('上次 PR 更新结果尚未明确，请先核对更新结果', code='pr_update_outcome_unknown')
    if sum(j.kind == kind for j in wb.jobs(task_id)) >= (64 if check else 16):
        raise WorkbenchError('本任务的 PR 文案更新预算已用完', code='pr_update_budget')
    if check and not wb.latest(task_id, 'pr_update_confirmed'):
        raise WorkbenchError('尚无已确认的 PR 文案更新，请先生成预览', code='pr_update_unauthorized')
    if not check:
        from app.model_settings import ModelSettingsService
        models = ModelSettingsService(wb.session)
        model_profile_id = model_profile_id or models.task_profiles(task_id, bind=True).get('planning')
        if model_profile_id:
            models.get(model_profile_id, 'profile')
    job = wb.enqueue(task_id, kind, {'text':text,'target':target,'publisher_account_hash':account_hash,
        'document_model_profile_id':model_profile_id}, key=kind+':'+key, actor='local-user', commit=False)
    if not check:
        wb.append(task_id,'pr_update_requested',{'text':text},key='pr-update-message:'+key,
                  actor='local-user',job_id=job.id,commit=False)
    if commit:
        wb.session.commit()
    return job


def acknowledgement(p: dict) -> str:
    return f"确认将 {p['target']['pr_url']} 的标题和正文更新为上述预览，仅更新 PR 文案。"


def update_view(wb: Workbench, task_id: str) -> dict:
    event = wb.latest(task_id,'pr_update_prepared')
    confirmed = wb.latest(task_id,'pr_update_confirmed')
    completed = wb.latest(task_id,'pr_update_completed')
    observed = wb.latest(task_id,'pr_update_observed')
    preview = None
    if event:
        preview = {**event.payload,'record_hash':event.record_hash,
            'acknowledgement':acknowledgement(event.payload),
            'consumed':any(e.kind=='pr_update_confirmed' and e.payload['preview_hash']==event.record_hash for e in wb.history(task_id))}
    return {'preview':preview,'confirmed':confirmed.payload if confirmed else None,
        'completed':completed.payload if completed else None,'observed':observed.payload if observed else None,
        'unresolved':unresolved_update(wb,task_id)}


def confirm_update(wb: Workbench, task_id: str, *, preview_hash: str, nonce: str,
                   text: str, approved: bool) -> Job:
    _lock(wb)
    from app.conversation import require_settled_messages
    require_settled_messages(wb, task_id)
    wb.assert_idle(task_id)
    event = wb.latest(task_id,'pr_update_prepared')
    if not event or event.record_hash != preview_hash:
        raise WorkbenchError('PR 更新预览已变化，请重新生成', code='pr_update_stale')
    p = event.payload
    if not approved or text != acknowledgement(p) or not secrets.compare_digest(p['nonce'],nonce):
        raise WorkbenchError('请检查完整预览并单独确认本次 PR 文案更新', code='pr_update_consent_required')
    if datetime.now(timezone.utc) >= datetime.fromisoformat(p['expires_at']):
        raise WorkbenchError('PR 更新预览已过期，请重新生成', code='pr_update_expired')
    require_account(wb.session,p['publisher_account_hash'])
    from app.pr_lifecycle import unresolved_ready
    if published_target(wb,task_id) != p['target'] or unresolved_update(wb,task_id) or unresolved_ready(wb,task_id):
        raise WorkbenchError('PR 更新依据或状态已变化，请先核对', code='pr_update_stale')
    if any(e.kind=='pr_update_confirmed' and e.payload['preview_hash']==preview_hash for e in wb.history(task_id)):
        raise WorkbenchError('本次更新授权已使用，请核对结果', code='pr_update_consumed')
    verified_evidence(wb,task_id,p['target'],p['evidence_hash'])
    job,_ = JobService(wb.session).enqueue(kind=WRITE,idempotency_key='pr-update-write:'+event.id,
        payload={'task_id':task_id,'publisher_account_hash':p['publisher_account_hash'],'preview_hash':preview_hash},
        max_attempts=3,timeout_seconds=600,commit=False)
    consent = wb.append(task_id,'pr_update_confirmed',{'preview_hash':preview_hash,'text':text,'action':ACTION},
        key='pr-update-consent:'+event.id,actor='local-user',job_id=job.id,commit=False)
    AuditService(wb.session).prepare(event_type='pr_update.authorized',actor_type='local_user',actor_id='local-user',
        correlation_id=job.id,payload={'consent_hash':consent.record_hash,'preview_hash':preview_hash,'action':ACTION})
    wb.session.commit()
    return job


def evidence(wb: Workbench, task_id: str, target: dict) -> dict:
    from app.publish_intents import PublishIntentService
    from app.reviews import ReviewRunService
    from app.planner import frozen_inputs
    from app.plans import PlanVersionService
    completed = wb.latest(task_id,'publication_completed')
    intent = PublishIntentService(wb.session).get_verified(completed.payload['publish_intent_id'])
    review = ReviewRunService(wb.session,wb.artifacts).get_verified(intent.review_run_id)
    if review.record_hash != target['review_hash'] or review.verdict != 'pass':
        raise WorkbenchError('已发布代码的审查依据无法核对',code='pr_update_provenance')
    plan = PlanVersionService(wb.session).get_verified(review.plan_version_id)
    if plan.record_hash != review.plan_record_hash:
        raise WorkbenchError('原发布方案哈希无效',code='pr_update_provenance')
    patch = wb.artifacts.read_bytes(review.diff_hash).decode('utf-8')
    paths = sorted(set(re.findall(r'^(?:\+\+\+ b/|--- a/)(.+)$',patch,re.M)))
    if not paths or len(paths)>200 or any('..' in path.split('/') or path.startswith('/') for path in paths):
        raise WorkbenchError('原发布修改路径无法核对',code='pr_update_provenance')
    return {'inputs':frozen_inputs(wb.session,task_id), 'approved_plan':PlanVersionService._validated_content(plan).hash_payload(),
        'diff':patch,'tests':wb.read(review.test_results_hash),'verification':wb.read(review.verify_result_hash),
        'changed_paths':paths,'published_target':target}


def verified_evidence(wb: Workbench, task_id: str, target: dict, artifact_hash: str) -> dict:
    stored = wb.read(artifact_hash)
    current = evidence(wb,task_id,target)
    if any(stored[k] != current[k] for k in current):
        raise WorkbenchError('PR 文案更新的发布证据已变化',code='pr_update_provenance')
    if requirements_hash(stored['requirements']) != stored['requirements']['hash']:
        raise WorkbenchError('PR 更新规范哈希无效',code='pr_update_provenance')
    return stored


def prepare_preview(wb: Workbench, job: Job, doc: Any, provider: dict) -> dict:
    _lock(wb)
    target = published_target(wb,job.payload['task_id'])
    require_account(wb.session,job.payload['publisher_account_hash'])
    stored = verified_evidence(wb,job.payload['task_id'],target,job.payload['evidence_hash'])
    if target != job.payload['target']:
        raise WorkbenchError('PR 更新目标已变化',code='pr_update_stale')
    # Keep the original publisher marker as hidden provenance, never model prose.
    body = re.sub(r'\s*<!-- contribos:[a-f0-9-]+:[a-f0-9]{64} -->','',doc.body).rstrip()
    body += f"\n\n<!-- contribos:{job.payload['task_id']}:{target['review_hash']} -->"
    validate_document(doc.title,body,stored['requirements'],doc.template_path,
                      issue_number=stored['inputs']['issue'].get('number'))
    p = {'target':target,'before':stored['current_pr'],'title':doc.title,'body':body,
        'notes':doc.notes,'template_path':doc.template_path,'requirements':stored['requirements'],
        'requirements_hash':stored['requirements']['hash'],'evidence_hash':job.payload['evidence_hash'],
        'publisher_account_hash':job.payload['publisher_account_hash'],'login':stored['login'],
        'user_id':stored['user_id'],'provider':provider,'actions':[ACTION],
        'nonce':secrets.token_hex(32),'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()}
    event = wb.append(job.payload['task_id'],'pr_update_prepared',p,key='pr-update-preview:'+job.id,
        job_id=job.id,commit=False)
    return {'preview_hash':event.record_hash}


class PRUpdateWorker(WorkbenchJobWorker):
    kinds = (READ,WRITE,CHECK)

    def __init__(self, database: Database, settings: Settings, *, worker_id: str, transport: PRUpdateTransport):
        super().__init__(database,worker_id=worker_id)
        self.settings,self.transport = settings,transport

    async def remote(self, job: Job, *, strict: bool = True) -> tuple[dict,dict,dict]:
        with self.database.session() as session:
            self.check(session,job)
            wb = Workbench(session,self.settings.artifact_root)
            target = published_target(wb,job.payload['task_id'])
            account = require_account(session,job.payload['publisher_account_hash'])
            user = {k:account.payload[k] for k in ('login','user_id')}
        login = await self.transport.api('GET','user')
        pr = await self.transport.api('GET',f"repos/{target['repository']}/pulls/{target['number']}")
        if (login.get('login') != user['login'] or login.get('id') != user['user_id']
            or pr.get('number') != target['number'] or pr.get('html_url') != target['pr_url']
            or pr.get('base',{}).get('repo',{}).get('full_name') != target['repository']
            or pr.get('user',{}).get('id') != user['user_id'] or pr.get('user',{}).get('login') != user['login']
            or (strict and (pr.get('state')!='open' or type(pr.get('draft')) is not bool or pr.get('merged') is True
                or pr.get('head',{}).get('sha') != target['head_sha'] or pr.get('head',{}).get('ref') != target['head_branch']))):
            raise WorkbenchError('PR、发布账号或提交已变化，文案更新已停止',code='pr_update_target_stale')
        remote = {'title':pr.get('title'),'body':pr.get('body') or '', 'head_sha':pr.get('head',{}).get('sha'),
            'base_sha':pr.get('base',{}).get('sha'),'base_ref':pr.get('base',{}).get('ref'),
            'state':pr.get('state'),'draft':pr.get('draft')}
        if (not isinstance(remote['title'],str) or not isinstance(remote['body'],str)
            or len(remote['title'])>256 or len(remote['body'].encode())>65536
            or not re.fullmatch('[a-f0-9]{40}',remote['base_sha'] or '')):
            raise WorkbenchError('PR 文案或版本响应无效',code='pr_update_remote_invalid')
        ensure_no_sensitive_data(remote,context='current PR text')
        return target,user,remote

    async def execute(self, job: Job) -> dict:
        if job.kind == WRITE:
            return await self.write(job)
        if job.kind == CHECK:
            return await self.reconcile(job)
        target,user,remote = await self.remote(job)
        if target != job.payload['target']:
            raise WorkbenchError('PR 更新目标已变化',code='pr_update_stale')
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root)
            data=evidence(wb,job.payload['task_id'],target)
        requirements=await read_requirements(self.transport,target['repository'],remote['base_sha'],data['changed_paths'])
        data.update(requirements=requirements,current_pr=remote,user_request=job.payload['text'],**user)
        ensure_no_sensitive_data(data,context='PR update evidence')
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
            require_account(session,job.payload['publisher_account_hash'])
            if published_target(wb,job.payload['task_id'])!=target:
                raise WorkbenchError('PR 更新目标已变化',code='pr_update_stale')
            artifact=wb.artifact(data)
            next_job=wb.enqueue(job.payload['task_id'],DOCUMENT,{'target':target,
                'evidence_hash':artifact,'publisher_account_hash':job.payload['publisher_account_hash'],
                'model_profile_id':job.payload.get('document_model_profile_id')},key='pr-update-document:'+job.id,commit=False)
            return self.finish(session,job,{'document_job_id':next_job.id})

    def receipt(self, wb: Workbench, job: Job, authorization: WorkbenchEvent, p: dict) -> dict:
        payload={'authorization_hash':authorization.record_hash,'preview_hash':authorization.payload['preview_hash'],
            'pr_url':p['target']['pr_url'],'title':p['title'],'body_hash':content_hash(p['body']),
            'observed_at':datetime.now(timezone.utc).isoformat()}
        wb.append(job.payload['task_id'],'pr_update_completed',payload,key='pr-update-receipt:'+authorization.id,
            job_id=job.id,commit=False)
        AuditService(wb.session).prepare(event_type='pr_update.completed',actor_type='publisher',actor_id=self.worker_id,
            correlation_id=job.id,payload=payload)
        return payload

    def confirmed(self, wb: Workbench, job: Job) -> tuple[WorkbenchEvent,WorkbenchEvent]:
        events=wb.history(job.payload['task_id'])
        auth=next((e for e in reversed(events) if e.kind=='pr_update_confirmed'
            and (job.kind==CHECK or e.job_id==job.id)),None)
        preview=next((e for e in events if auth and e.kind=='pr_update_prepared' and e.record_hash==auth.payload['preview_hash']),None)
        if not auth or not preview or auth.actor_id!='local-user' or auth.payload['action']!=ACTION:
            raise WorkbenchError('缺少本次 PR 更新的明确确认',code='pr_update_unauthorized')
        p=preview.payload
        if (p['actions']!=[ACTION] or auth.payload['text']!=acknowledgement(p)
            or p['publisher_account_hash']!=job.payload['publisher_account_hash']
            or (job.kind==WRITE and job.payload['preview_hash']!=preview.record_hash)):
            raise WorkbenchError('PR 更新授权内容无效',code='pr_update_unauthorized')
        return auth,preview

    async def reconcile(self, job: Job) -> dict:
        target,_,remote=await self.remote(job,strict=False)
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
            auth,preview=self.confirmed(wb,job)
            p=preview.payload
            matched=remote['title']==p['title'] and remote['body']==p['body']
            prior=next((e for e in wb.history(job.payload['task_id']) if e.kind=='pr_update_completed'
                and e.payload['authorization_hash']==auth.record_hash),None)
            if matched and not prior:
                self.receipt(wb,job,auth,p)
            result={'matched':matched,'pr_url':target['pr_url'],'observed_at':datetime.now(timezone.utc).isoformat()}
            wb.append(job.payload['task_id'],'pr_update_observed',result,key='pr-update-observed:'+job.id,job_id=job.id,commit=False)
            return self.finish(session,job,result)

    async def write(self, job: Job) -> dict:
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root);self.check(session,job)
            auth,preview=self.confirmed(wb,job);p=preview.payload
            events=wb.history(job.payload['task_id'])
            prior=next((e for e in events if e.kind=='pr_update_completed' and e.payload['authorization_hash']==auth.record_hash),None)
            if prior:return prior.payload
            if any(e.kind=='pr_update_rejected' and e.payload['authorization_hash']==auth.record_hash for e in events):
                raise WorkbenchError('GitHub 已拒绝本次更新，请重新生成预览',code='pr_update_rejected')
            started=any(e.kind=='pr_update_write_started' and e.payload['authorization_hash']==auth.record_hash for e in events)
            stored=verified_evidence(wb,job.payload['task_id'],p['target'],p['evidence_hash'])
        target,user,remote=await self.remote(job,strict=not started)
        matches=remote['title']==p['title'] and remote['body']==p['body']
        if not matches:
            if started:
                raise WorkbenchError('上次 PR 更新结果尚未明确，只会核对，不会重复覆盖远端文案',code='pr_update_outcome_unknown')
            if target!=p['target'] or user!={'login':p['login'],'user_id':p['user_id']} or remote!=p['before']:
                raise WorkbenchError('远端 PR 文案或目标已变化，请重新生成预览',code='pr_update_stale')
            if datetime.now(timezone.utc)>=datetime.fromisoformat(p['expires_at']):
                raise WorkbenchError('PR 更新确认已过期，请重新生成预览',code='pr_update_expired')
            current=await read_requirements(self.transport,target['repository'],remote['base_sha'],stored['changed_paths'])
            if current['hash']!=p['requirements_hash']:
                raise WorkbenchError('仓库贡献规范已变化，请重新生成预览',code='pr_update_stale')
            validate_document(p['title'],p['body'],current,p['template_path'],issue_number=stored['inputs']['issue'].get('number'))
            _,_,last=await self.remote(job)
            if last!=remote:
                raise WorkbenchError('远端 PR 文案已变化，请重新生成预览',code='pr_update_stale')
            with self.database.session() as session:
                wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
                require_account(session,p['publisher_account_hash'])
                if wb.latest(job.payload['task_id'],'pr_update_prepared').record_hash!=preview.record_hash:
                    raise WorkbenchError('PR 更新预览已变化',code='pr_update_stale')
                wb.append(job.payload['task_id'],'pr_update_write_started',{'authorization_hash':auth.record_hash,
                    'preview_hash':preview.record_hash,'action':ACTION},key='pr-update-send:'+auth.id,job_id=job.id)
            try:
                await self.transport.pr_text(target['repository'],target['number'],authorization={
                    'action':ACTION,'actor_id':'local-user','authorization_hash':auth.record_hash,
                    'repository':target['repository'],'number':target['number'],'title':p['title'],'body':p['body']})
            except WorkbenchError as exc:
                if exc.code=='pr_update_rejected':
                    with self.database.session() as session:
                        wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
                        wb.append(job.payload['task_id'],'pr_update_rejected',{'authorization_hash':auth.record_hash},
                            key='pr-update-rejected:'+auth.id,job_id=job.id)
                raise
            _,_,after=await self.remote(job,strict=False)
            if after['title']!=p['title'] or after['body']!=p['body']:
                raise WorkbenchError('GitHub 更新后的文案无法核对，请只读核对结果',code='pr_update_outcome_unknown')
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
            return self.finish(session,job,self.receipt(wb,job,auth,p))
