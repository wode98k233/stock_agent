"""顶栏（title 行）导航契约测试。

背景：2026-09-18 顶栏重构 —— 原先是 7 个并列 pill + 1 个「更多」下拉（内含 10 项）
+ 5 个系统图标，共 13 个独立小控件平铺在同一行，语义上把「看盘 / 研究 / 配置 / 运维」
四类东西混在一起，且「关闭服务」紧挨「K线图」。

重构后：1 条常驻工具条（5 项）+ 4 个按功能划分的分组菜单。

这个文件存在的意义：重构前顶栏**零测试覆盖**，所以漂移到 13 个控件也没人发现。
下面这些断言锁住的是「契约」，不是具体像素：

1. 所有 legacy id 必须保留 —— app.js 用 `getElementById("calendarBtn").onclick = ...`
   这类**无 null 保护**的写法，id 一丢就在启动时抛 TypeError。
2. 分组必须真的按功能划分：每组至少 3 项（防止退化成「某项独占一个分组」= 换个名字的小按钮）。
3. 危险操作必须在运维菜单里、带分隔线、且不与看盘项同组。
4. 无重复 id。
"""
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = ROOT / "server" / "static"


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _topbar_html() -> str:
    """截取 <header class="topbar"> 内的 topbar-actions 区块。"""
    html = _html()
    start = html.index('<div class="topbar-actions"')
    end = html.index("      </header>", start)
    return html[start:end]


def _chat_css() -> str:
    return (STATIC_DIR / "css" / "chat.css").read_text(encoding="utf-8")


# ── 1. legacy id 保留 ────────────────────────────────────────────

LEGACY_IDS = [
    # 常驻工具条
    "klineBtn", "watchlistBtn", "marketMonitorBtn", "portfolioBtn", "dataCenterBtn",
    # 行情资讯
    "newsBtn", "flowBtn", "calendarBtn",
    # 研究分析
    "backtestBtn", "strategyResearchBtn", "qualityPanelBtn",
    # 配置管理
    "configBtn", "memoryManagerBtn",
    # 运维
    "execBtn", "marketToggleBtn", "monitorBtn", "restartBtn", "shutdownBtn",
]


def test_all_legacy_topbar_ids_survive_the_restructure():
    """app.js/market-dashboard.js/memory-manager.js 都用 getElementById 取这些 id。"""
    bar = _topbar_html()
    missing = [i for i in LEGACY_IDS if f'id="{i}"' not in bar]
    assert not missing, f"重构后丢失的顶栏 id：{missing}"


def test_retired_more_tools_dropdown_is_fully_removed():
    """「更多」单一抽屉已被 4 个分组菜单取代，残留会让关闭逻辑指向 null。"""
    html = _html()
    assert "moreToolsBtn" not in html
    assert "moreToolsDropdown" not in html
    assert "更多工具" not in html


def test_ids_used_by_startup_wiring_exist_in_index_html():
    """跨文件反查：app.js 启动时就给按钮挂 onclick 的那些 id 必须都还在。

    这是本文件最有价值的一条 —— `document.getElementById("calendarBtn").onclick = ...`
    没有 null 保护，按钮一旦被挪走却没同步 id，页面启动即 TypeError。

    只取「赋值式」绑定（`.onclick =`），因为它是**启动期同步执行**的；
    `addEventListener` 与 `?.value` 这类写法不受 null 影响，纳入会误报。
    另：面板内部动态生成的 id（如 memory-manager.js 的 memProfileRisk）不在 index.html 里，
    本来就不该由这条断言覆盖。
    """
    html_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', _html()))
    js_dir = STATIC_DIR / "js"
    bound: set[str] = set()
    for name in ("app.js", "market-dashboard.js", "memory-manager.js"):
        src = (js_dir / name).read_text(encoding="utf-8")
        bound |= set(re.findall(
            r'getElementById\(\s*"([A-Za-z0-9_-]+)"\s*\)\s*\.onclick\s*=', src))
    assert len(bound) >= 10, f"只匹配到 {len(bound)} 个启动期绑定，正则可能已失效"
    missing = sorted(i for i in bound if i not in html_ids)
    assert not missing, f"启动期绑定了 index.html 中不存在的 id：{missing}"


# ── 2. 分组结构 ──────────────────────────────────────────────────

def test_topbar_has_one_rail_and_four_function_groups():
    bar = _topbar_html()
    assert bar.count('class="tool-rail"') == 1
    assert len(re.findall(r'class="tool-group"', bar)) == 4


def test_each_group_is_a_real_group_not_a_renamed_single_button():
    """每组至少 3 项：防止把「更多」的毛病换个名字复现（一项独占一个分组）。"""
    bar = _topbar_html()
    menus = re.findall(r'<div class="tool-menu" id="([^"]+)"(.*?)\n            </div>', bar, re.S)
    assert len(menus) == 4, f"应有 4 个分组菜单，实际 {len(menus)}"
    for menu_id, body in menus:
        items = body.count('class="tool-menu-item')
        assert items >= 3, f"分组 {menu_id} 只有 {items} 项，退化成单个按钮了"
        # 每组都要有分组标题，否则用户不知道这一组是什么
        assert 'class="tool-menu-head"' in body, f"分组 {menu_id} 缺少标题"


