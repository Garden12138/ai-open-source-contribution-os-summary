"""The boundary between task orchestration and an existing coding agent."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from acp.schema import TextContentBlock, ImageContentBlock, AudioContentBlock, ResourceContentBlock, EmbeddedResourceContentBlock

ContentBlock = TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
StopReason = Literal["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"]


@dataclass(frozen=True)
class AgentCapabilities:
    load_session: bool = False
    resume_session: bool = False
    modes: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    prompt_types: tuple[str, ...] = ("text",)
    auth_methods: tuple[str, ...] = ()


@dataclass
class TaskContext:
    task_id: str
    repo_full_name: str
    issue_number: int
    repo_dir: str
    context_dir: str
    session_id: str | None = None
    isolation: Literal["host", "sbx", "container"] = "host"
    trust_confirmed: bool = False
    default_branch: str = ""
    base_sha: str = ""
    work_branch: str = ""
    issue: dict[str, Any] = field(default_factory=dict)
    linked_prs: list[dict[str, Any]] = field(default_factory=list)
    instructions: str = ""
    recap: str = ""
    sandbox_ref: str | None = None
    recap_pending: bool = False


@dataclass(frozen=True)
class EngineEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def persistent(self) -> bool:
        return self.type not in {"message.delta", "reasoning.delta"}


@dataclass(frozen=True)
class TurnResult:
    stop_reason: StopReason


@dataclass(frozen=True)
class RequestResponse:
    option_id: str | None = None
    answers: dict[str, Any] | None = None
    cancelled: bool = False


def validate_answers(schema: dict[str, Any], answers: object) -> None:
    properties = schema.get('properties') or {}
    if not isinstance(answers, dict) or not all(key in answers for key in (schema.get('required') or [])):
        raise ValueError('Missing required answers')
    types = {'string': str, 'boolean': bool, 'integer': int, 'number': (int, float), 'array': list}
    for name, value in answers.items():
        spec = properties.get(name, {})
        kind = spec.get('type')
        expected = types.get(kind)
        if expected and (not isinstance(value, expected) or isinstance(value, bool) and kind in {'integer', 'number'}):
            raise ValueError('Invalid answer type')
        options = spec.get('enum')
        if spec.get('oneOf'):
            options = [item['const'] for item in spec['oneOf']]
        if options is not None and value not in options:
            raise ValueError('Invalid answer option')


class AgentError(RuntimeError):
    """Only a public error code and a fixed safe message may cross this boundary."""

    def __init__(self, code: str, message: str = "Agent request failed.", *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


@runtime_checkable
class AgentEngine(Protocol):
    capabilities: AgentCapabilities

    async def ensure_session(self, task: TaskContext) -> str: ...
    async def prompt(self, session_id: str, blocks: list[ContentBlock]) -> TurnResult: ...
    async def cancel(self, session_id: str) -> None: ...
    async def respond(self, request_id: str, response: RequestResponse) -> None: ...
    async def set_mode(self, session_id: str, mode: str) -> None: ...
    def events(self, session_id: str) -> AsyncIterator[EngineEvent]: ...
    async def close(self) -> None: ...
