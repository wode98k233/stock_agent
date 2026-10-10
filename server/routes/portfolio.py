"""持仓账本 API：持仓/汇总/流水/记账/信号联动。

数据源策略（设计文档 §5.1，2026-09-06 用户确认）：
- 现价走 SinaBatchQuotes（方向A 已建通道，批量 0.14s 级）——fetcher 的
  ak_spot_em 全市场快照经重试链实测 45-50s，面板场景不可接受
- 信号联动只读本地 signal_eval.db，无网络请求

行情合并走 to_thread（同步 requests 不得冻结 event loop）。
"""
import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request

from server.deps import get_web_state
from server.schemas import (
    EastmoneyImportRequest,
    EastmoneyPreviewRequest,
    PortfolioFromSignalRequest,
    PortfolioTradeRequest,
)
from server.services.portfolio_view import (
    EMPTY_SUMMARY,
    build_positions_view,
    fetch_quotes_sync as _fetch_quotes_sync,
)
from server.storage import PortfolioError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

_CODE_RE = re.compile(r"^\d{6}$")


def _get_portfolio(request: Request | None = None):
    """获取 WebStorage 的 PortfolioStorage（测试可 monkeypatch）。"""
    web = get_web_state(request)
    return web.storage.portfolio


async def _fetch_quotes(codes: list[str], source: str = "sina") -> dict:
    """行情拉取：失败降级为空 dict（成本侧仍可展示）。"""
    if not codes:
        return {}
    try:
        return await asyncio.to_thread(_fetch_quotes_sync, codes, source)
    except Exception:
        logger.debug("[持仓] 行情拉取失败，降级为成本侧展示", exc_info=True)
        return {}


def _load_signal_row(signal_id: int) -> dict | None:
    """只读 signal_eval.db 的 signal 行（测试可 monkeypatch）。"""
    from signal_eval.db import get_conn
    row = get_conn().execute(
        "SELECT id, decision, symbol_key, symbol_name, dialog_uuid, signal_time"
        " FROM signal WHERE id = ?", (signal_id,)).fetchone()
    return dict(row) if row else None


# ── 持仓 / 汇总 ──────────────────────────────────────────────────────────────

@router.get("/positions")
async def list_positions(request: Request, source: str = "sina"):
    """持仓列表 + 实时行情合并（实现见 server/services/portfolio_view.py）。

    source 指定行情数据源：sina/auto（默认，新浪批量快通道）/ 其它数据源名
    （走全市场快照过滤，较慢）。返回附 quote.source/quote.time 供前端展示
    「某来源 · 刷新时间」，避免用户误以为行情未更新。
    """
    portfolio = _get_portfolio(request)
    return await asyncio.to_thread(
        build_positions_view, portfolio, source or "sina", _fetch_quotes_sync)


@router.get("/summary")
async def get_summary(request: Request):
    """组合摘要（不拉行情：成本侧 + 已实现盈亏）。"""
    portfolio = _get_portfolio(request)
    return await asyncio.to_thread(portfolio.position_summary, None)


@router.get("/codes")
async def list_position_codes(request: Request):
    """持仓代码列表（仅供市场面板高亮，不拉行情、不返回数量成本）。"""
    portfolio = _get_portfolio(request)
    positions = await asyncio.to_thread(portfolio.list_positions)
    codes = sorted({p["stock_code"] for p in positions if (p.get("qty") or 0) > 0})
    return {"items": codes}


# ── 流水 / 记账 ──────────────────────────────────────────────────────────────

@router.get("/trades")
async def list_trades(request: Request, stock_code: str | None = None,
                      page: int = 1, size: int = 50):
    portfolio = _get_portfolio(request)
    return await asyncio.to_thread(
        portfolio.list_trades, stock_code, "cn", page, max(1, min(size, 200)))


@router.post("/trade")
async def add_trade(request: Request, payload: PortfolioTradeRequest):
    if not _CODE_RE.match(payload.stock_code or ""):
        raise HTTPException(status_code=400, detail=f"无效股票代码: {payload.stock_code}（需 6 位数字）")
    portfolio = _get_portfolio(request)
    try:
        result = await asyncio.to_thread(
            portfolio.add_trade, payload.stock_code, payload.side, payload.qty,
            payload.price, payload.trade_date,
            stock_name=payload.stock_name or None, market=payload.market,
            fee=payload.fee, source="manual", remark=payload.remark or None)
    except PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await asyncio.to_thread(_sync_profile_holdings, portfolio)
    return result


@router.delete("/trade/{trade_uuid}")
async def remove_trade(request: Request, trade_uuid: str):
    portfolio = _get_portfolio(request)
    try:
        ok = await asyncio.to_thread(portfolio.remove_trade, trade_uuid)
    except PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="流水不存在")
    await asyncio.to_thread(_sync_profile_holdings, portfolio)
    return {"success": True}


