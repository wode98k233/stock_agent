"""盯盘告警 API：规则 CRUD + 告警历史 + 监控状态 + 测试通知。

路径前缀 /api/monitor/*（与系统性能监控 /api/system/* 无冲突）。
规则 CRUD 直接读写 monitor_alerts.db；状态/手动触发经 scheduler 单例。
"""
import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException

from server.schemas import (
    AddMonitorRuleRequest,
    MarkMonitorAlertsReadRequest,
    PatchMonitorRuleRequest,
)
from server.monitoring.quotes import SinaBatchQuotes
from server.monitoring.rules import RULE_TYPES, is_valid_rule_type
from server.monitoring.scheduler import get_scheduler
from server.monitoring.store import MonitorStore
from utils.notification import send_notification, notification_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["market-monitor"])

_CODE_RE = re.compile(r"^\d{6}$")

_store: MonitorStore | None = None
_quotes = SinaBatchQuotes()


def _get_store() -> MonitorStore:
    global _store
    if _store is None:
        _store = MonitorStore()
    return _store


def _fetch_quotes_sync(codes: list[str]) -> dict:
    """同步批量报价（被 to_thread 调用，不得直接在协程执行）。"""
    return _quotes.fetch(codes)


def _validate_code(code: str) -> str:
    if not _CODE_RE.match(code or ""):
        raise HTTPException(status_code=400, detail=f"无效的股票代码: {code}（需 6 位数字）")
    return code


# ── 规则 CRUD ────────────────────────────────────────────────────────────────

@router.get("/rules")
async def list_rules():
    """规则列表，附带现价与距触发距离（distance>0 = 距触发还有空间）。"""
    store = _get_store()
    rules = store.list_rules()
    codes = list({r["stock_code"] for r in rules})
    quotes = {}
    if codes:
        try:
            quotes = await asyncio.to_thread(_fetch_quotes_sync, codes)
        except Exception:
            logger.debug("[盯盘] 规则列表拉取现价失败", exc_info=True)
    items = []
    for r in rules:
        q = quotes.get(r["stock_code"])
        item = dict(r)
        item["price"] = q.get("price") if q else None
        item["pct"] = q.get("pct") if q else None
        item["distance"] = _distance(r, q)
        items.append(item)
    return {"items": items}


def _distance(rule: dict, quote: dict | None) -> float | None:
    """现价距规则触发的距离（>0 未触发、<=0 已越线）。"""
    if not quote:
        return None
    rt, th = rule["rule_type"], rule["threshold"]
    price, pct = quote.get("price"), quote.get("pct")
    if rt in ("price_above", "price_below"):
        if price is None:
            return None
        return th - price if rt == "price_above" else price - th
    if pct is None:
        return None
    if rt == "pct_above":
        return th - pct
    if rt == "pct_below":
        return pct - th
    if rt == "pct_change":
        return th - abs(pct)
    return None


@router.post("/rules")
async def add_rule(payload: AddMonitorRuleRequest):
    if not is_valid_rule_type(payload.rule_type):
        raise HTTPException(
            status_code=400,
            detail=f"无效规则类型: {payload.rule_type}（可选: {', '.join(RULE_TYPES)}）")
    _validate_code(payload.stock_code)
    store = _get_store()
    rule_id = store.add_rule(
        stock_code=payload.stock_code,
        stock_name=payload.stock_name or None,
        rule_type=payload.rule_type,
        threshold=payload.threshold,
        once_only=payload.once_only,
        cooldown_s=payload.cooldown_s,
        remark=payload.remark or None,
        source="manual",
    )
    rules = {r["id"]: r for r in store.list_rules()}
    return rules[rule_id]


@router.patch("/rules/{rule_id}")
async def patch_rule(rule_id: int, payload: PatchMonitorRuleRequest):
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="无可更新字段")
    if not _get_store().update_rule(rule_id, **fields):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"success": True}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    if not _get_store().delete_rule(rule_id):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"success": True}


# ── 告警历史 ─────────────────────────────────────────────────────────────────

@router.get("/alerts")
async def list_alerts(page: int = 1, size: int = 20, unread: bool = False):
    return _get_store().list_alerts(page=page, size=max(1, min(size, 100)),
                                    unread=unread)


@router.post("/alerts/read")
async def mark_alerts_read(payload: MarkMonitorAlertsReadRequest):
    store = _get_store()
    if payload.all:
        # 全部已读：ids 传空列表实现
        unread = store.list_alerts(page=1, size=100, unread=True)
        ids = [a["id"] for a in unread["items"]]
        total = unread["total"]
        # 分页兜底循环（>100 条时）
        while len(ids) < total:
            page = len(ids) // 100 + 1
            batch = store.list_alerts(page=page, size=100, unread=True)
            ids.extend(a["id"] for a in batch["items"])
            if not batch["items"]:
                break
        updated = store.mark_read(ids)
    else:
        if not payload.ids:
            raise HTTPException(status_code=400, detail="ids 不能为空（或使用 all=true）")
        updated = store.mark_read(payload.ids)
    return {"updated": updated}


@router.delete("/alerts")
async def clear_alerts():
    """清空告警历史（不删规则；历史会无限累积）。"""
    return {"deleted": _get_store().clear_alerts()}


# ── 状态与测试 ───────────────────────────────────────────────────────────────

@router.get("/status")
async def monitor_status():
    from config import Config
    scheduler = get_scheduler()
    status = scheduler.get_status() if scheduler else {
        "last_poll_at": None, "last_duration_s": None, "enabled_rules": 0,
        "fail_streak": 0, "watchlist_count": 0,
    }
    status.update({
        "running": scheduler is not None,
        "interval_s": Config.MARKET_MONITOR_INTERVAL_S,
        "notification_enabled": notification_enabled(),
    })
    return status


@router.post("/test")
async def send_test_notification():
    """发送测试通知，引导用户确认渠道配置。"""
    try:
        results = send_notification(
            "盯盘测试通知", "这是一条测试通知，收到即说明盯盘告警渠道畅通。",
            report_type="alert")
        ok = [r.channel for r in results if getattr(r, "success", False)]
        return {"success": True, "channels_ok": ok,
                "notification_enabled": notification_enabled()}
    except Exception as e:
        return {"success": False, "error": str(e),
                "notification_enabled": notification_enabled()}
