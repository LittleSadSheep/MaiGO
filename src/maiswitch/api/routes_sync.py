"""核心业务同步路由（流式推拉、签出/心跳/签入与 SSE 变更通知）。"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from maiswitch.api.schemas import (
    ActiveSessionOut,
    ActorCardOut,
    AnchorProjectionOut,
    CheckinRequest,
    CheckinResponse,
    CheckoutRequest,
    CheckoutResponse,
    ContextViewOut,
    EventIn,
    EventsBatchIn,
    EventsIngestOut,
    EventsPayloadIn,
    HeartbeatRequest,
    HeartbeatResponse,
    PersonDirectoryEntryOut,
    TopicConsumeIn,
    TopicProjectionOut,
)
from maiswitch.api.state import CtxDep, require_auth
from maiswitch.runtime import get_logger
from maiswitch.services.sanitizer import normalize_visibility
from maiswitch.storage import ActorRef, NewExperienceEvent

logger = get_logger("api.sync")

router = APIRouter(dependencies=[Depends(require_auth)])
public_router = APIRouter()


@public_router.get("/health")
async def health_check() -> Dict[str, Any]:
    """健康检查端点（无需令牌，供进程守护与负载均衡探活）。"""
    return {"ok": True, "service": "maiswitch"}


def _default_visibility(ev: EventIn) -> str:
    if ev.visibility is not None:
        return ev.visibility
    scope = ev.context_scope.lower()
    if "private" in scope or "dm" in scope:
        return "private"
    if "group" in scope:
        return "group"
    return "public"


def _to_new_event(ev: EventIn) -> NewExperienceEvent:
    return NewExperienceEvent(
        idempotency_key=ev.idempotency_key,
        branch_id=ev.branch_id,
        occurred_at_ms=ev.occurred_at_ms,
        kind=ev.kind,
        actor_role=ev.actor_role,
        visibility=_default_visibility(ev),
        context_scope=ev.context_scope,
        actors=[
            ActorRef(
                platform=a.platform,
                external_id=a.external_id,
                display_name=a.display_name,
            )
            for a in ev.actors
        ],
        summary=ev.summary,
        verbatim=ev.verbatim,
        reaction=ev.reaction,
        save_id=ev.save_id,
        metadata=ev.metadata,
    )


def _parse_active_actors(raw: str) -> List[Tuple[str, str]]:
    """解析逗号分隔的 ``platform:external_id`` 列表，跳过空段与非法格式。"""
    result: List[Tuple[str, str]] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if ":" not in item:
            continue
        platform, external_id = item.split(":", 1)
        if platform.strip() and external_id.strip():
            result.append((platform.strip(), external_id.strip()))
    return result


@router.post("/api/v1/events", response_model=EventsIngestOut)
async def ingest_events(payload: EventsPayloadIn, ctx: CtxDep) -> EventsIngestOut:
    """接收单条或批量统一经历事件，幂等落库并触发人物互动登记与后台整理唤醒。"""
    items: List[EventIn] = payload.events if isinstance(payload, EventsBatchIn) else [payload]
    if not items:
        return EventsIngestOut(accepted=0, deduplicated=0, inserted_ids=[])

    # 先执行显式遗忘请求，确保在入库同批前就拦截后续写入
    for item in items:
        if item.kind == "forget_request":
            for actor in item.actors:
                await ctx.identity.forget_identity(platform=actor.platform, external_id=actor.external_id)

    new_events = [_to_new_event(item) for item in items]

    def _op(conn: sqlite3.Connection) -> tuple[List[int], int, int]:
        res = ctx.events_repo.ingest_batch(conn, new_events)
        branches_seen: set[str] = set()
        for eid in res.inserted_ids:
            ev = ctx.events_repo.get_by_id(conn, eid)
            if ev is None:
                continue
            branches_seen.add(ev.branch_id)
            if ev.kind != "forget_request" and ev.actors:
                ctx.identity.record_event_actors_sync(
                    conn,
                    event_id=ev.id,
                    branch_id=ev.branch_id,
                    visibility=ev.visibility,
                    summary=ev.summary,
                    occurred_at_ms=ev.occurred_at_ms,
                    actors=ev.actors,
                )
            if ev.save_id and ev.kind in ("milestone", "interaction"):
                ctx.sessions_repo.upsert_save_state(
                    conn,
                    save_id=ev.save_id,
                    branch_id=ev.branch_id,
                    summary=ev.summary,
                )
        for item in items:
            branches_seen.add(item.branch_id)
        for branch_id in branches_seen:
            ctx.sessions_repo.touch_branch(conn, branch_id=branch_id, mode="streaming", status="online")
        pending = ctx.events_repo.count_unsynthesized(conn)
        return res.inserted_ids, res.deduplicated_count, pending

    inserted_ids, deduplicated, pending = await ctx.db.run(_op)
    ctx.bg_synthesizer.notify_new_events(pending)
    if inserted_ids:
        ctx.broker.publish("events_ingested", {"count": len(inserted_ids), "ts_ms": int(ctx.clock_ms())})
    return EventsIngestOut(accepted=len(inserted_ids), deduplicated=deduplicated, inserted_ids=inserted_ids)


@router.get("/api/v1/views/context", response_model=ContextViewOut)
async def get_context_view(
    ctx: CtxDep,
    branch_id: str = Query(..., min_length=1),
    target_visibility: str = Query(default="public"),
    save_id: Optional[str] = Query(default=None),
    active_actors: str = Query(default=""),
    if_brief_etag: Optional[str] = Query(default=None),
    include_topics: bool = Query(default=True),
    topic_limit: int = Query(default=6, ge=1, le=30),
) -> ContextViewOut:
    """拉取按场合与分支裁剪的快慢分层视图（支持 etag 命中省略慢层）。"""
    await ctx.db.run(
        lambda c: ctx.sessions_repo.touch_branch(c, branch_id=branch_id, mode="streaming", status="online")
    )
    actors = _parse_active_actors(active_actors)
    proj = await ctx.projector.build_context(
        branch_id=branch_id,
        target_visibility=target_visibility,
        save_id=save_id,
        active_actors=actors,
        if_brief_etag=if_brief_etag,
        include_topics=include_topics,
        include_directory=False,
        topic_limit=topic_limit,
    )
    return ContextViewOut(
        brief_etag=proj.brief_etag,
        stable_unchanged=proj.stable_unchanged,
        stable_brief=proj.stable_brief,
        live_context=proj.live_context,
        actor_cards=[
            ActorCardOut(
                person_id=card.person_id,
                display_name=card.display_name,
                hint=card.hint,
                platforms=card.platforms,
            )
            for card in proj.actor_cards
        ],
        fresh_topics=[
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
        active_sessions=[
            ActiveSessionOut(
                checkout_id=s.checkout_id,
                branch_id=s.branch_id,
                external_session_id=s.external_session_id,
                title=s.title,
                platform=s.platform,
                room_id=s.room_id,
                segment_title=s.segment_title,
                save_id=s.save_id,
                started_at_ms=s.started_at_ms,
                last_heartbeat_ms=s.last_heartbeat_ms,
            )
            for s in proj.active_sessions
        ],
        save_brief=proj.save_brief,
    )


@router.post("/api/v1/topics/{topic_id}/consume")
async def consume_topic(
    topic_id: int,
    payload: TopicConsumeIn,
    ctx: CtxDep,
) -> Dict[str, Any]:
    """显式记录某分支已聊过某条话头，防止后续在其他分支机械复读。"""
    updated = await ctx.db.run(
        lambda c: ctx.topics_repo.mark_consumed(c, topic_id=topic_id, branch_id=payload.branch_id)
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"话头不存在: topic_id={topic_id}")
    return {"ok": True, "topic_id": updated.id, "consumed_branches": updated.consumed_branches}


@router.get("/api/v1/views/recall")
async def recall_life_view(
    ctx: CtxDep,
    query: str = Query(..., min_length=1, max_length=200),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=5, ge=1, le=20),
    target_visibility: str = Query(default="public"),
) -> Dict[str, Any]:
    """按关键词检索跨端经历与立场锚点（HTTP 接口，与 MCP ``switch_recall_life`` 同源）。"""
    vis = normalize_visibility(target_visibility, default="public")
    text = await ctx.projector.recall_life(query=query.strip(), days=days, limit=limit, target_visibility=vis)
    return {"ok": True, "text": text}


@router.post("/api/v1/sessions/checkout", response_model=CheckoutResponse)
async def checkout_session(
    payload: CheckoutRequest,
    ctx: CtxDep,
) -> CheckoutResponse:
    """场次型分支启动签出：一次性返回今日慢层简报、跨端承诺、话头卡与熟人目录。"""
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
    """刷新进行中场次的心跳状态，并同步回写本轮选用的话头消费。"""

    def _op(conn: sqlite3.Connection) -> tuple[Optional[str], int]:
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
    """场次下播签入：单批写入已整理的档案、承诺变更、熟人互动与存档收尾，并触发封存。"""
    session = await ctx.db.run(lambda c: ctx.sessions_repo.get_checkout(c, checkout_id))
    if session is None:
        raise HTTPException(status_code=404, detail=f"签出场次不存在: {checkout_id}")

    if session.kind == "rehearsal" or payload.discarded:
        await ctx.db.run(lambda c: ctx.sessions_repo.finish_checkout(c, checkout_id=checkout_id))
        await ctx.db.run(
            lambda c: ctx.sessions_repo.touch_branch(c, branch_id=session.branch_id, mode="session", status="offline")
        )
        return CheckinResponse(checkout_id=checkout_id, ignored_rehearsal=True)

    # 先把本场结构化成品映射成幂等标准事件再单次入库
    events_to_ingest: List[NewExperienceEvent] = []
    for idx, line in enumerate(payload.archive_lines):
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
                save_id=line.save_id or payload.save_id or session.save_id,
                metadata={"highlight": line.highlight, "topic_title": line.title},
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
                save_id=payload.save_id or session.save_id,
                metadata={"session_overview": True},
            )
        )

    for idx, inter in enumerate(payload.interactions):
        events_to_ingest.append(
            NewExperienceEvent(
                idempotency_key=f"checkin-{checkout_id}-int-{idx}",
                branch_id=session.branch_id,
                occurred_at_ms=payload.ended_at_ms,
                kind="interaction",
                actor_role="peer",
                visibility=inter.visibility,
                context_scope=f"{session.platform}:{session.room_id}",
                actors=[
                    ActorRef(platform=inter.platform, external_id=inter.external_id, display_name=inter.display_name)
                ],
                summary=inter.summary,
                save_id=payload.save_id or session.save_id,
            )
        )

    def _apply_checkin(conn: sqlite3.Connection) -> tuple[int, int]:
        ing = ctx.events_repo.ingest_batch(conn, events_to_ingest)
        for eid in ing.inserted_ids:
            ev = ctx.events_repo.get_by_id(conn, eid)
            if ev is not None and ev.actors:
                ctx.identity.record_event_actors_sync(
                    conn,
                    event_id=ev.id,
                    branch_id=ev.branch_id,
                    visibility=ev.visibility,
                    summary=ev.summary,
                    occurred_at_ms=ev.occurred_at_ms,
                    actors=ev.actors,
                )

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
                # 新立或新记下的承诺必须先落一条可核验的 self 证据事件
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
                            save_id=prom.save_id or payload.save_id or session.save_id,
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
                        save_id=prom.save_id or payload.save_id or session.save_id,
                        status=prom.status,
                        note=prom.note,
                    )
                    updated_promises += 1

        for seed_id in payload.consumed_seed_ids:
            ctx.topics_repo.mark_consumed(conn, topic_id=seed_id, branch_id=session.branch_id)

        target_save = payload.save_id or session.save_id
        if target_save and (payload.save_summary.strip() or payload.overview.strip()):
            ctx.sessions_repo.upsert_save_state(
                conn,
                save_id=target_save,
                branch_id=session.branch_id,
                summary=payload.save_summary.strip() or payload.overview.strip(),
            )

        ctx.sessions_repo.finish_checkout(conn, checkout_id=checkout_id)
        ctx.sessions_repo.touch_branch(conn, branch_id=session.branch_id, mode="session", status="offline")
        return len(ing.inserted_ids), updated_promises

    ingested_count, updated_promises = await ctx.db.run(_apply_checkin)
    # 下播签入后立即封存今日已发生时段，确保常驻群聊端下次立即同步到本场结果
    report = await ctx.bg_synthesizer.trigger_now(force_seal=True)
    ctx.broker.publish("presence_updated", {"branch_id": session.branch_id, "live": False})
    return CheckinResponse(
        checkout_id=checkout_id,
        ingested_events=ingested_count,
        updated_promises=updated_promises,
        sealed_slots=report.sealed_slots,
    )


@router.get("/api/v1/stream/notifications")
async def stream_notifications(ctx: CtxDep) -> StreamingResponse:
    """SSE 实时通知流：推送简报变更与场次在线状态变化。"""
    queue = ctx.broker.subscribe()

    async def _gen() -> AsyncIterator[str]:
        try:
            yield 'event: connected\ndata: {"ok": true}\n\n'
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            ctx.broker.unsubscribe(queue)

    return StreamingResponse(_gen(), media_type="text/event-stream")
