"""编年合成器的作息时段划分、LLM 接口协议与确定性规则草稿生成器。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

import httpx

from maiswitch.runtime import get_logger
from maiswitch.services.sanitizer import describe_visibility_scope, sanitize_for_model
from maiswitch.storage.models import DailyChronicleRow, ExperienceEventRow

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


@dataclass(frozen=True)
class TopicCandidate:
    """合成产物中的候选鲜活话头胶囊。"""

    title: str
    summary: str
    reaction: str
    source_event_id: int
    consumed_branches: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SlotSynthesisDraft:
    """单时段合成草稿（供 LLM 客户端或内置规则引擎共同输出）。"""

    body: str
    public_body: str
    group_body: str
    anchors: List[AnchorCandidate] = field(default_factory=list)
    topics: List[TopicCandidate] = field(default_factory=list)


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
        """调用外部模型将同一时段多渠道事件整理成第一人称纪要与候选锚点；失败返回 None 走规则兜底。"""
        if not self._base_url or not self._model:
            return None
        lines = [
            (
                f"[id={ev.id}][branch={ev.branch_id}][role={ev.actor_role}][vis={ev.visibility}]"
                f"[kind={ev.kind}] {ev.summary} | verbatim={ev.verbatim} | reaction={ev.reaction}"
            )
            for ev in events
        ]
        prompt = (
            "你是虚拟主播麦麦的记忆编年整理器。请把下面同一时段的多端经历整理为 JSON：\n"
            f"作息日={day_key} 时段={SLOT_LABELS.get(slot_key, slot_key)}\n"
            "输出字段：\n"
            '{"body": "含私聊在内的第一人称时段总结", '
            '"public_body": "严格去掉所有private事件与群隐私的公开总结", '
            '"group_body": "去掉private事件的群聊可见总结", '
            '"anchors": [{"anchor_type": "stance|promise", "content": "...", "source_event_id": 1, "status": "active|open|closed|void"}], '
            '"topics": [{"title": "...", "summary": "...", "reaction": "...", "source_event_id": 1}]}\n'
            "硬约束：anchors 只能从 actor_role=self 的事件中提取，严禁把 peer 的话当成麦麦自己的立场或承诺。\n"
            "事件列表：\n" + "\n".join(lines)
        )
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
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
            return SlotSynthesisDraft(
                body=str(data.get("body", "")).strip(),
                public_body=str(data.get("public_body", "")).strip(),
                group_body=str(data.get("group_body", "")).strip(),
                anchors=anchors,
                topics=topics,
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


def build_rule_draft(
    events: Sequence[ExperienceEventRow],
    *,
    known_external_ids: Sequence[str],
) -> SlotSynthesisDraft:
    """确定性规则合成器：在无外部 LLM 或接口故障时稳定产出三档可见性纪要与候选。"""
    body_lines: List[str] = []
    public_lines: List[str] = []
    group_lines: List[str] = []
    anchors: List[AnchorCandidate] = []
    topics: List[TopicCandidate] = []

    for ev in events:
        where = describe_visibility_scope(ev.visibility, branch_id=ev.branch_id)
        base = sanitize_for_model(ev.summary.strip(), known_external_ids)
        if ev.reaction.strip():
            base = f"{base}（想法：{sanitize_for_model(ev.reaction.strip(), known_external_ids)}）"
        elif ev.verbatim.strip() and ev.actor_role == "self" and ev.kind in ("browsed", "speech"):
            short_quote = sanitize_for_model(ev.verbatim.strip()[:60], known_external_ids)
            if short_quote and short_quote not in base:
                base = f"{base}，当时说道「{short_quote}」"

        line = f"在{where}{base}" if not base.startswith("在") else base
        if line not in body_lines:
            body_lines.append(line)
        if ev.visibility != "private":
            if line not in group_lines:
                group_lines.append(line)
            if line not in public_lines:
                public_lines.append(line)

        if ev.actor_role == "self":
            _extract_rule_anchors(ev, anchors)
        _extract_rule_topics(ev, topics, known_external_ids)

    return SlotSynthesisDraft(
        body="；".join(body_lines) + ("。" if body_lines else ""),
        public_body="；".join(public_lines) + ("。" if public_lines else ""),
        group_body="；".join(group_lines) + ("。" if group_lines else ""),
        anchors=anchors,
        topics=topics,
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
    if ev.verbatim.strip() and any(kw in text for kw in _PROMISE_KEYWORDS):
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
