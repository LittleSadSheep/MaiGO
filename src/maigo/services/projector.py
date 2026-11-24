"""快慢分层上下文视图投影器与跨端回忆检索服务。

职责边界：
1. 慢层 ``stable_brief``：只含昨日余温 + 今日已封存时段 + 生效中立场与承诺，绝不含跳动的相对时间词，
   在未发生跨段封存或承诺变动时逐字节一致，配套 ``brief_etag`` 保住接入端前缀缓存；
2. 快层 ``live_context``：当前跨端活动状态（如正在开播、近期刚下播）+ 当前未封存时段的实时进展；
3. 话头消费附注：若某话头已在其他分支聊过（如 ``maibot``），投影给新分支时自动附带自然过渡注记。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from maigo.runtime import ClockMs, now_ms
from maigo.services.identity import ActorCardProjection, IdentityService, PersonDirectoryEntry
from maigo.services.sanitizer import (
    allowed_visibilities,
    describe_visibility_scope,
    normalize_visibility,
    sanitize_for_model,
)
from maigo.services.synthesizer_rules import SLOT_LABELS, compute_day_and_slot
from maigo.storage.db import MaiGODatabase
from maigo.storage.models import CheckoutSessionRow, DailyChronicleRow, PersonaAnchorRow
from maigo.storage.repos import (
    DailyChronicleRepo,
    ExperienceEventRepo,
    PersonaAnchorRepo,
    PersonRepo,
    SessionRepo,
    TopicSeedRepo,
)

_BRANCH_LABELS = {
    "maibot": "QQ群",
    "amaidesu": "直播间",
    "bilibili_ops": "B站",
    "x_ops": "X",
    "mc_runner": "Minecraft",
}


@dataclass(frozen=True)
class TopicProjection:
    """投影给接入端的鲜活话头条目（含消费轨迹附注）。"""

    id: int
    title: str
    summary: str
    reaction: str
    source_branch: str
    consumed_branches: List[str]
    transition_hint: str


@dataclass(frozen=True)
class AnchorProjection:
    """投影给接入端的脱敏立场或承诺条目。"""

    id: int
    anchor_type: str
    content: str
    status: str
    source_branch: str
    save_id: Optional[str]


@dataclass(frozen=True)
class ContextProjection:
    """统一上下文投影结果（供流式视图与签出视图复用）。"""

    brief_etag: str
    stable_brief: Optional[str]
    stable_unchanged: bool
    live_context: str
    actor_cards: List[ActorCardProjection]
    fresh_topics: List[TopicProjection]
    open_promises: List[AnchorProjection]
    active_sessions: List[CheckoutSessionRow]
    save_brief: str = ""
    person_directory: List[PersonDirectoryEntry] = field(default_factory=list)


def _branch_label(branch_id: str) -> str:
    clean = branch_id.strip().lower()
    if clean in _BRANCH_LABELS:
        return _BRANCH_LABELS[clean]
    if "bili" in clean:
        return "B站"
    if "maibot" in clean or "qq" in clean:
        return "群聊"
    if "amaidesu" in clean or "live" in clean:
        return "直播间"
    return branch_id


def _topic_transition_hint(consumed_branches: Sequence[str], target_branch: str) -> str:
    """当话头已在其他分支聊过时，生成供大模型自然过渡的消费注记。"""
    others = [_branch_label(b) for b in consumed_branches if b and b != target_branch]
    if not others:
        return ""
    joined = "、".join(dict.fromkeys(others))
    target_label = _branch_label(target_branch)
    return f"今天已经在{joined}提过一嘴，{target_label}这边还没细聊"


def _chronicle_body_for_visibility(row: DailyChronicleRow, target_visibility: str) -> str:
    level = normalize_visibility(target_visibility, default="public")
    if level == "public":
        return row.public_body.strip()
    if level == "group":
        return row.group_body.strip()
    return row.body.strip()


class ViewProjector:
    """按目标分支、可见性与在场人物构建脱敏记忆投影。"""

    def __init__(
        self,
        db: MaiGODatabase,
        *,
        identity: IdentityService,
        day_rollover_hour: int = 4,
        timezone_offset_hours: int = 8,
        recent_window_ms: int = 1_800_000,
        clock_ms: ClockMs = now_ms,
    ) -> None:
        self._db = db
        self._identity = identity
        self._rollover_hour = day_rollover_hour
        self._tz_offset = timezone_offset_hours
        self._recent_window_ms = recent_window_ms
        self._clock_ms = clock_ms
        self._chronicle = DailyChronicleRepo(clock_ms=clock_ms)
        self._anchors = PersonaAnchorRepo(clock_ms=clock_ms)
        self._topics = TopicSeedRepo(clock_ms=clock_ms)
        self._events = ExperienceEventRepo(clock_ms=clock_ms)
        self._persons = PersonRepo(clock_ms=clock_ms)
        self._sessions = SessionRepo(clock_ms=clock_ms)

    async def build_context(
        self,
        *,
        branch_id: str,
        target_visibility: str = "public",
        save_id: Optional[str] = None,
        active_actors: Sequence[Tuple[str, str]] = (),
        if_brief_etag: Optional[str] = None,
        include_topics: bool = True,
        include_directory: bool = False,
        topic_limit: int = 6,
    ) -> ContextProjection:
        """为流式分支或场次签出分支渲染慢快分层视图。"""
        vis_level = normalize_visibility(target_visibility, default="public")
        vis_allowed = allowed_visibilities(vis_level)
        now = int(self._clock_ms())
        current_day, _ = compute_day_and_slot(
            now, day_rollover_hour=self._rollover_hour, timezone_offset_hours=self._tz_offset
        )

        def _op(conn: sqlite3.Connection) -> ContextProjection:
            all_ext_ids = [item.external_id for item in self._persons.list_all_identities(conn)]

            # 1. 慢层组装：仅读昨日余温 + 今日已封存时段 + 活跃立场/未兑现承诺
            sealed_rows = self._chronicle.list_by_day(conn, day_key=current_day, status="sealed")
            stances = self._anchors.list_anchors(
                conn, anchor_type="stance", statuses=["active"], visibilities=vis_allowed, save_id=save_id, limit=12
            )
            promises = self._anchors.list_anchors(
                conn, anchor_type="promise", statuses=["open"], visibilities=vis_allowed, save_id=save_id, limit=12
            )
            stable_text = self._render_stable_brief(
                sealed_rows=sealed_rows,
                stances=stances,
                promises=promises,
                target_visibility=vis_level,
                known_external_ids=all_ext_ids,
            )
            etag = hashlib.sha256(stable_text.encode("utf-8")).hexdigest()[:16]
            unchanged = bool(if_brief_etag and if_brief_etag.strip() == etag)

            # 2. 快层组装：在线正式直播状态 + 当前未封存窗口近期动态
            active_sessions = self._sessions.list_active_checkouts(conn)
            open_rows = self._chronicle.list_by_day(conn, day_key=current_day, status="open")
            recent_events = self._events.list_recent(
                conn,
                since_ms=now - self._recent_window_ms,
                visibilities=vis_allowed,
                limit=6,
            )
            live_text = self._render_live_context(
                branch_id=branch_id,
                active_sessions=active_sessions,
                open_rows=open_rows,
                recent_events=recent_events,
                target_visibility=vis_level,
                known_external_ids=all_ext_ids,
            )

            # 3. 熟人卡片与全量目录（已脱敏、已排除遗忘名单）
            actor_cards = (
                self._identity.build_actor_cards_sync(conn, actors=active_actors, target_visibility=vis_level)
                if active_actors
                else []
            )
            directory = (
                self._identity.build_person_directory_sync(conn, target_visibility=vis_level)
                if include_directory
                else []
            )

            # 4. 鲜活话头池与承诺列表
            topics_proj: List[TopicProjection] = []
            if include_topics:
                raw_topics = self._topics.list_available_for_branch(
                    conn,
                    branch_id=branch_id,
                    visibilities=vis_allowed,
                    now_override_ms=now,
                    limit=topic_limit,
                )
                for item in raw_topics:
                    topics_proj.append(
                        TopicProjection(
                            id=item.id,
                            title=sanitize_for_model(item.title, all_ext_ids),
                            summary=sanitize_for_model(item.summary, all_ext_ids),
                            reaction=sanitize_for_model(item.reaction, all_ext_ids),
                            source_branch=item.source_branch,
                            consumed_branches=list(item.consumed_branches),
                            transition_hint=_topic_transition_hint(item.consumed_branches, branch_id),
                        )
                    )

            promise_projs = [
                AnchorProjection(
                    id=p.id,
                    anchor_type=p.anchor_type,
                    content=sanitize_for_model(p.content, all_ext_ids),
                    status=p.status,
                    source_branch=p.branch_id,
                    save_id=p.save_id,
                )
                for p in promises
            ]

            save_brief = ""
            if save_id:
                save_row = self._sessions.get_save_state(conn, save_id)
                if save_row is not None and save_row.summary:
                    clean_save = sanitize_for_model(save_row.summary, all_ext_ids)
                    src_label = _branch_label(save_row.last_branch_id) if save_row.last_branch_id else "此前"
                    save_brief = f"当前存档最近记录（来自{src_label}）：{clean_save}"

            return ContextProjection(
                brief_etag=etag,
                stable_brief=None if unchanged else stable_text,
                stable_unchanged=unchanged,
                live_context=live_text,
                actor_cards=actor_cards,
                fresh_topics=topics_proj,
                open_promises=promise_projs,
                active_sessions=active_sessions,
                save_brief=save_brief,
                person_directory=directory,
            )

        return await self._db.run(_op)

    def _render_stable_brief(
        self,
        *,
        sealed_rows: Sequence[DailyChronicleRow],
        stances: Sequence[PersonaAnchorRow],
        promises: Sequence[PersonaAnchorRow],
        target_visibility: str,
        known_external_ids: Sequence[str],
    ) -> str:
        """渲染严格不含相对时间词的慢层简报。"""
        sections: List[str] = []

        chronicle_lines: List[str] = []
        for row in sealed_rows:
            body = _chronicle_body_for_visibility(row, target_visibility)
            if not body:
                continue
            label = SLOT_LABELS.get(row.slot_key, row.slot_key)
            chronicle_lines.append(f"- {label}：{sanitize_for_model(body, known_external_ids)}")
        if chronicle_lines:
            sections.append("【麦麦今天的经历】\n" + "\n".join(chronicle_lines))

        if stances:
            stance_lines = [
                f"- {sanitize_for_model(item.content, known_external_ids)}（来自{_branch_label(item.branch_id)}）"
                for item in stances
            ]
            sections.append("【近期观点与偏好】\n" + "\n".join(stance_lines))

        if promises:
            promise_lines = [
                f"- [约#{item.id}] {sanitize_for_model(item.content, known_external_ids)}"
                f"（在{_branch_label(item.branch_id)}答应）"
                for item in promises
            ]
            sections.append("【跨端还惦记的约定】\n" + "\n".join(promise_lines))

        return "\n\n".join(sections)

    def _render_live_context(
        self,
        *,
        branch_id: str,
        active_sessions: Sequence[CheckoutSessionRow],
        open_rows: Sequence[DailyChronicleRow],
        recent_events: Sequence[object],
        target_visibility: str,
        known_external_ids: Sequence[str],
    ) -> str:
        """渲染面向请求尾部的动态现况与未封存最近动态。"""
        parts: List[str] = []

        other_active = [s for s in active_sessions if s.branch_id != branch_id]
        if other_active:
            for sess in other_active:
                plat = sess.platform or "直播平台"
                title_part = f"《{sess.title}》" if sess.title else ""
                seg_part = f"，当前环节：{sess.segment_title}" if sess.segment_title else ""
                parts.append(f"你此刻正在{plat}开直播（你自己是主播）{title_part}{seg_part}。")

        open_lines: List[str] = []
        for row in open_rows:
            body = _chronicle_body_for_visibility(row, target_visibility)
            if not body:
                continue
            label = SLOT_LABELS.get(row.slot_key, "刚才")
            open_lines.append(f"- {label}进行中：{sanitize_for_model(body, known_external_ids)}")

        if not open_lines and recent_events:
            for ev in recent_events[:3]:
                where = describe_visibility_scope(ev.visibility, branch_id=ev.branch_id)  # type: ignore[attr-defined]
                clean = sanitize_for_model(ev.summary, known_external_ids)  # type: ignore[attr-defined]
                open_lines.append(f"- 近期在{where}：{clean}")

        if open_lines:
            parts.append("【刚刚的动态】\n" + "\n".join(open_lines))
        return "\n".join(parts)

    async def recall_life(
        self,
        *,
        query: str,
        days: int = 7,
        limit: int = 5,
        target_visibility: str = "public",
    ) -> str:
        """混合检索跨端经历与锚点，供 MCP ``maigo_recall_life`` 与看板检索使用。"""
        vis_level = normalize_visibility(target_visibility, default="public")
        vis_allowed = allowed_visibilities(vis_level)
        since_ms = int(self._clock_ms()) - max(1, int(days)) * 24 * 3600 * 1000

        def _op(conn: sqlite3.Connection) -> str:
            all_ext_ids = [item.external_id for item in self._persons.list_all_identities(conn)]
            ev_hits = self._events.search(conn, query=query, since_ms=since_ms, visibilities=vis_allowed, limit=limit)
            anc_hits = self._anchors.search(conn, query=query, visibilities=vis_allowed, limit=limit)
            lines: List[str] = []
            for anc in anc_hits:
                status_word = "已兑现" if anc.status == "closed" else ("已作废" if anc.status == "void" else "进行中")
                clean_c = sanitize_for_model(anc.content, all_ext_ids)
                lines.append(f"[锚点#{anc.id}·{status_word}] {clean_c}（出自{_branch_label(anc.branch_id)}）")
            for ev in ev_hits:
                if ev.kind == "forget_request":
                    continue
                where = describe_visibility_scope(ev.visibility, branch_id=ev.branch_id)
                clean_s = sanitize_for_model(ev.summary, all_ext_ids)
                quote = f" 原话：「{sanitize_for_model(ev.verbatim, all_ext_ids)}」" if ev.verbatim else ""
                lines.append(f"[经历·{where}] {clean_s}{quote}")
            if not lines:
                return f"最近 {days} 天里没有找到与「{query}」相关的跨端记录。"
            return "\n".join(lines[: max(1, int(limit))])

        return await self._db.run(_op)