# ── 信号联动 ─────────────────────────────────────────────────────────────────

@router.post("/from-signal")
async def trade_from_signal(request: Request, payload: PortfolioFromSignalRequest):
    """signal_eval 信号一键记入：查 signal 表 → 落流水 source='signal_eval'。

    幂等：同 signal_id 已记入过 → 409。
    """
    signal = _load_signal_row(payload.signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail=f"信号 {payload.signal_id} 不存在")
    if signal["decision"] not in ("buy", "sell"):
        raise HTTPException(status_code=400,
                            detail=f"信号决策为 {signal['decision']}，无记账意义")
    if not payload.price or payload.price <= 0:
        raise HTTPException(status_code=400,
                            detail="缺少成交价：请提供 price（前端预填信号 entry_price）")

    portfolio = _get_portfolio(request)
    if await asyncio.to_thread(_signal_already_recorded, portfolio,
                               payload.signal_id):
        raise HTTPException(status_code=409,
                            detail=f"信号 {payload.signal_id} 已记入持仓，勿重复记账")

    symbol_key = signal.get("symbol_key") or ""
    stock_code = symbol_key[2:] if symbol_key[:2] in ("sh", "sz") else symbol_key
    try:
        result = await asyncio.to_thread(
            portfolio.add_trade, stock_code, signal["decision"], payload.qty,
            payload.price, (signal.get("signal_time") or "")[:10],
            stock_name=signal.get("symbol_name"),
            source="signal_eval", signal_id=payload.signal_id,
            dialog_uuid=signal.get("dialog_uuid"))
    except PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await asyncio.to_thread(_sync_profile_holdings, portfolio)
    return result


def _signal_already_recorded(portfolio, signal_id: int) -> bool:
    """同 signal_id 是否已记入（幂等检查，线程池执行）。"""
    trades = portfolio.list_trades(size=1000)["items"]
    return any(t.get("signal_id") == signal_id
               and t.get("source") == "signal_eval" for t in trades)


# ── 持仓上下文（Phase 3 agent 注入的调试入口） ───────────────────────────────

@router.post("/position-context")
async def position_context(request: Request):
    """返回将注入 agent 的持仓上下文文本（调试用；Phase 3 复用同一构造逻辑）。"""
    portfolio = _get_portfolio(request)
    positions = await asyncio.to_thread(portfolio.list_positions)
    open_positions = [p for p in positions if p["qty"] > 0]
    if not open_positions:
        return {"text": "", "has_positions": False}
    quotes = await _fetch_quotes([p["stock_code"] for p in open_positions])
    return {"text": build_position_context(open_positions, quotes),
            "has_positions": True}


def build_position_context(positions: list[dict], quotes: dict | None) -> str:
    """构造注入 user message 的持仓块（无 IO，纯函数）。

    行情缺失时省略浮盈亏（AI 只看成本侧）。
    """
    lines = ["[用户持仓账本]"]
    for p in positions:
        name = p.get("stock_name") or p["stock_code"]
        line = (f"{name}({p['stock_code']}) 成本{p['avg_cost']:.2f}×{p['qty']:g}股")
        q = (quotes or {}).get(p["stock_code"])
        if q and q.get("price") and p["avg_cost"]:
            pnl_pct = (q["price"] - p["avg_cost"]) / p["avg_cost"] * 100
            line += f" 浮动{pnl_pct:+.1f}%"
        lines.append(line)
    return "\n".join(lines)


def _sync_profile_holdings(portfolio) -> None:
    """持仓写操作成功后回写用户画像 holdings（与账本打通）。

    覆盖式同步 open positions → 画像；任何异常全吞，绝不影响记账主链路。
    """
    try:
        positions = portfolio.list_positions()
        from memory.user_profile import Holding, UserProfileManager
        holdings = [
            Holding(stock_code=p["stock_code"],
                    stock_name=p.get("stock_name") or p["stock_code"],
                    cost=p.get("avg_cost") or 0.0,
                    shares=int(p.get("qty") or 0))
            for p in positions if (p.get("qty") or 0) > 0
        ]
        UserProfileManager(user_id="default").replace_holdings(holdings)
    except Exception:
        logger.debug("[Portfolio] 画像持仓同步失败（忽略）", exc_info=True)


# ── 东财持仓同步（docs/eastmoney持仓同步-设计方案.md v3） ───────────────────

def _em_ok(data) -> dict:
    """统一 {code, data, msg} 壳。"""
    return {"code": 0, "data": data, "msg": "ok"}


def _em_err(msg: str) -> dict:
    return {"code": 1, "data": None, "msg": msg}


