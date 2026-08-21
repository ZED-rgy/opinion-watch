from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from opinion_watch.storage import Storage

LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def report_date_for_now() -> str:
    return datetime.now(LOCAL_ZONE).date().isoformat()


def build_daily_report(storage: Storage, report_date: str | None = None) -> str:
    target_date = date.fromisoformat(report_date or report_date_for_now())
    start_local = datetime.combine(target_date, time.min, tzinfo=LOCAL_ZONE)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(UTC).isoformat()
    end_utc = end_local.astimezone(UTC).isoformat()

    with storage.connect() as connection:
        run_rows = connection.execute(
            """
            SELECT status, scanned_count, filtered_count, collected_count,
                   inserted_count, updated_count, suspected_count,
                   detailed_count, media_count
            FROM scan_runs
            WHERE started_at >= ? AND started_at < ?
            ORDER BY started_at
            """,
            (start_utc, end_utc),
        ).fetchall()
        content_stats = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN discovered_at >= ? AND discovered_at < ? THEN 1 ELSE 0 END)
                    AS inserted
            FROM content_items
            WHERE last_seen_at >= ? AND last_seen_at < ?
            """,
            (start_utc, end_utc, start_utc, end_utc),
        ).fetchone()
        severity_rows = connection.execute(
            """
            SELECT oa.severity, COUNT(*) AS count
            FROM opinion_assessments oa
            JOIN content_items ci ON ci.id = oa.content_item_id
            WHERE ci.last_seen_at >= ? AND ci.last_seen_at < ?
            GROUP BY oa.severity
            ORDER BY CASE oa.severity
                WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END
            """,
            (start_utc, end_utc),
        ).fetchall()
        pending_reviews = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM opinion_assessments oa
            JOIN content_items ci ON ci.id = oa.content_item_id
            WHERE ci.last_seen_at >= ? AND ci.last_seen_at < ?
              AND oa.review_status = 'pending'
            """,
            (start_utc, end_utc),
        ).fetchone()["count"]
        cluster_count = connection.execute(
            """
            SELECT COUNT(DISTINCT ecm.cluster_id) AS count
            FROM event_cluster_members ecm
            JOIN content_items ci ON ci.id = ecm.content_item_id
            WHERE ci.last_seen_at >= ? AND ci.last_seen_at < ?
            """,
            (start_utc, end_utc),
        ).fetchone()["count"]
        alert_count = connection.execute(
            "SELECT COUNT(*) AS count FROM alerts WHERE created_at >= ? AND created_at < ?",
            (start_utc, end_utc),
        ).fetchone()["count"]
        highlights = connection.execute(
            """
            SELECT
                oa.severity,
                oa.category,
                ci.title,
                ci.url,
                GROUP_CONCAT(DISTINCT b.name) AS brands
            FROM opinion_assessments oa
            JOIN content_items ci ON ci.id = oa.content_item_id
            LEFT JOIN content_matches cm ON cm.content_item_id = ci.id
            LEFT JOIN brands b ON b.id = cm.brand_id
            WHERE ci.last_seen_at >= ? AND ci.last_seen_at < ?
              AND oa.severity IN ('P0', 'P1', 'P2')
            GROUP BY oa.content_item_id, oa.severity, oa.category, ci.title, ci.url
            ORDER BY CASE oa.severity
                WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
                ci.last_seen_at DESC
            LIMIT 5
            """,
            (start_utc, end_utc),
        ).fetchall()

    status_counts: dict[str, int] = {}
    scanned = filtered = collected = inserted = updated = 0
    suspected = detailed = media_items = 0
    for row in run_rows:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        scanned += int(row["scanned_count"] or 0)
        filtered += int(row["filtered_count"] or 0)
        collected += int(row["collected_count"] or 0)
        inserted += int(row["inserted_count"] or 0)
        updated += int(row["updated_count"] or 0)
        suspected += int(row["suspected_count"] or 0)
        detailed += int(row["detailed_count"] or 0)
        media_items += int(row["media_count"] or 0)

    severity_counts = {str(row["severity"]): int(row["count"]) for row in severity_rows}
    lines = [
        f"# 品牌舆情巡检日报 · {target_date.isoformat()}",
        "",
        f"巡检轮次：{len(run_rows)} 次（成功 {status_counts.get('succeeded', 0)}，"
        f"部分完成 {status_counts.get('partial', 0)}，失败 {status_counts.get('failed', 0)}）",
        f"检索结果：{scanned} 条，入库 {collected} 条，过滤普通内容 {filtered} 条 "
        f"（新增 {inserted}，更新 {updated}）",
        f"疑似舆情：{suspected} 条，详情调查：{detailed} 条，媒体证据：{media_items} 条",
        f"聚合事件：{int(cluster_count or 0)} 个（仅统计疑似/高风险内容）",
        f"当日去重内容：{int(content_stats['total'] or 0)} 条，"
        f"待复核：{int(pending_reviews)} 条，运行告警：{int(alert_count)} 条",
        "风险分布："
        + "、".join(
            f"{severity} {severity_counts.get(severity, 0)} 条"
            for severity in ("P0", "P1", "P2", "P3")
        ),
        "",
        "## 重点内容",
    ]
    if not highlights:
        lines.append("今日暂无 P0-P2 重点内容。")
    else:
        for row in highlights:
            title = " ".join(str(row["title"] or "无标题").split())[:120]
            title = title.replace("[", "（").replace("]", "）")
            brands = str(row["brands"] or "未归属品牌")
            brands = brands.replace("[", "（").replace("]", "）").replace("*", "")
            # 抓取到的 URL 属于不可信输入：括号会提前闭合 Markdown 链接，
            # 让页面内容注入到日报正文里。
            url = str(row["url"] or "").replace("(", "%28").replace(")", "%29")
            lines.append(f"- **{row['severity']}** · {brands} · {title} [打开原帖]({url})")
    lines.extend(("", "本日报由品牌舆情监控系统自动生成；风险判断均为待人工核验的线索。"))
    return "\n".join(lines)[:19_000]
