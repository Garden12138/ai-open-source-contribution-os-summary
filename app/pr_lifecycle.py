"""Explicitly consented ready-for-review and bounded read-only PR follow-up."""
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
from app.pr_updates import evidence, verified_evidence, unresolved_update
from app.provenance import content_hash
from app.publication_requirements import read_requirements, validate_document
from app.publisher_accounts import require_account
from app.security import ensure_no_sensitive_data, redact_text
from app.workbench import Workbench, WorkbenchError
from app.workbench_worker import WorkbenchJobWorker

PREPARE, WRITE, FOLLOWUP = 'pr_ready_prepare', 'pr_ready_write', 'pr_followup'
ACTION = 'mark_pull_request_ready_for_review'
READY_QUERY = '''mutation ContribOSReady($input: MarkPullRequestReadyForReviewInput!) {
  markPullRequestReadyForReview(input: $input) {
    clientMutationId pullRequest { id number url isDraft headRefOid }
  }
}'''


class LifecycleTransport(Protocol):
    async def api(self, method: str, path: str, body: dict | None = None, *, missing: bool = False) -> Any: ...
    async def pr_ready(self, repository: str, number: int, *, authorization: dict) -> dict: ...


def conversation_action(text: str) -> str | None:
    """Intent routing only queues reads/previews, never authorizes a mutation."""
    if re.search(r'(?:Ready\s*for\s*review|转为.*审核|提交.*审核|取消草稿|转.*正式|准备.*审核)', text, re.I):
        if not re.search(r'^(?:能否|是否|怎么|如何|查看|核对|跟进|检查|can\b|how\b)|(?:吗|[？?])$', text.strip(), re.I):
            return PREPARE
        return FOLLOWUP
    if re.search(r'跟进|核对.*(?:PR|审核)|(?:PR|CI|审核|合并|草稿).*(?:状态|进度|失败|结果|意见)|(?:查看|检查|同步).*(?:PR|CI|审核)', text, re.I):
        return FOLLOWUP
    return None


def unresolved_ready(wb: Workbench, task_id: str) -> bool:
    events = wb.history(task_id)
    resolved = {e.payload['authorization_hash'] for e in events if e.kind in {'pr_ready_completed','pr_ready_rejected'}}
    return any(e.kind == 'pr_ready_write_started' and e.payload['authorization_hash'] not in resolved for e in events)


def acknowledgement(p: dict) -> str:
    return f"确认将 {p['target']['pr_url']} 转为 Ready for review，提交 {p['before']['head_sha']}；此操作不代表 CI 或审核已通过。"


def lifecycle_view(wb: Workbench, task_id: str) -> dict:
    observation = wb.latest(task_id,'pr_followup_observed')
    preview = wb.latest(task_id,'pr_ready_prepared')
    p = None
    if preview:
        p = {**preview.payload,'record_hash':preview.record_hash,'acknowledgement':acknowledgement(preview.payload),
             'consumed':any(e.kind=='pr_ready_confirmed' and e.payload['preview_hash']==preview.record_hash for e in wb.history(task_id))}
    completed = wb.latest(task_id,'pr_ready_completed')
    return {'snapshot':observation.payload if observation else None,'preview':p,
            'completed':completed.payload if completed else None,'unresolved':unresolved_ready(wb,task_id)}


