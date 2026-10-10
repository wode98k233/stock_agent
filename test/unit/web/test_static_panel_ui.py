import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = ROOT / "server" / "static"


# 资源引用必须带版本号 (?v=N)，避免浏览器缓存 stale。
# 测试只校验「资源被引用 + 带版本号机制」这两件事，不锁死具体数字 ——
# 否则每次发版都要回来改测试，是没必要的维护成本。
def _assert_versioned_asset(html: str, asset_path: str) -> None:
    """断言 index.html 引用了带 ?v=N 版本号的静态资源。"""
    pattern = re.compile(re.escape(asset_path) + r"\?v=\d+")
    assert pattern.search(html), f"资源 {asset_path} 缺少 ?v=N 版本号引用"


def test_config_panel_exposes_workspace_summary_and_preview_regions():
    config_js = (STATIC_DIR / "js" / "config.js").read_text(encoding="utf-8")
    config_css = (STATIC_DIR / "css" / "config-panel.css").read_text(encoding="utf-8")

    assert "config-summary" in config_js
    assert "config-form-area" in config_js
    assert "config-preview-rail" in config_js
    assert "config-change-count" in config_js
    assert "当前影响预览" in config_js

    assert ".config-summary" in config_css
    assert ".config-preview-rail" in config_css
    assert ".config-form-area" in config_css
    assert "预算分配估算" not in config_js
    assert "65.5k / 200k tokens" not in config_js
    assert "config-impact-bar" not in config_js


def test_config_panel_exposes_notification_workspace_and_help_tooltips():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    api_js = (STATIC_DIR / "js" / "api.js").read_text(encoding="utf-8")
    config_js = (STATIC_DIR / "js" / "config.js").read_text(encoding="utf-8")
    config_css = (STATIC_DIR / "css" / "config-panel.css").read_text(encoding="utf-8")

    assert "getNotificationStatus" in api_js
    assert "testNotification" in api_js
    assert "sendNotification" in api_js

    assert "'通知': ['通知']" in config_js
    assert "config-notification-panel" in config_js
    assert "notification-channel-grid" in config_js
    assert "notification-send-form" in config_js
    assert "data-tooltip" in config_js
    assert "NOTIFICATION_COOLDOWN" in config_js
    assert "PUSHOVER_USER_KEY" in config_js

    assert ".config-help" in config_css
    assert ".config-help::after" in config_css
    assert ".config-notification-panel" in config_css
    assert ".notification-channel-grid" in config_css
    assert ".notification-send-form" in config_css

    _assert_versioned_asset(index_html, "/static/js/api.js")
    _assert_versioned_asset(index_html, "/static/js/config.js")
    _assert_versioned_asset(index_html, "/static/css/config-panel.css")


def test_notification_ui_keeps_help_and_manual_send_fields_readable():
    config_js = (STATIC_DIR / "js" / "config.js").read_text(encoding="utf-8")
    config_css = (STATIC_DIR / "css" / "config-panel.css").read_text(encoding="utf-8")

    assert "notification-title-field" in config_js
    assert "notification-type-field" in config_js
    assert "notification-channel-field" in config_js

    assert ".config-section-card {" in config_css
    assert "overflow: visible;" in config_css
    assert ".notification-title-field" in config_css
    assert "grid-column: 1 / -1" in config_css
    assert ".config-help::after" in config_css
    assert 'class="config-help" tabindex="0" aria-label="' in config_js
    assert 'title="${this._esc(description)}"' not in config_js
    assert "left: calc(100% + 10px)" in config_css
    assert "max-width: min(360px, calc(100vw - 32px))" in config_css


def test_config_panel_explains_cache_prefix_backend_fields():
    config_js = (STATIC_DIR / "js" / "config.js").read_text(encoding="utf-8")

    # A8（5d535df）在「模型」tab 下新增 LLM 缓存/模型路由两组，映射从 ['LLM'] 扩展
    assert "'模型': ['LLM', 'LLM 缓存', '模型路由']" in config_js
    assert "CACHE_PREFIX_ENABLED" in config_js
    assert "CACHE_PREFIX_CONTENT" in config_js
    assert "固定前缀" in config_js
    assert "留空时使用默认 A 股知识前缀" in config_js


