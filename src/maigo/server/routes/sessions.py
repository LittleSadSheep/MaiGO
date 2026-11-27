"""按场次启停接入端的会话签出、心跳同步与签入路由（``/api/v1/sessions/*``）。"""

from __future__ import annotations

import sqlite3
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from maigo.core.models import NewEpisodeRecord
from maigo.core.privacy import normalize_visibility
from maigo.server.auth import CtxDep, require_auth
from maigo.server.routes.sync import ingest_knowledge_sync
from maigo.server.schemas import (
    AnchorProjectionOut,
    CheckinRequest,
    CheckinResponse,
    CheckoutRequest,
    CheckoutResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    KnowledgeEntityIn,
    KnowledgeRelationIn,
    PersonDirectoryEntryOut,
    TopicProjectionOut,
)
from maigo.storage import ActorRef, NewExperienceEvent

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/api/v1/sessions/checkout", response_model=CheckoutResponse)
async def checkout_session(
    payload: CheckoutRequest,
    ctx: CtxDep,
) -> CheckoutResponse:
    """会话签出：一次性返回稳定背景、实时动态、待兑现约定、待聊话题与跨端人物目录。"""
    checkout_id = uuid.uuid4().hex
    vis = normalize_visibility(payload.visibility, default="public")

    def _create(conn: sqlite3.Connection) -> None:
        ctx.sessions_repo.create_checkout(
            conn,
            checkout_id=checkout_id,
            branch_id=payload.branch_id,
            external_session_id=payload.external_session_id,
            kind=payload.kind,
            title=payload.title,
            platform=payload.platform,
            room_id=payload.room_id,
            segment_title=payload.segment_title,
            save_id=payload.save_id,
            visibility=vis,
        )
        ctx.sessions_repo.touch_branch(
            conn,
            branch_id=payload.branch_id,
            mode="session",
            status="live" if payload.kind == "live" else "rehearsal",
            detail={"checkout_id": checkout_id, "title": payload.title, "segment_title": payload.segment_title},
        )

    await ctx.db.run(_create)
    proj = await ctx.projector.build_context(
        branch_id=payload.branch_id,
        target_visibility=vis,
        save_id=payload.save_id,
        include_topics=True,
        include_directory=True,
        topic_limit=12,
    )
    if payload.kind == "live":
        ctx.broker.publish("presence_updated", {"branch_id": payload.branch_id, "live": True})
    return CheckoutResponse(
        checkout_id=checkout_id,
        brief_etag=proj.brief_etag,
        stable_brief=proj.stable_brief or "",
        live_context=proj.live_context,
        open_promises=[
            AnchorProjectionOut(
                id=a.id,
                anchor_type=a.anchor_type,
                content=a.content,
                status=a.status,
                source_branch=a.source_branch,
                save_id=a.save_id,
            )
            for a in proj.open_promises
        ],
        topic_cards=[
            TopicProjectionOut(
                id=t.id,
                title=t.title,
                summary=t.summary,
                reaction=t.reaction,
                source_branch=t.source_branch,
                consumed_branches=t.consumed_branches,
                transition_hint=t.transition_hint,
            )
            for t in proj.fresh_topics
        ],
        person_directory=[
            PersonDirectoryEntryOut(
                person_id=entry.person_id,
                platform=entry.platform,
                external_id=entry.external_id,
                display_name=entry.display_name,
                prompt_hint=entry.prompt_hint,
            )
            for entry in proj.person_directory
        ],
        save_brief=proj.save_brief,
    )


