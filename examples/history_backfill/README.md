# 历史记忆幂等回填工具（`history_backfill`）

用于将 `Amaidesu`（直播/游戏宿主）或 `MaiBot`（QQ 聊天宿主）在接入 `MaiGO` 之前积累的高信噪比历史数据，以只读方式抽取并分批导入 `MaiGO`：

- 所有记录均带稳定 `idempotency_key`，可反复执行不重复入库；
- 保留每条记录原始发生时间戳，自动落入对应作息日的时间线、约定台账与混合语义检索；
- 对已配置首次启动自动回填的宿主端，也可直接用本脚本先做 `--dry-run` 预览或指定更长天数补全。

## 1. 在直播机回填 `Amaidesu` 历史库

默认过滤 `rehearsal` 彩排场、带指人私密标记的承诺与已遗忘玩家，优先导出正式场档案行、公开约定和玩家互动（早期无档案行的场次自动回落 `timeline_summary` 与关键游戏事件）：

```powershell
# 1. 先只读预演，确认条数与抽样内容
uv run python examples/history_backfill/backfill.py amaidesu --db path/to/amaidesu.db --days 30 --dry-run

# 2. 正式分批写入远程 MaiGO 服务端并触发一轮日内整理
uv run python examples/history_backfill/backfill.py amaidesu --db path/to/amaidesu.db --url http://<maigo-host>:8768 --token "<auth_token>" --days 30 --synthesize
```

## 2. 在 `MaiBot` 主机回填 QQ 历史库

自动从 `bot_platform_accounts` 识别 Bot 自身账号，仅抽取白名单群聊与私聊中麦麦本人发出且达到字数门槛的有效回复（跳过表情、图片、命令与控制台调试记录）：

```bash
# 1. 预演指定群号与私聊最近 30 天的有效发言
uv run python examples/history_backfill/backfill.py maibot --db /root/maibot/data/MaiBot.db --group <群号> --days 30 --dry-run

# 2. 正式导入本机或远端 MaiGO
uv run python examples/history_backfill/backfill.py maibot --db /root/maibot/data/MaiBot.db --url http://127.0.0.1:8768 --token "<auth_token>" --group <群号> --days 30 --synthesize
```