def test_assistant_reports_expose_markdown_notification_action_without_image_backend_claim():
    chat_core_js = (STATIC_DIR / "js" / "chat-core.js").read_text(encoding="utf-8")
    chat_css = (STATIC_DIR / "css" / "chat.css").read_text(encoding="utf-8")

    assert "msg-notify-btn" in chat_core_js
    assert "msg-notify-menu" in chat_core_js
    assert "sendNotification" in chat_core_js
    assert "Markdown 文案" in chat_core_js
    assert "报告图片" in chat_core_js
    assert "sendImageNotification" in chat_core_js

    assert ".msg-notify-btn" in chat_css
    assert ".msg-notify-menu" in chat_css
    assert ".msg-notify-menu.open" in chat_css


def test_report_template_manager_static_assets_are_wired():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    api_js = (STATIC_DIR / "js" / "api.js").read_text(encoding="utf-8")
    manager_js = (STATIC_DIR / "js" / "template-manager.js").read_text(encoding="utf-8")
    manager_css = (STATIC_DIR / "css" / "template-manager.css").read_text(encoding="utf-8")

    assert "templateManagerBtn" in index_html
    assert "templateManagerPanel" in index_html
    assert "templateDiffModal" in index_html
    assert "templateDiffApplyBtn" in index_html
    _assert_versioned_asset(index_html, "/static/js/template-manager.js")
    _assert_versioned_asset(index_html, "/static/css/template-manager.css")

    assert "getReportTemplates" in api_js
    assert "getReportTemplate" in api_js
    assert "saveReportTemplate" in api_js

    assert "ReportTemplateManager" in manager_js
    assert "data-template-save" in manager_js
    assert "output_blocks" in manager_js
    assert "data_contract" in manager_js
    assert "qa_rules" in manager_js
    assert "data-block-id" in manager_js
    assert "template-horizon-options" in manager_js
    assert "template-block-picker" in manager_js
    assert "onclick = () => this.showSavePreview()" in manager_js
    assert "querySelectorAll('.family-tab[data-family]')" in manager_js
    assert "当前值" in manager_js
    assert "showSavePreview" in manager_js
    assert "confirmSave" in manager_js
    assert "_getTemplateChanges" in manager_js
    assert "template-diff-row" in manager_js

    assert ".template-manager-panel" in manager_css
    assert ".template-select-trigger" in manager_css
    assert ".template-editor-grid" in manager_css
    assert ".template-block-picker" in manager_css
    assert ".template-actions-inner" in manager_css
    assert ".template-diff-modal" in manager_css
    assert ".template-diff-value" in manager_css
    assert "white-space: nowrap;" in manager_css


def test_calendar_uses_fixed_day_detail_instead_of_popover_only():
    calendar_js = (STATIC_DIR / "js" / "trading-calendar.js").read_text(encoding="utf-8")
    calendar_css = (STATIC_DIR / "css" / "trading-calendar.css").read_text(encoding="utf-8")

    assert "calendar-stats" in calendar_js
    assert "calendar-day-pane" in calendar_js
    assert "calendar-record-list" in calendar_js
    assert "selectedDate" in calendar_js
    assert "交易日" in calendar_js

    assert ".calendar-day-pane" in calendar_css
    assert ".calendar-record-item" in calendar_css
    assert ".calendar-stats" in calendar_css


def test_watchlist_has_portfolio_summary_filters_and_table_header():
    watchlist_js = (STATIC_DIR / "js" / "watchlist.js").read_text(encoding="utf-8")
    watchlist_css = (STATIC_DIR / "css" / "watchlist.css").read_text(encoding="utf-8")

    assert "watchlist-summary" in watchlist_js
    assert "watchlist-filter-tabs" in watchlist_js
    assert "watchlist-table-head" in watchlist_js
    assert "selectedFilter" in watchlist_js
    assert "平均" in watchlist_js

    assert ".watchlist-summary" in watchlist_css
    assert ".watchlist-filter-tabs" in watchlist_css
    assert ".watchlist-table-head" in watchlist_css


