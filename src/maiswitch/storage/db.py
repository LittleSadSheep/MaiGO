"""SQLite 数据库连接与异步序列化执行封装。

所有磁盘写操作与事务在内部异步锁保护下通过 ``asyncio.to_thread`` 执行，
避免在 FastAPI 事件循环内发生同步阻塞，并支持 Windows 下显式释放文件锁。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Callable, Optional, TypeVar

from maiswitch.runtime import ClockMs, get_logger, now_ms
from maiswitch.storage.migrations import apply_migrations

logger = get_logger("storage.db")

T = TypeVar("T")


class SwitchDatabase:
    """MaiSwitch 的 SQLite 存储连接管理器。"""

    def __init__(self, db_path: Path | str, *, clock_ms: ClockMs = now_ms) -> None:
        self._db_path_str = str(db_path)
        self._is_memory = self._db_path_str == ":memory:" or self._db_path_str.startswith("file::memory:")
        self._clock_ms = clock_ms
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def db_path(self) -> str:
        """返回配置的数据库路径字符串。"""
        return self._db_path_str

    def open_sync(self) -> sqlite3.Connection:
        """同步打开连接、设置 WAL 与外键约束并完成迁移。"""
        if self._conn is not None:
            return self._conn
        if not self._is_memory:
            Path(self._db_path_str).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path_str, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        apply_migrations(conn, clock_ms=self._clock_ms)
        self._conn = conn
        logger.info(f"SQLite 存储已就绪: {self._db_path_str}")
        return conn

    async def initialize(self) -> None:
        """异步初始化连接并应用全部迁移。"""
        async with self._lock:
            await asyncio.to_thread(self.open_sync)

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """在序列化锁下将同步数据库读写操作调度至工作线程执行。"""
        async with self._lock:
            if self._conn is None:
                await asyncio.to_thread(self.open_sync)
            assert self._conn is not None
            return await asyncio.to_thread(fn, self._conn)

    def close_sync(self) -> None:
        """同步关闭数据库连接（释放文件句柄）。"""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning(f"关闭数据库连接时发生异常: {exc}")
            finally:
                self._conn = None

    async def close(self) -> None:
        """异步关闭数据库连接，确保 Windows 临时目录清理不报占用。"""
        async with self._lock:
            await asyncio.to_thread(self.close_sync)
