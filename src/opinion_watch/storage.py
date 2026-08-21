from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from opinion_watch.models import CollectedContent, Platform, UpsertStats


class _ManagedConnection(sqlite3.Connection):
    """Close SQLite connections after the standard commit/rollback context."""

    def __exit__(self, *args: object) -> None:
        try:
            super().__exit__(*args)
        finally:
            self.close()


class Storage:
    _OPERATIONAL_TABLES = (
        "content_items",
        "content_matches",
        "opinion_assessments",
        "scan_runs",
        "scan_attempts",
        "scan_run_contents",
        "scan_candidates",
        "alerts",
        "app_notifications",
        "daily_reports",
    )

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, factory=_ManagedConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def backup_to(self, backup_path: Path) -> None:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(backup_path) as destination:
            source.backup(destination)

    def operational_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in self._OPERATIONAL_TABLES
            }

    def reset_operational_data(self) -> dict[str, int]:
        before = self.operational_counts()
        delete_order = (
            "daily_reports",
            "app_notifications",
            "opinion_assessments",
            "alerts",
            "scan_run_contents",
            "scan_candidates",
            "scan_attempts",
            "scan_runs",
            "content_matches",
            "content_items",
        )
        with self.connect() as connection:
            for table in delete_order:
                connection.execute(f"DELETE FROM {table}")
            placeholders = ", ".join("?" for _ in delete_order)
            connection.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
                delete_order,
            )
        return before

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS brands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS brand_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    keyword TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(brand_id, keyword)
                );

                CREATE INDEX IF NOT EXISTS idx_brand_keywords_enabled
                    ON brand_keywords(brand_id, enabled, id);

                CREATE TABLE IF NOT EXISTS platform_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'not_logged_in',
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, display_name)
                );

                CREATE TABLE IF NOT EXISTS content_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    platform_content_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    published_at TEXT,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    fingerprint TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(platform, platform_content_id)
                );

                CREATE TABLE IF NOT EXISTS content_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_item_id INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    source_keyword TEXT NOT NULL,
                    first_matched_at TEXT NOT NULL,
                    last_matched_at TEXT NOT NULL,
                    UNIQUE(content_item_id, brand_id, source_keyword)
                );

                CREATE INDEX IF NOT EXISTS idx_content_last_seen
                    ON content_items(last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_match_brand
                    ON content_matches(brand_id, last_matched_at DESC);

                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    platforms_json TEXT NOT NULL,
                    brands_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    collected_count INTEGER NOT NULL DEFAULT 0,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    filtered_count INTEGER NOT NULL DEFAULT 0,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    succeeded_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    suspected_count INTEGER NOT NULL DEFAULT 0,
                    detailed_count INTEGER NOT NULL DEFAULT 0,
                    media_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    classification_json TEXT NOT NULL DEFAULT '{}',
                    model_summary_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS scan_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    collected_count INTEGER NOT NULL DEFAULT 0,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    filtered_count INTEGER NOT NULL DEFAULT 0,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    suspected_count INTEGER NOT NULL DEFAULT 0,
                    detailed_count INTEGER NOT NULL DEFAULT 0,
                    media_count INTEGER NOT NULL DEFAULT 0,
                    error_status TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    screenshot_path TEXT,
                    UNIQUE(run_id, platform, keyword, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS scan_run_contents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    attempt_id INTEGER NOT NULL REFERENCES scan_attempts(id) ON DELETE CASCADE,
                    content_item_id INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    source_keyword TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    UNIQUE(attempt_id, content_item_id, source_keyword)
                );

                CREATE INDEX IF NOT EXISTS idx_scan_run_contents_run
                    ON scan_run_contents(run_id, observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_scan_run_contents_content
                    ON scan_run_contents(content_item_id, observed_at DESC);

                CREATE TABLE IF NOT EXISTS scan_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    attempt_id INTEGER NOT NULL REFERENCES scan_attempts(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    platform_content_id TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    filter_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(attempt_id, platform_content_id)
                );

                CREATE INDEX IF NOT EXISTS idx_scan_candidates_run
                    ON scan_candidates(run_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
                    attempt_id INTEGER REFERENCES scan_attempts(id) ON DELETE SET NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    keyword TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    screenshot_path TEXT,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_scan_runs_started
                    ON scan_runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_scan_attempts_run
                    ON scan_attempts(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_alerts_unacknowledged
                    ON alerts(acknowledged_at, created_at DESC);

                CREATE TABLE IF NOT EXISTS opinion_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_item_id INTEGER NOT NULL UNIQUE
                        REFERENCES content_items(id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    rationale TEXT NOT NULL,
                    matched_signals_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    requires_review INTEGER NOT NULL DEFAULT 1,
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    review_note TEXT NOT NULL DEFAULT '',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_assessments_queue
                    ON opinion_assessments(review_status, severity, updated_at DESC);

                CREATE TABLE IF NOT EXISTS app_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    UNIQUE(kind, entity_type, entity_id)
                );

                CREATE INDEX IF NOT EXISTS idx_app_notifications_unread
                    ON app_notifications(read_at, created_at DESC);

                CREATE TABLE IF NOT EXISTS wecom_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    bot_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    ws_url TEXT NOT NULL DEFAULT 'wss://openws.work.weixin.qq.com',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT 'openai-compatible',
                    base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
                    model TEXT NOT NULL DEFAULT '',
                    max_candidates INTEGER NOT NULL DEFAULT 20,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    probed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL UNIQUE,
                    scan_run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_daily_reports_date
                    ON daily_reports(report_date DESC);

                CREATE TABLE IF NOT EXISTS task_leases (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedule_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    frequency TEXT NOT NULL DEFAULT 'daily',
                    schedule_time TEXT NOT NULL DEFAULT '09:00',
                    weekday INTEGER NOT NULL DEFAULT 0,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    scan_mode TEXT NOT NULL DEFAULT 'quick',
                    last_scheduled_at TEXT,
                    next_run_at TEXT,
                    missed_run_policy TEXT NOT NULL DEFAULT 'run_once',
                    legacy_imported INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            for column, definition in (
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("note", "TEXT NOT NULL DEFAULT ''"),
                ("classification_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("model_summary_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("suspected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("detailed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("media_count", "INTEGER NOT NULL DEFAULT 0"),
                ("scanned_count", "INTEGER NOT NULL DEFAULT 0"),
                ("filtered_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                try:
                    connection.execute(f"ALTER TABLE scan_runs ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            for column, definition in (
                ("capabilities_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("probed_at", "TEXT"),
            ):
                try:
                    connection.execute(f"ALTER TABLE llm_config ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            for column, definition in (
                ("suspected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("detailed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("media_count", "INTEGER NOT NULL DEFAULT 0"),
                ("scanned_count", "INTEGER NOT NULL DEFAULT 0"),
                ("filtered_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                try:
                    connection.execute(
                        f"ALTER TABLE scan_attempts ADD COLUMN {column} {definition}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO wecom_config(id, created_at, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO llm_config(id, updated_at)
                VALUES (1, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO schedule_config(id, updated_at)
                VALUES (1, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now,),
            )
            self._run_migrations(connection)

    @staticmethod
    def _run_migrations(connection: sqlite3.Connection) -> None:
        """Apply data migrations once; normal startup must be data-preserving."""
        current = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if int(current) >= 2:
            return
        if int(current) == 1:
            now = datetime.now(UTC).isoformat()
            Storage._migrate_content_match_keys(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (now,),
            )
            return
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            UPDATE scan_runs
            SET title = CASE WHEN trigger = 'watch' THEN '定时巡检' ELSE '手动巡检' END
                || ' ' || substr(started_at, 1, 16)
            WHERE title = ''
            """
        )
        connection.execute(
            """
            UPDATE opinion_assessments
            SET category = 'irrelevant', requires_review = 0,
                review_status = 'not_required', updated_at = ?
            WHERE source = 'rules' AND category = 'other'
              AND matched_signals_json = '[]'
            """,
            (now,),
        )
        connection.execute(
            """
            UPDATE opinion_assessments
            SET category = 'irrelevant', requires_review = 0,
                review_status = 'not_required', updated_at = ?
            WHERE source = 'rules' AND category = 'ordinary_grievance'
              AND requires_review = 0
            """,
            (now,),
        )
        connection.execute(
            """
            DELETE FROM app_notifications
            WHERE kind = 'opinion_review' AND entity_type = 'content_item'
              AND entity_id IN (
                SELECT CAST(content_item_id AS TEXT)
                FROM opinion_assessments
                WHERE source = 'rules' AND category = 'irrelevant'
                  AND requires_review = 0
              )
            """
        )
        connection.execute(
            """
            INSERT INTO app_notifications(
                kind, severity, title, message, entity_type, entity_id, created_at
            )
            SELECT 'opinion_review', severity, severity || ' 舆情待复核', rationale,
                   'content_item', CAST(content_item_id AS TEXT), updated_at
            FROM opinion_assessments
            WHERE requires_review = 1 AND review_status = 'pending'
            ON CONFLICT(kind, entity_type, entity_id) DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO scan_run_contents(
                run_id, attempt_id, content_item_id, platform,
                source_keyword, observed_at
            )
            SELECT a.run_id, a.id, ci.id, ci.platform,
                   cm.source_keyword, ci.last_seen_at
            FROM scan_attempts a
            JOIN content_items ci
              ON ci.platform = a.platform
             AND ci.last_seen_at >= a.started_at
             AND ci.last_seen_at <= COALESCE(a.finished_at, ci.last_seen_at)
            JOIN content_matches cm
              ON cm.content_item_id = ci.id
             AND cm.source_keyword = a.keyword
            WHERE a.status = 'succeeded'
            """
        )
        connection.execute(
            """
            INSERT INTO app_notifications(
                kind, severity, title, message, entity_type, entity_id, created_at
            )
            SELECT 'runtime_alert', severity, '巡检运行异常', message,
                   'alert', CAST(id AS TEXT), created_at
            FROM alerts
            WHERE acknowledged_at IS NULL
            ON CONFLICT(kind, entity_type, entity_id) DO NOTHING
            """
        )
        # The old implementation wrote these summary values into shifted
        # columns. Rebuild them from the attempt ledger; numeric error text is
        # explicitly marked as unrecoverable instead of being guessed.
        connection.execute(
            """
            UPDATE scan_runs
            SET collected_count = (
                    SELECT COALESCE(SUM(collected_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                scanned_count = (
                    SELECT COALESCE(SUM(scanned_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                filtered_count = (
                    SELECT COALESCE(SUM(filtered_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                inserted_count = (
                    SELECT COALESCE(SUM(inserted_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                updated_count = (
                    SELECT COALESCE(SUM(updated_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                succeeded_count = (
                    SELECT COUNT(*) FROM scan_attempts
                    WHERE run_id = scan_runs.id AND status = 'succeeded'
                ),
                failed_count = (
                    SELECT COUNT(*) FROM scan_attempts
                    WHERE run_id = scan_runs.id AND status = 'failed'
                ),
                suspected_count = (
                    SELECT COALESCE(SUM(suspected_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                detailed_count = (
                    SELECT COALESCE(SUM(detailed_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                media_count = (
                    SELECT COALESCE(SUM(media_count), 0)
                    FROM scan_attempts WHERE run_id = scan_runs.id
                ),
                error_message = CASE
                    WHEN error_message GLOB '[0-9]*' THEN '历史记录字段错位，原始错误信息不可恢复'
                    ELSE error_message
                END
            WHERE EXISTS (SELECT 1 FROM scan_attempts WHERE run_id = scan_runs.id)
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)", (now,)
        )
        Storage._migrate_content_match_keys(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            (now,),
        )

    @staticmethod
    def _migrate_content_match_keys(connection: sqlite3.Connection) -> None:
        connection.execute("DROP INDEX IF EXISTS idx_match_brand")
        connection.execute(
            """
            CREATE TABLE content_matches_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_item_id INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
                brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                source_keyword TEXT NOT NULL,
                first_matched_at TEXT NOT NULL,
                last_matched_at TEXT NOT NULL,
                UNIQUE(content_item_id, brand_id, source_keyword)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO content_matches_v2(
                id, content_item_id, brand_id, source_keyword,
                first_matched_at, last_matched_at
            )
            SELECT id, content_item_id, brand_id, source_keyword,
                   first_matched_at, last_matched_at
            FROM content_matches
            """
        )
        connection.execute("DROP TABLE content_matches")
        connection.execute("ALTER TABLE content_matches_v2 RENAME TO content_matches")
        connection.execute(
            """
            CREATE INDEX idx_match_brand
                ON content_matches(brand_id, last_matched_at DESC)
            """
        )

    def list_contents_for_assessment(
        self,
        *,
        limit: int = 100,
        include_assessed: bool = False,
        run_id: int | None = None,
    ) -> list[dict[str, object]]:
        assessed_filter = "" if include_assessed else "AND oa.id IS NULL"
        run_filter = ""
        parameters: list[object] = []
        if run_id is not None:
            run_filter = (
                "AND EXISTS (SELECT 1 FROM scan_run_contents src "
                "WHERE src.content_item_id = ci.id AND src.run_id = ?)"
            )
            parameters.append(run_id)
        query = f"""
            SELECT
                ci.id, ci.platform, ci.platform_content_id, ci.url, ci.title,
                ci.author_name, ci.published_at, ci.metrics_json, ci.raw_json,
                GROUP_CONCAT(b.name, char(31)) AS brand_names
            FROM content_items ci
            JOIN content_matches cm ON cm.content_item_id = ci.id
            JOIN brands b ON b.id = cm.brand_id
            LEFT JOIN opinion_assessments oa ON oa.content_item_id = ci.id
            WHERE 1 = 1 {assessed_filter} {run_filter}
            GROUP BY ci.id
            ORDER BY ci.last_seen_at DESC, ci.id DESC
            LIMIT ?
        """
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._content_for_assessment_dict(row) for row in rows]

    def list_model_candidates(
        self, *, limit: int = 20, run_id: int | None = None
    ) -> list[dict[str, object]]:
        run_filter = ""
        parameters: list[object] = []
        if run_id is not None:
            run_filter = (
                "AND EXISTS (SELECT 1 FROM scan_run_contents src "
                "WHERE src.content_item_id = ci.id AND src.run_id = ?)"
            )
            parameters.append(run_id)
        query = f"""
            SELECT
                ci.id, ci.platform, ci.platform_content_id, ci.url, ci.title,
                ci.author_name, ci.published_at, ci.metrics_json, ci.raw_json,
                GROUP_CONCAT(b.name, char(31)) AS brand_names
            FROM content_items ci
            JOIN content_matches cm ON cm.content_item_id = ci.id
            JOIN brands b ON b.id = cm.brand_id
            JOIN opinion_assessments oa ON oa.content_item_id = ci.id
            WHERE oa.source = 'rules'
              AND (
                oa.requires_review = 1
                OR oa.severity IN ('P1', 'P2')
                OR json_extract(ci.raw_json, '$.screening.admitted') = 1
              )
              {run_filter}
            GROUP BY ci.id
            ORDER BY
                CASE oa.severity
                    WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                    WHEN 'P2' THEN 2 ELSE 3
                END,
                ci.last_seen_at DESC, ci.id DESC
            LIMIT ?
        """
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._content_for_assessment_dict(row) for row in rows]

    def upsert_assessment(
        self,
        *,
        content_item_id: int,
        category: str,
        severity: str,
        confidence: float,
        rationale: str,
        matched_signals: list[str],
        requires_review: bool,
        source: str = "rules",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        review_status = "pending" if requires_review else "not_required"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO opinion_assessments(
                    content_item_id, category, severity, confidence, rationale,
                    matched_signals_json, source, requires_review, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_item_id) DO UPDATE SET
                    category = excluded.category,
                    severity = excluded.severity,
                    confidence = excluded.confidence,
                    rationale = excluded.rationale,
                    matched_signals_json = excluded.matched_signals_json,
                    source = excluded.source,
                    requires_review = excluded.requires_review,
                    review_status = excluded.review_status,
                    updated_at = excluded.updated_at
                WHERE opinion_assessments.source <> 'manual'
                """,
                (
                    content_item_id,
                    category,
                    severity,
                    confidence,
                    rationale,
                    json.dumps(matched_signals, ensure_ascii=False),
                    source,
                    int(requires_review),
                    review_status,
                    now,
                    now,
                ),
            )
            assessment = connection.execute(
                """
                SELECT content_item_id, severity, rationale, requires_review, review_status
                FROM opinion_assessments WHERE content_item_id = ?
                """,
                (content_item_id,),
            ).fetchone()
            if (
                assessment is not None
                and bool(assessment["requires_review"])
                and assessment["review_status"] == "pending"
            ):
                self._upsert_notification(
                    connection,
                    kind="opinion_review",
                    severity=str(assessment["severity"]),
                    title=f"{assessment['severity']} 舆情待复核",
                    message=str(assessment["rationale"]),
                    entity_type="content_item",
                    entity_id=str(assessment["content_item_id"]),
                    now=now,
                )

    def list_assessments(
        self,
        *,
        limit: int = 100,
        severity: str | None = None,
        requires_review: bool | None = None,
        run_id: int | None = None,
        source: str | None = None,
        platform: str | None = None,
    ) -> list[dict[str, object]]:
        conditions: list[str] = ["NOT (oa.source = 'rules' AND oa.category = 'irrelevant')"]
        parameters: list[object] = []
        if severity is not None:
            conditions.append("oa.severity = ?")
            parameters.append(severity)
        if requires_review is not None:
            conditions.append("oa.requires_review = ?")
            parameters.append(int(requires_review))
        if run_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM scan_run_contents src "
                "WHERE src.content_item_id = ci.id AND src.run_id = ?)"
            )
            parameters.append(run_id)
        if source is not None:
            conditions.append("oa.source = ?")
            parameters.append(source)
        if platform is not None:
            conditions.append("ci.platform = ?")
            parameters.append(platform)
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""
            SELECT
                oa.*, ci.platform, ci.platform_content_id, ci.url, ci.title,
                ci.author_name, ci.discovered_at, ci.last_seen_at,
                (
                    SELECT MAX(src.observed_at)
                    FROM scan_run_contents src
                    WHERE src.content_item_id = ci.id
                ) AS latest_observed_at,
                (
                    SELECT src.run_id
                    FROM scan_run_contents src
                    WHERE src.content_item_id = ci.id
                    ORDER BY src.observed_at DESC, src.id DESC
                    LIMIT 1
                ) AS latest_run_id,
                (
                    SELECT GROUP_CONCAT(DISTINCT src.source_keyword)
                    FROM scan_run_contents src
                    WHERE src.content_item_id = ci.id
                ) AS observed_keywords,
                (
                    SELECT GROUP_CONCAT(b.name, char(31))
                    FROM content_matches cm
                    JOIN brands b ON b.id = cm.brand_id
                    WHERE cm.content_item_id = ci.id
                ) AS brand_names
            FROM opinion_assessments oa
            JOIN content_items ci ON ci.id = oa.content_item_id
            {where_clause}
            ORDER BY
                CASE oa.severity
                    WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                    WHEN 'P2' THEN 2 ELSE 3
                END,
                oa.updated_at DESC
            LIMIT ?
        """
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._assessment_dict(row) for row in rows]

    def get_assessment(self, content_item_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    oa.*, ci.platform, ci.platform_content_id, ci.url, ci.title,
                    ci.author_name, ci.discovered_at, ci.last_seen_at,
                    (
                        SELECT MAX(src.observed_at)
                        FROM scan_run_contents src
                        WHERE src.content_item_id = ci.id
                    ) AS latest_observed_at,
                    (
                        SELECT src.run_id
                        FROM scan_run_contents src
                        WHERE src.content_item_id = ci.id
                        ORDER BY src.observed_at DESC, src.id DESC
                        LIMIT 1
                    ) AS latest_run_id,
                    (
                        SELECT GROUP_CONCAT(DISTINCT src.source_keyword)
                        FROM scan_run_contents src
                        WHERE src.content_item_id = ci.id
                    ) AS observed_keywords,
                    (
                        SELECT GROUP_CONCAT(b.name, char(31))
                        FROM content_matches cm
                        JOIN brands b ON b.id = cm.brand_id
                        WHERE cm.content_item_id = ci.id
                    ) AS brand_names
                FROM opinion_assessments oa
                JOIN content_items ci ON ci.id = oa.content_item_id
                WHERE oa.content_item_id = ?
                """,
                (content_item_id,),
            ).fetchone()
        return self._assessment_dict(row) if row is not None else None

    def link_scan_contents(
        self,
        *,
        run_id: int,
        attempt_id: int,
        items: Iterable[CollectedContent],
    ) -> None:
        observed_at = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            for item in items:
                row = connection.execute(
                    """
                    SELECT id FROM content_items
                    WHERE platform = ? AND platform_content_id = ?
                    """,
                    (item.platform.value, item.content_id),
                ).fetchone()
                if row is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO scan_run_contents(
                        run_id, attempt_id, content_item_id, platform,
                        source_keyword, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(attempt_id, content_item_id, source_keyword) DO UPDATE SET
                        observed_at = excluded.observed_at
                    """,
                    (
                        run_id,
                        attempt_id,
                        row["id"],
                        item.platform.value,
                        item.source_keyword,
                        observed_at,
                    ),
                )

    def review_assessment(
        self,
        content_item_id: int,
        *,
        category: str,
        severity: str,
        note: str,
        reviewer: str,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE opinion_assessments SET
                    category = ?, severity = ?, confidence = 1.0,
                    rationale = CASE WHEN ? <> '' THEN ? ELSE rationale END,
                    source = 'manual', requires_review = 0, review_status = 'reviewed',
                    review_note = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
                WHERE content_item_id = ?
                """,
                (
                    category,
                    severity,
                    note,
                    note,
                    note,
                    reviewer,
                    now,
                    now,
                    content_item_id,
                ),
            )
            if cursor.rowcount > 0:
                connection.execute(
                    """
                    UPDATE app_notifications SET read_at = COALESCE(read_at, ?)
                    WHERE kind = 'opinion_review'
                      AND entity_type = 'content_item' AND entity_id = ?
                    """,
                    (now, str(content_item_id)),
                )
        return cursor.rowcount > 0

    def create_manual_assessment(
        self,
        *,
        platform: str,
        title: str,
        url: str,
        brand_name: str,
        category: str,
        severity: str,
        rationale: str,
    ) -> int:
        clean_title = title.strip()
        clean_url = url.strip()
        clean_brand = brand_name.strip()
        if not clean_title or not clean_url or not clean_brand:
            raise ValueError("标题、链接和品牌不能为空")
        try:
            platform_enum = Platform(platform)
        except ValueError as exc:
            raise ValueError("平台不合法") from exc
        item = CollectedContent(
            platform=platform_enum,
            content_id=f"manual-{uuid.uuid4().hex}",
            url=clean_url,
            title=clean_title,
            source_keyword=clean_brand,
            brand_name=clean_brand,
        )
        self.upsert_contents([item])
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM content_items
                WHERE platform = ? AND platform_content_id = ?
                """,
                (platform, item.content_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("新增舆情内容失败")
        self.upsert_assessment(
            content_item_id=int(row["id"]),
            category=category,
            severity=severity,
            confidence=1.0,
            rationale=rationale.strip() or "人工新增记录",
            matched_signals=[],
            requires_review=False,
            source="manual",
        )
        return int(row["id"])

    def update_assessment(
        self,
        content_item_id: int,
        *,
        category: str,
        severity: str,
        rationale: str,
        review_status: str,
        reviewer: str = "运营人员",
    ) -> bool:
        if review_status not in {"pending", "reviewed", "not_required"}:
            raise ValueError("复核状态不合法")
        now = datetime.now(UTC).isoformat()
        requires_review = review_status == "pending"
        reviewed_at = now if review_status == "reviewed" else None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE opinion_assessments SET
                    category = ?, severity = ?, confidence = 1.0,
                    rationale = ?, source = 'manual', requires_review = ?,
                    review_status = ?, review_note = ?, reviewed_by = ?,
                    reviewed_at = ?, updated_at = ?
                WHERE content_item_id = ?
                """,
                (
                    category,
                    severity,
                    rationale.strip(),
                    int(requires_review),
                    review_status,
                    rationale.strip(),
                    reviewer.strip() or "运营人员",
                    reviewed_at,
                    now,
                    content_item_id,
                ),
            )
            if requires_review and cursor.rowcount > 0:
                self._upsert_notification(
                    connection,
                    kind="opinion_review",
                    severity=severity,
                    title=f"{severity} 舆情待复核",
                    message=rationale.strip(),
                    entity_type="content_item",
                    entity_id=str(content_item_id),
                    now=now,
                )
            elif cursor.rowcount > 0:
                connection.execute(
                    """
                    UPDATE app_notifications SET read_at = COALESCE(read_at, ?)
                    WHERE kind = 'opinion_review'
                      AND entity_type = 'content_item' AND entity_id = ?
                    """,
                    (now, str(content_item_id)),
                )
        return cursor.rowcount > 0

    def delete_assessments(self, content_item_ids: Iterable[int]) -> int:
        ids = sorted({int(value) for value in content_item_ids})
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as connection:
            connection.execute(
                f"""
                DELETE FROM app_notifications
                WHERE kind = 'opinion_review' AND entity_type = 'content_item'
                  AND entity_id IN ({placeholders})
                """,
                [str(value) for value in ids],
            )
            cursor = connection.execute(
                f"DELETE FROM opinion_assessments WHERE content_item_id IN ({placeholders})",
                ids,
            )
        return cursor.rowcount

    @staticmethod
    def _content_for_assessment_dict(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["brand_names"] = str(result.get("brand_names") or "").split("\x1f")
        result["metrics"] = json.loads(str(result.pop("metrics_json")))
        result["raw_data"] = json.loads(str(result.pop("raw_json")))
        return result

    @staticmethod
    def _assessment_dict(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["brand_names"] = str(result.get("brand_names") or "").split("\x1f")
        result["matched_signals"] = json.loads(str(result.pop("matched_signals_json")))
        result["observed_keywords"] = [
            value for value in str(result.get("observed_keywords") or "").split(",") if value
        ]
        result["requires_review"] = bool(result["requires_review"])
        return result

    def create_scan_run(
        self,
        *,
        trigger: str,
        platforms: list[str],
        brands: list[str],
        options: dict[str, object],
        title: str | None = None,
        note: str = "",
    ) -> int:
        now = datetime.now(UTC).isoformat()
        clean_title = (title or "").strip() or (
            ("定时巡检" if trigger == "watch" else "手动巡检")
            + " "
            + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_runs(
                    title, note, trigger, status, platforms_json, brands_json,
                    options_json, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    clean_title,
                    note.strip(),
                    trigger,
                    json.dumps(platforms, ensure_ascii=False),
                    json.dumps(brands, ensure_ascii=False),
                    json.dumps(options, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def recover_stale_scan_runs(self, *, timeout_minutes: int = 30) -> int:
        """Close abandoned runs left by a crashed worker or desktop process."""
        cutoff = datetime.now(UTC).timestamp() - timeout_minutes * 60
        cutoff_iso = datetime.fromtimestamp(cutoff, UTC).isoformat()
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scan_runs
                SET status = 'interrupted', finished_at = ?,
                    error_message = CASE WHEN error_message = ''
                        THEN '任务进程中断，未完成部分未继续执行' ELSE error_message END
                WHERE status = 'running' AND started_at < ?
                """,
                (now, cutoff_iso),
            )
        return cursor.rowcount

    def acquire_task_lease(self, name: str, owner: str, *, lease_seconds: int = 21_600) -> bool:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        expires_iso = datetime.fromtimestamp(now.timestamp() + lease_seconds, UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM task_leases WHERE name = ?", (name,)
            ).fetchone()
            if row is not None and str(row["owner"]) != owner:
                try:
                    expired = datetime.fromisoformat(str(row["expires_at"])) <= now
                except ValueError:
                    expired = True
                if not expired:
                    return False
            connection.execute(
                """
                INSERT INTO task_leases(name, owner, acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner = excluded.owner,
                    acquired_at = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                """,
                (name, owner, now_iso, now_iso, expires_iso),
            )
        return True

    def heartbeat_task_lease(self, name: str, owner: str, *, lease_seconds: int = 21_600) -> bool:
        now = datetime.now(UTC)
        expires_iso = datetime.fromtimestamp(now.timestamp() + lease_seconds, UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE name = ? AND owner = ?
                """,
                (now.isoformat(), expires_iso, name, owner),
            )
        return cursor.rowcount > 0

    def release_task_lease(self, name: str, owner: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM task_leases WHERE name = ? AND owner = ?",
                (name, owner),
            )
        return cursor.rowcount > 0

    def update_scan_run_metadata(self, run_id: int, *, title: str, note: str = "") -> bool:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("巡检记录标题不能为空")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE scan_runs SET title = ?, note = ? WHERE id = ?",
                (clean_title, note.strip(), run_id),
            )
        return cursor.rowcount > 0

    def delete_scan_run(self, run_id: int) -> bool:
        """Delete one run and its batch links, while retaining collected content."""
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM scan_runs WHERE id = ?", (run_id,))
        return cursor.rowcount > 0

    def finish_scan_run(
        self,
        run_id: int,
        *,
        status: str,
        collected: int,
        scanned: int = 0,
        filtered: int = 0,
        inserted: int = 0,
        updated: int = 0,
        succeeded: int = 0,
        failed: int = 0,
        suspected: int = 0,
        detailed: int = 0,
        media_items: int = 0,
        error_message: str = "",
        classification_summary: dict[str, object] | None = None,
        model_summary: dict[str, object] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE scan_runs SET
                    status = ?, finished_at = ?, collected_count = ?,
                    scanned_count = ?, filtered_count = ?, inserted_count = ?,
                    updated_count = ?, succeeded_count = ?, failed_count = ?, error_message = ?,
                    suspected_count = ?, detailed_count = ?, media_count = ?,
                    classification_json = ?, model_summary_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    collected,
                    scanned,
                    filtered,
                    inserted,
                    updated,
                    succeeded,
                    failed,
                    error_message,
                    suspected,
                    detailed,
                    media_items,
                    json.dumps(classification_summary or {}, ensure_ascii=False),
                    json.dumps(model_summary or {}, ensure_ascii=False),
                    run_id,
                ),
            )

    def create_scan_attempt(
        self,
        *,
        run_id: int,
        platform: str,
        keyword: str,
        attempt_no: int,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_attempts(
                    run_id, platform, keyword, attempt_no, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (run_id, platform, keyword, attempt_no, now),
            )
        return int(cursor.lastrowid)

    def finish_scan_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        collected: int = 0,
        scanned: int = 0,
        filtered: int = 0,
        inserted: int = 0,
        updated: int = 0,
        suspected: int = 0,
        detailed: int = 0,
        media_items: int = 0,
        error_status: str = "",
        error_message: str = "",
        screenshot_path: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE scan_attempts SET
                    status = ?, finished_at = ?, collected_count = ?,
                    scanned_count = ?, filtered_count = ?, inserted_count = ?,
                    updated_count = ?, error_status = ?, error_message = ?, screenshot_path = ?
                    , suspected_count = ?, detailed_count = ?, media_count = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    collected,
                    scanned,
                    filtered,
                    inserted,
                    updated,
                    error_status,
                    error_message,
                    screenshot_path,
                    suspected,
                    detailed,
                    media_items,
                    attempt_id,
                ),
            )

    def save_scan_candidates(
        self,
        *,
        run_id: int,
        attempt_id: int,
        items: Iterable[CollectedContent],
    ) -> int:
        """Persist lightweight search hits before admission filtering."""
        now = datetime.now(UTC).isoformat()
        count = 0
        with self.connect() as connection:
            for item in items:
                connection.execute(
                    """
                    INSERT INTO scan_candidates(
                        run_id, attempt_id, platform, keyword, platform_content_id,
                        url, title, author_name, raw_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
                    ON CONFLICT(attempt_id, platform_content_id) DO UPDATE SET
                        url = excluded.url, title = excluded.title,
                        author_name = excluded.author_name, raw_json = excluded.raw_json
                    """,
                    (
                        run_id,
                        attempt_id,
                        item.platform.value,
                        item.source_keyword,
                        item.content_id,
                        item.url,
                        item.title,
                        item.author_name,
                        json.dumps(item.raw_data, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                count += 1
        return count

    def mark_scan_candidates(
        self,
        *,
        attempt_id: int,
        admitted_content_ids: Iterable[str],
        filter_reason: str = "未达到入库条件",
    ) -> int:
        admitted = {str(value) for value in admitted_content_ids}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, platform_content_id FROM scan_candidates WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
            updated = 0
            for row in rows:
                is_admitted = str(row["platform_content_id"]) in admitted
                cursor = connection.execute(
                    """
                    UPDATE scan_candidates
                    SET status = ?, filter_reason = ?
                    WHERE id = ?
                    """,
                    (
                        "admitted" if is_admitted else "filtered",
                        "" if is_admitted else filter_reason,
                        row["id"],
                    ),
                )
                updated += cursor.rowcount
        return updated

    def create_alert(
        self,
        *,
        kind: str,
        severity: str,
        message: str,
        run_id: int | None = None,
        attempt_id: int | None = None,
        platform: str = "",
        keyword: str = "",
        screenshot_path: str | None = None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO alerts(
                    run_id, attempt_id, platform, keyword, kind, severity,
                    message, screenshot_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    attempt_id,
                    platform,
                    keyword,
                    kind,
                    severity,
                    message,
                    screenshot_path,
                    now,
                ),
            )
            alert_id = int(cursor.lastrowid)
            self._upsert_notification(
                connection,
                kind="runtime_alert",
                severity=severity,
                title="巡检运行异常",
                message=message,
                entity_type="alert",
                entity_id=str(alert_id),
                now=now,
            )
        return alert_id

    def list_scan_runs(self, *, limit: int = 20) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT sr.*, COUNT(DISTINCT src.content_item_id) AS linked_content_count
                FROM scan_runs sr
                LEFT JOIN scan_run_contents src ON src.run_id = sr.id
                GROUP BY sr.id
                ORDER BY sr.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._scan_run_dict(row) for row in rows]

    def get_scan_run(self, run_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            run_row = connection.execute(
                "SELECT * FROM scan_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                return None
            attempt_rows = connection.execute(
                "SELECT * FROM scan_attempts WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            content_count = connection.execute(
                "SELECT COUNT(DISTINCT content_item_id) FROM scan_run_contents WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        result = self._scan_run_dict(run_row)
        result["attempts"] = [dict(row) for row in attempt_rows]
        result["content_count"] = int(content_count)
        return result

    def list_scan_run_choices(self, *, limit: int = 30) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, trigger, status, started_at, finished_at,
                       title, note, collected_count, succeeded_count, failed_count
                FROM scan_runs
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_alerts(
        self,
        *,
        unacknowledged_only: bool = False,
        limit: int = 50,
        run_id: int | None = None,
    ) -> list[dict[str, object]]:
        conditions: list[str] = []
        parameters: list[object] = []
        if unacknowledged_only:
            conditions.append("acknowledged_at IS NULL")
        if run_id is not None:
            conditions.append("run_id = ?")
            parameters.append(run_id)
        query = "SELECT * FROM alerts"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_alert(self, alert_id: int) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE alerts SET acknowledged_at = ?
                WHERE id = ? AND acknowledged_at IS NULL
                """,
                (now, alert_id),
            )
        return cursor.rowcount > 0

    def list_notifications(
        self,
        *,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        query = "SELECT * FROM app_notifications"
        if unread_only:
            query += " WHERE read_at IS NULL"
        query += " ORDER BY id DESC LIMIT ?"
        with self.connect() as connection:
            rows = connection.execute(query, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def count_unread_notifications(self) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM app_notifications WHERE read_at IS NULL"
                ).fetchone()[0]
            )

    def create_notification(
        self, *, severity: str, title: str, message: str, read: bool = False
    ) -> int:
        clean_title = title.strip()
        clean_message = message.strip()
        if not clean_title or not clean_message:
            raise ValueError("标题和内容不能为空")
        now = datetime.now(UTC).isoformat()
        read_at = now if read else None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO app_notifications(
                    kind, severity, title, message, entity_type, entity_id,
                    created_at, read_at
                ) VALUES ('manual', ?, ?, ?, 'manual', ?, ?, ?)
                """,
                (severity, clean_title, clean_message, uuid.uuid4().hex, now, read_at),
            )
        return int(cursor.lastrowid)

    def update_notification(
        self,
        notification_id: int,
        *,
        severity: str,
        title: str,
        message: str,
        read: bool,
    ) -> bool:
        clean_title = title.strip()
        clean_message = message.strip()
        if not clean_title or not clean_message:
            raise ValueError("标题和内容不能为空")
        now = datetime.now(UTC).isoformat() if read else None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE app_notifications SET severity = ?, title = ?, message = ?, read_at = ?
                WHERE id = ?
                """,
                (severity, clean_title, clean_message, now, notification_id),
            )
        return cursor.rowcount > 0

    def delete_notifications(self, notification_ids: Iterable[int]) -> int:
        ids = sorted({int(value) for value in notification_ids})
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM app_notifications WHERE id IN ({placeholders})", ids
            )
        return cursor.rowcount

    def mark_notifications_read(self, notification_ids: Iterable[int]) -> int:
        ids = sorted({int(value) for value in notification_ids})
        if not ids:
            return 0
        now = datetime.now(UTC).isoformat()
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE app_notifications SET read_at = COALESCE(read_at, ?)
                WHERE id IN ({placeholders})
                """,
                [now, *ids],
            )
        return cursor.rowcount

    def mark_notification_read(self, notification_id: int) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE app_notifications SET read_at = ?
                WHERE id = ? AND read_at IS NULL
                """,
                (now, notification_id),
            )
        return cursor.rowcount > 0

    def get_schedule_config(self) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM schedule_config WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("定时巡检配置尚未初始化")
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["legacy_imported"] = bool(result["legacy_imported"])
        return result

    def save_schedule_config(
        self,
        *,
        enabled: bool,
        frequency: str,
        schedule_time: str,
        weekday: int,
        interval_minutes: int,
        scan_mode: str,
        last_scheduled_at: str | None = None,
        next_run_at: str | None = None,
        missed_run_policy: str = "run_once",
        legacy_imported: bool = True,
    ) -> None:
        if frequency not in {"daily", "weekly", "interval"}:
            raise ValueError("定时频次必须是 daily、weekly 或 interval")
        if scan_mode not in {"quick", "deep"}:
            raise ValueError("定时巡检模式必须是 quick 或 deep")
        if not 0 <= weekday <= 6:
            raise ValueError("执行日必须在周一到周日之间")
        if not 5 <= interval_minutes <= 1440:
            raise ValueError("巡检间隔必须在 5 到 1440 分钟之间")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO schedule_config(
                    id, enabled, frequency, schedule_time, weekday, interval_minutes,
                    scan_mode, last_scheduled_at, next_run_at, missed_run_policy,
                    legacy_imported, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    frequency = excluded.frequency,
                    schedule_time = excluded.schedule_time,
                    weekday = excluded.weekday,
                    interval_minutes = excluded.interval_minutes,
                    scan_mode = excluded.scan_mode,
                    last_scheduled_at = excluded.last_scheduled_at,
                    next_run_at = excluded.next_run_at,
                    missed_run_policy = excluded.missed_run_policy,
                    legacy_imported = excluded.legacy_imported,
                    updated_at = excluded.updated_at
                """,
                (
                    int(enabled),
                    frequency,
                    schedule_time,
                    weekday,
                    interval_minutes,
                    scan_mode,
                    last_scheduled_at,
                    next_run_at,
                    missed_run_policy,
                    int(legacy_imported),
                    now,
                ),
            )

    def get_wecom_config(self) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM wecom_config WHERE id = 1").fetchone()
        if row is None:
            return {
                "enabled": False,
                "bot_id": "",
                "chat_id": "",
                "ws_url": "wss://openws.work.weixin.qq.com",
            }
        return {**dict(row), "enabled": bool(row["enabled"])}

    def get_llm_config(self) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM llm_config WHERE id = 1").fetchone()
        if row is None:
            return {
                "enabled": False,
                "provider": "openai-compatible",
                "base_url": "https://api.openai.com/v1",
                "model": "",
                "max_candidates": 20,
                "capabilities": {},
                "probed_at": None,
            }
        result = {**dict(row), "enabled": bool(row["enabled"])}
        try:
            result["capabilities"] = json.loads(str(result.pop("capabilities_json", "{}")))
        except json.JSONDecodeError:
            result["capabilities"] = {}
        return result

    def save_llm_capabilities(self, capabilities: dict[str, object]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                (
                    "UPDATE llm_config SET capabilities_json = ?, probed_at = ?, "
                    "updated_at = ? WHERE id = 1"
                ),
                (json.dumps(capabilities, ensure_ascii=False), now, now),
            )

    def save_llm_config(
        self,
        *,
        enabled: bool,
        provider: str,
        base_url: str,
        model: str,
        max_candidates: int = 20,
    ) -> None:
        clean_provider = provider.strip() or "openai-compatible"
        clean_base_url = base_url.strip().rstrip("/")
        clean_model = model.strip()
        if not clean_base_url:
            raise ValueError("大模型 Base URL 不能为空")
        parsed_url = urlparse(clean_base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("大模型 Base URL 必须是 http 或 https 地址")
        if parsed_url.scheme == "http" and parsed_url.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("非本机大模型地址必须使用 HTTPS")
        if enabled and not clean_model:
            raise ValueError("启用大模型时必须填写模型名")
        if not 1 <= max_candidates <= 100:
            raise ValueError("大模型候选条数必须在 1 到 100 之间")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_config(
                    id, enabled, provider, base_url, model, max_candidates, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    provider = excluded.provider,
                    base_url = excluded.base_url,
                    model = excluded.model,
                    max_candidates = excluded.max_candidates,
                    capabilities_json = '{}',
                    probed_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    int(enabled),
                    clean_provider,
                    clean_base_url,
                    clean_model,
                    max_candidates,
                    now,
                ),
            )

    def save_wecom_config(
        self,
        *,
        enabled: bool,
        bot_id: str,
        chat_id: str,
        ws_url: str = "wss://openws.work.weixin.qq.com",
    ) -> None:
        clean_bot_id = bot_id.strip()
        clean_chat_id = chat_id.strip()
        clean_ws_url = ws_url.strip() or "wss://openws.work.weixin.qq.com"
        parsed_ws_url = urlparse(clean_ws_url)
        if clean_ws_url != "wss://openws.work.weixin.qq.com":
            raise ValueError("企微 WebSocket 地址必须使用官方 WSS 地址")
        if parsed_ws_url.scheme != "wss":
            raise ValueError("企微 WebSocket 地址必须使用 WSS")
        if enabled and not clean_bot_id:
            raise ValueError("启用企微日报时必须填写 Bot ID")
        if enabled and not clean_chat_id:
            raise ValueError("启用企微日报时必须填写群聊 ID")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO wecom_config(
                    id, enabled, bot_id, chat_id, ws_url, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    bot_id = excluded.bot_id,
                    chat_id = excluded.chat_id,
                    ws_url = excluded.ws_url,
                    updated_at = excluded.updated_at
                """,
                (
                    int(enabled),
                    clean_bot_id,
                    clean_chat_id,
                    clean_ws_url,
                    now,
                    now,
                ),
            )

    def get_daily_report(self, report_date: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM daily_reports WHERE report_date = ?",
                (report_date,),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_daily_report(
        self,
        *,
        report_date: str,
        scan_run_id: int,
        content: str,
        status: str = "pending",
        error_message: str = "",
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_reports(
                    report_date, scan_run_id, status, content, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_date) DO UPDATE SET
                    scan_run_id = excluded.scan_run_id,
                    status = excluded.status,
                    content = excluded.content,
                    error_message = excluded.error_message
                """,
                (report_date, scan_run_id, status, content, error_message, now),
            )
            row = connection.execute(
                "SELECT id FROM daily_reports WHERE report_date = ?", (report_date,)
            ).fetchone()
        if row is None:
            raise RuntimeError("日报写入后无法读取主键")
        return int(row["id"])

    def claim_daily_report(
        self,
        *,
        report_date: str,
        scan_run_id: int,
        content: str,
        stale_after_minutes: int = 30,
    ) -> bool:
        """Atomically claim a report date for one sender."""
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        stale_before = datetime.fromtimestamp(
            now.timestamp() - stale_after_minutes * 60, UTC
        ).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, created_at FROM daily_reports WHERE report_date = ?",
                (report_date,),
            ).fetchone()
            if row is not None and str(row["status"]) == "sent":
                return False
            if (
                row is not None
                and str(row["status"]) == "sending"
                and str(row["created_at"]) > stale_before
            ):
                return False
            if row is None:
                connection.execute(
                    """
                    INSERT INTO daily_reports(
                        report_date, scan_run_id, status, content, error_message, created_at
                    ) VALUES (?, ?, 'sending', ?, '', ?)
                    """,
                    (report_date, scan_run_id, content, now_iso),
                )
            else:
                connection.execute(
                    """
                    UPDATE daily_reports
                    SET scan_run_id = ?, status = 'sending', content = ?,
                        error_message = '', created_at = ?, sent_at = NULL
                    WHERE report_date = ?
                    """,
                    (scan_run_id, content, now_iso, report_date),
                )
        return True

    def mark_daily_report_sent(self, report_date: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE daily_reports
                SET status = 'sent', sent_at = ?, error_message = ''
                WHERE report_date = ? AND status = 'sending'
                """,
                (now, report_date),
            )
        return cursor.rowcount > 0

    def mark_daily_report_failed(self, report_date: str, error_message: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE daily_reports SET status = 'failed', error_message = ?
                WHERE report_date = ? AND status = 'sending'
                """,
                (error_message[:1000], report_date),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _upsert_notification(
        connection: sqlite3.Connection,
        *,
        kind: str,
        severity: str,
        title: str,
        message: str,
        entity_type: str,
        entity_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO app_notifications(
                kind, severity, title, message, entity_type, entity_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, entity_type, entity_id) DO UPDATE SET
                severity = excluded.severity,
                title = excluded.title,
                message = excluded.message
            """,
            (kind, severity, title, message, entity_type, entity_id, now),
        )

    @staticmethod
    def _scan_run_dict(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        for key in ("platforms_json", "brands_json", "options_json"):
            result[key.removesuffix("_json")] = json.loads(str(result.pop(key)))
        for key in ("classification_json", "model_summary_json"):
            raw = result.pop(key, "{}")
            try:
                result[key.removesuffix("_json")] = json.loads(str(raw))
            except json.JSONDecodeError:
                result[key.removesuffix("_json")] = {}
        return result

    def add_brand(self, name: str, *, enabled: bool = True) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("品牌名不能为空")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM brands WHERE name = ?", (clean_name,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO brands(name, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (clean_name, int(enabled), now, now),
            )
            brand = connection.execute(
                "SELECT id FROM brands WHERE name = ?", (clean_name,)
            ).fetchone()
            if brand is None:
                raise RuntimeError("品牌写入后无法读取主键")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO brand_keywords(brand_id, keyword, enabled, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(brand_id, keyword) DO NOTHING
                    """,
                    (brand["id"], clean_name, now, now),
                )

    def list_brands(self, *, enabled_only: bool = False) -> list[dict[str, object]]:
        query = "SELECT id, name, enabled, created_at, updated_at FROM brands"
        parameters: tuple[object, ...] = ()
        if enabled_only:
            query += " WHERE enabled = ?"
            parameters = (1,)
        query += " ORDER BY id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def set_brand_enabled(self, name: str, enabled: bool) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE brands SET enabled = ?, updated_at = ? WHERE name = ?",
                (int(enabled), now, name.strip()),
            )
        return cursor.rowcount > 0

    def rename_brand(self, old_name: str, new_name: str) -> bool:
        clean_new_name = new_name.strip()
        if not clean_new_name:
            raise ValueError("新品牌名不能为空")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE brands SET name = ?, updated_at = ? WHERE name = ?",
                (clean_new_name, now, old_name.strip()),
            )
        return cursor.rowcount > 0

    def delete_brand(self, name: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM brands WHERE name = ?", (name.strip(),))
        return cursor.rowcount > 0

    def add_keyword(self, brand_name: str, keyword: str, *, enabled: bool = True) -> int:
        clean_keyword = keyword.strip()
        if not clean_keyword:
            raise ValueError("关键词不能为空")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            brand = connection.execute(
                "SELECT id FROM brands WHERE name = ?", (brand_name.strip(),)
            ).fetchone()
            if brand is None:
                raise ValueError("未找到目标品牌")
            connection.execute(
                """
                INSERT INTO brand_keywords(brand_id, keyword, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(brand_id, keyword) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (brand["id"], clean_keyword, int(enabled), now, now),
            )
            row = connection.execute(
                "SELECT id FROM brand_keywords WHERE brand_id = ? AND keyword = ?",
                (brand["id"], clean_keyword),
            ).fetchone()
        if row is None:
            raise RuntimeError("关键词写入后无法读取主键")
        return int(row["id"])

    def list_keywords(
        self,
        *,
        brand_name: str | None = None,
        enabled_only: bool = False,
    ) -> list[dict[str, object]]:
        conditions: list[str] = []
        parameters: list[object] = []
        if brand_name is not None:
            conditions.append("b.name = ?")
            parameters.append(brand_name.strip())
        if enabled_only:
            conditions.extend(("b.enabled = 1", "bk.enabled = 1"))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT bk.id, bk.brand_id, b.name AS brand_name, bk.keyword,
                       bk.enabled, bk.created_at, bk.updated_at
                FROM brand_keywords bk
                JOIN brands b ON b.id = bk.brand_id
                {where}
                ORDER BY b.id, bk.id
                """,
                parameters,
            ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def list_scan_targets(self) -> list[dict[str, object]]:
        return self.list_keywords(enabled_only=True)

    def rename_keyword(self, keyword_id: int, new_keyword: str) -> bool:
        clean_keyword = new_keyword.strip()
        if not clean_keyword:
            raise ValueError("新关键词不能为空")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE brand_keywords SET keyword = ?, updated_at = ? WHERE id = ?",
                (clean_keyword, now, keyword_id),
            )
        return cursor.rowcount > 0

    def set_keyword_enabled(self, keyword_id: int, enabled: bool) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE brand_keywords SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), now, keyword_id),
            )
        return cursor.rowcount > 0

    def delete_keyword(self, keyword_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM brand_keywords WHERE id = ?", (keyword_id,))
        return cursor.rowcount > 0

    def add_account(self, platform: str, display_name: str) -> int:
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("账号名称不能为空")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO platform_accounts(
                    platform, display_name, enabled, status, created_at, updated_at
                ) VALUES (?, ?, 1, 'not_logged_in', ?, ?)
                ON CONFLICT(platform, display_name) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (platform, clean_name, now, now),
            )
            row = connection.execute(
                "SELECT id FROM platform_accounts WHERE platform = ? AND display_name = ?",
                (platform, clean_name),
            ).fetchone()
        if row is None:
            raise RuntimeError("账号写入后无法读取主键")
        return int(row["id"])

    def list_accounts(self, *, enabled_only: bool = False) -> list[dict[str, object]]:
        query = "SELECT * FROM platform_accounts"
        parameters: tuple[object, ...] = ()
        if enabled_only:
            query += " WHERE enabled = ?"
            parameters = (1,)
        query += " ORDER BY platform, id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def get_scan_account(self, platform: str) -> dict[str, object] | None:
        accounts = self.list_scan_accounts(platform)
        return accounts[0] if accounts else None

    def list_scan_accounts(self, platform: str) -> list[dict[str, object]]:
        """Return healthy accounts in least-recently-checked order."""
        accounts = [
            item
            for item in self.list_accounts(enabled_only=True)
            if str(item["platform"]) == platform and str(item["status"]) == "ready"
        ]
        return sorted(
            accounts,
            key=lambda item: (str(item.get("last_checked_at") or ""), int(item["id"])),
        )

    def set_account_enabled(self, account_id: int, enabled: bool) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE platform_accounts SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), now, account_id),
            )
        return cursor.rowcount > 0

    def update_account_status(self, account_id: int, status: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE platform_accounts
                SET status = ?, last_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, now, account_id),
            )
        return cursor.rowcount > 0

    def delete_account(self, account_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM platform_accounts WHERE id = ?", (account_id,))
        return cursor.rowcount > 0

    def upsert_contents(self, items: Iterable[CollectedContent]) -> UpsertStats:
        inserted = 0
        updated = 0
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            for item in items:
                existing = connection.execute(
                    """
                    SELECT id, raw_json FROM content_items
                    WHERE platform = ? AND platform_content_id = ?
                    """,
                    (item.platform.value, item.content_id),
                ).fetchone()
                raw_data = self._merge_raw_data(
                    str(existing["raw_json"]) if existing is not None else None,
                    item.raw_data,
                )

                connection.execute(
                    """
                    INSERT INTO content_items(
                        platform, platform_content_id, url, title, author_name,
                        published_at, metrics_json, raw_json, fingerprint,
                        discovered_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, platform_content_id) DO UPDATE SET
                        url = excluded.url,
                        title = CASE
                            WHEN excluded.title <> '' THEN excluded.title
                            ELSE content_items.title
                        END,
                        author_name = CASE
                            WHEN excluded.author_name <> '' THEN excluded.author_name
                            ELSE content_items.author_name
                        END,
                        published_at = COALESCE(excluded.published_at, content_items.published_at),
                        metrics_json = excluded.metrics_json,
                        raw_json = excluded.raw_json,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        item.platform.value,
                        item.content_id,
                        item.url,
                        item.title,
                        item.author_name,
                        item.published_at,
                        json.dumps(item.metrics, ensure_ascii=False, sort_keys=True),
                        json.dumps(raw_data, ensure_ascii=False, sort_keys=True),
                        item.fingerprint,
                        item.discovered_at,
                        now,
                    ),
                )
                content_row = connection.execute(
                    """
                    SELECT id FROM content_items
                    WHERE platform = ? AND platform_content_id = ?
                    """,
                    (item.platform.value, item.content_id),
                ).fetchone()
                if content_row is None:
                    raise RuntimeError("内容写入后无法读取主键")

                matched_brand = item.brand_name or item.source_keyword
                brand_row = connection.execute(
                    "SELECT id FROM brands WHERE name = ?",
                    (matched_brand,),
                ).fetchone()
                if brand_row is None:
                    self._insert_brand(connection, matched_brand, now)
                    brand_row = connection.execute(
                        "SELECT id FROM brands WHERE name = ?",
                        (matched_brand,),
                    ).fetchone()
                if brand_row is None:
                    raise RuntimeError("品牌写入后无法读取主键")

                connection.execute(
                    """
                    INSERT INTO content_matches(
                        content_item_id, brand_id, source_keyword,
                        first_matched_at, last_matched_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(content_item_id, brand_id, source_keyword) DO UPDATE SET
                        last_matched_at = excluded.last_matched_at
                    """,
                    (content_row["id"], brand_row["id"], item.source_keyword, now, now),
                )
                if existing is None:
                    inserted += 1
                else:
                    updated += 1
        return UpsertStats(inserted=inserted, updated=updated)

    @staticmethod
    def _insert_brand(connection: sqlite3.Connection, name: str, now: str) -> None:
        connection.execute(
            """
            INSERT INTO brands(name, enabled, created_at, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name.strip(), now, now),
        )
        brand = connection.execute(
            "SELECT id FROM brands WHERE name = ?", (name.strip(),)
        ).fetchone()
        if brand is not None:
            connection.execute(
                """
                INSERT INTO brand_keywords(brand_id, keyword, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(brand_id, keyword) DO NOTHING
                """,
                (brand["id"], name.strip(), now, now),
            )

    @staticmethod
    def _merge_raw_data(
        existing_json: str | None,
        incoming: dict[str, object],
    ) -> dict[str, object]:
        existing: dict[str, object] = {}
        if existing_json:
            try:
                decoded = json.loads(existing_json)
            except json.JSONDecodeError:
                decoded = {}
            if isinstance(decoded, dict):
                existing = decoded

        merged = {**existing, **incoming}
        for key in ("page_title", "description", "comments"):
            if not incoming.get(key) and existing.get(key):
                merged[key] = existing[key]
        return merged
