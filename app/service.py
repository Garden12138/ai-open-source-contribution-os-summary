from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain import (
    FilterDecision,
    IssueFacts,
    RepositoryFacts,
    ScoreResult,
    SelectionCandidate,
)
from app.models import (
    DailyPick,
    Opportunity,
    OpportunitySnapshot,
    Repository,
    ScanRun,
    ScoreVersion,
    utc_now,
)
from app.provenance import content_hash
from app.scoring import (
    SCORE_ALGORITHM_VERSION,
    SCORE_SCHEMA_VERSION,
    WEIGHTS,
    filter_issue,
    score_issue,
    select_daily_opportunities,
)
from app.security import redact_text


class GitHubReader(Protocol):
    rate_limit_remaining: int | None
    rate_limit_reset_at: datetime | None

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]: ...

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]: ...


def parse_github_datetime(
    value: str | None, fallback: datetime | None = None
) -> datetime:
    if not value:
        return fallback or utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def repository_name_from_issue(item: dict[str, Any]) -> str | None:
    repository_url = str(item.get("repository_url") or "")
    marker = "/repos/"
    if marker in repository_url:
        return repository_url.split(marker, 1)[1].strip("/") or None

    html_url = str(item.get("html_url") or "")
    parts = [part for part in urlparse(html_url).path.split("/") if part]
    if len(parts) >= 4 and parts[2] in {"issues", "pull"}:
        return f"{parts[0]}/{parts[1]}"
    return None


