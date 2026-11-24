"""MaiGO 独立日常 RSS/Atom 巡检投喂脚本（零新增外部依赖，仅用标准库 + httpx）。

定期抓取订阅源的新内容，生成第一人称观后感并上报 ``POST /api/v1/events``，
让虚拟主播像真人刷手机关注列表一样自然沉淀日常见闻与直播杂谈话头。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import tomllib
from typing import Any, Callable, Dict, List, Optional, Sequence
from xml.etree import ElementTree

import httpx

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_MAX_SEEN_KEYS = 2000


@dataclass(frozen=True)
class FeedSource:
    """单个订阅源配置。"""

    name: str
    url: str
    tag: str = ""


@dataclass(frozen=True)
class FeederLLMConfig:
    """可选观后感生成模型配置。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_ms: int = 10_000


@dataclass(frozen=True)
class FeederConfig:
    """日常巡检投喂器根配置。"""

    maigo_url: str = "http://127.0.0.1:8768"
    auth_token: str = ""
    branch_id: str = "daily_feeder"
    state_file: str = "data/daily_feeder_seen.json"
    interval_seconds: int = 1800
    max_items_per_feed: int = 3
    persona_name: str = "麦麦"
    llm: FeederLLMConfig = field(default_factory=FeederLLMConfig)
    feeds: List[FeedSource] = field(default_factory=list)


@dataclass(frozen=True)
class FeedItem:
    """解析后的单条订阅条目。"""

    title: str
    link: str
    summary: str
    guid: str = ""
    source_name: str = ""
    tag: str = ""


@dataclass(frozen=True)
class FeederRunResult:
    """单轮巡检执行统计。"""

    fetched_items: int = 0
    new_items: int = 0
    submitted_events: int = 0
    accepted: int = 0
    deduplicated: int = 0
    dry_run: bool = False
    preview_events: List[Dict[str, Any]] = field(default_factory=list)


def _local_name(tag: str) -> str:
    """去掉 XML 命名空间前缀，统一小写节点名判断。"""
    if "}" in tag:
        return tag.split("}", 1)[1].lower()
    return tag.lower()


def _clean_text(raw: Optional[str], *, max_len: int = 220) -> str:
    """清理 HTML 标签并压缩空白，提取紧凑易读摘要。"""
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    plain = _TAG_RE.sub(" ", unescaped)
    compact = _SPACE_RE.sub(" ", plain).strip()
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rstrip() + "…"


