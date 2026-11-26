"""B 站日常刷站与偏好养成脚本（bilibili_browser）离线确定性单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Dict, List, Sequence

import httpx

from maigo.config import MaiGOConfig, SynthesizerConfig
from maigo.server import create_app

_BROWSER_PATH = Path(__file__).resolve().parents[1] / "examples" / "bilibili_browser" / "browser.py"


def _load_browser_module() -> ModuleType:
    """按文件路径加载示例脚本（提前注入 sys.modules 兼容 dataclass 解析）。"""
    mod_name = "maigo_bilibili_browser_test_mod"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = spec_from_file_location(mod_name, _BROWSER_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_browser_module()


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


class _FakeBiliRunner:
    """可编程的离线 bili 子进程替身，记录实际发出的每条 CLI 参数。"""

    def __init__(self, responses: Dict[str, Dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: List[List[str]] = []

    async def run_json(self, args: Sequence[str], *, timeout_s: float = 120.0) -> Dict[str, Any]:
        del timeout_s
        arg_list = list(args)
        self.calls.append(arg_list)
        key = " ".join(arg_list)
        for prefix, resp in self.responses.items():
            if key.startswith(prefix):
                return resp
        return {"ok": True, "data": {}}


def test_config_loader_and_rule_selection_avoids_unrelated_videos(tmp_path: Path) -> None:
    toml_path = tmp_path / "bili.toml"
    toml_path.write_text(
        """
branch_id = "bilibili_ops"
interests = ["沙盒建造", "手工"]
avoid_topics = ["引战"]
max_watch_per_run = 2
max_duration_seconds = 1800

