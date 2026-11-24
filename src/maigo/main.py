"""MaiGO 命令行启动入口。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

import uvicorn

from maigo.api import create_app
from maigo.config import load_config
from maigo.runtime import setup_logging


def main(argv: Optional[List[str]] = None) -> None:
    """解析命令行参数并运行 Uvicorn ASGI 服务。"""
    parser = argparse.ArgumentParser(description="MaiGO 24h 跨端记忆与生活流中心交换服务")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="配置文件路径")
    parser.add_argument("--host", type=str, default=None, help="覆盖监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.host:
        cfg = cfg.model_copy(update={"host": args.host})
    if args.port:
        cfg = cfg.model_copy(update={"port": args.port})

    setup_logging(cfg.log_level)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
