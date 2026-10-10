"""AccessLogMiddleware 回归：客户端断开（call_next 抛 RuntimeError）时必须返回真实 Response。

事故（2026-09-23）：RuntimeError 分支只设 status_code=499、response 仍为 None，
`return response` 返回 None → 外层 BaseHTTPMiddleware `await response(...)` 报
"TypeError: 'NoneType' object is not callable"。
"""
import asyncio

from starlette.requests import Request
from starlette.responses import Response

from server.middleware import AccessLogMiddleware


def _req(path="/api/stock/query"):
    scope = {"type": "http", "method": "GET", "path": path,
             "query_string": b"", "headers": []}
    return Request(scope)


def test_returns_call_next_response():
    async def call_next(request):
        return Response("ok", status_code=200)

    resp = asyncio.run(AccessLogMiddleware(app=None).dispatch(_req(), call_next))
    assert resp is not None and resp.status_code == 200


def test_client_disconnect_returns_499_response_not_none():
    async def call_next(request):
        raise RuntimeError("No response returned.")

    resp = asyncio.run(AccessLogMiddleware(app=None).dispatch(_req(), call_next))
    assert resp is not None, "断开时不得返回 None（否则 ASGI 层 NullType 崩溃）"
    assert resp.status_code == 499
