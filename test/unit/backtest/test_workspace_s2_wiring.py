"""工作台 v3 · S2 接线测试（全屏 + 参数可读化）。

S1 审计的教训：「静态接线」类断言专抓那种 JS 静默失效的 bug——
改动面大、靠人眼回归不现实，就把它钉进测试。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BT_CSS = ROOT / "server/static/css/backtest.css"
BT_JS = ROOT / "server/static/js/backtest.js"
SR_CSS = ROOT / "server/static/css/strategy-research.css"
SR_JS = ROOT / "server/static/js/strategy-research.js"
FACTOR_CSS = ROOT / "server/static/css/factor.css"
INDEX = ROOT / "server/static/index.html"
TMPL_DIR = ROOT / "backtest/strategies/json_templates"

_EMOJI = re.compile(
    "[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\uFE00-\uFE0F"
    "\U0001F000-\U0001F02F\U0001FA00-\U0001FAFF\u200D\u20E3]"
)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── F4 全屏 ──────────────────────────────────────────────

def test_three_panels_use_fullscreen_width():
    assert "min(1600px, 98vw)" in _text(BT_CSS) or "1600px" in _text(BT_CSS)
    assert "min(1600px, 98vw)" in _text(SR_CSS)
    assert "min(1600px, 98vw)" in _text(FACTOR_CSS)
    assert "min(1180px" not in _text(SR_CSS), "策略研究还卡在旧的 1180px"


def test_editor_mode_toggles_in_backtest_js():
    js = _text(BT_JS)
    css = _text(BT_CSS)
    assert "bt-editor-mode" in css, "CSS 缺少编辑器模式样式"
    assert "bt-editor-mode #btStrategyList" in css, "编辑器模式下策略列表必须让位"
    assert js.count("bt-editor-mode") >= 2, "JS 需在 showEditor 加、closeEditor 删"


def test_sr_has_maximize_button():
    sr = _text(SR_JS)
    css = _text(SR_CSS)
    assert "sr-max-btn" in sr, "策略研究缺最大化按钮"
    assert 'sr-max' in sr, "缺最大化 class 切换"
    assert "<svg" in sr.split("sr-max-btn")[1][:400], "最大化按钮必须是内联 SVG（P0 禁字符图标）"
    # v4：默认全屏（研究内容多，浮窗装不下），按钮改为「全屏 ↔ 浮窗」切换
    assert "sr-full" in sr and "sr-full" in css, "缺全屏形态 class"
    assert "100vw" in css.split(".sr-modal.sr-full")[1][:200], "全屏形态必须是 100vw / 100vh"
    assert "this.modalEl.classList.add(\"sr-full\")" in sr, "open() 必须默认全屏"


def test_sr_embeds_kline_with_trade_points():
    """用户诉求：「为什么不像回测那样显示买点卖点」——研究页必须内嵌 K 线。"""
    sr = _text(SR_JS)
    css = _text(SR_CSS)
    assert "srKline" in sr, "缺 K 线容器"
    assert "renderBacktestKline" in sr, "K 线必须复用回测页的渲染函数（不复制第二份配置）"
    assert "backtest_run_id" in sr, "轮次 → 账本 run_id 必须由后端给，前端不拼字符串"
    assert "tradeSeries" in sr, "买卖点需要用可勾选图例的画法"
    # 图高 ≥ 420px：「展示不下」是本轮用户诉求的核心
    m = re.search(r"\.sr-kline\s*\{[^}]*height:\s*(\d+)px", css)
    assert m and int(m.group(1)) >= 420, "K 线卡片高度不足 420px"


# ── F3 参数可读化 ────────────────────────────────────────

def test_indicator_meta_has_help():
    from backtest.json_compiler import get_strategy_meta
    meta = get_strategy_meta()
    assert len(meta["indicators"]) >= 14
    for k in ("ma", "macd", "boll", "rsi", "kdj", "dpo", "cyw"):
        assert meta["indicators"][k].get("help"), f"指标 {k} 缺 help"


def test_all_template_params_have_help_and_recommended():
    """F3 的核心承诺：新人打开任何一个模板，每个参数都有解释和推荐区间。"""
    missing = []
    for p in sorted(TMPL_DIR.glob("*.json")):
        d = json.loads(_text(p))
        for k, v in (d.get("params") or {}).items():
            if not v.get("help") or v.get("recommended") is None:
                missing.append(f"{p.stem}.{k}")
    assert missing == [], f"模板参数缺 help/recommended: {missing}"


def test_builder_renders_help_and_reset():
    js = _text(BT_JS)
    assert "bt-builder-param-help" in js
    assert "恢复默认" in js
    assert "_builderResetParam" in js
    # 改值后要重新判定「超出推荐区间」
    assert "prop === 'value'" in js


def test_versions_bumped_in_index():
    html = _text(INDEX)
    assert "/static/js/backtest.js?v=40" in html
    assert "/static/css/backtest.css?v=26" in html
    assert "/static/js/strategy-research.js?v=49" in html
    assert "/static/css/strategy-research.css?v=46" in html
    assert "/static/js/chart-backtest-kline.js?v=5" in html


# S2 不引入新 emoji。以下为**改动前就存在**的历史符号，替换属独立 P0 清理任务
# （backtest.js 全文件的 ✓/✅/✕/❌/🏆 是删除按钮与状态标记，量大需专项）：
#   - backtest.css 的 ✦/✨（content 伪元素图标）
#   - strategy-research.js 的 ⚠️（免责提示文案）
# S2 新增代码已自查干净（新参数删除按钮改用内联 SVG）。
_EMOJI_ALLOWED = {"✦", "✨", "⚠", "️", "✓", "✅", "✕", "❌", "🏆"}


def test_no_new_emoji_in_touched_frontend():
    for p in (BT_CSS, BT_JS, SR_CSS, SR_JS):
        hits = [h for h in _EMOJI.findall(_text(p)) if h not in _EMOJI_ALLOWED]
        assert hits == [], f"{p.name} 含新增 emoji: {sorted(set(hits))[:5]}"
