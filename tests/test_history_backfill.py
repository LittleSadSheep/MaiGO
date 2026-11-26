"""历史记忆幂等回填工具（history_backfill）单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sqlite3
import sys
from types import ModuleType

import httpx

from maigo.config import MaiGOConfig, SynthesizerConfig
from maigo.server import create_app

_BACKFILL_PATH = Path(__file__).resolve().parents[1] / "examples" / "history_backfill" / "backfill.py"


def _load_backfill_module() -> ModuleType:
    mod_name = "maigo_history_backfill_test_mod"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = spec_from_file_location(mod_name, _BACKFILL_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_backfill_module()


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


def _build_fake_amaidesu_db(path: Path, *, now_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE live_sessions (id INTEGER PRIMARY KEY, started_at_ms INTEGER, kind TEXT, title TEXT);
        CREATE TABLE archive_lines (id INTEGER PRIMARY KEY, live_session_id INTEGER, text TEXT, quote TEXT, save_id TEXT, highlight INTEGER, created_at_ms INTEGER);
        CREATE TABLE commitments (id INTEGER PRIMARY KEY, what TEXT, quote TEXT, close_quote TEXT, viewer_id INTEGER, status TEXT, created_session_id INTEGER, save_id TEXT, created_at_ms INTEGER, updated_at_ms INTEGER);
        CREATE TABLE game_players (id INTEGER PRIMARY KEY, game TEXT, uuid TEXT, name TEXT, forget_requested_at_ms INTEGER);
        CREATE TABLE game_player_interactions (id INTEGER PRIMARY KEY, player_id INTEGER, live_session_id INTEGER, save_id TEXT, kind TEXT, fact TEXT, at_ms INTEGER);
        CREATE TABLE timeline_summary (id INTEGER PRIMARY KEY, live_session_id INTEGER, start_ms INTEGER, end_ms INTEGER, summary TEXT);
        CREATE TABLE game_events (id INTEGER PRIMARY KEY, live_session_id INTEGER, message TEXT, timestamp_ms INTEGER, save_id TEXT);
        """
    )
    recent_t = now_ms - 2 * 86_400_000
    old_t = now_ms - 45 * 86_400_000
    conn.execute("INSERT INTO live_sessions VALUES (1, ?, 'live', '林地建家直播')", (recent_t,))
    conn.execute("INSERT INTO live_sessions VALUES (2, ?, 'rehearsal', '彩排测试')", (recent_t + 1000,))
    conn.execute("INSERT INTO live_sessions VALUES (3, ?, 'live', '无档案老场')", (recent_t + 3_600_000,))
    conn.execute("INSERT INTO live_sessions VALUES (4, ?, 'live', '超期旧场')", (old_t,))

    conn.execute(
        "INSERT INTO archive_lines VALUES (10, 1, '在海岸边建起第一座高炉仓库', '高炉终于点亮啦！', 'save_mc_1', 1, ?)",
        (recent_t + 1000,),
    )
    conn.execute(
        "INSERT INTO archive_lines VALUES (11, 2, '彩排调试句不应被导入', '测试', 'save_mc_1', 0, ?)",
        (recent_t + 2000,),
    )
    conn.execute(
        "INSERT INTO commitments VALUES (20, '周末继续扩建海上铁路', '周末一定要把铁轨铺通', '', NULL, 'open', 1, 'save_mc_1', ?, ?)",
        (recent_t + 3000, recent_t + 3000),
    )
    conn.execute(
        "INSERT INTO commitments VALUES (21, '私下答应某观众的小礼物', '答应你', '', 999, 'open', 1, NULL, ?, ?)",
        (recent_t + 3500, recent_t + 3500),
    )
    conn.execute("INSERT INTO game_players VALUES (1, 'minecraft', 'uuid-keep-1', '好友甲', NULL)")
    conn.execute("INSERT INTO game_players VALUES (2, 'minecraft', 'uuid-forget-2', '遗忘玩家乙', ?)", (recent_t,))
    conn.execute(
        "INSERT INTO game_player_interactions VALUES (30, 1, 1, 'save_mc_1', 'coop', '和好友甲一起搬运原木建造屋顶', ?)",
        (recent_t + 4000,),
    )
    conn.execute(
        "INSERT INTO game_player_interactions VALUES (31, 2, 1, 'save_mc_1', 'coop', '这条遗忘玩家的互动必须剔除', ?)",
        (recent_t + 4500,),
    )
    conn.execute(
        "INSERT INTO timeline_summary VALUES (40, 3, ?, ?, '老场次下矿挖到第一颗钻石')",
        (recent_t + 3_600_000, recent_t + 3_900_000),
    )
    conn.execute(
        "INSERT INTO game_events VALUES (50, 3, '成功合成铁镐并探索矿洞', ?, 'save_mc_1')",
        (recent_t + 3_700_000,),
    )
    conn.commit()
    conn.close()


