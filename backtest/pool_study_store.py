"""池子级研究持久化（2026-10-18）。

为什么需要它
------------
研究结果此前只存在 server/routes/strategy_research.py 的进程内字典
_POOL_STUDIES 里 —— **重启即丢，也没有任何页面能看到**。用户「跑完切走再回来
就什么都没有了」，跟因子报告当年踩的是同一个坑（见 factor_store 模块头）。

分层（照抄 factor_store 的做法）
--------------------------------
- **文件**：output/pool_studies/<study_id>.json，一条研究一个文件。
  报告含 300 条标的明细 + 行业表，进库会让列表页和备份一起变重，所以走文件。
- **内存**：只保存「正在跑」的进度；跑完落盘后即可从文件读回。

口径
----
- 列表只回**摘要**（不含 items），列表页开销固定；
- 文件损坏/被删 → 明确跳过或如实报错，**不返回空壳**（数据缺失 ≠ 0）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
STUDY_DIR = ROOT / "output" / "pool_studies"

# 列表页摘要只取这些键，避免把 300 条明细带进列表响应
_SUMMARY_KEYS = ("study_id", "run_uuid", "index_code", "status", "error",
                 "created_at", "finished_at", "period", "universe",
                 "verdict", "overall", "elapsed_sec")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def path_of(study_id: str) -> Path:
    return STUDY_DIR / (str(study_id) + ".json")


def save_study(study_id: str, *, status: str, run_uuid: str = "",
               index_code: str = "", report: Optional[Dict[str, Any]] = None,
               error: str = "", created_at: str = "",
               elapsed_sec: Optional[float] = None) -> Optional[Path]:
    """落盘一条研究（跑完/失败都要落，否则页面看不到失败记录）。"""
    payload: Dict[str, Any] = {
        "study_id": str(study_id),
        "run_uuid": str(run_uuid or ""),
        "index_code": str(index_code or ""),
        "status": str(status),
        "error": str(error or ""),
        "created_at": created_at or _now(),
        "finished_at": _now(),
    }
    if elapsed_sec is not None:
        payload["elapsed_sec"] = round(float(elapsed_sec), 1)
    if report:
        # 报告的 period/universe/verdict/overall 提到顶层，列表页不必解包
        for k in ("period", "universe", "verdict", "overall"):
            if report.get(k) is not None:
                payload[k] = report[k]
        payload["report"] = report
    try:
        STUDY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path_of(study_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path_of(study_id))     # 原子替换，避免读到半截文件
        return path_of(study_id)
    except Exception:  # noqa: BLE001 - 落盘失败不能影响研究本身
        logger.debug("池子研究落盘失败", exc_info=True)
        return None


def load_study(study_id: str) -> Optional[Dict[str, Any]]:
    """读回一条研究；文件不存在/损坏返回 None。"""
    p = path_of(study_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def list_studies(limit: int = 50) -> List[Dict[str, Any]]:
    """列表（新→旧，只含摘要）。按 finished_at 排序，缺字段回退 created_at。"""
    if not STUDY_DIR.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for p in STUDY_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件跳过，不让整个列表挂掉
            continue
        rows.append({k: d.get(k) for k in _SUMMARY_KEYS if k in d})
    # 排序键必须是**组合**的：finished_at 精度到秒，同一秒内落盘的多条会并列，
    # 只按它会返回不确定顺序（实测两次保存同秒 → 列表顺序随机）。
    # 依次用 finished_at → created_at → study_id 做降序，保证结果可复现。
    rows.sort(key=lambda x: (str(x.get("finished_at") or ""),
                             str(x.get("created_at") or ""),
                             str(x.get("study_id") or "")),
              reverse=True)
    return rows[:max(1, int(limit))]


def delete_study(study_id: str) -> bool:
    p = path_of(study_id)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False
