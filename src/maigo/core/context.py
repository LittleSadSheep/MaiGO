"""快慢分层上下文视图投影器与跨端回忆检索服务。

职责边界：
1. 慢层 ``stable_brief``：只含昨日余温 + 今日已封存时段 + 生效中立场与承诺，绝不含跳动的相对时间词，
   在未发生跨段封存或承诺变动时逐字节一致，配套 ``brief_etag`` 保住接入端前缀缓存；
2. 快层 ``live_context``：当前跨端活动状态（如正在开播、近期刚下播）+ 当前未封存时段的实时进展；
3. 话头消费附注：若某话头已在其他分支聊过（如 ``maibot``），投影给新分支时自动附带自然过渡注记。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from maigo.core.digest_rules import (
    SLOT_LABELS,
    compute_day_and_slot,
    humanize_arc_title,
    is_hollow_interaction,
    slot_rank,
)
from maigo.core.identity import ActorCardProjection, IdentityService, PersonDirectoryEntry
from maigo.core.models import CheckoutSessionRow, DailyChronicleRow, EpisodeRow, LifeArcRow, PersonaAnchorRow
from maigo.core.privacy import (
    allowed_visibilities,
    describe_visibility_scope,
    normalize_visibility,
    sanitize_for_model,
)
from maigo.runtime import ClockMs, now_ms
from maigo.storage.db import MaiGODatabase
from maigo.storage.repos import (
    DailyChronicleRepo,
    EpisodeRepo,
    ExperienceEventRepo,
    LifeArcRepo,
    PersonaAnchorRepo,
    PersonRepo,
    SessionRepo,
    TopicSeedRepo,
)

if TYPE_CHECKING:
    from maigo.core.recall import HybridRecallService, RecallResult

_BRANCH_LABELS = {
    "maibot": "QQ群",
    "amaidesu": "直播间",
    "bilibili_ops": "B站",
    "x_ops": "X",
    "mc_runner": "Minecraft",
    "rss_collector": "订阅浏览",
    "daily_feeder": "日常浏览",
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


_THIRD_PERSON_LEAD_RE = re.compile(r"^(?:这|本)场直播中?[，,、\s]*主播")
_THIRD_PERSON_HOST_RE = re.compile(r"(?:这|本)场直播(?:里|中)?的主播")
_THIRD_PERSON_PRONOUN_RE = re.compile(r"(?<![其另])她")


def _to_first_person_recap(text: str) -> str:
    """将结场客观第三人称直播概览改写为第一人称自述，避免群聊模型把“主播/她”误认成他人。"""
    clean = str(text or "").strip()
    if not clean:
        return ""
    # 先替换句首“这场直播中，主播”与“这场直播的主播”，再收敛其余“主播”和第三人称“她”
    clean = _THIRD_PERSON_LEAD_RE.sub("刚才直播里我", clean)
    clean = _THIRD_PERSON_HOST_RE.sub("刚才直播里的我", clean)
    clean = clean.replace("主播", "我")
    clean = _THIRD_PERSON_PRONOUN_RE.sub("我", clean)
    return clean.replace("我我", "我")


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
        retrieval: Optional[HybridRecallService] = None,
        wake_hint_enabled: bool = True,
    ) -> None:
        self._db = db
        self._identity = identity
        self._rollover_hour = day_rollover_hour
        self._tz_offset = timezone_offset_hours
        self._recent_window_ms = recent_window_ms
        self._clock_ms = clock_ms
        self._retrieval = retrieval
        self._wake_hint_enabled = wake_hint_enabled
        self._chronicle = DailyChronicleRepo(clock_ms=clock_ms)
        self._anchors = PersonaAnchorRepo(clock_ms=clock_ms)
        self._topics = TopicSeedRepo(clock_ms=clock_ms)
        self._events = ExperienceEventRepo(clock_ms=clock_ms)
        self._persons = PersonRepo(clock_ms=clock_ms)
        self._sessions = SessionRepo(clock_ms=clock_ms)
        self._arcs = LifeArcRepo(clock_ms=clock_ms)
        self._episodes = EpisodeRepo(clock_ms=clock_ms)

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
        current_day, current_slot = compute_day_and_slot(
            now, day_rollover_hour=self._rollover_hour, timezone_offset_hours=self._tz_offset
        )

        def _op(conn: sqlite3.Connection) -> ContextProjection:
            all_ext_ids = [item.external_id for item in self._persons.list_all_identities(conn)]

            # 1. 慢层组装：温层近期主线 + 昨日余温 + 今日已封存时段 + 固定作息留白 + 生效中立场/承诺
            all_today = self._chronicle.list_by_day(conn, day_key=current_day)
            sealed_rows = [row for row in all_today if row.status == "sealed"]
            arcs = self._arcs.list_arcs(conn, statuses=["active"], visibilities=vis_allowed, save_id=save_id, limit=3)
            stances = self._anchors.list_anchors(
                conn, anchor_type="stance", statuses=["active"], visibilities=vis_allowed, save_id=save_id, limit=12
            )
            promises = self._anchors.list_anchors(
                conn, anchor_type="promise", statuses=["open"], visibilities=vis_allowed, save_id=save_id, limit=12
            )
            wake_hint = self._build_morning_wake_hint(
                conn,
                current_day=current_day,
                current_slot=current_slot,
                all_today=all_today,
                target_visibility=vis_level,
            )
            stable_text = self._render_stable_brief(
                arcs=arcs,
                sealed_rows=sealed_rows,
                wake_hint=wake_hint,
                stances=stances,
                promises=promises,
                target_visibility=vis_level,
                known_external_ids=all_ext_ids,
            )
            etag = hashlib.sha256(stable_text.encode("utf-8")).hexdigest()[:16]
            unchanged = bool(if_brief_etag and if_brief_etag.strip() == etag)

            # 2. 实时动态组装：在线正式直播状态、4 小时内刚下播余温与当前未封存窗口近期动态
            active_sessions = self._sessions.list_active_checkouts(conn)
            just_ended: List[Tuple[CheckoutSessionRow, Optional[EpisodeRow]]] = []
            if not any(s.branch_id != branch_id for s in active_sessions):
                recent_cutoff = now - 4 * 3600 * 1000
                for sess in self._sessions.list_recent_checkouts(conn, limit=5):
                    if sess.active or sess.branch_id == branch_id:
                        continue
                    if sess.ended_at_ms is not None and sess.ended_at_ms >= recent_cutoff:
                        ep = self._episodes.get_by_external(
                            conn, branch_id=sess.branch_id, external_episode_id=sess.external_session_id
                        )
                        just_ended.append((sess, ep))
                        break
            open_rows = self._chronicle.list_by_day(conn, day_key=current_day, status="open")
            recent_events = self._events.list_recent(
                conn,
                since_ms=now - self._recent_window_ms,
                visibilities=vis_allowed,
                limit=15,
            )
            live_text = self._render_live_context(
                branch_id=branch_id,
                active_sessions=active_sessions,
                just_ended=just_ended,
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

    def _build_morning_wake_hint(
        self,
        conn: sqlite3.Connection,
        *,
        current_day: str,
        current_slot: str,
        all_today: Sequence[DailyChronicleRow],
        target_visibility: str,
    ) -> str:
        """若今日已有其它生活记录但上午完全空白且已过中午，结合昨晚收工晚不晚补充固定上午留白。"""
        if not self._wake_hint_enabled or slot_rank(current_slot) < slot_rank("afternoon"):
            return ""
        has_morning = any(
            r.slot_key == "morning" and bool(_chronicle_body_for_visibility(r, target_visibility)) for r in all_today
        )
        if has_morning:
            return ""
        has_any_history = bool(all_today)
        prev_days = [d for d in self._chronicle.list_recent_days(conn, limit_days=3) if d < current_day]
        late_yesterday = False
        if prev_days:
            has_any_history = True
            late_row = self._chronicle.get_slot(conn, day_key=prev_days[0], slot_key="late_night")
            if late_row is not None and bool(_chronicle_body_for_visibility(late_row, target_visibility)):
                late_yesterday = True
        if not has_any_history:
            return ""
        return "昨晚忙到深夜收工，今天上午一直在补觉休息" if late_yesterday else "今天上午宅家摸鱼休息，没有在外头冒泡"

    def _render_stable_brief(
        self,
        *,
        arcs: Sequence[LifeArcRow],
        sealed_rows: Sequence[DailyChronicleRow],
        wake_hint: str,
        stances: Sequence[PersonaAnchorRow],
        promises: Sequence[PersonaAnchorRow],
        target_visibility: str,
        known_external_ids: Sequence[str],
    ) -> str:
        """渲染严格不含相对时间词的慢层简报。"""
        sections: List[str] = []

        if arcs:
            arc_lines: List[str] = []
            for arc in arcs:
                raw_sum = (arc.public_summary or arc.summary) if target_visibility == "public" else arc.summary
                clean_sum = sanitize_for_model(raw_sum, known_external_ids)
                clean_title = humanize_arc_title(
                    sanitize_for_model(arc.title, known_external_ids),
                    save_id=arc.save_id,
                    summary=clean_sum,
                )
                mood = f"（状态：{arc.mood_hint}）" if arc.mood_hint else ""
                arc_lines.append(f"- {clean_title}{mood}：{clean_sum}")
            sections.append("【近期一直在忙的主线】\n" + "\n".join(arc_lines))

        chronicle_lines: List[str] = []
        if wake_hint:
            chronicle_lines.append(f"- 今天上午：{wake_hint}")
        for row in sealed_rows:
            body = _chronicle_body_for_visibility(row, target_visibility)
            if not body:
                continue
            label = SLOT_LABELS.get(row.slot_key, row.slot_key)
            mood_part = f"（状态：{row.mood_tag}）" if row.mood_tag and row.slot_key != "yesterday_carry" else ""
            thread_part = (
                f"；待续：{sanitize_for_model(row.unfinished_thread, known_external_ids)}"
                if row.unfinished_thread and row.slot_key != "yesterday_carry"
                else ""
            )
            chronicle_lines.append(f"- {label}{mood_part}：{sanitize_for_model(body, known_external_ids)}{thread_part}")
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
        just_ended: Sequence[Tuple[CheckoutSessionRow, Optional[EpisodeRow]]] = (),
        open_rows: Sequence[DailyChronicleRow],
        recent_events: Sequence[object],
        target_visibility: str,
        known_external_ids: Sequence[str],
    ) -> str:
        """渲染面向请求尾部的实时动态（含在播态、4 小时内刚下播态与高信号近期事件）。"""
        parts: List[str] = []

        other_active = [s for s in active_sessions if s.branch_id != branch_id]
        if other_active:
            for sess in other_active:
                plat = sess.platform or "直播平台"
                title_part = f"《{sanitize_for_model(sess.title, known_external_ids)}》" if sess.title else ""
                seg_part = f"，当前环节：{sess.segment_title}" if sess.segment_title else ""
                parts.append(f"你此刻正在{plat}开直播（你自己是主播）{title_part}{seg_part}。")
        elif just_ended:
            for sess, ep in just_ended:
                plat = sess.platform or "直播平台"
                title_part = f"《{sanitize_for_model(sess.title, known_external_ids)}》" if sess.title else ""
                raw_ov = sanitize_for_model(ep.overview, known_external_ids) if (ep and ep.overview) else ""
                ov_text = _to_first_person_recap(raw_ov)[:240]
                ov_part = f"刚才直播回顾：{ov_text}" if ov_text else ""
                parts.append(f"你刚刚结束了{plat}直播{title_part}（你是这场直播的主播）。{ov_part}".strip())

        open_lines: List[str] = []
        for row in open_rows:
            body = _chronicle_body_for_visibility(row, target_visibility)
            if not body:
                continue
            label = SLOT_LABELS.get(row.slot_key, "刚才")
            open_lines.append(f"- {label}进行中：{sanitize_for_model(body, known_external_ids)}")

        # 优先过滤空壳游戏互动，仅展示真正高信号的近期动态
        signal_events = [ev for ev in recent_events if not is_hollow_interaction(ev)]  # type: ignore[arg-type]
        if not open_lines and signal_events:
            for ev in signal_events[:4]:
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
        days: int = 30,
        limit: int = 5,
        target_visibility: str = "public",
        exclude_branch: Optional[str] = None,
        high_confidence_only: bool = False,
    ) -> str:
        """混合检索跨端原件书库、实体关系、持续主线与观点约定。"""
        if self._retrieval is not None:
            return await self._retrieval.recall(
                query=query,
                days=days,
                limit=limit,
                target_visibility=target_visibility,
                exclude_branch=exclude_branch,
                high_confidence_only=high_confidence_only,
            )
        vis_level = normalize_visibility(target_visibility, default="public")
        vis_allowed = allowed_visibilities(vis_level)
        since_ms = 0 if int(days) <= 0 else int(self._clock_ms()) - int(days) * 24 * 3600 * 1000

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
                if ev.kind == "forget_request" or is_hollow_interaction(ev):
                    continue
                where = describe_visibility_scope(ev.visibility, branch_id=ev.branch_id)
                clean_s = sanitize_for_model(ev.summary, all_ext_ids)
                quote = f" 原话：「{sanitize_for_model(ev.verbatim, all_ext_ids)}」" if ev.verbatim else ""
                lines.append(f"[经历·{where}] {clean_s}{quote}")
            if not lines:
                return "" if high_confidence_only else f"最近 {days} 天里没有找到与「{query}」相关的跨端记录。"
            return "\n".join(lines[: max(1, int(limit))])

        return await self._db.run(_op)

    async def recall_life_structured(
        self,
        *,
        query: str,
        days: int = 30,
        limit: int = 5,
        target_visibility: str = "public",
        exclude_branch: Optional[str] = None,
        high_confidence_only: bool = False,
    ) -> Optional[RecallResult]:
        """返回带强命中判定的结构化召回结果（供高置信度自动预召回使用）。"""
        if self._retrieval is None:
            return None
        return await self._retrieval.recall_structured(
            query=query,
            days=days,
            limit=limit,
            target_visibility=target_visibility,
            exclude_branch=exclude_branch,
            high_confidence_only=high_confidence_only,
        )