@router.post("/eastmoney/preview")
async def eastmoney_preview(request: Request, payload: EastmoneyPreviewRequest):
    """粘贴报文 → 解析+拉取+normalize+diff → 预览（**不入库**）。

    三格式自动识别：PowerShell / fetch（凭据拉取）；JSON 直通（直接入库模式）。
    凭据仅在本请求内存中存活，不落库不写日志。
    """
    from server.services import eastmoney_sync as em
    try:
        parsed = await asyncio.to_thread(em.parse_paste, payload.paste_text)
    except em.EmsyncError as e:
        return _em_err(str(e))

    account_meta = dict(em.get_account_cache() or {})
    if isinstance(parsed, em.DirectSnapshot):
        snap = parsed.snapshot
        snap.mode = "direct"
        diff = await asyncio.to_thread(
            em.diff_snapshot, snap.positions,
            await asyncio.to_thread(_get_portfolio(request).list_positions))
    else:
        # session 模式：内存会话随响应一起消亡（设计：预览→确认 ≤5 分钟窗口，
        # 前端把 preview 拿到的 snapshot+diff 原样回传 /import，凭据不二次往返）
        try:
            resp = await asyncio.to_thread(em.fetch_positions, parsed)
        except em.EmsyncError as e:
            return _em_err(str(e))
        snap = em.normalize(resp)
        diff = await asyncio.to_thread(
            em.diff_snapshot, snap.positions,
            await asyncio.to_thread(_get_portfolio(request).list_positions))
        if snap.account.get("total_asset") is not None:
            # 记账户资金缓存（无凭据，仅 Zzc/Kyzj 等金额，供 /session 与 skill 展示）
            snap.account["synced_at"] = snap.account.get("synced_at") or None
            await asyncio.to_thread(em.set_account_cache, snap.account)
            account_meta = dict(em.get_account_cache() or {})

    return _em_ok({
        "snapshot": {
            "mode": snap.mode,
            "account": snap.account,
            "positions": snap.positions,
            "skipped_rows": snap.skipped_rows,
        },
        "diff": diff,
        "account_cache": account_meta,
    })


@router.post("/eastmoney/import")
async def eastmoney_import(request: Request, payload: EastmoneyImportRequest):
    """确认导入：覆盖式落库（source='eastmoney' 行对齐快照，幂等）。

    snapshot 为 /eastmoney/preview 返回的 data.snapshot 原样回传（前端透传）；
    本端点不再做网络请求、不再接触凭据。
    """
    from server.services import eastmoney_sync as em
    snap = payload.snapshot or {}
    positions = snap.get("positions") or []
    if not positions and not snap.get("account"):
        return _em_err("快照为空：请先在预览步骤解析成功后再导入")

    # diff 由服务端重算（不信任前端回传的 diff）
    portfolio = _get_portfolio(request)
    existing = await asyncio.to_thread(portfolio.list_positions)
    diff = await asyncio.to_thread(em.diff_snapshot, positions, existing)

    try:
        result = await asyncio.to_thread(
            portfolio.import_eastmoney_snapshot,
            positions, diff["removed"])
    except Exception as e:
        logger.error("[东财同步] 落库失败", exc_info=True)
        return _em_err(f"落库失败：{e}")

    # unchanged 判据来自服务端 diff（数值一致 = 零差异零写入语义）；
    # added/updated 按快照行在导入前是否存在对齐（removed/unchanged 取 diff）。
    existing_codes = {r["stock_code"] for r in existing}
    counts = {
        "added": len([p for p in positions
                      if p["stock_code"] not in existing_codes]),
        "updated": len([p for p in positions
                        if p["stock_code"] in existing_codes
                        and not em._row_matches(
                            p, next(r for r in existing
                                    if r["stock_code"] == p["stock_code"]))]),
        "removed": diff["removed"].__len__(),
        "unchanged": diff["unchanged"],
    }
    # 账户资金缓存（直通模式 preview 未写缓存的场景）
    account = snap.get("account") or {}
    if account.get("total_asset") is not None:
        account = dict(account)
        account.setdefault("synced_at", None)
        await asyncio.to_thread(em.set_account_cache, account)
    await asyncio.to_thread(_sync_profile_holdings, portfolio)
    return _em_ok(counts)


@router.get("/eastmoney/session")
async def eastmoney_session(request: Request):
    """上次同步信息：时间/来源计数（从表里读，无凭据）+ 账户资金缓存。"""
    from server.services import eastmoney_sync as em
    portfolio = _get_portfolio(request)
    meta = await asyncio.to_thread(portfolio.eastmoney_sync_meta)
    meta["account"] = em.get_account_cache()
    return _em_ok(meta)
