"""深度报告模板加载（C9：章节结构在 JSON 里，代码不写死）。

模板目录：agents/report_templates/deep/*.json
与既有 v2.0 模板（output_blocks/data_contract）并存，但 template_id 必须带 `deep_` 前缀，
避免与 stock_deep_dive / sector_landscape 等既有 id 撞名（评审 P2-3）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = ROOT / "agents" / "report_templates" / "deep"

REQUIRED_KEYS = ("template_id", "variant", "name", "sections")
_cache: Dict[str, Dict[str, Any]] = {}


class TemplateError(ValueError):
    """模板缺失/损坏 —— 明确报错，不静默回落默认模板。"""


def _validate(data: Dict[str, Any], src: Path) -> Dict[str, Any]:
    missing = [k for k in REQUIRED_KEYS if not data.get(k)]
    if missing:
        raise TemplateError(f"{src.name} 缺少字段: {missing}")
    tid = str(data["template_id"])
    if not tid.startswith("deep_"):
        raise TemplateError(f"{src.name}: template_id 必须以 deep_ 开头（避免与既有模板撞名），实际 {tid!r}")
    for i, sec in enumerate(data["sections"]):
        if not sec.get("id") or not sec.get("title"):
            raise TemplateError(f"{src.name}: 第 {i + 1} 个章节缺 id/title")
    if not data["sections"]:
        raise TemplateError(f"{src.name}: sections 为空")
    return data


def load_template(template_id: str = "deep_sector_finance", *, reload: bool = False) -> Dict[str, Any]:
    """按 id 加载模板；找不到抛 TemplateError（不静默降级）。"""
    if not reload and template_id in _cache:
        return _cache[template_id]
    path = TEMPLATE_DIR / f"{template_id}.json"
    if not path.exists():
        available = [p.stem for p in TEMPLATE_DIR.glob("*.json")] if TEMPLATE_DIR.exists() else []
        raise TemplateError(f"模板不存在: {template_id}（可用: {available}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TemplateError(f"模板 JSON 非法 {path.name}: {e}") from e
    _validate(data, path)
    _cache[template_id] = data
    return data


def list_templates() -> List[str]:
    if not TEMPLATE_DIR.exists():
        return []
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.json"))


def section_specs(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    """模板章节 → 内部 spec（补齐默认值，顺序即目录）。"""
    out: List[Dict[str, Any]] = []
    for i, sec in enumerate(template.get("sections") or [], start=1):
        tw = sec.get("target_words") or [0, 0]
        out.append({
            "seq": i,
            "section_id": str(sec["id"]),
            "title": str(sec["title"]),
            "target_words": [int(tw[0] or 0), int(tw[1] or 0)] if isinstance(tw, (list, tuple)) else [0, 0],
            "required": bool(sec.get("required", True)),
            "data_slots": list(sec.get("data_slots") or []),
            "charts": list(sec.get("charts") or []),
            "supply": list(sec.get("supply") or []),
            "prompt": str(sec.get("prompt") or ""),
        })
    return out


def total_words(template: Dict[str, Any]) -> List[int]:
    tw = template.get("total_words_target") or [0, 0]
    return [int(tw[0] or 0), int(tw[1] or 0)]
