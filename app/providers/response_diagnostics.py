"""Allowlisted response facts; never persist provider text or private reasoning."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


REASONS = frozenset({"choices_invalid", "usage_unverified", "finish_unverified", "output_truncated", "final_content_missing"})
FINISHES = frozenset({"stop", "tool_calls", "length", "content_filter", "insufficient_system_resource", "other", "missing"})
ENVELOPES = frozenset({"absent", "not_list", "multiple_calls", "call_invalid", "function_invalid", "name_mismatch",
    "arguments_missing", "arguments_blank", "arguments_invalid", "arguments_text", "arguments_object"})


@dataclass(frozen=True, slots=True)
class CompletionResponseFacts:
    reason: str
    choice_count: int | None
    finish: str
    final_text_present: bool
    tool_calls_present: bool
    reasoning_present: bool
    tool_envelope: str | None = None
    tool_call_count: int | None = None

    def __post_init__(self) -> None:
        if (type(self.reason) is not str or self.reason not in REASONS or type(self.finish) is not str or self.finish not in FINISHES
                or self.choice_count is not None and (type(self.choice_count) is not int or not 0 <= self.choice_count <= 1024)
                or any(type(v) is not bool for v in (self.final_text_present, self.tool_calls_present, self.reasoning_present))
                or self.tool_envelope is not None and (type(self.tool_envelope) is not str or self.tool_envelope not in ENVELOPES)
                or self.tool_call_count is not None and (type(self.tool_call_count) is not int or not 0 <= self.tool_call_count <= 1024)
                or self.tool_envelope is None and self.tool_call_count is not None):
            raise ValueError("invalid_completion_response_facts")

    def to_wire(self) -> dict:
        value = asdict(self)
        if self.tool_envelope is None:
            value.pop("tool_envelope")
            value.pop("tool_call_count")
        return {"version": "completion-response-facts-v2" if self.tool_envelope is not None else "completion-response-facts-v1", **value}


def completion_response_facts(value: Mapping[str, Any], reason: str, *, expected_tool_name: str | None = None) -> CompletionResponseFacts:
    """Project bounded booleans/enums only, even for malformed or hostile fields."""
    choices = value.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping) else {}
    finish = choice.get("finish_reason")
    finish = finish if isinstance(finish, str) and finish in FINISHES - {"missing", "other"} else "missing" if finish is None else "other"
    message = choice.get("message")
    message = message if isinstance(message, Mapping) else {}
    def present(key: str) -> bool:
        return isinstance(message.get(key), str) and bool(message[key].strip())
    calls = message.get("tool_calls")
    envelope, count = None, None
    if expected_tool_name is not None:
        count = len(calls) if isinstance(calls, list) and len(calls) <= 1024 else None
        if calls is None or calls == []: envelope = "absent"
        elif not isinstance(calls, list): envelope = "not_list"
        elif len(calls) != 1: envelope = "multiple_calls"
        elif not isinstance(calls[0], Mapping): envelope = "call_invalid"
        else:
            function = calls[0].get("function")
            if not isinstance(function, Mapping): envelope = "function_invalid"
            elif function.get("name") != expected_tool_name: envelope = "name_mismatch"
            elif "arguments" not in function: envelope = "arguments_missing"
            elif isinstance(function["arguments"], str): envelope = "arguments_text" if function["arguments"].strip() else "arguments_blank"
            elif isinstance(function["arguments"], Mapping): envelope = "arguments_object"
            else: envelope = "arguments_invalid"
    return CompletionResponseFacts(reason, len(choices) if isinstance(choices, list) and len(choices) <= 1024 else None,
        finish, present("content"), isinstance(calls, list) and bool(calls), present("reasoning_content"), envelope, count)
