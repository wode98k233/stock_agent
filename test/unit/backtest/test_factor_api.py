"""因子 API 路由的轻量测试（不触发重计算）。

重点：模块可导入（路由注册靠 ``importlib`` 自动拉取，导入失败整个服务起不来）、
响应结构符合 ``{code, data, message}``、因子清单与注册表一致。
"""
from backtest.factor_lib import FACTOR_IDS


def test_list_endpoint_shape():
    from server.routes.factor import list_factors
    out = list_factors()
    assert set(out) == {"code", "data", "message"}
    assert out["code"] == 0
    assert out["data"]["count"] == len(FACTOR_IDS)
    first = out["data"]["factors"][0]
    assert {"factor_id", "name", "category", "description", "min_rows"} <= set(first)


def test_router_prefix():
    from server.routes.factor import router
    assert router.prefix == "/api/v1/factor"


def test_workspace_v3_endpoints_registered():
    """F1/F7：持久化 + 异步评估 + SSE 的端点必须齐。"""
    from server.routes.factor import router
    paths = {getattr(r, "path", "") for r in router.routes}
    for p in ("/run", "/runs/{run_uuid}/events", "/runs/{run_uuid}/cancel",
              "/reports", "/reports/{report_uuid}",
              "/reports/{report_uuid}/page"):
        assert f"/api/v1/factor{p}" in paths, f"缺少端点 {p}"


def test_cache_roundtrip():
    from server.routes.factor import _cache_key, _get_cached, _set_cached
    key = _cache_key(index_code="000300", start="2024-01-01")
    assert _get_cached(key) is None
    _set_cached(key, {"ranking": []})
    assert _get_cached(key) == {"ranking": []}


def test_resolve_factor_ids():
    from server.routes.factor import resolve_factor_ids
    assert resolve_factor_ids(None, None) is None
    assert resolve_factor_ids("rsi_14", None) == ["rsi_14"]
    assert resolve_factor_ids(None, "rsi_14, macd_dif") == ["rsi_14", "macd_dif"]
    # 单个 factor_id 优先于 factor_ids
    assert resolve_factor_ids("rsi_14", "macd_dif") == ["rsi_14"]


def test_resolve_factor_ids_rejects_unknown():
    from fastapi import HTTPException
    import pytest
    from server.routes.factor import resolve_factor_ids
    with pytest.raises(HTTPException) as ei:
        resolve_factor_ids(None, "rsi_14,bad_id")
    assert ei.value.status_code == 400
    assert "bad_id" in str(ei.value.detail)
