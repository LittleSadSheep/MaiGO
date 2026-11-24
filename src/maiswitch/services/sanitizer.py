"""模型投影出口脱敏与可见性过滤工具。

确保所有送入各端语言模型的上下文：
1. 严格遵守 ``visibility`` 分级（``private`` 绝不进入 ``public`` / ``group`` 视图）；
2. 彻底剥离原始平台账号 ID（QQ 号、B 站 UID、Minecraft UUID 与已知外部账号）。
"""

from __future__ import annotations

import re
from typing import Sequence, Tuple

VALID_VISIBILITIES: Tuple[str, ...] = ("public", "group", "private")

# 匹配常见的显式平台前缀账号与 UUID
_PLATFORM_ID_RE = re.compile(r"(?i)\b(?:qq|bilibili|bili|uid|gid|group|openid|mc|minecraft)\s*[:：#\-_/]?\s*\d{4,20}\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_CHINESE_PREFIX_ID_RE = re.compile(r"(?:QQ号?|群号?|账号|B站UID|UID)\s*[:：是为]?\s*\d{4,20}")
# 纯长数字串（6 位及以上常见外部账号）在面向模型的提示词里做兜底替换，保留 1-5 位常规年份/数量词
_LONG_NUMERIC_ID_RE = re.compile(r"(?<![\d.])\d{6,20}(?![\d.])")


def normalize_visibility(value: str, *, default: str = "public") -> str:
    """规范化可见性取值，未知值安全回落。"""
    cleaned = (value or "").strip().lower()
    if cleaned in VALID_VISIBILITIES:
        return cleaned
    return default


def allowed_visibilities(target_visibility: str) -> Tuple[str, ...]:
    """根据目标场合返回允许投影的事件/记忆可见性集合。

    - ``public``（公开直播 / 公开社媒）：放行 ``public`` 与脱敏后的 ``group`` 梗，严格拦截 ``private``；
    - ``group``（受控群聊）：放行 ``public`` 与 ``group``，严格拦截 ``private``；
    - ``private``（一对一私聊）：放行全部。
    """
    level = normalize_visibility(target_visibility, default="public")
    if level == "private":
        return ("public", "group", "private")
    return ("public", "group")


def sanitize_for_model(text: str, known_external_ids: Sequence[str] = ()) -> str:
    """剥离面向模型文本中的原始平台账号、群号与 UUID。

    触发时机：每次构造 ``stable_brief``、``live_context``、``actor_cards``、``person_directory``
    的提示文本以及 MCP 工具返回文本前统一调用。
    """
    if not text:
        return ""
    cleaned = _PLATFORM_ID_RE.sub("[账号已脱敏]", text)
    cleaned = _CHINESE_PREFIX_ID_RE.sub("[账号已脱敏]", cleaned)
    cleaned = _UUID_RE.sub("[玩家标识已脱敏]", cleaned)

    # 先按精确已知外部 ID 剥离（覆盖 5 位短测试号如 10001 / 20002 等）
    for ext_id in sorted({str(x).strip() for x in known_external_ids if str(x).strip()}, key=len, reverse=True):
        if len(ext_id) < 3:
            continue
        pattern = re.compile(rf"(?<![\w]){re.escape(ext_id)}(?![\w])")
        cleaned = pattern.sub("[账号已脱敏]", cleaned)

    # 再兜底剥离孤立的 6~20 位长数字账号串
    cleaned = _LONG_NUMERIC_ID_RE.sub("[账号已脱敏]", cleaned)
    return cleaned


def describe_visibility_scope(visibility: str, *, branch_id: str) -> str:
    """将来源分支与可见性渲染成不含原始群号的自然中文出处。"""
    if visibility == "private":
        return "私聊"
    if visibility == "group":
        return "群聊"
    branch = branch_id.strip().lower()
    if "bili" in branch:
        return "B站"
    if branch.startswith("x_") or branch == "x":
        return "X"
    if "amaidesu" in branch or "live" in branch:
        return "直播间"
    if "mc" in branch or "minecraft" in branch:
        return "Minecraft"
    return "公开场合"
