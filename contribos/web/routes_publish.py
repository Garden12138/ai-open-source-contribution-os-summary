"""Browser confirmation and read-only status for Draft PR publications."""

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from contribos.publish.service import Publisher
from contribos.security import SESSION_COOKIE
from contribos.tasks.store import TaskError

router = APIRouter(prefix='/api/tasks/{task_id}/publish')


class PreviewText(BaseModel):
    model_config = ConfigDict(extra='forbid')

    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=65536)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    kind: Literal['create_pr', 'push_followup', 'update_pr', 'mark_ready'] = 'create_pr'


class Confirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')

    preview_hash: str = Field(pattern=r'\A[0-9a-f]{64}\z')


def publisher(request: Request) -> Publisher:
    if request.app.state.demo:
        raise TaskError('publish_unavailable', 'Publishing is unavailable in the offline demo.', 503)
    return request.app.state.publisher


def interactive(request: Request) -> Publisher:
    session = request.cookies.get(SESSION_COOKIE, '')
    if request.headers.get('authorization') or not request.app.state.auth.valid_session(session):
        raise TaskError('publish_confirmation_required', 'Confirm publishing in the ContribOS interface.', 403)
    return publisher(request)


@router.get('')
async def publication(request: Request, task_id: str) -> dict[str, Any]:
    return publisher(request).view(task_id)


@router.post('/preview', status_code=202)
async def prepare(request: Request, task_id: str, body: PreviewRequest | None = None) -> dict[str, str]:
    publisher(request).prepare(task_id, body.kind if body else 'create_pr')
    return {'status': 'preparing'}


@router.patch('/preview/{preview_id}')
async def edit(request: Request, task_id: str, preview_id: str, body: PreviewText) -> dict[str, Any]:
    return publisher(request).edit(task_id, preview_id, body.title, body.body)


@router.post('/confirm', status_code=202)
async def confirm(request: Request, task_id: str, body: Confirmation) -> dict[str, str]:
    interactive(request).confirm(task_id, body.preview_hash)
    return {'status': 'confirmed'}


@router.post('/reconcile', status_code=202)
async def reconcile(request: Request, task_id: str, body: Confirmation) -> dict[str, str]:
    interactive(request).confirm(task_id, body.preview_hash, reconcile=True)
    return {'status': 'confirmed'}
