from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opinion_watch.classification import classify_batch
from opinion_watch.models import CollectedContent, Platform, ScanTotals
from opinion_watch.storage import Storage

_RELEVANCE_VALUES = {"relevant", "uncertain", "irrelevant"}
_PLATFORM_HOSTS = {
    Platform.DOUYIN: ("douyin.com", "iesdouyin.com"),
    Platform.XIAOHONGSHU: ("xiaohongshu.com", "xhslink.com"),
}
_CONTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True, slots=True)
class AgentCandidate:
    item: CollectedContent
    relevance: str
    detail_collected: bool


def _canonical_url(platform: Platform, value: object) -> str:
    text = str(value or "").strip()
    parts = urlsplit(text)
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise ValueError("候选 url 必须是完整的 HTTPS 地址")
    hostname = (parts.hostname or "").lower()
    allowed_hosts = _PLATFORM_HOSTS[platform]
    if not any(hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts):
        raise ValueError(f"候选 url 域名与平台 {platform.value} 不匹配")
    return urlunsplit(("https", parts.netloc.lower(), parts.path, "", ""))


def _content_id(platform: Platform, value: object, url: str) -> str:
    explicit = str(value or "").strip()
    if explicit:
        if not _CONTENT_ID_PATTERN.fullmatch(explicit):
            raise ValueError("platform_content_id 只能包含字母、数字、下划线和连字符")
        return explicit
    digest = hashlib.sha256(f"{platform.value}:{url}".encode()).hexdigest()[:32]
    return f"agent-{digest}"


