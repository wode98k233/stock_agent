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

    assert "'模型': ['LLM']" in config_js
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
