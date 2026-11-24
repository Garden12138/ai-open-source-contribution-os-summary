"""Structured results at the end of an ACP turn."""

import json
import re
from typing import Any


def final_json(text: str) -> dict[str, Any] | None:
    fences = re.split(r'(?im)^\s*```json[ \t]*\r?\n', text)
    if len(fences) == 1:
        return None
    match = re.fullmatch(r'(.*?)\r?\n[ \t]*```[ \t\r\n]*', fences[-1], re.DOTALL)
    if match:
        try:
            value = json.loads(match[1])
            if isinstance(value, dict):
                return value
        except (ValueError, RecursionError):
            pass
    return None
