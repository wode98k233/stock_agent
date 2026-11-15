"""按源对账并修复日线里的「半截 bar」（盘中快照/错值入库后再也没被覆盖）。

## 背景

读取路径的补尾（``refresh_stale_tail``）只在本地落后于交易日时才触发，且按
「今天往前 N 天」整段重拉；已经是最新的日期不会再校验。于是某些标的的**历史某根**
会永久保留盘中快照：2026-11-12 审计发现上证指数 2026-10-08 / 10-16 两根、上证 50
2026-10-16 一根，收盘价比源低 0.3~0.9%，在图上就是"这根 K 线长得有点怪"。

## 用法

    # 只体检，不改库
    .\\venv\\Scripts\\python.exe scripts\\ops\\repair_daily_bars.py --all-indices --days 250

    # 体检并写回
    .\\venv\\Scripts\\python.exe scripts\\ops\\repair_daily_bars.py --all-indices --days 250 --apply

    # 指定标的
    ... --codes 000001,000016 --days 120 --apply
    ... --type stock --codes 600519 --days 120            # 个股默认只体检

个股默认**不写回**：库里的个股口径是混的（600519 是原始价、000001 已经是前复权），
用不复权的源覆盖前复权的行会把整段历史打回原始价。需要显式 ``--force-stock``。

口径说明：对账基准是腾讯 ``none``（不复权）。指数没有复权概念，可直接覆盖。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# 收盘偏差超过它就算"错 bar"；0.2% 对指数已是肉眼可见级别
_TOL = 0.002


def _symbol(code: str, is_index: bool = False) -> str:
    c = str(code).strip().lower()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    if is_index:
        # 指数前缀不能按个股规则猜：000001 是指数（上证）而不是平安银行（深市）。
        # 000xxx/880xxx 走上交所，399xxx 走深交所，899xxx 是北证。
        if c.startswith("399"):
            return f"sz{c}"
        if c.startswith("899"):
            return f"bj{c}"
        return f"sh{c}"
    if c.startswith(("60", "68", "50", "51", "52", "56", "58", "90", "11")):
        return f"sh{c}"
    if c.startswith(("43", "83", "87", "88", "92")):
        return f"bj{c}"
    return f"sz{c}"


def _fetch_raw(code: str, days: int, is_index: bool = False) -> pd.DataFrame:
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.6) + 20)).strftime("%Y-%m-%d")
    sym = _symbol(code, is_index)
    params = f"param={sym},day,{start},{end},{min(days + 40, 900)},none"
    req = urllib.request.Request(
        f"{_BASE}?{params}",
        headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.load(resp)
    node = ((payload.get("data") or {}).get(sym) or {})
    rows = node.get("day") or []
    out = []
    for item in rows:
        if len(item) < 6:
            continue
        out.append({"trade_date": str(item[0]), "open": float(item[1]), "close": float(item[2]),
                    "high": float(item[3]), "low": float(item[4]), "volume": float(item[5])})
    return pd.DataFrame(out)


def _local(code: str, is_index: bool, days: int) -> pd.DataFrame:
    from utils.cache.market_data_db import get_index_daily, get_stock_daily

    df = get_index_daily(code) if is_index else get_stock_daily(code, limit=days)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    return df.tail(days)


def repair(code: str, is_index: bool, days: int, apply: bool, force_stock: bool) -> dict:
    local = _local(code, is_index, days)
    if local.empty:
        return {"code": code, "status": "no_local", "bad": []}
    ref = _fetch_raw(code, days, is_index)
    if ref.empty:
        return {"code": code, "status": "no_ref", "bad": []}
    ref["trade_date"] = ref["trade_date"].astype(str)
    m = local.merge(ref, on="trade_date", suffixes=("_l", "_r"))
    if m.empty:
        return {"code": code, "status": "no_overlap", "bad": []}
    rel = (m["close_l"] - m["close_r"]).abs() / m["close_r"].replace(0, pd.NA)
    bad = m[rel.fillna(0) > _TOL]
    result = {
        "code": code,
        "status": "ok" if bad.empty else "bad",
        "checked": len(m),
        "worst_pct": round(float(rel.max()) * 100, 3) if len(rel) else 0.0,
        "bad": [
            {"date": r.trade_date, "local": round(float(r.close_l), 3),
             "ref": round(float(r.close_r), 3)}
            for r in bad.itertuples(index=False)
        ],
    }
    # 整段都对不上 = 拿错了标的（最常见：把 000001 指数按个股映射成深市平安银行）。
    # 这种情况绝不能写回，否则会把指数历史覆盖成个股价格。
    median_dev_all = float(rel.median()) if len(rel) else 0.0
    if median_dev_all > 0.2:
        result["status"] = "symbol_mismatch"
        result["median_dev_pct"] = round(median_dev_all * 100, 2)
        return result
    if bad.empty or not apply:
        return result

    if not is_index:
        # 个股口径混杂：只有当本地整体与不复权源吻合时才允许覆盖（说明它是原始价）
        whole = m
        median_dev = float(((whole["close_l"] - whole["close_r"]).abs()
                            / whole["close_r"].replace(0, pd.NA)).median())
        if median_dev > _TOL and not force_stock:
            result["status"] = "skip_basis"
            result["basis_dev_pct"] = round(median_dev * 100, 3)
            return result

    records = [{"code": code, "trade_date": r.trade_date, "open": r.open_r, "high": r.high_r,
                "low": r.low_r, "close": r.close_r, "volume": r.volume_r}
               for r in bad.itertuples(index=False)]
    from utils.cache.market_data_db import upsert_index_daily, upsert_stock_daily

    n = upsert_index_daily(records, source="repair:tencent") if is_index \
        else upsert_stock_daily(records, source="repair:tencent")
    result["written"] = n
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="日线半截 bar 对账/修复（基准：腾讯不复权）")
    ap.add_argument("--codes", default="", help="逗号分隔代码；配合 --type")
    ap.add_argument("--all-indices", action="store_true", help="对 index_daily 里所有指数体检")
    ap.add_argument("--type", choices=["index", "stock"], default="index")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--apply", action="store_true", help="写回修复（默认只体检）")
    ap.add_argument("--force-stock", action="store_true", help="个股即使口径不符也覆盖（危险）")
    args = ap.parse_args()

    if args.all_indices:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            codes = [r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM index_daily ORDER BY code")]
    else:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        ap.error("需要 --codes 或 --all-indices")

    is_index = args.type == "index"
    bad_total = 0
    for code in codes:
        try:
            r = repair(code, is_index, args.days, args.apply, args.force_stock)
        except Exception as e:  # noqa: BLE001 - 单只失败不影响整体对账
            print(f"[err] {code}: {type(e).__name__} {e}")
            continue
        if r["bad"]:
            bad_total += len(r["bad"])
            print(f"[bad] {code} 检查 {r.get('checked')} 天，最大偏差 {r.get('worst_pct')}%，"
                  f"{len(r['bad'])} 根：{r['bad'][:4]}"
                  f"{' → 已写回 ' + str(r.get('written')) if r.get('written') else ''}"
                  f"{' → 跳过（本地是前复权口径 ' + str(r.get('basis_dev_pct')) + '%）' if r['status'] == 'skip_basis' else ''}")
        elif r["status"] in ("symbol_mismatch",):
            print(f"[skip] {code} 参考序列与本地整体不符（中位偏差 {r.get('median_dev_pct')}%），"
                  f"疑似标的映射错误，未写库")
        # 只打印异常标的，几十只指数时输出才读得下去
    print(f"\n完成：{len(codes)} 只，异常 bar {bad_total} 根，"
          f"模式={'写回' if args.apply else '只体检'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
