"""阶段 4 可视化看板构建产物与 FastAPI SPA 静态托管测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from maigo.config import MaiGOConfig, SynthesizerConfig
from maigo.server import create_app

_DIST_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "dist"


async def test_dashboard_dist_spa_serving_and_api_passthrough(tmp_path: Path) -> None:
    has_index = await asyncio.to_thread((_DIST_DIR / "index.html").is_file)
    assert has_index, "请先在 dashboard/ 目录运行 pnpm run build"

    cfg = MaiGOConfig(
        db_path=str(tmp_path / "spa.sqlite3"),
        dashboard_dist_dir=str(_DIST_DIR),
        synthesizer=SynthesizerConfig(auto_background=False),
    )
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_root = await client.get("/")
            assert r_root.status_code == 200
            assert "MaiGO" in r_root.text

            r_spa = await client.get("/persons")
            assert r_spa.status_code == 200
            assert "MaiGO" in r_spa.text

            # 未知 API 路径仍返回 404 JSON，不被 SPA 回退劫持
            r_api_404 = await client.get("/api/v1/nonexistent")
            assert r_api_404.status_code == 404
            assert r_api_404.json()["detail"] == "Not Found"
