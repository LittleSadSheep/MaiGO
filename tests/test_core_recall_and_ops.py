"""里程碑 2：双路混合检索、优雅降级、热备份、手账导出与 CLI 子命令测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import List, Optional, Sequence

from maigo.config import BackupConfig, EmbeddingConfig, MaiGOConfig, SynthesizerConfig, load_config
from maigo.core import ChronicleSynthesizer, HybridRecallService, IdentityService, MaintenanceService
from maigo.main import main as cli_main
from maigo.storage import SCHEMA_VERSION, ActorRef, MaiGODatabase, NewExperienceEvent
from maigo.storage.repos import DailyChronicleRepo, ExperienceEventRepo, LifeArcRepo, PersonaAnchorRepo, PersonRepo


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


class _MutableClock:
    def __init__(self, initial_ms: int) -> None:
        self.now_ms = initial_ms

    def __call__(self) -> int:
        return self.now_ms


class _FakeEmbeddingClient:
    """可控确定性向量客户端，按主题映射到 3 维正交/相关向量，支持模拟远端故障。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: int = 0

    @property
    def model_name(self) -> str:
        return "fake-emb-v1"

    async def embed_texts(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        if self.fail:
            return None
        self.calls += 1
        vecs: List[List[float]] = []
        for text in texts:
            if any(k in text for k in ("小鱼", "甲鱼", "粘土", "史前")):
                vecs.append([0.95, 0.2, 0.0])
            elif any(k in text for k in ("体检", "密码", "保密")):
                vecs.append([0.0, 0.95, 0.2])
            else:
                vecs.append([0.1, 0.1, 0.95])
        return vecs


async def test_hybrid_recall_semantic_fallback_and_visibility_isolation(tmp_path: Path) -> None:
    clock = _MutableClock(_ms(2026, 11, 24, 19, 0))
    db = MaiGODatabase(tmp_path / "hybrid.sqlite3", clock_ms=clock)
    await db.initialize()
    identity = IdentityService(db, clock_ms=clock)
    event_repo = ExperienceEventRepo(clock_ms=clock)
    person_repo = PersonRepo(clock_ms=clock)
    arc_repo = LifeArcRepo(clock_ms=clock)

    def _seed(conn: sqlite3.Connection) -> tuple[int, int, int]:
        p = person_repo.create_person(conn, display_name="老熟人甲")
        person_repo.bind_identity(conn, person_id=p.id, platform="bilibili", external_id="20002")
        ing = event_repo.ingest_batch(
            conn,
            [
                NewExperienceEvent(
                    idempotency_key="ev-fish",
                    branch_id="bilibili_ops",
                    occurred_at_ms=_ms(2026, 11, 24, 15, 0),
                    kind="browsed",
                    actor_role="self",
                    visibility="public",
                    actors=[ActorRef(platform="bilibili", external_id="20002", display_name="甲B站")],
                    summary="下午刷到用粘土捏萨卡班甲鱼的视频",
                    verbatim="呆呆的太像了",
                ),
                NewExperienceEvent(
                    idempotency_key="ev-secret",
                    branch_id="maibot",
                    occurred_at_ms=_ms(2026, 11, 24, 16, 0),
                    kind="interaction",
                    actor_role="peer",
                    visibility="private",
                    actors=[ActorRef(platform="qq", external_id="10001")],
                    summary="私聊悄悄说起周末去体检的保密安排",
                ),
            ],
        )
        arc = arc_repo.upsert_or_create(
            conn,
            title="捏粘土系列整活",
            summary="近期一直在看奇奇怪怪的手工动物视频",
            public_summary="近期一直在看奇奇怪怪的手工动物视频",
            source_event_ids=[ing.inserted_ids[0]],
            last_event_at_ms=_ms(2026, 11, 24, 15, 0),
        )
        return ing.inserted_ids[0], ing.inserted_ids[1], arc.id

    ev_pub_id, ev_priv_id, arc_id = await db.run(_seed)

    fake_emb = _FakeEmbeddingClient()
    recall_svc = HybridRecallService(
        db,
        identity=identity,
        embedding_config=EmbeddingConfig(model="fake-emb-v1"),
        embedding_client=fake_emb,
        clock_ms=clock,
    )
    assert recall_svc.mode == "hybrid"

    saved = await recall_svc.index_items(
        [
            ("event", ev_pub_id, "下午刷到用粘土捏萨卡班甲鱼的视频"),
            ("event", ev_priv_id, "私聊悄悄说起周末去体检的保密安排"),
            ("arc", arc_id, "捏粘土系列整活：近期一直在看奇奇怪怪的手工动物视频"),
        ]
    )
    assert saved == 3
    # 内容未变时再次 index_items 应命中 content_hash 幂等跳过，不发重复向量请求
    calls_before = fake_emb.calls
    assert await recall_svc.index_items([("event", ev_pub_id, "下午刷到用粘土捏萨卡班甲鱼的视频")]) == 0
    assert fake_emb.calls == calls_before

    # 用完全不同措辞“那个史前小鱼是啥”（稀疏关键词 0 命中），依赖语义向量仍能召回萨卡班甲鱼
    hit_sem = await recall_svc.recall("那个史前小鱼是啥", days=7, limit=5, target_visibility="public")
    assert "萨卡班甲鱼" in hit_sem
    assert "20002" not in hit_sem
    assert "体检" not in hit_sem

    # 私聊词汇在 public 视角不可被语义路泄露；切 private 后可正常召回
    assert "没有找到" in await recall_svc.recall("体检秘密如何", days=7, limit=5, target_visibility="public")
    priv_hit = await recall_svc.recall("体检秘密如何", days=7, limit=5, target_visibility="private")
    assert "体检" in priv_hit

    # 模拟向量服务故障：自动平滑退化为纯稀疏检索，且 trigram 仍能命中 3 字词“萨卡班”
    fake_emb.fail = True
    fallback_hit = await recall_svc.recall("萨卡班", days=7, limit=5, target_visibility="public")
    assert "萨卡班甲鱼" in fallback_hit

    await db.close()


async def test_backup_diary_export_and_cli_commands(tmp_path: Path, capsys) -> None:
    clock = _MutableClock(_ms(2026, 11, 24, 21, 0))
    db_file = tmp_path / "live.sqlite3"
    backup_dir = tmp_path / "backups"
    cfg = MaiGOConfig(
        db_path=str(db_file),
        backup=BackupConfig(dir=str(backup_dir), keep_days=2, auto_daily=True),
    )
    db = MaiGODatabase(db_file, clock_ms=clock)
    await db.initialize()

    chronicle_repo = DailyChronicleRepo(clock_ms=clock)
    anchor_repo = PersonaAnchorRepo(clock_ms=clock)
    arc_repo = LifeArcRepo(clock_ms=clock)

    def _seed(conn: sqlite3.Connection) -> None:
        chronicle_repo.upsert_slot(
            conn,
            day_key="2026-11-24",
            slot_key="afternoon",
            status="sealed",
            body="下午在B站刷到手工粘土，另外私聊答应老熟人甲帮看体检报告。",
            public_body="下午在B站刷到手工粘土教程，觉得挺有趣。",
            group_body="下午在B站刷到手工粘土教程。",
            source_event_ids=[],
            mood_tag="惬意",
            unfinished_thread="晚上想顺着聊聊手工翻车经历",
        )
        arc_repo.upsert_or_create(
            conn,
            title="海洋灯塔工程",
            summary="连续两天都在海岸边修灯塔",
            public_summary="连续两天都在海岸边修灯塔",
            mood_hint="干劲满满",
        )
        anchor_repo.create(
            conn,
            anchor_type="promise",
            content="今晚直播点亮灯塔",
            verbatim="今晚直播一定点亮",
            source_event_id=None,
            branch_id="maibot",
            visibility="public",
        )

    await db.run(_seed)

    maint = MaintenanceService(db, config=cfg, clock_ms=clock)
    # 1. 在线热备份并验证滚动保留策略（最多保留 keep_days=2 份）
    b1 = await maint.create_backup()
    clock.now_ms += 1_000
    b2 = await maint.create_backup()
    clock.now_ms += 1_000
    b3 = await maint.create_backup()
    assert b3.exists() and len(maint.list_backups()) == 2
    assert maint.has_backup_for_day("2026-11-24") is True
    # 校验生成的备份是一个可独立打开的完整 SQLite 库
    verify_conn = sqlite3.connect(b3)
    assert verify_conn.execute("PRAGMA integrity_check(1)").fetchone()[0] == "ok"
    assert len(verify_conn.execute("SELECT * FROM daily_chronicles").fetchall()) == 1
    verify_conn.close()
    del b1, b2

    # 2. 导出公开手账 Markdown：包含情绪与悬念，绝不泄露私聊句子
    md = await maint.export_diary_markdown(day_key="2026-11-24", days=1, target_visibility="public")
    assert "心情：惬意" in md
    assert "手工粘土教程" in md
    assert "海洋灯塔工程" in md
    assert "体检报告" not in md

    # 3. doctor_report 输出完整巡检字段
    doc = await maint.doctor_report(synthesizer_mode="rule", retrieval_mode="sparse")
    assert doc["ok"] is True
    assert doc["db"]["schema_version"] == SCHEMA_VERSION
    assert doc["db"]["counts"]["arcs"] == 1
    assert doc["backup_count"] == 2
    await db.close()


def test_cli_subcommands_doctor_backup_export(tmp_path: Path, capsys) -> None:
    """同步测试 maigo doctor / backup / export-diary 命令行入口。"""
    db_file = tmp_path / "cli_live.sqlite3"
    backup_dir = tmp_path / "cli_backups"
    cfg_toml = tmp_path / "cfg.toml"
    cfg_toml.write_text(
        f'db_path = "{db_file.as_posix()}"\n[backup]\ndir = "{backup_dir.as_posix()}"\nkeep_days = 2\n',
        encoding="utf-8",
    )

    # 先用 SQLite 直接准备一条主线供 export-diary 导出
    cli_main(["doctor", "--config", str(cfg_toml)])
    doc_out = json.loads(capsys.readouterr().out)
    assert doc_out["ok"] is True and doc_out["db"]["schema_version"] == SCHEMA_VERSION

    custom_bk = tmp_path / "manual.sqlite3"
    cli_main(["backup", "--config", str(cfg_toml), "--out", str(custom_bk)])
    bk_out = json.loads(capsys.readouterr().out)
    assert bk_out["ok"] is True and custom_bk.exists()

    cli_main(["--config", str(cfg_toml), "export-diary", "--day", "2026-11-24"])
    diary_out = capsys.readouterr().out
    assert "生活手账" in diary_out


def test_load_config_resolves_relative_paths_against_config_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    """在不同工作目录下加载配置时，相对数据/看板/备份路径应锚定至 config.toml 所在目录。"""
    app_dir = tmp_path / "app_root"
    other_cwd = tmp_path / "elsewhere"
    app_dir.mkdir(parents=True)
    other_cwd.mkdir(parents=True)
    (app_dir / "dashboard" / "dist").mkdir(parents=True)
    (app_dir / "dashboard" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")

    cfg_toml = app_dir / "config.toml"
    cfg_toml.write_text(
        'db_path = "data/maigo.sqlite3"\ndashboard_dist_dir = "dashboard/dist"\n[backup]\ndir = "data/backups"\n',
        encoding="utf-8",
    )

    monkeypatch.chdir(other_cwd)
    cfg = load_config(cfg_toml)
    assert Path(cfg.db_path) == (app_dir / "data" / "maigo.sqlite3").resolve()
    assert Path(cfg.dashboard_dist_dir) == (app_dir / "dashboard" / "dist").resolve()
    assert Path(cfg.backup.dir) == (app_dir / "data" / "backups").resolve()

    cli_main(["doctor", "--config", str(cfg_toml)])
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert doc["dashboard_ready"] is True
    assert (app_dir / "data" / "maigo.sqlite3").is_file()
    assert not (other_cwd / "data").exists()


async def test_idle_maintenance_creates_daily_backup_and_checkpoints_wal(tmp_path: Path) -> None:
    """无未整理事件时，空闲例行维护应能生成当日幂等热备份并温和收拢 WAL。"""
    clock = _MutableClock(_ms(2026, 11, 26, 11, 30))
    db_file = tmp_path / "idle.sqlite3"
    backup_dir = tmp_path / "backups"
    cfg = MaiGOConfig(
        db_path=str(db_file),
        backup=BackupConfig(dir=str(backup_dir), keep_days=3, auto_daily=True),
    )
    db = MaiGODatabase(db_file, clock_ms=clock)
    await db.initialize()

    identity = IdentityService(db, identity_salt="test-salt")
    maint = MaintenanceService(db, config=cfg, clock_ms=clock)
    synth = ChronicleSynthesizer(
        db,
        identity=identity,
        config=SynthesizerConfig(),
        maintenance=maint,
        auto_daily_backup=True,
        clock_ms=clock,
    )

    await synth.run_idle_maintenance()
    backups_first = maint.list_backups()
    assert len(backups_first) == 1
    assert maint.has_backup_for_day("2026-11-26") is True

    # 同一作息日内重复触发保持幂等，不重复生成备份文件
    clock.now_ms += 600_000
    await synth.run_idle_maintenance()
    assert len(maint.list_backups()) == 1
    await db.close()
