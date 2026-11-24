"""MaiGO 运行配置模型与加载逻辑。

支持优先级：环境变量 ``MAIGO_*`` > TOML 配置文件 > 字段默认值。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field


class SynthesizerConfig(BaseModel):
    """后台日内编年合成器配置。"""

    model_config = ConfigDict(extra="forbid")

    batch_event_threshold: int = Field(default=8, ge=1, description="未整理事件累积达到该条数时触发一次合成")
    min_interval_ms: int = Field(default=900_000, ge=1_000, description="两次自动合成的最小间隔（毫秒）")
    recent_window_ms: int = Field(default=1_800_000, ge=60_000, description="未封存快层保留的最近动态窗口时长（毫秒）")
    topic_ttl_ms: int = Field(default=3 * 24 * 3600 * 1000, ge=60_000, description="鲜活话头默认有效期（毫秒）")
    auto_background: bool = Field(default=True, description="是否在应用启动后运行后台定时合成任务")
    poll_interval_ms: int = Field(default=30_000, ge=500, description="后台合成轮询检查周期（毫秒）")
    base_url: str = Field(default="", description="可选外部 OpenAI 兼容接口地址；留空走确定性规则合成")
    api_key: str = Field(default="", description="可选外部 LLM 接口密钥")
    model: str = Field(default="", description="可选外部 LLM 模型名称")
    timeout_ms: int = Field(default=15_000, ge=1_000, description="外部合成接口单次请求超时（毫秒）")
    stale_promise_days: int = Field(default=30, ge=1, description="未兑现承诺超过该天数自动降权为 stale")
    persona_name: str = Field(default="麦麦", min_length=1, description="第一人称编年合成使用的主播称呼")
    wake_hint_enabled: bool = Field(default=True, description="当日上午无记录时是否生成固定作息留白提示")


class EmbeddingConfig(BaseModel):
    """可选外部语义向量模型配置（留空则纯走 SQLite 稀疏混合检索）。"""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="", description="OpenAI 兼容 Embedding 接口根地址；留空禁用语义路")
    api_key: str = Field(default="", description="Embedding 接口密钥")
    model: str = Field(default="", description="Embedding 模型名称（如 text-embedding-3-small / bge-m3）")
    timeout_ms: int = Field(default=10_000, ge=500, description="单次向量化请求超时（毫秒）")
    auto_index: bool = Field(default=True, description="编年合成后是否自动为新经历/主线/锚点补齐向量")


class BackupConfig(BaseModel):
    """SQLite 在线热备份配置。"""

    model_config = ConfigDict(extra="forbid")

    dir: str = Field(default="data/backups", description="SQLite 热备份存放目录")
    keep_days: int = Field(default=7, ge=1, le=365, description="滚动保留的备份份数或天数上限")
    auto_daily: bool = Field(default=True, description="跨作息日切分时是否自动执行一次 VACUUM INTO 热备份")


class MaiGOConfig(BaseModel):
    """MaiGO 服务根配置。"""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="127.0.0.1", description="HTTP 与 MCP 服务监听地址")
    port: int = Field(default=8768, ge=1, le=65535, description="HTTP 与 MCP 服务监听端口")
    auth_token: str = Field(default="", description="Bearer 访问令牌；留空表示允许无鉴权本地调试")
    db_path: str = Field(default="data/maigo.sqlite3", description="SQLite 数据库文件路径")
    day_rollover_hour: int = Field(default=4, ge=0, le=23, description="作息切日小时（本地时间 0~23）")
    timezone_offset_hours: int = Field(default=8, ge=-12, le=14, description="本地时区相对 UTC 小时偏移")
    log_level: str = Field(default="INFO", description="日志级别")
    identity_salt: str = Field(default="maigo-default-salt", description="遗忘名单身份摘要盐值")
    dashboard_dist_dir: str = Field(default="dashboard/dist", description="前端看板构建产物目录")
    synthesizer: SynthesizerConfig = Field(default_factory=SynthesizerConfig, description="编年合成器配置")
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig, description="语义向量检索配置")
    backup: BackupConfig = Field(default_factory=BackupConfig, description="在线热备份配置")


def load_config(
    config_path: Optional[Path | str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> MaiGOConfig:
    """从 TOML 文件与环境变量加载配置，并保证覆盖顺序可预测。"""
    raw: dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path)
        if path.exists():
            raw = tomllib.loads(path.read_text(encoding="utf-8"))

    env_map = os.environ if env is None else env
    _apply_env_overrides(raw, env_map)
    return MaiGOConfig.model_validate(raw)


def _apply_section_env(
    section: dict[str, Any],
    env_map: Mapping[str, str],
    *,
    str_keys: Mapping[str, str] = (),  # type: ignore[assignment]
    int_keys: Mapping[str, str] = (),  # type: ignore[assignment]
    bool_keys: Mapping[str, str] = (),  # type: ignore[assignment]
) -> None:
    """按字段类型将扁平环境变量统一写入目标配置字典，消除重复分支。"""
    for env_key, field_name in dict(str_keys).items():
        if env_key in env_map and env_map[env_key] != "":
            section[field_name] = env_map[env_key]
    for env_key, field_name in dict(int_keys).items():
        if env_key in env_map and env_map[env_key].strip():
            section[field_name] = int(env_map[env_key])
    for env_key, field_name in dict(bool_keys).items():
        if env_key in env_map and env_map[env_key].strip():
            section[field_name] = env_map[env_key].strip().lower() in {"1", "true", "yes", "on"}


def _apply_env_overrides(raw: dict[str, Any], env_map: Mapping[str, str]) -> None:
    """将扁平环境变量映射到配置字典，避免调用方在运行前反复改配置文件。"""
    _apply_section_env(
        raw,
        env_map,
        str_keys={
            "MAIGO_HOST": "host",
            "MAIGO_AUTH_TOKEN": "auth_token",
            "MAIGO_DB_PATH": "db_path",
            "MAIGO_LOG_LEVEL": "log_level",
            "MAIGO_IDENTITY_SALT": "identity_salt",
            "MAIGO_DASHBOARD_DIST_DIR": "dashboard_dist_dir",
        },
        int_keys={
            "MAIGO_PORT": "port",
            "MAIGO_DAY_ROLLOVER_HOUR": "day_rollover_hour",
            "MAIGO_TIMEZONE_OFFSET_HOURS": "timezone_offset_hours",
        },
    )

    synth = dict(raw.get("synthesizer") or {})
    _apply_section_env(
        synth,
        env_map,
        str_keys={
            "MAIGO_LLM_BASE_URL": "base_url",
            "MAIGO_LLM_API_KEY": "api_key",
            "MAIGO_LLM_MODEL": "model",
            "MAIGO_SYNTH_PERSONA_NAME": "persona_name",
        },
        int_keys={
            "MAIGO_SYNTH_BATCH_THRESHOLD": "batch_event_threshold",
            "MAIGO_SYNTH_MIN_INTERVAL_MS": "min_interval_ms",
            "MAIGO_SYNTH_RECENT_WINDOW_MS": "recent_window_ms",
            "MAIGO_LLM_TIMEOUT_MS": "timeout_ms",
            "MAIGO_SYNTH_STALE_PROMISE_DAYS": "stale_promise_days",
        },
        bool_keys={
            "MAIGO_SYNTH_AUTO_BACKGROUND": "auto_background",
            "MAIGO_SYNTH_WAKE_HINT": "wake_hint_enabled",
        },
    )
    if synth:
        raw["synthesizer"] = synth

    emb = dict(raw.get("embedding") or {})
    _apply_section_env(
        emb,
        env_map,
        str_keys={
            "MAIGO_EMBEDDING_BASE_URL": "base_url",
            "MAIGO_EMBEDDING_API_KEY": "api_key",
            "MAIGO_EMBEDDING_MODEL": "model",
        },
        int_keys={"MAIGO_EMBEDDING_TIMEOUT_MS": "timeout_ms"},
        bool_keys={"MAIGO_EMBEDDING_AUTO_INDEX": "auto_index"},
    )
    if emb:
        raw["embedding"] = emb

    backup = dict(raw.get("backup") or {})
    _apply_section_env(
        backup,
        env_map,
        str_keys={"MAIGO_BACKUP_DIR": "dir"},
        int_keys={"MAIGO_BACKUP_KEEP_DAYS": "keep_days"},
        bool_keys={"MAIGO_BACKUP_AUTO_DAILY": "auto_daily"},
    )
    if backup:
        raw["backup"] = backup
