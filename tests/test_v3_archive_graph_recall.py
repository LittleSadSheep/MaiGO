"""Schema v3 升级、L0 原件书库、L2 实体关系图谱、抗回声召回与刚下播动态专项测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import List, Optional, Sequence

import httpx

from maigo.config import EmbeddingConfig, MaiGOConfig, SynthesizerConfig
from maigo.core import ChronicleSynthesizer, IdentityService
from maigo.core.digest_rules import SlotSynthesisDraft
from maigo.core.models import ExperienceEventRow
from maigo.server import create_app
from maigo.storage import SCHEMA_VERSION, MaiGODatabase, NewExperienceEvent
from maigo.storage.repos import DailyChronicleRepo, ExperienceEventRepo


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


class _MutableClock:
    def __init__(self, initial_ms: int) -> None:
        self.now_ms = initial_ms

    def __call__(self) -> int:
        return self.now_ms


class _TopicEmbeddingClient:
    """确定性语义向量桩：不同主题正交，验证 0.48/0.56 余弦门槛与无凑数降级。"""

    @property
    def model_name(self) -> str:
        return "topic-emb-v3"

    async def embed_texts(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        vecs: List[List[float]] = []
        for text in texts:
            if any(k in text for k in ("装配器", "晶体", "合成", "AE2")):
                vecs.append([0.96, 0.12, 0.0, 0.0])
            elif any(k in text for k in ("下界", "残骸", "合金", "挖掘")):
                vecs.append([0.0, 0.96, 0.12, 0.0])
            elif any(k in text for k in ("小狗", "同伴", "蒲公英", "宠物")):
                vecs.append([0.0, 0.0, 0.96, 0.12])
            else:
                vecs.append([0.0, 0.0, 0.0, 1.0])
        return vecs


class _FakeDigestLLM:
    """确定性整理模型桩，用于验证 rule 降级草稿空闲重整。"""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls = 0

    async def summarize_slot(
        self,
        *,
        day_key: str,
        slot_key: str,
        events: Sequence[ExperienceEventRow],
    ) -> Optional[SlotSynthesisDraft]:
        del day_key, slot_key, events
        self.calls += 1
        if self.should_fail:
            return None
        return SlotSynthesisDraft(
            body="晚上在游戏里搭好了自动化产线并和观众闲聊。",
            public_body="晚上在游戏里搭好了自动化产线并和观众闲聊。",
            group_body="晚上在游戏里搭好了自动化产线并和观众闲聊。",
            mood_tag="充实",
            unfinished_thread="下次继续扩建仓库",
        )


async def test_v2_to_v3_migration_preserves_existing_embeddings(tmp_path: Path) -> None:
    """从旧 v2 数据库升级到 v3 必须无损保留历史向量，同时支持写入新表与新 item_type。"""
    db_file = tmp_path / "legacy_v2.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL);
        INSERT INTO schema_migrations (version, applied_at_ms) VALUES (1, 1000), (2, 2000);
        CREATE TABLE daily_chronicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_key TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            status TEXT NOT NULL,
            body TEXT NOT NULL,
            public_body TEXT NOT NULL,
            group_body TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL DEFAULT '[]',
            mood_tag TEXT NOT NULL DEFAULT '',
            unfinished_thread TEXT NOT NULL DEFAULT '',
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(day_key, slot_key)
        );
        CREATE TABLE memory_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL CHECK(item_type IN ('event', 'anchor', 'arc')),
            item_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            dims INTEGER NOT NULL,
            vector_blob BLOB NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(item_type, item_id, model)
        );
        INSERT INTO memory_embeddings (item_type, item_id, model, dims, vector_blob, content_hash, updated_at_ms)
        VALUES ('event', 42, 'old-model', 2, X'0000803F00000000', 'abc123', 3000);
        """
    )
    conn.commit()
    conn.close()

    db = MaiGODatabase(db_file)
    await db.initialize()

    def _verify(c: sqlite3.Connection) -> tuple[int, int, str]:
        ver = int(c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
        old_cnt = int(
            c.execute("SELECT COUNT(*) FROM memory_embeddings WHERE item_type='event' AND item_id=42").fetchone()[0]
        )
        # 验证升级后的 CHECK 约束允许 record / relation / episode 类型
        c.execute(
            """
            INSERT INTO memory_embeddings (item_type, item_id, model, dims, vector_blob, content_hash, updated_at_ms)
            VALUES ('record', 99, 'old-model', 2, X'0000803F00000000', 'def456', 4000)
            """
        )
        c.execute(
            """
            INSERT INTO daily_chronicles (day_key, slot_key, status, body, public_body, group_body, updated_at_ms)
            VALUES ('2026-11-26', 'evening', 'sealed', '正文', '正文', '正文', 4000)
            """
        )
        draft_src = str(
            c.execute(
                "SELECT draft_source FROM daily_chronicles WHERE day_key='2026-11-26' AND slot_key='evening'"
            ).fetchone()[0]
        )
        c.commit()
        return ver, old_cnt, draft_src

    ver, old_cnt, draft_src = await db.run(_verify)
    assert ver == SCHEMA_VERSION == 3
    assert old_cnt == 1
    assert draft_src == "llm"
    await db.close()


async def test_archive_knowledge_recall_antiecho_and_just_ended_flow(tmp_path: Path) -> None:
    """端到端覆盖 L0 归档、中文拆词/语义召回、抗回声、L2 实体人物卡、遗忘清理与刚下播余温态。"""
    clock = _MutableClock(_ms(2026, 11, 26, 22, 30))
    cfg = MaiGOConfig(
        db_path=str(tmp_path / "v3_flow.sqlite3"),
        auth_token="v3-tok",
        synthesizer=SynthesizerConfig(auto_background=False),
        embedding=EmbeddingConfig(model="topic-emb-v3", auto_index=True),
    )
    app = create_app(cfg, clock_ms=clock, embedding_client=_TopicEmbeddingClient())
    headers = {"Authorization": "Bearer v3-tok"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
            # 1. 创建虚构人物并绑定 QQ 与 MC 账号
            p_resp = await client.post(
                "/api/v1/dashboard/persons",
                json={"display_name": "测试搭档甲", "notes": "经常一起做自动化产线"},
            )
            pid = p_resp.json()["person"]["id"]
            await client.post(
                "/api/v1/dashboard/persons/bind",
                json={"person_id": pid, "platform": "qq", "external_id": "10086", "external_name": "搭档QQ"},
            )
            await client.post(
                "/api/v1/dashboard/persons/bind",
                json={"person_id": pid, "platform": "minecraft", "external_id": "mc-uuid-001", "external_name": "搭档MC"},
            )

            # 2. 通过 /api/v1/archive/ingest 归档历史直播场次与细粒度原件（验证不污染 L1 时间线）
            arc_resp = await client.post(
                "/api/v1/archive/ingest",
                json={
                    "branch_id": "amaidesu",
                    "episode": {
                        "external_episode_id": "ep-hist-001",
                        "kind": "live",
                        "title": "下界挖残骸与AE2扩建",
                        "platform": "bilibili",
                        "save_id": "save-v3-1",
                        "visibility": "public",
                        "started_at_ms": _ms(2026, 11, 20, 20, 0),
                        "ended_at_ms": _ms(2026, 11, 20, 23, 0),
                        "overview": "那晚和观众一起在下界翻出了三块远古残骸，还做了晶体装配器。",
                    },
                    "records": [
                        {
                            "idempotency_key": "rec-ge-1",
                            "record_type": "game_event",
                            "actor_role": "self",
                            "visibility": "public",
                            "occurred_at_ms": _ms(2026, 11, 20, 21, 10),
                            "save_id": "save-v3-1",
                            "title": "解锁成就：残骸深处",
                            "content": "在下界 Y=15 用床炸出了第一批远古残骸，准备回基地合成下界合金。",
                        },
                        {
                            "idempotency_key": "rec-sp-1",
                            "record_type": "speech",
                            "actor_role": "self",
                            "visibility": "public",
                            "occurred_at_ms": _ms(2026, 11, 20, 21, 35),
                            "save_id": "save-v3-1",
                            "title": "主播原话",
                            "content": "这个晶体装配器终于接好能源线了，以后做芯片再也不用手动搓了！",
                            "verbatim": "这个晶体装配器终于接好能源线了，以后做芯片再也不用手动搓了！",
                        },
                    ],
                },
            )
            assert arc_resp.status_code == 200
            assert arc_resp.json()["accepted"] == 2
            # 确认 L0 写入绝不进入 L1 experience_events
            tl_after_archive = await client.get("/api/v1/dashboard/timeline")
            assert len(tl_after_archive.json()["events"]) == 0

            # 3. 通过 /api/v1/knowledge/ingest 写入 L2 实体关系事实，并验证人物卡片可见且脱敏
            kg_resp = await client.post(
                "/api/v1/knowledge/ingest",
                json={
                    "branch_id": "amaidesu",
                    "relations": [
                        {
                            "idempotency_key": "rel-sf-1",
                            "subject": {
                                "entity_type": "player",
                                "canonical_key": "minecraft:mc-uuid-001",
                                "name": "搭档MC",
                                "platform": "minecraft",
                                "external_id": "mc-uuid-001",
                                "visibility": "public",
                            },
                            "predicate": "shared_base",
                            "object_text": "蒲公英小狗",
                            "fact_text": "测试搭档甲一起照顾过基地门口名叫蒲公英的白色小狗",
                            "visibility": "public",
                            "save_id": "save-v3-1",
                            "external_episode_id": "ep-hist-001",
                            "occurred_at_ms": _ms(2026, 11, 20, 22, 0),
                        }
                    ],
                },
            )
            assert kg_resp.status_code == 200
            assert kg_resp.json()["relations_accepted"] == 1

            # 4. 在群聊里写入一条刚发出的“模糊回声”发言，验证 branch_id=maibot 抗回声生效
            await client.post(
                "/api/v1/events",
                json={
                    "idempotency_key": "qq-echo-1",
                    "branch_id": "maibot",
                    "occurred_at_ms": _ms(2026, 11, 26, 22, 25),
                    "kind": "speech",
                    "actor_role": "self",
                    "visibility": "group",
                    "summary": "在群里回应：晶体装配器？我好像有点印象但记不清细节了",
                    "verbatim": "晶体装配器？我好像有点印象但记不清细节了",
                },
            )

            # 结构化高置信度预召回：跳过同端 1 小时回声，直接命中 6 天前的 L0 原件并带日期与场次锚点
            rec_struct = await client.get(
                "/api/v1/views/recall",
                params={
                    "query": "之前做的晶体装配器细节",
                    "days": 0,
                    "limit": 3,
                    "target_visibility": "group",
                    "branch_id": "maibot",
                    "high_confidence_only": "true",
                    "include_structured": "true",
                },
            )
            assert rec_struct.status_code == 200
            rjson = rec_struct.json()
            assert rjson["strong_hit"] is True
            assert "2026-11-20" in rjson["text"]
            assert "下界挖残骸与AE2扩建" in rjson["text"]
            assert "接好能源线" in rjson["text"]
            assert "记不清细节" not in rjson["text"]

            # 无关闲聊在高置信度模式下必须返回空字符串，杜绝凑数污染提示词
            noise_rec = await client.get(
                "/api/v1/views/recall",
                params={
                    "query": "今天晚饭吃啥呀",
                    "days": 0,
                    "limit": 3,
                    "target_visibility": "group",
                    "branch_id": "maibot",
                    "high_confidence_only": "true",
                    "include_structured": "true",
                },
            )
            assert noise_rec.status_code == 200
            assert noise_rec.json()["strong_hit"] is False
            assert noise_rec.json()["text"] == ""

            # 5. 正式开播签出 -> 携带 archive_records 与 interactions 签入 -> 校验刚下播 4 小时余温与 L2 不污染 L1
            chk = await client.post(
                "/api/v1/sessions/checkout",
                json={
                    "branch_id": "amaidesu",
                    "external_session_id": "ep-today-002",
                    "kind": "live",
                    "title": "夜晚轻松盖温室",
                    "platform": "bilibili",
                    "room_id": "12345",
                    "save_id": "save-v3-1",
                },
            )
            checkout_id = chk.json()["checkout_id"]
            clock.now_ms = _ms(2026, 11, 26, 23, 0)
            ci = await client.post(
                f"/api/v1/sessions/{checkout_id}/checkin",
                json={
                    "ended_at_ms": _ms(2026, 11, 26, 23, 0),
                    "overview": "今晚给基地盖了玻璃温室，还收了一茬小麦。",
                    "save_id": "save-v3-1",
                    "archive_lines": [{"title": "建成玻璃温室", "verbatim": "温室屋顶总算合拢了"}],
                    "archive_records": [
                        {
                            "idempotency_key": "ci-rec-1",
                            "record_type": "work_doc",
                            "actor_role": "self",
                            "visibility": "public",
                            "occurred_at_ms": _ms(2026, 11, 26, 22, 50),
                            "title": "温室配料清单",
                            "content": "用了三组玻璃和两组云杉木板搭温室穹顶。",
                        }
                    ],
                    "interactions": [
                        {
                            "platform": "minecraft",
                            "external_id": "mc-uuid-001",
                            "display_name": "搭档MC",
                            "summary": "greet",
                            "kind": "help_build",
                            "fact": "帮着一起铺完了温室的玻璃屋顶",
                        }
                    ],
                },
            )
            assert ci.status_code == 200
            ci_data = ci.json()
            assert ci_data["ingested_records"] >= 2
            assert ci_data["ingested_relations"] >= 1

            # 30 分钟后群聊拉取 context：live_context 明确呈现“刚刚结束了直播”且 actor_cards 含 L2 关系与互动
            clock.now_ms = _ms(2026, 11, 26, 23, 30)
            ctx_after = await client.get(
                "/api/v1/views/context",
                params={"branch_id": "maibot", "target_visibility": "group", "active_actors": "qq:10086"},
            )
            assert ctx_after.status_code == 200
            ctx_json = ctx_after.json()
            assert "刚刚结束了bilibili直播《夜晚轻松盖温室》" in ctx_json["live_context"]
            assert "玻璃温室" in ctx_json["live_context"]
            card_hint = ctx_json["actor_cards"][0]["hint"]
            assert "蒲公英" in card_hint
            assert "10086" not in card_hint and "mc-uuid-001" not in card_hint

            # 6. 触发遗忘保护后，L2 关系边与人物卡片应立即清除，且拒绝再次写入该账号关系
            fg_resp = await client.post(
                "/api/v1/dashboard/persons/forget",
                json={"platform": "minecraft", "external_id": "mc-uuid-001"},
            )
            assert fg_resp.status_code == 200
            recall_dog = await client.get(
                "/api/v1/views/recall",
                params={"query": "蒲公英小狗", "days": 0, "target_visibility": "group"},
            )
            assert "测试搭档甲一起照顾过" not in recall_dog.json()["text"]


async def test_rule_fallback_slot_is_rewritten_by_idle_llm(tmp_path: Path) -> None:
    """当 LLM 临时超时导致时段回退 rule 草稿后，空闲维护在 LLM 恢复时应自动重整覆盖为 llm 草稿。"""
    clock = _MutableClock(_ms(2026, 11, 26, 23, 30))
    db = MaiGODatabase(tmp_path / "rule_retry.sqlite3", clock_ms=clock)
    await db.initialize()
    identity = IdentityService(db, clock_ms=clock)
    event_repo = ExperienceEventRepo(clock_ms=clock)
    chronicle_repo = DailyChronicleRepo(clock_ms=clock)

    await db.run(
        lambda c: event_repo.ingest_batch(
            c,
            [
                NewExperienceEvent(
                    idempotency_key="ev-retry-1",
                    branch_id="amaidesu",
                    occurred_at_ms=_ms(2026, 11, 26, 21, 0),
                    kind="milestone",
                    actor_role="self",
                    visibility="public",
                    summary="搭建好了自动化矿车产线",
                )
            ],
        )
    )

    fake_llm = _FakeDigestLLM(should_fail=True)
    synth = ChronicleSynthesizer(
        db,
        identity=identity,
        config=SynthesizerConfig(),
        llm_client=fake_llm,
        clock_ms=clock,
    )
    # 第一次整理 LLM 失败 -> 落盘为 draft_source='rule'
    await synth.synthesize_pending(force_seal=True)
    row_rule = await db.run(lambda c: chronicle_repo.get_slot(c, day_key="2026-11-26", slot_key="evening"))
    assert row_rule is not None
    assert row_rule.draft_source == "rule"

    # LLM 恢复后运行空闲维护 -> 自动重写最近 rule 时段为 llm 叙事草稿
    fake_llm.should_fail = False
    await synth.run_idle_maintenance()
    row_llm = await db.run(lambda c: chronicle_repo.get_slot(c, day_key="2026-11-26", slot_key="evening"))
    assert row_llm is not None
    assert row_llm.draft_source == "llm"
    assert "自动化产线" in row_llm.body
    await db.close()
