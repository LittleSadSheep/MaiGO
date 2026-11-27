"""MaiGO HTTP REST API 与看板管理接口 Pydantic 契约定义。"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ActorRefIn(BaseModel):
    """外部参与者引用。"""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)


class EventIn(BaseModel):
    """统一经历事件入站信封（对应 ``protocol.md`` 第 2 节）。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(..., min_length=1, max_length=256)
    branch_id: str = Field(..., min_length=1, max_length=64)
    occurred_at_ms: int = Field(..., ge=0)
    kind: Literal["speech", "interaction", "browsed", "milestone", "promise_update", "forget_request"]
    actor_role: Literal["self", "peer", "system"]
    visibility: Optional[Literal["public", "group", "private"]] = None
    context_scope: str = Field(default="", max_length=256)
    actors: List[ActorRefIn] = Field(default_factory=list)
    summary: str = Field(..., min_length=1, max_length=4000)
    verbatim: str = Field(default="", max_length=4000)
    reaction: str = Field(default="", max_length=2000)
    save_id: Optional[str] = Field(default=None, max_length=128)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EventsBatchIn(BaseModel):
    """批量事件入站包裹体。"""

    model_config = ConfigDict(extra="forbid")

    events: List[EventIn] = Field(default_factory=list)


EventsPayloadIn = Union[EventsBatchIn, EventIn]


class EventsIngestOut(BaseModel):
    """事件入站结果。"""

    ok: bool = True
    accepted: int
    deduplicated: int
    inserted_ids: List[int]


class ActorCardOut(BaseModel):
    """脱敏跨端熟人卡片。"""

    person_id: int
    display_name: str
    hint: str
    platforms: List[str]


class TopicProjectionOut(BaseModel):
    """投影话头条目。"""

    id: int
    title: str
    summary: str
    reaction: str
    source_branch: str
    consumed_branches: List[str]
    transition_hint: str


class AnchorProjectionOut(BaseModel):
    """投影立场或承诺条目。"""

    id: int
    anchor_type: str
    content: str
    status: str
    source_branch: str
    save_id: Optional[str] = None


class ActiveSessionOut(BaseModel):
    """跨端活跃场次状态。"""

    checkout_id: str
    branch_id: str
    external_session_id: str
    title: str
    platform: str
    room_id: str
    segment_title: str
    save_id: Optional[str] = None
    started_at_ms: int
    last_heartbeat_ms: int


class ContextViewOut(BaseModel):
    """流式端拉取的快慢分层上下文视图。"""

    brief_etag: str
    stable_unchanged: bool = False
    stable_brief: Optional[str] = None
    live_context: str = ""
    actor_cards: List[ActorCardOut] = Field(default_factory=list)
    fresh_topics: List[TopicProjectionOut] = Field(default_factory=list)
    open_promises: List[AnchorProjectionOut] = Field(default_factory=list)
    active_sessions: List[ActiveSessionOut] = Field(default_factory=list)
    save_brief: str = ""


class TopicConsumeIn(BaseModel):
    """标记话头消费请求。"""

    model_config = ConfigDict(extra="forbid")

    branch_id: str = Field(..., min_length=1, max_length=64)


class CheckoutRequest(BaseModel):
    """场次型端开播/启动签出请求。"""

    model_config = ConfigDict(extra="forbid")

    branch_id: str = Field(..., min_length=1, max_length=64)
    external_session_id: str = Field(..., min_length=1, max_length=128)
    kind: Literal["live", "rehearsal"] = "live"
    visibility: Literal["public", "group", "private"] = "public"
    title: str = Field(default="", max_length=256)
    platform: str = Field(default="bilibili", max_length=64)
    room_id: str = Field(default="", max_length=128)
    segment_title: str = Field(default="", max_length=256)
    save_id: Optional[str] = Field(default=None, max_length=128)


class PersonDirectoryEntryOut(BaseModel):
    """签出下发的本地熟人映射（外层键仅作宿主索引，hint 脱敏）。"""

    person_id: int
    platform: str
    external_id: str
    display_name: str
    prompt_hint: str


class CheckoutResponse(BaseModel):
    """开播签出打包快照。"""

    checkout_id: str
    brief_etag: str
    stable_brief: str
    live_context: str = ""
    open_promises: List[AnchorProjectionOut] = Field(default_factory=list)
    topic_cards: List[TopicProjectionOut] = Field(default_factory=list)
    person_directory: List[PersonDirectoryEntryOut] = Field(default_factory=list)
    save_brief: str = ""


class HeartbeatRequest(BaseModel):
    """场中心跳与话头消费回执。"""

    model_config = ConfigDict(extra="forbid")

    segment_title: Optional[str] = Field(default=None, max_length=256)
    save_id: Optional[str] = Field(default=None, max_length=128)
    title: Optional[str] = Field(default=None, max_length=256)
    consumed_seed_ids: List[int] = Field(default_factory=list)


