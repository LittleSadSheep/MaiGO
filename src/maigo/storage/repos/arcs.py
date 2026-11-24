"""温层近期主线仓储（life_arcs）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, List, Optional, Sequence

from maigo.core.models import LifeArcRow
from maigo.runtime import ClockMs, now_ms


def _row_to_arc(row: sqlite3.Row) -> LifeArcRow:
    raw_ids = json.loads(row["source_event_ids_json"] or "[]")
    return LifeArcRow(
        id=int(row["id"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        public_summary=str(row["public_summary"]),
        status=str(row["status"]),
        visibility=str(row["visibility"]),
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        mood_hint=str(row["mood_hint"]),
        source_event_ids=[int(x) for x in raw_ids],
        last_event_at_ms=int(row["last_event_at_ms"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _merge_ids(existing: Sequence[int], incoming: Sequence[int]) -> List[int]:
    merged: List[int] = []
    seen: set[int] = set()
    for val in (*existing, *incoming):
        eid = int(val)
        if eid not in seen:
            seen.add(eid)
            merged.append(eid)
    return merged


class LifeArcRepo:
    """管理跨日持续的近期主线（工作周、连续在玩存档或热议主题）。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def get_by_id(self, conn: sqlite3.Connection, arc_id: int) -> Optional[LifeArcRow]:
        """按主键查询单条主线。"""
        row = conn.execute("SELECT * FROM life_arcs WHERE id = ?", (int(arc_id),)).fetchone()
        return _row_to_arc(row) if row is not None else None

    def find_matching(
        self,
        conn: sqlite3.Connection,
        *,
        title: str,
        save_id: Optional[str] = None,
    ) -> Optional[LifeArcRow]:
        """按游戏存档或精确标题查找尚未完结的主线以便延续而非重复建条。"""
        if save_id:
            row = conn.execute(
                "SELECT * FROM life_arcs WHERE save_id = ? AND status IN ('active', 'paused') ORDER BY id DESC LIMIT 1",
                (save_id,),
            ).fetchone()
            if row is not None:
                return _row_to_arc(row)
        cleaned = title.strip()
        if not cleaned:
            return None
        row = conn.execute(
            "SELECT * FROM life_arcs WHERE title = ? AND status IN ('active', 'paused') ORDER BY id DESC LIMIT 1",
            (cleaned,),
        ).fetchone()
        return _row_to_arc(row) if row is not None else None

    def upsert_or_create(
        self,
        conn: sqlite3.Connection,
        *,
        title: str,
        summary: str,
        public_summary: str = "",
        status: str = "active",
        visibility: str = "public",
        save_id: Optional[str] = None,
        mood_hint: str = "",
        source_event_ids: Sequence[int] = (),
        last_event_at_ms: Optional[int] = None,
    ) -> LifeArcRow:
        """创建新主线或衔接同存档/同标题的已有主线。"""
        now = int(self._clock_ms())
        event_ts = now if last_event_at_ms is None else int(last_event_at_ms)
        existing = self.find_matching(conn, title=title, save_id=save_id)
        if existing is not None:
            merged_ids = _merge_ids(existing.source_event_ids, source_event_ids)
            next_pub = public_summary.strip() or existing.public_summary
            next_mood = mood_hint.strip() or existing.mood_hint
            conn.execute(
                """
                UPDATE life_arcs
                SET title = ?, summary = ?, public_summary = ?, status = ?, visibility = ?,
                    save_id = ?, mood_hint = ?, source_event_ids_json = ?,
                    last_event_at_ms = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    title.strip() or existing.title,
                    summary.strip() or existing.summary,
                    next_pub,
                    status,
                    visibility,
                    save_id if save_id is not None else existing.save_id,
                    next_mood,
                    json.dumps(merged_ids),
                    max(existing.last_event_at_ms, event_ts),
                    now,
                    existing.id,
                ),
            )
            conn.commit()
            updated = self.get_by_id(conn, existing.id)
            assert updated is not None
            return updated

        cursor = conn.execute(
            """
            INSERT INTO life_arcs (
                title, summary, public_summary, status, visibility, save_id,
                mood_hint, source_event_ids_json, last_event_at_ms, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                summary.strip(),
                public_summary.strip(),
                status,
                visibility,
                save_id,
                mood_hint.strip(),
                json.dumps([int(x) for x in source_event_ids]),
                event_ts,
                now,
                now,
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        created = self.get_by_id(conn, int(cursor.lastrowid))
        assert created is not None
        return created

    def list_arcs(
        self,
        conn: sqlite3.Connection,
        *,
        statuses: Optional[Sequence[str]] = None,
        visibilities: Optional[Sequence[str]] = None,
        save_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[LifeArcRow]:
        """按状态与可见性列出近期主线（最近推进者靠前）。"""
        clauses: List[str] = []
        params: List[Any] = []
        if statuses is not None:
            st_list = list(statuses)
            if not st_list:
                return []
            placeholders = ",".join("?" for _ in st_list)
            clauses.append(f"status IN ({placeholders})")
            params.extend(st_list)
        if visibilities is not None:
            vis_list = list(visibilities)
            if not vis_list:
                return []
            placeholders = ",".join("?" for _ in vis_list)
            clauses.append(f"visibility IN ({placeholders})")
            params.extend(vis_list)
        if save_id is not None:
            clauses.append("(save_id IS NULL OR save_id = ?)")
            params.append(save_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM life_arcs {where_sql} ORDER BY last_event_at_ms DESC, id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [_row_to_arc(row) for row in rows]

    def update(
        self,
        conn: sqlite3.Connection,
        arc_id: int,
        *,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        public_summary: Optional[str] = None,
        status: Optional[str] = None,
        mood_hint: Optional[str] = None,
    ) -> Optional[LifeArcRow]:
        """部分更新主线标题、描述、状态或心情备注。"""
        existing = self.get_by_id(conn, arc_id)
        if existing is None:
            return None
        now = int(self._clock_ms())
        conn.execute(
            """
            UPDATE life_arcs
            SET title = ?, summary = ?, public_summary = ?, status = ?, mood_hint = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (
                existing.title if title is None else title.strip(),
                existing.summary if summary is None else summary.strip(),
                existing.public_summary if public_summary is None else public_summary.strip(),
                existing.status if status is None else status,
                existing.mood_hint if mood_hint is None else mood_hint.strip(),
                now,
                int(arc_id),
            ),
        )
        conn.commit()
        return self.get_by_id(conn, arc_id)

    def delete(self, conn: sqlite3.Connection, arc_id: int) -> bool:
        """物理删除指定主线。"""
        cursor = conn.execute("DELETE FROM life_arcs WHERE id = ?", (int(arc_id),))
        conn.commit()
        return int(cursor.rowcount) > 0

    def search(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        visibilities: Sequence[str],
        limit: int = 10,
    ) -> List[LifeArcRow]:
        """按子串关键词检索温层近期主线，严格过滤可见性。"""
        vis_list = list(visibilities)
        if not vis_list:
            return []
        stripped = query.strip()
        if not stripped:
            return self.list_arcs(conn, visibilities=vis_list, limit=limit)
        vis_placeholders = ",".join("?" for _ in vis_list)
        pattern = f"%{stripped}%"
        rows = conn.execute(
            f"""
            SELECT * FROM life_arcs
            WHERE visibility IN ({vis_placeholders})
              AND (title LIKE ? OR summary LIKE ? OR public_summary LIKE ? OR mood_hint LIKE ?)
            ORDER BY last_event_at_ms DESC, id DESC
            LIMIT ?
            """,
            (*vis_list, pattern, pattern, pattern, pattern, max(1, int(limit))),
        ).fetchall()
        return [_row_to_arc(row) for row in rows]
