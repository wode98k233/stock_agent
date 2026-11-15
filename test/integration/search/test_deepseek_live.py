# -*- coding: utf-8 -*-
"""DeepSeek 官方 Search / Fetch 真网集成测试（2026-10-18 新增）。

⚠️ **会真的花钱**：每次成功调用 ≈ ¥0.005（¥5/1000 次）。所以本文件刻意只保留 2 个请求，
且必须显式提供 `DEEPSEEK_API_KEYS` 才会跑（`pytest test/integration/` 默认在未配置时全部跳过）。

跑法：
    python -m pytest test/integration/search/test_deepseek_live.py -v -s
"""
import os

import pytest


def _has_deepseek_key() -> bool:
    return bool((os.getenv("DEEPSEEK_API_KEYS") or "").strip())


pytestmark = pytest.mark.skipif(not _has_deepseek_key(),
                                reason="需要 DEEPSEEK_API_KEYS（真实调用会按次计费）")


def test_live_search_returns_billed_results():
    """真搜一次：能拿到结果，且计费声明透传到载荷里。"""
    from tools.search.deepseek_provider import DeepSeekSearchProvider
    from tools.search.service import normalize_search_response

    key = (os.getenv("DEEPSEEK_API_KEYS") or "").split(",")[0].strip()
    resp = DeepSeekSearchProvider([key]).search("贵州茅台 2026 年半年度报告", max_results=3)
    print(f"\n[live] provider={resp.provider} success={resp.success} "
          f"n={len(resp.results)} err={resp.error_message}")

    if not resp.success or not resp.results:
        pytest.skip(f"上游未返回结果（不计费）：{resp.error_message}")

    payload = normalize_search_response(resp, "贵州茅台 2026 年半年度报告")
    assert payload["billed_service"] == "deepseek-search"
    first = payload["results"][0]
    assert first["url"].startswith("http")
    assert first["title"]
    print(f"[live] 首条：{first['title']} | {first['url']} | {first['published_date']}")


def test_live_fetch_returns_markdown():
    """真抓一次：拿到 Markdown 正文，且只有拿到正文才 billed=True。"""
    from tools.search.deepseek_fetch import fetch_web_page

    out = fetch_web_page("https://api-docs.deepseek.com/zh-cn/guides/search", max_chars=2000)
    print(f"\n[live] fetch success={out['success']} billed={out['billed']} "
          f"chars={out['char_count']} err={out.get('error')}")

    if not out["success"]:
        pytest.skip(f"上游未返回正文（不计费）：{out.get('error')}")

    assert out["billed"] is True
    assert out["content"]
    assert len(out["content"]) <= 2000
