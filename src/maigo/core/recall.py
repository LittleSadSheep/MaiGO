"""双路混合检索服务：SQLite FTS5/LIKE 稀疏召回 + 可选单机语义向量 RRF 融合。"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import math
import sqlite3
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

import httpx

from maigo.config import EmbeddingConfig
from maigo.core.identity import IdentityService
from maigo.core.models import ExperienceEventRow, LifeArcRow, PersonaAnchorRow
from maigo.core.privacy import (
    allowed_visibilities,
    describe_visibility_scope,
    normalize_visibility,
    sanitize_for_model,
)
from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.storage.db import MaiGODatabase
from maigo.storage.repos import EmbeddingRepo, ExperienceEventRepo, LifeArcRepo, PersonaAnchorRepo, PersonRepo

logger = get_logger("services.retrieval")

_RRF_K = 60.0
_MIN_COSINE_SCORE = 0.25
_DAY_MS = 24 * 3600 * 1000


class EmbeddingClient(Protocol):
    """可替换的文本向量化客户端协议（便于离线单测注入确定性向量）。"""

    @property
    def model_name(self) -> str: ...

    async def embed_texts(self, texts: Sequence[str]) -> Optional[List[List[float]]]: ...


class OpenAIEmbeddingClient:
    """基于 OpenAI 兼容 ``/embeddings`` 接口的异步向量化客户端。"""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_ms: int = 10_000) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model.strip()
        self._timeout_s = max(0.5, timeout_ms / 1000.0)

    @property
    def model_name(self) -> str:
        return self._model

    async def embed_texts(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        """批量向量化文本；远端不可达或返回异常时记录告警并返回 None 降级纯稀疏检索。"""
        if not self._base_url or not self._model or not texts:
            return None
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {"model": self._model, "input": list(texts)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(f"{self._base_url}/embeddings", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json().get("data") or []
            ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
            vectors = [[float(x) for x in (item.get("embedding") or [])] for item in ordered]
            return vectors if len(vectors) == len(texts) and all(vectors) else None
        except Exception as exc:
            logger.warning(f"调用外部 Embedding 服务失败，自动退回纯稀疏检索: {exc}")
            return None


def _pack_vector(vec: Sequence[float]) -> bytes:
    arr = array("f", (float(x) for x in vec))
    return arr.tobytes()


def _unpack_vector(blob: bytes) -> List[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return dot / denom if denom > 1e-12 else 0.0


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _branch_label(branch_id: str) -> str:
    clean = branch_id.strip().lower()
    mapping = {
        "maibot": "QQ群",
        "amaidesu": "直播间",
        "bilibili_ops": "B站",
        "x_ops": "X",
        "rss_collector": "订阅浏览",
        "daily_feeder": "日常浏览",
    }
    if clean in mapping:
        return mapping[clean]
    if "bili" in clean:
        return "B站"
    if "maibot" in clean or "qq" in clean:
        return "群聊"
    return branch_id


@dataclass
class _CandidateItem:
    key: Tuple[str, int]
    text: str
    occurred_at_ms: int
    weight: float = 1.0
    sparse_rank: Optional[int] = None
    dense_rank: Optional[int] = None


class HybridRecallService:
    """结合稀疏全文、可选语义向量、时间衰减与隐私过滤的跨端记忆检索器。"""

    def __init__(
        self,
        db: MaiGODatabase,
        *,
        identity: IdentityService,
        embedding_config: Optional[EmbeddingConfig] = None,
        embedding_client: Optional[EmbeddingClient] = None,
        clock_ms: ClockMs = now_ms,
    ) -> None:
        self._db = db
        self._identity = identity
        self._config = embedding_config or EmbeddingConfig()
        self._clock_ms = clock_ms
        self._events = ExperienceEventRepo(clock_ms=clock_ms)
        self._anchors = PersonaAnchorRepo(clock_ms=clock_ms)
        self._arcs = LifeArcRepo(clock_ms=clock_ms)
        self._embeddings = EmbeddingRepo(clock_ms=clock_ms)
        self._persons = PersonRepo(clock_ms=clock_ms)

        if embedding_client is not None:
            self._client: Optional[EmbeddingClient] = embedding_client
        elif self._config.base_url.strip() and self._config.model.strip():
            self._client = OpenAIEmbeddingClient(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                model=self._config.model,
                timeout_ms=self._config.timeout_ms,
            )
        else:
            self._client = None

    @property
    def mode(self) -> str:
        """返回当前检索模式（hybrid=稀疏+向量融合 / sparse=纯单机稀疏）。"""
        return "hybrid" if self._client is not None and bool(self._client.model_name.strip()) else "sparse"

    async def index_items(self, items: Sequence[Tuple[str, int, str]]) -> int:
        """为一批 ``(item_type, item_id, text)`` 条目按内容指纹幂等补齐语义向量。"""
        if self._client is None or not items:
            return 0
        model_name = self._client.model_name
        clean_items: List[Tuple[str, int, str, str]] = []
        for item_type, item_id, text in items:
            cleaned = text.strip()
            if not cleaned:
                continue
            clean_items.append((item_type, int(item_id), cleaned, _content_hash(cleaned)))
        if not clean_items:
            return 0

        def _filter_changed(conn: sqlite3.Connection) -> List[Tuple[str, int, str, str]]:
            pending: List[Tuple[str, int, str, str]] = []
            for itype, iid, txt, digest in clean_items:
                if self._embeddings.get_hash(conn, item_type=itype, item_id=iid, model=model_name) != digest:
                    pending.append((itype, iid, txt, digest))
            return pending

        to_embed = await self._db.run(_filter_changed)
        if not to_embed:
            return 0

        vectors = await self._client.embed_texts([entry[2] for entry in to_embed])
        if not vectors or len(vectors) != len(to_embed):
            return 0

        def _persist(conn: sqlite3.Connection) -> int:
            saved = 0
            for (itype, iid, _txt, digest), vec in zip(to_embed, vectors, strict=True):
                if not vec:
                    continue
                self._embeddings.upsert(
                    conn,
                    item_type=itype,
                    item_id=iid,
                    model=model_name,
                    dims=len(vec),
                    vector_blob=_pack_vector(vec),
                    content_hash=digest,
                )
                saved += 1
            return saved

        return await self._db.run(_persist)

    async def recall(
        self,
        query: str,
        *,
        days: int = 7,
        limit: int = 5,
        target_visibility: str = "public",
    ) -> str:
        """执行稀疏+可选语义向量混合召回并输出脱敏、按综合权重排序的回想文本。"""
        vis_level = normalize_visibility(target_visibility, default="public")
        vis_allowed = allowed_visibilities(vis_level)
        now = int(self._clock_ms())
        since_ms = now - max(1, int(days)) * _DAY_MS
        cap = max(1, int(limit))

        def _sparse_collect(conn: sqlite3.Connection) -> tuple[List[str], Dict[Tuple[str, int], _CandidateItem]]:
            all_ext_ids = [item.external_id for item in self._persons.list_all_identities(conn)]
            cands: Dict[Tuple[str, int], _CandidateItem] = {}

            anc_hits = self._anchors.search(conn, query=query, visibilities=vis_allowed, limit=cap * 2)
            ev_hits = self._events.search(conn, query=query, since_ms=since_ms, visibilities=vis_allowed, limit=cap * 2)
            arc_hits = self._arcs.search(conn, query=query, visibilities=vis_allowed, limit=cap)

            rank = 0
            for anc in anc_hits:
                if anc.status == "void":
                    continue
                cands[("anchor", anc.id)] = self._format_anchor(anc, all_ext_ids, sparse_rank=rank)
                rank += 1
            for arc in arc_hits:
                cands[("arc", arc.id)] = self._format_arc(
                    arc, target_visibility=vis_level, all_ext_ids=all_ext_ids, sparse_rank=rank
                )
                rank += 1
            for ev in ev_hits:
                if ev.kind == "forget_request" or self._identity.has_forgotten_actor_sync(conn, ev.actors):
                    continue
                cands[("event", ev.id)] = self._format_event(ev, all_ext_ids, sparse_rank=rank)
                rank += 1
            return all_ext_ids, cands

        all_ext_ids, candidates = await self._db.run(_sparse_collect)

        # 语义向量召回分支（仅在配置模型且成功向量化 query 时执行，失败无感跳过）
        query_vec: Optional[List[float]] = None
        if self._client is not None and query.strip():
            vecs = await self._client.embed_texts([query.strip()])
            if vecs and vecs[0]:
                query_vec = vecs[0]

        if query_vec is not None and self._client is not None:
            model_name = self._client.model_name

            def _dense_collect(
                conn: sqlite3.Connection,
            ) -> List[Tuple[float, Tuple[str, int], Optional[_CandidateItem]]]:
                emb_rows = self._embeddings.list_by_model(conn, model=model_name, limit=2000)
                scored: List[Tuple[float, Tuple[str, int]]] = []
                for erow in emb_rows:
                    sim = _cosine_similarity(query_vec, _unpack_vector(erow.vector_blob))
                    if sim >= _MIN_COSINE_SCORE:
                        scored.append((sim, (erow.item_type, erow.item_id)))
                scored.sort(key=lambda item: item[0], reverse=True)

                resolved: List[Tuple[float, Tuple[str, int], Optional[_CandidateItem]]] = []
                for sim, (itype, iid) in scored[: cap * 4]:
                    key = (itype, iid)
                    if key in candidates:
                        resolved.append((sim, key, None))
                        continue
                    item = self._load_dense_item(
                        conn,
                        item_type=itype,
                        item_id=iid,
                        since_ms=since_ms,
                        vis_allowed=vis_allowed,
                        target_visibility=vis_level,
                        all_ext_ids=all_ext_ids,
                    )
                    if item is not None:
                        resolved.append((sim, key, item))
                return resolved

            dense_hits = await self._db.run(_dense_collect)
            for dense_idx, (_sim, key, extra_item) in enumerate(dense_hits):
                if key in candidates:
                    candidates[key].dense_rank = dense_idx
                elif extra_item is not None:
                    extra_item.dense_rank = dense_idx
                    candidates[key] = extra_item

        if not candidates:
            return f"最近 {days} 天里没有找到与「{query}」相关的跨端记录。"

        def _final_score(item: _CandidateItem) -> float:
            rrf = 0.0
            if item.sparse_rank is not None:
                rrf += 1.0 / (_RRF_K + item.sparse_rank + 1)
            if item.dense_rank is not None:
                rrf += 1.0 / (_RRF_K + item.dense_rank + 1)
            age_days = max(0.0, (now - item.occurred_at_ms) / float(_DAY_MS))
            decay = 1.0 / (1.0 + age_days / 14.0)
            return rrf * decay * item.weight

        ranked = sorted(candidates.values(), key=lambda c: (_final_score(c), c.occurred_at_ms), reverse=True)
        return "\n".join(c.text for c in ranked[:cap])

    def _load_dense_item(
        self,
        conn: sqlite3.Connection,
        *,
        item_type: str,
        item_id: int,
        since_ms: int,
        vis_allowed: Sequence[str],
        target_visibility: str,
        all_ext_ids: Sequence[str],
    ) -> Optional[_CandidateItem]:
        if item_type == "event":
            ev = self._events.get_by_id(conn, item_id)
            if (
                ev is None
                or ev.visibility not in vis_allowed
                or ev.occurred_at_ms < since_ms
                or ev.kind == "forget_request"
                or self._identity.has_forgotten_actor_sync(conn, ev.actors)
            ):
                return None
            return self._format_event(ev, all_ext_ids)
        if item_type == "anchor":
            anc = self._anchors.get_by_id(conn, item_id)
            if anc is None or anc.visibility not in vis_allowed or anc.status == "void":
                return None
            return self._format_anchor(anc, all_ext_ids)
        if item_type == "arc":
            arc = self._arcs.get_by_id(conn, item_id)
            if arc is None or arc.visibility not in vis_allowed or arc.last_event_at_ms < since_ms:
                return None
            return self._format_arc(arc, target_visibility=target_visibility, all_ext_ids=all_ext_ids)
        return None

    def _format_anchor(
        self,
        anc: PersonaAnchorRow,
        all_ext_ids: Sequence[str],
        *,
        sparse_rank: Optional[int] = None,
    ) -> _CandidateItem:
        status_map = {
            "closed": "已兑现",
            "void": "已作废",
            "superseded": "已演变",
            "stale": "久远待定",
        }
        status_word = status_map.get(anc.status, "进行中")
        clean_c = sanitize_for_model(anc.content, all_ext_ids)
        return _CandidateItem(
            key=("anchor", anc.id),
            text=f"[锚点#{anc.id}·{status_word}] {clean_c}（出自{_branch_label(anc.branch_id)}）",
            occurred_at_ms=anc.updated_at_ms,
            weight=1.1,
            sparse_rank=sparse_rank,
        )

    def _format_arc(
        self,
        arc: LifeArcRow,
        *,
        target_visibility: str,
        all_ext_ids: Sequence[str],
        sparse_rank: Optional[int] = None,
    ) -> _CandidateItem:
        raw_body = (arc.public_summary or arc.summary) if target_visibility == "public" else arc.summary
        clean_title = sanitize_for_model(arc.title, all_ext_ids)
        clean_body = sanitize_for_model(raw_body, all_ext_ids)
        return _CandidateItem(
            key=("arc", arc.id),
            text=f"[近期主线·{clean_title}] {clean_body}",
            occurred_at_ms=arc.last_event_at_ms,
            weight=1.15,
            sparse_rank=sparse_rank,
        )

    def _format_event(
        self,
        ev: ExperienceEventRow,
        all_ext_ids: Sequence[str],
        *,
        sparse_rank: Optional[int] = None,
    ) -> _CandidateItem:
        where = describe_visibility_scope(ev.visibility, branch_id=ev.branch_id)
        clean_s = sanitize_for_model(ev.summary, all_ext_ids)
        quote = f" 原话：「{sanitize_for_model(ev.verbatim, all_ext_ids)}」" if ev.verbatim else ""
        weight = 1.0
        if ev.metadata.get("highlight"):
            weight *= 1.25
        if ev.actor_role == "self":
            weight *= 1.15
        return _CandidateItem(
            key=("event", ev.id),
            text=f"[经历·{where}] {clean_s}{quote}",
            occurred_at_ms=ev.occurred_at_ms,
            weight=weight,
            sparse_rank=sparse_rank,
        )
