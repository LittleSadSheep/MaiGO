# MaiGO 日常 RSS / Atom 巡检投喂示例（`daily_feeder`）

即使暂时还没有接入 B 站或 X 的外部运维程序，这个轻量化示例脚本也能让麦麦像真人刷关注列表一样，定时看几条自己感兴趣的科技/手工/日常资讯，并将「看到了什么 + 第一人称小感想」上报给 `MaiGO`：

- **零额外依赖**：仅依赖 Python 标准库与 `MaiGO` 已内置的 `httpx`，无 GPL/AGPL 组件；
- **兼容两种常见订阅**：同时解析 **RSS 2.0** 与 **Atom 1.0**（亦可直接指定本地 `.xml` 快照路径用于调试）；
- **自动去重**：使用本地 JSON 状态文件（默认 `data/daily_feeder_seen.json`）按内容指纹记录已看过的条目，配合 `POST /api/v1/events` 的 `idempotency_key` 双重幂等；
- **可选感想小模型**：在 `[llm]` 段填写 OpenAI 兼容接口后自动生成一句口语化想法；留空时使用轻量化规则感想兜底。

---

## 快速上手

1. 复制示例配置并按需替换为你自己的订阅源 URL（可保留多条 `[[feeds]]`）：

```powershell
Copy-Item examples/daily_feeder/feeds.example.toml feeds.toml
```

2. 先用 `--dry-run --once` 试跑一轮，确认解析和感想输出符合预期：

```powershell
uv run python examples/daily_feeder/feeder.py --config feeds.toml --once --dry-run
```

3. 确保 `MaiGO` 服务端已在运行（默认 `http://127.0.0.1:8768`），执行一次真实投喂：

```powershell
uv run python examples/daily_feeder/feeder.py --config feeds.toml --once
```

4. 如需长期常驻定时刷订阅，直接去掉 `--once`（默认每 `interval_seconds = 1800` 秒巡检一次）：

```powershell
uv run python examples/daily_feeder/feeder.py --config feeds.toml
```

投喂成功后，打开 `MaiGO` 看板「今日生活时间线」或触发一轮合成，就能在 `stable_brief` 与「鲜活话头池（`fresh_topics`）」里看到刚刷到的日常谈资。
