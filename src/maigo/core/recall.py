"""双路多源混合检索服务：SQLite FTS5/拆词稀疏召回 + 单机语义向量 RRF 融合。

职责边界：
1. 同时检索 L0 原件书库（episode_records / episodes）、L2 实体关系图谱（knowledge_relations）、
   以及 L1 事件/主线/约定，按综合权重、温和时间衰减与抗同端回声规则排序；
2. 所有命中结果强制附带本地日期与场次/来源锚点，并严格执行可见性裁剪、遗忘过滤与账号脱敏；
3. 提供高置信度结构化召回接口（``high_confidence_only=True``），供接入端自动预召回宁缺毋滥。
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import math
import sqlite3
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

import httpx

from maigo.config import EmbeddingConfig
from maigo.core.digest_rules import is_hollow_interaction
from maigo.core.identity import IdentityService
from maigo.core.models import (
    EpisodeRecordRow,
    EpisodeRow,
    ExperienceEventRow,
    KnowledgeRelationRow,
    LifeArcRow,
    PersonaAnchorRow,
)
from maigo.core.privacy import (
    allowed_visibilities,
    describe_visibility_scope,
    normalize_visibility,
    sanitize_for_model,
)
from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.storage.db import MaiGODatabase
from maigo.storage.repos import (
    EmbeddingRepo,
    EpisodeRepo,
    ExperienceEventRepo,
    KnowledgeGraphRepo,
    LifeArcRepo,
    PersonaAnchorRepo,
    PersonRepo,
)
from maigo.storage.repos._text import count_term_hits, extract_query_terms

logger = get_logger("services.retrieval")

_RRF_K = 60.0
# 面向中文向量模型（如 bge-m3）的余弦底线：低于 0.48 不参与融合，杜绝无关记录凑数
_MIN_COSINE_SCORE = 0.48
# 自动预召回的强语义命中门槛，宁缺毋滥
_HIGH_CONF_COSINE_SCORE = 0.56
_DECAY_HALF_LIFE_DAYS = 60.0
_DAY_MS = 24 * 3600 * 1000
_ECHO_WINDOW_MS = 60 * 60 * 1000

_RECORD_TYPE_LABELS: Dict[str, str] = {
    "game_event": "游戏记录",
    "speech": "原话",
    "timeline_chunk": "分段实录",
    "work_doc": "存档笔记",
    "creature": "同伴名册",
    "archive_line": "场次档案",
    "interaction_fact": "互动细节",
}


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


@dataclass(frozen=True)
class RecallHit:
    """结构化召回单条结果（供高置信自动预召回与 HTTP/MCP 响应共用）。"""

    item_type: str
    item_id: int
    text: str
    score: float
    occurred_at_ms: int
    strong_hit: bool


@dataclass(frozen=True)
class RecallResult:
    """结构化召回总结果。"""

    text: str
    hits: List[RecallHit]
    strong_hit: bool


@dataclass
class _CandidateItem:
    key: Tuple[str, int]
    text: str
    raw_text: str
    occurred_at_ms: int
    weight: float = 1.0
    sparse_rank: Optional[int] = None
    dense_rank: Optional[int] = None
    cosine_score: float = 0.0
    term_hits: int = 0


class HybridRecallService:
    """结合稀疏全文、语义向量、实体关系与原件书库的跨端记忆检索器。"""

    def __init__(
        self,
        db: MaiGODatabase,
        *,
        identity: IdentityService,
        embedding_config: Optional[EmbeddingConfig] = None,
        embedding_client: Optional[EmbeddingClient] = None,
        timezone_offset_hours: int = 8,
        clock_ms: ClockMs = now_ms,
    ) -> None:
        self._db = db
        self._identity = identity
        self._config = embedding_config or EmbeddingConfig()
        self._tz = timezone(timedelta(hours=timezone_offset_hours))
        self._clock_ms = clock_ms
        self._events = ExperienceEventRepo(clock_ms=clock_ms)
        self._anchors = PersonaAnchorRepo(clock_ms=clock_ms)
        self._arcs = LifeArcRepo(clock_ms=clock_ms)
        self._embeddings = EmbeddingRepo(clock_ms=clock_ms)
        self._persons = PersonRepo(clock_ms=clock_ms)
        self._episodes = EpisodeRepo(clock_ms=clock_ms)
        self._knowledge = KnowledgeGraphRepo(clock_ms=clock_ms)

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

    def _date_tag(self, ts_ms: int) -> str:
        if ts_ms <= 0:
            return ""
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=self._tz).strftime("%Y-%m-%d")

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
            clean_items.append((item_type, int(item_id), cleaned[:2000], _content_hash(cleaned)))
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

        # 分批向量化防止单次批量过大触发网关超时
        total_saved = 0
        batch_size = 32
        for idx in range(0, len(to_embed), batch_size):
            chunk = to_embed[idx : idx + batch_size]
            vectors = await self._client.embed_texts([entry[2] for entry in chunk])
            if not vectors or len(vectors) != len(chunk):
                continue

            def _persist(
                conn: sqlite3.Connection,
                sub_chunk: Sequence[Tuple[str, int, str, str]] = chunk,
                sub_vecs: Sequence[List[float]] = vectors,
            ) -> int:
                saved = 0
                for (itype, iid, _txt, digest), vec in zip(sub_chunk, sub_vecs, strict=True):
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

            total_saved += await self._db.run(_persist)
        return total_saved

    async def recall(
        self,
        query: str,
        *,
        days: int = 30,
        limit: int = 5,
        target_visibility: str = "public",
        exclude_branch: Optional[str] = None,
        high_confidence_only: bool = False,
    ) -> str:
        """执行多源混合召回并输出脱敏、带日期与场次锚点的回想文本。"""
        result = await self.recall_structured(
            query,
            days=days,
            limit=limit,
            target_visibility=target_visibility,
            exclude_branch=exclude_branch,
            high_confidence_only=high_confidence_only,
        )
        if result.text:
            return result.text
        if high_confidence_only:
            return ""
        if int(days) > 0:
            return f"最近 {days} 天里没有找到与「{query}」相关的跨端记录。"
        return f"没有找到与「{query}」相关的跨端记录。"

    async def recall_structured(
        self,
        query: str,
        *,
        days: int = 30,
        limit: int = 5,
        target_visibility: str = "public",
        exclude_branch: Optional[str] = None,
        high_confidence_only: bool = False,
    ) -> RecallResult:
        """执行六源混合检索并返回带置信度标记的结构化结果，供自动预召回判断是否注入。"""
        vis_level = normalize_visibility(target_visibility, default="public")
        vis_allowed = allowed_visibilities(vis_level)
        now = int(self._clock_ms())
        since_ms = 0 if int(days) <= 0 else now - int(days) * _DAY_MS
        cap = max(1, int(limit))
        terms = extract_query_terms(query)
        echo_branch = (exclude_branch or "").strip().lower()

        def _is_recent_self_echo(branch_id: str, kind: str, occurred_ms: int) -> bool:
            return bool(
                echo_branch
                and branch_id.strip().lower() == echo_branch
                and kind == "speech"
                and (now - occurred_ms) <= _ECHO_WINDOW_MS
            )

        def _sparse_collect(conn: sqlite3.Connection) -> tuple[List[str], Dict[Tuple[str, int], _CandidateItem]]:
            all_ext_ids = [item.external_id for item in self._persons.list_all_identities(conn)]
            cands: Dict[Tuple[str, int], _CandidateItem] = {}
            ep_cache: Dict[int, Optional[EpisodeRow]] = {}

            def _get_ep(eid: Optional[int]) -> Optional[EpisodeRow]:
                if eid is None:
                    return None
                if eid not in ep_cache:
                    ep_cache[eid] = self._episodes.get_episode(conn, eid)
                return ep_cache[eid]

            rank = 0
            # 1. L0 原件书库（优先呈现最细颗粒度一手证据）
            for rec in self._episodes.search_records(
                conn, query=query, since_ms=since_ms, visibilities=vis_allowed, limit=cap * 2
            ):
                if high_confidence_only and _is_recent_self_echo(rec.branch_id, rec.record_type, rec.occurred_at_ms):
                    continue
                item = self._format_record(rec, _get_ep(rec.episode_id), all_ext_ids, terms, sparse_rank=rank)
                cands[item.key] = item
                rank += 1

            # 2. L0 场次概览（直接回答“哪一场直播”）
            for ep in self._episodes.search_episodes(
                conn, query=query, since_ms=since_ms, visibilities=vis_allowed, limit=cap
            ):
                item = self._format_episode(ep, all_ext_ids, terms, sparse_rank=rank)
                cands[item.key] = item
                rank += 1

            # 3. L2 实体关系图谱
            for rel in self._knowledge.search_relations(
                conn, query=query, since_ms=since_ms, visibilities=vis_allowed, limit=cap * 2
            ):
                item = self._format_relation(rel, _get_ep(rel.episode_id), all_ext_ids, terms, sparse_rank=rank)
                cands[item.key] = item
                rank += 1

            # 4. 观点与约定 & 持续主线
            for anc in self._anchors.search(conn, query=query, visibilities=vis_allowed, limit=cap * 2):
                if anc.status == "void":
                    continue
                item = self._format_anchor(anc, all_ext_ids, terms, sparse_rank=rank)
                cands[item.key] = item
                rank += 1

            for arc in self._arcs.search(conn, query=query, visibilities=vis_allowed, limit=cap):
                item = self._format_arc(arc, target_visibility=vis_level, all_ext_ids=all_ext_ids, terms=terms, sparse_rank=rank)
                cands[item.key] = item
                rank += 1

            # 5. L1 原始事件（跳过空壳游戏互动，对同端近期群聊回声降权或跳过）
            for ev in self._events.search(conn, query=query, since_ms=since_ms, visibilities=vis_allowed, limit=cap * 2):
                if (
                    ev.kind == "forget_request"
                    or is_hollow_interaction(ev)
                    or self._identity.has_forgotten_actor_sync(conn, ev.actors)
                ):
                    continue
                is_echo = _is_recent_self_echo(ev.branch_id, ev.kind, ev.occurred_at_ms)
                if high_confidence_only and is_echo:
                    continue
                item = self._format_event(ev, all_ext_ids, terms, sparse_rank=rank, echo_penalty=is_echo)
                cands[item.key] = item
                rank += 1

            return all_ext_ids, cands

        all_ext_ids, candidates = await self._db.run(_sparse_collect)

        # 语义向量召回分支（配置模型且成功向量化时启用，失败静默降级纯稀疏）
        query_vec: Optional[List[float]] = None
        if self._client is not None and query.strip():
            vecs = await self._client.embed_texts([query.strip()])
            if vecs and vecs[0]:
                query_vec = vecs[0]

        cos_floor = _HIGH_CONF_COSINE_SCORE if high_confidence_only else _MIN_COSINE_SCORE
        if query_vec is not None and self._client is not None:
            model_name = self._client.model_name

            def _dense_collect(
                conn: sqlite3.Connection,
            ) -> List[Tuple[float, Tuple[str, int], Optional[_CandidateItem]]]:
                emb_rows = self._embeddings.list_by_model(conn, model=model_name, limit=4000)
                scored: List[Tuple[float, Tuple[str, int]]] = []
                for erow in emb_rows:
                    sim = _cosine_similarity(query_vec, _unpack_vector(erow.vector_blob))
                    if sim >= cos_floor:
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
                        terms=terms,
                        now_ms_val=now,
                        echo_branch=echo_branch,
                        high_confidence_only=high_confidence_only,
                    )
                    if item is not None:
                        resolved.append((sim, key, item))
                return resolved

            dense_hits = await self._db.run(_dense_collect)
            for dense_idx, (sim, key, extra_item) in enumerate(dense_hits):
                if key in candidates:
                    candidates[key].dense_rank = dense_idx
                    candidates[key].cosine_score = max(candidates[key].cosine_score, sim)
                elif extra_item is not None:
                    extra_item.dense_rank = dense_idx
                    extra_item.cosine_score = sim
                    candidates[key] = extra_item

        if not candidates:
            return RecallResult(text="", hits=[], strong_hit=False)

        def _is_strong(item: _CandidateItem) -> bool:
            if item.cosine_score >= _HIGH_CONF_COSINE_SCORE:
                return True
            if item.term_hits >= 2:
                return True
            return bool(item.sparse_rank is not None and item.term_hits >= 1 and len(terms) <= 2)

        def _final_score(item: _CandidateItem) -> float:
            rrf = 0.0
            if item.sparse_rank is not None:
                rrf += 1.0 / (_RRF_K + item.sparse_rank + 1)
            if item.dense_rank is not None:
                rrf += 1.0 / (_RRF_K + item.dense_rank + 1)
            if item.term_hits >= 2:
                rrf *= 1.0 + min(0.5, 0.12 * item.term_hits)
            age_days = max(0.0, (now - item.occurred_at_ms) / float(_DAY_MS))
            decay = 1.0 / (1.0 + age_days / _DECAY_HALF_LIFE_DAYS)
            return rrf * decay * item.weight

        pool = list(candidates.values())
        if high_confidence_only:
            pool = [c for c in pool if _is_strong(c)]
            if not pool:
                return RecallResult(text="", hits=[], strong_hit=False)

        ranked = sorted(pool, key=lambda c: (_final_score(c), c.occurred_at_ms), reverse=True)
        # 按正文前缀去重，避免同一件事既在 L0 档案行又在 L1 事件里重复占坑
        selected: List[RecallHit] = []
        seen_sigs: set[str] = set()
        for cand in ranked:
            sig = cand.raw_text.strip()[:48].lower()
            if sig and sig in seen_sigs:
                continue
            if sig:
                seen_sigs.add(sig)
            selected.append(
                RecallHit(
                    item_type=cand.key[0],
                    item_id=cand.key[1],
                    text=cand.text,
                    score=_final_score(cand),
                    occurred_at_ms=cand.occurred_at_ms,
                    strong_hit=_is_strong(cand),
                )
            )
            if len(selected) >= cap:
                break

        any_strong = any(h.strong_hit for h in selected)
        return RecallResult(
            text="\n".join(h.text for h in selected),
            hits=selected,
            strong_hit=any_strong,
        )

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
        terms: List[str],
        now_ms_val: int,
        echo_branch: str,
        high_confidence_only: bool,
    ) -> Optional[_CandidateItem]:
        if item_type == "record":
            rec = self._episodes.get_record_by_id(conn, item_id)
            if rec is None or rec.visibility not in vis_allowed or (since_ms > 0 and rec.occurred_at_ms < since_ms):
                return None
            ep = self._episodes.get_episode(conn, rec.episode_id) if rec.episode_id is not None else None
            return self._format_record(rec, ep, all_ext_ids, terms)
        if item_type == "episode":
            ep = self._episodes.get_episode(conn, item_id)
            if ep is None or ep.visibility not in vis_allowed or (since_ms > 0 and ep.started_at_ms < since_ms):
                return None
            return self._format_episode(ep, all_ext_ids, terms)
        if item_type == "relation":
            rel = self._knowledge.get_relation_by_id(conn, item_id)
            if (
                rel is None
                or rel.status != "active"
                or rel.visibility not in vis_allowed
                or (since_ms > 0 and rel.occurred_at_ms < since_ms)
            ):
                return None
            ep = self._episodes.get_episode(conn, rel.episode_id) if rel.episode_id is not None else None
            return self._format_relation(rel, ep, all_ext_ids, terms)
        if item_type == "event":
            ev = self._events.get_by_id(conn, item_id)
            if (
                ev is None
                or ev.visibility not in vis_allowed
                or (since_ms > 0 and ev.occurred_at_ms < since_ms)
                or ev.kind == "forget_request"
                or is_hollow_interaction(ev)
                or self._identity.has_forgotten_actor_sync(conn, ev.actors)
            ):
                return None
            is_echo = bool(
                echo_branch
                and ev.branch_id.strip().lower() == echo_branch
                and ev.kind == "speech"
                and (now_ms_val - ev.occurred_at_ms) <= _ECHO_WINDOW_MS
            )
            if high_confidence_only and is_echo:
                return None
            return self._format_event(ev, all_ext_ids, terms, echo_penalty=is_echo)
        if item_type == "anchor":
            anc = self._anchors.get_by_id(conn, item_id)
            if anc is None or anc.visibility not in vis_allowed or anc.status == "void":
                return None
            return self._format_anchor(anc, all_ext_ids, terms)
        if item_type == "arc":
            arc = self._arcs.get_by_id(conn, item_id)
            if arc is None or arc.visibility not in vis_allowed or (since_ms > 0 and arc.last_event_at_ms < since_ms):
                return None
            return self._format_arc(arc, target_visibility=target_visibility, all_ext_ids=all_ext_ids, terms=terms)
        return None

    def _format_record(
        self,
        rec: EpisodeRecordRow,
        ep: Optional[EpisodeRow],
        all_ext_ids: Sequence[str],
        terms: List[str],
        *,
        sparse_rank: Optional[int] = None,
    ) -> _CandidateItem:
        dtag = self._date_tag(rec.occurred_at_ms)
        ep_label = f"直播《{sanitize_for_model(ep.title, all_ext_ids)}》" if (ep and ep.title) else _branch_label(rec.branch_id)
        kind_word = _RECORD_TYPE_LABELS.get(rec.record_type, rec.record_type)
        prefix = f"[{dtag} · {ep_label} · {kind_word}]" if dtag else f"[{ep_label} · {kind_word}]"
        clean_title = sanitize_for_model(rec.title, all_ext_ids)
        clean_body = sanitize_for_model(rec.content, all_ext_ids)
        head = f"{clean_title}：{clean_body}" if (clean_title and clean_title not in clean_body) else (clean_body or clean_title)
        quote = f" 原话：「{sanitize_for_model(rec.verbatim, all_ext_ids)[:240]}」" if rec.verbatim and rec.verbatim != rec.content else ""
        raw = f"{rec.title} {rec.content} {rec.verbatim}"
        return _CandidateItem(
            key=("record", rec.id),
            text=f"{prefix} {head}{quote}",
            raw_text=clean_body or clean_title,
            occurred_at_ms=rec.occurred_at_ms,
            weight=1.25,
            sparse_rank=sparse_rank,
            term_hits=count_term_hits(raw, terms),
        )

    def _format_episode(
        self,
        ep: EpisodeRow,
        all_ext_ids: Sequence[str],
        terms: List[str],
        *,
        sparse_rank: Optional[int] = None,
    ) -> _CandidateItem:
        dtag = self._date_tag(ep.started_at_ms)
        clean_title = sanitize_for_model(ep.title or ep.external_episode_id, all_ext_ids)
        clean_ov = sanitize_for_model(ep.overview, all_ext_ids)
        prefix = f"[{dtag} · 场次《{clean_title}》]" if dtag else f"[场次《{clean_title}》]"
        return _CandidateItem(
            key=("episode", ep.id),
            text=f"{prefix} {clean_ov}",
            raw_text=clean_ov or clean_title,
            occurred_at_ms=ep.ended_at_ms or ep.started_at_ms,
            weight=1.2,
            sparse_rank=sparse_rank,
            term_hits=count_term_hits(f"{ep.title} {ep.overview}", terms),
        )

    def _format_relation(
        self,
        rel: KnowledgeRelationRow,
        ep: Optional[EpisodeRow],
        all_ext_ids: Sequence[str],
        terms: List[str],
        *,
        sparse_rank: Optional[int] = None,
    ) -> _CandidateItem:
        dtag = self._date_tag(rel.occurred_at_ms)
        ep_part = f" · 《{sanitize_for_model(ep.title, all_ext_ids)}》" if (ep and ep.title) else ""
        clean_fact = sanitize_for_model(rel.fact_text, all_ext_ids)
        clean_sub = sanitize_for_model(rel.subject_name, all_ext_ids)
        body = clean_fact if (not clean_sub or clean_sub in clean_fact) else f"{clean_sub}：{clean_fact}"
        prefix = f"[关系·{dtag}{ep_part}]" if dtag else f"[关系{ep_part}]"
        raw = f"{rel.subject_name} {rel.object_name} {rel.object_text} {rel.fact_text} {rel.quote}"
        return _CandidateItem(
            key=("relation", rel.id),
            text=f"{prefix} {body}",
            raw_text=clean_fact,
            occurred_at_ms=rel.occurred_at_ms,
            weight=1.2,
            sparse_rank=sparse_rank,
            term_hits=count_term_hits(raw, terms),
        )

    def _format_anchor(
        self,
        anc: PersonaAnchorRow,
        all_ext_ids: Sequence[str],
        terms: List[str],
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
        dtag = self._date_tag(anc.updated_at_ms)
        clean_c = sanitize_for_model(anc.content, all_ext_ids)
        date_part = f"·{dtag}" if dtag else ""
        return _CandidateItem(
            key=("anchor", anc.id),
            text=f"[锚点#{anc.id}·{status_word}{date_part}] {clean_c}（出自{_branch_label(anc.branch_id)}）",
            raw_text=clean_c,
            occurred_at_ms=anc.updated_at_ms,
            weight=1.1,
            sparse_rank=sparse_rank,
            term_hits=count_term_hits(f"{anc.content} {anc.verbatim} {anc.note}", terms),
        )

    def _format_arc(
        self,
        arc: LifeArcRow,
        *,
        target_visibility: str,
        all_ext_ids: Sequence[str],
        terms: List[str],
        sparse_rank: Optional[int] = None,
    ) -> _CandidateItem:
        raw_body = (arc.public_summary or arc.summary) if target_visibility == "public" else arc.summary
        clean_title = sanitize_for_model(arc.title, all_ext_ids)
        clean_body = sanitize_for_model(raw_body, all_ext_ids)
        return _CandidateItem(
            key=("arc", arc.id),
            text=f"[持续主线·{clean_title}] {clean_body}",
            raw_text=clean_body or clean_title,
            occurred_at_ms=arc.last_event_at_ms,
            weight=1.15,
            sparse_rank=sparse_rank,
            term_hits=count_term_hits(f"{arc.title} {raw_body}", terms),
        )

    def _format_event(
        self,
        ev: ExperienceEventRow,
        all_ext_ids: Sequence[str],
        terms: List[str],
        *,
        sparse_rank: Optional[int] = None,
        echo_penalty: bool = False,
    ) -> _CandidateItem:
        dtag = self._date_tag(ev.occurred_at_ms)
        where = describe_visibility_scope(ev.visibility, branch_id=ev.branch_id)
        clean_s = sanitize_for_model(ev.summary, all_ext_ids)
        quote = f" 原话：「{sanitize_for_model(ev.verbatim, all_ext_ids)}」" if ev.verbatim else ""
        weight = 1.0
        if ev.metadata.get("highlight") or ev.metadata.get("session_overview"):
            weight *= 1.25
        if ev.actor_role == "self":
            weight *= 1.15
        if echo_penalty:
            # 刚在本端群聊发出的回复大幅降权，防止“刚刚说不记得”把更早的真实直播经历挤出前列
            weight *= 0.30
        prefix = f"[{dtag} · {where}]" if dtag else f"[经历·{where}]"
        return _CandidateItem(
            key=("event", ev.id),
            text=f"{prefix} {clean_s}{quote}",
            raw_text=clean_s,
            occurred_at_ms=ev.occurred_at_ms,
            weight=weight,
            sparse_rank=sparse_rank,
            term_hits=count_term_hits(f"{ev.summary} {ev.verbatim} {ev.reaction}", terms),
        )
