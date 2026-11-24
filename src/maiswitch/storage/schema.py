"""SQLite 表结构定义与唯一事实源。

所有时间戳字段均采用 Unix epoch 毫秒整数（``*_ms``）。
"""

from __future__ import annotations

SCHEMA_VERSION: int = 1

EXPECTED_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "experience_events",
    "experience_events_fts",
    "daily_chronicles",
    "persona_anchors",
    "persona_anchors_fts",
    "topic_seeds",
    "persons",
    "person_identities",
    "person_interactions",
    "bind_tokens",
    "identity_candidates",
    "forget_identities",
    "checkout_sessions",
    "branch_presence",
    "save_states",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS experience_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    occurred_at_ms INTEGER NOT NULL,
    ingested_at_ms INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('speech', 'interaction', 'browsed', 'milestone', 'promise_update', 'forget_request')),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('self', 'peer', 'system')),
    visibility TEXT NOT NULL CHECK (visibility IN ('public', 'group', 'private')),
    context_scope TEXT NOT NULL DEFAULT '',
    actors_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL,
    verbatim TEXT NOT NULL DEFAULT '',
    reaction TEXT NOT NULL DEFAULT '',
    save_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    synthesized_at_ms INTEGER,
    UNIQUE (branch_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_events_occurred ON experience_events (occurred_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_events_unsynth ON experience_events (synthesized_at_ms, occurred_at_ms ASC);
CREATE INDEX IF NOT EXISTS idx_events_save ON experience_events (save_id, occurred_at_ms DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS experience_events_fts USING fts5(
    summary,
    verbatim,
    reaction,
    context_scope,
    content='experience_events',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS trg_events_ai AFTER INSERT ON experience_events BEGIN
    INSERT INTO experience_events_fts(rowid, summary, verbatim, reaction, context_scope)
    VALUES (new.id, new.summary, new.verbatim, new.reaction, new.context_scope);
END;

CREATE TRIGGER IF NOT EXISTS trg_events_ad AFTER DELETE ON experience_events BEGIN
    INSERT INTO experience_events_fts(experience_events_fts, rowid, summary, verbatim, reaction, context_scope)
    VALUES ('delete', old.id, old.summary, old.verbatim, old.reaction, old.context_scope);
END;

CREATE TRIGGER IF NOT EXISTS trg_events_au AFTER UPDATE ON experience_events BEGIN
    INSERT INTO experience_events_fts(experience_events_fts, rowid, summary, verbatim, reaction, context_scope)
    VALUES ('delete', old.id, old.summary, old.verbatim, old.reaction, old.context_scope);
    INSERT INTO experience_events_fts(rowid, summary, verbatim, reaction, context_scope)
    VALUES (new.id, new.summary, new.verbatim, new.reaction, new.context_scope);
END;

CREATE TABLE IF NOT EXISTS daily_chronicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_key TEXT NOT NULL,
    slot_key TEXT NOT NULL CHECK (slot_key IN ('yesterday_carry', 'morning', 'afternoon', 'evening', 'late_night')),
    status TEXT NOT NULL CHECK (status IN ('open', 'sealed')),
    body TEXT NOT NULL DEFAULT '',
    public_body TEXT NOT NULL DEFAULT '',
    group_body TEXT NOT NULL DEFAULT '',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    sealed_at_ms INTEGER,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (day_key, slot_key)
);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    visibility_tier TEXT NOT NULL DEFAULT 'public' CHECK (visibility_tier IN ('public', 'group', 'private')),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_type TEXT NOT NULL CHECK (anchor_type IN ('stance', 'promise')),
    content TEXT NOT NULL,
    verbatim TEXT NOT NULL DEFAULT '',
    source_event_id INTEGER REFERENCES experience_events(id) ON DELETE SET NULL,
    branch_id TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'group', 'private')),
    save_id TEXT,
    target_person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'open', 'closed', 'void')),
    note TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_anchors_status ON persona_anchors (anchor_type, status, updated_at_ms DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS persona_anchors_fts USING fts5(
    content,
    verbatim,
    note,
    content='persona_anchors',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS trg_anchors_ai AFTER INSERT ON persona_anchors BEGIN
    INSERT INTO persona_anchors_fts(rowid, content, verbatim, note)
    VALUES (new.id, new.content, new.verbatim, new.note);
END;

CREATE TRIGGER IF NOT EXISTS trg_anchors_ad AFTER DELETE ON persona_anchors BEGIN
    INSERT INTO persona_anchors_fts(persona_anchors_fts, rowid, content, verbatim, note)
    VALUES ('delete', old.id, old.content, old.verbatim, old.note);
END;

CREATE TRIGGER IF NOT EXISTS trg_anchors_au AFTER UPDATE ON persona_anchors BEGIN
    INSERT INTO persona_anchors_fts(persona_anchors_fts, rowid, content, verbatim, note)
    VALUES ('delete', old.id, old.content, old.verbatim, old.note);
    INSERT INTO persona_anchors_fts(rowid, content, verbatim, note)
    VALUES (new.id, new.content, new.verbatim, new.note);
END;

CREATE TABLE IF NOT EXISTS topic_seeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    reaction TEXT NOT NULL DEFAULT '',
    source_branch TEXT NOT NULL,
    source_event_id INTEGER REFERENCES experience_events(id) ON DELETE SET NULL,
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'group', 'private')),
    consumed_branches_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'expired')),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_topics_active ON topic_seeds (status, expires_at_ms, updated_at_ms DESC);

CREATE TABLE IF NOT EXISTS person_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    external_name TEXT NOT NULL DEFAULT '',
    bind_source TEXT NOT NULL DEFAULT 'manual',
    bound_at_ms INTEGER NOT NULL,
    UNIQUE (platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_person_identities_pid ON person_identities (person_id);

CREATE TABLE IF NOT EXISTS person_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    branch_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'group', 'private')),
    summary TEXT NOT NULL,
    occurred_at_ms INTEGER NOT NULL,
    source_event_id INTEGER REFERENCES experience_events(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_person_interactions_pid ON person_interactions (person_id, occurred_at_ms DESC);

CREATE TABLE IF NOT EXISTS bind_tokens (
    token TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    platform_hint TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    used_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS identity_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    external_name TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at_ms INTEGER NOT NULL,
    resolved_at_ms INTEGER,
    UNIQUE (person_id, platform, external_id)
);

CREATE TABLE IF NOT EXISTS forget_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_hash TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS checkout_sessions (
    checkout_id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'live' CHECK (kind IN ('live', 'rehearsal')),
    title TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    room_id TEXT NOT NULL DEFAULT '',
    segment_title TEXT NOT NULL DEFAULT '',
    save_id TEXT,
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'group', 'private')),
    active INTEGER NOT NULL DEFAULT 1,
    started_at_ms INTEGER NOT NULL,
    last_heartbeat_ms INTEGER NOT NULL,
    ended_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_checkout_active ON checkout_sessions (active, last_heartbeat_ms DESC);

CREATE TABLE IF NOT EXISTS branch_presence (
    branch_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'streaming',
    status TEXT NOT NULL DEFAULT 'online',
    detail_json TEXT NOT NULL DEFAULT '{}',
    last_seen_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS save_states (
    save_id TEXT PRIMARY KEY,
    last_branch_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


def build_schema_sql() -> str:
    """返回完整建表 DDL（幂等）。"""
    return _SCHEMA_SQL
