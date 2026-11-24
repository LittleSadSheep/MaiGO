"""SQLite 数据库迁移管理。"""

from __future__ import annotations

import sqlite3
from typing import Callable, List, Tuple

from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.storage.schema import EXPECTED_TABLES, SCHEMA_VERSION, build_schema_sql

logger = get_logger("storage.migrations")

MigrationFn = Callable[[sqlite3.Connection], None]


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """初始化版本 1：通过幂等 DDL 建立全部基础业务表与 FTS5 索引。"""
    conn.executescript(build_schema_sql())


MIGRATIONS: List[Tuple[int, MigrationFn]] = [
    (1, _migrate_v1),
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
