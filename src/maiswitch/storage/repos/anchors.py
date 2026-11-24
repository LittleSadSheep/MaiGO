"""言行立场与跨端承诺锚点仓储（persona_anchors + persona_anchors_fts）。"""

from __future__ import annotations

import sqlite3
from typing import Any, List, Optional, Sequence

from maiswitch.runtime import ClockMs, now_ms
from maiswitch.storage.models import PersonaAnchorRow


def _row_to_anchor(row: sqlite3.Row) -> PersonaAnchorRow:
    return PersonaAnchorRow(
        id=int(row["id"]),
        anchor_type=str(row["anchor_type"]),
        content=str(row["content"]),
        verbatim=str(row["verbatim"]),
        source_event_id=int(row["source_event_id"]) if row["source_event_id"] is not None else None,
        branch_id=str(row["branch_id"]),
        visibility=str(row["visibility"]),
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        target_person_id=int(row["target_person_id"]) if row["target_person_id"] is not None else None,
        status=str(row["status"]),
        note=str(row["note"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


class PersonaAnchorRepo:
    """管理麦麦的跨端立场与承诺锚点。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def create(
        self,
        conn: sqlite3.Connection,
        *,
        anchor_type: str,
        content: str,
        verbatim: str,
        source_event_id: Optional[int],
        branch_id: str,
        visibility: str = "public",
        save_id: Optional[str] = None,
        target_person_id: Optional[int] = None,
        status: Optional[str] = None,
        note: str = "",
    ) -> PersonaAnchorRow:
        """创建一条新的立场或承诺锚点。"""
        now = int(self._clock_ms())
        resolved_status = status or ("open" if anchor_type == "promise" else "active")
        cursor = conn.execute(
            """
            INSERT INTO persona_anchors (
                anchor_type, content, verbatim, source_event_id, branch_id,
                visibility, save_id, target_person_id, status, note,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                anchor_type,
                content,
                verbatim,
                source_event_id,
                branch_id,
                visibility,
                save_id,
                target_person_id,
                resolved_status,
                note,
                now,
                now,
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        created = self.get_by_id(conn, int(cursor.lastrowid))
        assert created is not None
        return created

    def get_by_id(self, conn: sqlite3.Connection, anchor_id: int) -> Optional[PersonaAnchorRow]:
        """按主键获取锚点。"""
        row = conn.execute("SELECT * FROM persona_anchors WHERE id = ?", (int(anchor_id),)).fetchone()
        return _row_to_anchor(row) if row is not None else None

    def find_by_content(
        self,
        conn: sqlite3.Connection,
        *,
        anchor_type: str,
        content: str,
    ) -> Optional[PersonaAnchorRow]:
        """按类型与内容精确查找是否已有同类锚点，避免重复登记。"""
        row = conn.execute(
            """
            SELECT * FROM persona_anchors
            WHERE anchor_type = ? AND content = ? AND status IN ('active', 'open')
            ORDER BY id DESC
            LIMIT 1
            """,
            (anchor_type, content),
        ).fetchone()
        return _row_to_anchor(row) if row is not None else None

    def list_anchors(
        self,
        conn: sqlite3.Connection,
        *,
        anchor_type: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None,
        visibilities: Optional[Sequence[str]] = None,
        save_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[PersonaAnchorRow]:
        """按条件过滤列出锚点（默认按更新时刻倒序）。"""
        clauses: List[str] = []
        params: List[Any] = []
        if anchor_type:
            clauses.append("anchor_type = ?")
            params.append(anchor_type)
        if statuses is not None:
            status_list = list(statuses)
            if not status_list:
                return []
            placeholders = ",".join("?" for _ in status_list)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_list)
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
            f"SELECT * FROM persona_anchors {where_sql} ORDER BY updated_at_ms DESC, id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [_row_to_anchor(row) for row in rows]

    def update(
        self,
        conn: sqlite3.Connection,
        anchor_id: int,
        *,
        content: Optional[str] = None,
        status: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Optional[PersonaAnchorRow]:
        """部分更新锚点内容、状态（如兑现 closed / 误记 void）或备注。"""
        existing = self.get_by_id(conn, anchor_id)
        if existing is None:
            return None
        new_content = existing.content if content is None else content
        new_status = existing.status if status is None else status
        new_note = existing.note if note is None else note
        now = int(self._clock_ms())
        conn.execute(
            "UPDATE persona_anchors SET content = ?, status = ?, note = ?, updated_at_ms = ? WHERE id = ?",
            (new_content, new_status, new_note, now, int(anchor_id)),
        )
        conn.commit()
        return self.get_by_id(conn, anchor_id)

    def delete(self, conn: sqlite3.Connection, anchor_id: int) -> bool:
        """物理删除指定锚点。"""
        cursor = conn.execute("DELETE FROM persona_anchors WHERE id = ?", (int(anchor_id),))
        conn.commit()
        return int(cursor.rowcount) > 0

    def search(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        visibilities: Sequence[str],
        limit: int = 10,
    ) -> List[PersonaAnchorRow]:
        """检索锚点全文与子串，按可见性裁剪。"""
        vis_list = list(visibilities)
        if not vis_list:
            return []
        vis_placeholders = ",".join("?" for _ in vis_list)
        stripped = query.strip()
        if not stripped:
            return self.list_anchors(conn, visibilities=vis_list, limit=limit)
        pattern = f"%{stripped}%"
        rows = conn.execute(
            f"""
            SELECT * FROM persona_anchors
            WHERE visibility IN ({vis_placeholders})
              AND (content LIKE ? OR verbatim LIKE ? OR note LIKE ?)
            ORDER BY updated_at_ms DESC, id DESC
            LIMIT ?
            """,
            (*vis_list, pattern, pattern, pattern, max(1, int(limit))),
        ).fetchall()
        return [_row_to_anchor(row) for row in rows]
