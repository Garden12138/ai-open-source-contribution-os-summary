"""Local contribution history and background PR refresh requests."""

from fastapi import APIRouter, Request

from contribos.tasks.store import TaskError
from contribos.tracker import PRTracker

router = APIRouter(prefix='/api')


@router.get('/stats')
async def stats(request: Request) -> dict:
    return request.app.state.supervisor.tracker.stats()


def online_tracker(request: Request) -> PRTracker:
    if request.app.state.demo:
        raise TaskError('offline_demo', 'Refreshing a pull request requires the online service.')
    return request.app.state.supervisor.tracker


@router.post('/tasks/{task_id}/pr/refresh', status_code=202)
async def refresh_task_pr(request: Request, task_id: str) -> dict:
    online_tracker(request).request(task_id=task_id)
    return {'status': 'refreshing'}


@router.post('/pull-requests/{pr_id}/refresh', status_code=202)
async def refresh_history_pr(request: Request, pr_id: int) -> dict:
    online_tracker(request).request(pr_id=pr_id)
    return {'status': 'refreshing'}
