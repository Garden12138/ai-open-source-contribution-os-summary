"""Allowlisted planning validation diagnostics; never expose model input."""

from pydantic import ValidationError

MESSAGES = {
    "planning_outcome_missing": "规划模型返回了空回复，未提供说明、问题、补读文件或方案。上下文已保留。",
    "planning_outcome_conflict": "规划模型同时返回了多个处理分支，无法确定是提问、补读文件还是提交方案。上下文已保留。",
    "planning_question_ids_duplicate": "规划模型返回的问题编号重复，未保存该回复。上下文已保留。",
    "planning_sensitive_output": "规划模型回复含疑似凭证内容，已拒绝保存。上下文已保留。",
    "planning_output_invalid": "规划模型回复字段或 JSON 格式未通过校验，现有上下文已保留。",
}
SAFE_TYPES = frozenset(MESSAGES) | {
    "missing",
    "extra_forbidden",
    "json_invalid",
    "model_type",
    "model_attributes_type",
    "string_type",
    "string_too_short",
    "string_too_long",
    "string_pattern_mismatch",
    "list_type",
    "dict_type",
    "too_short",
    "too_long",
    "int_type",
    "int_parsing",
    "float_type",
    "float_parsing",
    "literal_error",
    "value_error",
    "greater_than_equal",
    "less_than_equal",
    "bool_type",
    "bool_parsing",
}
SAFE_FIELDS = frozenset(
    {
        "reply",
        "questions",
        "read_paths",
        "read_context",
        "reference_id",
        "offset",
        "max_bytes",
        "query",
        "start_line",
        "max_lines",
        "plan",
        "id",
        "prompt",
        "options",
        "parent_version_id",
        "goal",
        "acceptance_criteria",
        "files_to_inspect",
        "files_likely_to_change",
        "implementation_steps",
        "tests_to_add_or_run",
        "commands_to_run",
        "command_id",
        "purpose",
        "argv",
        "working_directory",
        "risks",
        "questions_for_maintainer",
        "baseline_expectation",
        "version",
        "issue_url",
        "test_node",
        "failure_literal",
        "exit_code",
    }
)


def planning_read_bounds_error(details: dict) -> bool:
    """Only numeric page-bound failures may use the existing v2 repair slots."""
    fields, types = details.get("fields"), details.get("validation_types")
    return (details.get("reason_code") == "planning_output_invalid"
        and isinstance(fields, list) and bool(fields) and all(isinstance(v, str) for v in fields)
        and set(fields) <= {"read_context.max_lines", "read_context.max_bytes", "read_context.offset", "read_context.start_line"}
        and isinstance(types, list) and bool(types) and all(isinstance(v, str) for v in types)
        and set(types) <= {"less_than_equal", "greater_than_equal"})


def planning_read_bounds_repair(details: dict, *, remaining_rounds: int) -> dict:
    """Tell the model the real schema bounds; never clamp or execute bad intent."""
    from app.context_compaction import ContextRead

    properties = ContextRead.model_json_schema()["properties"]
    bounds = {}
    for field in ("max_lines", "max_bytes", "offset", "start_line"):
        prop = properties[field]
        numeric = next((v for v in prop.get("anyOf", [prop]) if v.get("type") == "integer"), {})
        bounds[field] = {k: numeric[k] for k in ("minimum", "maximum") if k in numeric}
    return {**{k: details[k] for k in ("reason_code", "fields", "validation_types")},
        "constraint": "context_read_parameter_bounds", "context_read_bounds": bounds,
        "read_rounds_remaining": remaining_rounds,
        "instruction": ("No read rounds remain. Return a complete evidence-based plan or material questions; do not request reads."
            if remaining_rounds == 0 else
            "Rejected reads were not executed. Correct the listed page parameters within these bounds, using issued references. Do not infer omitted source. Existing read, repair and model budgets still apply.")}


def planning_domain_details(error: ValueError) -> dict:
    """Map only our fixed validator messages; never echo rejected values."""
    message = str(error)
    if message == "Expected reproduction must target an exact approved test node":
        return {"reason_code": "planning_output_invalid", "validation_types": ["value_error"],
            "fields": ["commands_to_run.baseline_expectation.test_node"],
            "constraint": "expected_reproduction_exact_target",
            "remediation": "Use an existing exact test node verbatim in argv, or omit baseline_expectation for a check required to pass. Never invent a target or treat the field as phase scheduling."}
    names = {
        "Plan goal": "goal", "Plan command ID": "commands_to_run.command_id",
        "Plan command purpose": "commands_to_run.purpose",
        "Plan command argv": "commands_to_run.argv",
        "Plan command working directory": "commands_to_run.working_directory",
        "Plan commands": "commands_to_run",
        **{name.replace("_", " "): name for name in SAFE_FIELDS},
    }
    field = next((field for prefix, field in names.items()
                  if message.startswith(prefix + " ")), "plan")
    constraint = ("unsafe_path" if "repository" in message else
                  "duplicate_or_count" if "unique" in message else "invalid_value")
    return {"reason_code": "planning_output_invalid", "validation_types": ["value_error"],
            "fields": [field], "constraint": constraint}


def planning_validation_details(error: ValidationError) -> dict:
    codes, fields = set(), set()
    for item in error.errors(
        include_input=False, include_context=False, include_url=False
    ):
        codes.add(
            item["type"] if item["type"] in SAFE_TYPES else "unknown_validation_error"
        )
        # Never copy arbitrary model-supplied keys, discriminator labels or indexes.
        parts = [part for part in item["loc"] if isinstance(part, str)]
        prefix = []
        for part in parts:
            if part not in SAFE_FIELDS:
                break
            prefix.append(part)
        # Report the known enclosing field for a rejected nested key, without
        # copying arbitrary model-supplied keys or list indexes into diagnostics.
        fields.add(".".join(prefix) if prefix and (len(prefix) == len(parts) or len(prefix) >= 2)
                   else "response")
    reason = next(
        (
            code
            for code in (
                "planning_sensitive_output",
                "planning_outcome_missing",
                "planning_outcome_conflict",
                "planning_question_ids_duplicate",
            )
            if code in codes
        ),
        "planning_output_invalid",
    )
    return {
        "reason_code": reason,
        "validation_types": sorted(codes)[:12],
        "fields": sorted(fields)[:12],
    }
