from __future__ import annotations

import asyncio
import math
import random
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.browser import BrowserProfileLocked, BrowserSession, summarize_browser_error
from opinion_watch.classification import (
    brand_matches_card,
    classify_batch,
    classify_content,
    is_suspected,
)
from opinion_watch.collectors import collector_for
from opinion_watch.collectors.base import BaseCollector, CollectorRuntimeError
from opinion_watch.config import Settings
from opinion_watch.events import serialize_event
from opinion_watch.llm import LLMAssessment, LLMCallBudget, classify_with_llm, screen_items_with_llm
from opinion_watch.maintenance import run_maintenance
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
    concurrency: int = 1
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
        if not 1 <= self.concurrency <= 4:
            raise ValueError("concurrency 必须在 1 到 4 之间")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_BLOCKING_STATUSES = {
    SessionStatus.LOGIN_REQUIRED,
    SessionStatus.VERIFICATION_REQUIRED,
    SessionStatus.CAPTCHA_REQUIRED,
    SessionStatus.RATE_LIMITED,
}

_WARNING_KIND_NAMES = {
    "zero_detail_coverage": "详情覆盖不足",
    "coverage_shortfall": "检索结果不足",
    "search_data_quality": "搜索卡片质量异常",
    "zero_results": "检索结果为空",
}
# 可见卡片数达到目标数的这一比例即视为覆盖正常。平台首屏本就会掺入
# "相关搜索"等非内容卡片并被采集器跳过，要求严格等于目标数会把每个
# 关键词都变成一条告警。
_COVERAGE_SHORTFALL_RATIO = 0.6
_WORKER_LEASE_SECONDS = 180
_WORKER_HEARTBEAT_SECONDS = 60
_BASELINE_DETAIL_RATIO = 0.25
_BASELINE_DETAIL_MAX = 10


async def _heartbeat_lease(
    storage: Storage,
    name: str,
    owner: str,
    *,
    cancel_task: asyncio.Task[Any] | None = None,
    heartbeat_seconds: float = _WORKER_HEARTBEAT_SECONDS,
) -> None:
    """Keep a worker lease alive while allowing quick recovery after a crash."""
    loop = asyncio.get_running_loop()
    last_success = loop.time()
    delay = heartbeat_seconds
    renewal_warning_created = False
    while True:
        await asyncio.sleep(delay)
        try:
            renewed = storage.heartbeat_task_lease(name, owner, lease_seconds=_WORKER_LEASE_SECONDS)
        except Exception as exc:
            # SQLite 短暂写锁不等于租约已丢失，直接让心跳任务崩掉反而会让
            # 主巡检永远继续运行。短间隔重试；距离租约到期只剩一个心跳
            # 周期时才停止主任务，给浏览器清理留出安全余量。
            if not renewal_warning_created:
                with suppress(Exception):
                    storage.create_alert(
                        kind="lease_heartbeat_error",
                        severity="warning",
                        message=f"任务租约 {name} 暂时无法续期，正在重试：{exc}",
                    )
                renewal_warning_created = True
            if loop.time() - last_success >= _WORKER_LEASE_SECONDS - _WORKER_HEARTBEAT_SECONDS:
                renewed = False
            else:
                delay = min(5.0, max(0.01, heartbeat_seconds))
                continue
        if not renewed:
            # 续租失败说明租约已经过期并被别人接手，而主流程还在照常抓取——
            # 此刻很可能有第二个进程在同一个浏览器档案上跑。静默 return 会让
            # 这种互斥失效完全查不出来，至少要留下一条告警。
            # 告警写库也可能正处在锁竞争中；它失败不能阻止后面的安全取消。
            with suppress(Exception):
                storage.create_alert(
                    kind="lease_lost",
                    severity="error",
                    message=(
                        f"任务租约 {name} 续租失败，可能已被其他进程接管；"
                        "本轮结果请人工确认，并检查是否有重复运行的巡检。"
                    ),
                )
            # 告警本身不能恢复互斥。一旦租约已被接管，继续抓取会让两个
            # worker 同时操作同一个持久化浏览器档案。直接取消主巡检任务，
            # 由它的 finally 关闭浏览器并释放仍属于自己的其他租约。
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel(f"lease_lost:{name}")
            return
        last_success = loop.time()
        delay = heartbeat_seconds
        renewal_warning_created = False


async def _prepare_account_status_page(page: Page, collector: BaseCollector) -> None:
    """在平台首页检查账号状态，避免把搜索路由遮罩误认为账号失效。

    首页往往挂着推荐流和一堆第三方资源，domcontentloaded 超时是常态。
    这个异常一旦冒泡就会被外层当成 browser_error，整个平台的全部关键词
    直接跳过。因此只要页面已经离开 about:blank（DOM 已经渲染出可判定的
    内容），超时就当作可接受；仍然停在空白页时才继续抛出，避免拿空白页
    去判登录态、把账号误标成已退出。
    """
    if str(getattr(page, "url", "")).lower() not in {"", "about:blank"}:
        return
    try:
        await page.goto(
            collector.home_url,
            wait_until="domcontentloaded",
            timeout=30_000,
        )
    except PlaywrightTimeoutError:
        if str(getattr(page, "url", "")).lower() in {"", "about:blank"}:
            raise


