"""后台日内编年合成器：把跨端无序经历收敛为日内纪要、言行锚点与鲜活话头。"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Set, Tuple

from maigo.config import SynthesizerConfig
from datetime import datetime, timedelta, timezone
from maigo.core.digest_rules import (
    AnchorCandidate,
    OpenAISynthesisClient,
    SlotSynthesisDraft,
    SynthesisLLMClient,
    build_rule_draft,
    compress_yesterday,
    compute_day_and_slot,
    prioritize_slot_events,
    slot_rank,
)
from maigo.core.identity import IdentityService
from maigo.core.models import DailyChronicleRow, ExperienceEventRow, TopicSeedRow
from maigo.core.privacy import sanitize_for_model
from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.storage.db import MaiGODatabase
from maigo.storage.repos import (
    DailyChronicleRepo,
    ExperienceEventRepo,
    LifeArcRepo,
    PersonaAnchorRepo,
    TopicSeedRepo,
)

if TYPE_CHECKING:
    from maigo.core.maintenance import MaintenanceService
    from maigo.core.recall import HybridRecallService

logger = get_logger("services.synthesizer")


@dataclass(frozen=True)
class SynthesisReport:
    """单轮合成执行摘要（供后台观测与单元测试断言）。"""

    processed_events: int = 0
    touched_slots: int = 0
    sealed_slots: int = 0
    created_anchors: int = 0
    updated_anchors: int = 0
    blocked_poison_anchors: int = 0
    created_topics: int = 0
    marked_topic_consumptions: int = 0
    upserted_arcs: int = 0
    staled_promises: int = 0


class ChronicleSynthesizer:
    """编年合成主服务：串联未整理事件拉取、时段封存、代码防投毒入账、温层主线与跨日归档。"""

    def __init__(
        self,
        db: MaiGODatabase,
        *,
        identity: IdentityService,
        config: SynthesizerConfig,
        day_rollover_hour: int = 4,
        timezone_offset_hours: int = 8,
        clock_ms: ClockMs = now_ms,
        llm_client: Optional[SynthesisLLMClient] = None,
        retrieval: Optional[HybridRecallService] = None,
        maintenance: Optional[MaintenanceService] = None,
        auto_daily_backup: bool = False,
        auto_index_embeddings: bool = True,
    ) -> None:
        self._db = db
        self._identity = identity
        self._config = config
        self._rollover_hour = day_rollover_hour
        self._tz_offset = timezone_offset_hours
        self._clock_ms = clock_ms
        self._events = ExperienceEventRepo(clock_ms=clock_ms)
        self._chronicle = DailyChronicleRepo(clock_ms=clock_ms)
        self._anchors = PersonaAnchorRepo(clock_ms=clock_ms)
        self._topics = TopicSeedRepo(clock_ms=clock_ms)
        self._arcs = LifeArcRepo(clock_ms=clock_ms)
        self._retrieval = retrieval
        self._maintenance = maintenance
        self._auto_daily_backup = auto_daily_backup
        self._auto_index_embeddings = auto_index_embeddings

        if llm_client is not None:
            self._llm: Optional[SynthesisLLMClient] = llm_client
        elif config.base_url.strip() and config.model.strip():
            self._llm = OpenAISynthesisClient(
                base_url=config.base_url,
                api_key=config.api_key,
                model=config.model,
                timeout_ms=config.timeout_ms,
            )
        else:
            self._llm = None

    @property
    def mode(self) -> str:
        """返回当前编年整理方式（llm / rule）。"""
        return "llm" if self._llm is not None else "rule"

    async def synthesize_pending(self, *, force_seal: bool = False) -> SynthesisReport:
        """执行一轮编年合成，处理全部未整理事件、按作息封存时段、沉淀温层主线与自动维护。"""
        now = int(self._clock_ms())
        current_day, current_slot = compute_day_and_slot(
            now, day_rollover_hour=self._rollover_hour, timezone_offset_hours=self._tz_offset
        )

        # 第一步：跨日封存旧日开放时段、补全昨日余温，并按配置降权超期未兑现承诺
        await self._db.run(lambda c: self._handle_day_rollover_sync(c, current_day=current_day))
        stale_before = now - max(1, int(self._config.stale_promise_days)) * 24 * 3600 * 1000

        unsynthesized = await self._db.run(lambda c: self._events.list_unsynthesized(c, limit=400))
        if not unsynthesized:
            sealed_count = (
                await self._db.run(lambda c: self._seal_day_slots_sync(c, day_key=current_day)) if force_seal else 0
            )
            staled = await self._db.run(lambda c: self._anchors.mark_stale_promises(c, before_ms=stale_before))
            await self._maybe_run_daily_backup(current_day)
            return SynthesisReport(sealed_slots=sealed_count, staled_promises=staled)

        known_external_ids = await self._db.run(
            lambda c: [item.external_id for item in self._identity._persons.list_all_identities(c)]
        )

        groups: Dict[Tuple[str, str], List[ExperienceEventRow]] = {}
        for ev in unsynthesized:
            key = compute_day_and_slot(
                ev.occurred_at_ms,
                day_rollover_hour=self._rollover_hour,
                timezone_offset_hours=self._tz_offset,
            )
            groups.setdefault(key, []).append(ev)

        touched_slots = 0
        sealed_slots = 0
        created_anchors = 0
        updated_anchors = 0
        blocked_poison = 0
        created_topics = 0
        marked_consumptions = 0
        upserted_arcs = 0
        processed_ids: List[int] = []
        index_queue: List[Tuple[str, int, str]] = []

        for (day_key, slot_key), batch in sorted(groups.items(), key=lambda kv: (kv[0][0], slot_rank(kv[0][1]))):
            is_past_slot = day_key < current_day or (
                day_key == current_day and slot_rank(slot_key) < slot_rank(current_slot)
            )
            target_status = "sealed" if (force_seal or is_past_slot) else "open"

            existing_slot = await self._load_slot(day_key=day_key, slot_key=slot_key)
            slot_events = await self._load_combined_slot_events(existing_slot, batch)
            valid_events = await self._filter_forgotten_events(slot_events)

            draft = await self._draft_slot(day_key=day_key, slot_key=slot_key, valid_events=valid_events)
            draft_source = "llm"
            if draft is None:
                draft = build_rule_draft(valid_events, known_external_ids=known_external_ids)
                draft_source = "rule"

            if valid_events:
                await self._save_slot(
                    day_key=day_key,
                    slot_key=slot_key,
                    status=target_status,
                    draft=draft,
                    source_event_ids=[ev.id for ev in slot_events],
                    known_external_ids=known_external_ids,
                    draft_source=draft_source,
                )
                touched_slots += 1
                if target_status == "sealed":
                    sealed_slots += 1

            ev_map = {ev.id: ev for ev in batch}
            c_anc, u_anc, blk, c_top, m_cons, u_arc, new_idx = await self._commit_candidates(
                batch=batch,
                ev_map=ev_map,
                draft=draft,
                known_external_ids=known_external_ids,
            )
            created_anchors += c_anc
            updated_anchors += u_anc
            blocked_poison += blk
            created_topics += c_top
            marked_consumptions += m_cons
            upserted_arcs += u_arc
            index_queue.extend(new_idx)
            processed_ids.extend(ev.id for ev in batch)

            for ev in batch:
                if ev.kind == "forget_request":
                    continue
                ev_text = f"{ev.summary} {ev.verbatim} {ev.reaction}".strip()
                if ev_text:
                    index_queue.append(("event", ev.id, sanitize_for_model(ev_text, known_external_ids)))

        sealed_slots += await self._db.run(
            lambda c: self._seal_earlier_slots_sync(
                c, current_day=current_day, current_slot=current_slot, force_seal=force_seal
            )
        )
        await self._db.run(lambda c: self._handle_day_rollover_sync(c, current_day=current_day))
        staled_promises = await self._db.run(lambda c: self._anchors.mark_stale_promises(c, before_ms=stale_before))
        await self._db.run(lambda c: self._events.mark_synthesized(c, processed_ids, synthesized_at_ms=now))

        if self._retrieval is not None and self._auto_index_embeddings and index_queue:
            try:
                await self._retrieval.index_items(index_queue)
            except Exception as exc:
                logger.warning(f"编年后自动补齐语义向量失败（不影响主库落盘）: {exc}")

        await self._maybe_run_daily_backup(current_day)

        return SynthesisReport(
            processed_events=len(processed_ids),
            touched_slots=touched_slots,
            sealed_slots=sealed_slots,
            created_anchors=created_anchors,
            updated_anchors=updated_anchors,
            blocked_poison_anchors=blocked_poison,
            created_topics=created_topics,
            marked_topic_consumptions=marked_consumptions,
            upserted_arcs=upserted_arcs,
            staled_promises=staled_promises,
        )

    async def _draft_slot(
        self,
        *,
        day_key: str,
        slot_key: str,
        valid_events: Sequence[ExperienceEventRow],
    ) -> Optional[SlotSynthesisDraft]:
        if self._llm is None or not valid_events:
            return None
        # 送入外部模型前先降噪并限定条数，降低长时段整理超时概率
        focused = prioritize_slot_events(valid_events, max_events=40)
        with_ctx = getattr(self._llm, "summarize_slot_with_context", None)
        if callable(with_ctx):
            stances, arcs = await self._db.run(
                lambda c: (
                    self._anchors.list_anchors(c, anchor_type="stance", statuses=["active"], limit=10),
                    self._arcs.list_arcs(c, statuses=["active"], limit=6),
                )
            )
            return await with_ctx(
                day_key=day_key,
                slot_key=slot_key,
                events=focused,
                existing_stances=stances,
                existing_arcs=arcs,
                persona_name=self._config.persona_name,
            )
        return await self._llm.summarize_slot(day_key=day_key, slot_key=slot_key, events=focused)

    async def _maybe_run_daily_backup(self, current_day: str) -> None:
        if self._maintenance is None or not self._auto_daily_backup:
            return
        try:
            if not self._maintenance.has_backup_for_day(current_day):
                await self._maintenance.create_backup()
        except Exception as exc:
            logger.warning(f"每日自动热备份失败（跳过本次）: {exc}")

    async def run_idle_maintenance(self) -> None:
        """无未处理事件时的后台例行维护：补写规则回落时段、跨日封存、约定过期、WAL 收拢与每日热备份。"""
        now = int(self._clock_ms())
        current_day, _ = compute_day_and_slot(
            now, day_rollover_hour=self._rollover_hour, timezone_offset_hours=self._tz_offset
        )
        stale_before = now - max(1, int(self._config.stale_promise_days)) * 24 * 3600 * 1000

        # 空闲期优先用外部模型补写近 7 天内因超时退回规则拼接的时段（每次最多 2 段，防长阻塞）
        if self._llm is not None:
            await self._retry_rule_fallback_slots(now=now, current_day=current_day, limit=2)

        def _maintain(conn: sqlite3.Connection) -> None:
            self._handle_day_rollover_sync(conn, current_day=current_day)
            self._anchors.mark_stale_promises(conn, before_ms=stale_before)
            # 在仅接收低写入心跳的空闲期定期做非阻塞 checkpoint，避免 WAL 持续膨胀
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

        await self._db.run(_maintain)
        await self._maybe_run_daily_backup(current_day)

    async def _retry_rule_fallback_slots(self, *, now: int, current_day: str, limit: int = 2) -> int:
        """将此前因模型超时降级为 rule 的近期时段重新用 LLM 润色重写，解除永久锁死。"""
        tz = timezone(timedelta(hours=self._tz_offset))
        since_day = (datetime.fromtimestamp(now / 1000.0, tz=tz) - timedelta(days=7)).strftime("%Y-%m-%d")
        fallback_slots = await self._db.run(
            lambda c: self._chronicle.list_rule_fallback_slots(c, since_day_key=since_day, limit=limit)
        )
        if not fallback_slots:
            return 0
        known_external_ids = await self._db.run(
            lambda c: [item.external_id for item in self._identity._persons.list_all_identities(c)]
        )
        rewritten = 0
        for slot in fallback_slots:
            slot_events = await self._load_combined_slot_events(slot, ())
            valid_events = await self._filter_forgotten_events(slot_events)
            if not valid_events:
                continue
            draft = await self._draft_slot(day_key=slot.day_key, slot_key=slot.slot_key, valid_events=valid_events)
            if draft is None:
                continue
            await self._save_slot(
                day_key=slot.day_key,
                slot_key=slot.slot_key,
                status=slot.status,
                draft=draft,
                source_event_ids=[ev.id for ev in slot_events],
                known_external_ids=known_external_ids,
                draft_source="llm",
            )
            rewritten += 1
        if rewritten > 0:
            await self._db.run(lambda c: self._handle_day_rollover_sync(c, current_day=current_day))
        return rewritten

    async def _load_slot(self, *, day_key: str, slot_key: str) -> Optional[DailyChronicleRow]:
        def _op(conn: sqlite3.Connection) -> Optional[DailyChronicleRow]:
            return self._chronicle.get_slot(conn, day_key=day_key, slot_key=slot_key)

        return await self._db.run(_op)

    async def _save_slot(
        self,
        *,
        day_key: str,
        slot_key: str,
        status: str,
        draft: SlotSynthesisDraft,
        source_event_ids: Sequence[int],
        known_external_ids: Sequence[str],
        draft_source: str = "llm",
    ) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            self._chronicle.upsert_slot(
                conn,
                day_key=day_key,
                slot_key=slot_key,
                status=status,
                body=sanitize_for_model(draft.body, known_external_ids),
                public_body=sanitize_for_model(draft.public_body, known_external_ids),
                group_body=sanitize_for_model(draft.group_body, known_external_ids),
                source_event_ids=source_event_ids,
                mood_tag=sanitize_for_model(draft.mood_tag, known_external_ids)[:24],
                unfinished_thread=sanitize_for_model(draft.unfinished_thread, known_external_ids)[:100],
                draft_source=draft_source,
            )

        await self._db.run(_op)

    async def _commit_candidates(
        self,
        *,
        batch: Sequence[ExperienceEventRow],
        ev_map: Dict[int, ExperienceEventRow],
        draft: SlotSynthesisDraft,
        known_external_ids: Sequence[str],
    ) -> Tuple[int, int, int, int, int, int, List[Tuple[str, int, str]]]:
        def _op(conn: sqlite3.Connection) -> Tuple[int, int, int, int, int, int, List[Tuple[str, int, str]]]:
            return self._commit_candidates_sync(
                conn,
                batch=batch,
                ev_map=ev_map,
                draft=draft,
                known_external_ids=known_external_ids,
            )

        return await self._db.run(_op)

    async def _load_combined_slot_events(
        self,
        existing_slot: Optional[DailyChronicleRow],
        new_batch: Sequence[ExperienceEventRow],
    ) -> List[ExperienceEventRow]:
        """合并时段内既有的来源事件与本轮新事件并去重排序。"""
        if existing_slot is None or not existing_slot.source_event_ids:
            return list(new_batch)

        def _fetch(conn: sqlite3.Connection) -> List[ExperienceEventRow]:
            by_id: Dict[int, ExperienceEventRow] = {}
            for eid in existing_slot.source_event_ids:
                row = self._events.get_by_id(conn, eid)
                if row is not None:
                    by_id[row.id] = row
            for ev in new_batch:
                by_id[ev.id] = ev
            return sorted(by_id.values(), key=lambda x: (x.occurred_at_ms, x.id))

        return await self._db.run(_fetch)

    async def _filter_forgotten_events(self, events: Sequence[ExperienceEventRow]) -> List[ExperienceEventRow]:
        """过滤关联了已遗忘外部身份的事件，确保不再进入合成纪要。"""

        def _check(conn: sqlite3.Connection) -> List[ExperienceEventRow]:
            return [
                ev
                for ev in events
                if ev.kind != "forget_request" and not self._identity.has_forgotten_actor_sync(conn, ev.actors)
            ]

        return await self._db.run(_check)

    def _commit_candidates_sync(
        self,
        conn: sqlite3.Connection,
        *,
        batch: Sequence[ExperienceEventRow],
        ev_map: Dict[int, ExperienceEventRow],
        draft: SlotSynthesisDraft,
        known_external_ids: Sequence[str],
    ) -> Tuple[int, int, int, int, int, int, List[Tuple[str, int, str]]]:
        """校验并入账候选锚点、话头、温层主线与跨端消费轨迹。"""
        index_items: List[Tuple[str, int, str]] = []
        created_anchors, updated_anchors, blocked_poison, anchor_idx = self._persist_anchors_sync(
            conn,
            ev_map=ev_map,
            candidates=draft.anchors,
            known_external_ids=known_external_ids,
        )
        index_items.extend(anchor_idx)

        created_topics = 0
        expires_at = int(self._clock_ms()) + self._config.topic_ttl_ms
        for tcand in draft.topics:
            src = ev_map.get(tcand.source_event_id) or self._events.get_by_id(conn, tcand.source_event_id)
            if src is None or src.visibility == "private" or not tcand.title.strip():
                continue
            before_exists = conn.execute(
                "SELECT id FROM topic_seeds WHERE source_event_id = ? LIMIT 1",
                (src.id,),
            ).fetchone()
            self._topics.create(
                conn,
                title=sanitize_for_model(tcand.title, known_external_ids),
                summary=sanitize_for_model(tcand.summary, known_external_ids),
                reaction=sanitize_for_model(tcand.reaction, known_external_ids),
                source_branch=src.branch_id,
                source_event_id=src.id,
                visibility=src.visibility,
                consumed_branches=tcand.consumed_branches,
                expires_at_ms=expires_at,
            )
            if before_exists is None:
                created_topics += 1

        upserted_arcs = 0
        for acand in draft.arcs:
            if not acand.title.strip() or acand.visibility == "private":
                continue
            src_arc = (
                (ev_map.get(acand.source_event_id) or self._events.get_by_id(conn, acand.source_event_id))
                if acand.source_event_id is not None
                else None
            )
            if src_arc is not None and src_arc.visibility == "private":
                continue
            clean_title = sanitize_for_model(acand.title, known_external_ids)[:64]
            clean_sum = sanitize_for_model(acand.summary, known_external_ids)[:240]
            clean_pub = sanitize_for_model(acand.public_summary or acand.summary, known_external_ids)[:240]
            saved_arc = self._arcs.upsert_or_create(
                conn,
                title=clean_title,
                summary=clean_sum,
                public_summary=clean_pub,
                status=acand.status if acand.status in ("active", "paused", "completed") else "active",
                visibility=acand.visibility if acand.visibility in ("public", "group", "private") else "public",
                save_id=acand.save_id or (src_arc.save_id if src_arc is not None else None),
                mood_hint=sanitize_for_model(acand.mood_hint, known_external_ids)[:40],
                source_event_ids=[src_arc.id] if src_arc is not None else (),
                last_event_at_ms=src_arc.occurred_at_ms if src_arc is not None else None,
            )
            upserted_arcs += 1
            index_items.append(("arc", saved_arc.id, f"{saved_arc.title}：{saved_arc.summary}"))

        marked_consumptions = self._detect_topic_consumptions_sync(conn, batch=batch)
        return (
            created_anchors,
            updated_anchors,
            blocked_poison,
            created_topics,
            marked_consumptions,
            upserted_arcs,
            index_items,
        )

    def _persist_anchors_sync(
        self,
        conn: sqlite3.Connection,
        *,
        ev_map: Dict[int, ExperienceEventRow],
        candidates: Sequence[AnchorCandidate],
        known_external_ids: Sequence[str],
    ) -> Tuple[int, int, int, List[Tuple[str, int, str]]]:
        """强校验锚点候选（仅允许源自存在的 self 事件且具原话），支持立场演化替代链。"""
        created = 0
        updated = 0
        blocked = 0
        seen_keys: Set[Tuple[str, str]] = set()
        index_items: List[Tuple[str, int, str]] = []

        for cand in candidates:
            src = ev_map.get(cand.source_event_id) or self._events.get_by_id(conn, cand.source_event_id)
            if src is None:
                blocked += 1
                logger.warning(f"拦截未知出处锚点候选: content={cand.content!r}")
                continue
            if src.actor_role != "self":
                blocked += 1
                logger.warning(f"防投毒护栏拦截非 self 锚点候选: event_id={src.id} role={src.actor_role}")
                continue
            if src.kind != "promise_update" and not src.verbatim.strip():
                blocked += 1
                logger.warning(f"防投毒护栏拦截无麦麦原话的锚点候选: event_id={src.id}")
                continue

            clean_content = sanitize_for_model(cand.content, known_external_ids).strip()
            if not clean_content or (cand.anchor_type, clean_content) in seen_keys:
                continue
            seen_keys.add((cand.anchor_type, clean_content))

            resolved_type = cand.anchor_type if cand.anchor_type in ("stance", "promise") else "stance"
            # 同一源事件已产出过同类锚点（包含已兑现或判误作废）时，同槽重合成不得将其复活为未结项
            if cand.status in ("open", "active"):
                prior_for_source = conn.execute(
                    "SELECT id FROM persona_anchors WHERE source_event_id = ? AND anchor_type = ? LIMIT 1",
                    (src.id, resolved_type),
                ).fetchone()
                if prior_for_source is not None:
                    continue

            existing = self._anchors.find_by_content(conn, anchor_type=resolved_type, content=clean_content)
            if existing is not None:
                if cand.status in ("closed", "void") and existing.status != cand.status:
                    self._anchors.update(conn, existing.id, status=cand.status, note=cand.note or existing.note)
                    updated += 1
                continue

            target_pid = cand.target_person_id
            if target_pid is None and len(src.actors) == 1:
                ident = self._identity._persons.get_identity(
                    conn, platform=src.actors[0].platform, external_id=src.actors[0].external_id
                )
                if ident is not None:
                    target_pid = ident.person_id

            created_row = self._anchors.create(
                conn,
                anchor_type=resolved_type,
                content=clean_content,
                verbatim=sanitize_for_model(src.verbatim, known_external_ids),
                source_event_id=src.id,
                branch_id=src.branch_id,
                visibility=src.visibility,
                save_id=cand.save_id or src.save_id,
                target_person_id=target_pid,
                status=cand.status if cand.status in ("active", "open", "closed", "void") else None,
                note=sanitize_for_model(cand.note, known_external_ids),
            )
            created += 1
            index_items.append(("anchor", created_row.id, clean_content))

            if cand.supersedes_id is not None and resolved_type == "stance":
                old_row = self._anchors.get_by_id(conn, cand.supersedes_id)
                if old_row is not None and old_row.anchor_type == "stance" and old_row.status == "active":
                    self._anchors.supersede(conn, old_row.id, created_row.id)
                    updated += 1
        return created, updated, blocked, index_items

    def _detect_topic_consumptions_sync(
        self,
        conn: sqlite3.Connection,
        *,
        batch: Sequence[ExperienceEventRow],
    ) -> int:
        """探测麦麦在群聊或直播发言中提及活跃话头，自动登记消费分支轨迹。"""
        marked = 0
        active_topics = self._topics.list_all(conn, status="active", limit=100)
        for ev in batch:
            if ev.actor_role != "self" or ev.visibility == "private":
                continue
            explicit_id = ev.metadata.get("topic_seed_id")
            if explicit_id is not None:
                if self._topics.mark_consumed(conn, topic_id=int(explicit_id), branch_id=ev.branch_id) is not None:
                    marked += 1
                continue
            combined_text = f"{ev.summary} {ev.verbatim}".lower()
            for topic in active_topics:
                if topic.source_event_id == ev.id or ev.branch_id in topic.consumed_branches:
                    continue
                if self._topic_mentioned(topic, combined_text):
                    self._topics.mark_consumed(conn, topic_id=topic.id, branch_id=ev.branch_id)
                    marked += 1
        return marked

    @staticmethod
    def _topic_mentioned(topic: TopicSeedRow, lower_text: str) -> bool:
        """判断麦麦发言是否聊及某个活跃话头（按核心标题或高重合 2-gram 判定）。"""
        title = re.sub(r"[\s，。！？、“”\"'（）()]+", "", topic.title).lower()
        title = re.sub(r"\[[^\]]*\]", "", title)
        compact_text = re.sub(r"\s+", "", lower_text)
        if len(title) >= 3 and title in compact_text:
            return True
        if len(title) >= 4:
            # 去掉常见动作前缀（如“刷到”）提升核心名词命中率
            core = re.sub(r"^(?:刷到|看到|听说|聊到)(?:一个|用)?", "", title)
            if len(core) >= 4 and core in compact_text:
                return True
            tokens = {core[i : i + 2] for i in range(max(0, len(core) - 1))}
            if not tokens:
                return False
            hits = sum(1 for tok in tokens if tok in compact_text)
            return hits >= min(4, max(2, len(tokens) // 3))
        return False

    def _seal_slot_row(self, conn: sqlite3.Connection, slot: DailyChronicleRow) -> None:
        self._chronicle.upsert_slot(
            conn,
            day_key=slot.day_key,
            slot_key=slot.slot_key,
            status="sealed",
            body=slot.body,
            public_body=slot.public_body,
            group_body=slot.group_body,
            source_event_ids=slot.source_event_ids,
            mood_tag=slot.mood_tag,
            unfinished_thread=slot.unfinished_thread,
            draft_source=slot.draft_source,
        )

    def _handle_day_rollover_sync(self, conn: sqlite3.Connection, *, current_day: str) -> None:
        """封存早于今天作息日的遗留开放时段，并将上一天纪要压缩写入今日昨日余温。"""
        stale_open = self._chronicle.list_open_before_day(conn, day_key=current_day)
        for slot in stale_open:
            self._seal_slot_row(conn, slot)

        recent_days = [d for d in self._chronicle.list_recent_days(conn, limit_days=5) if d < current_day]
        if not recent_days:
            return
        prev_day = recent_days[0]
        prev_rows = self._chronicle.list_by_day(conn, day_key=prev_day, status="sealed")
        if not prev_rows:
            return
        body = compress_yesterday(prev_rows, "body")
        pub_body = compress_yesterday(prev_rows, "public_body")
        grp_body = compress_yesterday(prev_rows, "group_body")
        if not (body or pub_body or grp_body):
            return
        existing_carry = self._chronicle.get_slot(conn, day_key=current_day, slot_key="yesterday_carry")
        if existing_carry is not None and existing_carry.body == body and existing_carry.public_body == pub_body:
            return
        self._chronicle.upsert_slot(
            conn,
            day_key=current_day,
            slot_key="yesterday_carry",
            status="sealed",
            body=body,
            public_body=pub_body,
            group_body=grp_body,
            source_event_ids=[],
        )

    def _seal_day_slots_sync(self, conn: sqlite3.Connection, *, day_key: str) -> int:
        """强制封存指定日期下所有未封存时段（如直播下播签入后立刻落定慢层）。"""
        count = 0
        for slot in self._chronicle.list_by_day(conn, day_key=day_key, status="open"):
            self._seal_slot_row(conn, slot)
            count += 1
        return count

    def _seal_earlier_slots_sync(
        self,
        conn: sqlite3.Connection,
        *,
        current_day: str,
        current_slot: str,
        force_seal: bool,
    ) -> int:
        """封存早于当前时段的所有当日开放段。"""
        count = 0
        cur_rank = slot_rank(current_slot)
        for slot in self._chronicle.list_by_day(conn, day_key=current_day, status="open"):
            if force_seal or slot_rank(slot.slot_key) < cur_rank:
                self._seal_slot_row(conn, slot)
                count += 1
        return count


class BackgroundSynthesizer:
    """后台异步合成调度器：根据事件攒批门槛与最小间隔自动运行编年整理。"""

    def __init__(
        self,
        db: MaiGODatabase,
        synthesizer: ChronicleSynthesizer,
        config: SynthesizerConfig,
        *,
        clock_ms: ClockMs = now_ms,
        on_brief_updated: Optional[Callable[[], object]] = None,
    ) -> None:
        self._db = db
        self._synthesizer = synthesizer
        self._config = config
        self._clock_ms = clock_ms
        self._on_brief_updated = on_brief_updated
        self._events = ExperienceEventRepo(clock_ms=clock_ms)
        self._wakeup = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._last_run_ms = 0
        self._last_maintenance_ms = 0
        self._stopped = False

    def start(self) -> None:
        """启动后台合成协程（幂等）。"""
        if not self._config.auto_background:
            return
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="maigo.synthesizer")

    async def stop(self) -> None:
        """停止后台合成协程并清理任务句柄。"""
        self._stopped = True
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def notify_new_events(self, pending_hint: Optional[int] = None) -> None:
        """当入库积累超过阈值时立即唤醒后台循环。"""
        if pending_hint is not None and pending_hint >= self._config.batch_event_threshold:
            self._wakeup.set()

    async def trigger_now(self, *, force_seal: bool = False) -> SynthesisReport:
        """立即执行一轮合成（供签入完成或看板手动触发调用）。"""
        report = await self._synthesizer.synthesize_pending(force_seal=force_seal)
        self._last_run_ms = int(self._clock_ms())
        if (
            report.sealed_slots > 0 or report.created_anchors > 0 or report.updated_anchors > 0
        ) and self._on_brief_updated:
            try:
                res = self._on_brief_updated()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.warning(f"通知简报变更回调失败: {exc}")
        return report

    async def _loop(self) -> None:
        poll_s = max(0.2, self._config.poll_interval_ms / 1000.0)
        while not self._stopped:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=poll_s)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()
            if self._stopped:
                break
            try:
                pending = await self._db.run(self._events.count_unsynthesized)
                now_val = int(self._clock_ms())
                if pending <= 0:
                    if (
                        self._last_maintenance_ms == 0
                        or now_val - self._last_maintenance_ms >= self._config.min_interval_ms
                    ):
                        await self._synthesizer.run_idle_maintenance()
                        self._last_maintenance_ms = now_val
                    continue
                elapsed = now_val - self._last_run_ms
                if pending >= self._config.batch_event_threshold or elapsed >= self._config.min_interval_ms:
                    await self.trigger_now(force_seal=False)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"后台编年合成循环异常: {exc}", exc_info=True)
