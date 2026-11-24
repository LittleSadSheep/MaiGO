"""跨端人物图谱、身份绑定、口令、候选与遗忘名单仓储。"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence

from maigo.runtime import ClockMs, now_ms
from maigo.storage.models import (
    BindTokenRow,
    IdentityCandidateRow,
    PersonIdentityRow,
    PersonInteractionRow,
    PersonRow,
)


def _row_to_person(row: sqlite3.Row) -> PersonRow:
    return PersonRow(
        id=int(row["id"]),
        display_name=str(row["display_name"]),
        notes=str(row["notes"]),
        visibility_tier=str(row["visibility_tier"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _row_to_identity(row: sqlite3.Row) -> PersonIdentityRow:
    return PersonIdentityRow(
        id=int(row["id"]),
        person_id=int(row["person_id"]),
        platform=str(row["platform"]),
        external_id=str(row["external_id"]),
        external_name=str(row["external_name"]),
        bind_source=str(row["bind_source"]),
        bound_at_ms=int(row["bound_at_ms"]),
    )


def _row_to_interaction(row: sqlite3.Row) -> PersonInteractionRow:
    return PersonInteractionRow(
        id=int(row["id"]),
        person_id=int(row["person_id"]),
        branch_id=str(row["branch_id"]),
        platform=str(row["platform"]),
        visibility=str(row["visibility"]),
        summary=str(row["summary"]),
        occurred_at_ms=int(row["occurred_at_ms"]),
        source_event_id=int(row["source_event_id"]) if row["source_event_id"] is not None else None,
    )


def _row_to_token(row: sqlite3.Row) -> BindTokenRow:
    return BindTokenRow(
        token=str(row["token"]),
        person_id=int(row["person_id"]),
        platform_hint=str(row["platform_hint"]),
        created_at_ms=int(row["created_at_ms"]),
        expires_at_ms=int(row["expires_at_ms"]),
        used_at_ms=int(row["used_at_ms"]) if row["used_at_ms"] is not None else None,
    )


def _row_to_candidate(row: sqlite3.Row) -> IdentityCandidateRow:
    return IdentityCandidateRow(
        id=int(row["id"]),
        person_id=int(row["person_id"]),
        platform=str(row["platform"]),
        external_id=str(row["external_id"]),
        external_name=str(row["external_name"]),
        reason=str(row["reason"]),
        status=str(row["status"]),
        created_at_ms=int(row["created_at_ms"]),
        resolved_at_ms=int(row["resolved_at_ms"]) if row["resolved_at_ms"] is not None else None,
    )


class PersonRepo:
    """管理跨端人物档案与平台身份绑定。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def create_person(
        self,
        conn: sqlite3.Connection,
        *,
        display_name: str,
        notes: str = "",
        visibility_tier: str = "public",
    ) -> PersonRow:
        """创建统一人物主档案。"""
        now = int(self._clock_ms())
        cursor = conn.execute(
            """
            INSERT INTO persons (display_name, notes, visibility_tier, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (display_name.strip(), notes.strip(), visibility_tier, now, now),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        person = self.get_person(conn, int(cursor.lastrowid))
        assert person is not None
        return person

    def get_person(self, conn: sqlite3.Connection, person_id: int) -> Optional[PersonRow]:
        """按主键获取人物档案。"""
        row = conn.execute("SELECT * FROM persons WHERE id = ?", (int(person_id),)).fetchone()
        return _row_to_person(row) if row is not None else None

    def list_persons(self, conn: sqlite3.Connection) -> List[PersonRow]:
        """列出全部人物档案（按最近更新倒序）。"""
        rows = conn.execute("SELECT * FROM persons ORDER BY updated_at_ms DESC, id ASC").fetchall()
        return [_row_to_person(row) for row in rows]

    def find_persons_by_name(self, conn: sqlite3.Connection, name: str) -> List[PersonRow]:
        """按显示名或外部昵称精确查找人物（仅用于待审候选探测，绝不自动合并）。"""
        target = name.strip()
        if not target:
            return []
        rows = conn.execute(
            """
            SELECT DISTINCT p.* FROM persons p
            LEFT JOIN person_identities pi ON pi.person_id = p.id
            WHERE p.display_name = ? OR pi.external_name = ?
            """,
            (target, target),
        ).fetchall()
        return [_row_to_person(row) for row in rows]

    def delete_person(self, conn: sqlite3.Connection, person_id: int) -> bool:
        """删除人物档案（级联删除其绑定账号、互动与待审候选）。"""
        cursor = conn.execute("DELETE FROM persons WHERE id = ?", (int(person_id),))
        conn.commit()
        return int(cursor.rowcount) > 0

    def bind_identity(
        self,
        conn: sqlite3.Connection,
        *,
        person_id: int,
        platform: str,
        external_id: str,
        external_name: str = "",
        bind_source: str = "manual",
    ) -> PersonIdentityRow:
        """将外部平台账号显式绑定到指定人物；若该账号原本挂在别人名下则重新归属。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            INSERT INTO person_identities (person_id, platform, external_id, external_name, bind_source, bound_at_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, external_id) DO UPDATE SET
                person_id = excluded.person_id,
                external_name = CASE WHEN excluded.external_name != '' THEN excluded.external_name ELSE person_identities.external_name END,
                bind_source = excluded.bind_source,
                bound_at_ms = excluded.bound_at_ms
            """,
            (int(person_id), platform.strip(), external_id.strip(), external_name.strip(), bind_source, now),
        )
        conn.execute("UPDATE persons SET updated_at_ms = ? WHERE id = ?", (now, int(person_id)))
        conn.commit()
        row = self.get_identity(conn, platform=platform, external_id=external_id)
        assert row is not None
        return row

    def unbind_identity(self, conn: sqlite3.Connection, *, platform: str, external_id: str) -> bool:
        """解除外部平台账号绑定。"""
        cursor = conn.execute(
            "DELETE FROM person_identities WHERE platform = ? AND external_id = ?",
            (platform.strip(), external_id.strip()),
        )
        conn.commit()
        return int(cursor.rowcount) > 0

    def get_identity(self, conn: sqlite3.Connection, *, platform: str, external_id: str) -> Optional[PersonIdentityRow]:
        """按平台与外部账号精确查找绑定记录。"""
        row = conn.execute(
            "SELECT * FROM person_identities WHERE platform = ? AND external_id = ?",
            (platform.strip(), external_id.strip()),
        ).fetchone()
        return _row_to_identity(row) if row is not None else None

    def list_identities_for_person(self, conn: sqlite3.Connection, person_id: int) -> List[PersonIdentityRow]:
        """列出指定人物绑定的全部外部账号。"""
        rows = conn.execute(
            "SELECT * FROM person_identities WHERE person_id = ? ORDER BY platform ASC, id ASC",
            (int(person_id),),
        ).fetchall()
        return [_row_to_identity(row) for row in rows]

    def list_all_identities(self, conn: sqlite3.Connection) -> List[PersonIdentityRow]:
        """列出全部已绑定账号，供开播签出打包本地名录。"""
        rows = conn.execute("SELECT * FROM person_identities ORDER BY person_id ASC, id ASC").fetchall()
        return [_row_to_identity(row) for row in rows]

    def add_interaction(
        self,
        conn: sqlite3.Connection,
        *,
        person_id: int,
        branch_id: str,
        platform: str,
        visibility: str,
        summary: str,
        occurred_at_ms: int,
        source_event_id: Optional[int],
    ) -> PersonInteractionRow:
        """为人物追加一条跨端互动履历。"""
        cursor = conn.execute(
            """
            INSERT INTO person_interactions (
                person_id, branch_id, platform, visibility, summary, occurred_at_ms, source_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(person_id),
                branch_id,
                platform,
                visibility,
                summary,
                int(occurred_at_ms),
                source_event_id,
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        row = conn.execute("SELECT * FROM person_interactions WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        assert row is not None
        return _row_to_interaction(row)

    def list_interactions(
        self,
        conn: sqlite3.Connection,
        *,
        person_id: int,
        visibilities: Sequence[str],
        limit: int = 10,
    ) -> List[PersonInteractionRow]:
        """按可见性过滤读取人物近期互动履历。"""
        vis_list = list(visibilities)
        if not vis_list:
            return []
        placeholders = ",".join("?" for _ in vis_list)
        rows = conn.execute(
            f"""
            SELECT * FROM person_interactions
            WHERE person_id = ? AND visibility IN ({placeholders})
            ORDER BY occurred_at_ms DESC, id DESC
            LIMIT ?
            """,
            (int(person_id), *vis_list, max(1, int(limit))),
        ).fetchall()
        return [_row_to_interaction(row) for row in rows]

    def clear_interactions_for_person(self, conn: sqlite3.Connection, person_id: int) -> int:
        """清空指定人物的互动履历。"""
        cursor = conn.execute("DELETE FROM person_interactions WHERE person_id = ?", (int(person_id),))
        conn.commit()
        return int(cursor.rowcount)

    def create_bind_token(
        self,
        conn: sqlite3.Connection,
        *,
        token: str,
        person_id: int,
        platform_hint: str,
        expires_at_ms: int,
    ) -> BindTokenRow:
        """签发一次性跨端绑定口令。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            INSERT INTO bind_tokens (token, person_id, platform_hint, created_at_ms, expires_at_ms, used_at_ms)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (token, int(person_id), platform_hint.strip(), now, int(expires_at_ms)),
        )
        conn.commit()
        row = self.get_bind_token(conn, token)
        assert row is not None
        return row

    def get_bind_token(self, conn: sqlite3.Connection, token: str) -> Optional[BindTokenRow]:
        """读取绑定口令。"""
        row = conn.execute("SELECT * FROM bind_tokens WHERE token = ?", (token.strip(),)).fetchone()
        return _row_to_token(row) if row is not None else None

    def mark_token_used(self, conn: sqlite3.Connection, token: str) -> None:
        """标记绑定口令已核销。"""
        now = int(self._clock_ms())
        conn.execute("UPDATE bind_tokens SET used_at_ms = ? WHERE token = ?", (now, token.strip()))
        conn.commit()

    def add_candidate(
        self,
        conn: sqlite3.Connection,
        *,
        person_id: int,
        platform: str,
        external_id: str,
        external_name: str,
        reason: str,
    ) -> Optional[IdentityCandidateRow]:
        """登记一条待运营人工确认的疑似同人候选（已存在则不重复写入）。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            INSERT OR IGNORE INTO identity_candidates (
                person_id, platform, external_id, external_name, reason, status, created_at_ms, resolved_at_ms
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)
            """,
            (int(person_id), platform.strip(), external_id.strip(), external_name.strip(), reason, now),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT * FROM identity_candidates
            WHERE person_id = ? AND platform = ? AND external_id = ?
            """,
            (int(person_id), platform.strip(), external_id.strip()),
        ).fetchone()
        return _row_to_candidate(row) if row is not None else None

    def list_candidates(self, conn: sqlite3.Connection, *, status: str = "pending") -> List[IdentityCandidateRow]:
        """列出疑似同人候选列表。"""
        rows = conn.execute(
            "SELECT * FROM identity_candidates WHERE status = ? ORDER BY created_at_ms DESC, id DESC",
            (status,),
        ).fetchall()
        return [_row_to_candidate(row) for row in rows]

    def get_candidate(self, conn: sqlite3.Connection, candidate_id: int) -> Optional[IdentityCandidateRow]:
        """按主键查候选。"""
        row = conn.execute("SELECT * FROM identity_candidates WHERE id = ?", (int(candidate_id),)).fetchone()
        return _row_to_candidate(row) if row is not None else None

    def resolve_candidate(
        self, conn: sqlite3.Connection, *, candidate_id: int, status: str
    ) -> Optional[IdentityCandidateRow]:
        """更新候选审核状态（approved / rejected）。"""
        now = int(self._clock_ms())
        conn.execute(
            "UPDATE identity_candidates SET status = ?, resolved_at_ms = ? WHERE id = ?",
            (status, now, int(candidate_id)),
        )
        conn.commit()
        return self.get_candidate(conn, candidate_id)

    def add_forget_hash(self, conn: sqlite3.Connection, *, identity_hash: str, platform: str) -> None:
        """将外部身份摘要写入遗忘名单（幂等）。"""
        now = int(self._clock_ms())
        conn.execute(
            "INSERT OR IGNORE INTO forget_identities (identity_hash, platform, created_at_ms) VALUES (?, ?, ?)",
            (identity_hash, platform.strip(), now),
        )
        conn.commit()

    def is_forgotten_hash(self, conn: sqlite3.Connection, identity_hash: str) -> bool:
        """查询外部身份摘要是否已进入遗忘名单。"""
        row = conn.execute(
            "SELECT 1 FROM forget_identities WHERE identity_hash = ? LIMIT 1",
            (identity_hash,),
        ).fetchone()
        return row is not None
