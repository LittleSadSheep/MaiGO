"""L2 实体与关系图谱仓储（knowledge_entities + knowledge_relations）。

设计原因：借鉴实体关系分层思想，把玩家互动、社交事实、宠物同伴、地点与项目关系
沉淀到独立实体图谱层，一方面彻底停止用 greet/gift 噪音污染日内时间线，另一方面
让人物卡片与跨端检索都能精确回答“谁和谁发生了什么关系”。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from maigo.core.models import KnowledgeEntityRow, KnowledgeRelationRow, NewKnowledgeRelation
from maigo.runtime import ClockMs, now_ms
from maigo.storage.repos._text import build_trigram_match, count_term_hits, extract_query_terms


def _row_to_entity(row: sqlite3.Row) -> KnowledgeEntityRow:
    raw_aliases = json.loads(row["aliases_json"] or "[]")
    raw_attrs = json.loads(row["attributes_json"] or "{}")
    return KnowledgeEntityRow(
        id=int(row["id"]),
        entity_type=str(row["entity_type"]),
        name=str(row["name"]),
        canonical_key=str(row["canonical_key"]),
        person_id=int(row["person_id"]) if row["person_id"] is not None else None,
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        visibility=str(row["visibility"]),
        summary=str(row["summary"]),
        aliases=[str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else [],
        attributes=raw_attrs if isinstance(raw_attrs, dict) else {},
        first_seen_ms=int(row["first_seen_ms"]),
        last_seen_ms=int(row["last_seen_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _row_to_relation(row: sqlite3.Row) -> KnowledgeRelationRow:
    keys = row.keys()
    return KnowledgeRelationRow(
        id=int(row["id"]),
        idempotency_key=str(row["idempotency_key"]),
        branch_id=str(row["branch_id"]),
        subject_entity_id=int(row["subject_entity_id"]),
        predicate=str(row["predicate"]),
        object_entity_id=int(row["object_entity_id"]) if row["object_entity_id"] is not None else None,
        object_text=str(row["object_text"]),
        fact_text=str(row["fact_text"]),
        quote=str(row["quote"]),
        visibility=str(row["visibility"]),
        save_id=str(row["save_id"]) if row["save_id"] is not None else None,
        episode_id=int(row["episode_id"]) if row["episode_id"] is not None else None,
        source_event_id=int(row["source_event_id"]) if row["source_event_id"] is not None else None,
        confidence=float(row["confidence"]),
        status=str(row["status"]),
        occurred_at_ms=int(row["occurred_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        subject_name=str(row["subject_name"]) if "subject_name" in keys and row["subject_name"] else "",
        object_name=str(row["object_name"]) if "object_name" in keys and row["object_name"] else "",
    )


class KnowledgeGraphRepo:
    """管理实体节点与关系事实边的幂等写入、人物关联投影与全文检索。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def upsert_entity(
        self,
        conn: sqlite3.Connection,
        *,
        entity_type: str,
        canonical_key: str,
        name: str,
        person_id: Optional[int] = None,
        save_id: Optional[str] = None,
        visibility: str = "public",
        summary: str = "",
        aliases: Sequence[str] = (),
        attributes: Optional[Dict[str, Any]] = None,
        seen_at_ms: Optional[int] = None,
    ) -> KnowledgeEntityRow:
        """按 (entity_type, canonical_key) 幂等登记实体，合并别名、属性与最近活跃时刻。"""
        now = int(self._clock_ms())
        seen = int(seen_at_ms) if seen_at_ms is not None else now
        valid_type = (
            entity_type
            if entity_type in ("person", "player", "creature", "place", "item", "project", "concept")
            else "concept"
        )
        key = canonical_key.strip().lower()
        clean_name = name.strip() or key
        existing = self.find_entity(conn, entity_type=valid_type, canonical_key=key)
        merged_aliases = list(dict.fromkeys([*(existing.aliases if existing else ()), *(a.strip() for a in aliases if a.strip())]))
        merged_attrs: Dict[str, Any] = dict(existing.attributes) if existing else {}
        if attributes:
            merged_attrs.update(attributes)
        conn.execute(
            """
            INSERT INTO knowledge_entities (
                entity_type, name, canonical_key, person_id, save_id, visibility,
                summary, aliases_json, attributes_json, first_seen_ms, last_seen_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, canonical_key) DO UPDATE SET
                name = CASE WHEN excluded.name != '' THEN excluded.name ELSE knowledge_entities.name END,
                person_id = COALESCE(excluded.person_id, knowledge_entities.person_id),
                save_id = COALESCE(excluded.save_id, knowledge_entities.save_id),
                summary = CASE WHEN excluded.summary != '' THEN excluded.summary ELSE knowledge_entities.summary END,
                aliases_json = excluded.aliases_json,
                attributes_json = excluded.attributes_json,
                last_seen_ms = MAX(knowledge_entities.last_seen_ms, excluded.last_seen_ms),
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                valid_type,
                clean_name,
                key,
                int(person_id) if person_id is not None else None,
                save_id,
                visibility,
                summary.strip(),
                json.dumps(merged_aliases, ensure_ascii=False),
                json.dumps(merged_attrs, ensure_ascii=False),
                seen,
                seen,
                now,
            ),
        )
        conn.commit()
        saved = self.find_entity(conn, entity_type=valid_type, canonical_key=key)
        assert saved is not None
        return saved

    def find_entity(
        self, conn: sqlite3.Connection, *, entity_type: str, canonical_key: str
    ) -> Optional[KnowledgeEntityRow]:
        row = conn.execute(
            "SELECT * FROM knowledge_entities WHERE entity_type = ? AND canonical_key = ?",
            (entity_type, canonical_key.strip().lower()),
        ).fetchone()
        return _row_to_entity(row) if row is not None else None

    def upsert_relation(
        self,
        conn: sqlite3.Connection,
        relation: NewKnowledgeRelation,
    ) -> Tuple[int, bool]:
        """按 (branch_id, idempotency_key) 幂等写入关系边，返回 (relation_id, created)。"""
        now = int(self._clock_ms())
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_relations (
                idempotency_key, branch_id, subject_entity_id, predicate, object_entity_id,
                object_text, fact_text, quote, visibility, save_id, episode_id,
                source_event_id, confidence, status, occurred_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation.idempotency_key,
                relation.branch_id,
                int(relation.subject_entity_id),
                relation.predicate.strip() or "related_to",
                int(relation.object_entity_id) if relation.object_entity_id is not None else None,
                relation.object_text.strip(),
                relation.fact_text.strip(),
                relation.quote.strip(),
                relation.visibility,
                relation.save_id,
                int(relation.episode_id) if relation.episode_id is not None else None,
                int(relation.source_event_id) if relation.source_event_id is not None else None,
                float(relation.confidence),
                relation.status if relation.status in ("active", "superseded") else "active",
                int(relation.occurred_at_ms),
                now,
            ),
        )
        conn.commit()
        if cur.rowcount > 0 and cur.lastrowid is not None:
            return int(cur.lastrowid), True
        existing = conn.execute(
            "SELECT id FROM knowledge_relations WHERE branch_id = ? AND idempotency_key = ?",
            (relation.branch_id, relation.idempotency_key),
        ).fetchone()
        return (int(existing["id"]) if existing is not None else 0), False

    def get_relation_by_id(self, conn: sqlite3.Connection, relation_id: int) -> Optional[KnowledgeRelationRow]:
        row = conn.execute(
            """
            SELECT r.*, s.name AS subject_name, COALESCE(o.name, '') AS object_name
            FROM knowledge_relations r
            JOIN knowledge_entities s ON s.id = r.subject_entity_id
            LEFT JOIN knowledge_entities o ON o.id = r.object_entity_id
            WHERE r.id = ?
            """,
            (int(relation_id),),
        ).fetchone()
        return _row_to_relation(row) if row is not None else None

    def list_relations_for_person(
        self,
        conn: sqlite3.Connection,
        *,
        person_id: int,
        visibilities: Sequence[str],
        limit: int = 5,
    ) -> List[KnowledgeRelationRow]:
        """列出与指定跨端人物绑定实体相关的现行关系事实，供人物卡片投影。"""
        vis_list = list(visibilities)
        if not vis_list:
            return []
        vis_ph = ",".join("?" for _ in vis_list)
        rows = conn.execute(
            f"""
            SELECT r.*, s.name AS subject_name, COALESCE(o.name, '') AS object_name
            FROM knowledge_relations r
            JOIN knowledge_entities s ON s.id = r.subject_entity_id
            LEFT JOIN knowledge_entities o ON o.id = r.object_entity_id
            WHERE r.status = 'active'
              AND r.visibility IN ({vis_ph})
              AND (s.person_id = ? OR o.person_id = ?)
            ORDER BY r.occurred_at_ms DESC, r.id DESC
            LIMIT ?
            """,
            (*vis_list, int(person_id), int(person_id), max(1, int(limit))),
        ).fetchall()
        return [_row_to_relation(r) for r in rows]

    def search_relations(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        since_ms: int,
        visibilities: Sequence[str],
        limit: int = 10,
    ) -> List[KnowledgeRelationRow]:
        """按实体名与关系事实做 trigram + 拆词 LIKE 混合检索。"""
        vis_list = list(visibilities)
        if not vis_list or not query.strip():
            return []
        vis_ph = ",".join("?" for _ in vis_list)
        terms = extract_query_terms(query)
        found: Dict[int, KnowledgeRelationRow] = {}

        tri_expr = build_trigram_match(query)
        if tri_expr:
            try:
                rows = conn.execute(
                    f"""
                    SELECT r.*, s.name AS subject_name, COALESCE(o.name, '') AS object_name
                    FROM knowledge_relations_fts f
                    JOIN knowledge_relations r ON r.id = f.rowid
                    JOIN knowledge_entities s ON s.id = r.subject_entity_id
                    LEFT JOIN knowledge_entities o ON o.id = r.object_entity_id
                    WHERE knowledge_relations_fts MATCH ?
                      AND r.status = 'active'
                      AND r.occurred_at_ms >= ?
                      AND r.visibility IN ({vis_ph})
                    ORDER BY rank, r.occurred_at_ms DESC
                    LIMIT ?
                    """,
                    (tri_expr, int(since_ms), *vis_list, max(1, int(limit) * 2)),
                ).fetchall()
                for row in rows:
                    rel = _row_to_relation(row)
                    found.setdefault(rel.id, rel)
            except sqlite3.OperationalError:
                pass

        like_terms = [t for t in terms if len(t) >= 2][:6]
        if like_terms:
            or_clauses = " OR ".join(
                "(s.name LIKE ? OR COALESCE(o.name, '') LIKE ? OR r.object_text LIKE ? OR r.fact_text LIKE ?)"
                for _ in like_terms
            )
            params: List[Any] = []
            for tok in like_terms:
                pat = f"%{tok}%"
                params.extend((pat, pat, pat, pat))
            rows = conn.execute(
                f"""
                SELECT r.*, s.name AS subject_name, COALESCE(o.name, '') AS object_name
                FROM knowledge_relations r
                JOIN knowledge_entities s ON s.id = r.subject_entity_id
                LEFT JOIN knowledge_entities o ON o.id = r.object_entity_id
                WHERE r.status = 'active'
                  AND r.occurred_at_ms >= ?
                  AND r.visibility IN ({vis_ph})
                  AND ({or_clauses})
                ORDER BY r.occurred_at_ms DESC
                LIMIT ?
                """,
                (int(since_ms), *vis_list, *params, max(1, int(limit) * 2)),
            ).fetchall()
            for row in rows:
                rel = _row_to_relation(row)
                found.setdefault(rel.id, rel)

        scored: List[Tuple[int, int, KnowledgeRelationRow]] = []
        for rel in found.values():
            combined = f"{rel.subject_name} {rel.object_name} {rel.object_text} {rel.fact_text} {rel.quote}"
            scored.append((count_term_hits(combined, terms), rel.occurred_at_ms, rel))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[: max(1, int(limit))]]

    def clear_relations_for_person(self, conn: sqlite3.Connection, person_id: int) -> int:
        """执行跨端遗忘时清除关联该人物节点的全部关系边。"""
        cur = conn.execute(
            """
            DELETE FROM knowledge_relations
            WHERE subject_entity_id IN (SELECT id FROM knowledge_entities WHERE person_id = ?)
               OR object_entity_id IN (SELECT id FROM knowledge_entities WHERE person_id = ?)
            """,
            (int(person_id), int(person_id)),
        )
        conn.commit()
        return int(cur.rowcount)
