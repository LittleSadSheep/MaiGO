"""MaiSwitch 存储包：SQLite 连接管理、迁移与仓储导出。"""

from maiswitch.storage.db import SwitchDatabase
from maiswitch.storage.migrations import apply_migrations, verify_expected_tables
from maiswitch.storage.models import (
    ActorRef,
    BindTokenRow,
    BranchPresenceRow,
    CheckoutSessionRow,
    DailyChronicleRow,
    ExperienceEventRow,
    IdentityCandidateRow,
    NewExperienceEvent,
    PersonaAnchorRow,
    PersonIdentityRow,
    PersonInteractionRow,
    PersonRow,
    SaveStateRow,
    TopicSeedRow,
)
from maiswitch.storage.schema import EXPECTED_TABLES, SCHEMA_VERSION, build_schema_sql

__all__ = [
    "ActorRef",
    "BindTokenRow",
    "BranchPresenceRow",
    "CheckoutSessionRow",
    "DailyChronicleRow",
    "EXPECTED_TABLES",
    "ExperienceEventRow",
    "IdentityCandidateRow",
    "NewExperienceEvent",
    "PersonaAnchorRow",
    "PersonIdentityRow",
    "PersonInteractionRow",
    "PersonRow",
    "SCHEMA_VERSION",
    "SaveStateRow",
    "SwitchDatabase",
    "TopicSeedRow",
    "apply_migrations",
    "build_schema_sql",
    "verify_expected_tables",
]