[feedback]
record_history = true
allow_like = true
""".strip(),
        encoding="utf-8",
    )
    cfg = _mod.load_browser_config(toml_path)
    assert cfg.branch_id == "bilibili_ops"
    assert cfg.feedback.allow_like is True
    assert cfg.feedback.allow_fav is False

    candidates = [
        _mod._parse_candidate(
            {
                "bvid": "BV1bad0001",
                "title": "全网引战大盘点与情绪输出",
                "author": {"mid": "300001", "name": "作者甲"},
                "duration": 300,
                "desc": "手工",
            }
        ),
        _mod._parse_candidate(
            {
                "bvid": "BV1long0002",
                "title": "超长沙盒建造直播录屏",
                "author": {"mid": "300002", "name": "作者乙"},
                "duration": 5400,
            }
        ),
        _mod._parse_candidate(
            {
                "bvid": "BV1good0003",
                "title": "用纸板做桌面手工小夜灯",
                "author": {"mid": "300003", "name": "手工创作者"},
                "duration": 420,
            }
        ),
        _mod._parse_candidate(
            {
                "bvid": "BV1plain0004",
                "title": "普通日常路边随拍",
                "author": {"mid": "300004", "name": "路人丙"},
                "duration": 180,
            }
        ),
    ]
    filtered = [c for c in candidates if c is not None and c.duration <= cfg.max_duration_seconds]
    picks_task = _mod.select_candidates(filtered, config=cfg, stable_brief="")
    import asyncio

    picks = asyncio.run(picks_task)
    assert [cand.bvid for cand, _ in picks] == ["BV1good0003"]

    unrelated = [c for c in filtered if c.bvid == "BV1plain0004"]
    empty_picks = asyncio.run(_mod.select_candidates(unrelated, config=cfg, stable_brief=""))
    assert empty_picks == []


async def test_dry_run_never_records_interacts_or_submits(tmp_path: Path) -> None:
    state_file = tmp_path / "seen.json"
    cfg = _mod.BrowserConfig(
        maigo_url="http://testserver",
        state_file=str(state_file),
        interests=["手工"],
        feedback=_mod.FeedbackPolicy(
            record_history=True,
            allow_like=True,
            allow_fav=True,
            allow_follow=True,
            like_min_score=3,
            fav_min_score=3,
            follow_min_score=3,
            remember_min_score=3,
        ),
    )
    runner = _FakeBiliRunner(
        {
            "feed recommend": {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "bvid": "BV1craft001",
                            "title": "木工榫卯手工小凳子制作全过程",
                            "author": {"mid": "880011", "name": "木匠大叔"},
                            "duration": 480,
                            "desc": "一步步展示手工细节",
                        }
                    ]
                },
            },
            "video watch BV1craft001": {
                "ok": True,
                "data": {
                    "bvid": "BV1craft001",
                    "title": "木工榫卯手工小凳子制作全过程",
                    "author": {"mid": "880011", "name": "木匠大叔"},
                    "duration": 480,
                    "tags": ["手工", "木工"],
                    "ai_summary": "演示了不用一根铁钉的手工榫卯拼接方法。",
                    "transcript": {"source": "subtitle:zh", "text": "今天用废旧松木做一把手工小凳子。"},
                    "hot_comments": [{"user": "观众A", "content": "太治愈了", "like": 120}],
                    "danmaku": {"total": 20, "peaks": [{"sample": "强迫症舒适"}]},
                },
            },
        }
    )

    maigo_calls: List[str] = []

    def _maigo_handler(request: httpx.Request) -> httpx.Response:
        maigo_calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/api/v1/views/context":
            return httpx.Response(200, json={"stable_brief": "最近对手工小物很有兴趣"})
        raise AssertionError(f"dry-run 不应触发写接口: {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_maigo_handler),
        base_url="http://testserver",
    ) as mclient:
        res = await _mod.run_once(cfg, dry_run=True, runner=runner, maigo_client=mclient, now_ms_fn=lambda: 1795488000000)

    assert res.dry_run is True
    assert res.watched_count == 1
    assert res.submitted_events == 0
    assert not state_file.exists()
    assert maigo_calls == ["GET /api/v1/views/context"]
    watch_calls = [c for c in runner.calls if c[:2] == ["video", "watch"]]
    assert len(watch_calls) == 1
    assert "--record" not in watch_calls[0]
    assert not any(c[:2] in (["video", "like"], ["video", "fav"], ["user", "follow"]) for c in runner.calls)
    assert "record_history" in res.results[0].planned_actions
    assert "like" in res.results[0].planned_actions


async def test_live_run_feeds_algorithm_and_ingests_into_maigo(tmp_path: Path) -> None:
    state_file = tmp_path / "seen.json"
    now_ms = _ms(2026, 11, 26, 14, 0)
    app = create_app(
        MaiGOConfig(
            db_path=str(tmp_path / "maigo.sqlite3"),
            auth_token="bili-secret",
            synthesizer=SynthesizerConfig(auto_background=False),
        ),
        clock_ms=lambda: now_ms,
    )

    llm_prompts: List[str] = []

    def _llm_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        user_msg = payload["messages"][0]["content"]
        llm_prompts.append(user_msg)
        if "推荐列表如下" in user_msg:
            reply = json.dumps({"picks": [{"bvid": "BV1build009", "reason": "想学自动化农场思路"}]}, ensure_ascii=False)
        else:
            reply = json.dumps(
                {
                    "interest_score": 5,
                    "reaction": "这个沙盒自动农场结构超工整，晚上开播可以试试搭一个",
                    "should_remember": True,
                    "want_like": True,
                    "want_fav": True,
                    "want_follow": True,
                    "topic_title": "沙盒自动农场结构参考",
                },
                ensure_ascii=False,
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    runner = _FakeBiliRunner(
        {
            "feed recommend": {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "bvid": "BV1build009",
                            "title": "十分钟搭出紧凑全自动沙盒农场",
                            "author": {"mid": "99887766", "name": "红石工匠乙"},
                            "duration": 610,
                            "desc": "无需复杂材料",
                            "followed_author": False,
                        }
                    ]
                },
            },
            "video watch BV1build009": {
                "ok": True,
                "data": {
                    "bvid": "BV1build009",
                    "title": "十分钟搭出紧凑全自动沙盒农场",
                    "author": {"mid": "99887766", "name": "红石工匠乙"},
                    "duration": 610,
                    "tags": ["沙盒", "建筑"],
                    "ai_summary": "利用紧凑水流和定时电路实现全自动收割。",
                    "transcript": {"source": "subtitle:zh", "text": "我们先找一块平地放水渠。"},
                    "hot_comments": [],
                    "danmaku": {"total": 12, "peaks": []},
                    "recorded_in_history": True,
                },
            },
        }
    )

    cfg = _mod.BrowserConfig(
        maigo_url="http://testserver",
        auth_token="bili-secret",
        branch_id="bilibili_ops",
        state_file=str(state_file),
        interests=["沙盒", "建造"],
        llm=_mod.BrowserLLMConfig(base_url="http://mockllm/v1", api_key="k", model="mini"),
        feedback=_mod.FeedbackPolicy(
            record_history=True,
            allow_like=True,
            allow_fav=True,
            fav_folder="建造灵感",
            allow_follow=True,
        ),
    )

    async with app.router.lifespan_context(app):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as mclient,
            httpx.AsyncClient(transport=httpx.MockTransport(_llm_handler), base_url="http://mockllm/v1") as lclient,
        ):
            headers = {"Authorization": "Bearer bili-secret"}
            # 先写入一条早期事件并封存，检验后续选片能否读到稳定背景
            await mclient.post(
                "/api/v1/events",
                headers=headers,
                json={
                    "idempotency_key": "seed-prev-1",
                    "branch_id": "amaidesu",
                    "occurred_at_ms": _ms(2026, 11, 26, 10, 0),
                    "kind": "milestone",
                    "actor_role": "system",
                    "visibility": "public",
                    "summary": "昨天建好了半截木屋仓库，还差自动农场",
                },
            )
            await mclient.post("/api/v1/dashboard/synthesize?force_seal=true", headers=headers)

            res = await _mod.run_once(
                cfg,
                dry_run=False,
                runner=runner,
                maigo_client=mclient,
                llm_client=lclient,
                now_ms_fn=lambda: now_ms,
            )
            assert res.watched_count == 1
            assert res.submitted_events == 1
            assert res.accepted == 1
            assert state_file.exists()
            assert any("还差自动农场" in p for p in llm_prompts)

            assert ["video", "like", "BV1build009"] in runner.calls
            assert ["video", "fav", "BV1build009", "--folder", "建造灵感"] in runner.calls
            assert ["user", "follow", "99887766"] in runner.calls
            watch_cmd = next(c for c in runner.calls if c[:2] == ["video", "watch"])
            assert "--record" in watch_cmd

            # 封存整理并从直播视角拉取上下文：可见话题且不泄露数字 UID、不误生成观点/约定
            await mclient.post("/api/v1/dashboard/synthesize?force_seal=true", headers=headers)
            view = await mclient.get(
                "/api/v1/views/context?branch_id=amaidesu&target_visibility=public",
                headers=headers,
            )
            assert view.status_code == 200
            vdata = view.json()
            brief = vdata.get("stable_brief") or ""
            assert "十分钟搭出紧凑全自动沙盒农场" in brief
            assert "99887766" not in brief
            assert any("沙盒自动农场结构参考" in t["title"] for t in vdata["fresh_topics"])

            anchors = await mclient.get("/api/v1/dashboard/anchors", headers=headers)
            assert anchors.status_code == 200
            assert anchors.json()["items"] == []

            # 第二轮执行自动跳过已看过的 BV1build009
            second = await _mod.run_once(
                cfg,
                dry_run=False,
                runner=runner,
                maigo_client=mclient,
                llm_client=lclient,
                now_ms_fn=lambda: now_ms + 60_000,
            )
            assert second.candidates_fetched == 0
            assert second.watched_count == 0


async def test_risk_control_stops_immediately_without_continuing(tmp_path: Path) -> None:
    state_file = tmp_path / "seen.json"
    cfg = _mod.BrowserConfig(
        maigo_url="http://testserver",
        state_file=str(state_file),
        interests=["科普"],
        max_watch_per_run=2,
    )
    runner = _FakeBiliRunner(
        {
            "feed recommend": {
                "ok": True,
                "data": {
                    "items": [
                        {"bvid": "BV1sci001", "title": "趣味物理科普实验一", "author": {"mid": "1", "name": "科普甲"}, "duration": 200},
                        {"bvid": "BV1sci002", "title": "趣味物理科普实验二", "author": {"mid": "2", "name": "科普乙"}, "duration": 220},
                    ]
                },
            },
            "video watch BV1sci001": {
                "ok": False,
                "error": {"code": "risk_control", "message": "触发风控拦截"},
            },
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"stable_brief": ""})),
        base_url="http://testserver",
    ) as mclient:
        res = await _mod.run_once(cfg, dry_run=False, runner=runner, maigo_client=mclient)

    assert res.stopped_reason == "risk_control"
    assert res.watched_count == 0
    assert not any(c[:3] == ["video", "watch", "BV1sci002"] for c in runner.calls)


def test_seed_mode_and_cli_entry(tmp_path: Path) -> None:
    toml_path = tmp_path / "seed.toml"
    toml_path.write_text(
        f"""
