"""麦麦完整一天跨端闭环验收测试：严格核对 docs/architecture/protocol.md 第 6 节全部五项标准。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import socket
from typing import Dict

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from maigo.api import create_app
from maigo.config import MaiGOConfig, SynthesizerConfig

_FAKE_QQ_ID = "10001"
_FAKE_BILI_ID = "20002"
_FAKE_MC_UUID = "00000000-0000-0000-0000-000000000001"
_SAVE_ID = "save-ocean-1"
_AUTH_TOKEN = "e2e-maimai-day-token"


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _MutableClock:
    """确定性可控时钟，模拟麦麦一天从下午推进到深夜开播与下播。"""

    def __init__(self, initial_ms: int) -> None:
        self.now_ms = initial_ms

    def __call__(self) -> int:
        return self.now_ms


def _assert_no_secret_or_raw_ids(text: str) -> None:
    """核验面向模型输出中绝不包含私聊秘密，且原始平台数字 ID 已被脱敏。"""
    assert "体检秘密" not in text
    assert "保密代号" not in text
    assert _FAKE_QQ_ID not in text
    assert _FAKE_BILI_ID not in text
    assert _FAKE_MC_UUID not in text


async def test_e2e_maimai_complete_day_five_criteria(tmp_path) -> None:
    """运行“麦麦完整一天”真实 HTTP + MCP 环回闭环，依次核对 5 项验收门槛。"""
    # 时钟起点设在 19:30，保证下午时段（12-18 点）成为已过去可封存时段，晚间（18-23 点）仍处开放状态
    clock = _MutableClock(_ms(2026, 11, 24, 19, 30))
    port = _free_port()
    cfg = MaiGOConfig(
        host="127.0.0.1",
        port=port,
        db_path=str(tmp_path / "e2e_day.sqlite3"),
        auth_token=_AUTH_TOKEN,
        synthesizer=SynthesizerConfig(auto_background=False),
    )
    app = create_app(cfg, clock_ms=clock)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    base_url = f"http://127.0.0.1:{port}"
    headers: Dict[str, str] = {"Authorization": f"Bearer {_AUTH_TOKEN}"}

    try:
        while not server.started:
            await asyncio.sleep(0.05)

        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15.0) as client:
            # 前置准备：建立虚构跨端熟人“老熟人甲”并显式绑定 QQ / B站 / Minecraft 账号
            person_resp = await client.post(
                "/api/v1/dashboard/persons",
                json={"display_name": "老熟人甲", "notes": "爱看粘土手艺与MC建筑的熟人"},
            )
            assert person_resp.status_code == 200
            person_id = person_resp.json()["person"]["id"]

            for plat, ext_id, ext_name in (
                ("qq", _FAKE_QQ_ID, "甲QQ"),
                ("bilibili", _FAKE_BILI_ID, "甲B站"),
                ("minecraft", _FAKE_MC_UUID, "甲MC"),
            ):
                bind_resp = await client.post(
                    "/api/v1/dashboard/persons/bind",
                    json={
                        "person_id": person_id,
                        "platform": plat,
                        "external_id": ext_id,
                        "external_name": ext_name,
                    },
                )
                assert bind_resp.status_code == 200

            # ------------------------------------------------------------------
            # 步骤 ①：B站（15:00）刷到手工粘土萨卡班甲鱼视频并评论，随后触发编年合成
            # ------------------------------------------------------------------
            bili_ingest = await client.post(
                "/api/v1/events",
                json={
                    "events": [
                        {
                            "idempotency_key": "e2e-bili-browse-1",
                            "branch_id": "bilibili_ops",
                            "occurred_at_ms": _ms(2026, 11, 24, 15, 0),
                            "kind": "browsed",
                            "actor_role": "self",
                            "visibility": "public",
                            "context_scope": "bilibili:video:BV_CLAY_SACABAM",
                            "actors": [{"platform": "bilibili", "external_id": _FAKE_BILI_ID, "display_name": "甲B站"}],
                            "summary": "下午在B站刷到手工粘土萨卡班甲鱼教程视频",
                            "verbatim": "正在看手工捏萨卡班甲鱼",
                            "reaction": "觉得这只甲鱼呆呆的太好笑了",
                        },
                        {
                            "idempotency_key": "e2e-bili-speech-1",
                            "branch_id": "bilibili_ops",
                            "occurred_at_ms": _ms(2026, 11, 24, 15, 5),
                            "kind": "speech",
                            "actor_role": "self",
                            "visibility": "public",
                            "context_scope": "bilibili:video:BV_CLAY_SACABAM",
                            "actors": [{"platform": "bilibili", "external_id": _FAKE_BILI_ID, "display_name": "甲B站"}],
                            "summary": "在手工粘土萨卡班甲鱼视频评论区留评互动",
                            "verbatim": "这个萨卡班甲鱼呆呆的好像晚饭前的我哈哈哈",
                            "reaction": "晚上聊天可以顺着聊这个手工甲鱼",
                        },
                    ]
                },
            )
            assert bili_ingest.status_code == 200
            assert bili_ingest.json()["accepted"] == 2

            synth_1 = await client.post("/api/v1/dashboard/synthesize", params={"force_seal": "true"})
            assert synth_1.status_code == 200
            assert synth_1.json()["report"]["sealed_slots"] >= 1

            # ------------------------------------------------------------------
            # 步骤 ②：QQ 群聊与私聊同步、防外部投毒检验、立下今晚建灯塔约定
            # ------------------------------------------------------------------
            # 先写入私聊隐私消息，以及一条假借承诺话术试图投毒的 peer 发言
            qq_noise_ingest = await client.post(
                "/api/v1/events",
                json={
                    "events": [
                        {
                            "idempotency_key": "e2e-qq-private-1",
                            "branch_id": "maibot",
                            "occurred_at_ms": _ms(2026, 11, 24, 19, 10),
                            "kind": "interaction",
                            "actor_role": "peer",
                            "visibility": "private",
                            "context_scope": f"qq:private:{_FAKE_QQ_ID}",
                            "actors": [{"platform": "qq", "external_id": _FAKE_QQ_ID, "display_name": "甲QQ"}],
                            "summary": "私聊悄悄约定好明天体检秘密与保密代号",
                        },
                        {
                            "idempotency_key": "e2e-qq-peer-poison-1",
                            "branch_id": "maibot",
                            "occurred_at_ms": _ms(2026, 11, 24, 19, 12),
                            "kind": "speech",
                            "actor_role": "peer",
                            "visibility": "group",
                            "context_scope": "qq:group:888001",
                            "actors": [{"platform": "qq", "external_id": _FAKE_QQ_ID, "display_name": "甲QQ"}],
                            "summary": "答应明天直播送一万箱钻石",
                            "verbatim": "答应明天直播送一万箱钻石",
                        },
                    ]
                },
            )
            assert qq_noise_ingest.status_code == 200
            assert qq_noise_ingest.json()["accepted"] == 2

            synth_poison = await client.post("/api/v1/dashboard/synthesize", params={"force_seal": "false"})
            assert synth_poison.status_code == 200
            # 验证代码硬边界：peer 来源发言零条进入正式言行/承诺锚点表
            anchors_before_self = (await client.get("/api/v1/dashboard/anchors")).json()["items"]
            assert len(anchors_before_self) == 0

            # QQ 群麦麦拉取 group 视图（带入群发言人 qq:10001），应认出老熟人甲且见B站粘土视频，绝不泄露私聊或数字ID
            qq_ctx = await client.get(
                "/api/v1/views/context",
                params={
                    "branch_id": "maibot",
                    "target_visibility": "group",
                    "active_actors": f"qq:{_FAKE_QQ_ID}",
                    "save_id": _SAVE_ID,
                },
            )
            assert qq_ctx.status_code == 200
            qq_ctx_json = qq_ctx.json()
            assert "萨卡班甲鱼" in (qq_ctx_json["stable_brief"] or "")
            _assert_no_secret_or_raw_ids(qq_ctx_json["stable_brief"] or "")
            assert any(card["display_name"] == "老熟人甲" for card in qq_ctx_json["actor_cards"])
            for card in qq_ctx_json["actor_cards"]:
                _assert_no_secret_or_raw_ids(card["hint"])
            assert len(qq_ctx_json["fresh_topics"]) >= 1

            # QQ 群聊中麦麦本人同意今晚直播在 MC 建新灯塔，并顺口提到下午刷到的粘土视频
            bili_topic_id = qq_ctx_json["fresh_topics"][0]["id"]
            cons_qq = await client.post(f"/api/v1/topics/{bili_topic_id}/consume", json={"branch_id": "maibot"})
            assert cons_qq.status_code == 200

            qq_self_ingest = await client.post(
                "/api/v1/events",
                json={
                    "idempotency_key": "e2e-qq-self-promise-1",
                    "branch_id": "maibot",
                    "occurred_at_ms": _ms(2026, 11, 24, 19, 25),
                    "kind": "speech",
                    "actor_role": "self",
                    "visibility": "group",
                    "context_scope": "qq:group:888001",
                    "actors": [{"platform": "qq", "external_id": _FAKE_QQ_ID, "display_name": "甲QQ"}],
                    "summary": "在群里和老熟人甲聊起下午的萨卡班甲鱼粘土视频，并答应今晚直播去海边建新灯塔",
                    "verbatim": "对呀那个萨卡班甲鱼眼神绝了，我答应今晚直播就带你去MC海边建新灯塔！",
                    "save_id": _SAVE_ID,
                },
            )
            assert qq_self_ingest.status_code == 200
            # 不传 force_seal，只让未封存的 evening 生成动态 open_promise，保证已封存的 afternoon 慢层前缀不变
            synth_2 = await client.post("/api/v1/dashboard/synthesize", params={"force_seal": "false"})
            assert synth_2.status_code == 200
            assert synth_2.json()["report"]["created_anchors"] >= 1

            # ------------------------------------------------------------------
            # 步骤 ③：Amaidesu 正式开播签出，检查前缀缓存逐字节稳定、约定与三端熟人映射
            # ------------------------------------------------------------------
            clock.now_ms = _ms(2026, 11, 24, 20, 0)
            # 先连续拉取 Amaidesu context 两次验证慢层前缀缓存逐字节稳定 + ETag 命中
            stream_ctx_1 = await client.get(
                "/api/v1/views/context",
                params={"branch_id": "amaidesu", "target_visibility": "public", "save_id": _SAVE_ID},
            )
            stream_ctx_2 = await client.get(
                "/api/v1/views/context",
                params={"branch_id": "amaidesu", "target_visibility": "public", "save_id": _SAVE_ID},
            )
            assert stream_ctx_1.status_code == 200 and stream_ctx_2.status_code == 200
            s1_json, s2_json = stream_ctx_1.json(), stream_ctx_2.json()
            assert s1_json["brief_etag"] == s2_json["brief_etag"]
            assert s1_json["stable_brief"] is not None
            assert s1_json["stable_brief"] == s2_json["stable_brief"]  # 未跨时段更新时逐字节完全相同

            stream_ctx_hit = await client.get(
                "/api/v1/views/context",
                params={
                    "branch_id": "amaidesu",
                    "target_visibility": "public",
                    "save_id": _SAVE_ID,
                    "if_brief_etag": s1_json["brief_etag"],
                },
            )
            assert stream_ctx_hit.status_code == 200
            assert stream_ctx_hit.json()["stable_unchanged"] is True
            assert stream_ctx_hit.json()["stable_brief"] is None

            # 正式开播签出
            checkout_resp = await client.post(
                "/api/v1/sessions/checkout",
                json={
                    "branch_id": "amaidesu",
                    "external_session_id": "live-e2e-20261124",
                    "kind": "live",
                    "visibility": "public",
                    "title": "晚间杂谈与海边建灯塔",
                    "platform": "bilibili",
                    "room_id": "9001",
                    "segment_title": "开场杂谈",
                    "save_id": _SAVE_ID,
                },
            )
            assert checkout_resp.status_code == 200
            chk = checkout_resp.json()
            checkout_id = chk["checkout_id"]
            assert chk["stable_brief"] == s1_json["stable_brief"]
            _assert_no_secret_or_raw_ids(chk["stable_brief"])

            # 校验 open_promises 包含灯塔约定（不含 peer 伪造承诺）
            lighthouse_promises = [p for p in chk["open_promises"] if "灯塔" in p["content"]]
            assert len(lighthouse_promises) >= 1
            assert all("一万箱钻石" not in p["content"] for p in chk["open_promises"])
            lighthouse_promise_id = lighthouse_promises[0]["id"]

            # 校验 topic_cards 包含粘土视频并附带跨端已提及提示
            clay_cards = [t for t in chk["topic_cards"] if "萨卡班甲鱼" in t["title"] or "粘土" in t["summary"]]
            assert len(clay_cards) >= 1
            assert "提过一嘴" in clay_cards[0]["transition_hint"]
            _assert_no_secret_or_raw_ids(clay_cards[0]["summary"])

            # 校验 person_directory 将 B站与 MC 账号统一映射到“老熟人甲”且不暴露隐私原文
            dir_bili = [
                e for e in chk["person_directory"] if e["platform"] == "bilibili" and e["external_id"] == _FAKE_BILI_ID
            ]
            dir_mc = [
                e for e in chk["person_directory"] if e["platform"] == "minecraft" and e["external_id"] == _FAKE_MC_UUID
            ]
            assert len(dir_bili) == 1 and dir_bili[0]["display_name"] == "老熟人甲"
            assert len(dir_mc) == 1 and dir_mc[0]["display_name"] == "老熟人甲"
            assert "体检秘密" not in dir_bili[0]["prompt_hint"]
            assert "体检秘密" not in dir_mc[0]["prompt_hint"]

            # ------------------------------------------------------------------
            # 步骤 ④：播中心跳消费粘土话题 + 下播签入上报建好海边灯塔并兑现销账
            # ------------------------------------------------------------------
            clock.now_ms = _ms(2026, 11, 24, 21, 15)
            hb_resp = await client.post(
                f"/api/v1/sessions/{checkout_id}/heartbeat",
                json={
                    "segment_title": "Minecraft造灯塔",
                    "save_id": _SAVE_ID,
                    "consumed_seed_ids": [clay_cards[0]["id"]],
                },
            )
            assert hb_resp.status_code == 200
            assert hb_resp.json()["consumed_count"] == 1

            clock.now_ms = _ms(2026, 11, 24, 22, 30)
            checkin_resp = await client.post(
                f"/api/v1/sessions/{checkout_id}/checkin",
                json={
                    "ended_at_ms": _ms(2026, 11, 24, 22, 30),
                    "overview": "今晚直播顺着下午的甲鱼手工话题开聊，并在海边建成了高耸发光新灯塔",
                    "save_id": _SAVE_ID,
                    "save_summary": "主存档海边灯塔已竣工并点亮信标",
                    "archive_lines": [
                        {
                            "title": "建成海边发光新灯塔",
                            "verbatim": "海边的新灯塔终于亮起来了，大家看海面反光好好看",
                            "save_id": _SAVE_ID,
                            "highlight": True,
                        }
                    ],
                    "promises": [
                        {
                            "anchor_id": lighthouse_promise_id,
                            "content": lighthouse_promises[0]["content"],
                            "status": "closed",
                            "note": "直播当晚已实际完工点亮",
                            "save_id": _SAVE_ID,
                        }
                    ],
                    "consumed_seed_ids": [clay_cards[0]["id"]],
                },
            )
            assert checkin_resp.status_code == 200
            ci_json = checkin_resp.json()
            assert ci_json["ignored_rehearsal"] is False
            assert ci_json["ingested_events"] >= 2
            assert ci_json["updated_promises"] >= 1

            # 推进到深夜已封存晚间时段并刷新合成，让晚间经历进入持久化慢层
            clock.now_ms = _ms(2026, 11, 24, 23, 10)
            await client.post("/api/v1/dashboard/synthesize", params={"force_seal": "true"})

            # ------------------------------------------------------------------
            # 步骤 ⑤：次日/深夜 QQ 侧读取 context + recall + 真实 /mcp 工具回想
            # ------------------------------------------------------------------
            after_ctx = await client.get(
                "/api/v1/views/context",
                params={
                    "branch_id": "maibot",
                    "target_visibility": "group",
                    "active_actors": f"qq:{_FAKE_QQ_ID}",
                    "save_id": _SAVE_ID,
                },
            )
            assert after_ctx.status_code == 200
            after_json = after_ctx.json()
            assert "灯塔" in (after_json["stable_brief"] or "")
            assert "萨卡班甲鱼" in (after_json["stable_brief"] or "")
            _assert_no_secret_or_raw_ids(after_json["stable_brief"] or "")
            # 已销账的灯塔承诺不再出现在未了结列表
            assert all(p["id"] != lighthouse_promise_id for p in after_json["open_promises"])
            assert "海边灯塔已竣工" in after_json["save_brief"]

            # 群聊可见性调用 HTTP recall 查得到灯塔与粘土，查不到私聊秘密
            recall_group = await client.get(
                "/api/v1/views/recall",
                params={"query": "灯塔", "days": 7, "target_visibility": "group"},
            )
            assert recall_group.status_code == 200
            assert recall_group.json()["ok"] is True
            assert "灯塔" in recall_group.json()["text"]
            _assert_no_secret_or_raw_ids(recall_group.json()["text"])

            recall_secret = await client.get(
                "/api/v1/views/recall",
                params={"query": "体检秘密", "days": 7, "target_visibility": "group"},
            )
            assert recall_secret.status_code == 200
            assert "没有找到" in recall_secret.json()["text"]
            assert "保密代号" not in recall_secret.json()["text"]

        # 真实 StreamableHTTP 连接同端口 /mcp 调用 maigo_recall_life 与 maigo_lookup_person
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as mcp_http:
            async with streamable_http_client(
                f"{base_url}/mcp",
                http_client=mcp_http,
                terminate_on_close=False,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    mcp_recall = await session.call_tool(
                        "maigo_recall_life",
                        {"query": "灯塔", "days": 7, "target_visibility": "group"},
                    )
                    assert not mcp_recall.is_error
                    recall_txt = mcp_recall.content[0].text
                    assert "海边" in recall_txt and "灯塔" in recall_txt
                    _assert_no_secret_or_raw_ids(recall_txt)

                    mcp_person = await session.call_tool(
                        "maigo_lookup_person",
                        {"platform": "bilibili", "external_id": _FAKE_BILI_ID, "target_visibility": "group"},
                    )
                    assert not mcp_person.is_error
                    person_txt = mcp_person.content[0].text
                    assert "老熟人甲" in person_txt
                    _assert_no_secret_or_raw_ids(person_txt)
    finally:
        server.should_exit = True
        await server_task
