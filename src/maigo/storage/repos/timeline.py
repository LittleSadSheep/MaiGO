"""日内生活编年纪要仓储（daily_chronicles）。"""

from __future__ import annotations

import json
import sqlite3
from typing import List, Optional, Sequence

from maigo.core.models import DailyChronicleRow
from maigo.runtime import ClockMs, now_ms


def _row_to_chronicle(row: sqlite3.Row) -> DailyChronicleRow:
    raw_ids = json.loads(row["source_event_ids_json"] or "[]")
    keys = row.keys()
    return DailyChronicleRow(
        id=int(row["id"]),
        day_key=str(row["day_key"]),
        slot_key=str(row["slot_key"]),
        status=str(row["status"]),
        body=str(row["body"]),
        public_body=str(row["public_body"]),
        group_body=str(row["group_body"]),
        source_event_ids=[int(x) for x in raw_ids],
        sealed_at_ms=int(row["sealed_at_ms"]) if row["sealed_at_ms"] is not None else None,
        updated_at_ms=int(row["updated_at_ms"]),
        mood_tag=str(row["mood_tag"]) if "mood_tag" in keys else "",
        unfinished_thread=str(row["unfinished_thread"]) if "unfinished_thread" in keys else "",
    )


class DailyChronicleRepo:
    """管理作息时段生活纪要的生成、追加与不可变封存。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def get_slot(self, conn: sqlite3.Connection, *, day_key: str, slot_key: str) -> Optional[DailyChronicleRow]:
        """读取指定作息日与时段的纪要记录。"""
        row = conn.execute(
            "SELECT * FROM daily_chronicles WHERE day_key = ? AND slot_key = ?",
            (day_key, slot_key),
        ).fetchone()
        return _row_to_chronicle(row) if row is not None else None

    def upsert_slot(
        self,
        conn: sqlite3.Connection,
        *,
        day_key: str,
        slot_key: str,
        status: str,
        body: str,
        public_body: str,
        group_body: str,
        source_event_ids: Sequence[int],
        mood_tag: str = "",
        unfinished_thread: str = "",
    ) -> DailyChronicleRow:
        """写入或更新某时段纪要；若该时段已封存且本次未强制重开，保持封存时刻连续性。"""
        now = int(self._clock_ms())
        existing = self.get_slot(conn, day_key=day_key, slot_key=slot_key)
        sealed_at_ms: Optional[int] = existing.sealed_at_ms if existing is not None else None
        if status == "sealed" and sealed_at_ms is None:
            sealed_at_ms = now
        elif status == "open":
            sealed_at_ms = None

        conn.execute(
            """
            INSERT INTO daily_chronicles (
                day_key, slot_key, status, body, public_body, group_body,
                mood_tag, unfinished_thread, source_event_ids_json, sealed_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day_key, slot_key) DO UPDATE SET
                status = excluded.status,
                body = excluded.body,
                public_body = excluded.public_body,
                group_body = excluded.group_body,
                mood_tag = excluded.mood_tag,
                unfinished_thread = excluded.unfinished_thread,
                source_event_ids_json = excluded.source_event_ids_json,
                sealed_at_ms = COALESCE(daily_chronicles.sealed_at_ms, excluded.sealed_at_ms),
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                day_key,
                slot_key,
                status,
                body,
                public_body,
                group_body,
                mood_tag.strip(),
                unfinished_thread.strip(),
                json.dumps([int(eid) for eid in source_event_ids]),
                sealed_at_ms,
                now,
            ),
        )
        conn.commit()
        row = self.get_slot(conn, day_key=day_key, slot_key=slot_key)
        assert row is not None
        return row

    def list_by_day(
        self,
        conn: sqlite3.Connection,
        *,
        day_key: str,
        status: Optional[str] = None,
    ) -> List[DailyChronicleRow]:
        """按作息时段先后顺序列出指定日期的纪要。"""
        order_case = """
            CASE slot_key
                WHEN 'yesterday_carry' THEN 0
                WHEN 'morning' THEN 1
                WHEN 'afternoon' THEN 2
                WHEN 'evening' THEN 3
                WHEN 'late_night' THEN 4
                ELSE 9
            END
        """
        if status:
            rows = conn.execute(
                f"SELECT * FROM daily_chronicles WHERE day_key = ? AND status = ? ORDER BY {order_case}",
                (day_key, status),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM daily_chronicles WHERE day_key = ? ORDER BY {order_case}",
                (day_key,),
            ).fetchall()
        return [_row_to_chronicle(row) for row in rows]

    def list_open_before_day(self, conn: sqlite3.Connection, *, day_key: str) -> List[DailyChronicleRow]:
        """查找早于当前作息日但仍处于开放态的历史时段，供跨日自动封存。"""
        rows = conn.execute(
            "SELECT * FROM daily_chronicles WHERE day_key < ? AND status = 'open' ORDER BY day_key ASC",
            (day_key,),
        ).fetchall()
        return [_row_to_chronicle(row) for row in rows]

    def list_recent_days(self, conn: sqlite3.Connection, *, limit_days: int = 7) -> List[str]:
        """列出最近有纪要的作息日键值（新到旧）。"""
        rows = conn.execute(
            "SELECT DISTINCT day_key FROM daily_chronicles ORDER BY day_key DESC LIMIT ?",
            (max(1, int(limit_days)),),
        ).fetchall()
        return [str(row["day_key"]) for row in rows]
