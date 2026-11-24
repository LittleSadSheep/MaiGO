"""语义向量本地持久化仓储（memory_embeddings）。"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from maigo.runtime import ClockMs, now_ms
from maigo.storage.models import EmbeddingRow


def _row_to_embedding(row: sqlite3.Row) -> EmbeddingRow:
    return EmbeddingRow(
        id=int(row["id"]),
        item_type=str(row["item_type"]),
        item_id=int(row["item_id"]),
        model=str(row["model"]),
        dims=int(row["dims"]),
        vector_blob=bytes(row["vector_blob"]),
        content_hash=str(row["content_hash"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


class EmbeddingRepo:
    """将可选语义向量以小端 float32 BLOB 存入单机 SQLite，支持内容指纹幂等更新。"""

    def __init__(self, *, clock_ms: ClockMs = now_ms) -> None:
        self._clock_ms = clock_ms

    def get_hash(
        self,
        conn: sqlite3.Connection,
        *,
        item_type: str,
        item_id: int,
        model: str,
    ) -> Optional[str]:
        """查询当前模型下已有向量的内容指纹，未变时可跳过重复向量化请求。"""
        row = conn.execute(
            "SELECT content_hash FROM memory_embeddings WHERE item_type = ? AND item_id = ? AND model = ?",
            (item_type, int(item_id), model),
        ).fetchone()
        return str(row["content_hash"]) if row is not None else None

    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        item_type: str,
        item_id: int,
        model: str,
        dims: int,
        vector_blob: bytes,
        content_hash: str,
    ) -> None:
        """幂等写入或刷新某条记忆的语义向量。"""
        now = int(self._clock_ms())
        conn.execute(
            """
            INSERT INTO memory_embeddings (
                item_type, item_id, model, dims, vector_blob, content_hash, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_type, item_id, model) DO UPDATE SET
                dims = excluded.dims,
                vector_blob = excluded.vector_blob,
                content_hash = excluded.content_hash,
                updated_at_ms = excluded.updated_at_ms
            """,
            (item_type, int(item_id), model, int(dims), vector_blob, content_hash, now),
        )
        conn.commit()

    def list_by_model(self, conn: sqlite3.Connection, *, model: str, limit: int = 2000) -> List[EmbeddingRow]:
        """按模型列出最近写入的向量（供单机混合检索做内存余弦排序）。"""
        rows = conn.execute(
            "SELECT * FROM memory_embeddings WHERE model = ? ORDER BY updated_at_ms DESC LIMIT ?",
            (model, max(1, int(limit))),
        ).fetchall()
        return [_row_to_embedding(row) for row in rows]

    def delete_for_item(self, conn: sqlite3.Connection, *, item_type: str, item_id: int) -> int:
        """删除被抹去或删除条目的全部关联向量。"""
        cursor = conn.execute(
            "DELETE FROM memory_embeddings WHERE item_type = ? AND item_id = ?",
            (item_type, int(item_id)),
        )
        conn.commit()
        return int(cursor.rowcount)

    def count_all(self, conn: sqlite3.Connection) -> int:
        """统计当前存储的向量总数。"""
        row = conn.execute("SELECT COUNT(*) AS cnt FROM memory_embeddings").fetchone()
        return int(row["cnt"]) if row is not None else 0
