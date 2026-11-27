"""同端口 Streamable HTTP MCP 服务端（挂载于 ``/mcp``）。

暴露给各接入端规划器使用的跨端记忆检索与约定销账工具：
- ``maigo_recall_life``：按关键词检索跨端经历与锚点（支持可见性裁剪）
- ``maigo_lookup_person``：按平台与外部 ID 查询已绑定熟人（脱敏、绝不泄露原账号）
- ``maigo_resolve_promise``：标记某条跨端约定已兑现或作废
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from maigo.container import MaiGOContext
from maigo.core.privacy import normalize_visibility
from maigo.runtime import get_logger
from maigo.server.auth import verify_token_value

logger = get_logger("server.mcp")


def _tool_definitions() -> List[types.Tool]:
    """声明暴露给规划器的三个只读/销账 MCP 工具。"""
    return [
        types.Tool(
            name="maigo_recall_life",
            description="检索麦麦在各个分支（QQ群、B站、X、直播、游戏）的经历、原件书库细节、实体关系与立场约定。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要回想的关键词或话题"},
                    "days": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 0,
                        "maximum": 365,
                        "description": "往前检索几天，默认 30（0 表示不限天数翻查全部历史）",
                    },
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    "target_visibility": {
                        "type": "string",
                        "enum": ["public", "group", "private"],
                        "default": "public",
                    },
                    "branch_id": {
                        "type": "string",
                        "description": "调用方分支标识，用于降低近期同端群聊回声权重",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="maigo_lookup_person",
            description="查询某个平台外部账号对应的已绑定熟人与近期跨端互动摘要（已脱敏，不含私聊与原账号）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "description": "平台名，如 qq / bilibili / minecraft"},
                    "external_id": {"type": "string", "description": "该平台下的外部账号或 UUID"},
                    "target_visibility": {
                        "type": "string",
                        "enum": ["public", "group", "private"],
                        "default": "public",
                    },
                },
                "required": ["platform", "external_id"],
            },
        ),
        types.Tool(
            name="maigo_resolve_promise",
            description="更新某条跨端约定（如约#N）的状态：兑现关闭（closed）或判误记作废（void）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "anchor_id": {"type": "integer", "description": "约定编号（即约#后的整数）"},
                    "status": {"type": "string", "enum": ["closed", "void"]},
                    "evidence": {"type": "string", "default": "", "description": "兑现或作废的说明依据"},
                },
                "required": ["anchor_id", "status"],
            },
        ),
    ]


class AuthorizedMcpEndpoint:
    """带 Bearer 鉴权的 ASGI 入口适配器，直接托管至 FastAPI ``/mcp`` 路由。"""

    def __init__(self, session_manager: StreamableHTTPSessionManager, *, auth_token: str) -> None:
        self._asgi_app = StreamableHTTPASGIApp(session_manager)
        self._auth_token = auth_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not verify_token_value(self._auth_token, authorization=headers.get("authorization")):
            await Response(status_code=401, content="Unauthorized")(scope, receive, send)
            return
        await self._asgi_app(scope, receive, send)


def build_mcp_components(ctx: MaiGOContext) -> tuple[StreamableHTTPSessionManager, AuthorizedMcpEndpoint]:
    """构建无状态 JSON 响应模式的 Streamable HTTP MCP 管理器与路由入口。"""

    async def handle_list_tools(
        _req_ctx: ServerRequestContext[Any],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_definitions())

    async def handle_call_tool(
        _req_ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        name = params.name
        args: Dict[str, Any] = dict(params.arguments or {})
        try:
            text, is_error = await _dispatch_tool(ctx, name=name, args=args)
        except Exception as exc:
            logger.error(f"执行 MCP 工具失败 ({name}): {exc}", exc_info=True)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"工具执行出错: {exc}")],
                is_error=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            is_error=is_error,
        )

    server: Server[Any] = Server(
        "maigo",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=security,
    )
    endpoint = AuthorizedMcpEndpoint(manager, auth_token=ctx.config.auth_token)
    return manager, endpoint


async def _dispatch_tool(ctx: MaiGOContext, *, name: str, args: Dict[str, Any]) -> tuple[str, bool]:
    if name == "maigo_recall_life":
        query = str(args.get("query") or "").strip()
        if not query:
            return "请提供要检索的 query 关键词。", True
        raw_days = args.get("days")
        days = int(raw_days) if raw_days is not None else 30
        raw_limit = args.get("limit")
        limit = int(raw_limit) if raw_limit is not None else 5
        vis = normalize_visibility(str(args.get("target_visibility") or "public"), default="public")
        branch_id = str(args.get("branch_id") or "").strip() or None
        text = await ctx.projector.recall_life(
            query=query,
            days=days,
            limit=limit,
            target_visibility=vis,
            exclude_branch=branch_id,
        )
        return text, False

    if name == "maigo_lookup_person":
        platform = str(args.get("platform") or "").strip()
        external_id = str(args.get("external_id") or "").strip()
        if not platform or not external_id:
            return "platform 与 external_id 均不能为空。", True
        vis = normalize_visibility(str(args.get("target_visibility") or "public"), default="public")
        cards = await ctx.db.run(
            lambda c: ctx.identity.build_actor_cards_sync(c, actors=[(platform, external_id)], target_visibility=vis)
        )
        if not cards:
            return "未查到该账号对应的已绑定跨端熟人（可能尚未绑定或已启用遗忘保护）。", False
        return cards[0].hint, False

    if name == "maigo_resolve_promise":
        try:
            anchor_id = int(args.get("anchor_id") or 0)
        except (TypeError, ValueError):
            return "anchor_id 必须是整数。", True
        status = str(args.get("status") or "").strip()
        if status not in ("closed", "void"):
            return "status 只能为 closed 或 void。", True
        evidence = str(args.get("evidence") or "").strip()

        def _update(conn: sqlite3.Connection) -> bool:
            existing = ctx.anchors_repo.get_by_id(conn, anchor_id)
            if existing is None or existing.anchor_type != "promise":
                return False
            note = evidence or existing.note
            return ctx.anchors_repo.update(conn, anchor_id, status=status, note=note) is not None

        ok = await ctx.db.run(_update)
        if not ok:
            return f"未找到编号为 {anchor_id} 的跨端约定。", True
        ctx.broker.publish("brief_updated", {"ts_ms": int(ctx.clock_ms())})
        verb = "已标记兑现" if status == "closed" else "已标记作废"
        return f"约#{anchor_id} {verb}。", False

    return f"未知工具: {name}", True