def _build_fake_maibot_db(path: Path, *, now_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE bot_platform_accounts (id INTEGER PRIMARY KEY, platform TEXT, account_id TEXT);
        CREATE TABLE mai_messages (
            id INTEGER PRIMARY KEY, message_id TEXT, timestamp REAL, platform TEXT, scope TEXT,
            user_id TEXT, group_id TEXT, group_name TEXT, session_id TEXT,
            is_emoji INTEGER, is_picture INTEGER, is_command INTEGER, is_notify INTEGER,
            processed_plain_text TEXT, raw_content TEXT
        );
        """
    )
    ts_sec = (now_ms - 86_400_000) / 1000.0
    conn.execute("INSERT INTO bot_platform_accounts VALUES (1, 'qq', 'bot-10000')")
    rows = [
        ("m1", ts_sec, "qq", "group", "bot-10000", "700001", "玩家测试群", "sess-g1", 0, 0, 0, 0, "我超喜欢做像素沙盒建筑！", ""),
        ("m2", ts_sec + 10, "qq", "group", "peer-20001", "700001", "玩家测试群", "sess-g1", 0, 0, 0, 0, "群友发言不应入账为麦麦本人观点", ""),
        ("m3", ts_sec + 20, "qq", "group", "bot-10000", "700002", "非白名单群", "sess-g2", 0, 0, 0, 0, "非白名单群消息必须过滤", ""),
        ("m4", ts_sec + 30, "qq", "private", "bot-10000", "", "", "sess-p1", 0, 0, 0, 0, "私聊偷偷记录一条测试想法", ""),
        ("m5", ts_sec + 40, "qq", "group", "bot-10000", "700001", "玩家测试群", "sess-g1", 0, 0, 0, 0, "嗯嗯", ""),
        ("m6", ts_sec + 50, "qq", "group", "bot-10000", "700001", "玩家测试群", "sess-g1", 1, 0, 0, 0, "[表情]大笑", ""),
        ("m7", ts_sec + 60, "bot_console", "private", "bot-10000", "", "", "sess-c", 0, 0, 0, 0, "控制台调试句子不应该导进去", ""),
    ]
    conn.executemany(
        "INSERT INTO mai_messages (message_id, timestamp, platform, scope, user_id, group_id, group_name, session_id, is_emoji, is_picture, is_command, is_notify, processed_plain_text, raw_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_extract_filters_high_signal_records_cleanly(tmp_path: Path) -> None:
    now_ms = _ms(2026, 11, 26, 12, 0)
    ama_db = tmp_path / "amaidesu.db"
    bot_db = tmp_path / "MaiBot.db"
    _build_fake_amaidesu_db(ama_db, now_ms=now_ms)
    _build_fake_maibot_db(bot_db, now_ms=now_ms)

    ama_events = _mod.extract_amaidesu_events(ama_db, since_days=30, now_ms=now_ms)
    summaries = [ev["summary"] for ev in ama_events]
    assert "在海岸边建起第一座高炉仓库" in summaries
    assert "周末继续扩建海上铁路" in summaries
    assert "和好友甲一起搬运原木建造屋顶" in summaries
    assert "老场次下矿挖到第一颗钻石" in summaries
    assert "成功合成铁镐并探索矿洞" in summaries
    assert not any("彩排调试" in s for s in summaries)
    assert not any("某观众" in s for s in summaries)
    assert not any("遗忘玩家" in s for s in summaries)

    bot_events = _mod.extract_maibot_events(bot_db, group_ids=["700001"], include_private=True, since_days=30, now_ms=now_ms)
    assert [ev["visibility"] for ev in bot_events] == ["group", "private"]
    assert "像素沙盒建筑" in bot_events[0]["verbatim"]
    assert "偷偷记录" in bot_events[1]["verbatim"]
    assert not any("群友发言" in ev["verbatim"] or "非白名单群" in ev["verbatim"] or "控制台" in ev["verbatim"] for ev in bot_events)


async def test_push_backfill_dry_run_idempotent_and_enters_maigo_views(tmp_path: Path) -> None:
    now_ms = _ms(2026, 11, 26, 12, 0)
    ama_db = tmp_path / "amaidesu.db"
    bot_db = tmp_path / "MaiBot.db"
    _build_fake_amaidesu_db(ama_db, now_ms=now_ms)
    _build_fake_maibot_db(bot_db, now_ms=now_ms)

    all_events = _mod.extract_amaidesu_events(ama_db, since_days=30, now_ms=now_ms) + _mod.extract_maibot_events(
        bot_db, group_ids=["700001"], include_private=True, since_days=30, now_ms=now_ms
    )

    app = create_app(
        MaiGOConfig(
            db_path=str(tmp_path / "maigo.sqlite3"),
            auth_token="backfill-sec",
            synthesizer=SynthesizerConfig(auto_background=False),
        ),
        clock_ms=lambda: now_ms,
    )

    async def _no_sleep(_s: float) -> None:
        return None

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            dry = await _mod.push_backfill_events(all_events, maigo_url="http://testserver", auth_token="backfill-sec", dry_run=True, client=client)
            assert dry.dry_run is True and dry.extracted == len(all_events) and dry.submitted == 0

            first = await _mod.push_backfill_events(
                all_events, maigo_url="http://testserver", auth_token="backfill-sec",
                batch_size=2, batch_interval_ms=10, trigger_synthesize=True, client=client, sleep_fn=_no_sleep,
            )
            assert first.accepted == len(all_events) and first.deduplicated == 0 and first.synthesized is True

            # 重复回填全部幂等去重
            second = await _mod.push_backfill_events(
                all_events, maigo_url="http://testserver", auth_token="backfill-sec",
                batch_size=4, batch_interval_ms=0, client=client, sleep_fn=_no_sleep,
            )
            assert second.accepted == 0 and second.deduplicated == len(all_events)

            headers = {"Authorization": "Bearer backfill-sec"}
            await client.post("/api/v1/dashboard/synthesize?force_seal=true", headers=headers)
            view_pub = (await client.get("/api/v1/views/context?branch_id=amaidesu&target_visibility=public", headers=headers)).json()
            assert "高炉仓库" in (view_pub.get("stable_brief") or "")
            assert "偷偷记录" not in (view_pub.get("stable_brief") or "")
            assert any("海上铁路" in p["content"] for p in view_pub.get("open_promises") or [])


def test_cli_entry_dry_run(tmp_path: Path) -> None:
    now_ms = _ms(2026, 11, 26, 12, 0)
    ama_db = tmp_path / "amaidesu.db"
    bot_db = tmp_path / "MaiBot.db"
    _build_fake_amaidesu_db(ama_db, now_ms=now_ms)
    _build_fake_maibot_db(bot_db, now_ms=now_ms)
    assert _mod.main(["amaidesu", "--db", str(ama_db), "--dry-run"]) == 0
    assert _mod.main(["maibot", "--db", str(bot_db), "--group", "700001", "--dry-run"]) == 0
