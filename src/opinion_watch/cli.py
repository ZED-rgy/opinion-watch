from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from opinion_watch.browser import BrowserProfileLocked, BrowserSession
from opinion_watch.classification import classify_batch
from opinion_watch.collectors import collector_for
from opinion_watch.collectors.base import CollectorRuntimeError
from opinion_watch.config import DEFAULT_BRANDS, Settings
from opinion_watch.credentials import CredentialStore
from opinion_watch.llm import LLMClient, LLMSettings
from opinion_watch.models import OpinionCategory, Platform, RiskSeverity, SessionStatus
from opinion_watch.runner import ScanOptions, _screen_items_for_admission, run_scan
from opinion_watch.scheduling import next_scheduled_datetime
from opinion_watch.storage import Storage
from opinion_watch.wecom import WeComClient


def _configure_utf8_output() -> None:
    """Keep collected Unicode content printable on Windows legacy code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _platform(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in Platform)
        raise argparse.ArgumentTypeError(f"未知平台 {value!r}，可选值：{choices}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="品牌舆情监控浏览器采集 POC")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化运行目录、数据库和默认品牌")
    subparsers.add_parser("doctor", help="检查 Chrome 和 Playwright 是否可以启动")

    login = subparsers.add_parser("login", help="打开专用浏览器并手动登录")
    login.add_argument("--platform", type=_platform, required=True)
    login.add_argument("--account-id", type=int)

    check = subparsers.add_parser("check", help="检查平台登录态")
    check.add_argument("--platform", type=_platform, required=True)
    check.add_argument("--account-id", type=int)
    check.add_argument("--headless", action="store_true")

    search = subparsers.add_parser("search", help="搜索单个平台的单个关键词")
    search.add_argument("--platform", type=_platform, required=True)
    search.add_argument("--keyword", required=True)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--detail-limit", type=int, default=5)
    search.add_argument("--comments-limit", type=int, default=20)
    search.add_argument("--headless", action="store_true")

    scan = subparsers.add_parser("scan", help="顺序扫描启用品牌")
    _add_scan_arguments(scan)
    scan.add_argument("--trigger", choices=("manual", "watch"), default="manual")

    watch = subparsers.add_parser("watch", help="按固定间隔循环扫描")
    _add_scan_arguments(watch)
    watch.add_argument("--interval-minutes", type=float, default=60)
    watch.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="最多运行次数，0 表示持续运行",
    )

    brand = subparsers.add_parser("brand", help="品牌增删改查")
    brand_subparsers = brand.add_subparsers(dest="brand_command", required=True)
    brand_subparsers.add_parser("list")
    brand_add = brand_subparsers.add_parser("add")
    brand_add.add_argument("name")
    brand_rename = brand_subparsers.add_parser("rename")
    brand_rename.add_argument("old_name")
    brand_rename.add_argument("new_name")
    for command in ("enable", "disable", "delete"):
        child = brand_subparsers.add_parser(command)
        child.add_argument("name")

    keyword = subparsers.add_parser("keyword", help="品牌检索关键词管理")
    keyword_subparsers = keyword.add_subparsers(dest="keyword_command", required=True)
    keyword_list = keyword_subparsers.add_parser("list")
    keyword_list.add_argument("--brand")
    keyword_add = keyword_subparsers.add_parser("add")
    keyword_add.add_argument("brand_name")
    keyword_add.add_argument("keyword")
    keyword_rename = keyword_subparsers.add_parser("rename")
    keyword_rename.add_argument("keyword_id", type=int)
    keyword_rename.add_argument("new_keyword")
    for command in ("enable", "disable", "delete"):
        child = keyword_subparsers.add_parser(command)
        child.add_argument("keyword_id", type=int)

    account = subparsers.add_parser("account", help="平台账号管理")
    account_subparsers = account.add_subparsers(dest="account_command", required=True)
    account_subparsers.add_parser("list")
    account_add = account_subparsers.add_parser("add")
    account_add.add_argument("--platform", type=_platform, required=True)
    account_add.add_argument("display_name")
    account_status = account_subparsers.add_parser("status")
    account_status.add_argument("account_id", type=int)
    account_status.add_argument(
        "status",
        choices=("not_logged_in", "ready", "login_required", "verification_required"),
    )
    for command in ("enable", "disable", "delete"):
        child = account_subparsers.add_parser(command)
        child.add_argument("account_id", type=int)

    run_history = subparsers.add_parser("run", help="查看扫描运行历史")
    run_subparsers = run_history.add_subparsers(dest="run_command", required=True)
    run_list = run_subparsers.add_parser("list")
    run_list.add_argument("--limit", type=int, default=20)
    run_show = run_subparsers.add_parser("show")
    run_show.add_argument("run_id", type=int)

    alert = subparsers.add_parser("alert", help="查看和确认运行告警")
    alert_subparsers = alert.add_subparsers(dest="alert_command", required=True)
    alert_list = alert_subparsers.add_parser("list")
    alert_list.add_argument("--limit", type=int, default=50)
    alert_list.add_argument("--all", action="store_true", help="包含已确认告警")
    alert_ack = alert_subparsers.add_parser("ack")
    alert_ack.add_argument("alert_id", type=int)

    notification = subparsers.add_parser("notification", help="应用内通知")
    notification_subparsers = notification.add_subparsers(
        dest="notification_command", required=True
    )
    notification_list = notification_subparsers.add_parser("list")
    notification_list.add_argument("--limit", type=int, default=100)
    notification_list.add_argument("--all", action="store_true", help="包含已读通知")
    notification_read = notification_subparsers.add_parser("read")
    notification_read.add_argument("notification_id", type=int)

    data = subparsers.add_parser("data", help="运行数据检查与清理")
    data_subparsers = data.add_subparsers(dest="data_command", required=True)
    data_subparsers.add_parser("status")
    data_reset = data_subparsers.add_parser("reset")
    data_reset.add_argument(
        "--confirm",
        required=True,
        help="必须明确传入 RESET；执行前自动备份数据库",
    )

    classify = subparsers.add_parser("classify", help="舆情分类、查询和人工复核")
    classify_subparsers = classify.add_subparsers(dest="classify_command", required=True)
    classify_run = classify_subparsers.add_parser("run")
    classify_run.add_argument("--limit", type=int, default=100)
    classify_run.add_argument("--force", action="store_true", help="重新运行自动规则")
    classify_list = classify_subparsers.add_parser("list")
    classify_list.add_argument("--limit", type=int, default=100)
    classify_list.add_argument("--severity", choices=[item.value for item in RiskSeverity])
    classify_list.add_argument("--needs-review", action="store_true")
    classify_show = classify_subparsers.add_parser("show")
    classify_show.add_argument("content_item_id", type=int)
    classify_review = classify_subparsers.add_parser("review")
    classify_review.add_argument("content_item_id", type=int)
    classify_review.add_argument(
        "--category",
        required=True,
        choices=[item.value for item in OpinionCategory],
    )
    classify_review.add_argument(
        "--severity",
        required=True,
        choices=[item.value for item in RiskSeverity],
    )
    classify_review.add_argument("--note", default="")
    classify_review.add_argument("--reviewer", default="operator")

    wecom = subparsers.add_parser("wecom", help="企微智能机器人日报")
    wecom_subparsers = wecom.add_subparsers(dest="wecom_command", required=True)
    wecom_subparsers.add_parser("test", help="向已配置群聊发送测试消息")
    wecom_discover = wecom_subparsers.add_parser("discover", help="监听群聊消息并发现目标群聊 ID")
    wecom_discover.add_argument("--timeout-seconds", type=int, default=120)

    llm = subparsers.add_parser("llm", help="大模型辅助研判配置与测试")
    llm_subparsers = llm.add_subparsers(dest="llm_command", required=True)
    llm_subparsers.add_parser("test", help="测试已配置的大模型接口")

    return parser


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--platforms",
        nargs="+",
        type=_platform,
        default=list(Platform),
    )
    parser.add_argument("--mode", choices=("quick", "deep"), default="quick")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--detail-limit", type=int, default=None)
    parser.add_argument("--comments-limit", type=int, default=20)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-delay-seconds", type=float, default=5)
    parser.add_argument("--brand-delay-seconds", type=float, default=3)
    parser.add_argument("--headless", action="store_true")


async def _doctor(settings: Settings) -> int:
    profile_dir = settings.runtime_dir / "browser-profiles" / "_doctor"
    async with BrowserSession(
        profile_dir,
        channel=settings.browser_channel,
        headless=True,
        artifact_dir=settings.artifact_dir / "doctor",
    ) as session:
        page = await session.page()
        await page.set_content("<title>opinion-watch-doctor</title><main>ok</main>")
        payload = {
            "browser_channel": settings.browser_channel,
            "page_title": await page.title(),
            "status": "ok",
        }
        print(json.dumps(payload, ensure_ascii=False))
    return 0


def _account_profile(
    settings: Settings,
    storage: Storage,
    platform: Platform,
    account_id: int | None,
) -> tuple[object, object]:
    if account_id is None:
        return settings.profile_dir(platform), None
    account = next(
        (item for item in storage.list_accounts() if int(item["id"]) == account_id),
        None,
    )
    if account is None:
        raise ValueError(f"未找到账号 ID：{account_id}")
    if str(account["platform"]) != platform.value:
        raise ValueError("账号所属平台与命令行参数不一致")
    return settings.account_profile_dir(platform, account_id), account


async def _login(
    settings: Settings, storage: Storage, platform: Platform, account_id: int | None
) -> int:
    collector = collector_for(platform)
    profile_dir, account = _account_profile(settings, storage, platform, account_id)
    lease_name = f"account:{int(account['id'])}" if account is not None else None
    owner = f"login-{uuid.uuid4()}"
    if lease_name is not None and not storage.acquire_task_lease(lease_name, owner):
        raise BrowserProfileLocked("该账号浏览器档案正在被其他任务使用，请先关闭对应窗口。")
    try:
        async with BrowserSession(
            profile_dir,
            channel=settings.browser_channel,
            headless=False,
            artifact_dir=settings.artifact_dir / platform.value,
        ) as session:
            page = await session.page()
            await page.goto(collector.home_url, wait_until="domcontentloaded", timeout=60_000)
            print(f"已打开 {platform.value}。请在浏览器中完成登录。")
            await asyncio.to_thread(input, "登录完成后回到此处按回车关闭浏览器：")
            status = await collector.session_status(page, session.active_context)
            if account is not None:
                storage.update_account_status(
                    int(account["id"]), "ready" if status is SessionStatus.HEALTHY else status.value
                )
            print(
                json.dumps({"platform": platform.value, "status": status.value}, ensure_ascii=False)
            )
            return 0 if status is SessionStatus.HEALTHY else 2
    finally:
        if lease_name is not None:
            storage.release_task_lease(lease_name, owner)


async def _check(
    settings: Settings,
    storage: Storage,
    platform: Platform,
    account_id: int | None,
    headless: bool,
) -> int:
    collector = collector_for(platform)
    profile_dir, account = _account_profile(settings, storage, platform, account_id)
    lease_name = f"account:{int(account['id'])}" if account is not None else None
    owner = f"check-{uuid.uuid4()}"
    if lease_name is not None and not storage.acquire_task_lease(lease_name, owner):
        raise BrowserProfileLocked("该账号浏览器档案正在被其他任务使用，请先关闭对应窗口。")
    try:
        async with BrowserSession(
            profile_dir,
            channel=settings.browser_channel,
            headless=headless,
            artifact_dir=settings.artifact_dir / platform.value,
        ) as session:
            page = await session.page()
            await page.goto(collector.home_url, wait_until="domcontentloaded", timeout=60_000)
            status = await collector.session_status(page, session.active_context)
            if account is not None:
                storage.update_account_status(
                    int(account["id"]), "ready" if status is SessionStatus.HEALTHY else status.value
                )
            print(
                json.dumps({"platform": platform.value, "status": status.value}, ensure_ascii=False)
            )
            return 0 if status is SessionStatus.HEALTHY else 2
    finally:
        if lease_name is not None:
            storage.release_task_lease(lease_name, owner)


async def _search_one(
    settings: Settings,
    storage: Storage,
    platform: Platform,
    keyword: str,
    limit: int,
    detail_limit: int,
    comments_limit: int,
    headless: bool,
) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1 到 100 之间")
    collector = collector_for(platform)
    async with BrowserSession(
        settings.profile_dir(platform),
        channel=settings.browser_channel,
        headless=headless,
        artifact_dir=settings.artifact_dir / platform.value,
    ) as session:
        page = await session.page()
        try:
            items = await collector.search(
                page,
                session.active_context,
                keyword,
                limit=limit,
            )
            items, detail_candidate_ids, screening_stats = await _screen_items_for_admission(
                storage,
                items,
                brand=keyword,
            )
            items = await collector.enrich_items(
                session.active_context,
                items,
                detail_limit=detail_limit,
                comments_limit=comments_limit,
                detail_candidate_ids=detail_candidate_ids,
                artifact_dir=settings.artifact_dir / platform.value / "media",
            )
        except CollectorRuntimeError as exc:
            screenshot = await session.capture_diagnostic(
                page, f"{platform.value}-{exc.status.value}"
            )
            payload = {
                "platform": platform.value,
                "keyword": keyword,
                "url": page.url,
                "status": exc.status.value,
                "message": str(exc),
                "screenshot": str(screenshot) if screenshot else None,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 2

        stats = storage.upsert_contents(items)
        empty_screenshot = None
        if not items:
            empty_screenshot = await session.capture_diagnostic(
                page,
                f"{platform.value}-empty-results",
            )
        print(
            json.dumps(
                {
                    "platform": platform.value,
                    "keyword": keyword,
                    "url": page.url,
                    "scanned": screening_stats["scanned"],
                    "collected": len(items),
                    "filtered": screening_stats["filtered"],
                    "inserted": stats.inserted,
                    "updated": stats.updated,
                    "screenshot": str(empty_screenshot) if empty_screenshot else None,
                },
                ensure_ascii=False,
            )
        )
    return 0


async def _scan(
    settings: Settings,
    storage: Storage,
    platforms: Sequence[Platform],
    mode: str,
    limit: int | None,
    detail_limit: int | None,
    comments_limit: int,
    headless: bool,
    retries: int,
    retry_delay_seconds: float,
    brand_delay_seconds: float,
    trigger: str = "manual",
) -> int:
    resolved_limit = limit if limit is not None else (50 if mode == "deep" else 20)
    resolved_detail_limit = detail_limit if detail_limit is not None else resolved_limit
    return await run_scan(
        settings,
        storage,
        platforms,
        options=ScanOptions(
            mode=mode,
            limit=resolved_limit,
            detail_limit=resolved_detail_limit,
            comments_limit=comments_limit,
            retries=retries,
            retry_delay_seconds=retry_delay_seconds,
            brand_delay_seconds=brand_delay_seconds,
            headless=headless,
        ),
        trigger=trigger,
    )


async def _watch(settings: Settings, storage: Storage, args: argparse.Namespace) -> int:
    if args.interval_minutes < 5:
        raise ValueError("interval-minutes 必须在 5 到 1440 分钟之间")
    if args.interval_minutes > 1440:
        raise ValueError("interval-minutes 必须在 5 到 1440 分钟之间")
    if args.max_runs < 0:
        raise ValueError("max-runs 不能小于 0")

    completed = 0
    exit_code = 0
    while True:
        current_code = await _scan(
            settings,
            storage,
            args.platforms,
            args.mode,
            args.limit,
            args.detail_limit,
            args.comments_limit,
            args.headless,
            args.retries,
            args.retry_delay_seconds,
            args.brand_delay_seconds,
            trigger="watch",
        )
        exit_code = max(exit_code, current_code)
        completed += 1
        if args.max_runs and completed >= args.max_runs:
            return exit_code

        now = datetime.now(UTC)
        next_run = next_scheduled_datetime(
            now,
            frequency="interval",
            schedule_time="00:00",
            interval_minutes=args.interval_minutes,
        )
        delay_seconds = max(0, (next_run - now).total_seconds())
        print(
            json.dumps(
                {
                    "status": "waiting",
                    "completed_runs": completed,
                    "next_run_at": next_run.isoformat(),
                    "interval_minutes": args.interval_minutes,
                },
                ensure_ascii=False,
            )
        )
        await asyncio.sleep(delay_seconds)


async def _wecom_test(storage: Storage) -> int:
    config = storage.get_wecom_config()
    secret = CredentialStore.get_wecom_secret()
    client = WeComClient(
        bot_id=str(config.get("bot_id") or ""),
        secret=secret,
        ws_url=str(config.get("ws_url") or ""),
    )
    await client.send_markdown(
        str(config.get("chat_id") or ""),
        "## 品牌舆情监控\n\n企微智能机器人连接测试成功。",
    )
    print(json.dumps({"status": "ok", "message": "测试消息已发送"}, ensure_ascii=False))
    return 0


async def _wecom_discover(storage: Storage, timeout_seconds: int) -> int:
    if timeout_seconds <= 0 or timeout_seconds > 600:
        raise ValueError("timeout-seconds 必须在 1 到 600 之间")
    config = storage.get_wecom_config()
    secret = CredentialStore.get_wecom_secret()
    client = WeComClient(
        bot_id=str(config.get("bot_id") or ""),
        secret=secret,
        ws_url=str(config.get("ws_url") or ""),
    )
    print("正在监听企微群聊消息，请在目标群聊中 @机器人 发送任意消息…", flush=True)
    chat_id = await client.discover_group_chat_id(timeout=timeout_seconds)
    storage.save_wecom_config(
        enabled=bool(config.get("enabled")),
        bot_id=str(config.get("bot_id") or ""),
        chat_id=chat_id,
        ws_url=str(config.get("ws_url") or ""),
    )
    print(json.dumps({"status": "ok", "chat_id": chat_id}, ensure_ascii=False))
    return 0


async def _llm_test(storage: Storage) -> int:
    config = storage.get_llm_config()
    client = LLMClient(
        LLMSettings(
            provider=str(config.get("provider") or "openai-compatible"),
            base_url=str(config.get("base_url") or ""),
            model=str(config.get("model") or ""),
            api_key=CredentialStore.get_llm_api_key(),
            max_candidates=int(config.get("max_candidates") or 20),
        )
    )
    capabilities = await client.probe_capabilities()
    storage.save_llm_capabilities(capabilities.as_dict())
    print(
        json.dumps(
            {
                "status": "ok" if capabilities.chat_completions else "failed",
                "message": "大模型接口连接成功"
                if capabilities.chat_completions
                else "大模型接口连接失败",
                "capabilities": capabilities.as_dict(),
            },
            ensure_ascii=False,
        )
    )
    return 0 if capabilities.chat_completions else 2


def _brand_command(storage: Storage, args: argparse.Namespace) -> int:
    command = args.brand_command
    if command == "list":
        print(json.dumps(storage.list_brands(), ensure_ascii=False, indent=2))
        return 0
    if command == "add":
        storage.add_brand(args.name)
        return 0
    if command == "rename":
        changed = storage.rename_brand(args.old_name, args.new_name)
    elif command == "enable":
        changed = storage.set_brand_enabled(args.name, True)
    elif command == "disable":
        changed = storage.set_brand_enabled(args.name, False)
    elif command == "delete":
        changed = storage.delete_brand(args.name)
    else:
        raise ValueError(f"未知品牌命令：{command}")
    if not changed:
        print("未找到目标品牌。", file=sys.stderr)
        return 2
    return 0


def _keyword_command(storage: Storage, args: argparse.Namespace) -> int:
    command = args.keyword_command
    if command == "list":
        payload: object = storage.list_keywords(brand_name=args.brand)
    elif command == "add":
        keyword_id = storage.add_keyword(args.brand_name, args.keyword)
        payload = {"id": keyword_id}
    elif command == "rename":
        changed = storage.rename_keyword(args.keyword_id, args.new_keyword)
        payload = {"updated": changed}
    elif command in {"enable", "disable"}:
        changed = storage.set_keyword_enabled(args.keyword_id, command == "enable")
        payload = {"updated": changed}
    elif command == "delete":
        changed = storage.delete_keyword(args.keyword_id)
        payload = {"deleted": changed}
    else:
        raise ValueError(f"未知关键词命令：{command}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _account_command(storage: Storage, args: argparse.Namespace) -> int:
    command = args.account_command
    if command == "list":
        payload: object = storage.list_accounts()
    elif command == "add":
        account_id = storage.add_account(args.platform.value, args.display_name)
        payload = {"id": account_id}
    elif command == "status":
        changed = storage.update_account_status(args.account_id, args.status)
        payload = {"updated": changed}
    elif command in {"enable", "disable"}:
        changed = storage.set_account_enabled(args.account_id, command == "enable")
        payload = {"updated": changed}
    elif command == "delete":
        changed = storage.delete_account(args.account_id)
        payload = {"deleted": changed}
    else:
        raise ValueError(f"未知账号命令：{command}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _run_history_command(storage: Storage, args: argparse.Namespace) -> int:
    if args.run_command == "list":
        if not 1 <= args.limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        payload: object = storage.list_scan_runs(limit=args.limit)
    elif args.run_command == "show":
        payload = storage.get_scan_run(args.run_id)
        if payload is None:
            print("未找到扫描运行记录。", file=sys.stderr)
            return 2
    else:
        raise ValueError(f"未知运行历史命令：{args.run_command}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _alert_command(storage: Storage, args: argparse.Namespace) -> int:
    if args.alert_command == "list":
        if not 1 <= args.limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        payload: object = storage.list_alerts(
            unacknowledged_only=not args.all,
            limit=args.limit,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.alert_command == "ack":
        changed = storage.acknowledge_alert(args.alert_id)
        if not changed:
            print("未找到未确认的告警。", file=sys.stderr)
            return 2
        return 0
    raise ValueError(f"未知告警命令：{args.alert_command}")


def _notification_command(storage: Storage, args: argparse.Namespace) -> int:
    if args.notification_command == "list":
        if not 1 <= args.limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        payload: object = storage.list_notifications(
            unread_only=not args.all,
            limit=args.limit,
        )
    elif args.notification_command == "read":
        payload = {"updated": storage.mark_notification_read(args.notification_id)}
    else:
        raise ValueError(f"未知通知命令：{args.notification_command}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _data_command(settings: Settings, storage: Storage, args: argparse.Namespace) -> int:
    if args.data_command == "status":
        payload: object = {"counts": storage.operational_counts()}
    elif args.data_command == "reset":
        if args.confirm != "RESET":
            raise ValueError("清理运行数据必须明确传入 --confirm RESET")
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        backup_path = settings.runtime_dir / "backups" / f"before-reset-{stamp}.db"
        storage.backup_to(backup_path)
        removed = storage.reset_operational_data()
        payload = {
            "backup": str(backup_path),
            "removed": removed,
            "remaining": storage.operational_counts(),
            "preserved": {
                "brands": len(storage.list_brands()),
                "keywords": len(storage.list_keywords()),
                "accounts": len(storage.list_accounts()),
                "browser_profiles": True,
            },
        }
    else:
        raise ValueError(f"未知数据命令：{args.data_command}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _classify_command(storage: Storage, args: argparse.Namespace) -> int:
    if args.classify_command in {"run", "list"} and not 1 <= args.limit <= 1000:
        raise ValueError("limit 必须在 1 到 1000 之间")
    if args.classify_command == "run":
        payload: object = classify_batch(storage, limit=args.limit, force=args.force)
    elif args.classify_command == "list":
        payload = storage.list_assessments(
            limit=args.limit,
            severity=args.severity,
            requires_review=True if args.needs_review else None,
        )
    elif args.classify_command == "show":
        payload = storage.get_assessment(args.content_item_id)
        if payload is None:
            print("未找到舆情判定记录。", file=sys.stderr)
            return 2
    elif args.classify_command == "review":
        changed = storage.review_assessment(
            args.content_item_id,
            category=args.category,
            severity=args.severity,
            note=args.note.strip(),
            reviewer=args.reviewer.strip() or "operator",
        )
        if not changed:
            print("未找到舆情判定记录，请先运行 classify run。", file=sys.stderr)
            return 2
        payload = storage.get_assessment(args.content_item_id)
    else:
        raise ValueError(f"未知分类命令：{args.classify_command}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


async def run(args: argparse.Namespace) -> int:
    settings = Settings.from_environment()
    settings.ensure_directories()
    storage = Storage(settings.database_path)
    storage.initialize()

    if args.command == "init":
        if not storage.list_brands():
            for brand in DEFAULT_BRANDS:
                storage.add_brand(brand)
        print(
            json.dumps(
                {
                    "runtime_dir": str(settings.runtime_dir),
                    "database": str(settings.database_path),
                    "brands": [item["name"] for item in storage.list_brands()],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "brand":
        return _brand_command(storage, args)
    if args.command == "keyword":
        return _keyword_command(storage, args)
    if args.command == "account":
        return _account_command(storage, args)
    if args.command == "run":
        return _run_history_command(storage, args)
    if args.command == "alert":
        return _alert_command(storage, args)
    if args.command == "notification":
        return _notification_command(storage, args)
    if args.command == "data":
        return _data_command(settings, storage, args)
    if args.command == "classify":
        return _classify_command(storage, args)
    if args.command == "wecom":
        if args.wecom_command == "test":
            return await _wecom_test(storage)
        if args.wecom_command == "discover":
            return await _wecom_discover(storage, args.timeout_seconds)
        raise ValueError(f"未知企微命令：{args.wecom_command}")
    if args.command == "llm":
        if args.llm_command == "test":
            return await _llm_test(storage)
        raise ValueError(f"未知大模型命令：{args.llm_command}")
    if args.command == "doctor":
        return await _doctor(settings)
    if args.command == "login":
        return await _login(settings, storage, args.platform, args.account_id)
    if args.command == "check":
        return await _check(settings, storage, args.platform, args.account_id, args.headless)
    if args.command == "search":
        return await _search_one(
            settings,
            storage,
            args.platform,
            args.keyword,
            args.limit,
            args.detail_limit,
            args.comments_limit,
            args.headless,
        )
    if args.command == "scan":
        return await _scan(
            settings,
            storage,
            args.platforms,
            args.mode,
            args.limit,
            args.detail_limit,
            args.comments_limit,
            args.headless,
            args.retries,
            args.retry_delay_seconds,
            args.brand_delay_seconds,
            trigger=args.trigger,
        )
    if args.command == "watch":
        return await _watch(settings, storage, args)
    raise ValueError(f"未知命令：{args.command}")


def main() -> None:
    _configure_utf8_output()
    parser = build_parser()
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(run(args))
    except BrowserProfileLocked as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 2
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        exit_code = 130
    except Exception as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
