"""Failed model calls keep reservations; measured usage is separate evidence."""
from __future__ import annotations

from app.providers.contracts import ProviderRunError


def provider_failure_receipt(error: ProviderRunError, *, input_bound: int, output_limit: int) -> dict:
    reported = None if error.usage is None else {
        "input_tokens": error.usage.input_tokens, "output_tokens": error.usage.output_tokens}
    status = "unreported"
    if reported is not None:
        if not all(type(v) is int and v >= 0 for v in reported.values()):
            reported, status = None, "reported_invalid"
        else:
            status = "reported_within_reservation" if (
                reported["input_tokens"] <= input_bound and reported["output_tokens"] <= output_limit) else "reported_exceeds_reservation"
    return {"version": "agent-provider-failure-v1",
        "reservation": {"input_tokens": input_bound, "output_tokens": output_limit},
        "reported_usage": reported, "usage_status": status, "reservation_retained": True,
        "response_facts": error.diagnostic.to_wire() if error.diagnostic else None}