class HeartbeatResponse(BaseModel):
    """心跳响应。"""

    ok: bool = True
    checkout_id: str
    consumed_count: int = 0


class CheckinArchiveLineIn(BaseModel):
    """签入上传的单条场次档案行。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    verbatim: str = Field(default="", max_length=2000)
    highlight: bool = False
    save_id: Optional[str] = Field(default=None, max_length=128)


class CheckinPromiseIn(BaseModel):
    """签入上传的跨端承诺变更。"""

    model_config = ConfigDict(extra="forbid")

    anchor_id: Optional[int] = None
    content: str = Field(..., min_length=1, max_length=400)
    verbatim: str = Field(default="", max_length=2000)
    status: Literal["open", "closed", "void"] = "open"
    save_id: Optional[str] = Field(default=None, max_length=128)
    note: str = Field(default="", max_length=400)


class EpisodeIn(BaseModel):
    """L0 场次目录入站契约（对应一场直播、长对话窗口或专题游戏段）。"""

    model_config = ConfigDict(extra="forbid")

    branch_id: str = Field(default="", max_length=64)
    external_episode_id: str = Field(..., min_length=1, max_length=128)
    kind: Literal["live", "chat_window", "game", "browse"] = "live"
    title: str = Field(default="", max_length=256)
    platform: str = Field(default="", max_length=64)
    save_id: Optional[str] = Field(default=None, max_length=128)
    visibility: Literal["public", "group", "private"] = "public"
    started_at_ms: int = Field(..., ge=0)
    ended_at_ms: Optional[int] = Field(default=None, ge=0)
    overview: str = Field(default="", max_length=4000)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EpisodeRecordIn(BaseModel):
    """L0 原件书库单条细粒度证据（不进入日内摘要，仅供按需全文与语义召回）。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(..., min_length=1, max_length=256)
    record_type: Literal[
        "game_event",
        "speech",
        "timeline_chunk",
        "work_doc",
        "creature",
        "archive_line",
        "interaction_fact",
    ]
    actor_role: Literal["self", "peer", "system"] = "self"
    visibility: Literal["public", "group", "private"] = "public"
    occurred_at_ms: int = Field(..., ge=0)
    save_id: Optional[str] = Field(default=None, max_length=128)
    title: str = Field(default="", max_length=256)
    content: str = Field(..., min_length=1, max_length=6000)
    verbatim: str = Field(default="", max_length=4000)
    external_episode_id: Optional[str] = Field(default=None, max_length=128)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArchiveIngestIn(BaseModel):
    """批量写入 L0 原件书库请求体。"""

    model_config = ConfigDict(extra="forbid")

    branch_id: str = Field(..., min_length=1, max_length=64)
    episode: Optional[EpisodeIn] = None
    external_episode_id: Optional[str] = Field(default=None, max_length=128)
    records: List[EpisodeRecordIn] = Field(default_factory=list)


class ArchiveIngestOut(BaseModel):
    """原件书库写入结果。"""

    ok: bool = True
    episode_id: Optional[int] = None
    accepted: int = 0
    deduplicated: int = 0
    inserted_ids: List[int] = Field(default_factory=list)


class KnowledgeEntityIn(BaseModel):
    """L2 实体图谱节点入站契约。"""

    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["person", "player", "creature", "place", "item", "project", "concept"] = "concept"
    canonical_key: str = Field(..., min_length=1, max_length=160)
    name: str = Field(..., min_length=1, max_length=160)
    platform: str = Field(default="", max_length=64)
    external_id: str = Field(default="", max_length=128)
    save_id: Optional[str] = Field(default=None, max_length=128)
    visibility: Literal["public", "group", "private"] = "public"
    summary: str = Field(default="", max_length=1000)
    aliases: List[str] = Field(default_factory=list)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    seen_at_ms: Optional[int] = Field(default=None, ge=0)