class DiscoveryService:
    def __init__(
        self, session: Session, github: GitHubReader, settings: Settings
    ) -> None:
        self.session = session
        self.github = github
        self.settings = settings

    async def scan(
        self,
        queries: list[str] | tuple[str, ...] | None = None,
        *,
        top_n: int | None = None,
        now: datetime | None = None,
    ) -> ScanRun:
        now = now or utc_now()
        queries = [
            query.strip()
            for query in (queries or self.settings.github_queries)
            if query.strip()
        ]
        if not queries:
            raise ValueError("At least one GitHub search query is required")
        top_n = self.settings.daily_pick_count if top_n is None else top_n
        if not 1 <= top_n <= 50:
            raise ValueError("top_n must be between 1 and 50")

        selection_date = self._local_date(now)
        run = ScanRun(
            id=str(uuid4()),
            status="running",
            provenance_status="verified",
            selection_date=selection_date,
            queries=queries,
            started_at=now,
        )
        self.session.add(run)
        self.session.commit()

        try:
            items_by_id = await self._collect_candidates(queries)
            repository_names = {
                name
                for item in items_by_id.values()
                if (name := repository_name_from_issue(item))
            }
            repositories = await self._load_repositories(repository_names, now)

            current_opportunities: list[Opportunity] = []
            provenance_by_opportunity: dict[
                int, tuple[OpportunitySnapshot, ScoreVersion]
            ] = {}
            for item in items_by_id.values():
                if "pull_request" in item:
                    continue
                full_name = repository_name_from_issue(item)
                repository = repositories.get(full_name or "")
                if repository is None:
                    continue
                issue_facts = self._to_issue_facts(item, repository)
                decision = filter_issue(issue_facts, self.settings, now=now)
                score = score_issue(issue_facts, self.settings, now=now)
                opportunity = self._upsert_opportunity(
                    item,
                    repository,
                    issue_facts,
                    decision.reasons,
                    decision.eligible,
                    score,
                    now,
                )
                self.session.flush()
                snapshot, score_version = self._create_provenance(
                    run,
                    opportunity,
                    issue_facts,
                    decision,
                    score,
                    now,
                )
                provenance_by_opportunity[opportunity.id] = (
                    snapshot,
                    score_version,
                )
                current_opportunities.append(opportunity)

            self.session.flush()
            eligible = [item for item in current_opportunities if item.eligible]
            candidates = [
                SelectionCandidate(
                    opportunity_id=item.id,
                    total_score=item.score_total,
                    impact_score=float(item.score_components.get("project_impact", 0)),
                    has_bounty=item.has_bounty,
                    is_strategic=item.is_strategic,
                    is_tech_match=item.is_tech_match,
                )
                for item in eligible
            ]
            selections = select_daily_opportunities(candidates, top_n=top_n)
            self.session.execute(
                delete(DailyPick).where(DailyPick.selection_date == selection_date)
            )
            for rank, selection in enumerate(selections, start=1):
                snapshot, score_version = provenance_by_opportunity[
                    selection.opportunity_id
                ]
                self.session.add(
                    DailyPick(
                        selection_date=selection_date,
                        rank=rank,
                        opportunity_id=selection.opportunity_id,
                        scan_run_id=run.id,
                        snapshot_id=snapshot.id,
                        score_version_id=score_version.id,
                        provenance_status="verified",
                        selection_reason=selection.selection_reason,
                        score_snapshot=selection.total_score,
                    )
                )

            run.status = "completed"
            run.candidate_count = len(items_by_id)
            run.eligible_count = len(eligible)
            run.selected_count = len(selections)
            run.repository_count = len(repository_names)
            run.rate_limit_remaining = self.github.rate_limit_remaining
            run.rate_limit_reset_at = self.github.rate_limit_reset_at
            run.completed_at = utc_now()
            self.session.commit()
            from app.product_experience import ProductExperienceService

            ProductExperienceService(
                self.session,
                secrets=(
                    self.settings.github_token,
                    self.settings.local_access_token,
                ),
            ).create_scan_notifications(run.id)
            return run
        except asyncio.CancelledError:
            self._fail_scan_run(
                run.id,
                RuntimeError("Discovery scan cancelled before completion"),
            )
            raise
        except Exception as exc:
            self._fail_scan_run(run.id, exc)
            raise

    async def _collect_candidates(
        self, queries: list[str]
    ) -> dict[int, dict[str, Any]]:
        items_by_id: dict[int, dict[str, Any]] = {}
        for query in queries:
            items = await self.github.search_issues(
                query, limit=self.settings.candidates_per_query
            )
            for item in items:
                issue_id = item.get("id")
                if not isinstance(issue_id, int):
                    continue
                if issue_id not in items_by_id:
                    items_by_id[issue_id] = {**item, "_source_queries": [query]}
                elif query not in items_by_id[issue_id]["_source_queries"]:
                    items_by_id[issue_id]["_source_queries"].append(query)
        return items_by_id

    async def _load_repositories(
        self, full_names: set[str], now: datetime
    ) -> dict[str, Repository]:
        if not full_names:
            return {}
        existing = {
            repository.full_name: repository
            for repository in self.session.scalars(
                select(Repository).where(Repository.full_name.in_(full_names))
            )
        }
        cache_cutoff = now - timedelta(hours=self.settings.repository_cache_hours)
        stale_names = [
            full_name
            for full_name in sorted(full_names)
            if full_name not in existing
            or existing[full_name].sync_error is not None
            or self._as_aware(existing[full_name].last_synced_at) < cache_cutoff
        ]

        semaphore = asyncio.Semaphore(max(1, self.settings.github_concurrency))

        async def fetch(full_name: str) -> tuple[str, dict[str, Any] | Exception]:
            async with semaphore:
                try:
                    return full_name, await self.github.get_repository_bundle(full_name)
                except Exception as exc:  # one broken repository must not fail the scan
                    return full_name, exc

        results = await asyncio.gather(*(fetch(name) for name in stale_names))
        for full_name, result in results:
            repository = existing.get(full_name)
            if repository is None:
                repository = Repository(full_name=full_name)
                self.session.add(repository)
                existing[full_name] = repository

            if isinstance(result, Exception):
                repository.sync_error = self._safe_error(result, limit=1000)
                continue
            self._apply_repository_bundle(repository, result, now)

        self.session.flush()
        return existing

    @staticmethod
    def _as_aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _apply_repository_bundle(
        repository: Repository, bundle: dict[str, Any], now: datetime
    ) -> None:
        data = bundle.get("repository") or {}
        community = bundle.get("community") or {}
        license_data = data.get("license") or {}
        files = community.get("files") or {}

        repository.github_id = data.get("id")
        repository.full_name = data.get("full_name") or repository.full_name
        repository.description = data.get("description")
        repository.html_url = data.get("html_url")
        repository.language = data.get("language")
        repository.license_spdx = license_data.get("spdx_id")
        repository.stars = int(data.get("stargazers_count") or 0)
        repository.forks = int(data.get("forks_count") or 0)
        repository.open_issues = int(data.get("open_issues_count") or 0)
        repository.archived = bool(data.get("archived"))
        repository.disabled = bool(data.get("disabled"))
        repository.default_branch = data.get("default_branch")
        repository.topics = [str(topic) for topic in (data.get("topics") or [])]
        repository.pushed_at = (
            parse_github_datetime(data["pushed_at"]) if data.get("pushed_at") else None
        )
        repository.has_contributing_guide = bool(files.get("contributing"))
        health = community.get("health_percentage")
        repository.health_percentage = int(health) if isinstance(health, int) else None
        repository.sync_error = None
        repository.last_synced_at = now

    @staticmethod
    def _to_issue_facts(item: dict[str, Any], repository: Repository) -> IssueFacts:
        labels = tuple(
            str(label.get("name") if isinstance(label, dict) else label)
            for label in (item.get("labels") or [])
        )
        assignees = item.get("assignees") or []
        assignees_count = len(assignees) if isinstance(assignees, list) else 0
        if assignees_count == 0 and item.get("assignee"):
            assignees_count = 1
        return IssueFacts(
            github_issue_id=int(item["id"]),
            number=int(item.get("number") or 0),
            title=str(item.get("title") or "Untitled issue"),
            body=str(item.get("body") or ""),
            html_url=str(item.get("html_url") or ""),
            state=str(item.get("state") or "open"),
            labels=labels,
            comments_count=int(item.get("comments") or 0),
            assignees_count=assignees_count,
            author_association=item.get("author_association"),
            created_at=parse_github_datetime(item.get("created_at")),
            updated_at=parse_github_datetime(item.get("updated_at")),
            source_queries=tuple(item.get("_source_queries") or []),
            repository=RepositoryFacts(
                full_name=repository.full_name,
                description=repository.description or "",
                language=repository.language,
                license_spdx=repository.license_spdx,
                stars=repository.stars,
                forks=repository.forks,
                archived=repository.archived,
                disabled=repository.disabled,
                topics=tuple(repository.topics or []),
                pushed_at=repository.pushed_at,
                has_contributing_guide=repository.has_contributing_guide,
                health_percentage=repository.health_percentage,
                sync_error=repository.sync_error,
            ),
        )

    def _upsert_opportunity(
        self,
        item: dict[str, Any],
        repository: Repository,
        facts: IssueFacts,
        filter_reasons: tuple[str, ...],
        eligible: bool,
        score: Any,
        now: datetime,
    ) -> Opportunity:
        opportunity = self.session.scalar(
            select(Opportunity).where(
                Opportunity.github_issue_id == facts.github_issue_id
            )
        )
        if opportunity is None:
            opportunity = Opportunity(
                github_issue_id=facts.github_issue_id,
                repository=repository,
                issue_number=facts.number,
                title=facts.title,
                body=facts.body,
                html_url=facts.html_url,
                issue_created_at=facts.created_at,
                issue_updated_at=facts.updated_at,
                first_seen_at=now,
            )
            self.session.add(opportunity)

        opportunity.repository = repository
        opportunity.issue_number = facts.number
        opportunity.title = facts.title
        opportunity.body = facts.body
        opportunity.html_url = facts.html_url
        opportunity.state = facts.state
        opportunity.labels = list(facts.labels)
        opportunity.comments_count = facts.comments_count
        opportunity.assignees_count = facts.assignees_count
        opportunity.author_association = facts.author_association
        opportunity.source_queries = list(facts.source_queries)
        opportunity.issue_created_at = facts.created_at
        opportunity.issue_updated_at = facts.updated_at
        opportunity.last_seen_at = now
        opportunity.eligible = eligible
        opportunity.filter_reasons = list(filter_reasons)
        opportunity.score_total = score.total
        opportunity.score_components = score.components
        opportunity.risk_penalty = score.risk_penalty
        opportunity.risk_reasons = list(score.risk_reasons)
        opportunity.has_bounty = score.has_bounty
        opportunity.bounty_amount_usd = score.bounty_amount_usd
        opportunity.is_strategic = score.is_strategic
        opportunity.is_tech_match = score.is_tech_match
        return opportunity

    def _create_provenance(
        self,
        run: ScanRun,
        opportunity: Opportunity,
        facts: IssueFacts,
        decision: FilterDecision,
        score: ScoreResult,
        now: datetime,
    ) -> tuple[OpportunitySnapshot, ScoreVersion]:
        issue_data: dict[str, object] = {
            "github_issue_id": facts.github_issue_id,
            "number": facts.number,
            "title": facts.title,
            "body": facts.body,
            "html_url": facts.html_url,
            "state": facts.state,
            "labels": list(facts.labels),
            "comments_count": facts.comments_count,
            "assignees_count": facts.assignees_count,
            "author_association": facts.author_association,
            "created_at": self._utc_iso(facts.created_at),
            "updated_at": self._utc_iso(facts.updated_at),
        }
        repository = facts.repository
        repository_record = opportunity.repository
        repository_data: dict[str, object] = {
            "github_id": repository_record.github_id,
            "full_name": repository.full_name,
            "description": repository.description,
            "html_url": repository_record.html_url,
            "language": repository.language,
            "license_spdx": repository.license_spdx,
            "stars": repository.stars,
            "forks": repository.forks,
            "open_issues": repository_record.open_issues,
            "archived": repository.archived,
            "disabled": repository.disabled,
            "default_branch": repository_record.default_branch,
            "topics": list(repository.topics),
            "pushed_at": (
                self._utc_iso(repository.pushed_at)
                if repository.pushed_at is not None
                else None
            ),
            "has_contributing_guide": repository.has_contributing_guide,
            "health_percentage": repository.health_percentage,
            "sync_error": repository.sync_error,
            "last_synced_at": self._utc_iso(repository_record.last_synced_at),
        }
        rule_config: dict[str, object] = {
            "filter_algorithm_version": "rules-v1",
            "score_algorithm_version": SCORE_ALGORITHM_VERSION,
            "score_schema_version": SCORE_SCHEMA_VERSION,
            "weights": dict(WEIGHTS),
            "preferred_languages": list(self.settings.preferred_languages),
            "strategic_keywords": list(self.settings.strategic_keywords),
            "minimum_issue_body_length": self.settings.minimum_issue_body_length,
            "repository_inactive_days": self.settings.repository_inactive_days,
        }
        snapshot_payload = {
            "schema_version": "1",
            "scan_run_id": run.id,
            "opportunity_id": opportunity.id,
            "captured_at": self._utc_iso(now),
            "issue_data": issue_data,
            "repository_data": repository_data,
            "source_queries": list(facts.source_queries),
            "rule_config": rule_config,
            "filter_eligible": decision.eligible,
            "filter_reasons": list(decision.reasons),
        }
        inputs_hash = content_hash(snapshot_payload)
        snapshot = OpportunitySnapshot(
            id=str(uuid4()),
            scan_run=run,
            opportunity=opportunity,
            schema_version="1",
            inputs_hash=inputs_hash,
            issue_data=issue_data,
            repository_data=repository_data,
            source_queries=list(facts.source_queries),
            rule_config=rule_config,
            filter_eligible=decision.eligible,
            filter_reasons=list(decision.reasons),
            captured_at=now,
            created_at=now,
        )
        score_payload = {
            "score_total": score.total,
            "score_components": score.components,
            "risk_penalty": score.risk_penalty,
            "risk_reasons": list(score.risk_reasons),
            "has_bounty": score.has_bounty,
            "bounty_amount_usd": score.bounty_amount_usd,
            "is_strategic": score.is_strategic,
            "is_tech_match": score.is_tech_match,
        }
        score_version = ScoreVersion(
            id=str(uuid4()),
            snapshot=snapshot,
            algorithm_version=SCORE_ALGORITHM_VERSION,
            schema_version=SCORE_SCHEMA_VERSION,
            inputs_hash=inputs_hash,
            output_hash=content_hash(
                {
                    "inputs_hash": inputs_hash,
                    "algorithm_version": SCORE_ALGORITHM_VERSION,
                    "schema_version": SCORE_SCHEMA_VERSION,
                    "result": score_payload,
                }
            ),
            **score_payload,
            created_at=now,
        )
        self.session.add_all((snapshot, score_version))
        self.session.flush()
        return snapshot, score_version

    @staticmethod
    def _utc_iso(value: datetime) -> str:
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()

    def _safe_error(self, error: BaseException, *, limit: int) -> str:
        return redact_text(
            error,
            secrets=(self.settings.github_token,),
        )[:limit]

    def _fail_scan_run(self, run_id: str, error: BaseException) -> None:
        self.session.rollback()
        failed_run = self.session.get(ScanRun, run_id)
        if failed_run is None:
            return
        failed_run.status = "failed"
        failed_run.error_message = self._safe_error(error, limit=2000)
        failed_run.completed_at = utc_now()
        failed_run.rate_limit_remaining = self.github.rate_limit_remaining
        failed_run.rate_limit_reset_at = self.github.rate_limit_reset_at
        self.session.commit()

    def _local_date(self, now: datetime):
        try:
            timezone_info = ZoneInfo(self.settings.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown APP_TIMEZONE: {self.settings.timezone}") from exc
        aware_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return aware_now.astimezone(timezone_info).date()
