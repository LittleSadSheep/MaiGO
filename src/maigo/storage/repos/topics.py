"""鲜活话头池仓储（topic_seeds）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, List, Optional, Sequence

from maigo.core.models import TopicSeedRow
from maigo.runtime import ClockMs, now_ms


def _row_to_topic(row: sqlite3.Row) -> TopicSeedRow:
    consumed = json.loads(row["consumed_branches_json"] or "[]")
    return TopicSeedRow(
        id=int(row["id"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        reaction=str(row["reaction"]),
        source_branch=str(row["source_branch"]),
        source_event_id=int(row["source_event_id"]) if row["source_event_id"] is not None else None,
        visibility=str(row["visibility"]),
        consumed_branches=[str(x) for x in consumed],
        status=str(row["status"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        expires_at_ms=int(row["expires_at_ms"]),
    )


class TopicSeedRepo:
    """管理带跨分支消费轨迹的鲜活话头池。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def create(
        self,
        conn: sqlite3.Connection,
        *,
        title: str,
        summary: str,
        reaction: str,
        source_branch: str,
        source_event_id: Optional[int],
        visibility: str = "public",
        consumed_branches: Optional[Sequence[str]] = None,
        expires_at_ms: int,
    ) -> TopicSeedRow:
        """创建一条新的鲜活话头；相同事件不重复创建。"""
        if source_event_id is not None:
            existing = conn.execute(
                "SELECT * FROM topic_seeds WHERE source_event_id = ? LIMIT 1",
                (int(source_event_id),),
            ).fetchone()
            if existing is not None:
                return _row_to_topic(existing)
        now = int(self._clock_ms())
        consumed = list(dict.fromkeys(str(b).strip() for b in (consumed_branches or []) if str(b).strip()))
        cursor = conn.execute(
            """
            INSERT INTO topic_seeds (
                title, summary, reaction, source_branch, source_event_id,
                visibility, consumed_branches_json, status,
                created_at_ms, updated_at_ms, expires_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                title,
                summary,
                reaction,
                source_branch,
                source_event_id,
                visibility,
                json.dumps(consumed, ensure_ascii=False),
                now,
                now,
                int(expires_at_ms),
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        row = self.get_by_id(conn, int(cursor.lastrowid))
        assert row is not None
        return row

    def get_by_id(self, conn: sqlite3.Connection, topic_id: int) -> Optional[TopicSeedRow]:
        """按主键查询话头。"""
        row = conn.execute("SELECT * FROM topic_seeds WHERE id = ?", (int(topic_id),)).fetchone()
        return _row_to_topic(row) if row is not None else None

    def list_available_for_branch(
        self,
        conn: sqlite3.Connection,
        *,
        branch_id: str,
        visibilities: Sequence[str],
        now_override_ms: Optional[int] = None,
        limit: int = 12,
    ) -> List[TopicSeedRow]:
        """查询当前未过期、且指定分支尚未消费过的话头。"""
        vis_list = list(visibilities)
        if not vis_list:
            return []
        current_ms = int(self._clock_ms()) if now_override_ms is None else int(now_override_ms)
        placeholders = ",".join("?" for _ in vis_list)
        rows = conn.execute(
            f"""
            SELECT * FROM topic_seeds
            WHERE status = 'active'
              AND expires_at_ms > ?
              AND visibility IN ({placeholders})
            ORDER BY updated_at_ms DESC, id DESC
            LIMIT ?
            """,
            (current_ms, *vis_list, max(1, int(limit) * 3)),
        ).fetchall()
        matched: List[TopicSeedRow] = []
        for row in rows:
            topic = _row_to_topic(row)
            if branch_id and branch_id in topic.consumed_branches:
                continue
            matched.append(topic)
            if len(matched) >= limit:
                break
        return matched

    def list_all(
        self,
        conn: sqlite3.Connection,
        *,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[TopicSeedRow]:
        """供看板展示与管理列出全部话头。"""
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM topic_seeds {where_sql} ORDER BY updated_at_ms DESC, id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [_row_to_topic(row) for row in rows]

    def mark_consumed(self, conn: sqlite3.Connection, *, topic_id: int, branch_id: str) -> Optional[TopicSeedRow]:
        """将某分支登记为已消费该话头（幂等追加）。"""
        existing = self.get_by_id(conn, topic_id)
        if existing is None:
            return None
        clean_branch = branch_id.strip()
        if not clean_branch:
            return existing
        consumed = list(existing.consumed_branches)
        if clean_branch not in consumed:
            consumed.append(clean_branch)
        now = int(self._clock_ms())
        conn.execute(
            "UPDATE topic_seeds SET consumed_branches_json = ?, updated_at_ms = ? WHERE id = ?",
            (json.dumps(consumed, ensure_ascii=False), now, int(topic_id)),
        )
        conn.commit()
        return self.get_by_id(conn, topic_id)

    def update(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        *,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        reaction: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[TopicSeedRow]:
        """更新话头文本或状态。"""
        existing = self.get_by_id(conn, topic_id)
        if existing is None:
            return None
        now = int(self._clock_ms())
        conn.execute(
            """
            UPDATE topic_seeds
            SET title = ?, summary = ?, reaction = ?, status = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (
                existing.title if title is None else title,
                existing.summary if summary is None else summary,
                existing.reaction if reaction is None else reaction,
                existing.status if status is None else status,
                now,
                int(topic_id),
            ),
        )
        conn.commit()
        return self.get_by_id(conn, topic_id)

    def delete(self, conn: sqlite3.Connection, topic_id: int) -> bool:
        """删除话头。"""
        cursor = conn.execute("DELETE FROM topic_seeds WHERE id = ?", (int(topic_id),))
        conn.commit()
        return int(cursor.rowcount) > 0
