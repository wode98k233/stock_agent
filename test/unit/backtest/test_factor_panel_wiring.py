"""因子面板接线测试：HTML 元素 id 与 JS 取值必须一一对应。

前端 bug 最常见的形态不是逻辑错，而是「JS 找的 id 在 HTML 里不存在」——
静默 null、界面一片空白。这些断言就是防这个。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INDEX = ROOT / "server/static/index.html"
FACTOR_JS = ROOT / "server/static/js/factor.js"
FACTOR_CSS = ROOT / "server/static/css/factor.css"
APP_JS = ROOT / "server/static/js/app.js"

_EMOJI = re.compile(
    "[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\uFE00-\uFE0F"
    "\U0001F000-\U0001F02F\U0001FA00-\U0001FAFF\u200D\u20E3]"
)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# 这些 id 由 JS 动态注入（innerHTML），不要求在静态 HTML 里存在
_DYNAMIC_IDS = {"frElapsed", "frProgFill", "frPhase", "frPct", "frCancel"}


def test_required_dom_ids_present():
    html = _text(INDEX)
    js = _text(FACTOR_JS)
    # 从 JS 里抽出所有 getElementById('xxx')，逐个检查 HTML 是否存在
    ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js)) - _DYNAMIC_IDS
    assert ids, "factor.js 未取任何 DOM id？"
    missing = [i for i in sorted(ids) if f'id="{i}"' not in html]
    assert not missing, f"HTML 缺少 JS 依赖的 id: {missing}"


def test_assets_referenced_with_version():
    html = _text(INDEX)
    assert re.search(r'/static/css/factor\.css\?v=\d+', html), "factor.css 未引用或未带版本号"
    assert re.search(r'/static/js/factor\.js\?v=\d+', html), "factor.js 未引用或未带版本号"


def test_toolbar_button_wired():
    html = _text(INDEX)
    app = _text(APP_JS)
    assert 'id="factorBtn"' in html
    assert 'initFactorPanel' in app, "app.js 未初始化因子面板"
    assert 'getElementById("factorBtn")' in app, "app.js 未绑定工具栏按钮"


def test_no_emoji_in_new_frontend_files():
    for p in (FACTOR_JS, FACTOR_CSS):
        hits = _EMOJI.findall(_text(p))
        assert hits == [], f"{p.name} 含 emoji: {sorted(set(hits))[:5]}"


def test_factor_js_esc_is_locally_defined():
    """S1 审计 blocking 的防回归：factor.js 的 IIFE 内裸调 esc() 但没定义过，
    浏览器 ReferenceError 且被空 catch 吞掉 → 历史列表/结果卡静默失效。
    这里只认「本文件内有模块级 function esc 定义」这一种解法。"""
    js = _text(FACTOR_JS)
    assert re.search(r"\bfunction esc\(", js), \
        "factor.js 缺少模块级 esc 定义（裸调 esc 会 ReferenceError）"
    assert "window.esc" not in js, "不应把 esc 挂到全局（避免与其它面板撞名）"
    assert js.count("this._esc(") == 0 or "function esc(" in js


def test_factor_js_loads_reports_on_init():
    """历史列表必须在面板初始化时加载，不能只在打开报告时才加载。"""
    js = _text(FACTOR_JS)
    init = re.search(r"async _initOnce\(\) \{(.{0,900}?)\n    \}", js, re.S)
    assert init, "未找到 _initOnce"
    assert "_loadReports()" in init.group(1), "_initOnce 未调用 _loadReports"


def test_panel_uses_neutral_quantile_labels():
    html = _text(INDEX)
    js = _text(FACTOR_JS)
    for blob in (html, js):
        assert "多头层" not in blob and "空头层" not in blob
    assert "Q1 = 因子值最小" in html


def test_no_hardcoded_brand_colors_outside_fallback():
    """CSS 里允许 var(--x, #fallback) 形式；不允许裸色值作为主色。"""
    css = _text(FACTOR_CSS)
    # 去掉所有 var(...) 内的 fallback 后再检查
    stripped = re.sub(r"var\([^)]*\)", "", css)
    offenders = re.findall(r"#[0-9a-fA-F]{6}", stripped)
    allowed = {"#ffffff", "#fafbfc", "#f3f4f6", "#f1f3f5", "#e5e7eb", "#f1f5f4", "#9ca3af"}
    bad = [c for c in offenders if c.lower() not in allowed]
    assert not bad, f"存在未走 Token 的硬编码颜色: {sorted(set(bad))[:6]}"
