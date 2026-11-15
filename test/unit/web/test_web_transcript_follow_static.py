"""过程流打字机 + 对话跟随（Follow）的静态契约测试。

前端无 JS 测试基建，仓库惯例是「Python 断言静态资源内容」（见
test_web_status_player_static.py / test_topbar_menu_behavior.py）。

守护的两条回归（均来自 2026-10-15 设计评审）：
1. 打字机必须**全局单飞 + 收敛步长**，不能退回「每条目各起一个 8ms/字循环」
   —— 那是「同一时刻多条同时展开、越落越远」的根因；
2. 跟随必须落在**真正可滚动的祖先**（.chat-pane），不能只写 messageList.scrollTop
   —— 那是「输出超过屏幕也不往下滚」的根因。
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = ROOT / "server" / "static"


def _read(*parts: str) -> str:
    return (STATIC_DIR.joinpath(*parts)).read_text(encoding="utf-8")


def test_typewriter_is_single_flight_and_converging():
    js = _read("js", "chat-transcript.js")

    # 单飞：模块级 typing 单例 + 代际令牌作废旧循环
    assert "const typing = { rec: null, gen: 0" in js
    assert "myGen !== typing.gen" in js
    # 收敛步长：每帧按「剩余字数 / 剩余帧数」重算，而不是固定 8ms/字
    assert "Math.ceil((full.length - i) / left)" in js
    assert "_TYPE_MAX_MS = 1200" in js
    # 每帧读最新文本（rec.entry.text），不能捕获调用时快照 —— 否则尾部永久丢失
    assert "const full = (rec.entry && rec.entry.text) || text;" in js
    # 旧的固定 8ms 循环必须已消失
    assert "setTimeout(r, 8)" not in js
    # ⚠️ 同名函数声明会因提升而「后定义者胜出」：重复定义等于新实现被静默架空
    assert js.count("function _typewrite(") == 1
    assert js.count("function _settleThink(") == 1
    assert js.count("function _flushTyping(") == 1


def test_typewriter_flushes_previous_block_on_new_think():
    js = _read("js", "chat-transcript.js")

    # 同时只展开最新一条：旧块立刻定格收尾
    assert "_settleOthers" in js
    assert "下一轮思考已开始，这里先收起了" in js
    # 收尾必须停掉定时器（只置 abort 会留下挂起的 setTimeout）
    assert "wasBusy" in js
    # 续打而非清空重打：同 entry_id 文本增长时不能闪一下
    assert "text.indexOf(shown) === 0 ? shown.length : 0" in js
    # reset() 必须停掉在飞的打字机
    assert "_stopTyping(typing.rec)" in js
    # 滚动交给 follow，不再自行 scrollIntoView
    assert "scrollIntoView" not in js


def test_follow_module_resolves_real_scroll_container():
    js = _read("js", "chat-follow.js")

    assert "window.StockRadar.chatFollow" in js
    # 真正滚动的是 .chat-pane（overflow-y:auto），#messageList 不是滚动容器
    assert "function scrollParent" in js
    assert "/(auto|scroll|overlay)/.test(s.overflowY)" in js
    # ⚠️ 不能按「当前是否溢出」筛选：空/短对话时 .chat-pane 不溢出，
    #    会被跳过而落到 document.body，之后 pin() 全程静默失效
    assert "scrollHeight > n.clientHeight" not in js
    # 用户主动上滚才暂停：看输入意图（wheel/touch/keydown），不看 scroll 事件
    assert '["wheel", "touchmove", "keydown"]' in js
    # 任务开始默认开启跟随
    assert "function setRunning" in js


def test_follow_button_is_wired_in_html_and_css():
    index_html = _read("index.html")
    css = _read("css", "chat-follow.css")

    assert 'id="chatFollowBtn"' in index_html
    assert "/static/js/chat-follow.js" in index_html
    assert "/static/css/chat-follow.css" in index_html
    # 按钮脚本必须在 app.js 之前加载（init 里要拿到 chatFollow）
    assert index_html.index("/static/js/chat-follow.js") < index_html.index("/static/js/app.js")
    assert "跟随中" in index_html and "回到底部" in index_html
    assert ".chat-follow-btn.is-on" in css


def test_app_wires_follow_init_and_running_state():
    app_js = _read("js", "app.js")

    assert "follow.init()" in app_js
    # 打字机每帧 → 驱动跟随滚动（否则思考正文涨出屏幕没人滚）
    assert "ct.setOnTick(() => follow.pin())" in app_js
    # 任务开始/结束驱动跟随开关
    assert "chatFollow.setRunning(running)" in app_js


def test_chat_core_scroll_bottom_respects_follow():
    core_js = _read("js", "chat-core.js")

    # 不能再无条件 scrollTop = scrollHeight（用户上滚阅读时会抢位置）
    assert "if (follow && !follow.isAtBottom()) return;" in core_js
    assert "function _scrollParent" in core_js
    assert "scrollHeight > n.clientHeight" not in core_js


def test_think_node_renders_ai_content_section():
    """「思考+文字」轮的正文必须进 UI（默认收起，JSON 美化）。"""
    js = _read("js", "chat-transcript.js")

    assert 'class="tr-out"' in js
    # 默认收起：open 类才展开，避免过程卡被 JSON 撑爆
    assert "rec.outOpen" in js
    assert "tr-out-caret" in js
    # JSON 美化 + 去掉 ```json 围栏
    assert "JSON.stringify(JSON.parse(body), null, 2)" in js
    assert "```(?:json)?" in js
    # 纯正文轮不能叫「思考中」
    assert '"输出正文"' in js


def test_transcript_splits_after_clarify():
    """澄清反问后必须另起过程卡，否则「继续执行」被画到反问上方。"""
    js = _read("js", "chat-transcript.js")
    app_js = _read("js", "app.js")

    assert "function splitTurn" in js
    assert "splitTurn," in js          # 已导出
    # 分段出去的卡必须能被 finish/freeze 找回，否则卡头永远停在「运行中…」
    assert "const closed = []" in js
    assert "function _allTurns" in js
    assert "closed.length = 0" in js   # reset 要清
    # app.js 两处澄清气泡追加后都要触发分段
    assert app_js.count("_splitTranscriptAfterClarify();") == 2
    # 分段后锚点置空 → 新卡追加到列表末尾
    assert "state.transcriptAppendTail" in app_js
    assert "ct.splitTurn(state.taskId)" in app_js


def test_transcript_card_collapses_as_a_whole():
    """整卡收起：一轮 80+ 节点，不收起会把一次对话拉得老长（2026-10-26 用户反馈）。

    口径（别改回去）：
    - 卡头即开关（点击 / Enter / 空格），收起后只留一行标题 + 节点数；
    - 完成 done、澄清分段 seg 自动收起；停止/出错 frozen **不**自动收起
      —— 失败现场要能直接看到最后一步；
    - 用户手动开合过（userToggled）就不再自动收起。
    """
    js = _read("js", "chat-transcript.js")
    css = _read("css", "chat-transcript.css")

    assert js.count("function _setCollapsed(") == 1
    assert js.count("function _toggleCollapse(") == 1
    # 卡头可点、可键盘操作（无障碍 + 不用鼠标也能收）
    assert 'role="button" tabindex="0"' in js
    assert 'ev.key === "Enter" || ev.key === " "' in js
    # done / seg 自动收起，frozen 不自动收起
    assert 'if ((mode === "done" || mode === "seg") && !t.userToggled) _setCollapsed(t, true);' in js
    assert "userToggled = true" in js
    # 收起时补齐打字机：展开不能看到半截推理
    assert 'if (id.startsWith("t:")) _flushTyping(rec);' in js
    # 卡高变化后交给 follow 修正滚动，不自行 scrollIntoView
    assert "if (follow) follow.pin();" in js

    # 收起真的隐藏节点区（不是只加个 class 没人接）
    assert ".transcript-card.collapsed .tcard-line { display: none; }" in css
    assert ".transcript-card.collapsed .tcard-caret" in css
    assert ".transcript-card.collapsed .tcard-hint { display: inline; }" in css


def test_static_assets_have_no_syntax_errors():
    """node 存在时做一次真解析（node --check），保证不是「看着对其实语法错」。"""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return
    for rel in ("js/chat-transcript.js", "js/chat-follow.js", "js/chat-core.js", "js/app.js"):
        r = subprocess.run(
            [node, "--check", str(STATIC_DIR / rel)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{rel} 语法错误: {r.stderr}"