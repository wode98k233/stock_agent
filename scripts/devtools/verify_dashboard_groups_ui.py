"""仪表盘 groups 渲染验收：market-like 定义（无 group_cards section）+ 真实 chat-dashboard.js（Playwright）。

3 张卡必须来自前端「数据含 groups 且定义未声明 section」时的通用注入，
同时校验 signal_row 的「整体判断」标题。市场定义没有 group_cards，通过即证明通用化生效。

边界（防回归）：
- 同一 DATA 二次渲染到另一个容器：仍 3 卡，且定义缓存 sections 未被追加 group_cards；
- 0 组：不注入卡片区、无「方向决策」；
- 1 组：1 张卡，signal_row 回落「决策信号」（多组才叫「整体判断」）。

运行：.\\venv\\Scripts\\python.exe scripts\\devtools\\verify_dashboard_groups_ui.py
成功：打印 ok 3 ... 并生成 output/_verify_dashboard_groups.png
"""
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = ROOT / "server/static/js/chat-dashboard.js"
CSS = ROOT / "server/static/css/dashboard.css"
OUT = ROOT / "output/_verify_dashboard_groups.png"

MARKET_DEF = {
    "id": "market", "display_name": "市场决策仪表盘",
    # 故意只声明 signal_row + verdict_text：market.json 没有 group_cards section，
    # 3 张卡必须由前端「数据带 groups 且定义未声明」时的通用注入产生。
    "sections": [
        {"id": "signal_row", "renderer": "signal_row", "required": True,
         "fields": ["decision_type", "confidence_level", "sentiment_score"]},
        {"id": "core_verdict", "renderer": "verdict_text", "required": True,
         "fields": ["core_verdict"]},
    ],
}

DATA = {
    "dashboard_id": "market", "scenario_tag": "市场决策仪表盘",
    "decision_type": "buy", "confidence_level": 0.62, "sentiment_score": 68,
    "trend_prediction": "bullish", "quality_tag": "完整分析",
    "core_verdict": "科技（1-5天）/ 消费（1-2月）/ 医疗（1-2年）三方向均可参与，条件各异。",
    "groups": [
        {"key": "科技", "horizon": "1-5天", "decision": "buy", "confidence": 0.66,
         "stance": "可参与但只低吸", "signal_symbol": "159516",
         "note": "回踩不破 0.456 可分批，勿追高",
         "candidates": [{"code": "159516", "name": "半导体设备ETF", "note": "主标的"},
                        {"code": "588000", "name": "科创50ETF", "note": "备选"}],
         "position": "≤10%", "entry": {"type": "pullback", "low": 0.46, "high": 0.47},
         "invalidation": "破0.456"},
        {"key": "消费", "horizon": "1-2个月", "decision": "buy", "confidence": 0.55,
         "stance": "方向成立但今天不是加仓点", "signal_symbol": "159928",
         "candidates": [{"code": "159928", "name": "消费ETF"}],
         "position": "≤12%", "entry": {"type": "breakout", "low": 0.69},
         "invalidation": "破0.676"},
        {"key": "医疗", "horizon": "1-2年", "decision": "buy", "confidence": 0.70,
         "stance": "长线最优", "signal_symbol": "513060",
         "candidates": [{"code": "513060", "name": "恒生医疗ETF"}],
         "position": "上限20%", "entry": {"type": "pullback", "high": 0.50},
         "invalidation": "破0.485"},
    ],
}


def _render(page, container_id: str, data: dict) -> None:
    page.evaluate(
        "(args) => document.getElementById(args.id).appendChild("
        "window.StockRadar.chatDashboard.renderDashboardCard(args.data))",
        {"id": container_id, "data": data})


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1000})
        page.set_content(
            "<html><body>"
            "<div id='root'></div><div id='root2'></div>"
            "<div id='root3'></div><div id='root4'></div>"
            "</body></html>")
        page.add_style_tag(path=str(CSS))
        page.evaluate(
            """(defs) => {
                // 直接持有缓存里的定义对象：渲染后回读，证明通用注入没有污染缓存
                window.__defs = defs;
                window.__defSectionsLen = defs[0].sections.length;
                window.__defsResolved = false;
                window.StockRadar = { utils: { esc: (s) => String(s ?? '').replace(
                    /[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])) } };
                window.fetch = () => Promise.resolve({ ok: true, json: () => {
                    window.__defsResolved = true;
                    return Promise.resolve({ dashboards: defs });
                } });
            }""", [MARKET_DEF])
        page.add_script_tag(path=str(JS))
        page.wait_for_function("window.StockRadar && window.StockRadar.chatDashboard")
        page.wait_for_function("() => window.__defsResolved")
        page.evaluate(
            "(data) => document.getElementById('root').appendChild("
            "window.StockRadar.chatDashboard.renderDashboardCard(data))", DATA)
        page.wait_for_function(
            "document.querySelectorAll('.dash-group-card').length === 3", timeout=5000)
        cards = page.locator(".dash-group-card").count()
        assert cards == 3, f"expected 3 group cards, got {cards}"
        title = page.locator(".dash-sig-label").first.inner_text()
        assert title == "整体判断", f"expected 整体判断, got {title}"
        feet = page.locator(".dash-group-foot").all_inner_texts()
        assert any("回踩 0.46–0.47" in t for t in feet), feet
        assert any("站稳 0.69" in t for t in feet), feet
        assert any("回踩 ≤0.5" in t for t in feet), feet
        assert "66%" in page.locator(".dash-group-decision").first.inner_text()
        notes = page.locator(".dash-group-note").all_inner_texts()
        assert notes and notes[0].startswith("备注："), notes
        page.screenshot(path=str(OUT), full_page=True)

        # ── 边界 1：同一 DATA 二次渲染到另一个容器 → 仍 3 卡，定义缓存不被污染 ──
        _render(page, "root2", DATA)
        assert page.locator("#root .dash-group-card").count() == 3, "二次渲染影响了第一次的容器"
        assert page.locator("#root2 .dash-group-card").count() == 3
        assert page.evaluate("() => window.__defSectionsLen") == len(MARKET_DEF["sections"]), \
            "渲染改写了定义缓存里的 sections 长度"
        assert page.evaluate(
            "() => window.__defs[0].sections.some((s) => s.renderer === 'group_cards')"
        ) is False, "通用注入往定义缓存 sections 里塞了 group_cards"

        # ── 边界 2：0 组 → 不注入卡片区，也不该出现「方向决策」标题 ──
        _render(page, "root3", {**DATA, "groups": []})
        assert page.locator("#root3 .dash-group-card").count() == 0
        assert "方向决策" not in page.locator("#root3 .dashboard-card").inner_text()
        assert page.locator("#root3 .dash-sig-label").first.inner_text() == "决策信号"

        # ── 边界 3：1 组 → 1 张卡；signal_row 回落「决策信号」（多组才叫「整体判断」）──
        _render(page, "root4", {**DATA, "groups": [DATA["groups"][0]]})
        assert page.locator("#root4 .dash-group-card").count() == 1
        assert page.locator("#root4 .dash-sig-label").first.inner_text() == "决策信号"

        print("ok", cards, "二次渲染/0组/1组断言通过", OUT)
        browser.close()


if __name__ == "__main__":
    main()
