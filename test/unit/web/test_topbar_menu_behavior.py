"""顶栏分组菜单交互行为测试 —— Node + DOM 桩，真跑一遍内联控制器。

**存在理由**：静态契约测试（test_topbar_nav.py）只能证明结构与类名对，
证明不了「点了 A 再点 B，A 会不会收起」。这是纯 JS 行为，只有真执行才测得到。

若 `closeAll` 失效，多个菜单会同时展开并互相叠压 —— 比重构前的单一「更多」还糟，
所以这几条断言是这轮改动里最该有的一条。

覆盖：单开互斥 / 二次点击收起 / 点外部关闭 / Esc 关闭 / 点菜单项后收起 / aria-expanded 同步。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = ROOT / "server" / "static" / "index.html"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(process.env.INDEX_HTML, 'utf8');
const MARK = '// 顶栏分组菜单：单开互斥';
const k = html.indexOf(MARK);
if (k < 0) { console.error('NO_CONTROLLER'); process.exit(4); }
const openTag = html.lastIndexOf('<script>', k);
const closeTag = html.indexOf('</script>', k);
const script = html.slice(openTag + '<script>'.length, closeTag);
// 只截控制器这一段：同 <script> 里后面的「示例问题 chip」逻辑依赖本桩未建的 DOM
const end = script.indexOf('})();', script.indexOf(MARK)) + '})();'.length;
const ctrl = script.slice(script.indexOf(MARK), end);

function el(tag, classes, parent) {
  const e = {
    tag, _cls: new Set(classes || []), _attrs: {}, _children: [], parent: parent || null,
    listeners: {},
    classList: {
      add: (c) => e._cls.add(c),
      remove: (c) => e._cls.delete(c),
      contains: (c) => e._cls.has(c),
      toggle: (c, force) => {
        const want = force === undefined ? !e._cls.has(c) : !!force;
        if (want) e._cls.add(c); else e._cls.delete(c);
        return want;
      },
    },
    setAttribute: (n, v) => { e._attrs[n] = String(v); },
    getAttribute: (n) => (n in e._attrs ? e._attrs[n] : null),
    addEventListener: (t, fn) => { (e.listeners[t] = e.listeners[t] || []).push(fn); },
    querySelector: (sel) => {
      const cls = sel.slice(1);
      return e._children.find((c) => c._cls.has(cls)) || null;
    },
    closest: (sel) => {
      const cls = sel.slice(1);
      let cur = e;
      while (cur) { if (cur._cls.has(cls)) return cur; cur = cur.parent; }
      return null;
    },
    fire: (t, ev) => {
      (e.listeners[t] || []).forEach((fn) =>
        fn(Object.assign({ type: t, stopPropagation() {} }, ev || {})));
    },
  };
  if (parent) parent._children.push(e);
  return e;
}

const groups = [];
for (let i = 0; i < 4; i++) {
  const g = el('div', ['tool-group']);
  const b = el('button', ['tool-group-btn'], g);
  b.setAttribute('aria-expanded', 'false');
  const m = el('div', ['tool-menu'], g);
  const item = el('button', ['tool-menu-item'], m);
  groups.push({ g, b, m, item });
}
const docL = {};
global.document = {
  querySelectorAll: (sel) => (sel === '.tool-group' ? groups.map((x) => x.g) : []),
  addEventListener: (t, fn) => { (docL[t] = docL[t] || []).push(fn); },
};

vm.runInThisContext(ctrl, { filename: 'topbar-controller.js' });

// 真事件派发：沿 parent 链冒泡，最后到 document，并尊重 stopPropagation。
// 少了冒泡这层，点菜单项就永远不会触达 menu 上的监听器，测出来的行为是假的。
function dispatch(target, type, extra) {
  const evt = Object.assign(
    { type, target, stopPropagation() { evt._stopped = true; } }, extra || {});
  let cur = target;
  while (cur) {
    (cur.listeners[type] || []).forEach((fn) => { if (!evt._stopped) fn(evt); });
    cur = cur.parent;
  }
  if (!evt._stopped) (docL[type] || []).forEach((fn) => fn(evt));
}

const openSet = () => groups.map((x, i) => (x.g._cls.has('open') ? i : -1)).filter((i) => i >= 0);
const ariaSet = () => groups.map((x) => x.b.getAttribute('aria-expanded'));
const outside = el('div', ['chat-pane']);

const steps = [];

steps.push({ name: 'initial_all_closed', open: openSet(), aria: ariaSet() });

dispatch(groups[0].b, 'click');
steps.push({ name: 'open_first', open: openSet(), aria: ariaSet() });

dispatch(groups[1].b, 'click');
steps.push({ name: 'open_second_closes_first', open: openSet(), aria: ariaSet() });

dispatch(groups[1].b, 'click');
steps.push({ name: 'click_again_collapses', open: openSet(), aria: ariaSet() });

dispatch(groups[2].b, 'click');
dispatch(outside, 'click');
steps.push({ name: 'outside_click_closes', open: openSet(), aria: ariaSet() });

dispatch(groups[3].b, 'click');
dispatch(outside, 'keydown', { key: 'Escape' });
steps.push({ name: 'escape_closes', open: openSet(), aria: ariaSet() });

dispatch(groups[0].b, 'click');
dispatch(groups[0].item, 'click');
steps.push({ name: 'item_click_closes', open: openSet(), aria: ariaSet() });

dispatch(groups[0].b, 'click');
dispatch(groups[0].m, 'click');
steps.push({ name: 'inside_click_keeps_open', open: openSet(), aria: ariaSet() });

process.stdout.write(JSON.stringify(steps));
"""


