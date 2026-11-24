"""MaiGO 核心业务服务包。"""

from maigo.services.identity import (
    ActorCardProjection,
    IdentityService,
    PersonDirectoryEntry,
    PersonWithIdentities,
)
from maigo.services.maintenance import MaintenanceService
from maigo.services.projector import (
    AnchorProjection,
    ContextProjection,
    TopicProjection,
    ViewProjector,
)
from maigo.services.retrieval import (
    EmbeddingClient,
    HybridRecallService,
    OpenAIEmbeddingClient,
)
from maigo.services.sanitizer import (
    allowed_visibilities,
    describe_visibility_scope,
    normalize_visibility,
    sanitize_for_model,
)
from maigo.services.synthesizer import (
    BackgroundSynthesizer,
    ChronicleSynthesizer,
    SynthesisReport,
)
from maigo.services.synthesizer_rules import (
    SLOT_LABELS,
    AnchorCandidate,
    ArcCandidate,
    SlotSynthesisDraft,
    SynthesisLLMClient,
    TopicCandidate,
    compute_day_and_slot,
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
