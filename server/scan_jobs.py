"""四个巡检作业的业务实现（框架在 `server/background_scans.py`）。

每个作业的返回值约定：dict，其中 `summary` 是一句人能读的话（直接给前端用），
其余标量键会作为「小指标」进状态页。**返回大对象没有意义**——状态只存内存。

作业内部一律自己吞掉可预期的局部失败（单个台账定义坏了、单个资产读不了），
只把「整体失败」留给框架记 `last_ok=False`。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _mb(size_bytes: float) -> str:
    mb = (size_bytes or 0) / 1048576
    return f"{mb:.1f} MB" if mb < 1024 else f"{mb / 1024:.2f} GB"


# ── ① 存储监控扫描 ───────────────────────────────────────────────

def storage_scan() -> dict:
    """跑一遍数据资产扫描（大小 / 行数 / 表级明细）并落快照。

    注意：这是**纯本地**扫描（文件大小 + SQLite COUNT），不联网；496 个板块那种
    限流问题不在这条链路上。单资产超时/权限失败会被 scanner 降级成 error 字段。
    """
    from utils import data_monitor as dbm
    report = dbm.scan()
    assets = report.get("assets") or []
    size = sum((a.get("size_bytes") or 0) for a in assets)
    records = sum((a.get("record_count") or 0) for a in assets)
    errors = [a for a in assets if a.get("error")]
    summary = (f"扫描 {len(assets)} 个数据资产 · {_mb(size)} · 共 {records:,} 行"
               + (f" · {len(errors)} 个读取失败" if errors else " · 全部可读"))
    return {
        "summary": summary,
        "assets": len(assets),
        "size_mb": round(size / 1048576, 2),
        "records": records,
        "errors": len(errors),
        "scanned_at": report.get("scanned_at"),
    }


# ── ② 研究台账刷新 ───────────────────────────────────────────────

def thesis_refresh() -> dict:
    """刷新**全部**台账定义。单个台账失败不影响其它台账（逐个 try）。"""
    from server import thesis_service
    from thesis import defs as thesis_defs

    ids = thesis_defs.list_defs()
    new_points = 0
    ok_items, failures = [], []
    for tid in ids:
        try:
            res = thesis_service.refresh(tid)
            snap = res.get("snapshot") or {}
            ok_items.append({
                "thesis_id": tid,
                "trade_date": snap.get("trade_date"),
                "total": snap.get("total"),
                "verdict": snap.get("verdict"),
                "alerts": res.get("new_alert_count", 0),
                "created": bool(res.get("snapshot_created")),
            })
            if res.get("snapshot_created"):
                new_points += 1
        except Exception as e:  # noqa: BLE001 —— 坏定义不该拖垮整轮
            failures.append(f"{tid}: {e}")
            logger.warning("[Scans] 台账 %s 刷新失败: %s", tid, e)

    alerts = sum(it["alerts"] for it in ok_items)
    summary = (f"刷新 {len(ok_items)}/{len(ids)} 个台账 · 新历史点 {new_points}"
               f" · 新增告警 {alerts}")
    if failures:
        summary += f" · {len(failures)} 个失败"
    out = {"summary": summary, "theses": len(ids), "refreshed": len(ok_items),
           "new_points": new_points, "alerts": alerts, "failed": len(failures),
           "items": ok_items}
    if failures:
        out["failures"] = failures[:5]
    return out


# ── ③ 全市场拥挤度扫描 ───────────────────────────────────────────

def crowding_scan() -> dict:
    """标的池 → 算分 → 按需轮转补数 → 落库。"""
    from crowding.engine import CrowdingEngine
    from crowding.storage import CrowdingStorage

    report = CrowdingEngine().scan()
    saved = CrowdingStorage.instance().save_scan(report)
    hot = report.get("hot") or []
    top = hot[0] if hot else None
    summary = (f"池 {report['pool_size']} · 算分 {report['computed_count']}"
               f" · 过热/拥挤 {report['hot_count']} · 补数 {report['collected_count']}"
               f"（{(report['elapsed_ms'] or 0) / 1000:.0f}s）")
    if report["insufficient_count"]:
        summary += f" · 待补数 {report['insufficient_count']}"
    out = {
        "summary": summary,
        "trade_date": report.get("trade_date"),
        "pool_size": report["pool_size"],
        "computed": report["computed_count"],
        "hot_count": report["hot_count"],
        "insufficient": report["insufficient_count"],
        "stale": report["stale_count"],
        "collected": report["collected_count"],
        "created": bool(saved.get("created")),
        "warnings": len(report.get("warnings") or []),
    }
    if top:
        out["top"] = f"{top['name']} {top['score']}"
    return out


# ── ④ 短线情绪预热 ───────────────────────────────────────────────

def shortline_refresh() -> dict:
    """拉一次当日短线情绪聚合（四池 + 天梯 + 溢价）并固化当日快照。

    复用 `shortline_service.build_overview`：60s 缓存 + hithink 串行节流 + 429 重试 +
    上游全败自动回退最近历史快照（stale 标记）——后台预热与页面请求共用同一条链路，
    撞车时由模块内 `_fetch_lock` 串行，不会重复打上游。
    """
    from server import shortline_service as sl

    data = sl.build_overview()   # 无缓存时触网拉取；上游全败也不抛错，返回降级形状
    s = data.get("summary") or {}
    degraded = data.get("degraded") or []
    trade_date = s.get("trade_date") or "—"
    score = s.get("score")
    level = s.get("level")

    parts = []
    if score is not None and level:
        parts.append(f"情绪 {score} 分 / {level}")
    parts.append(
        f"涨停 {s.get('limit_up_count', 0)} · 跌停 {s.get('limit_down_count', 0)}"
        f" · 炸板 {s.get('broken_count', 0)} · 口径日 {trade_date}")
    if data.get("stale"):
        parts.append(f"上游不可达，回退历史快照 {data.get('stale_date')}")
    if degraded:
        parts.append(f"降级 {len(degraded)} 项")
    if data.get("error"):
        parts.append(str(data["error"])[:80])

    return {
        "summary": " · ".join(parts),
        "trade_date": trade_date,
        "score": score,
        "level": level,
        "limit_up": s.get("limit_up_count"),
        "limit_down": s.get("limit_down_count"),
        "broken": s.get("broken_count"),
        "stale": bool(data.get("stale")),
        "degraded": len(degraded),
        "error": data.get("error"),
    }


# ── ⑤ K 线保活 ───────────────────────────────────────────────────

def kline_keepalive() -> dict:
    """把「持仓 ∪ 自选」的日线补到**最近已完成交易日**。

    存在理由（2026-09-30 实测）：`stock_daily` 覆盖从 501 只一路掉到个位数
    （09-24 516 只 → 09-28 367 → 09-29 只剩 24 → 09-30 只剩 14），**没有任何
    自动任务在维护 K 线**——4 个既有巡检作业都不碰它，`data_tasks` 里
    `tail_fill` 从未执行过、`daily_kline` 最后运行于 2026-06-06。
    结果是大屏策略信号大面积「数据截止 N 天前」，全靠用户打开页面时扫描路径
    的「预补尾 6 只」兜底（67 只落后时需 ≥12 轮、≥2 小时，且不打开页面就不推进）。

    预算：按 `SCAN_KLINE_MAX_CODES` 只 + `SCAN_KLINE_BUDGET_S` 秒双约束，
    取先到者。已是最新的标的会被 `refresh_stale_tail` 内部跳过（不耗预算）。
    """
    import time

    from config import Config
    from utils.cache.market_data_db import (
        TAIL_COLD_START_DAYS, fetch_and_store_daily, get_latest_trade_date,
        refresh_stale_tail,
    )
    from utils.trading_calendar import get_effective_trading_date

    started = time.time()
    budget_s = float(getattr(Config, "SCAN_KLINE_BUDGET_S", 120) or 120)
    max_codes = int(getattr(Config, "SCAN_KLINE_MAX_CODES", 200) or 200)

    effective = get_effective_trading_date("cn").isoformat()
    codes = _scan_universe()
    stale = [c for c in codes if str(get_latest_trade_date(c) or "") < effective]
    logger.info("[Scans] K线保活：宇宙 %d 只，落后 %d 只，目标 %s", len(codes), len(stale), effective)

    updated = cold_started = failed = 0
    for code in stale[:max_codes]:
        if time.time() - started > budget_s:
            logger.info("[Scans] K线保活达到 %.0fs 预算，提前收尾", budget_s)
            break
        try:
            # 冷启动（本地零行）必须单独走全量拉取：`refresh_stale_tail` 明确
            # **不负责**冷启动（它在 K 线图读取路径上，不能做数百天的重活）。
            # 新入自选、首次出现的 ETF 都属于这一类——不处理就永远是空图。
            # 2026-09-30 实测：ETF `510415` 本地零行，只走补尾会一直被静默跳过。
            if not get_latest_trade_date(code):
                if fetch_and_store_daily(code, days=TAIL_COLD_START_DAYS):
                    cold_started += 1
                else:
                    failed += 1
            elif refresh_stale_tail(code):
                updated += 1
            else:
                failed += 1          # 冷却中/取数失败/仍滞后，都算未成功
        except Exception as e:       # noqa: BLE001 —— 单只异常不该中断整轮
            failed += 1
            logger.debug("[Scans] K线保活 %s 失败: %s", code, e)

    # deferred 必须在循环**外**统一算：循环可能因预算 break 而止，也可能因
    # max_codes 正常跑完——只在预算分支里赋值会漏掉后者（单测抓到过）。
    deferred = max(0, len(stale) - (updated + cold_started + failed))
    elapsed = time.time() - started
    summary = (f"K线保活：{len(codes)} 只标的中 {len(stale)} 只落后 · "
               f"本轮补 {updated} 成功"
               + (f" / 冷启动 {cold_started}" if cold_started else "")
               + f" / {failed} 未成"
               + (f" · {deferred} 只顺延" if deferred else "")
               + f" · 耗时 {elapsed:.0f}s")
    return {
        "summary": summary,
        "universe": len(codes),
        "stale": len(stale),
        "updated": updated,
        "cold_started": cold_started,
        "failed": failed,
        "deferred": deferred,
        "effective_date": effective,
        "elapsed_s": round(elapsed, 1),
    }


def _scan_universe() -> list[str]:
    """「持仓 ∪ 自选」的可扫代码（去重排序）。与策略扫描块同口径。

    直读 `stock_radar.db` 而非持有 `WebStorage`：本作业跑在后台线程、没有请求上下文，
    而 `WebStorage` 没有全局单例（`server/bootstrap.py` 里 new 出来挂在 app state 上）。
    直读还能避免为一次性读取长期占住一个连接。
    """
    try:
        from server.storage import get_db_path
        from utils import sqlite_guard
    except Exception as e:                       # noqa: BLE001
        logger.warning("[Scans] 读取标的宇宙依赖不可用: %s", e)
        return []

    codes: set[str] = set()
    try:
        conn = sqlite_guard.connect(str(get_db_path()))
        try:
            conn.row_factory = None              # 只取单列，不需要 Row
            for row in conn.execute(
                    "SELECT stock_code FROM portfolio_position WHERE COALESCE(qty, 0) > 0"):
                codes.add(row[0])
            for row in conn.execute("SELECT stock_code FROM watchlist"):
                codes.add(row[0])
        finally:
            conn.close()
    except Exception as e:                       # noqa: BLE001 —— 读不到就返回空，别炸整轮
        logger.warning("[Scans] 读取持仓/自选失败: %s", e)
    return sorted(c for c in codes if c and str(c).isdigit() and len(str(c)) == 6)


# ── ⑦ 全市场成交额日终补录 ────────────────────────────────────────

def _amount_refresh_gate(now, is_open: bool) -> tuple[bool, str]:
    """日终补录的时点门禁（纯函数，便于单测）：返回 ``(是否记录, 原因)``。

    - 非交易日（周末/节假日）→ 不记。记了等于凭空造一个有成交的日子，
      还会顶掉短历史里真实存在的前一交易日。
    - 收盘前（<15:05）→ 不记。此时累计额不是全日真值，记进去就是「半日冒充全天」。
    """
    if now.weekday() >= 5 or not is_open:
        return False, f"{now:%Y-%m-%d} 非交易日，跳过记录"
    minutes = now.hour * 60 + now.minute
    if minutes < 15 * 60 + 5:
        return False, f"{now:%H:%M} 尚未收盘（≥15:05），当前累计额非全日真值，跳过"
    return True, ""


def market_amount_refresh() -> dict:
    """把当日全市场成交额按**收盘真值**写入 market_snapshot 的短历史。

    背景：`roll_prev_amount` 记的是「调用那一刻」的累计额，若某交易日最后一次调用发生在
    盘中（收盘后无人再跑），历史里留下的就是半日值且不会被修正 —— 次日
    `get_market_breadth` 的 `prev_amount` 变成半日量级，量能对比整体失真。
    实测：10-16 记成 9147.05 亿，收盘口径约 1.57 万亿，导致「半日已达前日 107%」的错误结论。

    口径纪律：
    - **非交易日不记录**（周末/节假日直接跳过，避免把假期写成有成交的一天）；
    - **收盘前不记录**（盘中累计值不是全日真值，会把半日量写成全天量）；
    - 归属日取「今天」，且必须是交易日 —— 节假日由 trading_calendar 的 fail-open 语义
      与 weekend fail-safe 双重把关，非交易日一律跳过。

    手动跑：`python scripts/ops/refresh_background_data.py --only market_amount_refresh`
    """
    from datetime import datetime, timedelta, timezone

    from tools.fetcher.hithink_ds import HithinkDataSource
    from tools.market_snapshot import compute_breadth, record_amount_for
    from utils.trading_calendar import is_market_open

    cst = timezone(timedelta(hours=8))
    now = datetime.now(cst)
    today = now.date()

    ok, reason = _amount_refresh_gate(now, is_market_open("cn", today))
    if not ok:
        return {"summary": reason, "trade_date": today.isoformat(), "skipped": True}

    df = HithinkDataSource.get_spot_all()
    breadth = compute_breadth(df)
    if not breadth or not breadth.get("amount"):
        summary = "全市场快照为空或成交额为 0，跳过"
        return {"summary": summary, "trade_date": today.isoformat(), "skipped": True}

    written = record_amount_for(today.isoformat(), breadth["amount"])
    if not written:
        summary = "写入短历史失败（缓存层不可用）"
        return {"summary": summary, "trade_date": today.isoformat(), "skipped": True}

    amount_yi = breadth["amount"] / 1e8
    verb = "覆盖" if written["overwritten"] else "补录"
    summary = (f"{today} 成交额{verb} {amount_yi:,.2f} 亿（收盘口径）"
               f" · 短历史 {written['history_size']} 日")
    return {
        "summary": summary,
        "trade_date": today.isoformat(),
        "amount": breadth["amount"],
        "amount_yi": round(amount_yi, 2),
        "up": breadth.get("up"),
        "down": breadth.get("down"),
        "overwritten": written["overwritten"],
        "history_size": written["history_size"],
    }


# ── 注册 ─────────────────────────────────────────────────────────

def default_jobs() -> list:
    """按 Config 生成默认作业（含开关、间隔、首次延迟）。"""
    from config import Config
    from server.background_scans import ScanJob

    return [
        ScanJob(
            key="storage_scan",
            name="存储监控扫描",
            fn=storage_scan,
            enabled=Config.BACKGROUND_SCANS_ENABLED and Config.SCAN_STORAGE_ENABLED,
            interval_minutes=Config.SCAN_STORAGE_INTERVAL_MINUTES,
            first_delay_s=Config.SCAN_STORAGE_FIRST_DELAY_S,
            description="扫描 db/ 与数据目录的大小、行数、表级明细，落到 data_monitor.db 快照",
        ),
        ScanJob(
            key="thesis_refresh",
            name="研究台账刷新",
            fn=thesis_refresh,
            enabled=Config.BACKGROUND_SCANS_ENABLED and Config.SCAN_THESIS_ENABLED,
            interval_minutes=Config.SCAN_THESIS_INTERVAL_MINUTES,
            first_delay_s=Config.SCAN_THESIS_FIRST_DELAY_S,
            description="刷新全部研究台账定义并产出变更告警（只报变化不报状态）",
        ),
        ScanJob(
            key="crowding_scan",
            name="全市场拥挤度扫描",
            fn=crowding_scan,
            enabled=Config.BACKGROUND_SCANS_ENABLED and Config.SCAN_CROWDING_ENABLED,
            interval_minutes=Config.SCAN_CROWDING_INTERVAL_MINUTES,
            first_delay_s=Config.SCAN_CROWDING_FIRST_DELAY_S,
            description="指数池 + 行业板块池的拥挤度打分与轮转补数，落到 crowding.db",
        ),
        ScanJob(
            key="shortline_refresh",
            name="短线情绪预热",
            fn=shortline_refresh,
            enabled=Config.BACKGROUND_SCANS_ENABLED and Config.SCAN_SHORTLINE_ENABLED,
            interval_minutes=Config.SCAN_SHORTLINE_INTERVAL_MINUTES,
            first_delay_s=Config.SCAN_SHORTLINE_FIRST_DELAY_S,
            description="拉取当日短线情绪聚合（四池/天梯/溢价）并固化 shortline.db 快照",
        ),
        ScanJob(
            key="pricing_check",
            name="定价目录巡检",
            fn=pricing_check,
            enabled=Config.BACKGROUND_SCANS_ENABLED and getattr(Config, "COST_FREE_VALIDITY_CHECK", True),
            interval_minutes=1440,
            first_delay_s=210,
            description="检查免费源有效期（过期自动降级为「未计价」并告警）；启动后错峰执行，之后每日一次",
        ),
        ScanJob(
            key="uploads_cleanup",
            name="对话图片清理",
            fn=uploads_cleanup,
            enabled=Config.BACKGROUND_SCANS_ENABLED,
            interval_minutes=1440,
            first_delay_s=225,
            description="清理孤儿/过期/超量的对话图片（消息里的文字转述保留）",
        ),
        ScanJob(
            key="kline_keepalive",
            name="K 线保活",
            fn=kline_keepalive,
            enabled=Config.BACKGROUND_SCANS_ENABLED and Config.SCAN_KLINE_ENABLED,
            interval_minutes=Config.SCAN_KLINE_INTERVAL_MINUTES,
            first_delay_s=Config.SCAN_KLINE_FIRST_DELAY_S,
            description="把「持仓 ∪ 自选」的日线补到最近已完成交易日，防止 K 线覆盖静默腐烂",
        ),
        ScanJob(
            key="market_amount_refresh",
            name="全市场成交额日终补录",
            fn=market_amount_refresh,
            # 默认关闭：这是**手动**补录工具（收盘后跑一次），不该在服务启动时自动执行。
            # 手动跑：python scripts/ops/refresh_background_data.py --only market_amount_refresh
            # 打开开关 Config.SCAN_MARKET_AMOUNT_ENABLED=true 才会进后台调度。
            enabled=Config.BACKGROUND_SCANS_ENABLED and getattr(Config, "SCAN_MARKET_AMOUNT_ENABLED", False),
            interval_minutes=getattr(Config, "SCAN_MARKET_AMOUNT_INTERVAL_MINUTES", 1440),
            first_delay_s=getattr(Config, "SCAN_MARKET_AMOUNT_FIRST_DELAY_S", 3600),
            description="收盘后把当日全市场成交额按收盘真值写入短历史（非交易日/收盘前自动跳过）",
        ),
    ]


# ── ⑥ 定价目录巡检 ──────────────────────────────────────────────

def uploads_cleanup() -> dict:
    """清理对话图片：孤儿（未引用）/过期（>45 天）/超量（>500MB 时 LRU）。

    只删图片文件，不动消息：消息里的文字转述仍在，前端图片位显示「已过期」占位。
    """
    from server.uploads import cleanup

    res = cleanup()
    mb = lambda n: f"{n / 1048576:.1f} MB"  # noqa: E731
    summary = (f"图片清理：删 {res['deleted']} 个 · 释放 {mb(res['freed_bytes'])} · "
               f"保留 {res['kept']} 个（{mb(res['total_bytes'])}，保留 {res['retention_days']} 天）")
    return {"summary": summary, "deleted": res["deleted"], "freed_bytes": res["freed_bytes"],
            "kept": res["kept"], "total_bytes": res["total_bytes"]}


def pricing_check() -> dict:
    """检查定价目录：免费源是否过期（过期即降级为「未计价」并告警）。

    为什么要巡检而不是只在调用时判断：免费额度到期是**沉默失效**——
    用户看到的是「成本变 0 或变未计价」，不知道原因。每日一查 + 启动一查，
    把「你的免费源到期了，去定价目录更新」明确说出来。
    """
    from utils import pricing
    res = pricing.check_free_validity()
    expired = res.get("expired") or []
    summary = (f"价目 v{res.get('version') or '-'} · {res.get('checked', 0)} 个供应商"
               + (f" · 免费源已过期：{'、'.join(expired)}（已按未计价处理，请更新定价目录）"
                  if expired else " · 免费源均在有效期内"))
    return {"summary": summary, "providers": res.get("checked", 0),
            "expired": len(expired), "expired_names": expired,
            "version": res.get("version") or ""}


def register_default_jobs(manager=None) -> list[str]:
    """注册默认作业，返回已注册的 key 列表（幂等，重复调用只覆盖定义）。"""
    from server.background_scans import get_manager
    mgr = manager or get_manager()
    jobs = default_jobs()
    for job in jobs:
        mgr.register(job)
    return [j.key for j in jobs]
