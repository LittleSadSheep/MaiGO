"""阶段 1 配置与 SQLite 存储层单元测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from maiswitch.config import load_config
from maiswitch.storage import (
    EXPECTED_TABLES,
    SCHEMA_VERSION,
    ActorRef,
    NewExperienceEvent,
    SwitchDatabase,
    apply_migrations,
)
from maiswitch.storage.repos import (
    DailyChronicleRepo,
    ExperienceEventRepo,
    PersonaAnchorRepo,
    PersonRepo,
    SessionRepo,
    TopicSeedRepo,
)


class FakeClock:
    """可手动推进的确定性毫秒时钟。"""

    def __init__(self, start_ms: int = 1_795_488_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, delta_ms: int) -> None:
        self.now_ms += delta_ms


def test_config_env_overrides(tmp_path: Path) -> None:
    toml_file = tmp_path / "config.toml"
    toml_file.write_text("port = 9001\n[synthesizer]\nbatch_event_threshold = 12\n", encoding="utf-8")
    cfg = load_config(
        toml_file,
        env={
            "MAISWITCH_PORT": "8769",
            "MAISWITCH_AUTH_TOKEN": "demo-token",
            "MAISWITCH_SYNTH_BATCH_THRESHOLD": "5",
        },
    )
    assert cfg.port == 8769
    assert cfg.auth_token == "demo-token"
    assert cfg.synthesizer.batch_event_threshold == 5


async def test_migrations_idempotent_and_wal(tmp_path: Path) -> None:
    clock = FakeClock()
    db_file = tmp_path / "switch.sqlite3"
    db = SwitchDatabase(db_file, clock_ms=clock)
    await db.initialize()

    def _inspect(conn: sqlite3.Connection) -> tuple[str, list[str], int]:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        tables = [
            str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
        ]
        # 重复应用迁移应保持幂等
        ver = apply_migrations(conn, clock_ms=clock)
        return mode, tables, ver

    mode, tables, ver = await db.run(_inspect)
    assert mode == "wal"
    assert ver == SCHEMA_VERSION
    for expected in EXPECTED_TABLES:
        assert expected in tables

    await db.close()
    # 验证 Windows 下关闭后文件可正常删除
    db_file.unlink()
    assert not db_file.exists()


async def test_event_idempotency_and_fts_search(tmp_path: Path) -> None:
    clock = FakeClock()
    db = SwitchDatabase(tmp_path / "events.sqlite3", clock_ms=clock)
    await db.initialize()
    event_repo = ExperienceEventRepo(clock_ms=clock)

    e1 = NewExperienceEvent(
        idempotency_key="bili-1",
        branch_id="bilibili_ops",
        occurred_at_ms=clock(),
        kind="browsed",
        actor_role="self",
        visibility="public",
        context_scope="bilibili:video:BV1001",
        actors=[ActorRef(platform="bilibili", external_id="20002", display_name="手工UP主")],
        summary="下午刷到用粘土捏萨卡班甲鱼的视频",
        verbatim="这个眼神简直一模一样",
        reaction="晚上直播杂谈可以聊手工翻车经历",
    )
    e2 = NewExperienceEvent(
        idempotency_key="qq-priv-1",
        branch_id="maibot",
        occurred_at_ms=clock() + 1000,
        kind="interaction",
        actor_role="peer",
        visibility="private",
        context_scope="qq:private:10001",
        actors=[ActorRef(platform="qq", external_id="10001", display_name="老熟人甲")],
        summary="老熟人甲私聊说了今晚加班很累",
    )

    first = await db.run(lambda c: event_repo.ingest_batch(c, [e1, e2]))
    assert len(first.inserted_ids) == 2
    assert first.deduplicated_count == 0

    dup = await db.run(lambda c: event_repo.ingest_batch(c, [e1]))
    assert dup.inserted_ids == []
    assert dup.deduplicated_count == 1

    # 检索“粘土”应命中 e1（中文字串混合检索）
    hits_pub = await db.run(lambda c: event_repo.search(c, query="粘土", since_ms=0, visibilities=["public"], limit=5))
    assert len(hits_pub) == 1
    assert hits_pub[0].id == first.inserted_ids[0]

    # 公开搜索绝不返回 private 记录
    hits_priv_from_pub = await db.run(
        lambda c: event_repo.search(c, query="加班", since_ms=0, visibilities=["public", "group"], limit=5)
    )
    assert hits_priv_from_pub == []

    await db.close()


async def test_cascade_delete_and_other_repos(tmp_path: Path) -> None:
    clock = FakeClock()
    db = SwitchDatabase(tmp_path / "repos.sqlite3", clock_ms=clock)
    await db.initialize()
    person_repo = PersonRepo(clock_ms=clock)
    chronicle_repo = DailyChronicleRepo(clock_ms=clock)
    anchor_repo = PersonaAnchorRepo(clock_ms=clock)
    topic_repo = TopicSeedRepo(clock_ms=clock)
    session_repo = SessionRepo(clock_ms=clock)
    event_repo = ExperienceEventRepo(clock_ms=clock)

    def _run_ops(conn: sqlite3.Connection) -> None:
        ing = event_repo.ingest_batch(
            conn,
            [
                NewExperienceEvent(
                    idempotency_key="ev-1",
                    branch_id="maibot",
                    occurred_at_ms=clock(),
                    kind="speech",
                    actor_role="self",
                    visibility="group",
                    summary="答应晚上建灯塔",
                    verbatim="今晚直播带大家把新灯塔建好",
                )
            ],
        )
        ev_id = ing.inserted_ids[0]

        p = person_repo.create_person(conn, display_name="老熟人甲")
        person_repo.bind_identity(conn, person_id=p.id, platform="qq", external_id="10001", external_name="甲")
        person_repo.add_interaction(
            conn,
            person_id=p.id,
            branch_id="maibot",
            platform="qq",
            visibility="group",
            summary="在群里约定晚上建灯塔",
            occurred_at_ms=clock(),
            source_event_id=ev_id,
        )
        assert len(person_repo.list_identities_for_person(conn, p.id)) == 1
        assert len(person_repo.list_interactions(conn, person_id=p.id, visibilities=["group"])) == 1

        # 级联删除人物应清理绑定与互动记录
        assert person_repo.delete_person(conn, p.id) is True
        assert person_repo.get_identity(conn, platform="qq", external_id="10001") is None

        # 编年、锚点、话头与场次仓储冒烟
        slot = chronicle_repo.upsert_slot(
            conn,
            day_key="2026-11-24",
            slot_key="afternoon",
            status="sealed",
            body="下午在群里聊了新灯塔。",
            public_body="下午刷到粘土视频。",
            group_body="下午在群里聊了新灯塔。",
            source_event_ids=[ev_id],
        )
        assert slot.status == "sealed"
        assert slot.sealed_at_ms is not None

        anc = anchor_repo.create(
            conn,
            anchor_type="promise",
            content="今晚直播建新灯塔",
            verbatim="今晚直播带大家把新灯塔建好",
            source_event_id=ev_id,
            branch_id="maibot",
            visibility="group",
        )
        updated_anc = anchor_repo.update(conn, anc.id, status="closed", note="直播已建完")
        assert updated_anc is not None and updated_anc.status == "closed"

        seed = topic_repo.create(
            conn,
            title="粘土甲鱼视频",
            summary="下午刷到的手工视频",
            reaction="好可爱",
            source_branch="bilibili_ops",
            source_event_id=ev_id,
            visibility="public",
            consumed_branches=["maibot"],
            expires_at_ms=clock() + 3600_000,
        )
        assert topic_repo.list_available_for_branch(conn, branch_id="maibot", visibilities=["public"]) == []
        for_ama = topic_repo.list_available_for_branch(conn, branch_id="amaidesu", visibilities=["public"])
        assert [t.id for t in for_ama] == [seed.id]

        co = session_repo.create_checkout(
            conn,
            checkout_id="chk-1",
            branch_id="amaidesu",
            external_session_id="42",
            kind="live",
            title="杂谈与建灯塔",
            platform="bilibili",
            room_id="999001",
            segment_title="杂谈",
            save_id="save-alpha",
            visibility="public",
        )
        assert co.active is True
        session_repo.upsert_save_state(conn, save_id="save-alpha", branch_id="amaidesu", summary="灯塔地基已完成")
        assert session_repo.get_save_state(conn, "save-alpha") is not None
        session_repo.finish_checkout(conn, checkout_id="chk-1")
        assert session_repo.list_active_checkouts(conn) == []

    await db.run(_run_ops)
    await db.close()
