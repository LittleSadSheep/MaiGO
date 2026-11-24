"""daily_feeder RSS/Atom 解析、去重与写入 MaiGO 闭环单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType

import httpx

from maigo.config import MaiGOConfig, SynthesizerConfig
from maigo.server import create_app

_FEEDER_PATH = Path(__file__).resolve().parents[1] / "examples" / "daily_feeder" / "feeder.py"


def _load_feeder_module() -> ModuleType:
    """按文件路径加载示例脚本模块（确保在 Python 3.14 下 dataclass 正常解析）。"""
    mod_name = "maigo_daily_feeder_test_mod"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = spec_from_file_location(mod_name, _FEEDER_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_feeder = _load_feeder_module()

_RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>手工周刊</title>
    <item>
      <title>旧纸箱改造桌面收纳盒教程</title>
      <link>https://example.com/posts/101</link>
      <guid>rss-101</guid>
      <description><![CDATA[<p>用瓦楞纸和白乳胶做了个分层小抽屉，成本只要几块钱。</p>]]></description>
    </item>
    <item>
      <title>手冲滤杯水温对比小实验</title>
      <link>https://example.com/posts/102</link>
      <guid>rss-102</guid>
      <description>对比了九十二度与八十八度的风味差异。</description>
    </item>
  </channel>
</rss>
"""

_ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>技术观察</title>
  <entry>
    <id>tag:example.org,2026:post-1</id>
    <title>SQLite WAL 模式在单机小服务里的应用</title>
    <link rel="alternate" href="https://example.org/notes/sqlite-wal" />
    <summary>总结了单写入连接与读写分离的注意事项。</summary>
  </entry>
</feed>
"""


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


def test_parse_rss_and_atom_feeds() -> None:
    rss_items = _feeder.parse_feed_xml(_RSS_SAMPLE, tag="手工")
    assert len(rss_items) == 2
    assert rss_items[0].source_name == "手工周刊"
    assert rss_items[0].title == "旧纸箱改造桌面收纳盒教程"
    assert "<p>" not in rss_items[0].summary
    assert "瓦楞纸和白乳胶" in rss_items[0].summary

    atom_items = _feeder.parse_feed_xml(_ATOM_SAMPLE, tag="科技")
    assert len(atom_items) == 1
    assert atom_items[0].source_name == "技术观察"
    assert atom_items[0].link == "https://example.org/notes/sqlite-wal"
    assert "单写入连接" in atom_items[0].summary


async def test_daily_feeder_dedup_dry_run_and_maigo_ingestion(tmp_path: Path) -> None:
    rss_file = tmp_path / "craft.xml"
    atom_file = tmp_path / "tech.xml"
    rss_file.write_text(_RSS_SAMPLE, encoding="utf-8")
    atom_file.write_text(_ATOM_SAMPLE, encoding="utf-8")
    state_file = tmp_path / "seen.json"

    cfg = _feeder.FeederConfig(
        maigo_url="http://testserver",
        auth_token="feeder-secret",
        branch_id="daily_feeder",
        state_file=str(state_file),
        max_items_per_feed=2,
        feeds=[
            _feeder.FeedSource(name="手工周刊", url=str(rss_file), tag="手工"),
            _feeder.FeedSource(name="技术观察", url=str(atom_file), tag="科技"),
        ],
    )

    now_ms = _ms(2026, 11, 24, 15, 0)
    app = create_app(
        MaiGOConfig(
            db_path=str(tmp_path / "maigo.sqlite3"),
            auth_token="feeder-secret",
            synthesizer=SynthesizerConfig(auto_background=False),
        ),
        clock_ms=lambda: now_ms,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. dry-run 不写入去重状态，也不请求服务端
            dry_res = await _feeder.run_once(cfg, dry_run=True, maigo_client=client, now_ms_fn=lambda: now_ms)
            assert dry_res.dry_run is True
            assert dry_res.new_items == 3
            assert dry_res.submitted_events == 0
            assert not state_file.exists()

            # 2. 真实上报后去重文件落盘，共写入 3 条浏览见闻
            push_res = await _feeder.run_once(cfg, dry_run=False, maigo_client=client, now_ms_fn=lambda: now_ms)
            assert push_res.new_items == 3
            assert push_res.accepted == 3
            assert state_file.exists()

            # 3. 再次运行自动跳过已看过条目
            second_res = await _feeder.run_once(cfg, dry_run=False, maigo_client=client, now_ms_fn=lambda: now_ms)
            assert second_res.new_items == 0
            assert second_res.submitted_events == 0

            # 4. 触发合成并封存后，在上下文简报与鲜活话头中可见
            headers = {"Authorization": "Bearer feeder-secret"}
            syn = await client.post("/api/v1/dashboard/synthesize?force_seal=true", headers=headers)
            assert syn.status_code == 200

            view = await client.get(
                "/api/v1/views/context?branch_id=amaidesu&target_visibility=public",
                headers=headers,
            )
            assert view.status_code == 200
            vdata = view.json()
            assert "旧纸箱改造桌面收纳盒教程" in (vdata["stable_brief"] or "")
            assert any("旧纸箱改造桌面收纳盒教程" in t["title"] for t in vdata["fresh_topics"])


def test_feeder_cli_once_dry_run_and_config_loader(tmp_path: Path) -> None:
    xml_path = tmp_path / "sample.xml"
    xml_path.write_text(_ATOM_SAMPLE, encoding="utf-8")
    toml_path = tmp_path / "feeds.toml"
    toml_path.write_text(
        f"""
branch_id = "daily_feeder"
state_file = "{(tmp_path / "state.json").as_posix()}"

[[feeds]]
name = "本地观察"
url = "{xml_path.as_posix()}"
tag = "科技"
""".strip(),
        encoding="utf-8",
    )
    loaded = _feeder.load_feeder_config(toml_path)
    assert loaded.branch_id == "daily_feeder"
    assert len(loaded.feeds) == 1

    code = _feeder.main(["--config", str(toml_path), "--once", "--dry-run"])
    assert code == 0
    assert not (tmp_path / "state.json").exists()
