"""L0 原件书库仓储（episodes + episode_records + FTS5/trigram）。

职责边界：按场次归档细粒度一手证据（游戏里程碑/成就、筛选后主播原话、逐段实录、
存档笔记与生物名册），严禁进入日内摘要与稳定背景，仅供按需全文与语义翻查。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from maigo.core.models import EpisodeRecordRow, EpisodeRow, NewEpisodeRecord
from maigo.runtime import ClockMs, now_ms
from maigo.storage.repos._text import build_fts_match, build_trigram_match, count_term_hits, extract_query_terms
from maigo.storage.repos.events import IngestResult


def _row_to_episode(row: sqlite3.Row) -> EpisodeRow:
    raw_meta = json.loads(row["metadata_json"] or "{}")
    return EpisodeRow(
        id=int(row["id"]),
        branch_id=str(row["branch_id"]),
        external_episode_id=str(row["external_episode_id"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        platform=str(row["platform"]),
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        visibility=str(row["visibility"]),
        started_at_ms=int(row["started_at_ms"]),
        ended_at_ms=int(row["ended_at_ms"]) if row["ended_at_ms"] is not None else None,
        overview=str(row["overview"]),
        metadata=raw_meta if isinstance(raw_meta, dict) else {},
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _row_to_record(row: sqlite3.Row) -> EpisodeRecordRow:
    raw_meta = json.loads(row["metadata_json"] or "{}")
    return EpisodeRecordRow(
        id=int(row["id"]),
        idempotency_key=str(row["idempotency_key"]),
        episode_id=int(row["episode_id"]) if row["episode_id"] is not None else None,
        branch_id=str(row["branch_id"]),
        record_type=str(row["record_type"]),
        actor_role=str(row["actor_role"]),
        visibility=str(row["visibility"]),
        occurred_at_ms=int(row["occurred_at_ms"]),
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        title=str(row["title"]),
        content=str(row["content"]),
        verbatim=str(row["verbatim"]),
        metadata=raw_meta if isinstance(raw_meta, dict) else {},
        ingested_at_ms=int(row["ingested_at_ms"]),
    )


class EpisodeRepo:
    """管理场次目录与 L0 原件书库的幂等写入和中文拆词检索。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def upsert_episode(
        self,
        conn: sqlite3.Connection,
        *,
        branch_id: str,
        external_episode_id: str,
        kind: str = "live",
        title: str = "",
        platform: str = "",
        save_id: Optional[str] = None,
        visibility: str = "public",
        started_at_ms: int,
        ended_at_ms: Optional[int] = None,
        overview: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EpisodeRow:
        """按 (branch_id, external_episode_id) 幂等登记或补齐场次标题与概览。"""
        now = int(self._clock_ms())
        clean_kind = kind if kind in ("live", "chat_window", "game", "browse") else "live"
        conn.execute(
            """
            INSERT INTO episodes (
                branch_id, external_episode_id, kind, title, platform, save_id,
                visibility, started_at_ms, ended_at_ms, overview, metadata_json, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch_id, external_episode_id) DO UPDATE SET
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE episodes.title END,
                platform = CASE WHEN excluded.platform != '' THEN excluded.platform ELSE episodes.platform END,
                save_id = COALESCE(excluded.save_id, episodes.save_id),
                ended_at_ms = COALESCE(excluded.ended_at_ms, episodes.ended_at_ms),
                overview = CASE WHEN excluded.overview != '' THEN excluded.overview ELSE episodes.overview END,
                metadata_json = CASE
                    WHEN excluded.metadata_json != '{}' THEN excluded.metadata_json
                    ELSE episodes.metadata_json
                END,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                branch_id.strip(),
                external_episode_id.strip(),
                clean_kind,
                title.strip(),
                platform.strip(),
                save_id,
                visibility,
                int(started_at_ms),
                int(ended_at_ms) if ended_at_ms is not None else None,
                overview.strip(),
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        row = self.get_by_external(conn, branch_id=branch_id.strip(), external_episode_id=external_episode_id.strip())
        assert row is not None
        return row

    def get_episode(self, conn: sqlite3.Connection, episode_id: int) -> Optional[EpisodeRow]:
        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (int(episode_id),)).fetchone()
        return _row_to_episode(row) if row is not None else None

    def get_by_external(
        self, conn: sqlite3.Connection, *, branch_id: str, external_episode_id: str
    ) -> Optional[EpisodeRow]:
        row = conn.execute(
            "SELECT * FROM episodes WHERE branch_id = ? AND external_episode_id = ?",
            (branch_id, external_episode_id),
        ).fetchone()
        return _row_to_episode(row) if row is not None else None

    def list_recent_episodes(
        self,
        conn: sqlite3.Connection,
        *,
        visibilities: Optional[Sequence[str]] = None,
        branch_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[EpisodeRow]:
        """按开始时刻倒序查询最近场次（供刚下播状态与看板展示）。"""
        clauses: List[str] = []
        params: List[Any] = []
        if visibilities is not None:
            vis_list = list(visibilities)
            if not vis_list:
                return []
            clauses.append(f"visibility IN ({','.join('?' for _ in vis_list)})")
            params.extend(vis_list)
        if branch_id:
            clauses.append("branch_id = ?")
            params.append(branch_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM episodes {where_sql} ORDER BY started_at_ms DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [_row_to_episode(r) for r in rows]

    def ingest_records(
        self,
        conn: sqlite3.Connection,
        records: Sequence[NewEpisodeRecord],
    ) -> IngestResult:
        """按 (branch_id, idempotency_key) 幂等批量写入 L0 原件记录。"""
        inserted: List[int] = []
        dedup = 0
        now = int(self._clock_ms())
        for rec in records:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO episode_records (
                    idempotency_key, episode_id, branch_id, record_type, actor_role,
                    visibility, occurred_at_ms, save_id, title, content, verbatim,
                    metadata_json, ingested_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.idempotency_key,
                    int(rec.episode_id) if rec.episode_id is not None else None,
                    rec.branch_id,
                    rec.record_type,
                    rec.actor_role,
                    rec.visibility,
                    int(rec.occurred_at_ms),
                    rec.save_id,
                    rec.title.strip(),
                    rec.content.strip(),
                    rec.verbatim.strip(),
                    json.dumps(rec.metadata, ensure_ascii=False),
                    now,
                ),
            )
            if cur.rowcount == 0 or cur.lastrowid is None:
                dedup += 1
            else:
                inserted.append(int(cur.lastrowid))
        conn.commit()
        return IngestResult(inserted_ids=inserted, deduplicated_count=dedup)

    def get_record_by_id(self, conn: sqlite3.Connection, record_id: int) -> Optional[EpisodeRecordRow]:
        row = conn.execute("SELECT * FROM episode_records WHERE id = ?", (int(record_id),)).fetchone()
        return _row_to_record(row) if row is not None else None

    def search_records(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        since_ms: int,
        visibilities: Sequence[str],
        limit: int = 12,
    ) -> List[EpisodeRecordRow]:
        """在 L0 原件书库执行 unicode61 + trigram + 拆词 LIKE 三路检索，按实词命中数排序。"""
        vis_list = list(visibilities)
        if not vis_list or not query.strip():
            return []
        vis_ph = ",".join("?" for _ in vis_list)
        terms = extract_query_terms(query)
        found: Dict[int, EpisodeRecordRow] = {}

        for table, expr in (
            ("episode_records_fts", build_fts_match(query)),
            ("episode_records_trigram", build_trigram_match(query)),
        ):
            if not expr:
                continue
            try:
                rows = conn.execute(
                    f"""
                    SELECT r.* FROM {table} f
                    JOIN episode_records r ON r.id = f.rowid
                    WHERE {table} MATCH ?
                      AND r.occurred_at_ms >= ?
                      AND r.visibility IN ({vis_ph})
                    ORDER BY rank, r.occurred_at_ms DESC
                    LIMIT ?
                    """,
                    (expr, int(since_ms), *vis_list, max(1, int(limit) * 2)),
                ).fetchall()
                for row in rows:
                    rec = _row_to_record(row)
                    found.setdefault(rec.id, rec)
            except sqlite3.OperationalError:
                continue

        # 拆词 LIKE 兜底：覆盖 2 字短词（如“袭击”“钻石”“蒲公英”）的精确 grep
        like_terms = [t for t in terms if len(t) >= 2][:6]
        if like_terms:
            or_clauses = " OR ".join("(title LIKE ? OR content LIKE ? OR verbatim LIKE ?)" for _ in like_terms)
            like_params: List[Any] = []
            for tok in like_terms:
                pat = f"%{tok}%"
                like_params.extend((pat, pat, pat))
            rows = conn.execute(
                f"""
                SELECT * FROM episode_records
                WHERE occurred_at_ms >= ?
                  AND visibility IN ({vis_ph})
                  AND ({or_clauses})
                ORDER BY occurred_at_ms DESC
                LIMIT ?
                """,
                (int(since_ms), *vis_list, *like_params, max(1, int(limit) * 2)),
            ).fetchall()
            for row in rows:
                rec = _row_to_record(row)
                found.setdefault(rec.id, rec)

        scored: List[Tuple[int, int, EpisodeRecordRow]] = []
        for rec in found.values():
            combined = f"{rec.title} {rec.content} {rec.verbatim}"
            hits = count_term_hits(combined, terms)
            scored.append((hits, rec.occurred_at_ms, rec))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[: max(1, int(limit))]]

    def search_episodes(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        since_ms: int,
        visibilities: Sequence[str],
        limit: int = 6,
    ) -> List[EpisodeRow]:
        """按拆词在场次标题与概览中检索，直接定位某一场直播或活动。"""
        vis_list = list(visibilities)
        terms = [t for t in extract_query_terms(query) if len(t) >= 2][:6]
        if not vis_list or not terms:
            return []
        vis_ph = ",".join("?" for _ in vis_list)
        or_clauses = " OR ".join("(title LIKE ? OR overview LIKE ?)" for _ in terms)
        params: List[Any] = []
        for tok in terms:
            pat = f"%{tok}%"
            params.extend((pat, pat))
        rows = conn.execute(
            f"""
            SELECT * FROM episodes
            WHERE started_at_ms >= ?
              AND visibility IN ({vis_ph})
              AND overview != ''
              AND ({or_clauses})
            ORDER BY started_at_ms DESC
            LIMIT ?
            """,
            (int(since_ms), *vis_list, *params, max(1, int(limit) * 2)),
        ).fetchall()
        scored = [
            (count_term_hits(f"{r['title']} {r['overview']}", terms), int(r["started_at_ms"]), _row_to_episode(r))
            for r in rows
        ]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[: max(1, int(limit))]]
