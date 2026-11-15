"""池子研究面板接线测试。

用户 2026-10-18：「你这个池子在哪看，目前是不是没有页面可以看到？就是没页面」——
研究结果此前只出现在对话结果卡里点一下按钮的当下，切走就没了。
这里锁住「有页面、能打开、id 对得上、资源带版本号」。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INDEX = ROOT / "server/static/index.html"
PS_JS = ROOT / "server/static/js/pool-study.js"
PS_CSS = ROOT / "server/static/css/pool-study.css"
APP_JS = ROOT / "server/static/js/app.js"
FACTOR_JS = ROOT / "server/static/js/factor.js"
CARD_JS = ROOT / "server/static/js/chat-strategy-card.js"

_EMOJI = re.compile(
    "[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\uFE00-\uFE0F"
    "\U0001F000-\U0001F02F\U0001FA00-\U0001FAFF\u200D\u20E3]"
)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# 由 JS innerHTML 动态注入，不要求在静态 HTML 里存在
_DYNAMIC_IDS = {"psProgFill"}


def test_required_dom_ids_present():
    html = _text(INDEX)
    js = _text(PS_JS)
    ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js)) - _DYNAMIC_IDS
    assert ids, "pool-study.js 未取任何 DOM id？"
    missing = [i for i in sorted(ids) if f'id="{i}"' not in html]
    assert not missing, f"HTML 缺少 JS 依赖的 id: {missing}"


def test_assets_referenced_with_version():
    html = _text(INDEX)
    assert re.search(r"/static/css/pool-study\.css\?v=\d+", html)
    assert re.search(r"/static/js/pool-study\.js\?v=\d+", html)


def test_toolbar_button_wired():
    html = _text(INDEX)
    app = _text(APP_JS)
    assert 'id="poolStudyBtn"' in html
    assert "initPoolStudyPanel" in app
    assert 'getElementById("poolStudyBtn")' in app


def test_panel_exposes_openpage():
    """卡片和工具栏都按 openPage 调；只给 open 会让按钮静默无反应（2026-10-18 踩过）。"""
    js = _text(PS_JS)
    assert "openPage" in js and "window.StockRadar.poolStudy" in js
    assert re.search(r"openPage\s*\(", js)


def test_delete_and_clean_failed_wired():
    """失败记录必须能从页面删掉（单测残留曾把列表堆满 56 条失败，用户 2026-10-25）。"""
    html = _text(INDEX)
    js = _text(PS_JS)
    assert 'id="psCleanFailed"' in html
    assert 'cleanFailed' in js
    assert "data-del" in js                       # 每条记录右上角的删除叉
    assert "method: 'DELETE'" in js               # 调 DELETE /pool-studies/{id}


def test_card_links_to_page():
    """跑完必须能在卡片上跳到独立页面看全部历史研究。"""
    js = _text(CARD_JS)
    assert "poolStudy.openPage" in js


def test_no_emoji_in_panel_files():
    for p in (PS_JS, PS_CSS):
        hits = _EMOJI.findall(_text(p))
        assert hits == [], f"{p.name} 含 emoji: {sorted(set(hits))[:5]}"


def test_factor_init_does_not_block_on_health():
    """回归护栏：/health 曾在服务刚启动时排队 56 秒（日志实测 56438.9ms），
    而 _initOnce 用 Promise.all 等它 → 用户侧就是「打开因子页要好几十秒」。
    面板只允许阻塞等 /list，附属请求必须 fire-and-forget。"""
    js = _text(FACTOR_JS)
    m = re.search(r"async _initOnce\(\) \{(.*?)\n    \}", js, re.S)
    assert m, "未找到 _initOnce"
    body = m.group(1)
    assert "_loadHealth()" in body, "_initOnce 未触发 /health 加载"
    assert not re.search(r"await[^\n]*_loadHealth", body), \
        "_initOnce 不得 await /health（会导致面板被排队卡住）"
    assert not re.search(r"Promise\.all\([^)]*health", body), \
        "_initOnce 不得把 /health 放进 Promise.all"


def test_reports_list_shows_pending_placeholder():
    """列表未回来时要有明确占位，不能空着让用户以为坏了。"""
    js = _text(FACTOR_JS)
    assert "历史报告加载中" in js
    assert "fr-report-item pending" in js


def test_unavailable_report_is_greyed_and_not_clickable():
    """payload 被清理的报告要置灰且不带 data-report（点进去只会报错）。"""
    js = _text(FACTOR_JS)
    assert "available === false" in js
    assert "fr-report-item" in js and "' off'" in js