def request_lifecycle(wb: Workbench, task_id: str, *, kind: str, account_hash: str, key: str, text: str = '', commit: bool = True,
                      origin: str = 'user', source: WorkbenchEvent | None = None) -> Job:
    _lock(wb)
    if kind not in {PREPARE,FOLLOWUP}:
        raise WorkbenchError('不允许的 PR 跟进请求',code='pr_lifecycle_unauthorized')
    if origin not in {'user','workflow','maintenance'}:
        raise WorkbenchError('PR 跟进来源无效',code='pr_lifecycle_unauthorized')
    trigger = None
    if origin != 'user':
        if origin == 'workflow' and (source is None or source.task_id != task_id or source.kind != 'publication_completed'
                or not any(e.id == source.id and e.record_hash == source.record_hash for e in wb.history(task_id))):
            raise WorkbenchError('自动跟进缺少已核实的发布来源',code='pr_lifecycle_unauthorized')
        trigger = {'kind':origin, **({'source_id':source.id,'source_hash':source.record_hash} if source else {})}
    ensure_no_sensitive_data(text,context='PR lifecycle request')
    old=wb.session.scalar(select(Job).where(Job.kind==kind,Job.idempotency_key==kind+':'+key))
    if old:
        if (old.payload['task_id']!=task_id or old.payload['text']!=text or old.payload['publisher_account_hash']!=account_hash
                or old.payload.get('trigger') != trigger):
            raise WorkbenchError('重复请求内容不同',code='pr_lifecycle_conflict')
        return old
    require_account(wb.session,account_hash);wb.assert_idle(task_id)
    target=published_target(wb,task_id)
    if kind==PREPARE and (unresolved_ready(wb,task_id) or unresolved_update(wb,task_id)):
        raise WorkbenchError('远端操作结果尚未明确，请先核对 PR 跟进或文案更新结果',code='pr_ready_outcome_unknown')
    if sum(j.kind==kind for j in wb.jobs(task_id)) >= (16 if kind==PREPARE else 64):
        raise WorkbenchError('本任务 PR 跟进预算已用完',code='pr_lifecycle_budget')
    actor = {'user':'local-user','workflow':'system','maintenance':'orchestrator'}[origin]
    job=wb.enqueue(task_id,kind,{'target':target,'publisher_account_hash':account_hash,'text':text,
                   **({'trigger':trigger} if trigger else {})},
                   key=kind+':'+key,actor=actor,max_attempts=2 if origin=='workflow' else 1,commit=False)
    if trigger:
        wb.append(task_id,'pr_lifecycle_triggered',trigger,key='lifecycle-trigger:'+job.id,actor=actor,job_id=job.id,commit=False)
    if text:wb.append(task_id,'pr_lifecycle_requested',{'text':text},key='lifecycle-message:'+key,actor='local-user',job_id=job.id,commit=False)
    if commit:
        wb.session.commit()
    return job


def confirm_ready(wb: Workbench, task_id: str, *, preview_hash: str, nonce: str, text: str, approved: bool) -> Job:
    _lock(wb);wb.assert_idle(task_id)
    from app.conversation import require_settled_messages
    require_settled_messages(wb, task_id)
    preview=wb.latest(task_id,'pr_ready_prepared')
    if not preview or preview.record_hash!=preview_hash:
        raise WorkbenchError('审核状态预览已变化，请重新准备',code='pr_ready_stale')
    p=preview.payload
    if not approved or text!=acknowledgement(p) or not secrets.compare_digest(p['nonce'],nonce):
        raise WorkbenchError('请核对预览并单独确认转为正式审核',code='pr_ready_consent_required')
    if datetime.now(timezone.utc)>=datetime.fromisoformat(p['expires_at']):
        raise WorkbenchError('审核状态预览已过期，请重新准备',code='pr_ready_expired')
    require_account(wb.session,p['publisher_account_hash'])
    if published_target(wb,task_id)!=p['target'] or unresolved_ready(wb,task_id) or unresolved_update(wb,task_id):
        raise WorkbenchError('PR 依据或远端操作状态已变化，请先核对',code='pr_ready_stale')
    if any(e.kind=='pr_ready_confirmed' and e.payload['preview_hash']==preview_hash for e in wb.history(task_id)):
        raise WorkbenchError('审核状态授权已使用，请核对结果',code='pr_ready_consumed')
    verified_evidence(wb,task_id,p['target'],p['evidence_hash'])
    job,_=JobService(wb.session).enqueue(kind=WRITE,idempotency_key='pr-ready-write:'+preview.id,
        payload={'task_id':task_id,'preview_hash':preview_hash,'publisher_account_hash':p['publisher_account_hash']},
        max_attempts=3,timeout_seconds=600,commit=False)
    auth=wb.append(task_id,'pr_ready_confirmed',{'preview_hash':preview_hash,'text':text,'action':ACTION},
        key='pr-ready-consent:'+preview.id,actor='local-user',job_id=job.id,commit=False)
    AuditService(wb.session).prepare(event_type='pr_ready.authorized',actor_type='local_user',actor_id='local-user',
        correlation_id=job.id,payload={'authorization_hash':auth.record_hash,'preview_hash':preview_hash,'action':ACTION})
    wb.session.commit()
    return job


