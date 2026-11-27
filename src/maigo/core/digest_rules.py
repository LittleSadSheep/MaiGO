"""编年合成器的作息时段划分、LLM 接口协议与确定性规则草稿生成器。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

import httpx

from maigo.core.models import DailyChronicleRow, ExperienceEventRow, LifeArcRow, PersonaAnchorRow
from maigo.core.privacy import describe_visibility_scope, sanitize_for_model
from maigo.runtime import get_logger

logger = get_logger("services.synthesizer.rules")

SLOT_ORDER: Tuple[str, ...] = ("yesterday_carry", "morning", "afternoon", "evening", "late_night")
SLOT_LABELS: Dict[str, str] = {
    "yesterday_carry": "昨日余温",
    "morning": "今天上午",
    "afternoon": "今天下午",
    "evening": "今天晚上",
    "late_night": "今天深夜",
}

_PROMISE_KEYWORDS = ("答应", "约定", "今晚直播", "明天直播", "下次直播", "一定带", "建新", "补发", "发动态")
_STANCE_KEYWORDS = ("喜欢", "讨厌", "最爱", "不吃", "超爱", "觉得", "看好", "坚持")
_MOOD_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("累", "困", "折腾", "翻车", "加班"), "略累但有劲"),
    (("得意", "点亮", "竣工", "建成", "通关", "搞定"), "很有成就感"),
    (("好笑", "哈哈", "呆呆", "有趣", "可爱", "搞笑"), "轻松惬意"),
    (("热闹", "讨论", "聊到", "约定", "直播"), "兴致不错"),
)
_HOLLOW_INTERACTION_RE = re.compile(r"发生游戏互动（(?:greet|gift|spar|helped|together|interaction)）")
_HASH_SAVE_TITLE_RE = re.compile(r"^(?:存档\s*)?[0-9a-fA-F]{12,}(?:\s*近况)?$")
_HEX_HASH_RE = re.compile(r"[0-9a-fA-F]{16,}")


def humanize_arc_title(
    title: str,
    *,
    save_id: Optional[str] = None,
    summary: str = "",
) -> str:
    """将含长哈希 save_id 的技术性主线标题改写为自然的人类可读标题。

    触发条件：规则提炼主线、LLM 输出主线或慢层简报渲染主线标题时调用；
    处理顺序：若标题本身形如「存档 <十六进制哈希>」或夹带长哈希串，
    按摘要关键词归类为可读名称，避免向模型暴露无意义的内部哈希 ID。
    """
    clean = str(title or "").strip()
    sid = str(save_id or "").strip()
    has_raw_hash = (
        not clean
        or bool(_HASH_SAVE_TITLE_RE.match(clean))
        or bool(_HEX_HASH_RE.search(clean))
        or (len(sid) >= 12 and sid in clean)
    )
    if not has_raw_hash:
        return clean[:64]
    low_sum = (summary or "").lower()
    if "minecraft" in low_sum or "mc" in low_sum:
        return "Minecraft 存档主线"
    return "游戏存档主线"
_KIND_PRIORITY = {
    "milestone": 0,
    "promise_update": 1,
    "browsed": 2,
    "speech": 3,
    "interaction": 4,
}


def is_hollow_interaction(ev: ExperienceEventRow) -> bool:
    """判断是否为无具体剧情细节的空壳游戏互动（如纯 greet/gift），防止其淹没主时间线。"""
    if ev.kind != "interaction":
        return False
    if ev.verbatim.strip() or ev.reaction.strip():
        return False
    return bool(_HOLLOW_INTERACTION_RE.search(ev.summary))


def prioritize_slot_events(
    events: Sequence[ExperienceEventRow],
    *,
    max_events: int = 40,
) -> List[ExperienceEventRow]:
    """整理前先剥离空壳游戏互动并按信息价值分层截断，避免长时段撑爆外部整理模型超时。"""
    non_hollow = [ev for ev in events if not is_hollow_interaction(ev)]
    pool = non_hollow if non_hollow else list(events[:5])
    if len(pool) <= max_events:
        return sorted(pool, key=lambda ev: (ev.occurred_at_ms, ev.id))
    # 优先保住场次总览、高光里程碑与公开承诺，再按最新顺序保留余量，最后恢复时间正序
    ranked = sorted(
        pool,
        key=lambda ev: (
            0 if ev.metadata.get("session_overview") or ev.metadata.get("highlight") else 1,
            _KIND_PRIORITY.get(ev.kind, 9),
            -ev.occurred_at_ms,
        ),
    )
    selected = ranked[:max_events]
    return sorted(selected, key=lambda ev: (ev.occurred_at_ms, ev.id))


@dataclass(frozen=True)
class AnchorCandidate:
    """合成产物中的候选言行锚点（必须经过代码硬校验才可入账）。"""

    anchor_type: str  # stance | promise
    content: str
    source_event_id: int
    status: str = "active"  # active | open | closed | void
    note: str = ""
    save_id: Optional[str] = None
    target_person_id: Optional[int] = None
    supersedes_id: Optional[int] = None


@dataclass(frozen=True)
class TopicCandidate:
    """合成产物中的候选鲜活话头胶囊。"""

    title: str
    summary: str
    reaction: str
    source_event_id: int
    consumed_branches: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ArcCandidate:
    """合成产物中的候选温层近期主线（连续几天在做的事）。"""

    title: str
    summary: str
    public_summary: str = ""
    status: str = "active"
    visibility: str = "public"
    save_id: Optional[str] = None
    mood_hint: str = ""
    source_event_id: Optional[int] = None


@dataclass(frozen=True)
class SlotSynthesisDraft:
    """单时段合成草稿（供 LLM 客户端或内置规则引擎共同输出）。"""

    body: str
    public_body: str
    group_body: str
    mood_tag: str = ""
    unfinished_thread: str = ""
    anchors: List[AnchorCandidate] = field(default_factory=list)
    topics: List[TopicCandidate] = field(default_factory=list)
    arcs: List[ArcCandidate] = field(default_factory=list)


class SynthesisLLMClient(Protocol):
    """可替换编年合成模型客户端协议（便于真模型接入与离线确定性测试）。"""

    async def summarize_slot(
        self,
        *,
        day_key: str,
        slot_key: str,
        events: Sequence[ExperienceEventRow],
    ) -> Optional[SlotSynthesisDraft]: ...


class OpenAISynthesisClient:
    """基于 OpenAI Chat Completions 兼容接口的异步编年合成器客户端。"""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_ms: int = 15_000) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = max(1.0, timeout_ms / 1000.0)

    async def summarize_slot(
        self,
        *,
        day_key: str,
        slot_key: str,
        events: Sequence[ExperienceEventRow],
    ) -> Optional[SlotSynthesisDraft]:
        """兼容简单 Protocol 的单时段整理入口。"""
        return await self.summarize_slot_with_context(day_key=day_key, slot_key=slot_key, events=events)

    async def summarize_slot_with_context(
        self,
        *,
        day_key: str,
        slot_key: str,
        events: Sequence[ExperienceEventRow],
        existing_stances: Sequence[PersonaAnchorRow] = (),
        existing_arcs: Sequence[LifeArcRow] = (),
        persona_name: str = "麦麦",
    ) -> Optional[SlotSynthesisDraft]:
        """调用外部模型将多渠道碎片整理成带情绪与因果线索的第一人称手账，失败回落规则兜底。"""
        if not self._base_url or not self._model:
            return None
        lines = [
            (
                f"[id={ev.id}][branch={ev.branch_id}][role={ev.actor_role}][vis={ev.visibility}]"
                f"[kind={ev.kind}][save={ev.save_id or '-'}] {ev.summary}"
                f" | verbatim={ev.verbatim} | reaction={ev.reaction}"
            )
            for ev in events
        ]
        stance_hint = "\n".join(f"- #{s.id}: {s.content}" for s in existing_stances[:10]) or "（暂无）"
        arc_hint = "\n".join(f"- [{a.status}] {a.title}: {a.summary}" for a in existing_arcs[:6]) or "（暂无）"
        prompt = (
            f"你是虚拟主播「{persona_name}」的 24 小时跨端生活手账整理器。\n"
            f"请把下面同一时段的多端碎片梳理成连贯、有因果和口语生活感的 JSON（严禁写相对时间词）：\n"
            f"作息日={day_key} 时段={SLOT_LABELS.get(slot_key, slot_key)}\n"
            f"当前已有立场（如有明确改变可填 supersedes_id）：\n{stance_hint}\n"
            f"近期进行中主线：\n{arc_hint}\n"
            "输出字段：\n"
            '{"body": "第一人称主线+插曲时段日记（含私聊）", '
            '"public_body": "去掉所有private并把群聊趣事自然改写为公开口吻的日记", '
            '"group_body": "去掉private事件的群聊可见日记", '
            '"mood_tag": "2~6字当前情绪底色", '
            '"unfinished_thread": "15~30字待续悬念或还没聊透的事（无则空串）", '
            '"anchors": [{"anchor_type": "stance|promise", "content": "...", "source_event_id": 1, "status": "active|open|closed|void", "supersedes_id": null}], '
            '"topics": [{"title": "...", "summary": "...", "reaction": "...", "source_event_id": 1}], '
            '"arcs": [{"title": "...", "summary": "...", "public_summary": "...", "save_id": null, "mood_hint": "...", "source_event_id": 1}]}\n'
            "硬约束：anchors 只能从 actor_role=self 的事件提取，严禁把 peer 的话当成自己的立场或承诺；"
            "arcs.title 必须使用简短自然的人类可读标题（如“Minecraft 营地建设”），严禁把 save_id 哈希串写进标题。\n"
            "事件列表：\n" + "\n".join(lines)
        )
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.25,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(f"{self._base_url}/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                content = str(resp.json()["choices"][0]["message"]["content"])
            data = json.loads(content)
            anchors = [
                AnchorCandidate(
                    anchor_type=str(a.get("anchor_type", "stance")),
                    content=str(a.get("content", "")).strip(),
                    source_event_id=int(a["source_event_id"]),
                    status=str(a.get("status", "active")),
                    note=str(a.get("note", "")),
                    supersedes_id=int(a["supersedes_id"]) if a.get("supersedes_id") is not None else None,
                )
                for a in (data.get("anchors") or [])
                if isinstance(a, dict) and a.get("source_event_id") is not None
            ]
            topics = [
                TopicCandidate(
                    title=str(t.get("title", "")).strip(),
                    summary=str(t.get("summary", "")).strip(),
                    reaction=str(t.get("reaction", "")).strip(),
                    source_event_id=int(t["source_event_id"]),
                )
                for t in (data.get("topics") or [])
                if isinstance(t, dict) and t.get("source_event_id") is not None
            ]
            arcs = [
                ArcCandidate(
                    title=humanize_arc_title(
                        str(ar.get("title", "")).strip(),
                        save_id=str(ar["save_id"]) if ar.get("save_id") else None,
                        summary=str(ar.get("summary", "")).strip(),
                    ),
                    summary=str(ar.get("summary", "")).strip(),
                    public_summary=str(ar.get("public_summary", "")).strip(),
                    save_id=str(ar["save_id"]) if ar.get("save_id") else None,
                    mood_hint=str(ar.get("mood_hint", "")).strip(),
                    source_event_id=int(ar["source_event_id"]) if ar.get("source_event_id") is not None else None,
                )
                for ar in (data.get("arcs") or [])
                if isinstance(ar, dict) and str(ar.get("title", "")).strip()
            ]
            return SlotSynthesisDraft(
                body=str(data.get("body", "")).strip(),
                public_body=str(data.get("public_body", "")).strip(),
                group_body=str(data.get("group_body", "")).strip(),
                mood_tag=str(data.get("mood_tag", "")).strip(),
                unfinished_thread=str(data.get("unfinished_thread", "")).strip(),
                anchors=anchors,
                topics=topics,
                arcs=arcs,
            )
        except Exception as exc:
            logger.warning(f"调用外部编年整理模型失败，自动回落规则合成: {exc}")
            return None


def compute_day_and_slot(
    timestamp_ms: int,
    *,
    day_rollover_hour: int = 4,
    timezone_offset_hours: int = 8,
) -> Tuple[str, str]:
    """根据毫秒时间戳、时区偏移与作息切日小时计算作息日 ``day_key`` 与时段 ``slot_key``。"""
    tz = timezone(timedelta(hours=timezone_offset_hours))
    local_dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=tz)
    shifted = local_dt - timedelta(hours=day_rollover_hour)
    day_key = shifted.strftime("%Y-%m-%d")

    hour = local_dt.hour
    if 4 <= hour < 12:
        slot_key = "morning"
    elif 12 <= hour < 18:
        slot_key = "afternoon"
    elif 18 <= hour < 23:
        slot_key = "evening"
    else:
        slot_key = "late_night"
    return day_key, slot_key


def slot_rank(slot_key: str) -> int:
    """返回作息时段顺序权重。"""
    try:
        return SLOT_ORDER.index(slot_key)
    except ValueError:
        return 99


def _infer_rule_mood(events: Sequence[ExperienceEventRow]) -> str:
    combined = " ".join(f"{e.summary} {e.reaction} {e.verbatim}" for e in events if e.visibility != "private")
    for kws, mood in _MOOD_RULES:
        if any(k in combined for k in kws):
            return mood
    return ""


def _extract_rule_arcs(
    ev: ExperienceEventRow,
    out: List[ArcCandidate],
    known_external_ids: Sequence[str],
) -> None:
    """仅当事件带显式 arc_title 或含 save_id 的重要里程碑时，提炼跨日温层主线。"""
    if ev.visibility == "private":
        return
    explicit_title = str(ev.metadata.get("arc_title") or "").strip()
    readable_save = str(ev.metadata.get("save_name") or ev.metadata.get("world_name") or "").strip()
    is_highlight_save = bool(ev.save_id and ev.kind == "milestone" and ev.metadata.get("highlight"))
    if not explicit_title and not is_highlight_save:
        return
    clean_sum = sanitize_for_model(ev.summary, known_external_ids)[:200]
    raw_title = explicit_title or (f"{readable_save} 近况" if readable_save else f"存档 {ev.save_id} 近况")
    title = humanize_arc_title(
        sanitize_for_model(raw_title, known_external_ids),
        save_id=ev.save_id,
        summary=clean_sum,
    )[:60]
    out.append(
        ArcCandidate(
            title=title,
            summary=clean_sum,
            public_summary=clean_sum,
            visibility=ev.visibility,
            save_id=ev.save_id,
            source_event_id=ev.id,
        )
    )


def build_rule_draft(
    events: Sequence[ExperienceEventRow],
    *,
    known_external_ids: Sequence[str],
) -> SlotSynthesisDraft:
    """确定性规则合成器：在无外部 LLM 或接口故障时稳定产出三档可见性纪要、情绪与候选。"""
    body_lines: List[str] = []
    public_lines: List[str] = []
    group_lines: List[str] = []
    anchors: List[AnchorCandidate] = []
    topics: List[TopicCandidate] = []
    arcs: List[ArcCandidate] = []
    unfinished_thread = ""

    # 规则拼接前先剔除空壳游戏互动噪音，避免 10 条 greet/gift 把真正的主线剧情挤到截断区外
    focused_events = prioritize_slot_events(events, max_events=30)
    for ev in focused_events:
        where = describe_visibility_scope(ev.visibility, branch_id=ev.branch_id)
        base = sanitize_for_model(ev.summary.strip(), known_external_ids)
        if ev.reaction.strip():
            base = f"{base}（想法：{sanitize_for_model(ev.reaction.strip(), known_external_ids)}）"
        elif ev.verbatim.strip() and ev.actor_role == "self" and ev.kind in ("browsed", "speech"):
            short_quote = sanitize_for_model(ev.verbatim.strip()[:60], known_external_ids)
            if short_quote and short_quote not in base:
                base = f"{base}，当时说道「{short_quote}」"

        line = base if where in base[: len(where) + 4] else f"在{where}{base}"
        if line not in body_lines:
            body_lines.append(line)
        if ev.visibility != "private":
            if line not in group_lines:
                group_lines.append(line)
            if line not in public_lines:
                public_lines.append(line)
            if ev.reaction.strip():
                unfinished_thread = sanitize_for_model(ev.reaction.strip()[:48], known_external_ids)

        if ev.actor_role == "self" and ev.kind in ("speech", "promise_update", "browsed"):
            _extract_rule_anchors(ev, anchors)
        _extract_rule_topics(ev, topics, known_external_ids)
        _extract_rule_arcs(ev, arcs, known_external_ids)

    return SlotSynthesisDraft(
        body="；".join(body_lines) + ("。" if body_lines else ""),
        public_body="；".join(public_lines) + ("。" if public_lines else ""),
        group_body="；".join(group_lines) + ("。" if group_lines else ""),
        mood_tag=_infer_rule_mood(events),
        unfinished_thread=unfinished_thread,
        anchors=anchors,
        topics=topics,
        arcs=arcs,
    )


def _extract_rule_anchors(ev: ExperienceEventRow, out: List[AnchorCandidate]) -> None:
    """从单条 ``self`` 事件提取承诺或立场候选。"""
    meta = ev.metadata
    if ev.kind == "promise_update" or meta.get("promise_action"):
        action = str(meta.get("promise_action") or "open").strip().lower()
        status = action if action in ("open", "closed", "void") else "open"
        content = str(meta.get("promise_content") or ev.summary).strip()
        if content:
            out.append(
                AnchorCandidate(
                    anchor_type="promise",
                    content=content,
                    source_event_id=ev.id,
                    status=status,
                    note=str(meta.get("note") or ""),
                    save_id=ev.save_id,
                )
            )
        return

    text = f"{ev.summary} {ev.verbatim}".strip()
    if ev.kind == "speech" and ev.verbatim.strip() and any(kw in text for kw in _PROMISE_KEYWORDS):
        content = str(meta.get("promise_content") or ev.summary or ev.verbatim).strip()[:100]
        out.append(
            AnchorCandidate(
                anchor_type="promise",
                content=content,
                source_event_id=ev.id,
                status="open",
                save_id=ev.save_id,
            )
        )
    elif ev.verbatim.strip() and any(kw in text for kw in _STANCE_KEYWORDS):
        content = (ev.reaction.strip() or ev.verbatim.strip() or ev.summary.strip())[:100]
        out.append(
            AnchorCandidate(
                anchor_type="stance",
                content=content,
                source_event_id=ev.id,
                status="active",
                save_id=ev.save_id,
            )
        )


def _extract_rule_topics(
    ev: ExperienceEventRow,
    out: List[TopicCandidate],
    known_external_ids: Sequence[str],
) -> None:
    """从刷到的社媒内容、带有主观感想的事件或高光节点提炼鲜活话头。"""
    if ev.visibility == "private":
        return
    is_highlight = bool(ev.metadata.get("highlight"))
    should_seed = ev.kind == "browsed" or bool(ev.reaction.strip()) or (ev.kind == "milestone" and is_highlight)
    if not should_seed:
        return
    raw_title = str(ev.metadata.get("topic_title") or ev.metadata.get("source_title") or ev.summary).strip()
    # 去掉脱敏标记与从句尾巴，保留紧凑直观的主题短标题
    clean_title = sanitize_for_model(raw_title, known_external_ids)
    clean_title = re.sub(r"\[[^\]]*已脱敏\]", "", clean_title)
    clean_title = re.split(r"[，。！？；\n]", clean_title)[0].strip()[:48]
    summary = sanitize_for_model(ev.summary, known_external_ids)[:180]
    reaction = sanitize_for_model(ev.reaction or ev.verbatim, known_external_ids)[:160]
    consumed: Tuple[str, ...] = ()
    if ev.kind == "speech" and ev.branch_id:
        consumed = (ev.branch_id,)
    out.append(
        TopicCandidate(
            title=clean_title or "今日见闻",
            summary=summary,
            reaction=reaction,
            source_event_id=ev.id,
            consumed_branches=consumed,
        )
    )


def compress_yesterday(rows: Sequence[DailyChronicleRow], attr: str) -> str:
    """将前一日各已封存时段的纪要拼接为简短的昨日余温（不含相对时间词）。"""
    parts: List[str] = []
    for row in rows:
        if row.slot_key == "yesterday_carry":
            continue
        text = str(getattr(row, attr, "") or "").strip()
        if not text:
            continue
        prefix = SLOT_LABELS.get(row.slot_key, "").replace("今天", "昨天")
        parts.append(f"{prefix}{text}" if prefix else text)
    return " ".join(parts).strip()
