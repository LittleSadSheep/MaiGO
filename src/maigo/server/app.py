"""FastAPI 应用装配、生命周期管理与看板静态产物托管。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional, Tuple

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from maigo.config import MaiGOConfig
from maigo.container import MaiGOContext, build_context
from maigo.core import EmbeddingClient
from maigo.core.digest_rules import SynthesisLLMClient
from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.server.mcp import build_mcp_components
from maigo.server.routes import dashboard_router, public_router, sessions_router, sync_router

logger = get_logger("server.app")


def create_app(
    config: Optional[MaiGOConfig] = None,
    *,
    clock_ms: ClockMs = now_ms,
    llm_client: Optional[SynthesisLLMClient] = None,
    embedding_client: Optional[EmbeddingClient] = None,
) -> FastAPI:
    """创建并装配完整的 MaiGO FastAPI + MCP ASGI 应用。"""
    cfg = config or MaiGOConfig()
    ctx: MaiGOContext = build_context(
        cfg,
        clock_ms=clock_ms,
        llm_client=llm_client,
        embedding_client=embedding_client,
    )
    mcp_manager, mcp_endpoint = build_mcp_components(ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await ctx.db.initialize()
        ctx.bg_synthesizer.start()
        async with mcp_manager.run():
            logger.info(f"MaiGO 服务已启动: http://{cfg.host}:{cfg.port}")
            try:
                yield
            finally:
                await ctx.bg_synthesizer.stop()
                await ctx.db.close()
                logger.info("MaiGO 服务已安全停止")

    app = FastAPI(title="MaiGO", version="0.2.0", lifespan=lifespan)
    app.state.maigo_ctx = ctx

    app.include_router(public_router)
    app.include_router(sync_router)
    app.include_router(sessions_router)
    app.include_router(dashboard_router)

    # 注册精确路径与带尾斜杠路径，防止 307 重定向影响 MCP 客户端
    app.router.routes.append(Route("/mcp", endpoint=mcp_endpoint, methods=["GET", "POST", "DELETE"]))
    app.router.routes.append(Route("/mcp/", endpoint=mcp_endpoint, methods=["GET", "POST", "DELETE"]))

    _mount_dashboard_spa(app, Path(cfg.dashboard_dist_dir))
    return app


def _resolve_spa_target(resolved_dist: Path, index_file: Path, full_path: str) -> Tuple[str, Path]:
    """在工作线程中安全解析静态文件目标路径，防目录穿越且不阻塞事件循环。"""
    candidate = (resolved_dist / full_path).resolve()
    try:
        candidate.relative_to(resolved_dist)
    except ValueError:
        return "forbidden", index_file
    if candidate.is_file():
        return "file", candidate
    return "fallback", index_file


def _mount_dashboard_spa(app: FastAPI, dist_dir: Path) -> None:
    """当存在前端构建产物时托管看板静态文件并支持 SPA 回退。"""
    resolved_dist = dist_dir.resolve()
    index_file = resolved_dist / "index.html"
    if not index_file.is_file():
        return

    assets_dir = resolved_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="dashboard-assets")

    @app.get("/", include_in_schema=False)
    async def serve_root() -> FileResponse:
        return FileResponse(index_file)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa_fallback(full_path: str) -> object:
        # 绝不拦截 API 或 MCP 前缀的未命中路径
        if full_path.startswith("api/") or full_path == "api" or full_path.startswith("mcp"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        kind, target = await asyncio.to_thread(_resolve_spa_target, resolved_dist, index_file, full_path)
        if kind == "forbidden":
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        return FileResponse(target)
