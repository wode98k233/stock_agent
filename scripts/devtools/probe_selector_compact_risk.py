# -*- coding: utf-8 -*-
"""select_skills 紧凑化的**离线风险评估**（零 LLM 调用，PRD-A11 FR-5.1）。

为什么不需要真实 LLM 的 A/B：紧凑化只砍 ``sections[].prompt`` 正文，
而「选哪些技能」这件事**不依赖**它。两条可机器复验的结构性证据（覆盖全部模板）：

  证据① 技能名的出现位置全部另有出处
        —— 若某技能只在 ``sections[].prompt`` 正文里被提到，紧凑化就真的会丢信息。
  证据② ``sections[].prompt`` 正文里**零处**提到工具名 / 数据源 / "调用·获取·查询"
        —— 这类措辞才是指向技能的语义；正文里没有，说明它写的是"报告怎么写"
        （篇幅 / 角度 / 口径），属报告正文层，与技能选择无关。

用法：
    python scripts/devtools/probe_selector_compact_risk.py
    python scripts/devtools/probe_selector_compact_risk.py --verbose
退出码：0 = 两项证据都成立（紧凑化可安全启用）；1 = 存在风险模板，需人工看。
"""
from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE_DIR = ROOT / "agents" / "report_templates"

# 判定"这段正文在讲技能/数据源"的措辞（命中任一即视为有决策价值）
SKILL_HINTS = ("mx_", "iwc_", "hithink", "同花顺", "问财",
               "工具", "skill", "调用", "获取", "查询", "数据源", "接口")

# 技能名的候选池（模板里只会用到这些）
SKILL_POOL = (
    "mx_data", "mx_search", "mx_xuangu", "money_flow", "market_review",
    "stock_analysis", "fund_query", "portfolio_query", "gold_env", "macro_data",
    "web_search", "hithink_market_query", "hithink_news", "iwc_query", "iwc_search",
    "etf_data", "bond_data", "dividend_env", "cu_regime", "hog_regime",
    "hang_seng_tech", "technical_analysis", "sentiment_analysis",
)


def _templates() -> list[tuple[str, dict]]:
    out = []
    for f in sorted(TEMPLATE_DIR.glob("*/template.json")):
        try:
            out.append((f.parent.name, json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:
            print(f"⚠️ 读取失败 {f}: {e}")
    return out


def _digest_keeps(template: dict) -> set[str]:
    """紧凑化后仍然保留的文本（= build_template_digest 覆盖的字段）。"""
    parts = [json.dumps({k: v for k, v in template.items() if k != "sections"},
                        ensure_ascii=False)]
    for sec in template.get("sections") or []:
        if isinstance(sec, dict):
            parts.append(json.dumps({k: v for k, v in sec.items() if k != "prompt"},
                                    ensure_ascii=False))
    return set("".join(parts).split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    tpls = _templates()
    if not tpls:
        print("没找到模板")
        return 1

    print(f"扫描 {len(tpls)} 个模板\n")

    risky: list[tuple[str, list[str]]] = []      # 证据①不成立
    hinted: list[tuple[str, str, list[str]]] = []  # 证据②不成立

    for name, tpl in tpls:
        kept_blob = "".join(_digest_keeps(tpl))
        dropped_blob = "".join(str(sec.get("prompt") or "")
                               for sec in (tpl.get("sections") or [])
                               if isinstance(sec, dict))

        only_in_prompt = sorted({s for s in SKILL_POOL
                                 if s in dropped_blob and s not in kept_blob})
        if only_in_prompt:
            risky.append((name, only_in_prompt))

        for sec in tpl.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            p = str(sec.get("prompt") or "")
            hit = [h for h in SKILL_HINTS if h in p]
            if hit:
                hinted.append((name, str(sec.get("title") or ""), hit))

    # ── 证据① ──
    print("== 证据① 技能名是否只在 sections[].prompt 正文里出现 ==")
    if risky:
        print(f"  ❌ {len(risky)}/{len(tpls)} 个模板存在「仅正文提到」的技能：")
        for name, skills in risky:
            print(f"     · {name}: {skills}")
        print("  → 紧凑化会丢信息，必须为这些模板保留正文或改契约字段。")
    else:
        print(f"  ✅ {len(tpls)}/{len(tpls)} 个模板的技能名全部另有出处"
              f"（skill_plan / data_contract / required_skills / fallback_skills）")

    # ── 证据② ──
    print("\n== 证据② sections[].prompt 正文是否含指向技能的措辞 ==")
    print(f"  判定词：{'、'.join(SKILL_HINTS)}")
    if hinted:
        print(f"  ❌ {len(hinted)} 处命中：")
        for name, title, hit in hinted[:20]:
            print(f"     · [{name}] {title}  命中={hit}")
    else:
        print(f"  ✅ 零处命中 —— 正文写的是「报告怎么写」，与「要拿什么数据」无关")

    ok = not risky and not hinted
    if args.verbose and not ok:
        print("\n== 命中处的正文片段 ==")
        for name, tpl in tpls:
            for sec in tpl.get("sections") or []:
                p = str(sec.get("prompt") or "")
                if any(h in p for h in SKILL_HINTS):
                    print(f"  [{name}] {sec.get('title')}: {p[:160]}")

    print(f"\n{'✅ 两项证据成立：紧凑化无需真实 LLM 的 A/B' if ok else '❌ 存在风险，需人工确认'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())