def safe_text(value: Any, limit: int = 2000) -> str:
    return redact_text(value if isinstance(value,str) else '')[:limit]


def source_url(value: Any, repository: str) -> str | None:
    prefix='https://github.com/'+repository+'/'
    if isinstance(value,str) and value.startswith(prefix) and re.fullmatch(r'(?:pull/[0-9]+(?:#[A-Za-z0-9_-]+)?|actions/runs/[0-9]+(?:/job/[0-9]+)?|runs/[0-9]+(?:/jobs/[0-9]+)?)',value[len(prefix):]):
        return value
    return None


def summarize(pr: dict, checks: list[dict], workflows: list[dict], statuses: list[dict], reviews: list[dict], complete: bool, target: dict) -> dict:
    """Observed checks are not proof that every repository merge rule is satisfied."""
    latest={}
    for x in workflows:
        key=(x.get('workflow_id') or x['name'],x.get('event'))
        if key not in latest or x['id']>latest[key]['id']:latest[key]=x
    latest_checks={}
    for x in checks:
        key=(x['name'],x.get('app'))
        if key not in latest_checks or x['id']>latest_checks[key]['id']:latest_checks[key]=x
    signals=list(latest.values())+list(latest_checks.values())+statuses
    approval=any(x.get('conclusion')=='action_required' or x.get('status')=='action_required' for x in signals)
    failed=any(x.get('conclusion') in {'failure','timed_out','cancelled','startup_failure','stale'} or x.get('state') in {'failure','error'} for x in signals)
    running=any(x.get('status') not in {None,'completed','action_required'} or x.get('state')=='pending' for x in signals)
    unknown_outcome=any(x.get('status')=='completed' and x.get('conclusion') not in {'success','neutral','skipped','failure','timed_out','cancelled','startup_failure','stale','action_required'} for x in signals)
    unknown_outcome = unknown_outcome or any(x.get('state') not in {'success','failure','error','pending'} for x in statuses)
    ci='awaiting_approval' if approval else 'failed' if failed else 'running' if running else 'unknown' if not complete or not signals or unknown_outcome else 'observed_passed'
    effective={}
    for x in sorted(reviews,key=lambda r:r['id']):
        if x['state'] in {'APPROVED','CHANGES_REQUESTED','DISMISSED'}:effective[x['login']]=x
    changes=any(x['state']=='CHANGES_REQUESTED' for x in effective.values())
    approved=any(x['state']=='APPROVED' and x.get('commit_id')==pr['head_sha'] for x in effective.values())
    stage='merged' if pr['merged'] else 'closed' if pr['state']=='closed' else 'head_changed' if pr['head_sha']!=target['head_sha'] else 'draft' if pr['draft'] else 'changes_requested' if changes else 'ci_awaiting_approval' if ci=='awaiting_approval' else 'ci_failed' if ci=='failed' else 'ci_running' if ci=='running' else 'checks_unknown' if ci=='unknown' else 'awaiting_merge' if approved else 'awaiting_review'
    messages={
        'merged':('none','PR 已合并，保留贡献记录。'), 'closed':('none','PR 已关闭且未合并，请查看关闭原因。'),
        'head_changed':('author','远端提交已变化，原沙箱验证不再覆盖当前 head；需要重新验证和审查。'),
        'draft':('author','代码与文案准备好后，可在对话中请求转为 Ready for review。'),
        'changes_requested':('author','阅读审核意见，修订代码后在沙箱验证；后续代码发布需要新的精确确认。'),
        'ci_awaiting_approval':('maintainer','Fork 工作流等待上游维护者批准运行；等待批准不等于测试失败。'),
        'ci_failed':('author','查看失败检查和步骤，修复后重新验证；沙箱通过不等于远端 CI 通过。'),
        'ci_running':('github','GitHub 检查正在执行，完成后再次跟进。'),
        'checks_unknown':('unknown','检查尚未运行、结果不完整或不可读取，不能判定全部通过。'),
        'awaiting_merge':('maintainer','已有针对当前提交的审核通过记录；仍需维护者核对仓库合并规则并合并。'),
        'awaiting_review':('maintainer','等待维护者审核，持续跟进审核意见。')}
    actor,message=messages[stage]
    return {'stage':stage,'ci':ci,'next_actor':actor,'next_action':message,'checks':list(latest_checks.values())[-20:],
            'workflows':list(latest.values())[-20:],'statuses':statuses[-20:],'reviews':reviews[-20:],'complete':complete}


