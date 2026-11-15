"""因子先验（S4 · F5-A）：把「哪个因子在 A 股还有效」喂给策略研究模型。

动机
----
因子评估的第一轮结论很反直觉：**36 个技术因子里只有 ADX 通过了多重检验校正**，
而它在沪深300 与中证500 上**符号相反**（2026-10-17 实测）。也就是说模型如果
不知道这件事，会把迭代轮次浪费在「用 ADX/RSI/MACD 做主条件」这种大概率无效的方向上。

因此做一层**先验**：
- 编译期/提示期：在 ``_initial_user_prompt`` / ``_round_prompt`` 里附一段
  「已知：X 稳健、Y 跨池变号别当主条件」的短文本（模型可见，零引擎改动）；
- 会话期：用户想法里点名了脆弱因子时，确认卡上先提醒一句（不阻断）。

先验文件 ``output/factor_prior.json``（**不是**断言，是数据快照）：
robust = 两池 DSR 均 ≥0.95 且同号；fragile = 两池都显著但**符号相反**。
没有跨池数据时只给方向提示并标注「单池，未验证跨池」。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
PRIOR_PATH = ROOT / "output" / "factor_prior.json"
COMPARE_GLOB = "factor_pool_compare_*.json"

_CACHE: Dict[str, Any] = {"mtime": None, "data": None}
_LOCK = threading.Lock()

# 想法关键词 → 因子 id（避免只按中文名匹配导致漏召）
_ALIASES: Dict[str, str] = {
    "adx": "dmi_adx", "趋势强度": "dmi_adx",
    "rsi": "rsi_24", "kdj": "kdj_j", "macd": "macd_dif", "boll": "boll_pctb",
    "布林": "boll_pctb", "均线": "ma_bias_20", "乖离": "ma_bias_20", "ma20": "ma_bias_20",
    "成交量": "vol_ratio_5_20", "量比": "vol_ratio_5_20", "obv": "obv_slope_20",
    "波动率": "volatility_20", "atr": "atr_pct", "位置": "close_position_20",
    "动量": "ret_60", "超买": "rsi_24", "超卖": "rsi_24", "cci": "cci_20", "psy": "psy_12",
}


def _latest_compare() -> Optional[dict]:
    files = sorted((ROOT / "output").glob(COMPARE_GLOB))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


logger = logging.getLogger(__name__)


def has_prior_source() -> bool:
    """是否存在「本可用于生成先验」的原始数据。

    把「先验文件缺失」从静默升级为可诊断：文件不在、但**因子评估报告 / 跨池对照
    JSON** 在 → 是漏跑生成脚本（scripts/ops/build_factor_prior.py）；两者都没有则
    属正常（尚未做过因子评估），无需告警。
    """
    try:
        from backtest import factor_store
        if factor_store.list_reports(limit=1):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(sorted((ROOT / "output").glob(COMPARE_GLOB)))


# 哨兵：区分「不传（去磁盘自动取）」与「显式传 None（无跨池数据）」
_AUTO = object()


def build_prior(*, reports: Optional[List[dict]] = None,
                compare: Any = _AUTO,
                written_at: str = "") -> dict:
    """汇总先验。

    Args:
        reports: 各池的因子评估报告；None → 自动从 ``factor_store`` 取。
        compare: 跨池对照 JSON；**默认从磁盘取最新**，显式传 ``None`` 表示无跨池数据。
    """
    from backtest.factor_lib import FACTOR_SPECS

    if reports is None:
        try:
            from backtest import factor_store
            reports = factor_store.list_reports(limit=50)
        except Exception:  # noqa: BLE001
            reports = []
    if compare is _AUTO:
        compare = _latest_compare()

    # 每个池取最新一份报告
    latest: Dict[str, dict] = {}
    for r in reports:
        pool = str(r.get("index_code") or "")
        if pool and pool not in latest:
            latest[pool] = r

    # 跨池结论
    cmp_rows: Dict[str, dict] = {}
    cmp_meta: Dict[str, Any] = {}
    if compare:
        # 必须拷贝：直接引用 compare['meta'] 再往里写会「dictionary changed size
        # during iteration」（2026-10-17 首次生成先验就踩到）
        cmp_meta = dict(compare.get("meta") or {})
        for pool_meta in (compare.get("meta") or {}).values():
            cmp_meta.update({k: v for k, v in (pool_meta or {}).items()
                             if k in ("index_code", "as_of_date", "start", "end")})
        for row in ((compare.get("compare") or {}).get("rows") or []):
            cmp_rows[row.get("factor_id")] = row

    factors: Dict[str, dict] = {}
    for fid, spec in FACTOR_SPECS.items():
        cmp_row = cmp_rows.get(fid) or {}
        ic_a, ic_b = cmp_row.get("rankic_a"), cmp_row.get("rankic_b")
        sign_agree = cmp_row.get("sign_agree")
        dsr_a = cmp_row.get("dsr_a") or 0.0
        dsr_b = cmp_row.get("dsr_b") or 0.0
        both = (ic_a is not None and ic_b is not None)
        robust = bool(both and sign_agree and dsr_a >= 0.95 and dsr_b >= 0.95)
        fragile = bool(both and sign_agree is False and abs(ic_a or 0) >= 0.01
                       and abs(ic_b or 0) >= 0.01)
        direction = 0
        if ic_a is not None and ic_b is not None:
            direction = 1 if (ic_a + ic_b) > 0 else -1
        elif ic_a is not None:
            direction = 1 if ic_a > 0 else -1
        factors[fid] = {
            "name": spec.name,
            "category": spec.category,
            "rankic_a": ic_a,
            "rankic_b": ic_b,
            "sign_agree": sign_agree,
            "dsr_a": dsr_a,
            "dsr_b": dsr_b,
            "robust": robust,
            "fragile": fragile,
            "direction": direction,
            "best_period": (cmp_row and None),
        }

    robust_list = sorted([f for f, v in factors.items() if v["robust"]],
                         key=lambda f: -(abs(factors[f]["rankic_a"] or 0)))
    fragile_list = sorted([f for f, v in factors.items() if v["fragile"]])
    reverse_list = sorted([f for f, v in factors.items()
                           if v["direction"] < 0 and not v["robust"]
                           and abs(v["rankic_a"] or 0) >= 0.02])

    return {
        "written_at": written_at,
        "source": {
            "pools": list(latest.keys()),
            "reports": len(latest),
            "cross_pool": bool(cmp_rows),
            "compare_pools": (compare or {}).get("compare", {}).get("pools"),
        },
        "params": {p: latest[p].get("params") for p in latest},
        "factors": factors,
        "summary": {
            "robust": robust_list,
            "fragile": fragile_list,
            "reverse": reverse_list,
            "n": len(factors),
            "caveat": ("两池均为 2 年日频单次样本；robust 只表示「在本次评估里通过"
                       "多重检验校正且跨池同号」，不表示未来仍有效。"),
        },
    }


def write_prior(data: Optional[dict] = None, written_at: str = "") -> dict:
    from datetime import datetime
    data = data or build_prior(written_at=written_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    PRIOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRIOR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with _LOCK:
        _CACHE["mtime"] = PRIOR_PATH.stat().st_mtime
        _CACHE["data"] = data
    return data


def load_prior(path: Optional[Path] = None) -> Optional[dict]:
    """读先验（按 mtime 缓存）。文件不存在或损坏 → None（调用方按「无先验」处理）。"""
    p = Path(path) if path else PRIOR_PATH
    if not p.exists():
        return None
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    with _LOCK:
        if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
            return _CACHE["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    with _LOCK:
        _CACHE["mtime"] = mtime
        _CACHE["data"] = data
    return data


def _fmt(v: Optional[float], digits: int = 4) -> str:
    return "—" if v is None else f"{v:+.{digits}f}"


def prior_prompt_text(prior: Optional[dict] = None, max_robust: int = 3,
                      max_fragile: int = 3) -> str:
    """给模型看的短文本（≤ 6 行）。没先验返回空串——不编造。"""
    prior = prior if prior is not None else load_prior()
    if not prior or not prior.get("factors"):
        return ""
    s = prior.get("summary") or {}
    lines: List[str] = []
    if s.get("robust"):
        names = [prior["factors"][f]["name"] for f in s["robust"][:max_robust] if f in prior["factors"]]
        if names:
            lines.append("【因子先验｜稳健（两池 DSR≥0.95 且同号）】" + "、".join(names))
    if s.get("fragile"):
        names = [prior["factors"][f]["name"] for f in s["fragile"][:max_fragile] if f in prior["factors"]]
        if names:
            lines.append("【因子先验｜跨池变号，别当主条件】" + "、".join(names))
    if s.get("reverse"):
        names = [prior["factors"][f]["name"] for f in s["reverse"][:max_fragile] if f in prior["factors"]]
        if names:
            lines.append("【因子先验｜方向为负（可作回避/反向过滤）】" + "、".join(names))
    if not prior.get("source", {}).get("cross_pool"):
        lines.append("（以上仅为单池结果，未做跨池验证——把结论当作弱先验。）")
    lines.append("以上是本项目自己的历史评估，不是通用常识；若与想法冲突，按想法走。")
    return "\n".join(lines)


def prior_warnings(idea: str, prior: Optional[dict] = None) -> List[str]:
    """想法里点名了「脆弱 / 反向」因子时返回提醒（不阻断，只提示）。"""
    prior = prior if prior is not None else load_prior()
    if not prior or not prior.get("factors"):
        return []
    text = (idea or "").lower()
    frag = set((prior.get("summary") or {}).get("fragile") or [])
    rev = set((prior.get("summary") or {}).get("reverse") or [])
    hit_frag, hit_rev = [], []
    for fid, v in prior["factors"].items():
        keys = [v.get("name", "")] + [k for k, target in _ALIASES.items() if target == fid]
        if not any(k and str(k).lower() in text for k in keys):
            continue
        if fid in frag:
            hit_frag.append(v["name"])
        elif fid in rev:
            hit_rev.append(v["name"])
    out: List[str] = []
    if hit_frag:
        out.append("提醒：你提到的「" + "、".join(hit_frag) + "」在本项目两池评估里符号相反，"
                   "不建议作为主条件（可以当过滤/回避用）。")
    if hit_rev:
        out.append("提醒：「" + "、".join(hit_rev) + "」在本项目评估里方向为负，"
                   "用它做主条件大概率跑出反向结论。")
    return out