def _css_rule(css: str, selector: str) -> str:
    """取出单条规则体，并**剥掉注释**再返回。

    不剥注释会踩到同类误报：本次修复在注释里写了「原来用 nowrap + ellipsis」，
    直接 `in` 判断会把注释当成生效声明，得出反向结论。
    """
    i = css.index(selector + " {")
    depth = 0
    for k in range(i, len(css)):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return re.sub(r"/\*.*?\*/", "", css[i:k + 1], flags=re.S)
    raise AssertionError("未闭合的规则: " + selector)


def test_watchlist_source_dropdown_is_not_clipped_or_truncated():
    """自选股「数据源」下拉的两个真实缺陷（2026-09-18 修复，此处锁死）。

    实测 /api/datasources 返回 11 个可用源（+「自动」共 12 项），
    其中同花顺的描述有 **89 个汉字**。

    缺陷一：`.watchlist-modal` 上的 `overflow: hidden` 把下拉裁掉。
        下拉是 absolute 定位、展开近 600px，而 modal 高度是内容驱动（自选股列表短就矮），
        超出 modal 下沿的部分被剪 → 表现为「下拉框没完整显示出来」。
    缺陷二：描述行 `10px + nowrap + ellipsis`，230px 宽只放得下约 17 字
        → 89 字的描述约 80% 被省略号吞掉，用户看不到内容。
    """
    css = (STATIC_DIR / "css" / "watchlist.css").read_text(encoding="utf-8")

    # 缺陷一：不能再有裁切源，圆角改由首尾行承接
    modal = _css_rule(css, ".watchlist-modal")
    assert "overflow" not in modal, "modal 上的 overflow 会裁掉数据源下拉"
    assert "border-radius" in _css_rule(css, ".watchlist-header")
    assert "border-radius" in _css_rule(css, ".watchlist-footer")

    # 下拉本身要够宽 + 限高可滚（12 项不会顶出视口）
    dd = _css_rule(css, ".wl-source-dropdown")
    assert "max-height" in dd, "下拉未限高，12 项会超出视口"
    assert "overflow-y: auto" in dd, "下拉限高后没有滚动条就看不全"
    assert "230px" not in dd, "下拉仍是旧的 230px 宽，装不下长描述"

    # 缺陷二：描述必须换行完整显示
    desc = _css_rule(css, ".wl-source-option-desc")
    assert "nowrap" not in desc, "长描述仍被强制单行"
    assert "text-overflow" not in desc, "长描述仍被省略号截断"
    assert "white-space: normal" in desc


def test_watchlist_source_dropdown_renders_every_available_source():
    """下拉项由 /api/datasources 的 available 源动态生成，不该有硬编码条数上限。"""
    js = (STATIC_DIR / "js" / "watchlist.js").read_text(encoding="utf-8")
    assert "getDataSources" in js
    assert "ds.available" in js
    # 反例守卫：任何 slice/数字上限都会让源显示不全
    assert "dataSources.slice" not in js


