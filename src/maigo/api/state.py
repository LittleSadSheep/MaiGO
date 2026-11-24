"""API 应用上下文、Bearer 鉴权与 SSE 变更通知广播器。"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, Optional, Set

from fastapi import Depends, Header, HTTPException, Query, Request

from maigo.config import MaiGOConfig
from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.services import (
    BackgroundSynthesizer,
    ChronicleSynthesizer,
    EmbeddingClient,
    HybridRecallService,
    IdentityService,
    MaintenanceService,
    ViewProjector,
)
from maigo.services.synthesizer_rules import SynthesisLLMClient
from maigo.storage import MaiGODatabase
from maigo.storage.repos import (
    DailyChronicleRepo,
    EmbeddingRepo,
    ExperienceEventRepo,
    LifeArcRepo,
    PersonaAnchorRepo,
    PersonRepo,
    SessionRepo,
    TopicSeedRepo,
)

logger = get_logger("api.state")


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

    def __post_init__(self) -> None:
        self.events_repo = ExperienceEventRepo(clock_ms=self.clock_ms)
        self.chronicle_repo = DailyChronicleRepo(clock_ms=self.clock_ms)
        self.arcs_repo = LifeArcRepo(clock_ms=self.clock_ms)
        self.anchors_repo = PersonaAnchorRepo(clock_ms=self.clock_ms)
        self.topics_repo = TopicSeedRepo(clock_ms=self.clock_ms)
        self.persons_repo = PersonRepo(clock_ms=self.clock_ms)
        self.sessions_repo = SessionRepo(clock_ms=self.clock_ms)
        self.embeddings_repo = EmbeddingRepo(clock_ms=self.clock_ms)


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


def get_ctx(request: Request) -> MaiGOContext:
    """从 FastAPI app.state 获取上下文。"""
    return request.app.state.maigo_ctx


CtxDep = Annotated[MaiGOContext, Depends(get_ctx)]


def verify_token_value(expected_token: str, *, authorization: Optional[str], query_token: Optional[str] = None) -> bool:
    """恒定时间比较 Bearer 令牌；当配置为空串时不强制拦截。"""
    clean_expected = expected_token.strip()
    if not clean_expected:
        return True
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            presented = authorization[len(prefix) :].strip()
            return secrets.compare_digest(presented, clean_expected)
    if query_token:
        return secrets.compare_digest(query_token.strip(), clean_expected)
    return False


async def require_auth(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> None:
    """路由鉴权依赖：令牌配置非空时拦截未授权访问。"""
    ctx = get_ctx(request)
    if not verify_token_value(ctx.config.auth_token, authorization=authorization, query_token=token):
        raise HTTPException(status_code=401, detail="无效或缺失的 Bearer 访问令牌")