class PRLifecycleWorker(WorkbenchJobWorker):
    kinds=(PREPARE,WRITE,FOLLOWUP)

    def __init__(self,database: Database,settings: Settings,*,worker_id: str,transport: LifecycleTransport):
        super().__init__(database,worker_id=worker_id);self.settings,self.transport=settings,transport

    async def remote(self,job: Job) -> tuple[dict,dict,dict]:
        with self.database.session() as session:
            self.check(session,job);wb=Workbench(session,self.settings.artifact_root)
            target=published_target(wb,job.payload['task_id']);account=require_account(session,job.payload['publisher_account_hash'])
            user={k:account.payload[k] for k in ('login','user_id')}
        actual=await self.transport.api('GET','user')
        value=await self.transport.api('GET',f"repos/{target['repository']}/pulls/{target['number']}")
        if (actual.get('login')!=user['login'] or actual.get('id')!=user['user_id'] or value.get('number')!=target['number']
            or value.get('html_url')!=target['pr_url'] or value.get('base',{}).get('repo',{}).get('full_name')!=target['repository']
            or value.get('user',{}).get('login')!=user['login'] or value.get('user',{}).get('id')!=user['user_id']):
            raise WorkbenchError('PR 或发布账号无法核对',code='pr_lifecycle_target_stale')
        pr={'node_id':value.get('node_id'),'title':value.get('title'),'body':value.get('body') or '',
            'head_sha':value.get('head',{}).get('sha'),'head_branch':value.get('head',{}).get('ref'),
            'base_sha':value.get('base',{}).get('sha'),'base_ref':value.get('base',{}).get('ref'),
            'state':value.get('state'),'draft':value.get('draft'),'merged':value.get('merged'),
            'merged_at':value.get('merged_at'),'closed_at':value.get('closed_at'),
            'merge_commit_sha':value.get('merge_commit_sha'),
            'mergeable':value.get('mergeable')}
        if (not isinstance(pr['node_id'],str) or not re.fullmatch(r'[A-Za-z0-9_=-]{8,256}',pr['node_id'])
            or not isinstance(pr['title'],str) or len(pr['title'])>256 or not isinstance(pr['body'],str) or len(pr['body'].encode())>65536
            or not re.fullmatch('[a-f0-9]{40}',pr['head_sha'] or '') or not re.fullmatch('[a-f0-9]{40}',pr['base_sha'] or '')
            or pr['state'] not in {'open','closed'} or type(pr['draft']) is not bool or type(pr['merged']) is not bool):
            raise WorkbenchError('PR 状态响应无效',code='pr_lifecycle_remote_invalid')
        for name in ('merged_at','closed_at'):
            stamp=pr[name]
            if stamp is not None:
                try:
                    if not isinstance(stamp,str) or len(stamp)>64 or datetime.fromisoformat(stamp.replace('Z','+00:00')).tzinfo is None:
                        raise ValueError()
                except ValueError:
                    raise WorkbenchError('PR 状态时间无效',code='pr_lifecycle_remote_invalid') from None
        if pr['merge_commit_sha'] is not None and not re.fullmatch('[a-f0-9]{40}',pr['merge_commit_sha'] if isinstance(pr['merge_commit_sha'],str) else ''):
            raise WorkbenchError('PR 合并提交无效',code='pr_lifecycle_remote_invalid')
        ensure_no_sensitive_data(pr,context='remote lifecycle')
        return target,user,pr

    async def observe(self,job: Job,target: dict,pr: dict) -> dict:
        root=f"repos/{target['repository']}";warnings=[]
        async def listing(path: str, key: str | None = None) -> list[dict]:
            rows=[]
            try:
                for page in range(1,5):
                    value=await self.transport.api('GET',path+('&' if '?' in path else '?')+f'per_page=100&page={page}')
                    batch=value.get(key) if key and isinstance(value,dict) else value
                    if not isinstance(batch,list) or len(batch)>100 or any(not isinstance(x,dict) for x in batch):raise ValueError()
                    rows.extend(batch)
                    if len(batch)<100:
                        if key and (type(value.get('total_count')) is not int or value['total_count'] != len(rows)):
                            warnings.append('GitHub 返回的检查数量不完整，不能判定全部通过。')
                        return rows
                warnings.append('列表超过 400 条，当前结果不完整。')
            except (WorkbenchError,ValueError,KeyError,TypeError):
                warnings.append('部分 GitHub 检查或反馈暂不可读取，不能据此判定全部通过。')
            return rows
        head=pr['head_sha'];repo=target['repository']
        raw_checks=await listing(root+f'/commits/{head}/check-runs?', 'check_runs')
        raw_workflows=await listing(root+f'/actions/runs?head_sha={head}&event=pull_request','workflow_runs')
        raw_statuses=await listing(root+f'/commits/{head}/statuses')
        raw_reviews=await listing(root+f"/pulls/{target['number']}/reviews")
        raw_comments=await listing(root+f"/pulls/{target['number']}/comments")
        raw_discussion=await listing(root+f"/issues/{target['number']}/comments")
        def check(x):
            return {'id':x['id'],'name':safe_text(x.get('name'),200),'status':x.get('status'), 'conclusion':x.get('conclusion'),
                'url':source_url(x.get('details_url') or x.get('html_url'),repo), 'app':safe_text(x.get('app',{}).get('slug'),100),
                'summary':safe_text(x.get('output',{}).get('summary')), 'workflow_id':x.get('workflow_id'),'event':x.get('event')}
        checks=[check(x) for x in raw_checks if type(x.get('id')) is int and x.get('head_sha',head)==head]
        workflows=[check(x) for x in raw_workflows if type(x.get('id')) is int and x.get('head_sha')==head and x.get('event')=='pull_request'
                   and (not x.get('pull_requests') or any(p.get('number')==target['number'] and p.get('base',{}).get('repo',{}).get('name',repo.split('/')[-1])==repo.split('/')[-1] for p in x['pull_requests']))]
        # Status API returns newest first; retain one per context.
        statuses={}
        for x in raw_statuses:
            context=safe_text(x.get('context'),200)
            if context not in statuses:statuses[context]={'name':context,'state':x.get('state'),'url':source_url(x.get('target_url'),repo)}
        reviews=[{'id':x['id'],'login':safe_text(x.get('user',{}).get('login'),100),'state':x.get('state'),
            'body':safe_text(x.get('body')),'commit_id':x.get('commit_id'),'url':source_url(x.get('html_url'),repo)}
            for x in raw_reviews if type(x.get('id')) is int and x.get('state')!='PENDING']
        comments=[{'id':x['id'],'login':safe_text(x.get('user',{}).get('login'),100),'body':safe_text(x.get('body')),
            'path':safe_text(x.get('path'),300),'line':x.get('line') if type(x.get('line')) is int else None,
            'url':source_url(x.get('html_url'),repo)} for x in [*raw_comments[-10:],*raw_discussion[-10:]] if type(x.get('id')) is int]
        latest_workflows={}
        for row in workflows:
            key=(row.get('workflow_id') or row['name'],row.get('event'))
            if key not in latest_workflows or row['id']>latest_workflows[key]['id']:latest_workflows[key]=row
        failures=[]
        for row in sorted(latest_workflows.values(),key=lambda x:x['id'],reverse=True)[:3]:
            if row['conclusion'] not in {'failure','timed_out','startup_failure'}:continue
            jobs=await listing(root+f"/actions/runs/{row['id']}/jobs",'jobs')
            for value in jobs:
                if value.get('conclusion') not in {'failure','timed_out','cancelled'} or value.get('head_sha',head)!=head:continue
                failures.append({'name':safe_text(value.get('name'),200),'conclusion':value.get('conclusion'),
                    'url':source_url(value.get('html_url'),repo),'steps':[{'name':safe_text(step.get('name'),200),
                    'conclusion':step.get('conclusion')} for step in value.get('steps',[]) if isinstance(step,dict)
                    and step.get('conclusion') in {'failure','timed_out','cancelled'}][:10]})
        annotations=[]
        for row in sorted(checks,key=lambda x:x['id'],reverse=True)[:3]:
            if row['conclusion'] not in {'failure','timed_out'}:continue
            values=await listing(root+f"/check-runs/{row['id']}/annotations")
            annotations.extend({'path':safe_text(value.get('path'),300),'line':value.get('start_line') if type(value.get('start_line')) is int else None,
                'message':safe_text(value.get('message')),'level':safe_text(value.get('annotation_level'),30),
                'url':row['url']} for value in values[:10])
        result=summarize(pr,checks,workflows,list(statuses.values()),reviews,not warnings,target)
        result.update(failures=failures[:10],annotations=annotations[:20])
        result.update(target=target,pr=pr,comments=comments,warnings=sorted(set(warnings)),observed_at=datetime.now(timezone.utc).isoformat())
        if pr['mergeable'] is False and not pr['merged'] and pr['state']=='open':result['warnings'].append('GitHub 报告合并冲突，需更新代码并重新验证。')
        ensure_no_sensitive_data(result,context='PR follow-up observation')
        return result

    def save_observation(self,wb: Workbench,job: Job,snapshot: dict) -> None:
        artifact=wb.artifact(snapshot)
        from app.pull_request_events import PullRequestEventService
        from app.authorizations import UserAction
        completed=wb.latest(job.payload['task_id'],'publication_completed')
        PullRequestEventService(wb.session).ingest(draft_pull_request_id=completed.payload['draft_pull_request_id'],
            action=UserAction.INGEST_PR_EVENT,remote_event_id='pr-followup:'+job.id,
            event_type='merged' if snapshot['stage']=='merged' else 'check',
            payload={'artifact_hash':artifact,'head_sha':snapshot['pr']['head_sha'],'stage':snapshot['stage']},
            actor_type='publisher',actor_id=self.worker_id,commit=False)
        wb.append(job.payload['task_id'],'pr_followup_observed',{**snapshot,'artifact_hash':artifact},
            key='pr-observed:'+job.id,job_id=job.id,commit=False)
        AuditService(wb.session).prepare(event_type='pr.followup',actor_type='publisher',actor_id=self.worker_id,
            correlation_id=job.id,payload={'task_id':job.payload['task_id'],'snapshot_hash':artifact,'stage':snapshot['stage']})

    def authorization(self,wb: Workbench,job: Job) -> tuple[WorkbenchEvent,WorkbenchEvent]:
        events=wb.history(job.payload['task_id'])
        auth=next((e for e in reversed(events) if e.kind=='pr_ready_confirmed' and (job.kind!=WRITE or e.job_id==job.id)),None)
        preview=next((e for e in events if auth and e.kind=='pr_ready_prepared' and e.record_hash==auth.payload['preview_hash']),None)
        if (not auth or not preview or auth.actor_id!='local-user' or auth.payload['action']!=ACTION
            or auth.payload['text']!=acknowledgement(preview.payload) or preview.payload['actions']!=[ACTION]
            or preview.payload['publisher_account_hash']!=job.payload['publisher_account_hash']
            or (job.kind==WRITE and job.payload['preview_hash']!=preview.record_hash)):
            raise WorkbenchError('缺少本次 Ready for review 精确授权',code='pr_ready_unauthorized')
        return auth,preview

    def receipt(self,wb: Workbench,job: Job,auth: WorkbenchEvent,p: dict,pr: dict) -> dict:
        if any(e.kind=='pr_ready_completed' and e.payload['authorization_hash']==auth.record_hash for e in wb.history(job.payload['task_id'])):
            return {'ready':True}
        payload={'authorization_hash':auth.record_hash,'preview_hash':auth.payload['preview_hash'],
            'pr_url':p['target']['pr_url'],'head_sha':pr['head_sha'],'observed_at':datetime.now(timezone.utc).isoformat()}
        wb.append(job.payload['task_id'],'pr_ready_completed',payload,key='pr-ready-receipt:'+auth.id,job_id=job.id,commit=False)
        AuditService(wb.session).prepare(event_type='pr_ready.completed',actor_type='publisher',actor_id=self.worker_id,correlation_id=job.id,payload=payload)
        return payload

    @staticmethod
    def matches_ready(p: dict,pr: dict) -> bool:
        return (pr['node_id']==p['before']['node_id'] and pr['head_sha']==p['before']['head_sha']
                and pr['head_branch']==p['before']['head_branch'] and pr['draft'] is False and not pr['merged'] and pr['state']=='open')

    async def execute(self,job: Job) -> dict:
        if job.kind==WRITE:return await self.write(job)
        target,user,pr=await self.remote(job)
        snapshot=await self.observe(job,target,pr)
        with self.database.session() as session:
            self.check(session,job);wb=Workbench(session,self.settings.artifact_root);_lock(wb)
            if published_target(wb,job.payload['task_id'])!=target:raise WorkbenchError('发布来源已变化',code='pr_ready_stale')
            require_account(session,job.payload['publisher_account_hash'])
            if job.kind==FOLLOWUP:
                if unresolved_ready(wb,job.payload['task_id']):
                    auth,preview=self.authorization(wb,job)
                    if self.matches_ready(preview.payload,pr):self.receipt(wb,job,auth,preview.payload,pr)
                self.save_observation(wb,job,snapshot)
                return self.finish(session,job,{'stage':snapshot['stage'],'ci':snapshot['ci']})
        if pr['state']!='open' or pr['merged'] or pr['head_sha']!=target['head_sha'] or pr['head_branch']!=target['head_branch']:
            raise WorkbenchError('PR 已关闭、合并或提交已变化，请先跟进最新状态',code='pr_ready_target_stale')
        if not pr['draft']:
            with self.database.session() as session:
                wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
                self.save_observation(wb,job,snapshot)
                return self.finish(session,job,{'already_ready':True})
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root);data=evidence(wb,job.payload['task_id'],target)
            document=wb.latest(job.payload['task_id'],'pr_update_prepared') or wb.latest(job.payload['task_id'],'publication_document_ready')
            template=document.payload.get('template_path') if document else None
        rules=await read_requirements(self.transport,target['repository'],pr['base_sha'],data['changed_paths'])
        validate_document(pr['title'],pr['body'],rules,template,issue_number=data['inputs']['issue'].get('number'))
        data['requirements']=rules
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
            require_account(session,job.payload['publisher_account_hash'])
            p={'target':target,'before':pr,**user,'publisher_account_hash':job.payload['publisher_account_hash'],
                'evidence_hash':wb.artifact(data),'requirements_hash':rules['hash'],'template_path':template,'actions':[ACTION],
                'repository_checks':rules['repository_checks'],
                'sources':[{k:source[k] for k in ('repository','path','commit')} for source in rules['sources']],
                'nonce':secrets.token_hex(32),'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()}
            self.save_observation(wb,job,snapshot)
            preview=wb.append(job.payload['task_id'],'pr_ready_prepared',p,key='pr-ready-preview:'+job.id,job_id=job.id,commit=False)
            return self.finish(session,job,{'preview_hash':preview.record_hash})

    async def write(self,job: Job) -> dict:
        with self.database.session() as session:
            self.check(session,job);wb=Workbench(session,self.settings.artifact_root)
            auth,preview=self.authorization(wb,job);p=preview.payload;events=wb.history(job.payload['task_id'])
            if any(e.kind=='pr_ready_completed' and e.payload['authorization_hash']==auth.record_hash for e in events):return {'ready':True}
            if any(e.kind=='pr_ready_rejected' and e.payload['authorization_hash']==auth.record_hash for e in events):
                raise WorkbenchError('GitHub 已拒绝本次操作，请重新准备预览',code='pr_ready_rejected')
            started=any(e.kind=='pr_ready_write_started' and e.payload['authorization_hash']==auth.record_hash for e in events)
            data=verified_evidence(wb,job.payload['task_id'],p['target'],p['evidence_hash'])
        target,user,pr=await self.remote(job)
        if not self.matches_ready(p,pr):
            if started:raise WorkbenchError('上次转为审核结果尚未明确，只会核对，不会重复提交',code='pr_ready_outcome_unknown')
            # mergeable is transient computed metadata, not authorization input.
            before={k:v for k,v in p['before'].items() if k!='mergeable'}
            if target!=p['target'] or user!={'login':p['login'],'user_id':p['user_id']} or {k:v for k,v in pr.items() if k!='mergeable'}!=before:
                raise WorkbenchError('PR 提交、状态或文案已变化，请重新准备预览',code='pr_ready_stale')
            if datetime.now(timezone.utc)>=datetime.fromisoformat(p['expires_at']):
                raise WorkbenchError('审核状态授权已过期，请重新准备',code='pr_ready_expired')
            rules=await read_requirements(self.transport,target['repository'],pr['base_sha'],data['changed_paths'])
            if rules['hash']!=p['requirements_hash']:raise WorkbenchError('仓库规范已变化，请重新准备预览',code='pr_ready_stale')
            validate_document(pr['title'],pr['body'],rules,p['template_path'],issue_number=data['inputs']['issue'].get('number'))
            _,_,last=await self.remote(job)
            if {k:v for k,v in last.items() if k!='mergeable'}!=before:
                raise WorkbenchError('远端 PR 已变化，请重新准备预览',code='pr_ready_stale')
            with self.database.session() as session:
                wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
                require_account(session,p['publisher_account_hash'])
                if wb.latest(job.payload['task_id'],'pr_ready_prepared').record_hash!=preview.record_hash or unresolved_update(wb,job.payload['task_id']):
                    raise WorkbenchError('预览或远端操作状态已变化',code='pr_ready_stale')
                wb.append(job.payload['task_id'],'pr_ready_write_started',{'authorization_hash':auth.record_hash,'preview_hash':preview.record_hash},
                    key='pr-ready-send:'+auth.id,job_id=job.id)
            try:
                await self.transport.pr_ready(target['repository'],target['number'],authorization={
                    'action':ACTION,'actor_id':'local-user','authorization_hash':auth.record_hash,
                    'repository':target['repository'],'number':target['number'],'node_id':pr['node_id'],'head_sha':pr['head_sha']})
            except WorkbenchError as exc:
                if exc.code=='pr_ready_rejected':
                    with self.database.session() as session:
                        wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
                        wb.append(job.payload['task_id'],'pr_ready_rejected',{'authorization_hash':auth.record_hash},key='pr-ready-rejected:'+auth.id,job_id=job.id)
                raise
            _,_,pr=await self.remote(job)
            if not self.matches_ready(p,pr):raise WorkbenchError('转为审核后的 PR 状态尚无法核对，请跟进结果',code='pr_ready_outcome_unknown')
        snapshot=await self.observe(job,target,pr)
        with self.database.session() as session:
            wb=Workbench(session,self.settings.artifact_root);_lock(wb);self.check(session,job)
            result=self.receipt(wb,job,auth,p,pr);self.save_observation(wb,job,snapshot)
            return self.finish(session,job,result)
