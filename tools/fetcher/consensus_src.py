"""研报/机构观点数据源（consensus 能力的 fetcher 实现）。

背景（2026-11-01）：
`consensus` 能力此前只有 `get_stock_rating`（个股评级）与厂商 query 工具兜底，
板块层面没有统一实现。这里把「问财研报检索」封装成**数据层函数**（厂商细节留在
数据层，能力层只声明 `kind=fetcher`），返回结构化记录供上层聚合。

返回记录字段：
    {"org","author","title","date","url","summary","rating","target"}
"""
from __future__ import annotations

import importlib
import json
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_RATING_PATTERNS = (
    ("减持", re.compile(r"减持|卖出|低配|弱于大市|不推荐")),
    ("中性", re.compile(r"中性|持有|标配|同步大市|谨慎推荐|审慎推荐")),
    ("买入", re.compile(r"买入|强烈推荐|推荐|增持|超配|优于大市")),
)
_TARGET = re.compile(r"目标价[位]?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)")


def _parse_date(raw: Any) -> str:
    s = str(raw or "")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else ""


def _rating_of(text: str) -> str:
    for label, pat in _RATING_PATTERNS:
        if pat.search(text or ""):
            return label
    return "未标注"


def _target_of(text: str) -> str:
    m = _TARGET.search(text or "")
    return m.group(1) if m else ""


def fetch_research_consensus(keyword: str, *, days: int = 120, limit: int = 20,
                             search_fn=None) -> List[Dict[str, Any]]:
    """按关键词取卖方研报摘要（带机构/作者/日期/评级）。失败返回空列表。

    `search_fn(query, channel, size) -> payload_text` 可注入（单测离线用）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    try:
        if search_fn is None:
            mod = importlib.import_module("tools.other_skills.10jqka.skills")
            payload = mod._iwc_search_core(f"{kw} 行业 深度报告", "report", int(limit))
        else:
            payload = search_fn(f"{kw} 行业 深度报告", "report", int(limit))
    except Exception as e:  # noqa: BLE001
        logger.warning("研报检索失败 keyword=%s: %s", kw, e)
        return []
    try:
        data = json.loads(payload)
    except Exception:
        logger.info("研报检索返回非 JSON（多为额度/限流），按空处理 keyword=%s", kw)
        return []
    if isinstance(data, dict) and data.get("error"):
        logger.info("研报检索失败 keyword=%s error=%s", kw, data.get("error"))
        return []
    rows = ((data or {}).get("data") or {}).get("data") or []
    out: List[Dict[str, Any]] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        extra = it.get("extra") or {}
        # 评级词常只在正文/摘要里（标题往往不含），source_original 前 600 字已够判定
        text = (f"{it.get('title') or ''} {it.get('summary') or ''} "
                f"{str(it.get('source_original') or '')[:600]}")
        out.append({
            "org": str(extra.get("organization") or ""),
            "author": str(extra.get("author") or ""),
            "title": str(it.get("title") or ""),
            "date": _parse_date(it.get("publish_date")),
            "url": str(it.get("url") or ""),
            "summary": str(it.get("summary") or "")[:400],
            "rating": _rating_of(text),
            "target": _target_of(text),
        })
    # 按日期排序后处理：
    # - 优先近 N 天；
    # - 若近期一篇都没有（问财按相关度返回，常给 1–2 年前的深度报告），
    #   保留最新几篇并标 stale=True —— 让上层写「研报较旧」，而不是假装「没有研报」。
    out.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    if days and int(days) > 0:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        recent = [r for r in out if not r["date"] or r["date"] >= cutoff]
        if recent:
            return recent
        for r in out[:5]:
            r["stale"] = True
        return out[:5]
    return out
