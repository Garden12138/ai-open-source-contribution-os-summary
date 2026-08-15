from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


ALLOWED_PUBLISH_ACTIONS = ("create_draft_pr",)
PROHIBITED_PUBLISH_ACTIONS = frozenset(
    {
        "force_push",
        "comment",
        "label",
        "assign",
        "claim",
        "ready_for_review",
        "merge",
        "delete_branch",
    }
)


class PublisherError(RuntimeError):
    pass


class PublisherProhibitedError(PublisherError):
    pass


class PublisherDisabledError(PublisherError):
    pass


@dataclass(frozen=True, slots=True)
class PublishRequest:
    upstream_repository: str
    head_branch: str
    base_commit_sha: str
    head_commit_sha: str
    title: str
    body: str
    actions: tuple[str, ...]
    diff_hash: str
    review_record_hash: str


@dataclass(frozen=True, slots=True)
class PublishResult:
    provider: str
    number: int
    html_url: str


class GitHubPublisher(Protocol):
    def create_draft_pull_request(self, request: PublishRequest) -> PublishResult:
        """Create exactly the approved Draft PR actions."""


class FakeGitHubPublisher:
    """Local-only publisher. Never talks to GitHub or Docker."""

    def __init__(self) -> None:
        self.calls: list[PublishRequest] = []
        self._number = 0

    def create_draft_pull_request(self, request: PublishRequest) -> PublishResult:
        validate_publish_actions(request.actions)
        self.calls.append(request)
        self._number += 1
        return PublishResult(
            provider="fake",
            number=self._number,
            html_url=(
                f"https://github.com/{request.upstream_repository}"
                f"/pull/{self._number}"
            ),
        )


class DisabledGitHubPublisher:
    def create_draft_pull_request(self, request: PublishRequest) -> PublishResult:
        del request
        raise PublisherDisabledError(
            "Real GitHub publication is not enabled"
        )


def validate_publish_actions(actions: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized = tuple(actions)
    if not normalized:
        raise PublisherProhibitedError("Publish action list is empty")
    if set(normalized) & PROHIBITED_PUBLISH_ACTIONS:
        raise PublisherProhibitedError(
            "Publish action list contains a prohibited GitHub write"
        )
    if set(normalized) != set(ALLOWED_PUBLISH_ACTIONS):
        raise PublisherProhibitedError(
            "Publish action list must be exactly create_draft_pr"
        )
    return ALLOWED_PUBLISH_ACTIONS
