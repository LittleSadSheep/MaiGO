"""MaiGO 存储包：SQLite 连接管理、迁移与仓储导出。"""

from maigo.storage.db import MaiGODatabase
from maigo.storage.migrations import apply_migrations, verify_expected_tables
from maigo.storage.models import (
    ActorRef,
    BindTokenRow,
    BranchPresenceRow,
    CheckoutSessionRow,
    DailyChronicleRow,
    EmbeddingRow,
    ExperienceEventRow,
    IdentityCandidateRow,
    LifeArcRow,
    NewExperienceEvent,
    PersonaAnchorRow,
    PersonIdentityRow,
    PersonInteractionRow,
    PersonRow,
    SaveStateRow,
    TopicSeedRow,
)
from maigo.storage.schema import EXPECTED_TABLES, SCHEMA_VERSION, build_schema_sql

__all__ = [
    "ActorRef",
    "BindTokenRow",
    "BranchPresenceRow",
    "CheckoutSessionRow",
    "DailyChronicleRow",
    "EXPECTED_TABLES",
    "EmbeddingRow",
    "ExperienceEventRow",
    "IdentityCandidateRow",
    "LifeArcRow",
    "NewExperienceEvent",
    "NewExperienceEvent",
    "PersonaAnchorRow",
    "PersonIdentityRow",
    "PersonInteractionRow",
    "PersonRow",
    "SCHEMA_VERSION",
    "SaveStateRow",
    "MaiGODatabase",
    "TopicSeedRow",
    "apply_migrations",
    "build_schema_sql",
    "verify_expected_tables",
]
