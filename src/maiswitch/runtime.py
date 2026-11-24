"""统一时间与日志辅助工具。

全站时间戳一律使用 Unix epoch 毫秒整数，并支持在测试中注入可替换时钟。
"""

from __future__ import annotations

import logging
import time
from typing import Callable

ClockMs = Callable[[], int]


def now_ms() -> int:
    """返回当前 Unix epoch 毫秒时间戳。"""
    return int(time.time() * 1000)


def get_logger(name: str) -> logging.Logger:
    """获取带模块命名空间的日志记录器。"""
    return logging.getLogger(f"maiswitch.{name}")


def setup_logging(level: str = "INFO") -> None:
    """初始化全局日志输出格式与级别（幂等配置基础 handler）。"""
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger("maiswitch")
    root.setLevel(numeric)
    if not root.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
