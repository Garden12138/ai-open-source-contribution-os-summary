"""Authenticated discovery reads, marks and background scan progress."""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from contribos.discovery.service import DiscoveryService
from contribos.tasks.store import TaskError

router = APIRouter(prefix='/api')


def service(request: Request) -> DiscoveryService:
    return request.app.state.supervisor.discovery


class ScanInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str = Field(min_length=3, max_length=140,
                            pattern=r'^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$')


class MarkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mark: Literal['starred', 'ignored'] | None


@router.post('/scans', status_code=202)
async def start_scan(request: Request, body: ScanInput | None = None) -> dict:
    if request.app.state.demo:
        raise TaskError('offline_demo', 'GitHub discovery requires the online service.', 409)
    repository = body.repository if body else None
    if repository and service(request).store.clean(repository) != repository:
        raise TaskError('invalid_repository', 'Provide a public repository name.', 422)
    return service(request).start(repository)


@router.get('/scans/latest')
async def latest_scan(request: Request) -> dict | None:
    discovery = service(request)
    row = discovery.db.execute('SELECT id FROM scan_runs ORDER BY started_at DESC LIMIT 1').fetchone()
    return discovery.scan(row['id']) if row else None


@router.get('/scans/{run_id}')
async def scan_status(request: Request, run_id: str) -> dict:
    return service(request).scan(run_id)


@router.get('/scans/{run_id}/stream')
async def scan_stream(request: Request, run_id: str) -> StreamingResponse:
    discovery = service(request)
    discovery.scan(run_id)

    async def events():
        previous = None
        while True:
            scan = discovery.scan(run_id)
            if scan != previous:
                yield f'data: {json.dumps(scan)}\n\n'
                previous = scan
            if scan['status'] != 'running':
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type='text/event-stream')


@router.get('/opportunities')
async def opportunities(request: Request, view: Literal['daily', 'all', 'starred', 'ignored'] = 'all',
                        lang: str = '', label: str = '', q: str = '', bounty: bool | None = None,
                        small: bool = False, sort: Literal['score', 'updated', 'stars'] = 'score',
                        offset: Annotated[int, Query(ge=0, le=2**31-1)] = 0,
                        limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict:
    items = service(request).opportunities(view=view, lang=lang, label=label, q=q, bounty=bounty,
                                          small=small, sort=sort, offset=offset, limit=limit + 1)
    return {'opportunities': items[:limit], 'more': len(items) > limit}


@router.get('/opportunities/{opportunity_id}')
async def opportunity(request: Request, opportunity_id: str) -> dict:
    return service(request).detail(opportunity_id)


@router.put('/opportunities/{opportunity_id}/mark')
async def mark(request: Request, opportunity_id: str, body: MarkInput) -> dict:
    return service(request).mark(opportunity_id, body.mark)