def _severity(status: SessionStatus) -> str:
    if status is SessionStatus.LOGIN_REQUIRED:
        return "error"
    if status in {
        SessionStatus.VERIFICATION_REQUIRED,
        SessionStatus.CAPTCHA_REQUIRED,
        SessionStatus.RATE_LIMITED,
    }:
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
                "brand_names": [item.brand_name] if item.brand_name else [],
                "raw_data": raw_data,
            }
        )
        suspected = is_suspected(result)
        brand_matched = brand_matches_card(
            {
                "title": getattr(item, "title", ""),
                "brand_names": [item.brand_name] if item.brand_name else [],
                "raw_data": raw_data,
            }
        )
        raw_data["precheck"] = {
            "suspected": suspected,
            "category": result.category.value,
            "severity": result.severity.value,
            "confidence": result.confidence,
            "matched_signals": result.matched_signals,
            "rationale": result.rationale,
            "requires_review": result.requires_review,
            "brand_matched": brand_matched,
        }
        screened_item = replace(item, raw_data=raw_data)
        screened.append(screened_item)
        if suspected and brand_matched:
            candidates.add(item.content_id)
    return screened, candidates


def _select_detail_candidates(
    items: list[CollectedContent],
    candidate_ids: set[str],
    *,
    detail_limit: int,
) -> set[str]:
    """Select suspected cards first, then the ordered two-card baseline sample."""
    if detail_limit <= 0:
        return set()
    suspected_ids: list[str] = []
    sample_ids: list[str] = []
    for item in items:
        if item.content_id not in candidate_ids:
            continue
        screening = item.raw_data.get("screening", {})
        decision = screening.get("decision") if isinstance(screening, dict) else ""
        if decision in {"规则疑似", "模型疑似"}:
            suspected_ids.append(item.content_id)
        elif decision == "保底抽查":
            sample_ids.append(item.content_id)
    ordered = [*suspected_ids, *sample_ids]
    return set(ordered[:detail_limit])


def _recheck_detail_items(
    items: list[CollectedContent],
    *,
    brand: str,
) -> tuple[list[CollectedContent], dict[str, str], int]:
    """Reclassify opened details and keep only confirmed or unresolved risks."""
    admitted: list[CollectedContent] = []
    filter_reasons: dict[str, str] = {}
    suspected_count = 0
    for item in items:
        raw_data = dict(item.raw_data)
        screening = dict(raw_data.get("screening", {}))
        decision = str(screening.get("decision") or "直接过滤")
        detail_collected = bool(raw_data.get("detail_collected"))
        if detail_collected:
            result = classify_content(
                {
                    "title": item.title,
                    "brand_names": [brand],
                    "raw_data": raw_data,
                }
            )
            final_suspected = is_suspected(result)
            screening.update(
                {
                    "detail_recheck": True,
                    "final_category": result.category.value,
                    "final_severity": result.severity.value,
                    "final_confidence": result.confidence,
                    "final_matched_signals": result.matched_signals,
                    "final_rationale": result.rationale,
                    "final_suspected": final_suspected,
                }
            )
            if not final_suspected:
                filter_reasons[item.content_id] = (
                    "保底抽查后无风险" if decision == "保底抽查" else "详情核查后无风险"
                )
                raw_data["screening"] = {**screening, "admitted": False}
                continue
            suspected_count += 1
        elif decision in {"规则疑似", "模型疑似"}:
            # A failed detail page must not make a brand warning disappear. Keep
            # the card-level decision and leave the failed detail visible in audit.
            screening.update(
                {
                    "detail_recheck": False,
                    "detail_failure": True,
                    "final_suspected": True,
                }
            )
            suspected_count += 1
        else:
            filter_reasons[item.content_id] = "详情失败"
            raw_data["screening"] = {**screening, "admitted": False}
            continue
        raw_data["screening"] = {**screening, "admitted": True}
        admitted.append(replace(item, raw_data=raw_data))
    return admitted, filter_reasons, suspected_count