class KnowledgeRelationIn(BaseModel):
    """L2 实体关系事实边入站契约。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(..., min_length=1, max_length=256)
    subject: KnowledgeEntityIn
    predicate: str = Field(..., min_length=1, max_length=80)
    object_entity: Optional[KnowledgeEntityIn] = None
    object_text: str = Field(default="", max_length=500)
    fact_text: str = Field(..., min_length=1, max_length=2000)
    quote: str = Field(default="", max_length=1000)
    visibility: Literal["public", "group", "private"] = "public"
    save_id: Optional[str] = Field(default=None, max_length=128)
    external_episode_id: Optional[str] = Field(default=None, max_length=128)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    status: Literal["active", "superseded"] = "active"
    occurred_at_ms: int = Field(..., ge=0)


class KnowledgeIngestIn(BaseModel):
    """批量写入 L2 实体与关系事实请求体。"""

    model_config = ConfigDict(extra="forbid")

    branch_id: str = Field(..., min_length=1, max_length=64)
    entities: List[KnowledgeEntityIn] = Field(default_factory=list)
    relations: List[KnowledgeRelationIn] = Field(default_factory=list)


class KnowledgeIngestOut(BaseModel):
    """实体关系写入结果。"""

    ok: bool = True
    entities_upserted: int = 0
    relations_accepted: int = 0
    relations_deduplicated: int = 0
    skipped_forgotten: int = 0


class CheckinInteractionIn(BaseModel):
    """签入上传的本场熟人互动摘要。"""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)
    summary: str = Field(..., min_length=1, max_length=1000)
    kind: str = Field(default="", max_length=64)
    fact: str = Field(default="", max_length=1000)
    visibility: Literal["public", "group", "private"] = "public"


class CheckinRequest(BaseModel):
    """场次下播签入大推送请求体。"""

    model_config = ConfigDict(extra="forbid")

    ended_at_ms: int = Field(..., ge=0)
    overview: str = Field(default="", max_length=4000)
    save_id: Optional[str] = Field(default=None, max_length=128)
    save_summary: str = Field(default="", max_length=2000)
    archive_lines: List[CheckinArchiveLineIn] = Field(default_factory=list)
    archive_records: List[EpisodeRecordIn] = Field(default_factory=list)
    promises: List[CheckinPromiseIn] = Field(default_factory=list)
    interactions: List[CheckinInteractionIn] = Field(default_factory=list)
    social_facts: List[KnowledgeRelationIn] = Field(default_factory=list)
    consumed_seed_ids: List[int] = Field(default_factory=list)
    discarded: bool = False


class CheckinResponse(BaseModel):
    """下播签入处理结果。"""

    ok: bool = True
    checkout_id: str
    ignored_rehearsal: bool = False
    ingested_events: int = 0
    ingested_records: int = 0
    ingested_relations: int = 0
    updated_promises: int = 0
    sealed_slots: int = 0


# ---- 看板管理接口模型 ----


class AnchorCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_type: Literal["stance", "promise"]
    content: str = Field(..., min_length=1, max_length=500)
    verbatim: str = Field(..., min_length=1, max_length=2000)
    branch_id: str = Field(default="operator", max_length=64)
    visibility: Literal["public", "group", "private"] = "public"
    save_id: Optional[str] = Field(default=None, max_length=128)
    note: str = Field(default="", max_length=400)


class AnchorPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Optional[str] = Field(default=None, min_length=1, max_length=500)
    status: Optional[Literal["active", "open", "closed", "void", "superseded", "stale"]] = None
    note: Optional[str] = Field(default=None, max_length=400)


class QuickNoteIn(BaseModel):
    """运营日常随手投喂事件请求。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., min_length=1, max_length=2000)
    reaction: str = Field(default="", max_length=1000)
    kind: Literal["browsed", "note", "speech", "game_moment"] = "browsed"
    visibility: Literal["public", "group", "private"] = "public"
    branch_id: str = Field(default="operator", min_length=1, max_length=64)
    save_id: Optional[str] = Field(default=None, max_length=128)
    synthesize_now: bool = False


class LifeArcCreateIn(BaseModel):
    """创建或延续近期主线（温层记忆）请求。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=120)
    summary: str = Field(..., min_length=1, max_length=1000)
    public_summary: str = Field(default="", max_length=1000)
    visibility: Literal["public", "group", "private"] = "public"
    save_id: Optional[str] = Field(default=None, max_length=128)
    mood_hint: str = Field(default="", max_length=80)


class LifeArcPatchIn(BaseModel):
    """部分更新近期主线状态或内容请求。"""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    summary: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    public_summary: Optional[str] = Field(default=None, max_length=1000)
    status: Optional[Literal["active", "paused", "completed", "archived"]] = None
    mood_hint: Optional[str] = Field(default=None, max_length=80)


class TopicCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=120)
    summary: str = Field(..., min_length=1, max_length=1000)
    reaction: str = Field(default="", max_length=1000)
    source_branch: str = Field(default="operator", max_length=64)
    visibility: Literal["public", "group", "private"] = "public"
    ttl_ms: Optional[int] = Field(default=None, ge=60_000)


class TopicPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    summary: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    reaction: Optional[str] = Field(default=None, max_length=1000)
    status: Optional[Literal["active", "archived", "expired"]] = None


class PersonCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(..., min_length=1, max_length=100)
    notes: str = Field(default="", max_length=500)
    visibility_tier: Literal["public", "group", "private"] = "public"


class IdentityBindIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: int
    platform: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=128)
    external_name: str = Field(default="", max_length=128)


class IdentityUnbindIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=128)


class BindTokenIssueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: int
    platform_hint: str = Field(default="", max_length=64)
    ttl_ms: int = Field(default=24 * 3600 * 1000, ge=60_000)


class BindTokenRedeemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=1, max_length=64)
    platform: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=128)
    external_name: str = Field(default="", max_length=128)


class ForgetIdentityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=128)
    wipe_person: bool = False
