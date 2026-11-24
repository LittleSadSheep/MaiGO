"""常驻接入端流式同步路由（健康检查、事件上报、视图查询、回忆检索与 SSE 通知）。"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from maigo.core.privacy import normalize_visibility
from maigo.runtime import get_logger
from maigo.server.auth import CtxDep, require_auth
from maigo.server.schemas import (
    ActiveSessionOut,
    ActorCardOut,
    AnchorProjectionOut,
    ContextViewOut,
    EventIn,
    EventsBatchIn,
    EventsIngestOut,
    EventsPayloadIn,
    TopicConsumeIn,
    TopicProjectionOut,
)
from maigo.storage import ActorRef, NewExperienceEvent
from maigo.storage.schema import SCHEMA_VERSION

logger = get_logger("server.sync")

router = APIRouter(dependencies=[Depends(require_auth)])
public_router = APIRouter()


@public_router.get("/health")
async def health_check(ctx: CtxDep) -> Dict[str, Any]:
    """健康检查端点（无需令牌，供进程守护与负载均衡探活）。"""
    return {
        "ok": True,
        "service": "maigo",
        "schema_version": SCHEMA_VERSION,
        "synthesizer_mode": ctx.synthesizer.mode,
        "retrieval_mode": ctx.retrieval.mode,
    }


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
    """按关键词检索跨端经历与立场锚点（HTTP 接口，与 MCP ``maigo_recall_life`` 同源）。"""
    vis = normalize_visibility(target_visibility, default="public")
    text = await ctx.projector.recall_life(query=query.strip(), days=days, limit=limit, target_visibility=vis)
    return {"ok": True, "text": text}


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