async def _screen_items_for_admission(
    storage: Storage,
    items: list[CollectedContent],
    *,
    brand: str,
    budget: LLMCallBudget | None = None,
    baseline_limit: int = 0,
) -> tuple[list[CollectedContent], set[str], dict[str, Any]]:
    """Filter ordinary search cards before they reach the operational database."""
    screened, rule_candidates = _screen_items_for_detail(items)
    model_candidate_ids = set(rule_candidates)
    for item in screened:
        precheck = item.raw_data.get("precheck", {})
        if not isinstance(precheck, dict):
            continue
        # Ordinary/uncertain negative content is worth a model review. Clean
        # search cards never reach the model and are filtered by local rules.
        if precheck.get("brand_matched", True) and (
            precheck.get("category") == "ordinary_grievance"
            or bool(precheck.get("requires_review"))
            or bool(precheck.get("matched_signals"))
        ):
            model_candidate_ids.add(item.content_id)
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
        if item.content_id in model_candidate_ids
    ]
    model_assessments: dict[str, LLMAssessment] = {}
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
    baseline_candidates: list[CollectedContent] = []
    filter_reasons: dict[str, str] = {}
    for item in screened:
        key = f"{item.platform.value}:{item.content_id}"
        rule_data = item.raw_data.get("precheck", {})
        brand_matched = bool(rule_data.get("brand_matched", True))
        rule_suspected = item.content_id in rule_candidates and brand_matched
        model_result = model_assessments.get(key)
        if model_result is None:
            suspected = rule_suspected
            source = "rules"
            decision = "规则疑似" if suspected else "直接过滤"
            screening_data: dict[str, Any] = {
                "source": source,
                "admitted": suspected,
                "suspected": suspected,
                "category": rule_data.get("category", "other"),
                "severity": rule_data.get("severity", "P3"),
                "matched_signals": rule_data.get("matched_signals", []),
                "rationale": rule_data.get("rationale", ""),
                "brand_matched": brand_matched,
                "decision": decision,
            }
        else:
            model_suspected = model_result.category.value not in {"other", "irrelevant"}
            model_suspected = model_suspected or model_result.requires_review
            # 模型只有文本候选阶段的证据，不能把没有目标品牌精确提及的
            # 搜索噪声提升为正式舆情；这类内容仅保留在候选留痕中。
            model_suspected = model_suspected and brand_matched
            suspected = rule_suspected or model_suspected
            source = "model"
            decision = (
                "规则疑似" if rule_suspected else "模型疑似" if model_suspected else "直接过滤"
            )
            screening_data = {
                "source": source,
                "admitted": suspected,
                "suspected": suspected,
                "category": model_result.category.value,
                "severity": model_result.severity.value,
                "confidence": model_result.confidence,
                "matched_signals": model_result.matched_signals,
                "rationale": model_result.rationale,
                "brand_matched": brand_matched,
                "decision": decision,
            }
        raw_data = {**item.raw_data, "screening": screening_data}
        item = replace(item, raw_data=raw_data)
        if suspected:
            admitted.append(item)
            detail_candidates.add(item.content_id)
            continue
        if brand_matched:
            baseline_candidates.append(item)
            filter_reasons[item.content_id] = "品牌相关但未命中风险"
        else:
            filter_reasons[item.content_id] = "未出现目标品牌"

    # 不能每轮都固定抽查搜索排序最前的几条，否则头部内容会被反复打开，
    # 后面的卡片永远没有详情覆盖。按历史检查次数和最后检查时间排序：
    # 从未检查 -> 检查次数少 -> 最久未检查 -> 当前搜索顺序。
    history = storage.detail_sampling_history(baseline_candidates)
    original_order = {item.content_id: index for index, item in enumerate(baseline_candidates)}
    baseline_candidates.sort(
        key=lambda item: (
            int(history.get((item.platform.value, item.content_id), {}).get("check_count", 0)),
            str(
                history.get((item.platform.value, item.content_id), {}).get("last_checked_at") or ""
            ),
            original_order[item.content_id],
        )
    )
    for item in baseline_candidates[: max(0, baseline_limit)]:
        screening = dict(item.raw_data.get("screening", {}))
        screening.update({"decision": "保底抽查", "admitted": True})
        item = replace(item, raw_data={**item.raw_data, "screening": screening})
        admitted.append(item)
        detail_candidates.add(item.content_id)
        filter_reasons.pop(item.content_id, None)

    return (
        admitted,
        detail_candidates,
        {
            "scanned": len(items),
            "admitted": len(admitted),
            "filtered": len(items) - len(admitted),
            "suspected": len(detail_candidates),
            "model_attempted": model_attempted,
            "model_candidates": len(payloads),
            "model_errors": model_errors,
            "brand_matched": sum(
                1
                for item in screened
                if bool(item.raw_data.get("precheck", {}).get("brand_matched"))
            ),
            "filter_reasons": filter_reasons,
            "detail_priority": [item.content_id for item in admitted],
        },
    )


