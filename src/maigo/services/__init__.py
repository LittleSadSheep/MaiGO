"""MaiGO 核心业务服务包。"""

from maigo.services.identity import (
    ActorCardProjection,
    IdentityService,
    PersonDirectoryEntry,
    PersonWithIdentities,
)
from maigo.services.projector import (
    AnchorProjection,
    ContextProjection,
    TopicProjection,
    ViewProjector,
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
    SlotSynthesisDraft,
    SynthesisLLMClient,
    TopicCandidate,
    compute_day_and_slot,
)

__all__ = [
    "ActorCardProjection",
    "AnchorCandidate",
    "AnchorProjection",
    "BackgroundSynthesizer",
    "ChronicleSynthesizer",
    "ContextProjection",
    "IdentityService",
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