def _child_text(elem: ElementTree.Element, names: Sequence[str]) -> str:
    """按候选短节点名查找首个非空文本。"""
    wanted = tuple(n.lower() for n in names)
    for child in elem:
        if _local_name(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _atom_link(entry: ElementTree.Element) -> str:
    """从 Atom entry 的 ``<link>`` 节点提取最佳跳转 URL。"""
    fallback = ""
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if not href:
            href = (child.text or "").strip()
        if not href:
            continue
        rel = (child.attrib.get("rel") or "alternate").strip().lower()
        if rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def parse_feed_xml(xml_text: str, *, source_name: str = "", tag: str = "") -> List[FeedItem]:
    """解析 RSS 2.0 或 Atom XML 文本为统一条目列表（纯标准库实现）。"""
    cleaned_xml = (xml_text or "").strip()
    if not cleaned_xml:
        return []
    root = ElementTree.fromstring(cleaned_xml)
    root_name = _local_name(root.tag)
    items: List[FeedItem] = []

    if root_name == "rss" or root_name == "rdf":
        # RSS 2.0 / 1.0：沿 channel/item 或根下 item 解析
        feed_title = source_name or ""
        channel_nodes = [c for c in root if _local_name(c.tag) == "channel"]
        search_roots = channel_nodes if channel_nodes else [root]
        for sr in search_roots:
            if not feed_title:
                feed_title = _clean_text(_child_text(sr, ("title",)), max_len=60)
            for node in sr:
                if _local_name(node.tag) != "item":
                    continue
                title = _clean_text(_child_text(node, ("title",)), max_len=120)
                link = _child_text(node, ("link",))
                guid = _child_text(node, ("guid",))
                desc = _clean_text(_child_text(node, ("encoded", "description", "summary")), max_len=240)
                if not title and not desc:
                    continue
                items.append(
                    FeedItem(
                        title=title or "未命名条目",
                        link=link,
                        summary=desc or title,
                        guid=guid,
                        source_name=feed_title,
                        tag=tag,
                    )
                )
        return items

    if root_name == "feed":
        # Atom 1.0：<feed><entry>...</entry></feed>
        feed_title = source_name or _clean_text(_child_text(root, ("title",)), max_len=60)
        for node in root:
            if _local_name(node.tag) != "entry":
                continue
            title = _clean_text(_child_text(node, ("title",)), max_len=120)
            link = _atom_link(node)
            guid = _child_text(node, ("id",))
            desc = _clean_text(_child_text(node, ("summary", "content")), max_len=240)
            if not title and not desc:
                continue
            items.append(
                FeedItem(
                    title=title or "未命名条目",
                    link=link,
                    summary=desc or title,
                    guid=guid,
                    source_name=feed_title,
                    tag=tag,
                )
            )
        return items

    return items


def load_feeder_config(path: Path | str) -> FeederConfig:
    """从 TOML 文件加载巡检配置，并支持 ``MAIGO_AUTH_TOKEN`` 环境变量覆盖令牌。"""
    cfg_path = Path(path)
    raw: Dict[str, Any] = tomllib.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    llm_raw = raw.get("llm") or {}
    llm_cfg = FeederLLMConfig(
        base_url=str(llm_raw.get("base_url") or ""),
        api_key=str(llm_raw.get("api_key") or os.environ.get("MAIGO_SYNTH_API_KEY", "")),
        model=str(llm_raw.get("model") or ""),
        timeout_ms=int(llm_raw.get("timeout_ms") or 10_000),
    )
    feeds: List[FeedSource] = []
    for entry in raw.get("feeds") or []:
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        feeds.append(
            FeedSource(
                name=str(entry.get("name") or "").strip(),
                url=url,
                tag=str(entry.get("tag") or "").strip(),
            )
        )
    token = str(raw.get("auth_token") or os.environ.get("MAIGO_AUTH_TOKEN", ""))
    return FeederConfig(
        maigo_url=str(raw.get("maigo_url") or "http://127.0.0.1:8768").rstrip("/"),
        auth_token=token,
        branch_id=str(raw.get("branch_id") or "daily_feeder"),
        state_file=str(raw.get("state_file") or "data/daily_feeder_seen.json"),
        interval_seconds=max(30, int(raw.get("interval_seconds") or 1800)),
        max_items_per_feed=max(1, int(raw.get("max_items_per_feed") or 3)),
        persona_name=str(raw.get("persona_name") or "麦麦"),
        llm=llm_cfg,
        feeds=feeds,
    )


def compute_item_key(item: FeedItem) -> str:
    """按订阅标识/链接/标题生成稳定的内容去重指纹。"""
    identity = f"{item.tag}|{item.guid or item.link or item.title}".strip()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def load_seen_keys(state_path: Path) -> Dict[str, int]:
    """读取本地去重状态文件；文件缺失或损坏时安全回空表。"""
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}


def save_seen_keys(state_path: Path, seen: Dict[str, int]) -> None:
    """保存最近若干条已见内容指纹到本地 JSON，控制文件体积不无限膨胀。"""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_SEEN_KEYS]
    state_path.write_text(json.dumps(dict(ordered), ensure_ascii=False, indent=2), encoding="utf-8")


def build_rule_reaction(item: FeedItem, *, persona_name: str = "麦麦") -> str:
    """在未配置小模型时按标签与标题产出自然的日常观后感。"""
    if item.tag:
        return f"看到{item.tag}向的内容《{item.title}》，觉得蛮有趣的，晚点闲聊可以顺带提一嘴"
    return f"刚刷到一篇《{item.title}》，{persona_name}觉得还挺新鲜，先记在小本本上"


