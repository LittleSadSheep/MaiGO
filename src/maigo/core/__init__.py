"""MaiGO 核心领域与记忆引擎包。"""

from maigo.core.context import (
    AnchorProjection,
    ContextProjection,
    TopicProjection,
    ViewProjector,
)
from maigo.core.digest import (
    BackgroundSynthesizer,
    ChronicleSynthesizer,
    SynthesisReport,
)
from maigo.core.digest_rules import (
    SLOT_LABELS,
    AnchorCandidate,
    ArcCandidate,
    SlotSynthesisDraft,
    SynthesisLLMClient,
    TopicCandidate,
    compute_day_and_slot,
)
from maigo.core.identity import (
    ActorCardProjection,
    IdentityService,
    PersonDirectoryEntry,
    PersonWithIdentities,
)
from maigo.core.maintenance import MaintenanceService
from maigo.core.privacy import (
    allowed_visibilities,
    describe_visibility_scope,
    normalize_visibility,
    sanitize_for_model,
)
from maigo.core.recall import (
    EmbeddingClient,
    HybridRecallService,
    OpenAIEmbeddingClient,
    RecallHit,
    RecallResult,
)

__all__ = [
    "ActorCardProjection",
    "AnchorCandidate",
    "AnchorProjection",
    "ArcCandidate",
    "BackgroundSynthesizer",
    "ChronicleSynthesizer",
    "ContextProjection",
    "EmbeddingClient",
    "HybridRecallService",
    "IdentityService",
    "MaintenanceService",
    "OpenAIEmbeddingClient",
    "PersonDirectoryEntry",
    "PersonWithIdentities",
    "RecallHit",
    "RecallResult",
    "SLOT_LABELS",
    "SlotSynthesisDraft",
    "SynthesisLLMClient",
    "SynthesisReport",
    "TopicCandidate",
    "TopicProjection",
    "ViewProjector",
    "allowed_visibilities",
    "compute_day_and_slot",
    "describe_visibility_scope",
    "normalize_visibility",
    "sanitize_for_model",
]