def test_group_buttons_are_wired_to_their_menus():
    """分组按钮用 disclosure 模式（aria-expanded + aria-controls），
    菜单容器与菜单项**不**带 role=menu/menuitem —— 那套需要方向键导航，
    只声明 role 不实现键盘交互反而会误导读屏软件。"""
    bar = _topbar_html()
    menus = re.findall(r'<div class="tool-menu" id="([^"]+)"', bar)
    buttons = re.findall(r'<button class="tool-group-btn"[^>]*>', bar)
    assert len(buttons) == len(menus) == 4

    for btn in buttons:
        assert 'aria-expanded="false"' in btn, btn
        ctrl = re.search(r'aria-controls="([^"]+)"', btn)
        assert ctrl, f"分组按钮缺少 aria-controls：{btn}"
        assert ctrl.group(1) in menus, f"aria-controls={ctrl.group(1)} 指向不存在的菜单"
        assert 'data-menu=' in btn, btn

    # 反例守卫：role=menu 但不实现方向键 = 反模式
    assert 'role="menu"' not in bar
    assert 'role="menuitem"' not in bar
    assert 'aria-haspopup' not in bar


def test_every_menu_has_its_own_header():
    bar = _topbar_html()
    assert bar.count('class="tool-menu-head"') == 5  # 4 组标题 + 运维区「危险操作」子标题


# ── 3. 危险操作隔离 ──────────────────────────────────────────────

@pytest.mark.parametrize("ident", ["restartBtn", "shutdownBtn"])
def test_destructive_actions_live_in_the_ops_menu_behind_a_separator(ident):
    """重启/关闭原先是顶栏图标，紧邻「K线图」；现在必须在运维菜单里且有分隔。"""
    bar = _topbar_html()
    ops_start = bar.index('id="menuOps"')
    pos = bar.index(f'id="{ident}"')
    assert pos > ops_start, f"{ident} 不在运维菜单内"

    # 该按钮之前必须出现过一条分隔线（把「危险操作」与状态项隔开）
    assert 'class="tool-menu-sep"' in bar[ops_start:pos], f"{ident} 之前没有分隔线"


def test_shutdown_is_marked_danger():
    bar = _topbar_html()
    m = re.search(r'<button id="shutdownBtn"[^>]*>', bar)
    assert m and "is-danger" in m.group(0), "关闭服务未标危险样式"


# ── 4. 全局唯一性 ────────────────────────────────────────────────

def test_no_duplicate_ids_anywhere_in_index_html():
    ids = re.findall(r'id="([A-Za-z0-9_-]+)"', _html())
    dups = sorted({i for i in ids if ids.count(i) > 1})
    assert not dups, f"重复 id：{dups}"


# ── 5. 样式与失效类名 ────────────────────────────────────────────

def test_new_nav_classes_are_styled_and_old_ones_are_gone():
    css = _chat_css()
    for cls in (".tool-rail", ".rail-chip", ".tool-group-btn", ".tool-menu",
                ".tool-menu-head", ".tool-menu-item", ".tool-menu-sep", ".is-danger"):
        assert cls in css, f"chat.css 缺少 {cls}"
    # 旧结构类名必须清干净，否则会有两套并存、改一处不生效
    for dead in (".tool-pill", ".tool-dropdown", ".tool-more", ".tool-sys-btn", ".tool-sep"):
        assert dead not in css, f"chat.css 残留失效类名 {dead}"


def test_running_dot_hook_was_retargeted_off_the_removed_class():
    """execBtn 的运行态小红点原来挂在 .tool-sys-btn.running 上，该类已删除。"""
    exec_css = (STATIC_DIR / "css" / "exec-drawer.css").read_text(encoding="utf-8")
    bar = _topbar_html()
    assert "tool-run-dot" in bar, "execBtn 缺少 tool-run-dot 钩子类"
    assert ".tool-run-dot.running" in exec_css
    # 断言选择器已消失（不能只查字符串 —— 注释里提到旧类名是正常的）
    assert ".tool-sys-btn {" not in exec_css
    assert ".tool-sys-btn.running" not in exec_css
    assert ".tool-run-dot.running::after" in exec_css


def test_market_toggle_keeps_narrow_screen_only_visibility():
    """市场情绪按钮只在 ≤1100px（面板变抽屉）时才需要，宽屏下市场面板常驻。"""
    css = (STATIC_DIR / "css" / "market-dashboard.css").read_text(encoding="utf-8")
    bar = _topbar_html()
    m = re.search(r'<button id="marketToggleBtn"[^>]*>', bar)
    assert m and "market-narrow-btn" in m.group(0), "市场情绪按钮丢了窄屏类名"
    assert ".market-narrow-btn { display: none; }" in css
    # 菜单项是整行 flex，不能写成 inline-flex（会被压成窄条）
    assert ".market-narrow-btn { display: flex; }" in css


def test_a11y_and_motion_guards_are_declared():
    css = _chat_css()
    assert "prefers-reduced-motion" in css, "新导航组件未尊重 prefers-reduced-motion"
    assert "pointer: coarse" in css, "未为触摸设备放宽命中区"
    assert ":focus-visible" in css, "交互元素缺少可见焦点态"


def test_stylesheets_are_version_bumped_in_index_html():
    html = _html()
    for asset in ("css/chat.css", "css/exec-drawer.css", "css/market-dashboard.css"):
        assert re.search(re.escape(asset) + r"\?v=\d+", html), f"{asset} 缺少 ?v= 版本号"
