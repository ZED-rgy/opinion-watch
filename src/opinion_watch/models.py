from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Platform(StrEnum):
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"


class SessionStatus(StrEnum):
    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"
    VERIFICATION_REQUIRED = "verification_required"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class DetailStatus(StrEnum):
    NOT_SELECTED = "not_selected"
    ATTEMPTING = "attempting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ScanRunStatus(StrEnum):
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OpinionCategory(StrEnum):
    SUSPECTED_FALSE_INFORMATION = "suspected_false_information"
    SUSPECTED_DEFAMATION = "suspected_defamation"
    COORDINATED_COMPLAINT = "coordinated_complaint"
    SUSPECTED_ASTROTURFING = "suspected_astroturfing"
    REASONABLE_CONSUMER_COMPLAINT = "reasonable_consumer_complaint"
    ORDINARY_GRIEVANCE = "ordinary_grievance"
    IRRELEVANT = "irrelevant"
    OTHER = "other"


class RiskSeverity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


@dataclass(frozen=True, slots=True)
class AnchorCandidate:
    href: str
    text: str = ""
    media_kind: str = ""
    author_name: str = ""
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class CollectedContent:
    platform: Platform
    content_id: str
    url: str
    title: str
    source_keyword: str
    brand_name: str = ""
    author_name: str = ""
    published_at: str | None = None
    metrics: dict[str, int | float | str] = field(default_factory=dict)
    raw_data: dict[str, Any] = field(default_factory=dict)
    discovered_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Full result links may contain short-lived access parameters.  Keep this
    # navigation-only value out of persistence, logs, and model payloads.
    navigation_url: str | None = field(default=None, repr=False, compare=False)

    @property
    def fingerprint(self) -> str:
        value = f"{self.platform.value}:{self.content_id}".encode()
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    content_inserted: int = 0
    new_opinion: int = 0
    rediscovered: int = 0
    ignored: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated


@dataclass(slots=True)
class ScanTotals:
    scanned: int = 0
    collected: int = 0
    filtered: int = 0
    inserted: int = 0
    updated: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    suspected: int = 0
    detailed: int = 0
    media_items: int = 0
    brand_matched: int = 0
    detail_attempted: int = 0
    detail_unavailable: int = 0
    content_inserted: int = 0
    new_opinion: int = 0
    rediscovered: int = 0
