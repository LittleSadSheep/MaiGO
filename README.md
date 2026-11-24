# MaiGO

MaiGO 是面向多端虚拟主播系统的跨端记忆与日常时间线服务，为 24 小时常驻端、按需启停的直播宿主、游戏宿主与外部社交平台客户端提供统一的事件收纳、日内整理与上下文视图。

## 职责边界

1. 独立承担跨端数据沉淀与视图投影，不接管群聊、直播或游戏宿主自身的实时对话决策循环。
2. 对外提供标准 HTTP JSON、SSE 与同端口 MCP 接口；各端通过边缘适配器接入，当 MaiGO 离线或禁用时各端仍可独立运行。
3. 观点与约定严格限制只从 `actor_role="self"` 的本人记录中提取，避免外部用户发言污染人设。
4. 跨平台人物账号仅依据显式绑定或自助口令合并，并在面向语言模型的视图中剥离原始平台账号与 `private` 级别私聊内容。
5. 上下文输出拆分为稳定背景（`stable_brief`）与实时动态（`live_context`），使系统提示词在同日无封存更新时维持前缀缓存有效。

## 核心能力与目录结构

- `src/maigo/core/`：领域模型（`models.py`）、隐私过滤与脱敏（`privacy.py`）、跨端人物档案（`identity.py`）、日内整理引擎（`digest.py`、`digest_rules.py`）、全文与向量双路检索（`recall.py`）、上下文构建（`context.py`）与运维维护（`maintenance.py`）。
- `src/maigo/storage/`：SQLite 连接封装（WAL 与 FTS5 全文索引）、Schema 迁移以及按表划分的仓储层（`repos/`）。
- `src/maigo/container.py`：服务依赖装配容器。
- `src/maigo/server/`：FastAPI 应用装配（`app.py`）、鉴权与通知广播（`auth.py`）、MCP 工具服务（`mcp.py`）以及划分清晰的路由（`routes/sync.py`、`routes/sessions.py`、`routes/dashboard.py`）。
- `dashboard/`：Vue 3 Web 管理看板，提供今日时间线、快捷录入、持续主线管理、观点与话题管理、跨端人物档案与接入监控功能。
- `examples/rss_collector/`：无额外第三方依赖的 RSS/Atom 订阅采集示例（仅用标准库与 `httpx`）。

## 快速启动与常用命令

1. 安装依赖并复制配置文件（`config.toml` 默认已被 Git 忽略）：

```powershell
uv sync
Copy-Item config.example.toml config.toml
```

2. 构建 Web 看板静态产物并启动服务（默认监听 `http://127.0.0.1:8768`）：

```powershell
pnpm --dir dashboard install
pnpm --dir dashboard build
uv run maigo serve --config config.toml
```

3. 诊断、热备份与记录导出命令：

```powershell
# 检查 SQLite 完整性、运行模式（llm/rule、hybrid/sparse）与备份数量
uv run maigo doctor --config config.toml

# 生成一份一致性 SQLite 热备份（按 keep_days 滚动清理旧文件）
uv run maigo backup --config config.toml

# 导出最近 3 天公开范围的日常记录 Markdown
uv run maigo export-diary --config config.toml --days 3 --visibility public
```

4. 试用 RSS 订阅采集器（详见 [`examples/rss_collector/README.md`](examples/rss_collector/README.md)）：

```powershell
uv run python examples/rss_collector/collector.py --config examples/rss_collector/feeds.example.toml --once --dry-run
```

## 文档导航

- [`CONTEXT.md`](CONTEXT.md)：全仓统一业务术语表与禁用别名说明。
- [架构总览](docs/architecture/overview.md)：模块分层、存储结构、混合检索与缓存策略说明。
- [开放协议与接入规范](docs/architecture/protocol.md)：HTTP/MCP 接口契约、多端接入流程与验收基准。
- [编码规范](AGENTS.md)：仓库架构约束、代码风格、脱敏要求与提交流程。
