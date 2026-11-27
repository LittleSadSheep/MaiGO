"""应用依赖容器、仓储装配与内部变更通知广播器。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from maigo.config import MaiGOConfig
from maigo.core import (
    BackgroundSynthesizer,
    ChronicleSynthesizer,
    EmbeddingClient,
    HybridRecallService,
    IdentityService,
    MaintenanceService,
    ViewProjector,
)
from maigo.core.digest_rules import SynthesisLLMClient
from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.storage import MaiGODatabase
from maigo.storage.repos import (
    DailyChronicleRepo,
    EmbeddingRepo,
    EpisodeRepo,
    ExperienceEventRepo,
    KnowledgeGraphRepo,
    LifeArcRepo,
    PersonaAnchorRepo,
    PersonRepo,
    SessionRepo,
    TopicSeedRepo,
)

logger = get_logger("container")


class NotificationBroker:
    """轻量 SSE 事件广播中心：向连接中的订阅端实时推送简报或在线状态变更。"""

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        """非阻塞向所有在线订阅队列投入 SSE 帧，满队列丢弃最旧帧防堆积。"""
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.debug("SSE 订阅端队列已满，已跳过本条通知")


@dataclass
class MaiGOContext:
    """FastAPI 与 MCP 共享的服务依赖容器。"""

    config: MaiGOConfig
    db: MaiGODatabase
    clock_ms: ClockMs
    identity: IdentityService
    retrieval: HybridRecallService
    maintenance: MaintenanceService
    synthesizer: ChronicleSynthesizer
    bg_synthesizer: BackgroundSynthesizer
    projector: ViewProjector
    broker: NotificationBroker = field(default_factory=NotificationBroker)
    events_repo: ExperienceEventRepo = field(init=False)
    chronicle_repo: DailyChronicleRepo = field(init=False)
    arcs_repo: LifeArcRepo = field(init=False)
    anchors_repo: PersonaAnchorRepo = field(init=False)
    topics_repo: TopicSeedRepo = field(init=False)
    persons_repo: PersonRepo = field(init=False)
    sessions_repo: SessionRepo = field(init=False)
    embeddings_repo: EmbeddingRepo = field(init=False)
    episodes_repo: EpisodeRepo = field(init=False)
    knowledge_repo: KnowledgeGraphRepo = field(init=False)

    def __post_init__(self) -> None:
        self.events_repo = ExperienceEventRepo(clock_ms=self.clock_ms)
        self.chronicle_repo = DailyChronicleRepo(clock_ms=self.clock_ms)
        self.arcs_repo = LifeArcRepo(clock_ms=self.clock_ms)
        self.anchors_repo = PersonaAnchorRepo(clock_ms=self.clock_ms)
        self.topics_repo = TopicSeedRepo(clock_ms=self.clock_ms)
        self.persons_repo = PersonRepo(clock_ms=self.clock_ms)
        self.sessions_repo = SessionRepo(clock_ms=self.clock_ms)
        self.embeddings_repo = EmbeddingRepo(clock_ms=self.clock_ms)
        self.episodes_repo = EpisodeRepo(clock_ms=self.clock_ms)
        self.knowledge_repo = KnowledgeGraphRepo(clock_ms=self.clock_ms)


def build_context(
    config: MaiGOConfig,
    *,
    clock_ms: ClockMs = now_ms,
    llm_client: Optional[SynthesisLLMClient] = None,
    embedding_client: Optional[EmbeddingClient] = None,
) -> MaiGOContext:
    """组装核心依赖容器、混合召回、运维工具与后台编年调度器。"""
    db = MaiGODatabase(config.db_path, clock_ms=clock_ms)
    identity = IdentityService(db, identity_salt=config.identity_salt, clock_ms=clock_ms)
    retrieval = HybridRecallService(
        db,
        identity=identity,
        embedding_config=config.embedding,
        embedding_client=embedding_client,
        clock_ms=clock_ms,
    )
    maintenance = MaintenanceService(db, config=config, clock_ms=clock_ms)
    synthesizer = ChronicleSynthesizer(
        db,
        identity=identity,
        config=config.synthesizer,
        day_rollover_hour=config.day_rollover_hour,
        timezone_offset_hours=config.timezone_offset_hours,
        clock_ms=clock_ms,
        llm_client=llm_client,
        retrieval=retrieval,
        maintenance=maintenance,
        auto_daily_backup=config.backup.auto_daily,
        auto_index_embeddings=config.embedding.auto_index,
    )
    broker = NotificationBroker()

    def _on_brief_updated() -> None:
        broker.publish("brief_updated", {"ts_ms": int(clock_ms())})

    bg_synthesizer = BackgroundSynthesizer(
        db,
        synthesizer,
        config.synthesizer,
        clock_ms=clock_ms,
        on_brief_updated=_on_brief_updated,
    )
    projector = ViewProjector(
        db,
        identity=identity,
        day_rollover_hour=config.day_rollover_hour,
        timezone_offset_hours=config.timezone_offset_hours,
        recent_window_ms=config.synthesizer.recent_window_ms,
        clock_ms=clock_ms,
        retrieval=retrieval,
        wake_hint_enabled=config.synthesizer.wake_hint_enabled,
    )
    return MaiGOContext(
        config=config,
        db=db,
        clock_ms=clock_ms,
        identity=identity,
        retrieval=retrieval,
        maintenance=maintenance,
        synthesizer=synthesizer,
        bg_synthesizer=bg_synthesizer,
        projector=projector,
        broker=broker,
    )