@router.post("/api/v1/sessions/{checkout_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat_session(
    checkout_id: str,
    payload: HeartbeatRequest,
    ctx: CtxDep,
) -> HeartbeatResponse:
    """刷新进行中会话的状态心跳，并同步回写本轮已使用的话题消费记录。"""

    def _op(conn: sqlite3.Connection) -> tuple[str | None, int]:
        sess = ctx.sessions_repo.update_heartbeat(
            conn,
            checkout_id=checkout_id,
            segment_title=payload.segment_title,
            save_id=payload.save_id,
            title=payload.title,
        )
        if sess is None:
            return None, 0
        consumed = 0
        for seed_id in payload.consumed_seed_ids:
            if ctx.topics_repo.mark_consumed(conn, topic_id=seed_id, branch_id=sess.branch_id) is not None:
                consumed += 1
        ctx.sessions_repo.touch_branch(
            conn,
            branch_id=sess.branch_id,
            mode="session",
            status="live" if (sess.active and sess.kind == "live") else "idle",
            detail={"checkout_id": checkout_id, "segment_title": sess.segment_title},
        )
        return sess.branch_id, consumed

    branch_id, consumed_count = await ctx.db.run(_op)
    if branch_id is None:
        raise HTTPException(status_code=404, detail=f"签出场次不存在: {checkout_id}")
    return HeartbeatResponse(checkout_id=checkout_id, consumed_count=consumed_count)


