"""SQLite 数据库迁移管理。"""

from __future__ import annotations

import sqlite3
from typing import Callable, List, Tuple

from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.storage.schema import EXPECTED_TABLES, SCHEMA_VERSION, build_schema_sql

logger = get_logger("storage.migrations")

MigrationFn = Callable[[sqlite3.Connection], None]


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """初始化版本 1：通过幂等 DDL 建立基础业务表与 FTS5 索引。"""
    conn.executescript(build_schema_sql())


def _rebuild_persona_anchors_v2(conn: sqlite3.Connection) -> None:
    """将旧版 persona_anchors 表无损升级为支持 superseded/stale 与替换指针的新表结构。"""
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS trg_anchors_ai;
            DROP TRIGGER IF EXISTS trg_anchors_ad;
            DROP TRIGGER IF EXISTS trg_anchors_au;
            DROP INDEX IF EXISTS idx_anchors_status;

            ALTER TABLE persona_anchors RENAME TO _persona_anchors_v1;

            CREATE TABLE persona_anchors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anchor_type TEXT NOT NULL CHECK (anchor_type IN ('stance', 'promise')),
                content TEXT NOT NULL,
                verbatim TEXT NOT NULL DEFAULT '',
                source_event_id INTEGER REFERENCES experience_events(id) ON DELETE SET NULL,
                branch_id TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'group', 'private')),
                save_id TEXT,
                target_person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'open', 'closed', 'void', 'superseded', 'stale')),
                superseded_by_id INTEGER REFERENCES persona_anchors(id) ON DELETE SET NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            );

            INSERT INTO persona_anchors (
                id, anchor_type, content, verbatim, source_event_id, branch_id,
                visibility, save_id, target_person_id, status, superseded_by_id,
                note, created_at_ms, updated_at_ms
            )
            SELECT
                id, anchor_type, content, verbatim, source_event_id, branch_id,
                visibility, save_id, target_person_id, status, NULL,
                note, created_at_ms, updated_at_ms
            FROM _persona_anchors_v1;

            DROP TABLE _persona_anchors_v1;

            CREATE INDEX IF NOT EXISTS idx_anchors_status ON persona_anchors (anchor_type, status, updated_at_ms DESC);

            CREATE TRIGGER trg_anchors_ai AFTER INSERT ON persona_anchors BEGIN
                INSERT INTO persona_anchors_fts(rowid, content, verbatim, note)
                VALUES (new.id, new.content, new.verbatim, new.note);
            END;

            CREATE TRIGGER trg_anchors_ad AFTER DELETE ON persona_anchors BEGIN
                INSERT INTO persona_anchors_fts(persona_anchors_fts, rowid, content, verbatim, note)
                VALUES ('delete', old.id, old.content, old.verbatim, old.note);
            END;

            CREATE TRIGGER trg_anchors_au AFTER UPDATE ON persona_anchors BEGIN
                INSERT INTO persona_anchors_fts(persona_anchors_fts, rowid, content, verbatim, note)
                VALUES ('delete', old.id, old.content, old.verbatim, old.note);
                INSERT INTO persona_anchors_fts(rowid, content, verbatim, note)
                VALUES (new.id, new.content, new.verbatim, new.note);
            END;

            INSERT INTO persona_anchors_fts(persona_anchors_fts) VALUES('rebuild');
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """升级版本 2：补齐情绪/悬念列、锚点演化链、中文 trigram 检索、温层主线与向量表。"""
    chronicle_cols = _column_names(conn, "daily_chronicles")
    if "mood_tag" not in chronicle_cols:
        conn.execute("ALTER TABLE daily_chronicles ADD COLUMN mood_tag TEXT NOT NULL DEFAULT ''")
    if "unfinished_thread" not in chronicle_cols:
        conn.execute("ALTER TABLE daily_chronicles ADD COLUMN unfinished_thread TEXT NOT NULL DEFAULT ''")

    anchor_cols = _column_names(conn, "persona_anchors")
    if "superseded_by_id" not in anchor_cols:
        _rebuild_persona_anchors_v2(conn)

    # 保证历史遗留事件也可被中文 trigram 索引命中
    conn.execute("INSERT INTO experience_events_trigram(experience_events_trigram) VALUES('rebuild')")
    conn.commit()


MIGRATIONS: List[Tuple[int, MigrationFn]] = [
    (1, _migrate_v1),
    (2, _migrate_v2),
]


def apply_migrations(conn: sqlite3.Connection, *, clock_ms: ClockMs = now_ms) -> int:
    """按版本号顺序幂等执行迁移，并返回最终 Schema 版本号。"""
    conn.executescript(build_schema_sql())
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC").fetchall()
    applied = {int(row[0]) for row in rows}

    for version, fn in MIGRATIONS:
        if version in applied:
            continue
        fn(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at_ms) VALUES (?, ?)",
            (version, int(clock_ms())),
        )
        conn.commit()
        logger.info(f"数据库迁移已应用至版本 v{version}")

    verify_expected_tables(conn)
    return SCHEMA_VERSION


def verify_expected_tables(conn: sqlite3.Connection) -> None:
    """校验全部预期表存在，缺一即报错，避免带着残缺库运行。"""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    existing = {str(row[0]) for row in rows}
    missing = [name for name in EXPECTED_TABLES if name not in existing]
    if missing:
        raise RuntimeError(f"数据库缺少必要表或虚表: {', '.join(missing)}")
