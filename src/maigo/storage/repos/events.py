"""原始经历事件仓储（experience_events + experience_events_fts）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from maigo.runtime import ClockMs, now_ms
from maigo.storage.models import ActorRef, ExperienceEventRow, NewExperienceEvent


@dataclass(frozen=True)
class IngestResult:
    """事件批量入库统计结果。"""

    inserted_ids: List[int]
    deduplicated_count: int


def _row_to_event(row: sqlite3.Row) -> ExperienceEventRow:
    """将数据库行反序列化为强类型事件记录。"""
    raw_actors = json.loads(row["actors_json"] or "[]")
    actors = [
        ActorRef(
            platform=str(item.get("platform", "")).strip(),
            external_id=str(item.get("external_id", "")).strip(),
            display_name=str(item.get("display_name", "")).strip(),
        )
        for item in raw_actors
        if isinstance(item, dict)
    ]
    raw_meta = json.loads(row["metadata_json"] or "{}")
    metadata: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    return ExperienceEventRow(
        id=int(row["id"]),
        idempotency_key=str(row["idempotency_key"]),
        branch_id=str(row["branch_id"]),
        occurred_at_ms=int(row["occurred_at_ms"]),
        ingested_at_ms=int(row["ingested_at_ms"]),
        kind=str(row["kind"]),
        actor_role=str(row["actor_role"]),
        visibility=str(row["visibility"]),
        context_scope=str(row["context_scope"]),
        actors=actors,
        summary=str(row["summary"]),
        verbatim=str(row["verbatim"]),
        reaction=str(row["reaction"]),
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        metadata=metadata,
        synthesized_at_ms=int(row["synthesized_at_ms"]) if row["synthesized_at_ms"] is not None else None,
    )


def _trigram_query(query: str) -> str:
    """为至少包含一个 3 字符词段的查询构造 FTS5 trigram 短语表达，提升中文局部匹配。"""
    tokens = [tok.replace('"', "").strip() for tok in query.split()]
    valid = [f'"{tok}"' for tok in tokens if len(tok) >= 3]
    if valid:
        return " OR ".join(valid)
    cleaned = query.replace('"', "").strip()
    return f'"{cleaned}"' if len(cleaned) >= 3 else ""


def _query_fts_table(
    conn: sqlite3.Connection,
    *,
    fts_table: str,
    match_expr: str,
    since_ms: int,
    vis_list: Sequence[str],
    limit: int,
) -> List[ExperienceEventRow]:
    if not match_expr:
        return []
    vis_placeholders = ",".join("?" for _ in vis_list)
    try:
        rows = conn.execute(
            f"""
            SELECT e.* FROM {fts_table} f
            JOIN experience_events e ON e.id = f.rowid
            WHERE {fts_table} MATCH ?
              AND e.occurred_at_ms >= ?
              AND e.visibility IN ({vis_placeholders})
            ORDER BY rank, e.occurred_at_ms DESC
            LIMIT ?
            """,
            (match_expr, int(since_ms), *vis_list, max(1, int(limit))),
        ).fetchall()
        return [_row_to_event(row) for row in rows]
    except sqlite3.OperationalError:
        return []


class ExperienceEventRepo:
    """负责 ``experience_events`` 的幂等写入、查询与全文检索。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def ingest_batch(
        self,
        conn: sqlite3.Connection,
        events: Sequence[NewExperienceEvent],
    ) -> IngestResult:
        """按 ``(branch_id, idempotency_key)`` 幂等写入事件批，返回新增主键与去重数。"""
        inserted_ids: List[int] = []
        deduplicated = 0
        ingested_at = int(self._clock_ms())

        for item in events:
            actors_payload = [
                {
                    "platform": actor.platform.strip(),
                    "external_id": actor.external_id.strip(),
                    "display_name": actor.display_name.strip(),
                }
                for actor in item.actors
            ]
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO experience_events (
                    idempotency_key, branch_id, occurred_at_ms, ingested_at_ms,
                    kind, actor_role, visibility, context_scope, actors_json,
                    summary, verbatim, reaction, save_id, metadata_json, synthesized_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    item.idempotency_key,
                    item.branch_id,
                    int(item.occurred_at_ms),
                    ingested_at,
                    item.kind,
                    item.actor_role,
                    item.visibility,
                    item.context_scope,
                    json.dumps(actors_payload, ensure_ascii=False),
                    item.summary,
                    item.verbatim,
                    item.reaction,
                    item.save_id,
                    json.dumps(item.metadata, ensure_ascii=False),
                ),
            )
            if cursor.rowcount == 0 or cursor.lastrowid is None:
                deduplicated += 1
            else:
                inserted_ids.append(int(cursor.lastrowid))
        conn.commit()
        return IngestResult(inserted_ids=inserted_ids, deduplicated_count=deduplicated)

    def get_by_id(self, conn: sqlite3.Connection, event_id: int) -> Optional[ExperienceEventRow]:
        """按主键查询单条事件。"""
        row = conn.execute("SELECT * FROM experience_events WHERE id = ?", (int(event_id),)).fetchone()
        return _row_to_event(row) if row is not None else None

    def count_unsynthesized(self, conn: sqlite3.Connection) -> int:
        """统计尚未进入编年合成的事件条数。"""
        row = conn.execute("SELECT COUNT(*) AS cnt FROM experience_events WHERE synthesized_at_ms IS NULL").fetchone()
        return int(row["cnt"]) if row is not None else 0

    def list_unsynthesized(self, conn: sqlite3.Connection, *, limit: int = 200) -> List[ExperienceEventRow]:
        """按发生时刻正序读取尚未整理的事件批。"""
        rows = conn.execute(
            """
            SELECT * FROM experience_events
            WHERE synthesized_at_ms IS NULL
            ORDER BY occurred_at_ms ASC, id ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def mark_synthesized(
        self,
        conn: sqlite3.Connection,
        event_ids: Sequence[int],
        *,
        synthesized_at_ms: Optional[int] = None,
    ) -> int:
        """批量标记事件已完成编年整理。"""
        if not event_ids:
            return 0
        stamp = int(self._clock_ms()) if synthesized_at_ms is None else int(synthesized_at_ms)
        placeholders = ",".join("?" for _ in event_ids)
        cursor = conn.execute(
            f"UPDATE experience_events SET synthesized_at_ms = ? WHERE id IN ({placeholders})",
            (stamp, *(int(eid) for eid in event_ids)),
        )
        conn.commit()
        return int(cursor.rowcount)

    def list_recent(
        self,
        conn: sqlite3.Connection,
        *,
        since_ms: Optional[int] = None,
        visibilities: Optional[Sequence[str]] = None,
        branch_id: Optional[str] = None,
        save_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[ExperienceEventRow]:
        """按时间倒序查询近期事件，支持可见性、分支与存档过滤。"""
        clauses: List[str] = []
        params: List[Any] = []
        if since_ms is not None:
            clauses.append("occurred_at_ms >= ?")
            params.append(int(since_ms))
        if visibilities is not None:
            vis_list = list(visibilities)
            if not vis_list:
                return []
            placeholders = ",".join("?" for _ in vis_list)
            clauses.append(f"visibility IN ({placeholders})")
            params.extend(vis_list)
        if branch_id:
            clauses.append("branch_id = ?")
            params.append(branch_id)
        if save_id:
            clauses.append("save_id = ?")
            params.append(save_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM experience_events {where_sql} ORDER BY occurred_at_ms DESC, id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def list_range(
        self,
        conn: sqlite3.Connection,
        *,
        start_ms: int,
        end_ms: int,
        visibilities: Optional[Sequence[str]] = None,
        limit: int = 500,
    ) -> List[ExperienceEventRow]:
        """按时间区间正序读取事件，供按时段整理或时间线展示。"""
        clauses = ["occurred_at_ms >= ?", "occurred_at_ms < ?"]
        params: List[Any] = [int(start_ms), int(end_ms)]
        if visibilities is not None:
            vis_list = list(visibilities)
            if not vis_list:
                return []
            placeholders = ",".join("?" for _ in vis_list)
            clauses.append(f"visibility IN ({placeholders})")
            params.extend(vis_list)
        rows = conn.execute(
            f"""
            SELECT * FROM experience_events
            WHERE {" AND ".join(clauses)}
            ORDER BY occurred_at_ms ASC, id ASC
            LIMIT ?
            """,
            (*params, max(1, int(limit))),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def search(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        since_ms: int,
        visibilities: Sequence[str],
        limit: int = 10,
    ) -> List[ExperienceEventRow]:
        """混合 FTS5 与中文子串兜底检索事件，按相关度与新近度去重返回。"""
        vis_list = list(visibilities)
        if not vis_list:
            return []
        vis_placeholders = ",".join("?" for _ in vis_list)
        stripped = query.strip()
        if not stripped:
            return self.list_recent(conn, since_ms=since_ms, visibilities=vis_list, limit=limit)

        results: Dict[int, Tuple[int, ExperienceEventRow]] = {}
        cleaned = stripped.replace('"', " ").strip()
        fts_query = f'"{cleaned}"' if cleaned else ""
        rank_cursor = 0

        for ev in _query_fts_table(
            conn,
            fts_table="experience_events_fts",
            match_expr=fts_query,
            since_ms=since_ms,
            vis_list=vis_list,
            limit=limit,
        ):
            if ev.id not in results:
                results[ev.id] = (rank_cursor, ev)
                rank_cursor += 1

        # 补一层 trigram 中文任意子串召回（3 字及以上连续词段命中）
        for ev in _query_fts_table(
            conn,
            fts_table="experience_events_trigram",
            match_expr=_trigram_query(stripped),
            since_ms=since_ms,
            vis_list=vis_list,
            limit=limit,
        ):
            if ev.id not in results:
                results[ev.id] = (rank_cursor, ev)
                rank_cursor += 1

        # 补一层 LIKE 子串精确召回（兜底 1~2 字超短中文词）
        like_pattern = f"%{stripped}%"
        like_rows = conn.execute(
            f"""
            SELECT * FROM experience_events
            WHERE occurred_at_ms >= ?
              AND visibility IN ({vis_placeholders})
              AND (summary LIKE ? OR verbatim LIKE ? OR reaction LIKE ? OR context_scope LIKE ?)
            ORDER BY occurred_at_ms DESC, id DESC
            LIMIT ?
            """,
            (int(since_ms), *vis_list, like_pattern, like_pattern, like_pattern, like_pattern, max(1, int(limit))),
        ).fetchall()
        for row in like_rows:
            ev = _row_to_event(row)
            if ev.id not in results:
                results[ev.id] = (rank_cursor, ev)
                rank_cursor += 1

        ordered = sorted(results.values(), key=lambda pair: (pair[0], -pair[1].occurred_at_ms))
        return [pair[1] for pair in ordered[: max(1, int(limit))]]

    def scrub_actor_name(self, conn: sqlite3.Connection, *, platform: str, external_id: str, replacement: str) -> int:
        """执行遗忘时，将事件 ``actors_json`` 中命中该外部账号的显示名抹去。"""
        rows = conn.execute("SELECT id, actors_json FROM experience_events WHERE actors_json != '[]'").fetchall()
        updated = 0
        for row in rows:
            raw = json.loads(row["actors_json"] or "[]")
            changed = False
            for actor in raw:
                if (
                    isinstance(actor, dict)
                    and str(actor.get("platform", "")) == platform
                    and str(actor.get("external_id", "")) == external_id
                ):
                    actor["display_name"] = replacement
                    actor["external_id"] = "redacted"
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE experience_events SET actors_json = ? WHERE id = ?",
                    (json.dumps(raw, ensure_ascii=False), int(row["id"])),
                )
                updated += 1
        if updated:
            conn.commit()
        return updated
