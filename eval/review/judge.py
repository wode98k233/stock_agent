"""评审 judge：调用 JUDGE LLM 并归一化输出。

维度清单**从注册表派生**（`eval/dimensions.py`），不再硬编码，
避免 prompt 与维度定义漂移。
"""
from __future__ import annotations

import logging
import math

from config import Config
from utils.llm_factory import get_judge_llm, llm_json_with_retry

from eval.dimensions import PROFILE_DIAGNOSTIC, dimensions_for
from eval.review.rubrics import (
    ATTRIBUTIONS,
    DIALOG_SYSTEM,
    RUN_SYSTEM,
    build_dialog_prompt,
    build_run_prompt,
)

logger = logging.getLogger("eval.review")

_DIMENSIONS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "run"))
_DIALOG_DIMENSIONS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "dialog"))
_ATTRIBUTION_UNKNOWN = "unknown"


def judge_model_name() -> str:
    return Config.JUDGE_LLM_MODEL or Config.OPENAI_MODEL_NAME or "unknown"


def _clamp(value, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return max(0.0, min(100.0, round(num, 2)))


def _as_str_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return []


def _normalize_evidence(value) -> list:
    """归一化 findings 的 evidence 引用（source / quote / step_ref / turn_index）。"""
    if not isinstance(value, list):
        return []
    out = []
    for e in value:
        if not isinstance(e, dict):
            continue
        item = {"source": str(e.get("source") or ""), "quote": str(e.get("quote") or "")}
        for key in ("step_ref", "turn_index"):
            if e.get(key) is None:
                continue
            try:
                item[key] = int(e[key])
            except (TypeError, ValueError):
                continue
        out.append(item)
    return out


def _normalize_attribution(value) -> str:
    """归因标签；不在允许清单内的归入 unknown（便于「改进清单」聚合时暴露异常值）。"""
    attr = str(value or "").strip()
    return attr if attr in ATTRIBUTIONS else _ATTRIBUTION_UNKNOWN


def _normalize_finding(item: dict, *, with_step: bool, with_turn: bool) -> dict:
    f: dict = {}
    if with_step:
        try:
            f["step_ref"] = int(item.get("step_ref") or 0)
        except (TypeError, ValueError):
            f["step_ref"] = 0
    if with_turn:
        try:
            f["turn_index"] = int(item.get("turn_index") or 0)
        except (TypeError, ValueError):
            f["turn_index"] = 0
    f["dimension"] = str(item.get("dimension") or "")
    f["verdict"] = str(item.get("verdict") or "")
    f["evidence"] = _normalize_evidence(item.get("evidence"))
    f["attribution"] = _normalize_attribution(item.get("attribution"))
    f["issues"] = _as_str_list(item.get("issues"))
    f["suggestions"] = _as_str_list(item.get("suggestions"))
    # 校验层会覆盖；judge 自报的 unverified 不作数（防 judge 自我豁免）
    f["unverified"] = False
    return f


def normalize_result(result) -> dict:
    if not isinstance(result, dict):
        raise ValueError("judge 输出不是 JSON 对象")
    dims_in = result.get("dimensions")
    if dims_in is None:
        dims_in = {}
    elif not isinstance(dims_in, dict):
        raise ValueError("judge 输出 dimensions 不是 JSON 对象")
    dimensions = {name: _clamp(dims_in.get(name), 0.0) for name in _DIMENSIONS}
    findings_in = result.get("findings")
    if findings_in is None:
        findings_in = []
    elif not isinstance(findings_in, list):
        raise ValueError("judge 输出 findings 不是 JSON 数组")
    findings = [_normalize_finding(item, with_step=True, with_turn=False)
                for item in findings_in if isinstance(item, dict)]
    overall = result.get("overall")
    if overall is None:
        vals = list(dimensions.values())
        overall = round(sum(vals) / len(vals), 2) if vals else 0.0
    return {
        "overall": _clamp(overall),
        "dimensions": dimensions,
        "capabilities_required": _as_str_list(result.get("capabilities_required")),
        "findings": findings,
        "summary": str(result.get("summary") or ""),
    }


def judge_run(digest: dict, llm=None, logger_obj=None, feedback: str = "") -> dict:
    """`feedback` 非空时作为「上次输出被驳回的原因」追加到 prompt（带反馈重试）。"""
    log = logger_obj or logger
    llm = llm if llm is not None else get_judge_llm()
    prompt = build_run_prompt(digest)
    if feedback:
        prompt += "\n\n## 上次输出被驳回的原因（请修正后重新输出完整 JSON）\n" + feedback
    messages = [("system", RUN_SYSTEM), ("user", prompt)]
    result = llm_json_with_retry(llm, messages, log, label="trace_review", max_retries=2)
    if result is None:
        raise ValueError("judge 输出无法解析为 JSON")
    return normalize_result(result)


def normalize_dialog_result(result) -> dict:
    if not isinstance(result, dict):
        raise ValueError("judge 输出不是 JSON 对象")
    dims_in = result.get("dimensions")
    if dims_in is None:
        dims_in = {}
    if not isinstance(dims_in, dict):
        raise ValueError("dimensions 必须是对象")
    dimensions = {name: _clamp(dims_in.get(name), 0.0) for name in _DIALOG_DIMENSIONS}
    findings_in = result.get("findings")
    if findings_in is None:
        findings_in = []
    if not isinstance(findings_in, list):
        raise ValueError("findings 必须是数组")
    findings = [_normalize_finding(item, with_step=False, with_turn=True)
                for item in findings_in if isinstance(item, dict)]
    overall = result.get("overall")
    if overall is None:
        vals = list(dimensions.values())
        overall = round(sum(vals) / len(vals), 2) if vals else 0.0
    return {
        "overall": _clamp(overall),
        "dimensions": dimensions,
        "findings": findings,
        "summary": str(result.get("summary") or ""),
    }


def judge_dialog(digest: dict, llm=None, logger_obj=None, feedback: str = "") -> dict:
    log = logger_obj or logger
    llm = llm if llm is not None else get_judge_llm()
    prompt = build_dialog_prompt(digest)
    if feedback:
        prompt += "\n\n## 上次输出被驳回的原因（请修正后重新输出完整 JSON）\n" + feedback
    messages = [("system", DIALOG_SYSTEM), ("user", prompt)]
    result = llm_json_with_retry(llm, messages, log, label="trace_review_dialog", max_retries=2)
    if result is None:
        raise ValueError("judge 输出无法解析为 JSON")
    return normalize_dialog_result(result)
