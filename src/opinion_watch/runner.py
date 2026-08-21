from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from opinion_watch.browser import BrowserSession
from opinion_watch.classification import classify_batch, classify_content, is_suspected
from opinion_watch.collectors import collector_for
from opinion_watch.collectors.base import CollectorRuntimeError
from opinion_watch.config import Settings
from opinion_watch.llm import LLMCallBudget, classify_with_llm, screen_items_with_llm
from opinion_watch.models import (
    CollectedContent,
    Platform,
    ScanRunStatus,
    ScanTotals,
    SessionStatus,
)
from opinion_watch.storage import Storage
from opinion_watch.wecom import send_daily_report_if_due


@dataclass(frozen=True, slots=True)
class ScanOptions:
    mode: str = "quick"
    limit: int = 20
    detail_limit: int = 20
    comments_limit: int = 20
    retries: int = 1
    retry_delay_seconds: float = 5
    brand_delay_seconds: float = 3
    headless: bool = False

    def validate(self) -> None:
        if self.mode not in {"quick", "deep"}:
            raise ValueError("巡检模式必须是 quick 或 deep")
        minimum = 50 if self.mode == "deep" else 20
        if not minimum <= self.limit <= 100:
            raise ValueError(f"{self.mode} 巡检的 limit 必须在 {minimum} 到 100 之间")
        if not 0 <= self.detail_limit <= 100:
            raise ValueError("detail-limit 必须在 0 到 100 之间")
        if not 0 <= self.comments_limit <= 1000:
            raise ValueError("comments-limit 必须在 0 到 1000 之间")
        if not 0 <= self.retries <= 5:
            raise ValueError("retries 必须在 0 到 5 之间")
        if not 0 <= self.retry_delay_seconds <= 3600:
            raise ValueError("retry-delay-seconds 必须在 0 到 3600 之间")
        if not 0 <= self.brand_delay_seconds <= 3600:
            raise ValueError("brand-delay-seconds 必须在 0 到 3600 之间")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_BLOCKING_STATUSES = {
    SessionStatus.LOGIN_REQUIRED,
    SessionStatus.VERIFICATION_REQUIRED,
    SessionStatus.RATE_LIMITED,
}


def _severity(status: SessionStatus) -> str:
    if status is SessionStatus.LOGIN_REQUIRED:
        return "error"
    if status in {SessionStatus.VERIFICATION_REQUIRED, SessionStatus.RATE_LIMITED}:
        return "warning"
    return "error"


def _account_status(status: SessionStatus) -> str:
    if status is SessionStatus.HEALTHY:
        return "ready"
    return status.value


def _screen_items_for_detail(
    items: list[CollectedContent],
) -> tuple[list[CollectedContent], set[str]]:
    """Run the cheap list-level rules before opening any detail pages."""
    screened: list[CollectedContent] = []
    candidates: set[str] = set()
    for item in items:
        # Keep this helper typed loosely so collectors can remain platform-neutral.
        raw_data = dict(item.raw_data)
        result = classify_content(
            {
                "title": getattr(item, "title", ""),
                "brand_names": [],
                "raw_data": raw_data,
            }
        )
        suspected = is_suspected(result)
        raw_data["precheck"] = {
            "suspected": suspected,
            "category": result.category.value,
            "severity": result.severity.value,
            "confidence": result.confidence,
            "matched_signals": result.matched_signals,
            "rationale": result.rationale,
        }
        screened_item = replace(item, raw_data=raw_data)
        screened.append(screened_item)
        if suspected:
            candidates.add(item.content_id)
    return screened, candidates


