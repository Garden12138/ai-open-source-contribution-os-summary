"""Validated, non-secret local preferences."""

from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from contribos.agent.profiles import PROFILES
from contribos.discovery.domain import DiscoveryRules
from contribos.tasks.store import TaskError

router = APIRouter(prefix='/api/settings')
TOOLS = tuple(dict.fromkeys([
    'git', 'gh', 'node', 'sbx', 'docker',
    *(command[0] for profile in PROFILES.values() for command in (profile.command, profile.auth_check, profile.login_command)),
]))
NotifyEvent = Literal['turn_completed', 'request_waiting', 'turn_error', 'publish_result', 'daily_recommendations']
Owner = Annotated[str, Field(pattern=r'^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$')]


class ResourceLimits(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cpus: int = Field(default=2, strict=True, ge=1, le=1024)
    memory_mib: int = Field(default=4096, strict=True, ge=1, le=2**31 - 1)
    disk_mib: int = Field(default=10240, strict=True, ge=1, le=2**31 - 1)


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')

    scan_time: str = Field(default='09:00', pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    preferred_languages: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=lambda: list(DiscoveryRules().preferred_languages), max_length=30)
    strategic_keywords: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=lambda: list(DiscoveryRules().strategic_keywords), max_length=30)
    trusted_owners: list[Owner] = Field(default_factory=list, max_length=1000)
    default_agent: str = 'codex'
    review_agent: str | None = None
    sandbox_backend: Literal['sbx', 'container'] = 'sbx'
    first_turn_policy: Literal['plan', 'implement'] = 'plan'
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    concurrency: int = Field(default=2, strict=True, ge=1, le=64)
    tool_paths: dict[str, str] = Field(default_factory=dict)
    git_author_name: str = Field(default='', max_length=128, pattern=r'^[^\x00-\x1f<>]*$')
    git_author_email: str = Field(default='', max_length=320, pattern=r'^(?:[^\s<>@]+@[^\s<>@]+)?$')
    git_dco_signoff: StrictBool = False
    notifications: dict[NotifyEvent, StrictBool] = Field(default_factory=lambda: dict.fromkeys(get_args(NotifyEvent), True))

    @field_validator('trusted_owners')
    @classmethod
    def normalize_owners(cls, owners: list[str]) -> list[str]:
        return list(dict.fromkeys(owner.lower() for owner in owners))

    @field_validator('default_agent', 'review_agent')
    @classmethod
    def validate_agent(cls, agent: str | None) -> str | None:
        if agent is not None and agent not in PROFILES:
            raise ValueError('Unknown Agent.')
        return agent

    @field_validator('tool_paths')
    @classmethod
    def validate_paths(cls, paths: dict[str, str]) -> dict[str, str]:
        for name, path in paths.items():
            if name not in TOOLS:
                raise ValueError('Unknown CLI tool.')
            if path and (not Path(path).is_absolute() or len(path) > 4096 or any(ord(char) < 32 for char in path)):
                raise ValueError('Tool paths must be absolute.')
        return {key: path for key, path in paths.items() if path}


DEFAULTS = Preferences().model_dump()


@router.get('')
async def get_settings(request: Request) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    store = supervisor.store
    settings = {key: store.setting(key, value) for key, value in DEFAULTS.items()}
    settings['notifications'] = {**DEFAULTS['notifications'], **settings['notifications']}
    settings['resource_limits'] = {**DEFAULTS['resource_limits'], **settings['resource_limits']}
    return store.sanitize({
        **settings,
        'available_isolation': list(supervisor.sandboxes),
        'tools': TOOLS,
        'agent_profiles': [{'id': profile.id, 'name': profile.name} for profile in PROFILES.values()],
    })


@router.put('')
async def save_settings(request: Request, body: Preferences) -> dict[str, Any]:
    store = request.app.state.supervisor.store
    values = {name: getattr(body, name) for name in body.model_fields_set}
    if 'resource_limits' in values:
        previous = store.setting('resource_limits', DEFAULTS['resource_limits'])
        values['resource_limits'] = {**previous, **body.resource_limits.model_dump(exclude_unset=True)}
    if 'notifications' in values:
        values['notifications'] = {**store.setting('notifications', DEFAULTS['notifications']), **body.notifications}
    if store.sanitize(values) != values:
        raise TaskError('invalid_settings', 'Settings must not contain secrets.', 422)
    with store.transaction():
        for key, value in values.items():
            store.set_setting(key, value)
    if 'tool_paths' in values:
        paths = request.app.state.settings.tool_paths
        paths.clear()
        paths.update(body.tool_paths)
    return await get_settings(request)
