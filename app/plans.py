from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ContributionTaskStateVersion,
    PlanVersion,
)
from app.planning import (
    ContributionTaskError,
    ContributionTaskService,
)
from app.provenance import content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
    task_state_record_payload,
)


PLAN_SCHEMA_VERSION = "1"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_COMMAND_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")


class PlanVersionError(RuntimeError):
    pass


class PlanVersionNotFoundError(PlanVersionError):
    pass


class PlanVersionConflictError(PlanVersionError):
    pass


@dataclass(frozen=True, slots=True)
class PlanFieldDifference:
    path: str
    left_present: bool
    right_present: bool
    left: object
    right: object


@dataclass(frozen=True, slots=True)
class PlanVersionComparison:
    left_version_id: str
    right_version_id: str
    task_id: str
    semantic_differences: tuple[PlanFieldDifference, ...]
    unified_diff: str


@dataclass(frozen=True, slots=True)
class PlanCommand:
    command_id: str
    purpose: str
    argv: tuple[str, ...]
    working_directory: str = "."

    def __post_init__(self) -> None:
        if not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("Plan command ID is invalid")
        purpose = _bounded_text(
            self.purpose,
            "Plan command purpose",
            maximum=1_000,
        )
        if isinstance(self.argv, (str, bytes)):
            raise ValueError("Plan command argv is invalid")
        argv = tuple(self.argv)
        if (
            not argv
            or len(argv) > 50
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 2_000
                or "\x00" in item
                for item in argv
            )
        ):
            raise ValueError("Plan command argv is invalid")
        directory = _repository_path(
            self.working_directory,
            "Plan command working directory",
            allow_root=True,
        )
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "working_directory", directory)
        ensure_no_sensitive_data(
            self.hash_payload(),
            context="Plan command",
        )

    def hash_payload(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "purpose": self.purpose,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
        }


@dataclass(frozen=True, slots=True)
class PlanContent:
    goal: str
    acceptance_criteria: tuple[str, ...]
    files_to_inspect: tuple[str, ...]
    files_likely_to_change: tuple[str, ...]
    implementation_steps: tuple[str, ...]
    tests_to_add_or_run: tuple[str, ...]
    commands_to_run: tuple[PlanCommand, ...]
    risks: tuple[str, ...]
    questions_for_maintainer: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "goal",
            _bounded_text(self.goal, "Plan goal", maximum=4_000),
        )
        for name, minimum, maximum, item_maximum in (
            ("acceptance_criteria", 1, 100, 2_000),
            ("implementation_steps", 1, 100, 4_000),
            ("tests_to_add_or_run", 1, 100, 2_000),
            ("risks", 0, 100, 2_000),
            ("questions_for_maintainer", 0, 100, 2_000),
        ):
            object.__setattr__(
                self,
                name,
                _bounded_text_tuple(
                    getattr(self, name),
                    name.replace("_", " "),
                    minimum=minimum,
                    maximum=maximum,
                    item_maximum=item_maximum,
                ),
            )
        for name in ("files_to_inspect", "files_likely_to_change"):
            values = tuple(
                _repository_path(item, name.replace("_", " "))
                for item in getattr(self, name)
            )
            if (
                not values
                or len(values) > 500
                or len(values) != len(set(values))
            ):
                raise ValueError(
                    f"{name.replace('_', ' ')} must be unique and contain "
                    "between 1 and 500 paths"
                )
            object.__setattr__(self, name, values)
        commands = tuple(self.commands_to_run)
        if (
            not commands
            or len(commands) > 100
            or any(not isinstance(item, PlanCommand) for item in commands)
            or len({item.command_id for item in commands}) != len(commands)
        ):
            raise ValueError(
                "Plan commands must be unique and contain between 1 and 100 items"
            )
        object.__setattr__(self, "commands_to_run", commands)
        ensure_no_sensitive_data(
            self.hash_payload(),
            context="Plan content",
        )

    @property
    def content_hash(self) -> str:
        return content_hash(self.hash_payload())

    def hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "goal": self.goal,
            "acceptance_criteria": list(self.acceptance_criteria),
            "files_to_inspect": list(self.files_to_inspect),
            "files_likely_to_change": list(self.files_likely_to_change),
            "implementation_steps": list(self.implementation_steps),
            "tests_to_add_or_run": list(self.tests_to_add_or_run),
            "commands_to_run": [
                command.hash_payload() for command in self.commands_to_run
            ],
            "risks": list(self.risks),
            "questions_for_maintainer": list(
                self.questions_for_maintainer
            ),
        }


class PlanVersionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_verified(self, version_id: str) -> PlanVersion:
        plan = self.session.get(PlanVersion, version_id)
        if plan is None:
            raise PlanVersionNotFoundError("PlanVersion was not found")
        self._validated_content(plan)
        try:
            task = ContributionTaskService(
                self.session
            ).get_verified(plan.task_id)
        except ContributionTaskError as exc:
            raise PlanVersionConflictError(str(exc)) from exc
        state = self.session.get(
            ContributionTaskStateVersion,
            plan.task_state_version_id,
        )
        if state is None:
            raise PlanVersionConflictError(
                "PlanVersion task state was not found"
            )
        try:
            source = (
                ContributionTaskState(state.from_state)
                if state.from_state is not None
                else None
            )
            target = ContributionTaskState(state.to_state)
        except ValueError as exc:
            raise PlanVersionConflictError(
                "PlanVersion task state is invalid"
            ) from exc
        expected_state_hash = content_hash(
            task_state_record_payload(
                task_id=task.id,
                task_record_hash=task.record_hash,
                sequence=state.sequence,
                from_state=source,
                to_state=target,
                reason_code=state.reason_code,
                previous_state_hash=state.previous_state_hash,
            )
        )
        if (
            plan.task_record_hash != task.record_hash
            or state.task_id != task.id
            or state.task_record_hash != task.record_hash
            or state.record_hash != expected_state_hash
            or plan.task_state_record_hash != state.record_hash
        ):
            raise PlanVersionConflictError(
                "PlanVersion task/state provenance does not match"
            )
        return plan

    def create_initial(
        self,
        *,
        task_id: str,
        content: PlanContent,
        idempotency_key: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> PlanVersion:
        key = _idempotency_key(idempotency_key)
        try:
            task = ContributionTaskService(
                self.session
            ).get_verified(task_id)
        except ContributionTaskError as exc:
            raise PlanVersionNotFoundError(str(exc)) from exc
        replay = self.session.scalar(
            select(PlanVersion).where(
                PlanVersion.idempotency_key == key
            )
        )
        if replay is not None:
            if (
                replay.task_id != task.id
                or replay.content_hash != content.content_hash
            ):
                raise PlanVersionConflictError(
                    "Plan idempotency key already belongs to different content"
                )
            return replay
        existing = self.session.scalar(
            select(PlanVersion).where(
                PlanVersion.task_id == task.id,
                PlanVersion.content_hash == content.content_hash,
            )
        )
        if existing is not None:
            return existing
        if self.session.scalar(
            select(PlanVersion.id).where(PlanVersion.task_id == task.id)
        ) is not None:
            raise PlanVersionConflictError(
                "ContributionTask already has an initial PlanVersion"
            )
        try:
            task_state = ContributionTaskStateService(
                self.session
            ).current(task.id)
        except TaskStateError as exc:
            raise PlanVersionConflictError(str(exc)) from exc
        if task_state.to_state != ContributionTaskState.PLANNING.value:
            raise PlanVersionConflictError(
                "Initial PlanVersion requires the task planning state"
            )

        record_payload = {
            **_plan_record_payload(
                task_id=task.id,
                task_record_hash=task.record_hash,
                task_state_version_id=task_state.id,
                task_state_record_hash=task_state.record_hash,
                version_number=1,
                content_hash_value=content.content_hash,
                parent_version_id=None,
                parent_record_hash=None,
            )
        }
        ensure_no_sensitive_data(
            {
                "idempotency_key": key,
                "record": record_payload,
                "content": content.hash_payload(),
            },
            context="PlanVersion",
        )
        commands = [
            command.hash_payload() for command in content.commands_to_run
        ]
        plan = PlanVersion(
            id=str(uuid4()),
            task_id=task.id,
            task_state_version_id=task_state.id,
            parent_version_id=None,
            parent_record_hash=None,
            schema_version=PLAN_SCHEMA_VERSION,
            version_number=1,
            idempotency_key=key,
            task_record_hash=task.record_hash,
            task_state_record_hash=task_state.record_hash,
            goal=content.goal,
            acceptance_criteria=list(content.acceptance_criteria),
            files_to_inspect=list(content.files_to_inspect),
            files_likely_to_change=list(content.files_likely_to_change),
            implementation_steps=list(content.implementation_steps),
            tests_to_add_or_run=list(content.tests_to_add_or_run),
            commands_to_run=commands,
            risks=list(content.risks),
            questions_for_maintainer=list(
                content.questions_for_maintainer
            ),
            content_hash=content.content_hash,
            record_hash=content_hash(record_payload),
            created_at=_aware(now),
        )
        self.session.add(plan)
        try:
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(PlanVersion).where(
                    PlanVersion.idempotency_key == key
                )
            )
            if (
                replay is not None
                and replay.task_id == task.id
                and replay.content_hash == content.content_hash
            ):
                return replay
            existing = self.session.scalar(
                select(PlanVersion).where(
                    PlanVersion.task_id == task.id,
                    PlanVersion.content_hash == content.content_hash,
                )
            )
            if existing is not None:
                return existing
            raise PlanVersionConflictError(
                "PlanVersion provenance constraints rejected the record"
            ) from exc
        return plan

    def create_revision(
        self,
        *,
        parent_version_id: str,
        content: PlanContent,
        idempotency_key: str,
        now: datetime | None = None,
        commit: bool = True,
        allow_repeated_content: bool = False,
    ) -> PlanVersion:
        key = _idempotency_key(idempotency_key)
        parent = self.get_verified(parent_version_id)
        task = ContributionTaskService(
            self.session
        ).get_verified(parent.task_id)
        replay = self.session.scalar(
            select(PlanVersion).where(
                PlanVersion.idempotency_key == key
            )
        )
        if replay is not None:
            if (
                replay.parent_version_id != parent.id
                or replay.content_hash != content.content_hash
            ):
                raise PlanVersionConflictError(
                    "Plan idempotency key already belongs to different content"
                )
            return replay
        matching_content = select(PlanVersion).where(
            PlanVersion.task_id == task.id,
            PlanVersion.content_hash == content.content_hash,
        )
        if allow_repeated_content:
            matching_content = matching_content.where(PlanVersion.parent_version_id == parent.id)
        existing = self.session.scalar(matching_content)
        if existing is not None:
            if existing.id == parent.id:
                raise PlanVersionConflictError(
                    "Plan revision content is unchanged"
                )
            if existing.parent_version_id == parent.id:
                return existing
            raise PlanVersionConflictError(
                "Plan revision content belongs to a different parent"
            )
        try:
            task_state = ContributionTaskStateService(
                self.session
            ).current(task.id)
        except TaskStateError as exc:
            raise PlanVersionConflictError(str(exc)) from exc
        if task_state.to_state != ContributionTaskState.PLANNING.value:
            raise PlanVersionConflictError(
                "Plan revision requires the task planning state"
            )
        latest = self.session.scalar(
            select(PlanVersion)
            .where(PlanVersion.task_id == task.id)
            .order_by(
                PlanVersion.version_number.desc(),
                PlanVersion.id.desc(),
            )
            .limit(1)
        )
        if latest is None or latest.id != parent.id:
            raise PlanVersionConflictError(
                "Plan revision parent is stale"
            )

        version_number = parent.version_number + 1
        record_payload = _plan_record_payload(
            task_id=task.id,
            task_record_hash=task.record_hash,
            task_state_version_id=task_state.id,
            task_state_record_hash=task_state.record_hash,
            version_number=version_number,
            content_hash_value=content.content_hash,
            parent_version_id=parent.id,
            parent_record_hash=parent.record_hash,
        )
        ensure_no_sensitive_data(
            {
                "idempotency_key": key,
                "record": record_payload,
                "content": content.hash_payload(),
            },
            context="PlanVersion revision",
        )
        revision = PlanVersion(
            id=str(uuid4()),
            task_id=task.id,
            task_state_version_id=task_state.id,
            parent_version_id=parent.id,
            parent_record_hash=parent.record_hash,
            schema_version=PLAN_SCHEMA_VERSION,
            version_number=version_number,
            idempotency_key=key,
            task_record_hash=task.record_hash,
            task_state_record_hash=task_state.record_hash,
            goal=content.goal,
            acceptance_criteria=list(content.acceptance_criteria),
            files_to_inspect=list(content.files_to_inspect),
            files_likely_to_change=list(content.files_likely_to_change),
            implementation_steps=list(content.implementation_steps),
            tests_to_add_or_run=list(content.tests_to_add_or_run),
            commands_to_run=[
                command.hash_payload() for command in content.commands_to_run
            ],
            risks=list(content.risks),
            questions_for_maintainer=list(
                content.questions_for_maintainer
            ),
            content_hash=content.content_hash,
            record_hash=content_hash(record_payload),
            created_at=_aware(now),
        )
        self.session.add(revision)
        try:
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(PlanVersion).where(
                    PlanVersion.idempotency_key == key
                )
            )
            if (
                replay is not None
                and replay.parent_version_id == parent.id
                and replay.content_hash == content.content_hash
            ):
                return replay
            raise PlanVersionConflictError(
                "Plan revision changed concurrently"
            ) from exc
        return revision

    def compare(
        self,
        left_version_id: str,
        right_version_id: str,
    ) -> PlanVersionComparison:
        left = self.get_verified(left_version_id)
        right = self.get_verified(right_version_id)
        if left.task_id != right.task_id:
            raise PlanVersionConflictError(
                "PlanVersions from different tasks cannot be compared"
            )
        left_content = self._validated_content(left).hash_payload()
        right_content = self._validated_content(right).hash_payload()
        differences = tuple(
            _differences(left_content, right_content, path="")
        )
        left_lines = _pretty_plan(left_content).splitlines(keepends=True)
        right_lines = _pretty_plan(right_content).splitlines(keepends=True)
        unified = "".join(
            difflib.unified_diff(
                left_lines,
                right_lines,
                fromfile=f"plan-v{left.version_number}",
                tofile=f"plan-v{right.version_number}",
                lineterm="\n",
            )
        )
        if len(unified) > 200_000:
            raise PlanVersionConflictError(
                "PlanVersion text difference exceeds the display limit"
            )
        return PlanVersionComparison(
            left_version_id=left.id,
            right_version_id=right.id,
            task_id=left.task_id,
            semantic_differences=differences,
            unified_diff=unified,
        )

    @staticmethod
    def _validated_content(plan: PlanVersion) -> PlanContent:
        try:
            commands = tuple(
                PlanCommand(
                    command_id=_mapping_value(
                        value,
                        "command_id",
                    ),
                    purpose=_mapping_value(value, "purpose"),
                    argv=tuple(_mapping_sequence(value, "argv")),
                    working_directory=_mapping_value(
                        value,
                        "working_directory",
                    ),
                )
                for value in plan.commands_to_run
            )
            content = PlanContent(
                goal=plan.goal,
                acceptance_criteria=tuple(plan.acceptance_criteria),
                files_to_inspect=tuple(plan.files_to_inspect),
                files_likely_to_change=tuple(
                    plan.files_likely_to_change
                ),
                implementation_steps=tuple(plan.implementation_steps),
                tests_to_add_or_run=tuple(plan.tests_to_add_or_run),
                commands_to_run=commands,
                risks=tuple(plan.risks),
                questions_for_maintainer=tuple(
                    plan.questions_for_maintainer
                ),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PlanVersionConflictError(
                "PlanVersion structured content is invalid"
            ) from exc
        expected_record_hash = content_hash(
            _plan_record_payload(
                task_id=plan.task_id,
                task_record_hash=plan.task_record_hash,
                task_state_version_id=plan.task_state_version_id,
                task_state_record_hash=plan.task_state_record_hash,
                version_number=plan.version_number,
                content_hash_value=content.content_hash,
                parent_version_id=plan.parent_version_id,
                parent_record_hash=plan.parent_record_hash,
            )
        )
        if (
            plan.content_hash != content.content_hash
            or plan.record_hash != expected_record_hash
        ):
            raise PlanVersionConflictError(
                "PlanVersion content or record hash does not match"
            )
        return content


def _plan_record_payload(
    *,
    task_id: str,
    task_record_hash: str,
    task_state_version_id: str,
    task_state_record_hash: str,
    version_number: int,
    content_hash_value: str,
    parent_version_id: str | None,
    parent_record_hash: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "task_id": task_id,
        "task_record_hash": task_record_hash,
        "task_state_version_id": task_state_version_id,
        "task_state_record_hash": task_state_record_hash,
        "version_number": version_number,
        "content_hash": content_hash_value,
    }
    if version_number > 1:
        payload["parent_version_id"] = parent_version_id
        payload["parent_record_hash"] = parent_record_hash
    return payload


def _differences(
    left: object,
    right: object,
    *,
    path: str,
) -> Iterator[PlanFieldDifference]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        keys = sorted(set(left) | set(right))
        for key in keys:
            child_path = f"{path}/{_pointer_token(str(key))}"
            if key not in left:
                yield PlanFieldDifference(
                    path=child_path,
                    left_present=False,
                    right_present=True,
                    left=None,
                    right=right[key],
                )
            elif key not in right:
                yield PlanFieldDifference(
                    path=child_path,
                    left_present=True,
                    right_present=False,
                    left=left[key],
                    right=None,
                )
            else:
                yield from _differences(
                    left[key],
                    right[key],
                    path=child_path,
                )
        return
    if isinstance(left, list) and isinstance(right, list):
        maximum = max(len(left), len(right))
        for index in range(maximum):
            child_path = f"{path}/{index}"
            if index >= len(left):
                yield PlanFieldDifference(
                    path=child_path,
                    left_present=False,
                    right_present=True,
                    left=None,
                    right=right[index],
                )
            elif index >= len(right):
                yield PlanFieldDifference(
                    path=child_path,
                    left_present=True,
                    right_present=False,
                    left=left[index],
                    right=None,
                )
            else:
                yield from _differences(
                    left[index],
                    right[index],
                    path=child_path,
                )
        return
    if left != right:
        yield PlanFieldDifference(
            path=path or "/",
            left_present=True,
            right_present=True,
            left=left,
            right=right,
        )


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pretty_plan(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _mapping_value(value: object, key: str) -> str:
    if not isinstance(value, Mapping) or set(value) != {
        "command_id",
        "purpose",
        "argv",
        "working_directory",
    }:
        raise ValueError("Plan command record is invalid")
    item = value[key]
    if not isinstance(item, str):
        raise ValueError("Plan command text is invalid")
    return item


def _mapping_sequence(value: object, key: str) -> list[str]:
    if not isinstance(value, Mapping):
        raise ValueError("Plan command record is invalid")
    item = value[key]
    if not isinstance(item, list) or not all(
        isinstance(part, str) for part in item
    ):
        raise ValueError("Plan command argv is invalid")
    return item


def _bounded_text(
    value: str,
    name: str,
    *,
    maximum: int,
) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must contain between 1 and {maximum} characters")
    return normalized


def _bounded_text_tuple(
    values: Sequence[str],
    name: str,
    *,
    minimum: int,
    maximum: int,
    item_maximum: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(
            f"{name} must be unique and contain between {minimum} and "
            f"{maximum} items"
        )
    normalized = tuple(
        _bounded_text(item, name, maximum=item_maximum)
        for item in values
    )
    if (
        not minimum <= len(normalized) <= maximum
        or len(normalized) != len(set(normalized))
    ):
        raise ValueError(
            f"{name} must be unique and contain between {minimum} and "
            f"{maximum} items"
        )
    return normalized


def _repository_path(
    value: str,
    name: str,
    *,
    allow_root: bool = False,
) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > 1_000
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith(("~", "/"))
    ):
        raise ValueError(f"{name} is not a safe repository-relative path")
    path = PurePosixPath(normalized)
    if any(part in {"", ".."} for part in path.parts):
        raise ValueError(f"{name} is not a safe repository-relative path")
    if path.parts and path.parts[0] == ".git":
        raise ValueError(f"{name} cannot reference repository Git metadata")
    result = path.as_posix()
    if result == "." and not allow_root:
        raise ValueError(f"{name} must name a repository path")
    return result


def _idempotency_key(value: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not _IDEMPOTENCY_KEY.fullmatch(key) or contains_sensitive_text(key):
        raise ValueError("PlanVersion idempotency key is invalid")
    return key


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
