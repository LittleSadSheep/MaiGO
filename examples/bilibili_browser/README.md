# B 站日常刷站与偏好养成示例（`bilibili_browser`）

独立于 `MaiGO` 主服务端的外部采集与推荐反馈示例，通过异步子进程调用开源 CLI 工具 `bili-agent`（命令名 `bili`），让账号按设定口味逐步收敛个性化推荐流，并把值得聊的见闻以 `browsed` 原始事件上报到 `MaiGO`。

> 依赖隔离说明：`bili-agent` 依赖的底层库声明为 `GPL-3.0-or-later`，因此本示例通过外部 `bili` 子进程 JSON 输出交互，**绝不将其引入 `MaiGO` 自身 Python 依赖**。

## 前置准备

1. 在独立命令行工具环境中安装 `bili-agent` 并使用正常使用过的账号扫码登录：

```powershell
uv tool install bili-agent
bili auth login
bili auth status --wait 120
```

2. 复制示例配置并按需求调整兴趣词、避开词与 `[llm]` 模型接口：

```powershell
Copy-Item examples/bilibili_browser/config.example.toml bilibili.toml
```

## 推荐试用顺序

1. **只读预演（推荐第一步）**：拉取当前首页推荐并输出选片、打分和拟执行动作，不回传观看历史，不写本地状态，也不向 `MaiGO` 提交事件：

```powershell
uv run python examples/bilibili_browser/browser.py --config bilibili.toml --once --dry-run
```

2. **冷启动播种（新空闲账号可先跑几天）**：按 `seed_queries` / `seed_zones` 搜寻并观看少量种子视频，把首页从默认大众热榜拉向目标兴趣：

```powershell
uv run python examples/bilibili_browser/browser.py --config bilibili.toml --once --mode seed --seed-query "手工制作"
```

3. **单次实操与日常周期运行**：确认选片口味稳定、且 `MaiGO` 服务端处于运行状态后执行：

```powershell
# 执行一轮完整“拉取背景 -> 选片观看并反馈 -> 上报 MaiGO”
uv run python examples/bilibili_browser/browser.py --config bilibili.toml --once

# 按配置间隔（带随机抖动、仅在 active_hours 内）持续巡航
uv run python examples/bilibili_browser/browser.py --config bilibili.toml
```

## 安全与防污染边界

- 所有上报 `MaiGO` 的刷站事件统一标为 `kind="browsed"` 与 `actor_role="system"`，确保他人视频观点不会误入`观点与约定`。
- 若配置放开 `allow_like` / `allow_fav` / `allow_follow`，建议仅在 `[llm]` 已接通且 `--dry-run` 观察无误后逐项开启。
- 一旦 `bili` 返回 `risk_control`、`captcha_required`、`not_logged_in` 或 `auth_expired`，脚本会立即中止本轮并退出，不做任何重试或绕过。
