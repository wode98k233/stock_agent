"""条件单触发判定（纯函数，不读写库/网络）。

口径：从信号日的**下一个交易日**开始扫描（信号日当天触达不算）；回踩看最低价
是否触达区间上沿，突破看收盘价是否站上阈值。窗口内没触达 = not_triggered；
K 线还没覆盖完整窗口 = pending。

输入契约（当前上游 `stats.fetch_kline` 已满足）：`kline` 按 `date` 升序、
价格字段为数值；`date` / `signal_date` 为 `YYYY-MM-DD` 字符串（定长格式的
字典序与时间序一致，内部按字符串比较）。不要传未排序的 K 线或字符串价格。
"""


def _scan_start(kline: list[dict], signal_date: str) -> int | None:
    """返回第一个可扫描的 K 线下标（严格晚于信号日）。

    用「严格大于」而不是「大于等于后 +1」：信号日可能是非交易日/缺 bar，
    这时第一根 >= 信号日的 K 线本身就是下一个交易日，必须纳入扫描，不能跳过。
    有精确同日 bar 时它不满足 >，自然从次日起扫。
    """
    d = (signal_date or "")[:10]
    for i, k in enumerate(kline):
        if (k.get("date") or "")[:10] > d:
            return i
    return None


def _level(value):
    """归一化单侧边界：数值型原样返回，其余（None/字符串等）返回 None。

    单侧缺失/非法时由 `trigger_status` 退化为另一侧（回踩只给 low 时用 low 作
    上沿，突破只给 high 时用 high 作阈值）；两侧都缺失/非法 → unverifiable，
    避免把非法输入静默当成「未触发」或拿字符串做比较。
    """
    return value if isinstance(value, (int, float)) else None


def trigger_status(kline: list[dict], signal_date: str, entry_type: str,
                   low, high, window: int = 10) -> dict:
    """返回 {status, trigger_date, days_to_trigger}。

    status: triggered / not_triggered / pending / unverifiable
    days_to_trigger: 触发发生在「信号后第 N 根可用 K 线」，1-based，首根可扫描的
        K 线记 1。有同日 bar 时等于「触发 bar 下标 − 信号日 bar 下标」；信号日
        缺 bar（非交易日/停牌）时，信号日之后的第一根同样记 1。下游会展示/求平均，
        不能改成 0-based。
    """
    etype = str(entry_type or "").lower()
    lo, hi = _level(low), _level(high)
    # 类型不认识或两个边界都没有 → 无法判定，不能默认成未触发（会污染触发率）
    if etype not in ("pullback", "breakout") or (lo is None and hi is None):
        return {"status": "unverifiable", "trigger_date": None, "days_to_trigger": None}

    # 窗口非正/非法按默认 10 处理；不能用 max(1, ...) 把负数悄悄夹成 1
    try:
        window = int(window)
    except (TypeError, ValueError):
        window = 10
    if window <= 0:
        window = 10

    kline = kline or []
    start = _scan_start(kline, signal_date)
    if start is None:
        # K 线还没走到信号日之后（未覆盖信号日或全部不晚于信号日），无法开始扫描
        return {"status": "pending", "trigger_date": None, "days_to_trigger": None}

    end = min(start + window, len(kline))
    for i in range(start, end):
        k = kline[i]
        if etype == "pullback":
            # 回踩：最低价触及区间上沿即算（上沿缺失时退化为下沿）；
            # 缺 low 的 bar 视为不命中，不能拿 close 兜底。
            level = hi if hi is not None else lo
            price = k.get("low")
            hit = price is not None and price <= level
        else:
            # 突破：只看收盘价，盘中假突破不算
            level = lo if lo is not None else hi
            price = k.get("close")
            hit = price is not None and price >= level
        if hit:
            return {
                "status": "triggered",
                "trigger_date": (k.get("date") or "")[:10] or None,
                "days_to_trigger": i - start + 1,
            }

    # 扫描区间不足完整窗口 → 数据还没走完，不能断言未触发
    if end - start < window:
        return {"status": "pending", "trigger_date": None, "days_to_trigger": None}
    return {"status": "not_triggered", "trigger_date": None, "days_to_trigger": None}
