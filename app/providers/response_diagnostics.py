"""Allowlisted response facts; never persist provider text or private reasoning."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


REASONS = frozenset({"choices_invalid", "usage_unverified", "finish_unverified", "output_truncated", "final_content_missing"})
FINISHES = frozenset({"stop", "tool_calls", "length", "content_filter", "insufficient_system_resource", "other", "missing"})


@dataclass(frozen=True, slots=True)
class CompletionResponseFacts:
    reason: str
    choice_count: int | None
    finish: str
    final_text_present: bool
    tool_calls_present: bool
    reasoning_present: bool

    def __post_init__(self) -> None:
        if (type(self.reason) is not str or self.reason not in REASONS or type(self.finish) is not str or self.finish not in FINISHES
                or self.choice_count is not None and (type(self.choice_count) is not int or not 0 <= self.choice_count <= 1024)
                or any(type(v) is not bool for v in (self.final_text_present, self.tool_calls_present, self.reasoning_present))):
            raise ValueError("invalid_completion_response_facts")

    def to_wire(self) -> dict:
        return {"version": "completion-response-facts-v1", **asdict(self)}


def completion_response_facts(value: Mapping[str, Any], reason: str) -> CompletionResponseFacts:
    """Project bounded booleans/enums only, even for malformed or hostile fields."""
    choices = value.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping) else {}
    finish = choice.get("finish_reason")
    finish = finish if isinstance(finish, str) and finish in FINISHES - {"missing", "other"} else "missing" if finish is None else "other"
    message = choice.get("message")
    message = message if isinstance(message, Mapping) else {}
    def present(key: str) -> bool:
        return isinstance(message.get(key), str) and bool(message[key].strip())
    return CompletionResponseFacts(reason, len(choices) if isinstance(choices, list) and len(choices) <= 1024 else None,
        finish, present("content"), isinstance(message.get("tool_calls"), list) and bool(message["tool_calls"]), present("reasoning_content"))
