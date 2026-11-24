# RSS 订阅采集器（`rss_collector`）

独立于服务端的 RSS 2.0 / Atom 1.0 采集示例脚本，定时读取订阅源中的新增条目，附加第一人称简短评价后以 `browsed` 原始事件上报给 MaiGO：

- 仅使用 Python 标准库与已内置的 `httpx`，不引入额外强传染依赖。
- 同时支持 RSS 2.0 与 Atom 1.0（亦可在 `url` 中填本地 `.xml` 文件路径用于调试）。
- 使用本地 JSON 状态文件（默认 `data/rss_collector_seen.json`）按内容指纹记录已采集条目，并与 `POST /api/v1/events` 的 `idempotency_key` 配合防重。
- 可选通过 `[llm]` 段接入 OpenAI 兼容小模型生成简短评价；留空时自动回落规则文本。

## 使用方法

1. 复制示例配置并按需编辑订阅源 URL：

```powershell
Copy-Item examples/rss_collector/feeds.example.toml feeds.toml
```

2. 使用 `--dry-run --once` 预览解析与事件结构（不写状态也不请求服务端）：

```powershell
uv run python examples/rss_collector/collector.py --config feeds.toml --once --dry-run
```

3. 确认 MaiGO 服务端处于运行状态后，执行单次或周期采集：

```powershell
# 单次运行
uv run python examples/rss_collector/collector.py --config feeds.toml --once

# 常驻周期运行（默认每 1800 秒一次）
uv run python examples/rss_collector/collector.py --config feeds.toml
```

采集并经日内整理后，新增浏览记录会进入**稳定背景**（`stable_brief`）与**话题池**（`fresh_topics`）。
