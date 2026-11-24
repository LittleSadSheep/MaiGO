"""MaiGO v0.2 HTTP API、看板运维接口与混合语义召回集成测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Dict, List, Optional, Sequence

import httpx

from maigo.api import create_app
from maigo.config import BackupConfig, EmbeddingConfig, MaiGOConfig, SynthesizerConfig
from maigo.storage.schema import SCHEMA_VERSION


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


class MutableClock:
    """可确定性推进的毫秒时钟。"""

    def __init__(self, initial_ms: int) -> None:
        self.now_ms = initial_ms

    def __call__(self) -> int:
        return self.now_ms


class FakeSemanticEmbeddingClient:
    """按预置语义簇返回确定性单位向量的测试替身（不依赖外部网络）。"""

    def __init__(self, *, model: str = "fake-v2-embed") -> None:
        self._model = model
        self.calls: int = 0

    @property
    def model_name(self) -> str:
        return self._model

    async def embed_texts(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        self.calls += 1
        vectors: List[List[float]] = []
        for text in texts:
            if any(kw in text for kw in ("熬夜看球赛", "绿茵场转播", "足球赛事")):
                vectors.append([1.0, 0.0, 0.0, 0.0])
            elif any(kw in text for kw in ("烘培饼干", "烤箱")):
                vectors.append([0.0, 1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0, 0.0])
        return vectors


async def test_v2_health_status_backup_diary_quick_note_arcs_and_anchors(tmp_path: Path) -> None:
    clock = MutableClock(_ms(2026, 11, 24, 15, 0))
    backup_dir = tmp_path / "backups"
    cfg = MaiGOConfig(
        db_path=str(tmp_path / "v2_api.sqlite3"),
        auth_token="v2-test-token",
        synthesizer=SynthesizerConfig(auto_background=False),
        backup=BackupConfig(dir=str(backup_dir), keep_days=3, auto_daily=False),
    )
    app = create_app(cfg, clock_ms=clock)
    headers: Dict[str, str] = {"Authorization": "Bearer v2-test-token"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. /health 暴露版本、合成与召回模式
            health = await client.get("/health")
            assert health.status_code == 200
            hdata = health.json()
            assert hdata["ok"] is True
            assert hdata["service"] == "maigo"
            assert hdata["schema_version"] == SCHEMA_VERSION
            assert hdata["synthesizer_mode"] == "rule"
            assert hdata["retrieval_mode"] == "sparse"

            # 2. /dashboard/status 与热备份接口
            status_resp = await client.get("/api/v1/dashboard/status", headers=headers)
            assert status_resp.status_code == 200
            sdata = status_resp.json()
            assert sdata["ok"] is True
            assert sdata["db"]["schema_version"] == SCHEMA_VERSION

            backup_resp = await client.post("/api/v1/dashboard/backup", headers=headers)
            assert backup_resp.status_code == 200
            bdata = backup_resp.json()
            assert bdata["ok"] is True
            filename = bdata["filename"]
            assert filename.endswith(".sqlite3")
            assert bdata["size_bytes"] > 0
            # 不暴露宿主机目录前缀，只返回文件名与字节信息
            assert "/" not in filename and "\\" not in filename
            backup_file = backup_dir / filename
            assert backup_file.is_file()
            with sqlite3.connect(backup_file) as bconn:
                ver = bconn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                assert int(ver) == SCHEMA_VERSION

            # 3. 随手投喂（公开见闻 + 灵感笔记 + 游戏高光 + 私密记录）与日记导出隐私隔离
            q1 = await client.post(
                "/api/v1/dashboard/quick-note",
                headers=headers,
                json={
                    "kind": "browsed",
                    "visibility": "public",
                    "content": "刷到一个手工木作修复短视频",
                    "reaction": "周末想试着用砂纸打磨旧桌面",
                    "synthesize_now": True,
                },
            )
            assert q1.status_code == 200
            assert q1.json()["event_id"] is not None
            assert q1.json()["synthesis"]["processed_events"] >= 1

            q2 = await client.post(
                "/api/v1/dashboard/quick-note",
                headers=headers,
                json={
                    "kind": "note",
                    "visibility": "public",
                    "content": "顺手记下明天早起买手冲滤纸",
                    "synthesize_now": True,
                },
            )
            assert q2.status_code == 200

            q3 = await client.post(
                "/api/v1/dashboard/quick-note",
                headers=headers,
                json={
                    "kind": "game_moment",
                    "visibility": "public",
                    "save_id": "skyline_s1",
                    "content": "修通跨海铁路枢纽主桥",
                    "synthesize_now": True,
                },
            )
            assert q3.status_code == 200

            q_priv = await client.post(
                "/api/v1/dashboard/quick-note",
                headers=headers,
                json={
                    "kind": "speech",
                    "visibility": "private",
                    "content": "绝密私聊口令仅自己可见",
                    "synthesize_now": True,
                },
            )
            assert q_priv.status_code == 200

            # 4. 近期主线 CRUD 与 timeline 带出 arcs / slots
            arc_create = await client.post(
                "/api/v1/dashboard/arcs",
                headers=headers,
                json={
                    "title": "木作工坊翻新计划",
                    "summary": "正在打磨书桌和整理工具架",
                    "mood_hint": "干劲十足",
                },
            )
            assert arc_create.status_code == 200
            arc_id = arc_create.json()["item"]["id"]

            arcs_list = await client.get("/api/v1/dashboard/arcs", headers=headers)
            assert arcs_list.status_code == 200
            assert any(item["id"] == arc_id for item in arcs_list.json()["items"])

            timeline = await client.get("/api/v1/dashboard/timeline", headers=headers)
            assert timeline.status_code == 200
            tdata = timeline.json()
            assert any(item["id"] == arc_id for item in tdata["arcs"])
            assert len(tdata["slots"]) >= 1
            assert "mood_tag" in tdata["slots"][0]
            assert "unfinished_thread" in tdata["slots"][0]

            arc_patch = await client.patch(
                f"/api/v1/dashboard/arcs/{arc_id}",
                headers=headers,
                json={"status": "completed", "mood_hint": "顺利收尾"},
            )
            assert arc_patch.status_code == 200
            assert arc_patch.json()["item"]["status"] == "completed"

            # 已完结主线不再出现在 timeline 活跃列表里
            timeline2 = await client.get("/api/v1/dashboard/timeline", headers=headers)
            assert not any(item["id"] == arc_id for item in timeline2.json()["arcs"])

            assert (await client.delete(f"/api/v1/dashboard/arcs/{arc_id}", headers=headers)).status_code == 200
            assert (await client.delete(f"/api/v1/dashboard/arcs/{arc_id}", headers=headers)).status_code == 404

            # 5. Markdown 手账导出严格按 visibility 裁剪
            pub_diary = await client.get(
                "/api/v1/dashboard/diary/export?days=2&target_visibility=public",
                headers=headers,
            )
            assert pub_diary.status_code == 200
            pub_md = pub_diary.json()["markdown"]
            assert "手工木作修复短视频" in pub_md
            assert "绝密私聊口令仅自己可见" not in pub_md

            priv_diary = await client.get(
                "/api/v1/dashboard/diary/export?days=2&target_visibility=private",
                headers=headers,
            )
            assert priv_diary.status_code == 200
            assert "绝密私聊口令仅自己可见" in priv_diary.json()["markdown"]

            # 6. 锚点状态支持 superseded / stale 并支持重新激活
            a_resp = await client.post(
                "/api/v1/dashboard/anchors",
                headers=headers,
                json={
                    "anchor_type": "promise",
                    "content": "下周补完手工记录视频",
                    "verbatim": "下周一定补完手工记录视频！",
                },
            )
            assert a_resp.status_code == 200
            aid = a_resp.json()["item"]["id"]

            for st in ("stale", "superseded", "open"):
                p = await client.patch(
                    f"/api/v1/dashboard/anchors/{aid}",
                    headers=headers,
                    json={"status": st},
                )
                assert p.status_code == 200
                assert p.json()["item"]["status"] == st


async def test_v2_hybrid_recall_via_api_with_injected_embedding_client(tmp_path: Path) -> None:
    clock = MutableClock(_ms(2026, 11, 24, 21, 0))
    cfg = MaiGOConfig(
        db_path=str(tmp_path / "hybrid_api.sqlite3"),
        auth_token="hybrid-token",
        synthesizer=SynthesizerConfig(auto_background=False),
        embedding=EmbeddingConfig(model="fake-v2-embed", auto_index=True),
    )
    fake_emb = FakeSemanticEmbeddingClient(model="fake-v2-embed")
    app = create_app(cfg, clock_ms=clock, embedding_client=fake_emb)
    headers = {"Authorization": "Bearer hybrid-token"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = (await client.get("/health")).json()
            assert health["retrieval_mode"] == "hybrid"

            # 投喂两件字面零重合的事：询问「熬夜看球赛」应靠向量命中「绿茵场转播」
            ev_resp = await client.post(
                "/api/v1/events",
                headers=headers,
                json={
                    "events": [
                        {
                            "idempotency_key": "sem-1",
                            "branch_id": "bilibili_ops",
                            "occurred_at_ms": _ms(2026, 11, 24, 10, 0),
                            "kind": "browsed",
                            "actor_role": "self",
                            "visibility": "public",
                            "summary": "昨晚守着绿茵场转播到凌晨两点",
                        },
                        {
                            "idempotency_key": "sem-2",
                            "branch_id": "maibot",
                            "occurred_at_ms": _ms(2026, 11, 24, 11, 0),
                            "kind": "browsed",
                            "actor_role": "self",
                            "visibility": "public",
                            "summary": "今天用烤箱做了黄油曲奇",
                        },
                    ]
                },
            )
            assert ev_resp.status_code == 200

            syn = await client.post("/api/v1/dashboard/synthesize", headers=headers)
            assert syn.status_code == 200

            status = (await client.get("/api/v1/dashboard/status", headers=headers)).json()
            assert status["retrieval_mode"] == "hybrid"
            assert status["db"]["counts"]["embeddings"] >= 2

            recall = await client.get(
                "/api/v1/views/recall?query=熬夜看球赛&days=7&limit=3&target_visibility=public",
                headers=headers,
            )
            assert recall.status_code == 200
            text = recall.json()["text"]
            assert "绿茵场转播" in text
