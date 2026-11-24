"""服务端 HTTP 路由聚合导出。"""

from maigo.server.routes.dashboard import router as dashboard_router
from maigo.server.routes.sessions import router as sessions_router
from maigo.server.routes.sync import public_router, router as sync_router

__all__ = [
    "dashboard_router",
    "public_router",
    "sessions_router",
    "sync_router",
]