def _baseline_detail_limit(search_result_count: int) -> int:
    """Inspect a bounded rotating sample instead of the same two cards forever."""
    if search_result_count <= 0:
        return 0
    return min(
        _BASELINE_DETAIL_MAX,
        max(2, math.ceil(search_result_count * _BASELINE_DETAIL_RATIO)),
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
    if not storage.acquire_task_lease("scan", owner, lease_seconds=_WORKER_LEASE_SECONDS):
        raise RuntimeError("已有巡检任务正在运行，请等待当前任务完成。")
    worker_task = asyncio.current_task()
    heartbeat = asyncio.create_task(
        _heartbeat_lease(storage, "scan", owner, cancel_task=worker_task)
    )
    try:
        return await _run_scan_locked(
            settings, storage, platforms, owner=owner, options=options, trigger=trigger
        )
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
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
    llm_config = storage.get_llm_config()
    run_id = storage.create_scan_run(
        trigger=trigger,
        platforms=platform_values,
        brands=brands,
        options={
            **options.to_dict(),
            # Keep the setting that was actually used at run start. The
            # settings page can be edited while a subprocess is running, so
            # reading it only after completion is misleading.
            "llm_enabled_at_start": bool(llm_config.get("enabled")),
        },
    )
    totals = ScanTotals()
    coverage_shortfalls: list[str] = []
    # 预留 1/3 额度给最终复判：粗筛按关键词逐批消耗，排在它后面的复判否则会
    # 拿到 0 条额度而被静默跳过。至少留 1，保证小额度配置下复判也能跑起来。
    _llm_limit = int(llm_config.get("max_candidates") or 20)
    llm_budget = LLMCallBudget(_llm_limit, reserved=max(1, _llm_limit // 3) if _llm_limit else 0)
    run_warning_groups: dict[str, dict[str, Any]] = {}

    def record_run_warning(
        kind: str,
        message: str,
        *,
        severity: str = "warning",
        screenshot_path: str | None = None,
    ) -> None:
        """合并同一轮同类告警，避免每个关键词各生成一条播报。"""
        clean_message = message.strip()
        if clean_message and clean_message not in coverage_shortfalls:
            coverage_shortfalls.append(clean_message)
        group = run_warning_groups.setdefault(
            kind,
            {"messages": [], "severity": "warning", "screenshot_path": None},
        )
        messages = group["messages"]
        if clean_message and clean_message not in messages:
            messages.append(clean_message)
        if severity == "error":
            group["severity"] = "error"
        if screenshot_path and not group["screenshot_path"]:
            group["screenshot_path"] = screenshot_path

    print(
        serialize_event(
            "scan.started",
            {
                "run_id": run_id,
                "trigger": trigger,
                "status": ScanRunStatus.RUNNING.value,
                "platforms": platform_values,
                "brands": brands,
                "llm_enabled": bool(llm_config.get("enabled")),
            },
            ensure_ascii=False,
        )
    )

    try:
        for platform in platforms:
            # 必须显式传租期：默认值是 6 小时，一次省略就把 _heartbeat_lease
            # 精心维持的 180 秒租约顶成 6 小时。此后进程崩溃会让这个 run 卡在
            # running 六小时，期间无法再启动任何巡检。
            storage.heartbeat_task_lease("scan", owner, lease_seconds=_WORKER_LEASE_SECONDS)
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
                    serialize_event(
                        "scan.account_not_ready",
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
            ready_accounts = storage.list_scan_accounts(platform.value)
            if len(ready_accounts) > 1:
                selected_account: dict[str, Any] | None = None
                for candidate_account in ready_accounts:
                    candidate_lease_name = f"account:{int(candidate_account['id'])}"
                    if not storage.acquire_task_lease(
                        candidate_lease_name, owner, lease_seconds=_WORKER_LEASE_SECONDS
                    ):
                        continue
                    try:
                        async with BrowserSession(
                            settings.account_profile_dir(platform, int(candidate_account["id"])),
                            channel=settings.browser_channel,
                            headless=options.headless,
                            artifact_dir=settings.artifact_dir / platform.value,
                        ) as probe_session:
                            probe_page = await probe_session.page()
                            await _prepare_account_status_page(probe_page, collector)
                            probe_status = await collector.session_status(
                                probe_page, probe_session.active_context
                            )
                    except BrowserProfileLocked as exc:
                        # 档案被占用是瞬时竞争（用户自己开着那个 Chrome 窗口），
                        # 不是账号登录态问题。绝不能改成 error——那会让账号从
                        # list_scan_accounts 里掉出去，直到人工重新登录才回来。
                        storage.create_alert(
                            run_id=run_id,
                            platform=platform.value,
                            kind="account_busy",
                            severity="warning",
                            message=(
                                f"{platform.value} 账号 #{candidate_account['id']} "
                                f"的浏览器档案正在被占用，本轮跳过：{exc}"
                            ),
                        )
                        continue
                    except Exception as exc:
                        storage.update_account_status(int(candidate_account["id"]), "error")
                        storage.create_alert(
                            run_id=run_id,
                            platform=platform.value,
                            kind="account_probe_error",
                            severity="warning",
                            message=(
                                f"{platform.value} 账号 #{candidate_account['id']} "
                                f"登录态探测失败：{exc}"
                            ),
                        )
                        continue
                    finally:
                        storage.release_task_lease(candidate_lease_name, owner)
                    if probe_status is SessionStatus.HEALTHY:
                        selected_account = candidate_account
                        break
                    storage.update_account_status(
                        int(candidate_account["id"]), _account_status(probe_status)
                    )
                if selected_account is None:
                    totals.failed += 1
                    storage.create_alert(
                        run_id=run_id,
                        platform=platform.value,
                        kind="account_not_ready",
                        severity="error",
                        message=f"{platform.value} 的可用账号均未通过登录态检查。",
                    )
                    continue
                account = selected_account
            account_lease_name = f"account:{int(account['id'])}"
            if not storage.acquire_task_lease(
                account_lease_name, owner, lease_seconds=_WORKER_LEASE_SECONDS
            ):
                totals.failed += 1
                storage.create_alert(
                    run_id=run_id,
                    platform=platform.value,
                    kind="account_busy",
                    severity="warning",
                    message=f"{platform.value} 账号浏览器档案正在被其他任务使用。",
                )
                continue
            account_heartbeat = asyncio.create_task(
                _heartbeat_lease(
                    storage,
                    account_lease_name,
                    owner,
                    cancel_task=asyncio.current_task(),
                )
            )
            try:
                async with BrowserSession(
                    settings.account_profile_dir(platform, int(account["id"])),
                    channel=settings.browser_channel,
                    headless=options.headless,
                    artifact_dir=settings.artifact_dir / platform.value,
                ) as session:
                    page = await session.page()
                    await _prepare_account_status_page(page, collector)
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
                            serialize_event(
                                "scan.session_status",
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
                    prefetched_search: dict[str, list[CollectedContent] | Exception] = {}
                    prefetched_started_at: dict[str, str] = {}
                    prefetched_diagnostics: dict[str, str | None] = {}
                    prefetched_pages: dict[str, Page] = {}
                    if options.concurrency > 1 and len(targets) > 1:
                        # Keep one persistent profile/context per account, but use
                        # separate tabs for independent keyword searches. This is
                        # safe for the current account lease and avoids opening the
                        # same Chrome profile from multiple processes.
                        prefetch_semaphore = asyncio.Semaphore(options.concurrency)

                        async def prefetch_target(
                            target: dict[str, Any],
                            *,
                            semaphore: asyncio.Semaphore = prefetch_semaphore,
                            cache: dict[str, list[CollectedContent] | Exception] = (
                                prefetched_search
                            ),
                            started_at_cache: dict[str, str] = prefetched_started_at,
                            diagnostics_cache: dict[str, str | None] = prefetched_diagnostics,
                            pages_cache: dict[str, Page] = prefetched_pages,
                            target_platform: str = platform.value,
                            target_collector=collector,
                        ) -> None:
                            brand = str(target["brand_name"])
                            keyword = str(target["keyword"])
                            cache_key = f"{brand}\x00{keyword}"
                            async with semaphore:
                                prefetch_page = None
                                try:
                                    prefetch_page = await session.active_context.new_page()
                                    started_at_cache[cache_key] = datetime.now(UTC).isoformat()
                                    cache[cache_key] = await target_collector.search(
                                        prefetch_page,
                                        session.active_context,
                                        keyword,
                                        limit=options.limit,
                                        artifact_dir=settings.artifact_dir / target_platform,
                                        diagnostic_key=cache_key,
                                    )
                                    diagnostics_cache[cache_key] = (
                                        target_collector.pop_search_diagnostic(cache_key)
                                    )
                                    # Keep the page alive: detail enrichment must click
                                    # the same live card that produced these candidates.
                                    pages_cache[cache_key] = prefetch_page
                                    prefetch_page = None
                                except Exception as exc:
                                    # 缓存异常而不是抛出：gather 一旦抛出，其余
                                    # 预取任务会继续挂在即将关闭的浏览器上下文上。
                                    cache[cache_key] = exc
                                    if prefetch_page is not None:
                                        diagnostic = await session.capture_diagnostic(
                                            prefetch_page,
                                            f"{target_platform}-search-error",
                                        )
                                        # Path 对象不能作为 SQLite 参数绑定；预取阶段
                                        # 缓存的诊断路径必须与后续 attempt 持久化边界一致。
                                        diagnostics_cache[cache_key] = (
                                            str(diagnostic) if diagnostic else None
                                        )
                                finally:
                                    if prefetch_page is not None:
                                        with suppress(Exception):
                                            await prefetch_page.close()

                        await asyncio.gather(*(prefetch_target(target) for target in targets))
                    for target_index, target in enumerate(targets):
                        brand = str(target["brand_name"])
                        keyword = str(target["keyword"])
                        cache_key = f"{brand}\x00{keyword}"
                        cached_search = prefetched_search.pop(cache_key, None)
                        detail_search_page = prefetched_pages.pop(cache_key, None) or page
                        platform_blocked = False
                        for attempt_no in range(1, options.retries + 2):
                            attempt_id = storage.create_scan_attempt(
                                run_id=run_id,
                                platform=platform.value,
                                keyword=keyword,
                                attempt_no=attempt_no,
                                started_at=prefetched_started_at.pop(cache_key, None),
                            )
                            diagnostic_path = prefetched_diagnostics.pop(cache_key, None)
                            quality_partial = False
                            quality_message = ""
                            detail_coverage_partial = False
                            detail_coverage_message = ""
                            try:
                                if isinstance(cached_search, Exception):
                                    search_items = None
                                    prefetch_error = cached_search
                                    cached_search = None
                                    raise prefetch_error
                                if cached_search is not None:
                                    search_items = cached_search
                                    cached_search = None
                                else:
                                    search_items = await collector.search(
                                        detail_search_page,
                                        session.active_context,
                                        keyword,
                                        limit=options.limit,
                                        artifact_dir=settings.artifact_dir / platform.value,
                                        diagnostic_key=cache_key,
                                    )
                                    diagnostic_path = collector.pop_search_diagnostic(cache_key)
                                search_items = [
                                    replace(item, brand_name=brand) for item in search_items
                                ]
                                raw_search_items = search_items
                                ignored_ids = storage.ignored_content_ids(
                                    platform.value, (item.content_id for item in search_items)
                                )
                                ignored_filter_reasons = {
                                    content_id: "永久忽略" for content_id in ignored_ids
                                }
                                search_items = [
                                    item
                                    for item in search_items
                                    if item.content_id not in ignored_ids
                                ]
                                quality = collector.search_quality(search_items)
                                if quality["needs_diagnostic"] and quality["total"] > 0:
                                    diagnostic_path = diagnostic_path or next(
                                        (
                                            str(item.raw_data.get("search_diagnostic_path"))
                                            for item in search_items
                                            if item.raw_data.get("search_diagnostic_path")
                                        ),
                                        None,
                                    )
                                    quality_message = (
                                        f"{platform.value}/{keyword} 检索到 "
                                        f"{quality['total']} 条候选，"
                                        f"其中 {quality['empty_title']} 条标题为空"
                                        f"（{quality['empty_title_ratio']:.0%}），"
                                        "搜索卡片文本质量不足，已保存搜索页诊断。"
                                    )
                                    if quality["no_usable_text"]:
                                        storage.save_scan_candidates(
                                            run_id=run_id,
                                            attempt_id=attempt_id,
                                            items=search_items,
                                        )
                                        storage.mark_scan_candidates(
                                            attempt_id=attempt_id,
                                            admitted_content_ids=(),
                                            filter_reason="搜索卡片标题全部为空，未进入详情和入库",
                                        )
                                        storage.finish_scan_attempt(
                                            attempt_id,
                                            status="failed",
                                            scanned=quality["total"],
                                            filtered=quality["total"],
                                            error_status="data_quality_error",
                                            error_message=quality_message,
                                            screenshot_path=diagnostic_path,
                                        )
                                        record_run_warning(
                                            "search_data_quality",
                                            quality_message,
                                            severity="error",
                                            screenshot_path=diagnostic_path,
                                        )
                                        totals.failed += 1
                                        print(
                                            serialize_event(
                                                "scan.attempt_failed",
                                                {
                                                    "run_id": run_id,
                                                    "attempt_id": attempt_id,
                                                    "platform": platform.value,
                                                    "keyword": keyword,
                                                    "status": "data_quality_error",
                                                    "message": quality_message,
                                                    "screenshot": diagnostic_path,
                                                },
                                                ensure_ascii=False,
                                            )
                                        )
                                        break
                                    if quality["degrades_run"]:
                                        quality_partial = True
                                        # 标题为空但卡片原文尚在时，品牌匹配仍然
                                        # 成立。只丢弃真正没有任何可判断文本的卡片，
                                        # 否则一次标题选择器退化就会静默漏掉舆情。
                                        search_items = [
                                            item
                                            for item in search_items
                                            if item.title.strip()
                                            or str(
                                                item.raw_data.get("search_card_text") or ""
                                            ).strip()
                                        ]
                                storage.save_scan_candidates(
                                    run_id=run_id,
                                    attempt_id=attempt_id,
                                    items=raw_search_items,
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
                                    baseline_limit=_baseline_detail_limit(len(search_items)),
                                )
                                screening_stats["filter_reasons"].update(ignored_filter_reasons)
                                detail_ids = _select_detail_candidates(
                                    items,
                                    detail_candidate_ids,
                                    detail_limit=options.detail_limit,
                                )
                                detail_scope: list[CollectedContent] = []
                                for item in items:
                                    screening = item.raw_data.get("screening", {})
                                    decision = (
                                        screening.get("decision")
                                        if isinstance(screening, dict)
                                        else ""
                                    )
                                    if decision == "保底抽查" and item.content_id not in detail_ids:
                                        screening_stats["filter_reasons"][item.content_id] = (
                                            "详情名额已由疑似项占用，未执行保底抽查"
                                        )
                                        continue
                                    detail_scope.append(item)
                                items = detail_scope
                                if quality_partial:
                                    invalid_count = quality["total"] - len(search_items)
                                    screening_stats["scanned"] = quality["total"]
                                    screening_stats["filtered"] += invalid_count
                                    record_run_warning(
                                        "search_data_quality",
                                        quality_message,
                                        screenshot_path=diagnostic_path,
                                    )
                                enriched_items = await collector.enrich_items(
                                    session.active_context,
                                    items,
                                    detail_limit=options.detail_limit,
                                    comments_limit=options.comments_limit,
                                    detail_candidate_ids=detail_ids,
                                    artifact_dir=settings.artifact_dir / platform.value / "media",
                                    search_page=detail_search_page,
                                )
                                items, detail_filter_reasons, final_suspected_count = (
                                    _recheck_detail_items(enriched_items, brand=brand)
                                )
                                screening_stats["filter_reasons"].update(detail_filter_reasons)
                                screening_stats["admitted"] = len(items)
                                screening_stats["filtered"] = screening_stats["scanned"] - len(
                                    items
                                )
                                screening_stats["suspected"] = final_suspected_count
                                detail_audits: dict[str, dict[str, Any]] = {
                                    item.content_id: {
                                        "detail_status": "not_selected",
                                        "detail_checked_at": None,
                                        "detail_error": "",
                                        "triage_decision": (
                                            "永久忽略"
                                            if item.content_id in ignored_ids
                                            else "直接过滤"
                                        ),
                                    }
                                    for item in raw_search_items
                                }
                                for item in enriched_items:
                                    screening = item.raw_data.get("screening", {})
                                    detail_audits[item.content_id] = {
                                        "detail_status": (
                                            item.raw_data.get("detail_status")
                                            or (
                                                "succeeded"
                                                if item.raw_data.get("detail_collected")
                                                else "not_selected"
                                            )
                                        ),
                                        "detail_checked_at": item.raw_data.get("detail_checked_at"),
                                        "detail_error": item.raw_data.get("detail_error", ""),
                                        "triage_decision": (
                                            screening.get("decision", "")
                                            if isinstance(screening, dict)
                                            else ""
                                        ),
                                    }
                                storage.mark_scan_candidates(
                                    attempt_id=attempt_id,
                                    admitted_content_ids=(item.content_id for item in items),
                                    filter_reasons=screening_stats["filter_reasons"],
                                    detail_audits=detail_audits,
                                )
                                stats = storage.upsert_contents(items, allow_brand_creation=False)
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
                                screenshot = None
                                if not diagnostic_path:
                                    screenshot = await session.capture_diagnostic(
                                        detail_search_page,
                                        f"{platform.value}-{exc.status.value}",
                                    )
                                screenshot_path = diagnostic_path or (
                                    str(screenshot) if screenshot else None
                                )
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
                                    serialize_event(
                                        "scan.attempt_failed",
                                        {
                                            "run_id": run_id,
                                            "attempt_id": attempt_id,
                                            "attempt": attempt_no,
                                            "platform": platform.value,
                                            "brand": brand,
                                            "keyword": keyword,
                                            "url": BaseCollector.canonical_url(page.url),
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
                                # 搜索路由的登录遮罩、限流和页面错误不代表账号
                                # 已退出。只有明确的登录失效才能覆盖账号状态。
                                if exc.status is SessionStatus.LOGIN_REQUIRED:
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
                                screenshot = None
                                if not diagnostic_path:
                                    screenshot = await session.capture_diagnostic(
                                        detail_search_page,
                                        f"{platform.value}-unexpected-error",
                                    )
                                screenshot_path = diagnostic_path or (
                                    str(screenshot) if screenshot else None
                                )
                                storage.finish_scan_attempt(
                                    attempt_id,
                                    status="failed",
                                    error_status=SessionStatus.ERROR.value,
                                    error_message=str(exc),
                                    screenshot_path=screenshot_path,
                                )
                                retrying = attempt_no <= options.retries
                                print(
                                    serialize_event(
                                        "scan.attempt_error",
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
                                    int(
                                        item.raw_data.get("detail_status") == "succeeded"
                                        or (
                                            not item.raw_data.get("detail_status")
                                            and bool(item.raw_data.get("detail_collected"))
                                        )
                                    )
                                    for item in enriched_items
                                )
                                detail_attempted_count = len(detail_ids)
                                detail_unavailable_count = sum(
                                    int(item.raw_data.get("detail_status") == "unavailable")
                                    for item in enriched_items
                                )
                                media_count = sum(
                                    len(item.raw_data.get("media", []))
                                    for item in items
                                    if isinstance(item.raw_data.get("media", []), list)
                                )
                                scanned_count = int(screening_stats["scanned"])
                                filtered_count = int(screening_stats["filtered"])
                                suspected_count = int(screening_stats["suspected"])
                                if (
                                    options.detail_limit > 0
                                    and int(screening_stats.get("brand_matched", 0)) > 0
                                    and detailed_count == 0
                                ):
                                    detail_coverage_partial = True
                                    detail_coverage_message = (
                                        f"{platform.value}/{keyword} 已命中品牌卡片，"
                                        "但详情核查为 0 条；"
                                        "请检查详情页加载、登录态或平台页面结构。"
                                    )
                                    detail_errors = [
                                        str(item.raw_data.get("detail_error") or "").strip()
                                        for item in enriched_items
                                        if str(item.raw_data.get("detail_error") or "").strip()
                                    ]
                                    if detail_errors:
                                        detail_coverage_message += (
                                            f"首条详情错误：{detail_errors[0][:240]}"
                                        )
                                    coverage_diagnostic = diagnostic_path
                                    if not coverage_diagnostic:
                                        captured = await session.capture_diagnostic(
                                            detail_search_page,
                                            f"{platform.value}-zero-detail",
                                        )
                                        coverage_diagnostic = str(captured) if captured else None
                                    record_run_warning(
                                        "zero_detail_coverage",
                                        detail_coverage_message,
                                        screenshot_path=coverage_diagnostic,
                                    )
                                else:
                                    coverage_diagnostic = diagnostic_path
                                # Zero results can be a legitimate no-match
                                # search. Warn when the platform returned a
                                # partial page, which is the actionable case
                                # for coverage and loading diagnostics.
                                #
                                # 采集器会主动跳过"相关搜索"等噪声卡片，所以满页
                                # 也常常只剩 16-18 条有效卡片。把目标数当硬下限会
                                # 让每个关键词都告警一次，真正的覆盖问题反而被淹没。
                                # 只有明显低于目标时才值得人工核查。
                                coverage_floor = int(options.limit * _COVERAGE_SHORTFALL_RATIO)
                                if 0 < scanned_count < coverage_floor:
                                    shortfall = (
                                        f"{platform.value}/{keyword} 实际检索 {scanned_count} 条，"
                                        f"低于目标 {options.limit} 条"
                                    )
                                    record_run_warning(
                                        "coverage_shortfall",
                                        "平台当前可见结果不足目标数量，已完成可见范围扫描："
                                        + shortfall,
                                    )
                                elif scanned_count == 0:
                                    # A silent selector break looks identical to
                                    # "no results"; surface it for human triage.
                                    record_run_warning(
                                        "zero_results",
                                        f"{platform.value}/{keyword} 检索结果为 0 条。"
                                        "可能确实没有公开结果，也可能是页面结构变化导致"
                                        "选择器失效，建议人工打开搜索页核对一次。",
                                        screenshot_path=diagnostic_path,
                                    )
                                totals.scanned += scanned_count
                                totals.filtered += filtered_count
                                totals.suspected += suspected_count
                                totals.detailed += detailed_count
                                totals.media_items += media_count
                                totals.brand_matched += int(screening_stats.get("brand_matched", 0))
                                totals.detail_attempted += detail_attempted_count
                                totals.detail_unavailable += detail_unavailable_count
                                totals.collected += len(items)
                                totals.inserted += stats.inserted
                                totals.updated += stats.updated
                                totals.content_inserted += stats.content_inserted
                                totals.new_opinion += stats.new_opinion
                                totals.rediscovered += stats.rediscovered
                                attempt_status = (
                                    "partial"
                                    if quality_partial or detail_coverage_partial
                                    else "succeeded"
                                )
                                if quality_partial or detail_coverage_partial:
                                    totals.partial += 1
                                else:
                                    totals.succeeded += 1
                                storage.link_scan_contents(
                                    run_id=run_id,
                                    attempt_id=attempt_id,
                                    items=items,
                                )
                                storage.finish_scan_attempt(
                                    attempt_id,
                                    status=attempt_status,
                                    collected=len(items),
                                    scanned=scanned_count,
                                    filtered=filtered_count,
                                    inserted=stats.inserted,
                                    updated=stats.updated,
                                    partial=int(attempt_status == "partial"),
                                    suspected=suspected_count,
                                    detailed=detailed_count,
                                    media_items=media_count,
                                    brand_matched=int(screening_stats.get("brand_matched", 0)),
                                    detail_attempted=detail_attempted_count,
                                    detail_unavailable=detail_unavailable_count,
                                    content_inserted=stats.content_inserted,
                                    new_opinion=stats.new_opinion,
                                    rediscovered=stats.rediscovered,
                                    error_status=(
                                        "data_quality_warning"
                                        if quality_partial
                                        else "zero_detail_coverage"
                                        if detail_coverage_partial
                                        else ""
                                    ),
                                    error_message=(
                                        quality_message
                                        if quality_partial
                                        else detail_coverage_message
                                        if detail_coverage_partial
                                        else ""
                                    ),
                                    screenshot_path=coverage_diagnostic,
                                )
                                print(
                                    serialize_event(
                                        "scan.attempt_succeeded",
                                        {
                                            "run_id": run_id,
                                            "attempt_id": attempt_id,
                                            "attempt": attempt_no,
                                            "platform": platform.value,
                                            "brand": brand,
                                            "keyword": keyword,
                                            "status": attempt_status,
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

                        if detail_search_page is not page:
                            with suppress(Exception):
                                await detail_search_page.close()  # type: ignore[attr-defined]
                        if platform_blocked:
                            break
                        if target_index < len(targets) - 1 and options.brand_delay_seconds > 0:
                            # 关键词间隔加随机抖动，避免形成机械的固定访问节奏。
                            await asyncio.sleep(
                                options.brand_delay_seconds * random.uniform(0.7, 1.6)
                            )
                    # 预取会为每个关键词开一个常驻标签页，正常路径上逐个处理、逐个关闭。
                    # 但 platform_blocked 会直接 break，剩下的标签页没人认领，只能等
                    # 整个上下文关闭时才回收；被限流时这恰恰是最不该继续占着资源的时刻。
                    for leftover in prefetched_pages.values():
                        with suppress(Exception):
                            await leftover.close()
                    prefetched_pages.clear()
            except BrowserProfileLocked as exc:
                totals.failed += 1
                message = f"账号浏览器档案正在使用，未改动账号登录状态：{exc}"
                storage.create_alert(
                    run_id=run_id,
                    platform=platform.value,
                    kind="account_busy",
                    severity="warning",
                    message=message,
                )
                print(
                    serialize_event(
                        "scan.browser_error",
                        {
                            "run_id": run_id,
                            "platform": platform.value,
                            "status": "account_busy",
                            "message": f"{platform.value}：{message}",
                        },
                        ensure_ascii=False,
                    )
                )
            except Exception as exc:
                totals.failed += 1
                # 浏览器进程异常、Chrome 更新或机器资源问题不能证明账号已
                # 退出登录。保留上一次可信的账号状态，避免界面误报“登录异常”。
                message = summarize_browser_error(exc)
                storage.create_alert(
                    run_id=run_id,
                    platform=platform.value,
                    kind="browser_error",
                    severity="error",
                    message=message,
                )
                print(
                    serialize_event(
                        "scan.browser_error",
                        {
                            "run_id": run_id,
                            "platform": platform.value,
                            "status": "browser_error",
                            "message": f"{platform.value}：{message}",
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                account_heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await account_heartbeat
                storage.release_task_lease(account_lease_name, owner)
    except asyncio.CancelledError as exc:
        cancellation_reason = str(exc)
        lease_lost = cancellation_reason.startswith("lease_lost:")
        message = (
            f"任务租约丢失，巡检已中止（{cancellation_reason.removeprefix('lease_lost:')}）"
            if lease_lost
            else "任务被取消"
        )
        storage.terminate_scan_run(
            run_id,
            status=(
                ScanRunStatus.INTERRUPTED.value if lease_lost else ScanRunStatus.CANCELLED.value
            ),
            reason=message,
        )
        raise

    for kind, group in run_warning_groups.items():
        messages = group["messages"]
        if not messages:
            continue
        # 同类告警始终合并成一条。每个关键词各发一条时，播报页会被同一个
        # 问题的多条重复消息刷屏，而合并后的摘要保留了全部明细。
        if len(messages) > 1:
            kind_name = _WARNING_KIND_NAMES.get(kind, kind)
            summary = f"本轮巡检共 {len(messages)} 项{kind_name}：\n" + "\n".join(
                f"- {message}" for message in messages
            )
            messages = [summary]
        for message in messages:
            storage.create_alert(
                run_id=run_id,
                kind=kind,
                severity=str(group["severity"]),
                message=message,
                screenshot_path=group["screenshot_path"],
            )

    classification_summary: dict[str, Any] | None = None
    model_summary: dict[str, Any] | None = None
    # A completed card scan with detail warnings still produced auditable
    # content. Continue classification, clustering, and notifications for it.
    if totals.collected > 0:
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
            model_summary["enabled_at_start"] = bool(llm_config.get("enabled"))
            if model_summary.get("failed"):
                storage.create_alert(
                    run_id=run_id,
                    kind="llm_classification_error",
                    severity="warning",
                    message="；".join(str(item) for item in model_summary.get("errors", []))
                    or "部分内容的大模型复判失败。",
                )
        except Exception as exc:
            model_summary = {
                "enabled": bool(llm_config.get("enabled")),
                "enabled_at_start": bool(llm_config.get("enabled")),
                "processed": 0,
                "failed": 1,
            }
            storage.create_alert(
                run_id=run_id,
                kind="llm_classification_error",
                severity="warning",
                message=f"大模型复判未执行：{exc}",
            )
        try:
            # Rebuild from the full suspected/high-risk history so a new run does not
            # erase clusters created by earlier runs. The run remains available in
            # scan_run_contents for filtering/reporting at read time.
            cluster_count = storage.rebuild_event_clusters()
            if classification_summary is None:
                classification_summary = {}
            classification_summary["event_clusters"] = cluster_count
        except Exception as exc:
            storage.create_alert(
                run_id=run_id,
                kind="event_clustering_error",
                severity="warning",
                message=f"事件聚合未执行：{exc}",
            )

    if totals.failed == 0 and totals.partial == 0:
        status = ScanRunStatus.SUCCEEDED
    elif totals.succeeded > 0 or totals.partial > 0 or totals.collected > 0:
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
        partial=totals.partial,
        failed=totals.failed,
        suspected=totals.suspected,
        detailed=totals.detailed,
        media_items=totals.media_items,
        brand_matched=totals.brand_matched,
        detail_attempted=totals.detail_attempted,
        detail_unavailable=totals.detail_unavailable,
        content_inserted=totals.content_inserted,
        new_opinion=totals.new_opinion,
        rediscovered=totals.rediscovered,
        note=("；".join(coverage_shortfalls) if coverage_shortfalls else ""),
        classification_summary=classification_summary,
        model_summary=model_summary,
    )
    if storage.maintenance_due("retention"):
        try:
            run_maintenance(storage, settings.artifact_dir)
            storage.mark_maintenance_succeeded("retention")
        except Exception as exc:
            # 数据治理失败不能抹掉已经完成的巡检，但必须可见，避免磁盘在后台
            # 无上限增长。
            storage.create_alert(
                run_id=run_id,
                kind="maintenance_error",
                severity="warning",
                message=f"历史候选或诊断文件清理失败：{exc}",
            )
    wecom_report_sent = False
    if trigger in {"watch", "manual"} and status in {
        ScanRunStatus.SUCCEEDED,
        ScanRunStatus.PARTIAL,
    }:
        try:
            wecom_report_sent = await send_daily_report_if_due(
                storage,
                scan_run_id=run_id,
                force=trigger == "manual",
            )
        except Exception as exc:
            storage.create_alert(
                run_id=run_id,
                kind="wecom_report_error",
                severity="error",
                message=f"企微日报发送失败：{exc}",
            )
    print(
        serialize_event(
            "scan.finished",
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