branch_id = "bilibili_ops"
state_file = "{(tmp_path / "seen.json").as_posix()}"
mode = "seed"
seed_queries = ["桌面手工"]
interests = ["手工"]
""".strip(),
        encoding="utf-8",
    )
    cfg = _mod.load_browser_config(toml_path)
    runner = _FakeBiliRunner(
        {
            "search video 桌面手工": {
                "ok": True,
                "data": {
                    "items": [
                        {"bvid": "BV1seed001", "title": "桌面手工小风扇改装", "author": {"mid": "55", "name": "工匠丙"}, "duration": 180}
                    ]
                },
            },
            "video watch BV1seed001": {
                "ok": True,
                "data": {
                    "bvid": "BV1seed001",
                    "title": "桌面手工小风扇改装",
                    "author": {"mid": "55", "name": "工匠丙"},
                    "duration": 180,
                    "tags": ["手工"],
                },
            },
        }
    )

    import asyncio

    async def _run() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"stable_brief": ""})),
            base_url="http://testserver",
        ) as mclient:
            return await _mod.run_once(cfg, dry_run=True, runner=runner, maigo_client=mclient)

    res = asyncio.run(_run())
    assert res.watched_count == 1
    assert ["search", "video", "桌面手工", "--size", "8"] in runner.calls
    # 找不到 bili 可执行文件时安全返回退出码 2（stop=not_installed），不抛出异常
    bad_toml = tmp_path / "missing_bin.toml"
    bad_toml.write_text('bili_bin = "definitely_nonexistent_bili_bin_xyz"\n', encoding="utf-8")
    assert _mod.main(["--config", str(bad_toml), "--once", "--dry-run"]) == 2
