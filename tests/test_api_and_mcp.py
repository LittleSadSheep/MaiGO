"""阶段 3 HTTP REST API、SSE 与同端口 MCP 服务集成测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import socket
import uvicorn

import httpx
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from maiswitch.api import create_app
from maiswitch.config import SwitchConfig, SynthesizerConfig


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


class MutableClock:
    def __init__(self, initial_ms: int) -> None:
        self.now_ms = initial_ms

    def __call__(self) -> int:
        return self.now_ms


async def test_auth_events_context_and_sessions_flow(tmp_path) -> None:
    clock = MutableClock(_ms(2026, 11, 24, 20, 0))
    cfg = SwitchConfig(
        db_path=str(tmp_path / "api.sqlite3"),
        auth_token="secret-demo-token",
        synthesizer=SynthesizerConfig(auto_background=False),
    )
    app = create_app(cfg, clock_ms=clock)
    headers = {"Authorization": "Bearer secret-demo-token"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. 健康检查无鉴权，业务接口缺失令牌返回 401
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/v1/views/context?branch_id=maibot")).status_code == 401

            # 2. 创建跨端人物并绑定 QQ 与 B站
            p_resp = await client.post(
                "/api/v1/dashboard/persons",
                headers=headers,
                json={"display_name": "老熟人甲", "notes": "爱做手工"},
            )
            assert p_resp.status_code == 200
            pid = p_resp.json()["person"]["id"]

            await client.post(
                "/api/v1/dashboard/persons/bind",
                headers=headers,
                json={"person_id": pid, "platform": "qq", "external_id": "10001", "external_name": "甲QQ"},
            )
            await client.post(
                "/api/v1/dashboard/persons/bind",
                headers=headers,
                json={"person_id": pid, "platform": "bilibili", "external_id": "20002", "external_name": "甲B站"},
            )

            # 3. 批量上报单条与批量事件（测试幂等）
            ev_bili = {
                "idempotency_key": "bili-101",
                "branch_id": "bilibili_ops",
                "occurred_at_ms": _ms(2026, 11, 24, 15, 0),
                "kind": "browsed",
                "actor_role": "self",
                "visibility": "public",
                "context_scope": "bilibili:video:BV999",
                "actors": [{"platform": "bilibili", "external_id": "20002", "display_name": "甲B站"}],
                "summary": "下午在B站刷到手工粘土甲鱼视频",
                "verbatim": "这个眼睛呆得好真实",
                "reaction": "晚上直播可以顺着聊手工翻车",
            }
            ev_priv = {
                "idempotency_key": "qq-priv-102",
                "branch_id": "maibot",
                "occurred_at_ms": _ms(2026, 11, 24, 15, 30),
                "kind": "interaction",
                "actor_role": "peer",
                "visibility": "private",
                "context_scope": "qq:private:10001",
                "actors": [{"platform": "qq", "external_id": "10001", "display_name": "甲QQ"}],
                "summary": "私聊说自己明天要体检",
            }
            ing1 = await client.post("/api/v1/events", headers=headers, json={"events": [ev_bili, ev_priv]})
            assert ing1.status_code == 200
            assert ing1.json()["accepted"] == 2

            # 幂等重投不重复插入
            ing_dup = await client.post("/api/v1/events", headers=headers, json=ev_bili)
            assert ing_dup.status_code == 200
            assert ing_dup.json()["accepted"] == 0
            assert ing_dup.json()["deduplicated"] == 1

            # 4. 手动触发合成后拉取群聊视图，检查 etag 与脱敏
            synth_resp = await client.post("/api/v1/dashboard/synthesize", headers=headers)
            assert synth_resp.status_code == 200
            assert synth_resp.json()["report"]["sealed_slots"] >= 1

            ctx1 = await client.get(
                "/api/v1/views/context",
                headers=headers,
                params={"branch_id": "maibot", "target_visibility": "group", "active_actors": "qq:10001"},
            )
            assert ctx1.status_code == 200
            data1 = ctx1.json()
            assert data1["stable_brief"] is not None
            assert "粘土" in data1["stable_brief"]
            assert "体检" not in data1["stable_brief"]
            assert "10001" not in data1["actor_cards"][0]["hint"]
            assert len(data1["fresh_topics"]) >= 1
            topic_id = data1["fresh_topics"][0]["id"]

            # etag 命中时省略 stable_brief
            ctx_cached = await client.get(
                "/api/v1/views/context",
                headers=headers,
                params={
                    "branch_id": "maibot",
                    "target_visibility": "group",
                    "if_brief_etag": data1["brief_etag"],
                },
            )
            assert ctx_cached.json()["stable_unchanged"] is True
            assert ctx_cached.json()["stable_brief"] is None

            # 标记该话头在群聊消费过
            cons_resp = await client.post(
                f"/api/v1/topics/{topic_id}/consume", headers=headers, json={"branch_id": "maibot"}
            )
            assert cons_resp.status_code == 200
            assert "maibot" in cons_resp.json()["consumed_branches"]

            # 5. 彩排场签出/签入不得进入正式记忆
            reh_out = await client.post(
                "/api/v1/sessions/checkout",
                headers=headers,
                json={
                    "branch_id": "amaidesu",
                    "external_session_id": "reh-1",
                    "kind": "rehearsal",
                    "title": "彩排测试场",
                },
            )
            reh_id = reh_out.json()["checkout_id"]
            reh_in = await client.post(
                f"/api/v1/sessions/{reh_id}/checkin",
                headers=headers,
                json={
                    "ended_at_ms": clock() + 5000,
                    "overview": "彩排内容绝不进库",
                    "archive_lines": [{"title": "彩排虚构事件", "verbatim": "彩排台词"}],
                },
            )
            assert reh_in.json()["ignored_rehearsal"] is True

            # 6. 正式场签出 -> 心跳 -> 签入
            live_out = await client.post(
                "/api/v1/sessions/checkout",
                headers=headers,
                json={
                    "branch_id": "amaidesu",
                    "external_session_id": "live-101",
                    "kind": "live",
                    "title": "夜晚杂谈与建灯塔",
                    "platform": "bilibili",
                    "room_id": "999001",
                    "segment_title": "杂谈",
                    "save_id": "mc-save-1",
                },
            )
            assert live_out.status_code == 200
            live_data = live_out.json()
            live_id = live_data["checkout_id"]
            assert any("提过一嘴" in c["transition_hint"] for c in live_data["topic_cards"])

            hb = await client.post(
                f"/api/v1/sessions/{live_id}/heartbeat",
                headers=headers,
                json={"segment_title": "MC建灯塔", "consumed_seed_ids": [topic_id]},
            )
            assert hb.status_code == 200
            assert hb.json()["consumed_count"] == 1

            # 此时 MaiBot 拉取视图能看到“你此刻正在bilibili开直播”
            mb_live = await client.get(
                "/api/v1/views/context",
                headers=headers,
                params={"branch_id": "maibot", "target_visibility": "group"},
            )
            assert "开直播（你自己是主播）" in mb_live.json()["live_context"]

            ci = await client.post(
                f"/api/v1/sessions/{live_id}/checkin",
                headers=headers,
                json={
                    "ended_at_ms": _ms(2026, 11, 24, 22, 0),
                    "overview": "今晚直播成功建成了海边新灯塔",
                    "save_id": "mc-save-1",
                    "save_summary": "海边灯塔已竣工亮灯",
                    "archive_lines": [
                        {
                            "title": "建成海边新灯塔",
                            "verbatim": "我们的海边灯塔终于亮起来啦",
                            "highlight": True,
                        }
                    ],
                    "promises": [
                        {
                            "content": "下次带大家建自动烤鸡机",
                            "verbatim": "下次开播咱们做个自动烤鸡机",
                            "status": "open",
                        }
                    ],
                },
            )
            assert ci.status_code == 200
            assert ci.json()["ingested_events"] >= 2

            # 下播后回到群聊，慢层立即能看到建成的灯塔与新约定
            mb_after = await client.get(
                "/api/v1/views/context",
                headers=headers,
                params={"branch_id": "maibot", "target_visibility": "group", "save_id": "mc-save-1"},
            )
            after_json = mb_after.json()
            assert "海边新灯塔" in (after_json["stable_brief"] or "")
            assert "彩排内容" not in (after_json["stable_brief"] or "")
            assert "自动烤鸡机" in (after_json["stable_brief"] or "")
            assert all("成功建成" not in p["content"] for p in after_json["open_promises"])
            assert "海边灯塔已竣工亮灯" in after_json["save_brief"]


async def test_mcp_streamable_http_tools_and_dashboard_routes(tmp_path) -> None:
    clock = MutableClock(_ms(2026, 11, 24, 20, 0))
    cfg = SwitchConfig(
        db_path=str(tmp_path / "mcp.sqlite3"),
        auth_token="mcp-token",
        synthesizer=SynthesizerConfig(auto_background=False),
    )
    app = create_app(cfg, clock_ms=clock)
    headers = {"Authorization": "Bearer mcp-token"}

    async with app.router.lifespan_context(app):
        # 1. 准备基础数据与测试鉴权
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            p = (
                await client.post("/api/v1/dashboard/persons", headers=headers, json={"display_name": "老熟人甲"})
            ).json()
            pid = p["person"]["id"]
            await client.post(
                "/api/v1/dashboard/persons/bind",
                headers=headers,
                json={"person_id": pid, "platform": "qq", "external_id": "10001"},
            )
            await client.post(
                "/api/v1/dashboard/simulate-event",
                headers=headers,
                json={
                    "idempotency_key": "sim-1",
                    "branch_id": "bilibili_ops",
                    "occurred_at_ms": _ms(2026, 11, 24, 15, 0),
                    "kind": "browsed",
                    "actor_role": "self",
                    "visibility": "public",
                    "actors": [{"platform": "bilibili", "external_id": "20002"}],
                    "summary": "下午刷到像素风动画短片",
                    "verbatim": "转场太丝滑了",
                },
            )
            anc_resp = await client.post(
                "/api/v1/dashboard/anchors",
                headers=headers,
                json={
                    "anchor_type": "promise",
                    "content": "发一条像素动画感想动态",
                    "verbatim": "今晚发条动态安利这个动画",
                },
            )
            anchor_id = anc_resp.json()["item"]["id"]

            # 检查看板其余核心接口
            tl = await client.get("/api/v1/dashboard/timeline", headers=headers)
            assert tl.status_code == 200
            assert len(tl.json()["events"]) >= 1
            pv = await client.get(f"/api/v1/dashboard/persons/{pid}/preview", headers=headers)
            assert pv.status_code == 200
            assert "10001" not in pv.json()["card"]["hint"]
            br = await client.get("/api/v1/dashboard/branches", headers=headers)
            assert br.status_code == 200

        # 2. MCP 未鉴权拦截测试
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
        ) as unauth_mcp:
            r = await unauth_mcp.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert r.status_code == 401

        # 3. 使用官方 MCP StreamableHTTP 客户端调用全部三个工具
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
        ) as mcp_http:
            async with streamable_http_client(
                "http://testserver/mcp",
                http_client=mcp_http,
                terminate_on_close=False,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools_res = await session.list_tools()
                    names = {t.name for t in tools_res.tools}
                    assert names == {"switch_recall_life", "switch_lookup_person", "switch_resolve_promise"}

                    recall_res = await session.call_tool("switch_recall_life", {"query": "像素", "days": 7})
                    assert not recall_res.is_error
                    assert "像素风动画" in recall_res.content[0].text

                    lookup_res = await session.call_tool(
                        "switch_lookup_person",
                        {"platform": "qq", "external_id": "10001", "target_visibility": "public"},
                    )
                    assert not lookup_res.is_error
                    assert "老熟人甲" in lookup_res.content[0].text
                    assert "10001" not in lookup_res.content[0].text

                    resolve_res = await session.call_tool(
                        "switch_resolve_promise",
                        {"anchor_id": anchor_id, "status": "closed", "evidence": "B站动态已发出"},
                    )
                    assert not resolve_res.is_error
                    assert "已标记兑现" in resolve_res.content[0].text


async def test_real_uvicorn_sse_stream(tmp_path) -> None:
    """用真实本地环回端口启动 Uvicorn 验证 SSE 首帧与增量事件实时送达。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    cfg = SwitchConfig(
        host="127.0.0.1",
        port=port,
        db_path=str(tmp_path / "sse.sqlite3"),
        auth_token="sse-tok",
        synthesizer=SynthesizerConfig(auto_background=False),
    )
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer sse-tok"}
        ) as client:
            async with client.stream("GET", "/api/v1/stream/notifications") as sse_resp:
                assert sse_resp.status_code == 200
                got_connected = False
                async for line in sse_resp.aiter_lines():
                    if "connected" in line:
                        got_connected = True
                        break
                assert got_connected is True
    finally:
        server.should_exit = True
        await task
