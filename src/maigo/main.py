"""MaiGO 命令行启动与运维子命令入口。

支持：
- ``maigo [--config ...]`` 或 ``maigo serve ...``：启动 24h HTTP + MCP + 看板服务
- ``maigo doctor [--config ...]``：诊断 SQLite 完整性、表结构、模式与备份状态
- ``maigo backup [--config ...] [--out ...]``：生成一致性在线热备份
- ``maigo export-diary [--config ...] [--day YYYY-MM-DD] [--days N]``：导出生活手账 Markdown
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import List, Optional

import uvicorn

from maigo.api import create_app
from maigo.config import MaiGOConfig, load_config
from maigo.runtime import setup_logging
from maigo.services import HybridRecallService, IdentityService, MaintenanceService
from maigo.storage import MaiGODatabase


async def _run_doctor(cfg: MaiGOConfig) -> None:
    db = MaiGODatabase(cfg.db_path)
    await db.initialize()
    try:
        identity = IdentityService(db, identity_salt=cfg.identity_salt)
        retrieval = HybridRecallService(db, identity=identity, embedding_config=cfg.embedding)
        maint = MaintenanceService(db, config=cfg)
        synth_mode = "llm" if (cfg.synthesizer.base_url.strip() and cfg.synthesizer.model.strip()) else "rule"
        report = await maint.doctor_report(synthesizer_mode=synth_mode, retrieval_mode=retrieval.mode)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await db.close()


async def _run_backup(cfg: MaiGOConfig, out_path: Optional[Path]) -> None:
    db = MaiGODatabase(cfg.db_path)
    await db.initialize()
    try:
        maint = MaintenanceService(db, config=cfg)
        dest = await maint.create_backup(out_path=out_path)
        print(json.dumps({"ok": True, "backup_name": dest.name, "size_bytes": dest.stat().st_size}, ensure_ascii=False))
    finally:
        await db.close()


async def _run_export_diary(cfg: MaiGOConfig, *, day_key: Optional[str], days: int, visibility: str) -> None:
    db = MaiGODatabase(cfg.db_path)
    await db.initialize()
    try:
        maint = MaintenanceService(db, config=cfg)
        md = await maint.export_diary_markdown(day_key=day_key, days=days, target_visibility=visibility)
        print(md)
    finally:
        await db.close()


def main(argv: Optional[List[str]] = None) -> None:
    """解析命令行参数与子命令并运行相应入口。"""
    parser = argparse.ArgumentParser(description="MaiGO 24h 跨端记忆与生活流中心服务")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="配置文件路径")
    parser.add_argument("--host", type=str, default=None, help="覆盖监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")

    sub = parser.add_subparsers(dest="command")
    serve_p = sub.add_parser("serve", help="启动 HTTP + MCP + Web 看板服务（默认行为）")
    serve_p.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    serve_p.add_argument("--host", type=str, default=argparse.SUPPRESS)
    serve_p.add_argument("--port", type=int, default=argparse.SUPPRESS)

    doc_p = sub.add_parser("doctor", help="输出 SQLite 一致性、模式与备份诊断 JSON")
    doc_p.add_argument("--config", type=Path, default=argparse.SUPPRESS)

    bk_p = sub.add_parser("backup", help="执行一次 SQLite 在线热备份")
    bk_p.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    bk_p.add_argument("--out", type=Path, default=None, help="可选自定义备份文件路径")

    diary_p = sub.add_parser("export-diary", help="导出日内生活手账 Markdown")
    diary_p.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    diary_p.add_argument("--day", type=str, default=None, help="起始作息日 YYYY-MM-DD（默认今日）")
    diary_p.add_argument("--days", type=int, default=1, help="导出的连续天数")
    diary_p.add_argument("--visibility", type=str, default="public", choices=("public", "group", "private"))

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "host", None):
        cfg = cfg.model_copy(update={"host": args.host})
    if getattr(args, "port", None):
        cfg = cfg.model_copy(update={"port": args.port})

    cmd = args.command or "serve"
    if cmd == "doctor":
        asyncio.run(_run_doctor(cfg))
        return
    if cmd == "backup":
        asyncio.run(_run_backup(cfg, getattr(args, "out", None)))
        return
    if cmd == "export-diary":
        asyncio.run(
            _run_export_diary(
                cfg,
                day_key=getattr(args, "day", None),
                days=int(getattr(args, "days", 1)),
                visibility=str(getattr(args, "visibility", "public")),
            )
        )
        return

    setup_logging(cfg.log_level)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
