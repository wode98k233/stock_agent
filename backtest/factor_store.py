"""因子评估报告持久化（工作台 v3 · F1）。

分层
----
- **库**（`factor_report` 表，backtest.db）：摘要 + 路径。列表页只读摘要，开销固定。
- **文件**（`output/factor_reports/<uuid>.json|.html`）：完整 payload / 可分发 HTML。
  payload 3~12MB，进库会让列表页和备份一起变重，所以走文件。

为什么必须持久化：此前结果只在进程内 `_CACHE`（TTL 30min，重启即丢），
用户「跑完切走再回来」就什么都没有了。

测试注入：所有函数带 ``db`` 参数（上下文管理器工厂），默认用真实 backtest.db；
单测传临时库工厂即可，不污染生产库。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "output" / "factor_reports"


def _default_db():
    from utils.cache.backtest_db import get_backtest_db
    return get_backtest_db()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _rel_or_abs(p: Path) -> str:
    """库内存相对路径（便于整体搬迁）；不在 ROOT 之下（单测临时目录）则存绝对路径。

    ⚠️ 必须存 POSIX 斜杠（``as_posix()``）：Windows 下默认反斜杠，DB 带着走
    到 Linux 后 ``_abs`` 会把整串当单文件名 → load_payload 永远 None（S1 审计 advisory 5）。
    """
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _kpi_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    """列表页摘要：只留一眼能分辨「这次跑的是什么」的字段。"""
    ranking = payload.get("ranking") or []
    sig = payload.get("significance") or {}
    top = next((r for r in ranking if r.get("rank_ic_mean") is not None), None)
    return {
        "top_factor": top["name"] if top else None,
        "top_factor_id": top["factor_id"] if top else None,
        "top_rank_ic": top.get("rank_ic_mean") if top else None,
        "dsr_pass": sum(1 for r in ranking if (r.get("dsr") or 0) >= 0.95),
        "n_factors": len(ranking),
        "n_trials": sig.get("n_trials"),
    }


def save_report(*, payload: Dict[str, Any], params: Dict[str, Any], meta: Dict[str, Any],
                html: Optional[str] = None, db: Optional[Callable] = None) -> str:
    """保存一次评估结果，返回 report_uuid。

    幂等性不做在 uuid 上（每次运行都是新记录），由调用方决定是否先删旧档。
    """
    from uuid import uuid4

    cm = db or _default_db
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_uuid = f"FR-{stamp}-{uuid4().hex[:6]}"

    payload_path = REPORT_DIR / f"{report_uuid}.json"
    _write_json(payload_path, payload)
    html_path = REPORT_DIR / f"{report_uuid}.html"
    if html:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
    else:
        html_path = None

    summary = _kpi_summary(payload)
    cov = payload.get("coverage") or {}
    row = {
        "report_uuid": report_uuid,
        "title": f"{cov.get('evaluated', 0)} 因子 · {meta.get('index_code', '')}",
        "index_code": meta.get("index_code", ""),
        "start_date": meta.get("start"),
        "end_date": meta.get("end"),
        "params_json": json.dumps(params, ensure_ascii=False),
        "kpi_json": json.dumps(summary, ensure_ascii=False),
        "payload_path": _rel_or_abs(payload_path),
        "html_path": _rel_or_abs(html_path) if html_path else None,
        "n_factors": int(cov.get("evaluated", 0)),
        "status": "done",
        "created_at": _now(),
    }
    with cm() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO factor_report "
            "(report_uuid,title,index_code,start_date,end_date,params_json,kpi_json,"
            "payload_path,html_path,n_factors,status,created_at) "
            "VALUES (:report_uuid,:title,:index_code,:start_date,:end_date,:params_json,"
            ":kpi_json,:payload_path,:html_path,:n_factors,:status,:created_at)", row)
    return report_uuid


def list_reports(limit: int = 50, db: Optional[Callable] = None) -> List[Dict[str, Any]]:
    """历史报告列表（新→旧），带摘要，不带 payload。"""
    cm = db or _default_db
    with cm() as conn:
        rows = conn.execute(
            "SELECT * FROM factor_report ORDER BY created_at DESC, report_uuid DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["kpi"] = json.loads(d.pop("kpi_json") or "{}")
        d["params"] = json.loads(d.pop("params_json") or "{}")
        # payload 文件是否还在：清理过/搬过目录的报告点开只会报错，
        # 前端据此置灰并提示，不让用户点进去踩空（2026-10-18）。
        p = _abs(d.get("payload_path"))
        d["available"] = bool(p and p.exists())
        out.append(d)
    return out


def get_report(report_uuid: str, db: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
    cm = db or _default_db
    with cm() as conn:
        row = conn.execute("SELECT * FROM factor_report WHERE report_uuid = ?",
                           (report_uuid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["kpi"] = json.loads(d.pop("kpi_json") or "{}")
    d["params"] = json.loads(d.pop("params_json") or "{}")
    return d


def _abs(rel: Optional[str]) -> Optional[Path]:
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def load_payload(report_uuid: str, db: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
    """读完整 payload；文件缺失返回 None（调用方显示「数据已清理」，不空白页）。"""
    row = get_report(report_uuid, db)
    if not row:
        return None
    p = _abs(row.get("payload_path"))
    if not p or not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_html(report_uuid: str, db: Optional[Callable] = None) -> Optional[str]:
    row = get_report(report_uuid, db)
    if not row:
        return None
    p = _abs(row.get("html_path"))
    if not p or not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def delete_report(report_uuid: str, db: Optional[Callable] = None) -> bool:
    """删记录 + 删文件。文件删除失败（被占用/已清）不阻断——记录删掉即不可见。"""
    cm = db or _default_db
    row = get_report(report_uuid, db)
    if not row:
        return False
    with cm() as conn:
        conn.execute("DELETE FROM factor_report WHERE report_uuid = ?", (report_uuid,))
    for key in ("payload_path", "html_path"):
        p = _abs(row.get(key))
        if p and p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    return True