async def _screen_items_for_admission(
    storage: Storage,
    items: list[CollectedContent],
    *,
    brand: str,
    budget: LLMCallBudget | None = None,
) -> tuple[list[CollectedContent], set[str], dict[str, object]]:
    """Filter ordinary search cards before they reach the operational database."""
    screened, rule_candidates = _screen_items_for_detail(items)
    payloads = [
        {
            "platform": item.platform.value,
            "platform_content_id": item.content_id,
            "url": item.url,
            "title": item.title,
            "author_name": item.author_name,
            "brand_names": [brand],
            "raw_data": item.raw_data,
        }
        for item in screened
    ]
    model_assessments = {}
    model_errors: list[str] = []
    model_attempted = 0
    try:
        if budget is None:
            model_assessments, model_errors, model_attempted = await screen_items_with_llm(
                storage, payloads
            )
        else:
            model_assessments, model_errors, model_attempted = await screen_items_with_llm(
                storage, payloads, budget=budget
            )
    except Exception as exc:
        model_errors = [str(exc)[:300]]

    admitted: list[CollectedContent] = []
    detail_candidates: set[str] = set()
    for item in screened:
        key = f"{item.platform.value}:{item.content_id}"
        rule_data = item.raw_data.get("precheck", {})
        rule_admitted = item.content_id in rule_candidates
        model_result = model_assessments.get(key)
        if model_result is None:
            keep = rule_admitted
            source = "rules"
            suspected = rule_admitted
            screening_data: dict[str, object] = {
                "source": source,
                "admitted": keep,
                "suspected": suspected,
                "category": rule_data.get("category", "other"),
                "severity": rule_data.get("severity", "P3"),
                "matched_signals": rule_data.get("matched_signals", []),
                "rationale": rule_data.get("rationale", ""),
            }
        else:
            keep = model_result.category.value not in {"other", "irrelevant"}
            keep = keep or model_result.requires_review
            source = "model"
            suspected = keep
            screening_data = {
                "source": source,
                "admitted": keep,
                "suspected": suspected,
                "category": model_result.category.value,
                "severity": model_result.severity.value,
                "confidence": model_result.confidence,
                "matched_signals": model_result.matched_signals,
                "rationale": model_result.rationale,
            }
        raw_data = {**item.raw_data, "screening": screening_data}
        item = replace(item, raw_data=raw_data)
        if not keep:
            continue
        admitted.append(item)
        if suspected:
            detail_candidates.add(item.content_id)

    return (
        admitted,
        detail_candidates,
        {
            "scanned": len(items),
            "admitted": len(admitted),
            "filtered": len(items) - len(admitted),
            "suspected": len(detail_candidates),
            "model_attempted": model_attempted,
            "model_errors": model_errors,
        },
    )


async def run_scan(
    settings: Settings,
    storage: Storage,
    platforms: Sequence[Platform],
    *,
    options: ScanOptions,
    trigger: str = "manual",
) -> int:
    """Run one scan under a process-wide SQLite lease."""
    options.validate()
    storage.recover_stale_scan_runs()
    owner = str(uuid.uuid4())
    if not storage.acquire_task_lease("scan", owner):
        raise RuntimeError("已有巡检任务正在运行，请等待当前任务完成。")
    try:
        return await _run_scan_locked(
            settings, storage, platforms, owner=owner, options=options, trigger=trigger
        )
    finally:
        storage.release_task_lease("scan", owner)


