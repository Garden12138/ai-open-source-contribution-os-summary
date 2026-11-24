"""Background review and explicit application of findings."""

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from contribos.review.service import ReviewService
from contribos.tasks.store import TaskError

router = APIRouter(prefix='/api/tasks/{task_id}')


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    agent: str | None = Field(default=None, min_length=1, max_length=100)


def reviews(request: Request) -> ReviewService:
    if request.app.state.demo:
        raise TaskError('review_unavailable', 'Independent review is unavailable in the offline demo.', 503)
    return request.app.state.reviews


@router.post('/review', status_code=202)
async def start(review_request: Request, task_id: str, body: ReviewInput | None = None) -> dict[str, str]:
    review_id = reviews(review_request).start(task_id, body.agent if body else None)
    return {'review_id': review_id, 'status': 'running'}


@router.get('/reviews')
async def history(request: Request, task_id: str) -> dict[str, list[dict[str, Any]]]:
    return {'reviews': reviews(request).history(task_id)}


@router.post('/reviews/{review_id}/apply', status_code=202)
async def apply(request: Request, task_id: str, review_id: str) -> dict[str, str]:
    turn = reviews(request).apply(task_id, review_id)
    return {'turn_id': turn['id'], 'status': 'queued'}