@router.post("/api/v1/sessions/{checkout_id}/checkin", response_model=CheckinResponse)
async def checkin_session(
    checkout_id: str,
    payload: CheckinRequest,
    ctx: CtxDep,
) -> CheckinResponse:
    """会话签入：单批写入本场纪要、约定状态、人物互动与存档更新，并触发封存整理。"""
    session = await ctx.db.run(lambda c: ctx.sessions_repo.get_checkout(c, checkout_id))
    if session is None:
        raise HTTPException(status_code=404, detail=f"签出场次不存在: {checkout_id}")

    if session.kind == "rehearsal" or payload.discarded:
        await ctx.db.run(lambda c: ctx.sessions_repo.finish_checkout(c, checkout_id=checkout_id))
        await ctx.db.run(
            lambda c: ctx.sessions_repo.touch_branch(c, branch_id=session.branch_id, mode="session", status="offline")
        )
        return CheckinResponse(checkout_id=checkout_id, ignored_rehearsal=True)

    # 先将本场 L1 纪要映射为幂等原始事件，同步拆出 L0 原件书库记录与 L2 实体关系事实
    # （玩家互动不再写入 L1 experience_events，避免空壳 greet/gift 噪音污染日内时间线）
    events_to_ingest: List[NewExperienceEvent] = []
    records_to_ingest: List[NewEpisodeRecord] = []
    target_save = payload.save_id or session.save_id

    for idx, line in enumerate(payload.archive_lines):
        line_save = line.save_id or target_save
        events_to_ingest.append(
            NewExperienceEvent(
                idempotency_key=f"checkin-{checkout_id}-arc-{idx}",
                branch_id=session.branch_id,
                occurred_at_ms=payload.ended_at_ms,
                kind="milestone",
                actor_role="self",
                visibility=session.visibility,
                context_scope=f"{session.platform}:{session.room_id}",
                summary=line.title,
                verbatim=line.verbatim,
                save_id=line_save,
                metadata={"highlight": line.highlight, "topic_title": line.title},
            )
        )
        records_to_ingest.append(
            NewEpisodeRecord(
                idempotency_key=f"checkin-{checkout_id}-arc-{idx}",
                episode_id=None,
                branch_id=session.branch_id,
                record_type="archive_line",
                actor_role="self",
                visibility=session.visibility,
                occurred_at_ms=payload.ended_at_ms,
                save_id=line_save,
                title=line.title,
                content=line.verbatim.strip() or line.title,
                verbatim=line.verbatim,
                metadata={"highlight": line.highlight},
            )
        )

    for rec in payload.archive_records:
        records_to_ingest.append(
            NewEpisodeRecord(
                idempotency_key=rec.idempotency_key,
                episode_id=None,
                branch_id=session.branch_id,
                record_type=rec.record_type,
                actor_role=rec.actor_role,
                visibility=normalize_visibility(rec.visibility, default=session.visibility),
                occurred_at_ms=rec.occurred_at_ms,
                save_id=rec.save_id or target_save,
                title=rec.title,
                content=rec.content,
                verbatim=rec.verbatim,
                metadata=rec.metadata,
            )
        )

    if payload.overview.strip():
        events_to_ingest.append(
            NewExperienceEvent(
                idempotency_key=f"checkin-{checkout_id}-overview",
                branch_id=session.branch_id,
                occurred_at_ms=payload.ended_at_ms,
                kind="milestone",
                actor_role="self",
                visibility=session.visibility,
                context_scope=f"{session.platform}:{session.room_id}",
                summary=payload.overview.strip(),
                verbatim=payload.overview.strip(),
                save_id=target_save,
                metadata={"session_overview": True},
            )
        )

    relations_to_ingest: List[KnowledgeRelationIn] = list(payload.social_facts)
    for idx, inter in enumerate(payload.interactions):
        fact_text = inter.fact.strip() or inter.summary.strip()
        if not fact_text:
            continue
        plat_lower = inter.platform.strip().lower()
        relations_to_ingest.append(
            KnowledgeRelationIn(
                idempotency_key=f"checkin-{checkout_id}-int-{idx}",
                subject=KnowledgeEntityIn(
                    entity_type="player" if plat_lower == "minecraft" else "person",
                    canonical_key=f"{plat_lower}:{inter.external_id.strip()}",
                    name=inter.display_name.strip() or inter.external_id.strip(),
                    platform=inter.platform,
                    external_id=inter.external_id,
                    save_id=target_save,
                    visibility=inter.visibility,
                    seen_at_ms=payload.ended_at_ms,
                ),
                predicate=inter.kind.strip() or "interacted_with",
                fact_text=fact_text,
                visibility=inter.visibility,
                save_id=target_save,
                occurred_at_ms=payload.ended_at_ms,
            )
        )

    def _apply_checkin(conn: sqlite3.Connection) -> tuple[int, int, int, int, List[tuple[str, int, str]]]:
        # 1. 登记或更新 L0 场次目录，支撑 4 小时内刚下播余温态与按场次召回锚点
        ep_row = ctx.episodes_repo.upsert_episode(
            conn,
            branch_id=session.branch_id,
            external_episode_id=session.external_session_id,
            kind="live",
            title=session.title,
            platform=session.platform,
            save_id=target_save,
            visibility=session.visibility,
            started_at_ms=session.started_at_ms,
            ended_at_ms=payload.ended_at_ms,
            overview=payload.overview.strip(),
            metadata={"checkout_id": checkout_id, "room_id": session.room_id},
        )
        to_embed: List[tuple[str, int, str]] = []
        if ep_row.overview:
            to_embed.append(("episode", ep_row.id, f"{ep_row.title} {ep_row.overview}".strip()))

        # 2. 写入 L1 场次主线事件
        ing = ctx.events_repo.ingest_batch(conn, events_to_ingest)

        # 3. 写入 L0 原件书库记录并挂接本场 episode_id
        bound_records = [
            NewEpisodeRecord(
                idempotency_key=r.idempotency_key,
                episode_id=ep_row.id,
                branch_id=r.branch_id,
                record_type=r.record_type,
                actor_role=r.actor_role,
                visibility=r.visibility,
                occurred_at_ms=r.occurred_at_ms,
                save_id=r.save_id,
                title=r.title,
                content=r.content,
                verbatim=r.verbatim,
                metadata=r.metadata,
            )
            for r in records_to_ingest
        ]
        rec_ing = ctx.episodes_repo.ingest_records(conn, bound_records)
        for rid in rec_ing.inserted_ids:
            saved_rec = ctx.episodes_repo.get_record_by_id(conn, rid)
            if saved_rec is not None:
                text = f"{saved_rec.title} {saved_rec.content}".strip()
                if text:
                    to_embed.append(("record", saved_rec.id, text))

        # 4. 本场熟人互动写入跨端人物履历与 L2 实体关系图谱（自动跳过遗忘账号）
        for inter in payload.interactions:
            ctx.identity.record_event_actors_sync(
                conn,
                event_id=None,
                branch_id=session.branch_id,
                visibility=inter.visibility,
                summary=inter.fact.strip() or inter.summary.strip(),
                occurred_at_ms=payload.ended_at_ms,
                actors=[
                    ActorRef(platform=inter.platform, external_id=inter.external_id, display_name=inter.display_name)
                ],
            )
        _ent_cnt, rel_accepted, _rel_dup, _skipped, rel_embed = ingest_knowledge_sync(
            ctx,
            conn,
            branch_id=session.branch_id,
            entities=(),
            relations=relations_to_ingest,
            default_episode_id=ep_row.id,
        )
        to_embed.extend(rel_embed)

        updated_promises = 0
        for p_idx, prom in enumerate(payload.promises):
            if prom.anchor_id is not None:
                res = ctx.anchors_repo.update(conn, prom.anchor_id, status=prom.status, note=prom.note)
                if res is not None:
                    updated_promises += 1
                continue
            existing = ctx.anchors_repo.find_by_content(conn, anchor_type="promise", content=prom.content)
            if existing is not None:
                ctx.anchors_repo.update(conn, existing.id, status=prom.status, note=prom.note or existing.note)
                updated_promises += 1
            elif prom.verbatim.strip():
                # 新增或更新的约定须先记录可核验的 self 原始事件
                ev_res = ctx.events_repo.ingest_batch(
                    conn,
                    [
                        NewExperienceEvent(
                            idempotency_key=f"checkin-{checkout_id}-prom-{p_idx}",
                            branch_id=session.branch_id,
                            occurred_at_ms=payload.ended_at_ms,
                            kind="promise_update",
                            actor_role="self",
                            visibility=session.visibility,
                            summary=prom.content,
                            verbatim=prom.verbatim,
                            save_id=prom.save_id or target_save,
                            metadata={
                                "promise_action": prom.status,
                                "promise_content": prom.content,
                                "note": prom.note,
                            },
                        )
                    ],
                )
                if ev_res.inserted_ids:
                    ctx.anchors_repo.create(
                        conn,
                        anchor_type="promise",
                        content=prom.content,
                        verbatim=prom.verbatim,
                        source_event_id=ev_res.inserted_ids[0],
                        branch_id=session.branch_id,
                        visibility=session.visibility,
                        save_id=prom.save_id or target_save,
                        status=prom.status,
                        note=prom.note,
                    )
                    updated_promises += 1

        for seed_id in payload.consumed_seed_ids:
            ctx.topics_repo.mark_consumed(conn, topic_id=seed_id, branch_id=session.branch_id)

        if target_save and (payload.save_summary.strip() or payload.overview.strip()):
            ctx.sessions_repo.upsert_save_state(
                conn,
                save_id=target_save,
                branch_id=session.branch_id,
                summary=payload.save_summary.strip() or payload.overview.strip(),
            )

        ctx.sessions_repo.finish_checkout(conn, checkout_id=checkout_id)
        ctx.sessions_repo.touch_branch(conn, branch_id=session.branch_id, mode="session", status="offline")
        return len(ing.inserted_ids), len(rec_ing.inserted_ids), rel_accepted, updated_promises, to_embed

    ingested_count, records_count, relations_count, updated_promises, to_embed = await ctx.db.run(_apply_checkin)
    if ctx.config.embedding.auto_index and to_embed:
        await ctx.retrieval.index_items(to_embed)
    # 签入后立即执行一轮封存整理，使常驻分支下次即可拉取到本场结论
    report = await ctx.bg_synthesizer.trigger_now(force_seal=True)
    ctx.broker.publish("presence_updated", {"branch_id": session.branch_id, "live": False})
    return CheckinResponse(
        checkout_id=checkout_id,
        ingested_events=ingested_count,
        ingested_records=records_count,
        ingested_relations=relations_count,
        updated_promises=updated_promises,
        sealed_slots=report.sealed_slots,
    )
