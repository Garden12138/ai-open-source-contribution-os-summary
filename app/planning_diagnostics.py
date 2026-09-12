"""Allowlisted planning validation diagnostics; never expose model input."""

from pydantic import ValidationError

MESSAGES = {
    "planning_outcome_missing": "规划模型只返回了说明，未提供问题、补读文件或可编辑方案。上下文已保留。",
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
    }
)


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
        fields.add(
            ".".join(parts)
            if parts and all(part in SAFE_FIELDS for part in parts)
            else "response"
        )
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
