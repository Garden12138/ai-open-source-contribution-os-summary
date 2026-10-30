"""Bound new tool delivery before recording what a model actually receives."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from app.workbench import WorkbenchError

VERSION = "context-read-delivery-v1"
HARD_PROMPT_BYTES = 96_000
MIN_PAGE_BYTES = 256
MAX_DELIVERY_PASSES = 10


def limit_context_pages(results: list[dict], maximum_text_bytes: int) -> list[dict]:
    """Fair UTF-8 prefixes with offsets into the original serialized reference."""
    raw = [result["text"].encode() for result in results]
    minima = [min(len(value), MIN_PAGE_BYTES) for value in raw]
    if type(maximum_text_bytes) is not int or maximum_text_bytes < sum(minima):
        raise WorkbenchError("规划上下文已达上限", code="planning_context_limit")
    if maximum_text_bytes >= sum(map(len, raw)):
        return deepcopy(results)
    capacities = [len(value)-minimum for value, minimum in zip(raw, minima)]
    extra = maximum_text_bytes-sum(minima)
    shares = [extra*capacity//sum(capacities) for capacity in capacities]
    remainder = extra-sum(shares)
    for index, capacity in enumerate(capacities):
        if remainder and shares[index] < capacity:
            shares[index] += 1
            remainder -= 1
    delivered = deepcopy(results)
    for result, value, minimum, share in zip(delivered, raw, minima, shares):
        if minimum+share >= len(value):
            continue
        text = value[:minimum+share].decode("utf-8", errors="ignore")
        count = len(text.encode())
        result.update(text=text, partial=True, next_offset=result["offset"]+count,
            delivery={"version":VERSION,"limited":True,"initial_page_bytes":len(value),"delivered_bytes":count})
        if result.get("read_request"):
            result["delivery"]["continuation"] = {key: item for key, item in result["read_request"].items()
                if key != "reference_id"}
            result["delivery"]["continuation"]["offset"] = result["next_offset"]
        if result.get("source_complete") is not None:
            result["source_complete"] = False
    return delivered


def fit_context_pages(results: list[dict], preview: Callable[[list[dict]], str]) -> tuple[list[dict], dict]:
    """Only new, not-yet-delivered pages may shrink; model/read caps stay fixed."""
    initial = sum(len(result["text"].encode()) for result in results)
    minimum = sum(min(len(result["text"].encode()), MIN_PAGE_BYTES) for result in results)
    allowance = initial
    first_size = None
    for attempt in range(MAX_DELIVERY_PASSES):
        pages = limit_context_pages(results, allowance)
        size = len(preview(pages).encode())
        if first_size is None:
            first_size = size
        if size <= HARD_PROMPT_BYTES:
            return pages, {"version":VERSION,"limited":allowance<initial,"preview_passes":attempt+1,
                "original_prompt_bytes":first_size,"prompt_bytes":size,"initial_page_bytes":initial,
                "delivered_page_bytes":sum(len(result["text"].encode()) for result in pages)}
        if allowance == minimum:
            break
        allowance = (minimum if attempt == MAX_DELIVERY_PASSES-2 else
            max(minimum, allowance-max(size-HARD_PROMPT_BYTES+2048, allowance//4, 1)))
    raise WorkbenchError("规划上下文已达上限", code="planning_context_limit")
