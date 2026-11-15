"""条件单区块渲染验收：静态页 + 真实 signal-eval.js（Playwright）。

运行：.\\venv\\Scripts\\python.exe scripts\\devtools\\verify_conditional_ui.py
成功：打印 ok 3 并生成 output/_verify_conditional_ui.png
"""
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = ROOT / "server/static/js/signal-eval.js"
OUT = ROOT / "output/_verify_conditional_ui.png"

DATA = {
    "count": 3, "by_type": {"pullback": 2, "breakout": 1, "unverifiable": 0},
    "triggered": 1, "not_triggered": 1, "pending": 1, "trigger_rate": 0.5,
    "avg_days_to_trigger": 2.0,
    "items": [
        {"symbol_key": "sz159516", "signal_time": "2026-11-04", "decision": "buy",
         "entry_type": "pullback", "entry_low": 0.46, "entry_high": 0.47,
         "horizon_days": 5, "trigger_status": "triggered",
         "trigger_date": "2026-11-06", "days_to_trigger": 2, "group_key": "科技"},
        {"symbol_key": "sz159928", "signal_time": "2026-11-04", "decision": "buy",
         "entry_type": "breakout", "entry_low": 0.69, "entry_high": None,
         "horizon_days": 20, "trigger_status": "not_triggered",
         "trigger_date": None, "days_to_trigger": None, "group_key": "消费"},
        {"symbol_key": "sh513060", "signal_time": "2026-11-04", "decision": "buy",
         "entry_type": "pullback", "entry_low": None, "entry_high": 0.50,
         "horizon_days": 250, "trigger_status": "pending",
         "trigger_date": None, "days_to_trigger": None, "group_key": "医疗"},
    ],
}


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 700})
        page.set_content(
            "<html><body><div id='btSignalEvalView'><div id='seConditional'></div>"
            "</div></body></html>")
        page.add_style_tag(path=str(ROOT / "server/static/css/variables.css"))
        page.add_style_tag(path=str(ROOT / "server/static/css/signal-eval.css"))
        page.add_script_tag(path=str(JS))
        page.evaluate(
            "(d) => { window._SignalEval._data.conditional = d;"
            " window._SignalEval.renderConditional(); }", DATA)
        page.wait_for_function(
            "document.querySelectorAll('#seConditional .se-cond-row').length === 3",
            timeout=5000)
        rows = page.locator("#seConditional tr.se-cond-row").count()
        assert rows == 3, f"expected 3 conditional rows, got {rows}"
        text = page.locator("#seConditional").inner_text()
        assert "回踩 0.46–0.47" in text
        assert "涨破 0.69" in text
        assert "回踩 ≤0.5" in text
        assert "待定" in text and "不可判定" in text
        assert page.locator("#seConditional .se-stat").count() == 6
        assert "50%" in text and "科技" in text
        page.screenshot(path=str(OUT), full_page=True)
        print("ok", rows, OUT)
        browser.close()


if __name__ == "__main__":
    main()
