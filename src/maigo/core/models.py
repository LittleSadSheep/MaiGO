"""SQLite 存储层数据结构（供仓储与业务服务共同复用）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ActorRef:
    """事件关联的外部参与者标识。"""

    platform: str
    external_id: str
    display_name: str = ""


@dataclass(frozen=True)
class NewExperienceEvent:
    """待写入的原始经历事件。"""

    idempotency_key: str
    branch_id: str
    occurred_at_ms: int
    kind: str
    actor_role: str
    visibility: str
    summary: str
    context_scope: str = ""
    actors: List[ActorRef] = field(default_factory=list)
    verbatim: str = ""
    reaction: str = ""
    save_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExperienceEventRow:
    """落库后的原始经历事件记录。"""

    id: int
    idempotency_key: str
    branch_id: str
    occurred_at_ms: int
    ingested_at_ms: int
    kind: str
    actor_role: str
    visibility: str
    context_scope: str
    actors: List[ActorRef]
    summary: str
    verbatim: str
    reaction: str
    save_id: Optional[str]
    metadata: Dict[str, Any]
    synthesized_at_ms: Optional[int] = None


@dataclass(frozen=True)
class DailyChronicleRow:
    """日内作息时段纪要行。"""

    id: int
    day_key: str
    slot_key: str
    status: str
    body: str
    public_body: str
    group_body: str
    source_event_ids: List[int]
    sealed_at_ms: Optional[int]
    updated_at_ms: int
    mood_tag: str = ""
    unfinished_thread: str = ""


@dataclass(frozen=True)
class PersonaAnchorRow:
    """言行立场与跨端承诺锚点记录。"""

    id: int
    anchor_type: str
    content: str
    verbatim: str
    source_event_id: Optional[int]
    branch_id: str
    visibility: str
    save_id: Optional[str]
    target_person_id: Optional[int]
    status: str
    note: str
    created_at_ms: int
    updated_at_ms: int
    superseded_by_id: Optional[int] = None


@dataclass(frozen=True)
class TopicSeedRow:
    """带多分支消费轨迹的鲜活话头记录。"""

    id: int
    title: str
    summary: str
    reaction: str
    source_branch: str
    source_event_id: Optional[int]
    visibility: str
    consumed_branches: List[str]
    status: str
    created_at_ms: int
    updated_at_ms: int
    expires_at_ms: int


@dataclass(frozen=True)
class PersonRow:
    """跨端人物主档案记录。"""

    id: int
    display_name: str
    notes: str
    visibility_tier: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class PersonIdentityRow:
    """人物绑定的外部平台账号记录。"""

    id: int
    person_id: int
    platform: str
    external_id: str
    external_name: str
    bind_source: str
    bound_at_ms: int


@dataclass(frozen=True)
class PersonInteractionRow:
    """跨端人物互动履历行。"""

    id: int
    person_id: int
    branch_id: str
    platform: str
    visibility: str
    summary: str
    occurred_at_ms: int
    source_event_id: Optional[int]


@dataclass(frozen=True)
class BindTokenRow:
    """用户自助跨端绑定口令记录。"""

    token: str
    person_id: int
    platform_hint: str
    created_at_ms: int
    expires_at_ms: int
    used_at_ms: Optional[int]


@dataclass(frozen=True)
class IdentityCandidateRow:
    """待运营人工确认的疑似同人候选记录。"""

    id: int
    person_id: int
    platform: str
    external_id: str
    external_name: str
    reason: str
    status: str
    created_at_ms: int
    resolved_at_ms: Optional[int]


@dataclass(frozen=True)
class CheckoutSessionRow:
    """场次型接入端签出句柄记录。"""

    checkout_id: str
    branch_id: str
    external_session_id: str
    kind: str
    title: str
    platform: str
    room_id: str
    segment_title: str
    save_id: Optional[str]
    visibility: str
    active: bool
    started_at_ms: int
    last_heartbeat_ms: int
    ended_at_ms: Optional[int]


@dataclass(frozen=True)
class BranchPresenceRow:
    """各接入分支实时在线与活跃状态。"""

    branch_id: str
    mode: str
    status: str
    detail: Dict[str, Any]
    last_seen_ms: int


@dataclass(frozen=True)
class SaveStateRow:
    """按游戏存档共享的跨宿主最新世界近况。"""

    save_id: str
    last_branch_id: str
    summary: str
    updated_at_ms: int
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class LifeArcRow:
    """温层近期主线（连续多日进行的跨端故事或项目线索）。"""

    id: int
    title: str
    summary: str
    public_summary: str
    status: str
    visibility: str
    save_id: Optional[str]
    mood_hint: str
    source_event_ids: List[int]
    last_event_at_ms: int
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class EmbeddingRow:
    """单条记忆的语义向量持久化记录（BLOB 存小端 float32 数组）。"""

    id: int
    item_type: str
    item_id: int
    model: str
    dims: int
    vector_blob: bytes
    content_hash: str
    updated_at_ms: int
