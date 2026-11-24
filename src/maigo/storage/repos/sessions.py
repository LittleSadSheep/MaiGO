"""场次签出句柄、分支在线状态与游戏存档状态仓储。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from maigo.runtime import ClockMs, now_ms
from maigo.storage.models import BranchPresenceRow, CheckoutSessionRow, SaveStateRow


def _row_to_checkout(row: sqlite3.Row) -> CheckoutSessionRow:
    return CheckoutSessionRow(
        checkout_id=str(row["checkout_id"]),
        branch_id=str(row["branch_id"]),
        external_session_id=str(row["external_session_id"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        platform=str(row["platform"]),
        room_id=str(row["room_id"]),
        segment_title=str(row["segment_title"]),
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        visibility=str(row["visibility"]),
        active=bool(row["active"]),
        started_at_ms=int(row["started_at_ms"]),
        last_heartbeat_ms=int(row["last_heartbeat_ms"]),
        ended_at_ms=int(row["ended_at_ms"]) if row["ended_at_ms"] is not None else None,
    )


def _row_to_presence(row: sqlite3.Row) -> BranchPresenceRow:
    raw = json.loads(row["detail_json"] or "{}")
    detail: Dict[str, Any] = raw if isinstance(raw, dict) else {}
    return BranchPresenceRow(
        branch_id=str(row["branch_id"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        detail=detail,
        last_seen_ms=int(row["last_seen_ms"]),
    )


def _row_to_save(row: sqlite3.Row) -> SaveStateRow:
    raw = json.loads(row["metadata_json"] or "{}")
    meta: Dict[str, Any] = raw if isinstance(raw, dict) else {}
    return SaveStateRow(
        save_id=str(row["save_id"]),
        last_branch_id=str(row["last_branch_id"]),
        summary=str(row["summary"]),
        updated_at_ms=int(row["updated_at_ms"]),
        metadata=meta,
    )


class SessionRepo:
    """管理签出场次、分支在线状态与按存档隔离的游戏世界摘要。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def create_checkout(
        self,
        conn: sqlite3.Connection,
        *,
        checkout_id: str,
        branch_id: str,
        external_session_id: str,
        kind: str,
        title: str,
        platform: str,
        room_id: str,
        segment_title: str,
        save_id: Optional[str],
        visibility: str,
    ) -> CheckoutSessionRow:
        """创建场次签出记录，并同步把同一分支之前的孤儿活跃签出标为结束。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            UPDATE checkout_sessions
            SET active = 0, ended_at_ms = COALESCE(ended_at_ms, ?)
            WHERE branch_id = ? AND active = 1
            """,
            (now, branch_id),
        )
        conn.execute(
            """
            INSERT INTO checkout_sessions (
                checkout_id, branch_id, external_session_id, kind, title, platform,
                room_id, segment_title, save_id, visibility, active,
                started_at_ms, last_heartbeat_ms, ended_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
            """,
            (
                checkout_id,
                branch_id,
                external_session_id,
                kind,
                title,
                platform,
                room_id,
                segment_title,
                save_id,
                visibility,
                now,
                now,
            ),
        )
        conn.commit()
        created = self.get_checkout(conn, checkout_id)
        assert created is not None
        return created

    def get_checkout(self, conn: sqlite3.Connection, checkout_id: str) -> Optional[CheckoutSessionRow]:
        """按签出句柄查询场次记录。"""
        row = conn.execute(
            "SELECT * FROM checkout_sessions WHERE checkout_id = ?",
            (checkout_id,),
        ).fetchone()
        return _row_to_checkout(row) if row is not None else None

    def update_heartbeat(
        self,
        conn: sqlite3.Connection,
        *,
        checkout_id: str,
        segment_title: Optional[str] = None,
        save_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[CheckoutSessionRow]:
        """刷新进行中场次的心跳时间与当前环节。"""
        existing = self.get_checkout(conn, checkout_id)
        if existing is None:
            return None
        now = int(self._clock_ms())
        conn.execute(
            """
            UPDATE checkout_sessions
            SET segment_title = ?, save_id = ?, title = ?, last_heartbeat_ms = ?
            WHERE checkout_id = ?
            """,
            (
                existing.segment_title if segment_title is None else segment_title,
                existing.save_id if save_id is None else save_id,
                existing.title if title is None else title,
                now,
                checkout_id,
            ),
        )
        conn.commit()
        return self.get_checkout(conn, checkout_id)

    def finish_checkout(self, conn: sqlite3.Connection, *, checkout_id: str) -> Optional[CheckoutSessionRow]:
        """签入完成后关闭签出场次。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            UPDATE checkout_sessions
            SET active = 0, ended_at_ms = COALESCE(ended_at_ms, ?), last_heartbeat_ms = ?
            WHERE checkout_id = ?
            """,
            (now, now, checkout_id),
        )
        conn.commit()
        return self.get_checkout(conn, checkout_id)

    def list_active_checkouts(self, conn: sqlite3.Connection) -> List[CheckoutSessionRow]:
        """列出全部进行中的正式签出场次（过滤彩排场）。"""
        rows = conn.execute(
            """
            SELECT * FROM checkout_sessions
            WHERE active = 1 AND kind = 'live'
            ORDER BY started_at_ms DESC
            """
        ).fetchall()
        return [_row_to_checkout(row) for row in rows]

    def list_recent_checkouts(self, conn: sqlite3.Connection, *, limit: int = 10) -> List[CheckoutSessionRow]:
        """列出最近的正式签出场次（含已下播）。"""
        rows = conn.execute(
            """
            SELECT * FROM checkout_sessions
            WHERE kind = 'live'
            ORDER BY started_at_ms DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [_row_to_checkout(row) for row in rows]

    def touch_branch(
        self,
        conn: sqlite3.Connection,
        *,
        branch_id: str,
        mode: str = "streaming",
        status: str = "online",
        detail: Optional[Dict[str, Any]] = None,
    ) -> BranchPresenceRow:
        """更新分支在线心跳与状态。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            INSERT INTO branch_presence (branch_id, mode, status, detail_json, last_seen_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(branch_id) DO UPDATE SET
                mode = excluded.mode,
                status = excluded.status,
                detail_json = excluded.detail_json,
                last_seen_ms = excluded.last_seen_ms
            """,
            (
                branch_id.strip(),
                mode,
                status,
                json.dumps(detail or {}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM branch_presence WHERE branch_id = ?", (branch_id.strip(),)).fetchone()
        assert row is not None
        return _row_to_presence(row)

    def list_branches(self, conn: sqlite3.Connection) -> List[BranchPresenceRow]:
        """列出全部分支当前在线状态。"""
        rows = conn.execute("SELECT * FROM branch_presence ORDER BY last_seen_ms DESC").fetchall()
        return [_row_to_presence(row) for row in rows]

    def upsert_save_state(
        self,
        conn: sqlite3.Connection,
        *,
        save_id: str,
        branch_id: str,
        summary: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SaveStateRow:
        """更新指定游戏存档的最新世界摘要。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            INSERT INTO save_states (save_id, last_branch_id, summary, updated_at_ms, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(save_id) DO UPDATE SET
                last_branch_id = excluded.last_branch_id,
                summary = excluded.summary,
                updated_at_ms = excluded.updated_at_ms,
                metadata_json = excluded.metadata_json
            """,
            (
                save_id.strip(),
                branch_id.strip(),
                summary.strip(),
                now,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = self.get_save_state(conn, save_id)
        assert row is not None
        return row

    def get_save_state(self, conn: sqlite3.Connection, save_id: str) -> Optional[SaveStateRow]:
        """按存档 ID 读取跨宿主世界近况。"""
        row = conn.execute("SELECT * FROM save_states WHERE save_id = ?", (save_id.strip(),)).fetchone()
        return _row_to_save(row) if row is not None else None
