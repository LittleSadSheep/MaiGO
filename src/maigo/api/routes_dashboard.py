"""可视化看板与运维管理 HTTP 路由（``/api/v1/dashboard/*``）。"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from maigo.api.routes_sync import _parse_active_actors, ingest_events
from maigo.api.schemas import (
    AnchorCreateIn,
    AnchorPatchIn,
    BindTokenIssueIn,
    BindTokenRedeemIn,
    EventIn,
    ForgetIdentityIn,
    IdentityBindIn,
    IdentityUnbindIn,
    LifeArcCreateIn,
    LifeArcPatchIn,
    PersonCreateIn,
    QuickNoteIn,
    TopicCreateIn,
    TopicPatchIn,
)
from maigo.api.state import CtxDep, require_auth
from maigo.services.sanitizer import normalize_visibility
from maigo.services.synthesizer_rules import SLOT_LABELS, compute_day_and_slot
from maigo.storage import NewExperienceEvent

router = APIRouter(prefix="/api/v1/dashboard", dependencies=[Depends(require_auth)])

_QUICK_NOTE_KIND_MAP = {
    "browsed": "browsed",
    "note": "browsed",
    "speech": "speech",
    "game_moment": "milestone",
}


@router.get("/timeline")
async def get_timeline(
    ctx: CtxDep,
    day_key: Optional[str] = Query(default=None),
    limit_events: int = Query(default=60, ge=1, le=300),
) -> Dict[str, Any]:
    """获取指定作息日的生活编年各时段纪要、近期主线、在线场次与底层原始经历事件流。"""
    now = int(ctx.clock_ms())
    current_day, current_slot = compute_day_and_slot(
        now,
        day_rollover_hour=ctx.config.day_rollover_hour,
        timezone_offset_hours=ctx.config.timezone_offset_hours,
    )
    target_day = (day_key or "").strip() or current_day

    def _op(conn: sqlite3.Connection) -> Dict[str, Any]:
        slots = ctx.chronicle_repo.list_by_day(conn, day_key=target_day)
        arcs = ctx.arcs_repo.list_arcs(conn, statuses=("active", "paused"), limit=20)
        events = ctx.events_repo.list_recent(conn, limit=limit_events)
        active = ctx.sessions_repo.list_active_checkouts(conn)
        branches = ctx.sessions_repo.list_branches(conn)
        days = ctx.chronicle_repo.list_recent_days(conn, limit_days=14)
        return {
            "day_key": target_day,
            "current_day": current_day,
            "current_slot": current_slot,
            "available_days": days if target_day in days else [target_day, *days],
            "slots": [
                {
                    **asdict(slot),
                    "slot_label": SLOT_LABELS.get(slot.slot_key, slot.slot_key),
                }
                for slot in slots
            ],
            "arcs": [asdict(a) for a in arcs],
            "events": [asdict(ev) for ev in events],
            "active_sessions": [asdict(s) for s in active],
            "branches": [asdict(b) for b in branches],
        }

    return await ctx.db.run(_op)


@router.get("/status")
async def get_system_status(ctx: CtxDep) -> Dict[str, Any]:
    """获取数据库完整度、各子系统运行模式（LLM/规则、混合/稀疏）及近期热备份状态。"""
    return await ctx.maintenance.doctor_report(
        synthesizer_mode=ctx.synthesizer.mode,
        retrieval_mode=ctx.retrieval.mode,
    )


@router.post("/backup")
async def trigger_backup(ctx: CtxDep) -> Dict[str, Any]:
    """在看板立即执行一次 SQLite 在线热备份并清理超期旧备份。"""
    path = await ctx.maintenance.create_backup()
    size_bytes, backups = await asyncio.to_thread(lambda: (path.stat().st_size, ctx.maintenance.list_backups()))
    return {
        "ok": True,
        "filename": path.name,
        "size_bytes": size_bytes,
        "backups": backups,
    }


@router.get("/diary/export")
async def export_diary(
    ctx: CtxDep,
    day_key: Optional[str] = Query(default=None),
    days: int = Query(default=1, ge=1, le=30),
    target_visibility: str = Query(default="public"),
) -> Dict[str, Any]:
    """导出连续若干个作息日的脱敏 Markdown 生活手账。"""
    current_day, _ = compute_day_and_slot(
        int(ctx.clock_ms()),
        day_rollover_hour=ctx.config.day_rollover_hour,
        timezone_offset_hours=ctx.config.timezone_offset_hours,
    )
    effective_day = (day_key or "").strip() or current_day
    vis = normalize_visibility(target_visibility, default="public")
    markdown = await ctx.maintenance.export_diary_markdown(
        day_key=effective_day,
        days=days,
        target_visibility=vis,
    )
    return {
        "ok": True,
        "day_key": effective_day,
        "days": days,
        "target_visibility": vis,
        "markdown": markdown,
    }


@router.post("/quick-note")
async def submit_quick_note(payload: QuickNoteIn, ctx: CtxDep) -> Dict[str, Any]:
    """日常随手投喂一条见闻、灵感或游戏片段，按需立即跑一轮编年合成。"""
    now = int(ctx.clock_ms())
    clean_content = payload.content.strip()
    stored_kind = _QUICK_NOTE_KIND_MAP[payload.kind]
    metadata: Dict[str, Any] = {"manual_quick_note": True, "note_kind": payload.kind}
    if payload.kind == "game_moment":
        metadata["highlight"] = True

    event_in = EventIn(
        idempotency_key=f"quick-note-{now}-{uuid.uuid4().hex[:12]}",
        branch_id=payload.branch_id.strip() or "operator",
        occurred_at_ms=now,
        kind=stored_kind,
        actor_role="self",
        visibility=payload.visibility,
        summary=clean_content,
        verbatim=clean_content if payload.kind == "speech" else "",
        reaction=payload.reaction.strip(),
        save_id=payload.save_id,
        metadata=metadata,
    )
    ingest_res = await ingest_events(event_in, ctx=ctx)
    synthesis_dict: Optional[Dict[str, Any]] = None
    if payload.synthesize_now:
        report = await ctx.bg_synthesizer.trigger_now(force_seal=False)
        synthesis_dict = asdict(report)
    ctx.broker.publish("brief_updated", {"ts_ms": now})
    return {
        "ok": True,
        "event_id": ingest_res.inserted_ids[0] if ingest_res.inserted_ids else None,
        "synthesis": synthesis_dict,
    }


# ---- 近期主线（温层记忆） ----


@router.get("/arcs")
async def list_arcs(
    ctx: CtxDep,
    status: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """列出跨日持续的近期主线（按最近推进时间倒序）。"""
    statuses = [status] if status else None
    rows = await ctx.db.run(lambda c: ctx.arcs_repo.list_arcs(c, statuses=statuses, limit=100))
    return {"items": [asdict(item) for item in rows]}


@router.post("/arcs")
async def create_arc(payload: LifeArcCreateIn, ctx: CtxDep) -> Dict[str, Any]:
    """新建或延续一条近期主线。"""
    created = await ctx.db.run(
        lambda c: ctx.arcs_repo.upsert_or_create(
            c,
            title=payload.title,
            summary=payload.summary,
            public_summary=payload.public_summary,
            visibility=payload.visibility,
            save_id=payload.save_id,
            mood_hint=payload.mood_hint,
        )
    )
    ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
    return {"item": asdict(created)}


@router.patch("/arcs/{arc_id}")
async def patch_arc(
    arc_id: int,
    payload: LifeArcPatchIn,
    ctx: CtxDep,
) -> Dict[str, Any]:
    """更新近期主线的内容或生命周期状态（active/paused/completed/archived）。"""
    updated = await ctx.db.run(
        lambda c: ctx.arcs_repo.update(
            c,
            arc_id,
            title=payload.title,
            summary=payload.summary,
            public_summary=payload.public_summary,
            status=payload.status,
            mood_hint=payload.mood_hint,
        )
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"主线不存在: {arc_id}")
    ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
    return {"item": asdict(updated)}


@router.delete("/arcs/{arc_id}")
async def delete_arc(arc_id: int, ctx: CtxDep) -> Dict[str, Any]:
    """删除指定近期主线。"""
    ok = await ctx.db.run(lambda c: ctx.arcs_repo.delete(c, arc_id))
    if not ok:
        raise HTTPException(status_code=404, detail=f"主线不存在: {arc_id}")
    ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
    return {"ok": True}


@router.post("/synthesize")
async def trigger_synthesize(
    ctx: CtxDep,
    force_seal: bool = Query(default=False),
) -> Dict[str, Any]:
    """在看板上手动立即触发一轮编年合成。"""
    report = await ctx.bg_synthesizer.trigger_now(force_seal=force_seal)
    return {"ok": True, "report": asdict(report)}


# ---- 立场与承诺锚点 ----


@router.get("/anchors")
async def list_anchors(
    ctx: CtxDep,
    anchor_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """列出立场与承诺锚点。"""
    statuses = [status] if status else None
    rows = await ctx.db.run(
        lambda c: ctx.anchors_repo.list_anchors(c, anchor_type=anchor_type, statuses=statuses, limit=200)
    )
    return {"items": [asdict(item) for item in rows]}


@router.post("/anchors")
async def create_anchor(payload: AnchorCreateIn, ctx: CtxDep) -> Dict[str, Any]:
    """运营手工创建锚点（同步记一条 self 证据事件以保证可追溯）。"""

    def _op(conn: sqlite3.Connection) -> Dict[str, Any]:
        ev_res = ctx.events_repo.ingest_batch(
            conn,
            [
                NewExperienceEvent(
                    idempotency_key=f"manual-anchor-{int(ctx.clock_ms())}-{uuid.uuid4().hex[:12]}",
                    branch_id=payload.branch_id,
                    occurred_at_ms=int(ctx.clock_ms()),
                    kind="promise_update" if payload.anchor_type == "promise" else "speech",
                    actor_role="self",
                    visibility=payload.visibility,
                    summary=payload.content,
                    verbatim=payload.verbatim,
                    save_id=payload.save_id,
                    metadata={"manual_operator": True},
                )
            ],
        )
        src_id = ev_res.inserted_ids[0] if ev_res.inserted_ids else None
        created = ctx.anchors_repo.create(
            conn,
            anchor_type=payload.anchor_type,
            content=payload.content,
            verbatim=payload.verbatim,
            source_event_id=src_id,
            branch_id=payload.branch_id,
            visibility=payload.visibility,
            save_id=payload.save_id,
            note=payload.note,
        )
        return {"item": asdict(created)}

    res = await ctx.db.run(_op)
    ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
    return res


@router.patch("/anchors/{anchor_id}")
async def patch_anchor(
    anchor_id: int,
    payload: AnchorPatchIn,
    ctx: CtxDep,
) -> Dict[str, Any]:
    """编辑锚点内容、标已兑现或判误记作废。"""
    updated = await ctx.db.run(
        lambda c: ctx.anchors_repo.update(
            c, anchor_id, content=payload.content, status=payload.status, note=payload.note
        )
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"锚点不存在: {anchor_id}")
    ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
    return {"item": asdict(updated)}


@router.delete("/anchors/{anchor_id}")
async def delete_anchor(anchor_id: int, ctx: CtxDep) -> Dict[str, Any]:
    """删除指定锚点。"""
    ok = await ctx.db.run(lambda c: ctx.anchors_repo.delete(c, anchor_id))
    if not ok:
        raise HTTPException(status_code=404, detail=f"锚点不存在: {anchor_id}")
    ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
    return {"ok": True}


# ---- 鲜活话头池 ----


@router.get("/topics")
async def list_topics(
    ctx: CtxDep,
    status: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """列出鲜活话头及消费分支轨迹。"""
    rows = await ctx.db.run(lambda c: ctx.topics_repo.list_all(c, status=status, limit=200))
    return {"items": [asdict(item) for item in rows]}


@router.post("/topics")
async def create_topic(payload: TopicCreateIn, ctx: CtxDep) -> Dict[str, Any]:
    """运营人工投喂一条日常见闻话头进池子。"""
    ttl = payload.ttl_ms or ctx.config.synthesizer.topic_ttl_ms
    expires_at = int(ctx.clock_ms()) + ttl
    created = await ctx.db.run(
        lambda c: ctx.topics_repo.create(
            c,
            title=payload.title,
            summary=payload.summary,
            reaction=payload.reaction,
            source_branch=payload.source_branch,
            source_event_id=None,
            visibility=payload.visibility,
            expires_at_ms=expires_at,
        )
    )
    return {"item": asdict(created)}


@router.patch("/topics/{topic_id}")
async def patch_topic(
    topic_id: int,
    payload: TopicPatchIn,
    ctx: CtxDep,
) -> Dict[str, Any]:
    """更新或归档话头。"""
    updated = await ctx.db.run(
        lambda c: ctx.topics_repo.update(
            c,
            topic_id,
            title=payload.title,
            summary=payload.summary,
            reaction=payload.reaction,
            status=payload.status,
        )
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"话头不存在: {topic_id}")
    return {"item": asdict(updated)}


@router.delete("/topics/{topic_id}")
async def delete_topic(topic_id: int, ctx: CtxDep) -> Dict[str, Any]:
    """删除话头。"""
    ok = await ctx.db.run(lambda c: ctx.topics_repo.delete(c, topic_id))
    if not ok:
        raise HTTPException(status_code=404, detail=f"话头不存在: {topic_id}")
    return {"ok": True}


# ---- 跨端人物图谱 ----


@router.get("/persons")
async def list_persons(ctx: CtxDep) -> Dict[str, Any]:
    """列出人物图谱及关联平台账号与待审候选。"""
    people = await ctx.identity.list_persons_with_identities()
    candidates = await ctx.identity.list_candidates(status="pending")
    return {
        "persons": [
            {
                **asdict(item.person),
                "identities": [asdict(ident) for ident in item.identities],
            }
            for item in people
        ],
        "candidates": [asdict(cand) for cand in candidates],
    }


@router.post("/persons")
async def create_person(payload: PersonCreateIn, ctx: CtxDep) -> Dict[str, Any]:
    """创建统一人物档案。"""
    person = await ctx.identity.create_person(
        display_name=payload.display_name,
        notes=payload.notes,
        visibility_tier=payload.visibility_tier,
    )
    return {"person": asdict(person)}


@router.delete("/persons/{person_id}")
async def delete_person(person_id: int, ctx: CtxDep) -> Dict[str, Any]:
    """删除人物档案。"""
    ok = await ctx.identity.delete_person(person_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"人物不存在: {person_id}")
    return {"ok": True}


@router.post("/persons/bind")
async def bind_person_identity(payload: IdentityBindIn, ctx: CtxDep) -> Dict[str, Any]:
    """显式绑定平台账号到人物。"""
    try:
        ident = await ctx.identity.bind_identity(
            person_id=payload.person_id,
            platform=payload.platform,
            external_id=payload.external_id,
            external_name=payload.external_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"identity": asdict(ident)}


@router.post("/persons/unbind")
async def unbind_person_identity(payload: IdentityUnbindIn, ctx: CtxDep) -> Dict[str, Any]:
    """解除平台账号绑定。"""
    ok = await ctx.identity.unbind_identity(platform=payload.platform, external_id=payload.external_id)
    return {"ok": ok}


@router.post("/persons/bind-tokens")
async def issue_bind_token(payload: BindTokenIssueIn, ctx: CtxDep) -> Dict[str, Any]:
    """签发用户自助绑定口令。"""
    try:
        tok = await ctx.identity.issue_bind_token(
            person_id=payload.person_id,
            platform_hint=payload.platform_hint,
            ttl_ms=payload.ttl_ms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"token": asdict(tok)}


@router.post("/persons/bind-tokens/redeem")
async def redeem_bind_token(payload: BindTokenRedeemIn, ctx: CtxDep) -> Dict[str, Any]:
    """核销自助绑定口令。"""
    try:
        ident = await ctx.identity.redeem_bind_token(
            token=payload.token,
            platform=payload.platform,
            external_id=payload.external_id,
            external_name=payload.external_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"identity": asdict(ident)}


@router.post("/persons/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: int, ctx: CtxDep) -> Dict[str, Any]:
    """批准疑似同人候选并完成绑定。"""
    resolved = await ctx.identity.resolve_candidate(candidate_id=candidate_id, approve=True)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"候选不存在: {candidate_id}")
    return {"candidate": asdict(resolved)}


@router.post("/persons/candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: int, ctx: CtxDep) -> Dict[str, Any]:
    """驳回疑似同人候选。"""
    resolved = await ctx.identity.resolve_candidate(candidate_id=candidate_id, approve=False)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"候选不存在: {candidate_id}")
    return {"candidate": asdict(resolved)}


@router.get("/persons/{person_id}/preview")
async def preview_person_projection(
    person_id: int,
    ctx: CtxDep,
    target_visibility: str = Query(default="public"),
) -> Dict[str, Any]:
    """按可见性等级预览某位熟人送给模型看的脱敏卡片。"""
    card = await ctx.identity.preview_person(person_id=person_id, target_visibility=target_visibility)
    if card is None:
        raise HTTPException(status_code=404, detail=f"人物不存在: {person_id}")
    return {"card": asdict(card)}


@router.post("/persons/forget")
async def forget_person_identity(payload: ForgetIdentityIn, ctx: CtxDep) -> Dict[str, Any]:
    """执行跨端遗忘，永久停止记录并擦除关联互动。"""
    res = await ctx.identity.forget_identity(
        platform=payload.platform,
        external_id=payload.external_id,
        wipe_person=payload.wipe_person,
    )
    ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
    return {"ok": True, **res}


# ---- 分支监控与联调演练台 ----


@router.get("/branches")
async def list_branches(ctx: CtxDep) -> Dict[str, Any]:
    """列出各接入分支当前状态与近期签出场次。"""

    def _op(conn: sqlite3.Connection) -> Dict[str, Any]:
        return {
            "branches": [asdict(b) for b in ctx.sessions_repo.list_branches(conn)],
            "recent_sessions": [asdict(s) for s in ctx.sessions_repo.list_recent_checkouts(conn, limit=10)],
        }

    return await ctx.db.run(_op)


@router.post("/simulate-event")
async def simulate_event(
    payload: EventIn,
    ctx: CtxDep,
    synthesize_now: bool = Query(default=True),
    force_seal: bool = Query(default=True),
) -> Dict[str, Any]:
    """联调演练台：投递一条模拟事件，并可立即运行一轮合成查看效果。"""
    ingest_res = await ingest_events(payload, ctx=ctx)
    report_dict: Optional[Dict[str, Any]] = None
    if synthesize_now:
        report = await ctx.bg_synthesizer.trigger_now(force_seal=force_seal)
        report_dict = asdict(report)
    return {"ingest": ingest_res.model_dump(), "synthesis": report_dict}


@router.get("/preview-view")
async def preview_view(
    ctx: CtxDep,
    branch_id: str = Query(default="amaidesu"),
    target_visibility: str = Query(default="public"),
    save_id: Optional[str] = Query(default=None),
    active_actors: str = Query(default=""),
) -> Dict[str, Any]:
    """联调演练台：实时预览指定分支与场合下模型将收到的完整慢快双层文本。"""
    proj = await ctx.projector.build_context(
        branch_id=branch_id,
        target_visibility=target_visibility,
        save_id=save_id,
        active_actors=_parse_active_actors(active_actors),
        include_topics=True,
        include_directory=True,
    )
    return {
        "brief_etag": proj.brief_etag,
        "stable_brief": proj.stable_brief or "",
        "live_context": proj.live_context,
        "actor_cards": [asdict(c) for c in proj.actor_cards],
        "fresh_topics": [asdict(t) for t in proj.fresh_topics],
        "open_promises": [asdict(p) for p in proj.open_promises],
        "save_brief": proj.save_brief,
    }
