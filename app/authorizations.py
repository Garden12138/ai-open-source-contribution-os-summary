from __future__ import annotations

from enum import StrEnum


class UserAction(StrEnum):
    APPROVE_PLAN = "approve_plan"
    START_EXECUTION = "start_execution"
    ACCEPT_CHANGE_SET = "accept_change_set"
    START_REVIEW = "start_review"
    START_REPAIR = "start_repair"
    CREATE_PUBLISH_INTENT = "create_publish_intent"
    PUBLISH_DRAFT_PR = "publish_draft_pr"
    ABANDON_TASK = "abandon_task"
    FAIL_TASK = "fail_task"
    REJECT_TASK = "reject_task"
    MARK_REWARD = "mark_reward"
    REVISE_PLAN = "revise_plan"
    INGEST_PR_EVENT = "ingest_pr_event"


class AuthorizationActionError(RuntimeError):
    pass


def require_user_action(
    actual: UserAction | str,
    *,
    expected: UserAction,
) -> UserAction:
    try:
        action = UserAction(actual)
    except ValueError as exc:
        raise AuthorizationActionError(
            "User authorization action is unknown"
        ) from exc
    if action is not expected:
        raise AuthorizationActionError(
            f"User authorization for {action.value} cannot authorize "
            f"{expected.value}"
        )
    return action
