"""HTTP 与 MCP 共用的 Bearer 鉴权及请求上下文依赖。"""

from __future__ import annotations

import secrets
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, Query, Request

from maigo.container import MaiGOContext


def get_ctx(request: Request) -> MaiGOContext:
    """从 FastAPI app.state 获取已装配的服务容器。"""
    return request.app.state.maigo_ctx


CtxDep = Annotated[MaiGOContext, Depends(get_ctx)]


def verify_token_value(expected_token: str, *, authorization: Optional[str], query_token: Optional[str] = None) -> bool:
    """恒定时间比较 Bearer 令牌；配置为空串时放行。"""
    clean_expected = expected_token.strip()
    if not clean_expected:
        return True
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            presented = authorization[len(prefix) :].strip()
            return secrets.compare_digest(presented, clean_expected)
    if query_token:
        return secrets.compare_digest(query_token.strip(), clean_expected)
    return False


async def require_auth(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> None:
    """路由鉴权依赖：令牌非空时拦截未授权请求。"""
    ctx = get_ctx(request)
    if not verify_token_value(ctx.config.auth_token, authorization=authorization, query_token=token):
        raise HTTPException(status_code=401, detail="无效或缺失的 Bearer 访问令牌")