async def generate_reaction(
    item: FeedItem,
    *,
    config: FeederConfig,
    http_client: Optional[httpx.AsyncClient] = None,
) -> str:
    """优先调用可选 OpenAI 兼容模型写一句第一人称随想，不可用时回落规则感想。"""
    llm = config.llm
    if not llm.base_url.strip() or not llm.model.strip():
        return build_rule_reaction(item, persona_name=config.persona_name)

    headers = {"Content-Type": "application/json"}
    if llm.api_key:
        headers["Authorization"] = f"Bearer {llm.api_key}"
    prompt = (
        f"你是{config.persona_name}，刚随手刷到一条关注动态：\n"
        f"来源：{item.source_name or '日常资讯'}（{item.tag or '杂项'}）\n"
        f"标题：{item.title}\n"
        f"摘要：{item.summary}\n"
        "请用一句自然口语写出你的真实感想或吐槽（不超过 45 个字，不带标题前缀）。"
    )
    payload = {
        "model": llm.model,
        "temperature": 0.6,
        "max_tokens": 90,
        "messages": [{"role": "user", "content": prompt}],
    }
    timeout_s = max(1.0, llm.timeout_ms / 1000.0)
    try:
        if http_client is not None:
            resp = await http_client.post(
                f"{llm.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        else:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(
                    f"{llm.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        clean = _SPACE_RE.sub(" ", str(content)).strip()
        return clean[:120] if clean else build_rule_reaction(item, persona_name=config.persona_name)
    except Exception:
        return build_rule_reaction(item, persona_name=config.persona_name)


def build_event_payload(
    item: FeedItem,
    *,
    item_key: str,
    branch_id: str,
    reaction: str,
    occurred_at_ms: int,
) -> Dict[str, Any]:
    """封装符合 MaiGO ``POST /api/v1/events`` 契约的统一 ``browsed`` 事件。"""
    source_label = item.source_name or "关注列表"
    summary_text = f"浏览了「{source_label}」的《{item.title}》：{item.summary}"
    if len(summary_text) > 360:
        summary_text = summary_text[:359].rstrip() + "…"
    return {
        "idempotency_key": f"feeder-{item_key}",
        "branch_id": branch_id,
        "occurred_at_ms": int(occurred_at_ms),
        "kind": "browsed",
        "actor_role": "self",
        "visibility": "public",
        "context_scope": f"feed:{source_label}"[:120],
        "actors": [],
        "summary": summary_text,
        "verbatim": "",
        "reaction": reaction,
        "save_id": None,
        "metadata": {
            "topic_title": item.title[:48],
            "source_title": item.title,
            "feed_tag": item.tag,
            "link": item.link,
        },
    }


async def _fetch_feed_xml(url: str, *, client: httpx.AsyncClient) -> str:
    """抓取远端 HTTP(S) 订阅或读取本地 XML 快照文件（便于离线测试）。"""
    if url.startswith(("http://", "https://")):
        resp = await client.get(url, follow_redirects=True, timeout=15.0)
        resp.raise_for_status()
        return resp.text
    path = Path(url)
    return await asyncio.to_thread(path.read_text, encoding="utf-8")


async def run_once(
    config: FeederConfig,
    *,
    dry_run: bool = False,
    maigo_client: Optional[httpx.AsyncClient] = None,
    feed_client: Optional[httpx.AsyncClient] = None,
    now_ms_fn: Callable[[], int] = lambda: int(time.time() * 1000),
) -> FeederRunResult:
    """执行一轮订阅巡检，抽取新增条目并投喂到 MaiGO。"""
    state_path = Path(config.state_file)
    seen = await asyncio.to_thread(load_seen_keys, state_path)

    fetched_items = 0
    candidate_events: List[Dict[str, Any]] = []
    candidate_keys: List[str] = []

    owns_feed_client = feed_client is None
    active_feed_client = feed_client or httpx.AsyncClient(timeout=15.0)
    try:
        for feed in config.feeds:
            try:
                xml_text = await _fetch_feed_xml(feed.url, client=active_feed_client)
                parsed = parse_feed_xml(xml_text, source_name=feed.name, tag=feed.tag)
            except Exception as exc:
                print(f"[daily_feeder] 拉取订阅源失败 ({feed.url}): {exc}", file=sys.stderr)
                continue

            fetched_items += len(parsed)
            picked = 0
            for item in parsed:
                item_key = compute_item_key(item)
                if item_key in seen:
                    continue
                reaction = await generate_reaction(item, config=config, http_client=active_feed_client)
                ev = build_event_payload(
                    item,
                    item_key=item_key,
                    branch_id=config.branch_id,
                    reaction=reaction,
                    occurred_at_ms=now_ms_fn(),
                )
                candidate_events.append(ev)
                candidate_keys.append(item_key)
                picked += 1
                if picked >= config.max_items_per_feed:
                    break
    finally:
        if owns_feed_client:
            await active_feed_client.aclose()

    if not candidate_events or dry_run:
        return FeederRunResult(
            fetched_items=fetched_items,
            new_items=len(candidate_events),
            submitted_events=0,
            accepted=0,
            deduplicated=0,
            dry_run=dry_run,
            preview_events=candidate_events,
        )

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if config.auth_token.strip():
        headers["Authorization"] = f"Bearer {config.auth_token.strip()}"

    owns_maigo_client = maigo_client is None
    active_maigo_client = maigo_client or httpx.AsyncClient(base_url=config.maigo_url, timeout=10.0)
    try:
        resp = await active_maigo_client.post(
            "/api/v1/events",
            headers=headers,
            json={"events": candidate_events},
        )
        resp.raise_for_status()
        body = resp.json()
    finally:
        if owns_maigo_client:
            await active_maigo_client.aclose()

    ts_now = now_ms_fn()
    for key in candidate_keys:
        seen[key] = ts_now
    await asyncio.to_thread(save_seen_keys, state_path, seen)

    return FeederRunResult(
        fetched_items=fetched_items,
        new_items=len(candidate_events),
        submitted_events=len(candidate_events),
        accepted=int(body.get("accepted", 0)),
        deduplicated=int(body.get("deduplicated", 0)),
        dry_run=False,
        preview_events=candidate_events,
    )


async def _run_loop(args: argparse.Namespace) -> int:
    cfg = load_feeder_config(args.config)
    interval = int(args.interval) if args.interval is not None else cfg.interval_seconds

    while True:
        res = await run_once(cfg, dry_run=bool(args.dry_run))
        mode_tag = "DRY-RUN" if res.dry_run else "PUSH"
        print(
            f"[daily_feeder] {mode_tag}: fetched={res.fetched_items}, "
            f"new={res.new_items}, accepted={res.accepted}, dedup={res.deduplicated}"
        )
        if args.dry_run:
            for ev in res.preview_events:
                print(f"  - {ev['summary']} | 感想: {ev['reaction']}")
        if args.once:
            return 0
        await asyncio.sleep(max(5, interval))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：支持 ``--config``、``--once``、``--dry-run`` 与 ``--interval``。"""
    parser = argparse.ArgumentParser(description="MaiGO 日常 RSS/Atom 巡检投喂脚本")
    parser.add_argument("--config", default="examples/daily_feeder/feeds.example.toml", help="TOML 配置文件路径")
    parser.add_argument("--once", action="store_true", help="仅执行一轮巡检后退出")
    parser.add_argument("--dry-run", action="store_true", help="仅解析和预览待上报事件，不写入状态也不调用服务端")
    parser.add_argument("--interval", type=int, default=None, help="覆盖常驻巡检间隔秒数")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_loop(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