def _string_list(value: object, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_record(record: object, *, line_number: int) -> AgentCandidate:
    if not isinstance(record, dict):
        raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
    try:
        platform = Platform(str(record.get("platform") or "").strip())
    except ValueError as exc:
        raise ValueError(f"第 {line_number} 行 platform 不合法") from exc
    brand = str(record.get("brand") or record.get("brand_name") or "").strip()
    if not brand:
        raise ValueError(f"第 {line_number} 行缺少 brand")
    keyword = str(record.get("keyword") or brand).strip()
    url = _canonical_url(platform, record.get("url"))
    relevance = str(record.get("relevance") or "uncertain").strip().lower()
    if relevance not in _RELEVANCE_VALUES:
        raise ValueError(f"第 {line_number} 行 relevance 必须是 relevant、uncertain 或 irrelevant")
    detail_value = record.get("detail_collected", False)
    if not isinstance(detail_value, bool):
        raise ValueError(f"第 {line_number} 行 detail_collected 必须是布尔值")
    detail_collected = detail_value
    description = str(record.get("description") or record.get("snippet") or "").strip()
    title = str(record.get("title") or "").strip() or description[:200]
    if not title:
        raise ValueError(f"第 {line_number} 行缺少 title 或 snippet")
    evidence = _string_list(record.get("evidence"), field="evidence")
    comments = _string_list(record.get("comments"), field="comments")
    risk_signals = _string_list(record.get("risk_signals"), field="risk_signals")
    relevance_reason = str(record.get("relevance_reason") or "").strip()
    decision = {
        "relevant": "Agent 判断与品牌相关",
        "uncertain": "Agent 判断品牌相关性不确定",
        "irrelevant": "Agent 判断与品牌无关",
    }[relevance]
    raw_data: dict[str, Any] = {
        "ingest_source": "agent",
        "agent_relevance": relevance,
        "relevance_reason": relevance_reason,
        "risk_signals": risk_signals,
        "evidence": evidence,
        "description": description,
        "comments": comments,
        "detail_collected": detail_collected,
        "detail_status": "succeeded" if detail_collected else "not_selected",
        "candidate_stage": "detailed" if detail_collected else "shallow",
        "screening": {
            "decision": decision,
            "admitted": relevance != "irrelevant",
        },
    }
    task_id = str(record.get("agent_task_id") or "").strip()
    if task_id:
        raw_data["agent_task_id"] = task_id
    metrics = record.get("metrics")
    if metrics is not None and not isinstance(metrics, dict):
        raise ValueError(f"第 {line_number} 行 metrics 必须是 JSON 对象")
    published_at = str(record.get("published_at") or "").strip() or None
    discovered_at = str(record.get("collected_at") or "").strip() or datetime.now(UTC).isoformat()
    return AgentCandidate(
        item=CollectedContent(
            platform=platform,
            content_id=_content_id(
                platform,
                record.get("platform_content_id") or record.get("content_id"),
                url,
            ),
            url=url,
            title=title[:500],
            source_keyword=keyword,
            brand_name=brand,
            author_name=str(record.get("author") or record.get("author_name") or "").strip()[:200],
            published_at=published_at,
            metrics=dict(metrics or {}),
            raw_data=raw_data,
            discovered_at=discovered_at,
        ),
        relevance=relevance,
        detail_collected=detail_collected,
    )


def load_agent_jsonl(path: Path) -> list[AgentCandidate]:
    if not path.is_file():
        raise ValueError(f"Agent 候选文件不存在：{path}")
    candidates: list[AgentCandidate] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON：{exc.msg}") from exc
        candidates.append(_parse_record(record, line_number=line_number))
    if not candidates:
        raise ValueError("Agent 候选文件中没有可导入记录")
    return candidates


def _ingest_agent_jsonl_locked(storage: Storage, path: Path) -> dict[str, Any]:
    """Import one validated Agent result file through the normal audit pipeline."""
    candidates = load_agent_jsonl(path)
    configured_brands = {str(item["name"]) for item in storage.list_brands()}
    missing_brands = sorted(
        {candidate.item.brand_name for candidate in candidates} - configured_brands
    )
    if missing_brands:
        raise ValueError("以下品牌尚未在系统中配置：" + "、".join(missing_brands))

    platforms = list(dict.fromkeys(candidate.item.platform.value for candidate in candidates))
    brands = list(dict.fromkeys(candidate.item.brand_name for candidate in candidates))
    run_id = storage.create_scan_run(
        trigger="agent",
        platforms=platforms,
        brands=brands,
        options={"source": "agent-jsonl", "input_file": path.name},
        title="Agent 候选导入 " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
    )
    totals = ScanTotals()
    grouped: dict[tuple[Platform, str, str], list[AgentCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[
            (
                candidate.item.platform,
                candidate.item.brand_name,
                candidate.item.source_keyword,
            )
        ].append(candidate)

    current_attempt_id: int | None = None
    try:
        for attempt_no, ((platform, _brand, keyword), group) in enumerate(grouped.items(), 1):
            attempt_id = storage.create_scan_attempt(
                run_id=run_id,
                platform=platform.value,
                keyword=keyword,
                attempt_no=attempt_no,
            )
            current_attempt_id = attempt_id
            items = [candidate.item for candidate in group]
            storage.save_scan_candidates(run_id=run_id, attempt_id=attempt_id, items=items)
            admitted = [
                candidate.item for candidate in group if candidate.relevance != "irrelevant"
            ]
            filtered = len(group) - len(admitted)
            detail_audits = {
                candidate.item.content_id: {
                    "detail_status": "succeeded" if candidate.detail_collected else "not_selected",
                    "detail_checked_at": (
                        candidate.item.discovered_at if candidate.detail_collected else None
                    ),
                    "triage_decision": str(
                        candidate.item.raw_data.get("screening", {}).get("decision", "")
                    ),
                }
                for candidate in group
            }
            storage.mark_scan_candidates(
                attempt_id=attempt_id,
                admitted_content_ids=(item.content_id for item in admitted),
                filter_reasons={
                    candidate.item.content_id: "Agent 判断与品牌无关"
                    for candidate in group
                    if candidate.relevance == "irrelevant"
                },
                detail_audits=detail_audits,
            )
            stats = storage.upsert_contents(admitted, allow_brand_creation=False)
            storage.link_scan_contents(run_id=run_id, attempt_id=attempt_id, items=admitted)
            detailed = sum(candidate.detail_collected for candidate in group)
            brand_matched = sum(candidate.relevance == "relevant" for candidate in group)
            storage.finish_scan_attempt(
                attempt_id,
                status="succeeded",
                collected=len(admitted),
                scanned=len(group),
                filtered=filtered,
                inserted=stats.inserted,
                updated=stats.updated,
                detailed=detailed,
                brand_matched=brand_matched,
                detail_attempted=detailed,
                content_inserted=stats.content_inserted,
                new_opinion=stats.new_opinion,
                rediscovered=stats.rediscovered,
            )
            current_attempt_id = None
            totals.scanned += len(group)
            totals.collected += len(admitted)
            totals.filtered += filtered
            totals.inserted += stats.inserted
            totals.updated += stats.updated
            totals.succeeded += 1
            totals.detailed += detailed
            totals.brand_matched += brand_matched
            totals.detail_attempted += detailed
            totals.content_inserted += stats.content_inserted
            totals.new_opinion += stats.new_opinion
            totals.rediscovered += stats.rediscovered

        # Agent runs often rediscover an existing item with richer detail. Re-run the
        # rule assessment for every item linked to this run so the saved assessment
        # reflects the newest evidence instead of silently keeping a shallow result.
        classification = classify_batch(
            storage,
            limit=max(100, len(candidates)),
            force=True,
            run_id=run_id,
        )
        classification["event_clusters"] = storage.rebuild_event_clusters()
        totals.suspected = int(classification.get("requires_review", 0))
        storage.finish_scan_run(
            run_id,
            status="succeeded",
            collected=totals.collected,
            scanned=totals.scanned,
            filtered=totals.filtered,
            inserted=totals.inserted,
            updated=totals.updated,
            succeeded=totals.succeeded,
            suspected=totals.suspected,
            detailed=totals.detailed,
            brand_matched=totals.brand_matched,
            detail_attempted=totals.detail_attempted,
            content_inserted=totals.content_inserted,
            new_opinion=totals.new_opinion,
            rediscovered=totals.rediscovered,
            classification_summary=classification,
        )
    except Exception as exc:
        if current_attempt_id is not None:
            storage.finish_scan_attempt(
                current_attempt_id,
                status="failed",
                error_status="agent_ingest_error",
                error_message=str(exc),
            )
        storage.finish_scan_run(
            run_id,
            status="failed",
            collected=totals.collected,
            scanned=totals.scanned,
            filtered=totals.filtered,
            inserted=totals.inserted,
            updated=totals.updated,
            succeeded=totals.succeeded,
            failed=1,
            detailed=totals.detailed,
            brand_matched=totals.brand_matched,
            detail_attempted=totals.detail_attempted,
            content_inserted=totals.content_inserted,
            new_opinion=totals.new_opinion,
            rediscovered=totals.rediscovered,
            error_message=str(exc),
        )
        raise

    return {
        "run_id": run_id,
        "source": "agent",
        "scanned": totals.scanned,
        "admitted": totals.collected,
        "filtered": totals.filtered,
        "detailed": totals.detailed,
        "requires_review": totals.suspected,
        "inserted": totals.inserted,
        "updated": totals.updated,
        "event_clusters": int(classification.get("event_clusters", 0)),
    }


def ingest_agent_jsonl(storage: Storage, path: Path) -> dict[str, Any]:
    """Import one Agent file while excluding concurrent browser scans and imports."""
    owner = f"agent-ingest:{uuid.uuid4()}"
    if not storage.acquire_task_lease("scan", owner, lease_seconds=900):
        raise RuntimeError("已有巡检或 Agent 导入正在运行，请稍后重试")
    try:
        return _ingest_agent_jsonl_locked(storage, path)
    finally:
        storage.release_task_lease("scan", owner)
