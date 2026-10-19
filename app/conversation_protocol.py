"""Pure turn protocol: read requests cannot become workflow decisions."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

REASONING_VERSION = "conversation-reasoning-v3"
LEGACY_REASONING_VERSION = "conversation-reasoning-v2"
REASONING_VERSIONS = {REASONING_VERSION, LEGACY_REASONING_VERSION}
EvidenceName = Literal["task", "plan", "changes", "tests", "review", "publication_document",
                       "publication_preview", "ready_preview", "pull_request", "publisher_account", "repository_file"]
Action = Literal["respond", "discuss_plan", "revise_plan", "resume_work", "prepare_publication",
                 "renew_publication_preview", "revise_document", "followup_pr", "prepare_ready_for_review", "read_cla",
                 "check_cla", "refresh_repository", "stop_work"]


class EvidenceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: EvidenceName
    path: str | None = Field(default=None, max_length=300)
    query: str | None = Field(default=None, max_length=200)
    start_line: int = Field(default=1, ge=1, le=1_000_000)


class TurnEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(max_length=6000)
    action: Action
    reads: list[EvidenceRead] = Field(max_length=3)
    references: list[EvidenceName] = Field(max_length=3)


class ConversationDecision(TurnEnvelope):
    @model_validator(mode="after")
    def require_response_or_reads(self) -> ConversationDecision:
        if self.reads and self.action != "respond":
            raise PydanticCustomError("read_workflow_conflict", "Read requests cannot dispatch a workflow")
        if self.reads and self.references:
            raise PydanticCustomError("references_before_read", "Finish reading before citing evidence")
        if not self.reads and not self.reply.strip():
            raise PydanticCustomError("reply_required", "A final decision requires a user-facing reply")
        return self


class TurnProtocolError(ValueError):
    def __init__(self, code: str, instruction: str) -> None:
        super().__init__(instruction)
        self.code, self.instruction = code, instruction


def parse_step(raw: str, *, version: str, actions: list[str], known: set[str],
               allow_reads: bool) -> tuple[ConversationDecision, bool]:
    """Ignore presentation on a read-only request, never infer an action."""
    envelope = TurnEnvelope.model_validate_json(raw)
    normalized = False
    if version == REASONING_VERSION and envelope.reads:
        if envelope.action != "respond":
            raise TurnProtocolError("read_workflow_conflict", "Return a read-only request with action respond; do not mix a workflow with reads.")
        if not allow_reads:
            raise TurnProtocolError("reads_after_budget", "No model calls remain after this one. Answer from tool_results with reads:[]; state any evidence limitation.")
        requested = {r.name for r in envelope.reads}
        if not set(envelope.references) <= known | requested:
            raise TurnProtocolError("reference_not_read", "References must name evidence already read, or requested by this read-only step.")
        normalized = bool(envelope.reply or envelope.references)
        envelope.reply, envelope.references = "", []
    decision = ConversationDecision.model_validate(envelope.model_dump())
    if decision.action not in actions:
        raise TurnProtocolError("action_unavailable", "Choose an action from available_actions. No workflow has been dispatched.")
    if not set(decision.references) <= known:
        raise TurnProtocolError("reference_not_read", "Use only reference_names_available_now; otherwise request the evidence first.")
    return decision, normalized


def step_schema(actions: list[str], known: set[str], *, allow_reads: bool) -> dict:
    """Expose the actual phase/budget constraints to structured-output providers."""
    schema = deepcopy(TurnEnvelope.model_json_schema())
    schema["properties"]["action"] = {"type":"string", "enum":actions}
    if known:
        schema["properties"]["references"]["items"] = {"type":"string", "enum":sorted(known)}
    else:
        schema["properties"]["references"]["maxItems"] = 0
    if allow_reads:
        schema["anyOf"] = [
            {"properties":{"reads":{"maxItems":0}, "reply":{"minLength":1}}},
            {"properties":{"action":{"const":"respond"}, "reads":{"minItems":1},
                           "reply":{"maxLength":0}, "references":{"maxItems":0}}},
        ]
    else:
        schema["properties"]["reads"]["maxItems"] = 0
        schema["properties"]["reply"]["minLength"] = 1
    return schema
