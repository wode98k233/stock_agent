"""对话内策略进度卡的接线门禁（2026-10-17）。

三道闸：
1. **字段/类名对得上**：JS 找的 `.sc-phase` / `.sc-rounds` 在 CSS 里真的存在。
2. **自动升级链路在**：`upgradeToResult` 必须真的请求 `/card`，不能只是改文案。
3. **命名不冲突**：`sc-phase-*` / `sc-round-*` 只能出现在 strategy-card.css，
   避免与其它面板的同名类互相污染（用户明确要求检查 name 冲突）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "server/static/js/chat-strategy-card.js"
CSS = ROOT / "server/static/css/strategy-card.css"
CSS_DIR = ROOT / "server/static/css"

_EMOJI = re.compile(
    "[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\uFE00-\uFE0F"
    "\U0001F000-\U0001F02F\U0001FA00-\U0001FAFF\u200D\u20E3]"
)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_phase_and_round_classes_defined():
    css = _text(CSS)
    for cls in (".sc-phase", ".sc-phase-item", ".sc-phase-item.on",
                ".sc-phase-item.done", ".sc-phase-item.bad", ".sc-phase-arw",
                ".sc-rounds", ".sc-round", ".sc-round-no", ".sc-round-hyp",
                ".sc-round-meta", ".sc-round-verdict"):
        assert cls in css, f"CSS 缺少 {cls}"


def test_js_renders_phase_and_rounds():
    js = _text(JS)
    assert "function renderPhaseStrip" in js
    assert "function renderRounds" in js
    assert "function _paintRounds" in js
    assert "sc-phase" in js and "sc-round" in js
    # 默认阶段与后端 PROGRESS_PHASES 对齐（防两边各改一处）
    assert "确认口径" in js and "迭代研发" in js


def test_auto_upgrade_calls_card_api():
    """跑完必须真的去拉结果卡，而不是让用户再问一句。"""
    js = _text(JS)
    assert "function upgradeToResult" in js
    assert "/card" in js
    assert "replaceChild(node, card)" in js
    # onFinal 的成功分支必须调用升级。
    # 注意不锁死首个实参名：2026-10-18 修「进度只有刷新才动」时，这里从闭包捕获的
    # card 换成了按 data-run-uuid 现查的活节点（liveNodeFor），否则对话重渲染后
    # 事件会画在已经脱离文档的旧节点上。**要锁的是「升级被调用了」这个行为。**
    m = re.search(r"function onFinal\(d\) \{(.*?)\n    \}", js, re.S)
    assert m, "未找到 onFinal"
    assert re.search(r"upgradeToResult\(\w+, runUuid\)", m.group(1)), \
        "onFinal 成功分支未调用 upgradeToResult"


def test_card_endpoint_registered():
    from server.routes.strategy_research import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/strategy-research/runs/{run_uuid}/card" in paths


def test_no_emoji_in_touched_frontend():
    hits = _EMOJI.findall(_text(JS)) + _EMOJI.findall(_text(CSS))
    assert hits == [], f"含 emoji: {sorted(set(hits))[:5]}"


def test_new_class_names_do_not_leak_into_other_stylesheets():
    """sc-phase-* / sc-round-* 只能在本文件定义；别的 CSS 出现同名即视为冲突风险。"""
    offenders = []
    for p in CSS_DIR.glob("*.css"):
        if p.name == "strategy-card.css":
            continue
        body = _text(p)
        if re.search(r"\.sc-(phase|round)", body):
            offenders.append(p.name)
    assert offenders == [], f"类名冲突：{offenders}"


def test_progress_card_actions_kept():
    """新增阶段条不能顺手删掉既有的「看研发过程」入口。"""
    js = _text(JS)
    assert "看研发过程" in js
    assert "strategyResearchPanel" in js


# ── 续跑入口（2026-10-18）─────────────────────────────────

def test_resume_button_wired_to_api():
    """中断任务必须在卡片上给出续跑入口，且真的打 resume 接口。"""
    js = JS.read_text(encoding="utf-8")
    assert "data-resume" in js, "缺幂等标记，重复渲染会叠出多个按钮"
    assert "/resume" in js and "method: 'POST'" in js
    assert "从第 " in js, "按钮文案要带轮次，否则用户不知道从哪接"


def test_interrupted_does_not_subscribe_sse():
    """中断的任务不会有后续事件：不能白占浏览器 6 条连接之一（服务端要轮询到 1h）。"""
    js = JS.read_text(encoding="utf-8")
    idx = js.index("function subscribeLive")
    head = js[idx: idx + 900]
    assert "'interrupted'" in head
    assert "return;" in head, "interrupted 分支必须提前返回，不建 EventSource"
    assert "ensureResume" in head


def test_interrupted_final_shows_resume():
    js = JS.read_text(encoding="utf-8")
    assert "status === 'interrupted'" in js
    assert "ensureResume(card, runUuid" in js


# ── 续跑入口（2026-10-18）─────────────────────────────────

def test_resume_button_wired_to_api():
    """中断任务必须在卡片上给出续跑入口，且真的打 resume 接口。"""
    js = JS.read_text(encoding="utf-8")
    assert "data-resume" in js, "缺幂等标记，重复渲染会叠出多个按钮"
    assert "/resume" in js and "method: 'POST'" in js
    assert "从第 " in js, "按钮文案要带轮次，否则用户不知道从哪接"


def test_interrupted_does_not_subscribe_sse():
    """中断的任务不会有后续事件：不能白占浏览器 6 条连接之一（服务端要轮询到 1h）。"""
    js = JS.read_text(encoding="utf-8")
    idx = js.index("function subscribeLive")
    head = js[idx: idx + 900]
    assert "'interrupted'" in head
    assert "return;" in head, "interrupted 分支必须提前返回，不建 EventSource"
    assert "ensureResume" in head


def test_interrupted_final_shows_resume():
    js = JS.read_text(encoding="utf-8")
    assert "status === 'interrupted'" in js
    assert "ensureResume(card, runUuid" in js
