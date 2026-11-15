"""策略意图词表配置服务（设计文档 §5.2.3 / 需求单 R3）。

职责：校验 → 备份 → 原子写 → 热重载。**不含任何判定逻辑**——判定只在
agents/shared/strategy_intent.py（单一事实源），本模块只搬运与校验。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agents.shared import strategy_intent as si

_RULES_PATH = Path(si.__file__).with_name("strategy_intent_rules.json")
_BACKUP_DIR = Path(__file__).resolve().parents[1] / "data" / "backups"


def read_rules() -> Dict[str, Any]:
    """读盘上的原始词表（不走缓存——配置页要看真实内容）"""
    with open(_RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_rules(rules: Any) -> Dict[str, Any]:
    """校验 → 备份 → 原子写 → 热重载；非法结构抛 ValueError（路由层转 400）。"""
    errs: List[str] = si.validate_rules(rules)
    if errs:
        raise ValueError("；".join(errs))
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = _BACKUP_DIR / ("strategy_intent_rules." + stamp + ".json")
    shutil.copy2(_RULES_PATH, backup)
    tmp = _RULES_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # 固定 LF，避免保存后整文件行尾翻转
        f.write(json.dumps(rules, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(_RULES_PATH)  # 原子替换：避免半截文件被判定函数读到
    si.reload_rules()  # 预览 / 闸门 / test 三处同时生效，免重启
    return {"success": True, "backup": str(backup)}


def test_input(text: str) -> Dict[str, Any]:
    """检测一句话：**与闸门同一个函数**（禁止复刻判定逻辑）"""
    verdict = si.detect_strategy_intent(text or "")
    sig = verdict.get("signals") or {}
    return {
        "is_strategy": bool(verdict.get("is_strategy")),
        "disambiguate": bool(verdict.get("disambiguate")),
        "confidence": float(verdict.get("confidence") or 0.0),
        "reason": verdict.get("reason") or "",
        "hits": {k: (sig.get(k) or []) for k in
                 ("imperatives", "intent_words", "condition_words", "entry_words", "exit_words")},
        "question_marker": bool(sig.get("question_marker")),
        "slots": verdict.get("slots") or {},
    }
