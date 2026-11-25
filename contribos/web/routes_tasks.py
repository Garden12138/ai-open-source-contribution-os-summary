"""Authenticated HTTP entrypoints for tasks, agents and the event stream."""

from dataclasses import asdict
from json import loads
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from contribos.agent.engine import RequestResponse
from contribos.agent.profiles import PROFILES, AgentStatus, agent_statuses, check_profile
from contribos.review.service import APPLY_PHRASES
from contribos.tasks.instructions import CONTINUE_PROMPT
from contribos.tasks.issue import IssueError, parse_issue_url
from contribos.tasks.store import TaskError
from contribos.tasks.supervisor import TaskSupervisor
from contribos.web.demo import demo_statuses

router = APIRouter(prefix='/api')
CURSOR_MAX = 2**63 - 1


class TaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    opportunity_id: str | None = Field(default=None, min_length=1, max_length=200)
    issue_url: str | None = Field(default=None, min_length=1, max_length=2048)
    agent: str | None = Field(default=None, min_length=1, max_length=100)
    isolation: Literal['host', 'sbx', 'container'] | None = None
    trust_confirmed: StrictBool = False
    trust_owner: StrictBool = False


class MessageInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    text: str = Field(min_length=1, max_length=131072)
    client_message_id: str = Field(min_length=1, max_length=200)
    delivery: Literal['queue', 'interrupt'] = 'queue'


class RequestInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    option_id: str | None = Field(default=None, min_length=1, max_length=200)
    answers: dict[str, Any] | None = None


class SwitchAgentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    agent: str = Field(min_length=1, max_length=100)
    client_message_id: str = Field(min_length=1, max_length=200)


def _supervisor(request: Request) -> TaskSupervisor:
    return request.app.state.supervisor


def _agent_info(request: Request, status: AgentStatus) -> dict[str, Any]:
    profile = PROFILES[status.id]
    supports = ['host'] + (['sbx'] if profile.sbx else [])
    if request.app.state.demo:
        supports = ['host', 'sbx', 'container']
    configured = request.app.state.supervisor.sandboxes
    return request.app.state.supervisor.store.sanitize({
        **asdict(status), 'name': profile.name, 'installed': status.executable is not None, 'accepted': profile.accepted,
        'sandbox_unavailable_reason': profile.sandbox_unavailable_reason if 'sbx' not in supports else '',
        'isolation': supports, 'available_isolation': [kind for kind in supports if kind in configured and profile.accepted],
    })


@router.get('/agents')
def agents(request: Request) -> dict[str, Any]:
    statuses = demo_statuses() if request.app.state.demo else agent_statuses(request.app.state.settings)
    return {'agents': [_agent_info(request, status) for status in statuses]}


@router.post('/agents/{agent_id}/check')
def agent_check(request: Request, agent_id: str) -> dict[str, Any]:
    profile = PROFILES.get(agent_id)
    if profile is None:
        raise TaskError('unknown_agent', 'The selected Agent does not exist.', 404)
    status = (next(status for status in demo_statuses() if status.id == agent_id)
              if request.app.state.demo else check_profile(profile, request.app.state.settings))
    return _agent_info(request, status)


@router.post('/tasks', status_code=201)
async def create_task(request: Request, body: TaskInput) -> dict[str, str]:
    supervisor = _supervisor(request)
    if (body.opportunity_id is None) == (body.issue_url is None):
        raise TaskError('invalid_issue_source', 'Provide either an Issue URL or an opportunity ID.', 422)
    if body.opportunity_id:
        ref = supervisor.store.opportunity(body.opportunity_id)
    else:
        try:
            ref = parse_issue_url(body.issue_url)
        except IssueError:
            raise TaskError('invalid_issue_url', 'Provide a GitHub Issue URL.', 422) from None
    row = supervisor.create(ref, agent=body.agent, isolation=body.isolation,
                            trust_confirmed=body.trust_confirmed, trust_owner=body.trust_owner,
                            opportunity_id=body.opportunity_id)
    return {'task_id': row['id']}


_TASK_SUMMARY = ('id', 'opportunity_id', 'repo_full_name', 'issue_number', 'title', 'status', 'agent_state',
                 'agent_profile', 'isolation', 'last_error_code', 'last_error_message', 'created_at', 'updated_at')


@router.get('/tasks')
async def tasks(request: Request) -> dict[str, Any]:
    store = _supervisor(request).store
    result = {'tasks': [store.sanitize({key: row[key] for key in _TASK_SUMMARY}) for row in store.list_tasks()]}
    if request.app.state.demo:
        result['demo'] = True
    return result