def _node_bin() -> str | None:
    for cand in (os.environ.get("NODE_BIN"), shutil.which("node")):
        if cand and Path(cand).exists():
            return cand
    managed = Path.home() / ".workbuddy" / "binaries" / "node"
    if managed.is_dir():
        for exe in sorted(managed.glob("versions/*/node.exe"), reverse=True):
            return str(exe)
    return None


@pytest.fixture(scope="module")
def steps(tmp_path_factory):
    node = _node_bin()
    if not node:
        pytest.skip("未找到 node，跳过顶栏菜单行为测试")

    d = tmp_path_factory.mktemp("topbarmenu")
    harness = d / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    env = dict(os.environ, INDEX_HTML=str(INDEX_HTML))
    proc = subprocess.run([node, str(harness)], capture_output=True, env=env, timeout=60)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, "控制器执行失败:\n%s\n%s" % (err, out[:800])
    assert out, "无输出"
    return json.loads(out)


def _step(steps, name):
    hit = [s for s in steps if s["name"] == name]
    assert hit, f"缺失步骤 {name}（已跑：{[s['name'] for s in steps]}）"
    return hit[0]


def test_controller_is_extractable_from_index_html():
    """控制器必须还能被 marker 定位 —— marker 改了这条会先失败，便于定位。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "// 顶栏分组菜单：单开互斥" in html
    assert html.count("closeAll") >= 3


def test_menus_start_closed(steps):
    s = _step(steps, "initial_all_closed")
    assert s["open"] == []
    assert s["aria"] == ["false"] * 4


def test_single_open_exclusion(steps):
    """核心：开第二个必须关掉第一个，否则菜单会叠压。"""
    s = _step(steps, "open_second_closes_first")
    assert s["open"] == [1], f"应只有第 2 个展开，实际 {s['open']}"
    assert s["aria"] == ["false", "true", "false", "false"]


def test_second_click_collapses(steps):
    s = _step(steps, "click_again_collapses")
    assert s["open"] == []
    assert s["aria"] == ["false"] * 4


def test_outside_click_closes(steps):
    s = _step(steps, "outside_click_closes")
    assert s["open"] == [], f"点外部未收起：{s['open']}"


def test_escape_closes(steps):
    s = _step(steps, "escape_closes")
    assert s["open"] == [], f"Esc 未收起：{s['open']}"


def test_clicking_a_menu_item_closes_the_menu(steps):
    s = _step(steps, "item_click_closes")
    assert s["open"] == [], f"点菜单项后未收起：{s['open']}"


def test_click_inside_the_group_does_not_close(steps):
    """点分组内非菜单项的区域（如标题）应保持展开，不该被「点外部」逻辑误关。"""
    s = _step(steps, "inside_click_keeps_open")
    assert s["open"] == [0], f"组内点击被误判为外部点击：{s['open']}"


def test_aria_expanded_tracks_visual_state(steps):
    """aria-expanded 必须与 .open 完全一致，否则读屏用户听到的状态是假的。"""
    for s in steps:
        opened = set(s["open"])
        for i, aria in enumerate(s["aria"]):
            assert aria == ("true" if i in opened else "false"), (
                f"步骤 {s['name']}: 组 {i} .open={i in opened} 但 aria-expanded={aria}"
            )