async def _run_scan_locked(
    settings: Settings,
    storage: Storage,
    platforms: Sequence[Platform],
    *,
    owner: str,
    options: ScanOptions,
    trigger: str = "manual",
) -> int:
    options.validate()
    targets = storage.list_scan_targets()
    if not targets:
        raise ValueError("没有启用的检索关键词，请先添加并启用品牌关键词。")
    brands = list(dict.fromkeys(str(item["brand_name"]) for item in targets))

    platform_values = [platform.value for platform in platforms]
    run_id = storage.create_scan_run(
        trigger=trigger,
        platforms=platform_values,
        brands=brands,
        options=options.to_dict(),
    )
    totals = ScanTotals()
    llm_config = storage.get_llm_config()
    llm_budget = LLMCallBudget(int(llm_config.get("max_candidates") or 20))
    print(
        json.dumps(
            {
                "run_id": run_id,
                "trigger": trigger,
                "status": ScanRunStatus.RUNNING.value,
                "platforms": platform_values,
                "brands": brands,
            },
            ensure_ascii=False,
        )
    )

    try:
        for platform in platforms:
            storage.heartbeat_task_lease("scan", owner)
            collector = collector_for(platform)
            account = storage.get_scan_account(platform.value)
            if account is None:
                totals.failed += 1
                storage.create_alert(
                    run_id=run_id,
                    platform=platform.value,
                    kind="account_not_ready",
                    severity="error",
                    message=(
                        f"{platform.value} 没有可用于自动巡检的已登录账号，请先登录并检查账号状态。"
                    ),
                )
                print(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "platform": platform.value,
                            "status": "account_not_ready",
                            "message": "没有可用于自动巡检的已登录账号。",
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            try:
                async with BrowserSession(
                    settings.account_profile_dir(platform, int(account["id"])),
                    channel=settings.browser_channel,
                    headless=options.headless,
                    artifact_dir=settings.artifact_dir / platform.value,
                ) as session:
                    page = await session.page()
                    session_status = await collector.session_status(page, session.active_context)
                    if session_status is not SessionStatus.HEALTHY:
                        totals.failed += 1
                        storage.update_account_status(
                            int(account["id"]), _account_status(session_status)
                        )
                        message = f"{platform.value} 登录档案状态：{session_status.value}。"
                        storage.create_alert(
                            run_id=run_id,
                            platform=platform.value,
                            kind=session_status.value,
                            severity=_severity(session_status),
                            message=message,
                        )
                        print(
                            json.dumps(
                                {
                                    "run_id": run_id,
                                    "platform": platform.value,
                                    "status": session_status.value,
                                    "message": message,
                                },
                                ensure_ascii=False,
                            )
                        )
                        continue
                    storage.update_account_status(int(account["id"]), "ready")
                    for target_index, target in enumerate(targets):
                        brand = str(target["brand_name"])
                        keyword = str(target["keyword"])
                        platform_blocked = False
                        for attempt_no in range(1, options.retries + 2):
                            attempt_id = storage.create_scan_attempt(
                                run_id=run_id,
                                platform=platform.value,
                                keyword=keyword,
                                attempt_no=attempt_no,
                            )
                            try:
                                search_items = await collector.search(
                                    page,
                                    session.active_context,
                                    keyword,
                                    limit=options.limit,
                                )
                                search_items = [
                                    replace(item, brand_name=brand) for item in search_items
                                ]
                                storage.save_scan_candidates(
                                    run_id=run_id,
                                    attempt_id=attempt_id,
                                    items=search_items,
                                )
                                (
                                    items,
                                    detail_candidate_ids,
                                    screening_stats,
                                ) = await _screen_items_for_admission(
                                    storage,
                                    search_items,
                                    brand=brand,
                                    budget=llm_budget,
                                )
                                storage.mark_scan_candidates(
                                    attempt_id=attempt_id,
                                    admitted_content_ids=(item.content_id for item in items),
                                )
                                items = await collector.enrich_items(
                                    session.active_context,
                                    items,
                                    detail_limit=options.detail_limit,
                                    comments_limit=options.comments_limit,
                                    detail_candidate_ids=detail_candidate_ids,
                                    artifact_dir=settings.artifact_dir / platform.value / "media",
                                )
                                stats = storage.upsert_contents(items)
                                if screening_stats["model_errors"]:
                                    storage.create_alert(
                                        run_id=run_id,
                                        attempt_id=attempt_id,
                                        platform=platform.value,
                                        keyword=keyword,
                                        kind="llm_screening_error",
                                        severity="warning",
                                        message=(
                                            "入库前大模型筛选部分失败，已对未完成模型判断的内容回退规则筛选："
                                            + "；".join(
                                                str(error)
                                                for error in screening_stats["model_errors"]
                                            )
                                        ),
                                    )
                            except CollectorRuntimeError as exc:
                                screenshot = await session.capture_diagnostic(
                                    page,
                                    f"{platform.value}-{exc.status.value}",
                                )
                                screenshot_path = str(screenshot) if screenshot else None
                                storage.finish_scan_attempt(
                                    attempt_id,
                                    status="failed",
                                    error_status=exc.status.value,
                                    error_message=str(exc),
                                    screenshot_path=screenshot_path,
                                )
                                retrying = (
                                    exc.status is SessionStatus.ERROR
                                    and attempt_no <= options.retries
                                )
                                print(
                                    json.dumps(
                                        {
                                            "run_id": run_id,
                                            "attempt_id": attempt_id,
                                            "attempt": attempt_no,
                                            "platform": platform.value,
                                            "brand": brand,
                                            "keyword": keyword,
                                            "url": page.url,
                                            "status": (
                                                "retrying" if retrying else exc.status.value
                                            ),
                                            "message": str(exc),
                                            "screenshot": screenshot_path,
                                        },
                                        ensure_ascii=False,
                                    )
                                )
                                if retrying:
                                    await asyncio.sleep(options.retry_delay_seconds)
                                    continue

                                totals.failed += 1
                                storage.update_account_status(
                                    int(account["id"]), _account_status(exc.status)
                                )
                                storage.create_alert(
                                    run_id=run_id,
                                    attempt_id=attempt_id,
                                    platform=platform.value,
                                    keyword=keyword,
                                    kind=exc.status.value,
                                    severity=_severity(exc.status),
                                    message=str(exc),
                                    screenshot_path=screenshot_path,
                                )
                                platform_blocked = exc.status in _BLOCKING_STATUSES
                                break
                            except Exception as exc:
                                screenshot = await session.capture_diagnostic(
                                    page,
                                    f"{platform.value}-unexpected-error",
                                )
                                screenshot_path = str(screenshot) if screenshot else None
                                storage.finish_scan_attempt(
                                    attempt_id,
                                    status="failed",
                                    error_status=SessionStatus.ERROR.value,
                                    error_message=str(exc),
                                    screenshot_path=screenshot_path,
                                )
                                retrying = attempt_no <= options.retries
                                print(
                                    json.dumps(
                                        {
                                            "run_id": run_id,
                                            "attempt_id": attempt_id,
                                            "attempt": attempt_no,
                                            "platform": platform.value,
                                            "brand": brand,
                                            "keyword": keyword,
                                            "status": "retrying" if retrying else "error",
                                            "message": str(exc),
                                            "screenshot": screenshot_path,
                                        },
                                        ensure_ascii=False,
                                    )
                                )
                                if retrying:
                                    await asyncio.sleep(options.retry_delay_seconds)
                                    continue

                                totals.failed += 1
                                storage.create_alert(
                                    run_id=run_id,
                                    attempt_id=attempt_id,
                                    platform=platform.value,
                                    keyword=keyword,
                                    kind="unexpected_error",
                                    severity="error",
                                    message=str(exc),
                                    screenshot_path=screenshot_path,
                                )
                                break
                            else:
                                detailed_count = sum(
                                    int(bool(item.raw_data.get("detail_collected")))
                                    for item in items
                                )
                                media_count = sum(
                                    len(item.raw_data.get("media", []))
                                    for item in items
                                    if isinstance(item.raw_data.get("media", []), list)
                                )
                                scanned_count = int(screening_stats["scanned"])
                                filtered_count = int(screening_stats["filtered"])
                                suspected_count = int(screening_stats["suspected"])
                                totals.scanned += scanned_count
                                totals.filtered += filtered_count
                                totals.suspected += suspected_count
                                totals.detailed += detailed_count
                                totals.media_items += media_count
                                totals.collected += len(items)
                                totals.inserted += stats.inserted
                                totals.updated += stats.updated
                                totals.succeeded += 1
                                storage.link_scan_contents(
                                    run_id=run_id,
                                    attempt_id=attempt_id,
                                    items=items,
                                )
                                storage.finish_scan_attempt(
                                    attempt_id,
                                    status="succeeded",
                                    collected=len(items),
                                    scanned=scanned_count,
                                    filtered=filtered_count,
                                    inserted=stats.inserted,
                                    updated=stats.updated,
                                    suspected=suspected_count,
                                    detailed=detailed_count,
                                    media_items=media_count,
                                )
                                print(
                                    json.dumps(
                                        {
                                            "run_id": run_id,
                                            "attempt_id": attempt_id,
                                            "attempt": attempt_no,
                                            "platform": platform.value,
                                            "brand": brand,
                                            "keyword": keyword,
                                            "status": "succeeded",
                                            "scanned": scanned_count,
                                            "collected": len(items),
                                            "filtered": filtered_count,
                                            "suspected": suspected_count,
                                            "detailed": detailed_count,
                                            "media_items": media_count,
                                            "inserted": stats.inserted,
                                            "updated": stats.updated,
                                        },
                                        ensure_ascii=False,
                                    )
                                )
                                break

                        if platform_blocked:
                            break
                        if target_index < len(targets) - 1 and options.brand_delay_seconds > 0:
                            await asyncio.sleep(options.brand_delay_seconds)
            except Exception as exc:
                totals.failed += 1
                storage.update_account_status(int(account["id"]), "error")
                storage.create_alert(
                    run_id=run_id,
                    platform=platform.value,
                    kind="browser_error",
                    severity="error",
                    message=str(exc),
                )
                print(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "platform": platform.value,
                            "status": "browser_error",
                            "message": str(exc),
                        },
                        ensure_ascii=False,
                    )
                )
    except asyncio.CancelledError:
        storage.finish_scan_run(
            run_id,
            status=ScanRunStatus.CANCELLED.value,
            scanned=totals.scanned,
            collected=totals.collected,
            filtered=totals.filtered,
            inserted=totals.inserted,
            updated=totals.updated,
            succeeded=totals.succeeded,
            failed=totals.failed,
            error_message="任务被取消",
        )
        raise

    classification_summary: dict[str, object] | None = None
    model_summary: dict[str, object] | None = None
    if totals.succeeded > 0:
        try:
            classification_summary = classify_batch(
                storage,
                limit=min(1000, max(100, totals.collected)),
                force=True,
                run_id=run_id,
            )
        except Exception as exc:
            totals.failed += 1
            storage.create_alert(
                run_id=run_id,
                kind="classification_error",
                severity="error",
                message=str(exc),
            )
        try:
            model_summary = await classify_with_llm(storage, run_id=run_id, budget=llm_budget)
            if model_summary.get("failed"):
                storage.create_alert(
                    run_id=run_id,
                    kind="llm_classification_error",
                    severity="warning",
                    message="；".join(str(item) for item in model_summary.get("errors", []))
                    or "部分内容的大模型复判失败。",
                )
        except Exception as exc:
            model_summary = {"enabled": True, "processed": 0, "failed": 1}
            storage.create_alert(
                run_id=run_id,
                kind="llm_classification_error",
                severity="warning",
                message=f"大模型复判未执行：{exc}",
            )

    if totals.failed == 0:
        status = ScanRunStatus.SUCCEEDED
    elif totals.succeeded > 0:
        status = ScanRunStatus.PARTIAL
    else:
        status = ScanRunStatus.FAILED
    storage.finish_scan_run(
        run_id,
        status=status.value,
        scanned=totals.scanned,
        collected=totals.collected,
        filtered=totals.filtered,
        inserted=totals.inserted,
        updated=totals.updated,
        succeeded=totals.succeeded,
        failed=totals.failed,
        suspected=totals.suspected,
        detailed=totals.detailed,
        media_items=totals.media_items,
        classification_summary=classification_summary,
        model_summary=model_summary,
    )
    wecom_report_sent = False
    if trigger == "watch" and status in {ScanRunStatus.SUCCEEDED, ScanRunStatus.PARTIAL}:
        try:
            wecom_report_sent = await send_daily_report_if_due(storage, scan_run_id=run_id)
        except Exception as exc:
            storage.create_alert(
                run_id=run_id,
                kind="wecom_report_error",
                severity="error",
                message=f"企微日报发送失败：{exc}",
            )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": status.value,
                **asdict(totals),
                "mode": options.mode,
                "classification": classification_summary,
                "model_classification": model_summary,
                "wecom_report_sent": wecom_report_sent,
            },
            ensure_ascii=False,
        )
    )
    return 0 if status is ScanRunStatus.SUCCEEDED else 2