@router.get('/tasks/{task_id}')
async def task(request: Request, task_id: str) -> dict[str, Any]:
    supervisor = _supervisor(request)
    row = supervisor.store.get(task_id)
    keys = (*_TASK_SUMMARY, 'default_branch', 'base_sha', 'work_branch', 'tokens_input', 'tokens_output', 'model', 'mode')
    turns = [{key: turn[key] for key in ('id', 'kind', 'status', 'client_message_id', 'stop_reason', 'retry_count',
              'error_code', 'started_at', 'completed_at')} for turn in supervisor.store.turns(task_id)]
    return supervisor.store.sanitize({
        **{key: row[key] for key in keys}, 'issue_snapshot': loads(row['issue_snapshot_json']),
        'agent_name': PROFILES[row['agent_profile']].name if row['agent_profile'] in PROFILES else row['agent_profile'],
        'deleting': bool(row['delete_requested']),
        'switch_agents': [{'id': p.id, 'name': p.name} for p in PROFILES.values()
                          if p.accepted and p.id != row['agent_profile']
                          and (request.app.state.demo or row['isolation'] == 'host')],
        'agent_capabilities': loads(row['agent_caps_json']), 'turns': turns,
        'pending_requests': [asdict(event) for event in supervisor.events.pending(task_id).values()],
    })


@router.delete('/tasks/{task_id}', status_code=202)
async def delete_task(request: Request, task_id: str) -> dict[str, str]:
    _supervisor(request).delete(task_id)
    return {'status': 'deleting'}


@router.post('/tasks/{task_id}/messages', status_code=202)
async def message(request: Request, task_id: str, body: MessageInput) -> dict[str, str]:
    if not body.text.strip():
        raise TaskError('invalid_message', 'Enter a message.', 422)
    interrupt = body.delivery == 'interrupt'
    if not request.app.state.demo and body.text.strip() in APPLY_PHRASES:
        turn = request.app.state.reviews.apply(task_id, None, body.client_message_id, interrupt=interrupt)
    else:
        turn = _supervisor(request).send(task_id, body.text, body.client_message_id, interrupt=interrupt)
    return {'turn_id': turn['id'], 'status': 'queued'}


@router.post('/tasks/{task_id}/switch-agent', status_code=202)
async def switch_agent(request: Request, task_id: str, body: SwitchAgentInput) -> dict[str, str]:
    turn = _supervisor(request).send(task_id, CONTINUE_PROMPT, body.client_message_id,
                                     kind='recap', switch_to=body.agent)
    return {'turn_id': turn['id'], 'status': turn['status']}


@router.post('/tasks/{task_id}/interrupt', status_code=202)
async def interrupt(request: Request, task_id: str) -> dict[str, bool]:
    return {'interrupted': _supervisor(request).interrupt(task_id)}


@router.post('/tasks/{task_id}/continue', status_code=202)
async def continue_task(request: Request, task_id: str) -> dict[str, str]:
    turn = _supervisor(request).continue_task(task_id)
    if turn is None:
        return {'status': 'preparing'}
    return {'turn_id': turn['id'], 'status': 'queued'}


@router.post('/tasks/{task_id}/requests/{request_id}/respond')
async def respond(request: Request, task_id: str, request_id: str, body: RequestInput) -> dict[str, str]:
    if (body.option_id is None) == (body.answers is None):
        raise TaskError('invalid_response', 'Provide a permission option or answers.', 422)
    supervisor = _supervisor(request)
    await supervisor.respond(task_id, request_id, RequestResponse(
        option_id=body.option_id, answers=supervisor.store.sanitize(body.answers)))
    return {'status': 'accepted'}


@router.get('/tasks/{task_id}/events')
async def events(request: Request, task_id: str, after: Annotated[int, Query(ge=0, le=CURSOR_MAX)] = 0,
                 limit: Annotated[int, Query(ge=1, le=200)] = 200) -> dict[str, Any]:
    supervisor = _supervisor(request)
    supervisor.store.get(task_id)
    page = supervisor.events.history(task_id, after, limit=limit)
    return {'events': [asdict(event) for event in page], 'next_after': page[-1].seq if page else after}


@router.get('/tasks/{task_id}/stream')
async def stream(request: Request, task_id: str, last_event_id: Annotated[str | None, Header()] = None) -> StreamingResponse:
    supervisor = _supervisor(request)
    supervisor.store.get(task_id)
    cursor = 0
    if last_event_id:
        if len(last_event_id) > 19 or not last_event_id.isascii() or not last_event_id.isdecimal():
            raise TaskError('invalid_event_cursor', 'Use a nonnegative event sequence.', 422)
        cursor = int(last_event_id)
        if cursor > CURSOR_MAX:
            raise TaskError('invalid_event_cursor', 'Use a nonnegative event sequence.', 422)
    return StreamingResponse(supervisor.events.stream(task_id, cursor), media_type='text/event-stream',
                             headers={'X-Accel-Buffering': 'no'})


@router.get('/tasks/{task_id}/diff')
async def diff(request: Request, task_id: str) -> dict[str, Any]:
    return _supervisor(request).store.diff(task_id)


@router.post('/tasks/{task_id}/issue/refresh', status_code=202)
async def refresh_issue(request: Request, task_id: str) -> dict[str, str]:
    _supervisor(request).refresh_issue(task_id)
    return {'status': 'refreshing'}


@router.get('/tasks/{task_id}/files')
async def files(request: Request, task_id: str, path: Annotated[str, Query(min_length=1, max_length=4096)]) -> dict[str, str]:
    return await _supervisor(request).read_file(task_id, path)
