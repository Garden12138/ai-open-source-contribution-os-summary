from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.provenance import canonical_json, content_hash


def test_content_hash_is_canonical_for_mapping_and_sequence_values() -> None:
    left = {
        "timestamp": datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc),
        "nested": {"b": 2, "a": 1},
        "values": ("one", "two"),
    }
    right = {
        "values": ["one", "two"],
        "nested": {"a": 1, "b": 2},
        "timestamp": "2026-07-30T10:00:00+00:00",
    }

    assert canonical_json(left) == canonical_json(right)
    assert content_hash(left) == content_hash(right)
    assert len(content_hash(left)) == 64


def test_content_hash_rejects_non_json_and_non_finite_values() -> None:
    with pytest.raises(TypeError, match="Unsupported provenance value"):
        content_hash({"unsupported": object()})

    with pytest.raises(ValueError, match="Out of range float"):
        content_hash({"invalid": float("nan")})