def test_data_center_crowding_tab_is_fully_wired():
    """真实事故：面板 HTML、CSS、JS 都写好了，但**页签按钮和 script 标签没加**，
    页面看上去就是「功能没做」——三处必须一起校验（文案/外壳/引用/切换钩子）。"""
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    center_js = (STATIC_DIR / "js" / "data-center.js").read_text(encoding="utf-8")
    crowding_js = (STATIC_DIR / "js" / "crowding.js").read_text(encoding="utf-8")
    monitor_js = (STATIC_DIR / "js" / "data-monitor.js").read_text(encoding="utf-8")
    center_css = (STATIC_DIR / "css" / "data-center.css").read_text(encoding="utf-8")

    # ① 页签按钮 + 面板外壳
    assert 'data-tab="dcCrowding"' in index_html
    assert "拥挤度排查" in index_html
    assert 'id="dcCrowding"' in index_html
    for el in ("crMeta", "crCards", "crStrip", "crWarn", "crTable", "crFoot",
               "crFilterCat", "crFilterLevel", "crBtnScan", "crBtnRefresh"):
        assert 'id="%s"' % el in index_html, "缺少 %s" % el

    # ② script 引用（漏了它，面板永远不会被初始化）
    _assert_versioned_asset(index_html, "/static/js/crowding.js")
    _assert_versioned_asset(index_html, "/static/js/data-center.js")
    _assert_versioned_asset(index_html, "/static/js/data-monitor.js")
    _assert_versioned_asset(index_html, "/static/css/data-center.css")

    # ③ 页签切换钩子（漏了它，点页签没有任何反应）
    assert "dcCrowding" in center_js
    assert "initCrowdingScan" in center_js

    # ④ 面板实现 + 后台巡检状态条
    assert "CrowdingPanel" in crowding_js
    assert "window.initCrowdingScan" in crowding_js
    assert "await this._refreshScanState()" in crowding_js     # 缺 await 会拿到半渲染面板
    assert "/api/crowding/latest" in crowding_js
    assert "/api/scans/status" in crowding_js
    assert "后台扫描" in index_html

    assert 'id="dmScans"' in index_html
    assert "loadScans" in monitor_js
    assert "/api/scans/status" in monitor_js
    assert "/api/data-monitor/scan" in monitor_js

    # ⑤ 样式：分档色带 lv-0（舒适）→ lv-4（过热），A 股习惯绿→红
    for cls in (".cr-card", ".cr-strip", ".cr-level", ".dm-scans"):
        assert cls in center_css
    for lv in range(5):
        assert ".lv-%d" % lv in center_css


def test_config_schema_groups_are_all_mapped_to_a_tab():
    """`_tabMapping()` 未列出的分组会被**静默丢掉**（页面不报错、就是不显示）。

    所以 schema 里出现的每个 group 都必须能在映射表里找到归宿，否则配置项等于
    「写了但用户改不了」。这里把两边对齐校验。
    """
    import re

    from server.config_schema import get_config_schema

    config_js = (STATIC_DIR / "js" / "config.js").read_text(encoding="utf-8")
    mapping_block = config_js.split("_tabMapping()", 1)[1].split("};", 1)[0]
    mapped = set(re.findall(r"'([^']+)'", mapping_block))

    groups = {item["group"] for item in get_config_schema()}
    missing = groups - mapped
    assert not missing, "这些配置分组没挂到任何 tab，页面上看不到：%s" % sorted(missing)

    tabs = set(re.findall(r"'([^']+)'",
                          config_js.split("_tabOrder()", 1)[1].split(";", 1)[0]))
    assert tabs >= {"模型", "Agent", "报告", "数据", "记忆系统", "通知", "调度", "诊断"}


def test_background_scan_keys_are_exposed_and_tunable():
    """后台巡检必须能从配置页看到/关掉 —— 否则用户无法关掉一个会跑十几分钟的扫描。"""
    from server.config_schema import get_config_schema

    schema = {s["key"]: s for s in get_config_schema()}
    for key in ("BACKGROUND_SCANS_ENABLED", "SCAN_STORAGE_ENABLED",
                "SCAN_STORAGE_FIRST_DELAY_S", "SCAN_STORAGE_INTERVAL_MINUTES",
                "SCAN_THESIS_ENABLED", "SCAN_THESIS_FIRST_DELAY_S",
                "SCAN_THESIS_INTERVAL_MINUTES", "SCAN_CROWDING_ENABLED",
                "SCAN_CROWDING_FIRST_DELAY_S", "SCAN_CROWDING_INTERVAL_MINUTES",
                "SCAN_SHORTLINE_ENABLED", "SCAN_SHORTLINE_FIRST_DELAY_S",
                "SCAN_SHORTLINE_INTERVAL_MINUTES"):
        assert key in schema, "配置页缺少 %s" % key
        assert schema[key]["group"] == "后台巡检"
    assert schema["BACKGROUND_SCANS_ENABLED"]["type"] == "bool"

    config_js = (STATIC_DIR / "js" / "config.js").read_text(encoding="utf-8")
    assert "'后台巡检'" in config_js
    _assert_versioned_asset((STATIC_DIR / "index.html").read_text(encoding="utf-8"),
                            "/static/js/config.js")
