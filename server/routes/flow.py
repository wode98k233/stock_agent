"""资金流向 API

提供板块资金流向、个股资金流向、北向资金查询接口。
数据来源：同花顺问财（iwencai），通过 SkillRegister 调用。
缓存：market_cache.db cache_kv，TTL 1小时。
"""
import json
import logging
import re
from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(tags=['flow'])

_SKILL_NAME = "hithink_market_query"
_TOOL_NAME = "iwc_market_query"
_CACHE_PREFIX = "flow"
_CACHE_TTL = 1  # 小时


def _cache_get(key: str):
    from utils.cache.core import _get
    return _get(_CACHE_PREFIX, key)


def _cache_set(key: str, data):
    from utils.cache.core import _set
    _set(_CACHE_PREFIX, key, data, _CACHE_TTL)


def _query_iwencai(query: str, reg) -> list:
    """通过 SkillRegister 调同花顺问财工具，返回 datas 列表"""
    raw = reg.execute_tool(_SKILL_NAME, _TOOL_NAME, {"query": query})
    result = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(result["error"])
    data = result.get("data", {}) if isinstance(result, dict) else {}
    return data.get("datas", [])


def _pick(item: dict, *prefixes):
    """按前缀匹配列名，返回第一个匹配的值（处理带日期后缀的动态列名）"""
    for prefix in prefixes:
        for k, v in item.items():
            clean_k = re.sub(r'\[\d{8}\]$', '', k)
            if clean_k == prefix or k == prefix:
                return v if v is not None else ""
        for k, v in item.items():
            clean_k = re.sub(r'\[\d{8}\]$', '', k)
            if prefix in clean_k:
                return v if v is not None else ""
    return ""


def _pick_num(item: dict, *prefixes) -> float:
    """同 _pick，但返回 float"""
    v = _pick(item, *prefixes)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(',', ''))
    except (ValueError, TypeError):
        return 0.0


def _parse_items(items: list) -> list:
    """统一解析 iwencai 返回数据为前端格式"""
    result = []
    for item in items:
        result.append({
            "name": _pick(item, "指数简称", "板块名称", "行业名称"),
            "change_pct": _pick(item, "最新涨跌幅", "涨跌幅", "区间涨跌幅"),
            "net_inflow": _pick_num(item, "主力净买入额", "资金净流入额", "主力净流入", "主力资金净流入", "净流入"),
        })
    return result


@router.get("/api/flow/sector")
async def get_sector_flow(
    request: Request,
    limit: int = Query(30, ge=1, le=50, description="每方向返回条数"),
    indicator: str = Query("今日", description="时间维度：今日/5日/10日"),
):
    """板块资金流向排名（流入TOP + 流出TOP）"""
    from server.deps import get_web_state
    reg = get_web_state(request).agent_context.skill_register
    try:
        cache_key = f"sector:{indicator}:{limit}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return {"items": cached, "total": len(cached), "indicator": indicator, "cached": True}

        items_in = _query_iwencai(f"{indicator}行业板块主力资金净流入排名前{limit}", reg)
        items_out = _query_iwencai(f"{indicator}行业板块主力资金净流出排名前{limit}", reg)

        parsed_in = _parse_items(items_in)
        parsed_out = _parse_items(items_out)

        for it in parsed_in:
            it["direction"] = "inflow"
        for it in parsed_out:
            it["direction"] = "outflow"
            if it["net_inflow"] > 0:
                it["net_inflow"] = -it["net_inflow"]

        merged = parsed_in + parsed_out
        _cache_set(cache_key, merged)
        return {"items": merged, "total": len(merged), "indicator": indicator, "cached": False}
    except Exception as e:
        logger.error(f"获取板块资金流向失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取板块资金流向失败: {e}")


@router.get("/api/flow/stock/{code}")
async def get_stock_flow(code: str, request: Request):
    """个股资金流向详情"""
    from server.deps import get_web_state
    reg = get_web_state(request).agent_context.skill_register
    try:
        cache_key = f"stock:{code}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return {"item": cached, "code": code, "cached": True}

        items = _query_iwencai(f"{code}主力资金流向", reg)
        if not items:
            return {"item": None, "code": code}

        item = items[0]
        result = {
            "code": _pick(item, "股票代码") or code,
            "name": _pick(item, "股票简称"),
            "price": _pick(item, "最新价"),
            "change_pct": _pick(item, "最新涨跌幅", "涨跌幅"),
            "main_net": _pick_num(item, "主力资金流向", "主力净流入", "主力资金净流入"),
        }
        _cache_set(cache_key, result)
        return {"item": result, "code": code, "cached": False}
    except Exception as e:
        logger.error(f"获取个股资金流向失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取个股资金流向失败: {e}")


@router.get("/api/flow/north")
async def get_north_flow(
    request: Request,
    limit: int = Query(30, ge=1, le=50, description="返回条数"),
):
    """北向资金流入板块排名"""
    from server.deps import get_web_state
    reg = get_web_state(request).agent_context.skill_register
    try:
        cache_key = f"north:{limit}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return {"items": cached, "total": len(cached), "cached": True}

        items = _query_iwencai(f"北向资金净流入的板块排名前{limit}", reg)

        result = []
        for item in items:
            result.append({
                "name": _pick(item, "指数简称", "板块名称", "行业名称"),
                "north_net": _pick_num(item, "净买入额合计值", "北向资金净流入", "沪股通净流入", "净流入"),
            })
        _cache_set(cache_key, result)
        return {"items": result, "total": len(result), "cached": False}
    except Exception as e:
        logger.error(f"获取北向资金失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取北向资金失败: {e}")
