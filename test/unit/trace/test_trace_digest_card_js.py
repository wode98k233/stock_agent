"""Trace Viewer 的「状态摘要卡」渲染（选项 A 的前端护栏）。

设计 §9.3：节点步的 `state_digest` 不能裸显示 JSON，要渲染成
「累积容器（只记形状）+ 已裁剪字段 + 保留字段」的卡片；LLM 步的
`llm_prompt_ref` 则提示"切到消息 tab 看全文"。

为什么用 node 跑：`renderSummaryCard` 是纯字符串函数，但它是 viewer 的浏览器端
代码，Python 侧测不到。这里按 `test_trace_static_assets.py` 同款方式在 node 里
`vm.runInThisContext` 加载 `utils.js → format.js → render.js`（三者都只有函数声明，
没有加载期 DOM 访问），直接断言产出 HTML。
"""
import json
import subprocess
from pathlib import Path

_project_root = Path(__file__).resolve().parents[3]

_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');

// render.js 顶部的函数会引用全局 S（viewer 的 state.js 提供）
global.S = { selectedId: null, selectedStep: null, drawerTab: 'input', runs: [] };
const escapeHtml = (value) => String(value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.document = {
  // utils.js::esc 走 createElement('div') 的 textContent/innerHTML 转义
  createElement() {
    return {
      _textContent: '',
      innerHTML: '',
      set textContent(value) { this._textContent = String(value); this.innerHTML = escapeHtml(value); },
      get textContent() { return this._textContent; },
    };
  },
  getElementById: () => ({ innerHTML: '', classList: { add(){}, remove(){}, contains(){ return false; } } }),
};

vm.runInThisContext(fs.readFileSync('utils/agent_trace/js/utils.js', 'utf8'));
vm.runInThisContext(fs.readFileSync('utils/agent_trace/js/format.js', 'utf8'));
vm.runInThisContext(fs.readFileSync('utils/agent_trace/js/render.js', 'utf8'));

// node -e 时 process.argv = [node, 参数1, 参数2, ...]（没有脚本路径）
const digest = JSON.parse(process.argv[1]);
const llmRef = JSON.parse(process.argv[2]);
const legacy = process.argv[3];

const out = {
  state: renderSummaryCard(digest),
  llm: renderSummaryCard(llmRef),
  legacyParsed: parseTraceSummary(legacy),
  digestParsed: parseTraceSummary(JSON.stringify(digest)),
};

// 摘要行不得被当成"普通 JSON"直接丢给格式化器
if (out.legacyParsed !== null) throw new Error('legacy plain text should not parse as summary');
if (!out.digestParsed || out.digestParsed._kind !== 'state_digest') throw new Error('digest not detected');
if (!out.state.includes('累积容器')) throw new Error('missing 累积容器 section');
if (!out.state.includes('已裁剪字段')) throw new Error('missing 已裁剪字段 section');
if (!out.state.includes('保留字段')) throw new Error('missing 保留字段 section');
if (!out.state.includes('selected_template_id')) throw new Error('保留字段未渲染（quality_service 口径）');
if (!out.state.includes('market_daily')) throw new Error('保留字段值未渲染');
// 卡片只该显示 acc_head 截断后的片段（≤256 字符），不该出现整段内容体
const mCount = (out.state.match(/M/g) || []).length;
if (mCount > 220) throw new Error('累积容器片段未按 acc_head 截断: ' + mCount + ' 个 M');
if (out.state.includes('\u0001B')) throw new Error('卡片里出现 blob 哨兵');
if (!out.llm.includes('prompt 已瘦身存储')) throw new Error('llm_prompt_ref 提示缺失');
if (!out.llm.includes('切到')) throw new Error('llm_prompt_ref 未指引到消息 tab');

console.log(JSON.stringify({ ok: true, stateLen: out.state.length, llmLen: out.llm.length }));
"""


def _digest_payload():
    """与 utils/agent_trace/payload.py::digest_state 同形（平铺保留字段）。"""
    return {
        "_kind": "state_digest",
        "_orig_chars": 128411,
        "_keys": ["messages", "tool_calls", "dispatch_history", "selected_template_id",
                  "current_step_index", "response"],
        "_acc": {
            "messages": {"type": "list", "count": 18, "chars": 92216, "last": "M" * 200},
            "tool_calls": {"type": "list", "count": 5, "chars": 67432, "last": "R" * 200},
        },
        "_clipped": {"response": {"type": "str", "chars": 8412, "head": "B" * 120}},
        "_truncated": ["response"],
        "selected_template_id": "market_daily",
        "current_step_index": 3,
    }


def test_state_digest_card_renders_sections_not_raw_json():
    digest = _digest_payload()
    llm_ref = {
        "_kind": "llm_prompt_ref",
        "msg_count": 18,
        "chars": 128411,
        "sha256": "0123456789abcdef",
        "preview": "content='[背景信息] ...",
    }
    legacy = '[\n  [\n    "content=\'x\'"\n  ]\n]'

    result = subprocess.run(
        ["node", "-e", _NODE_SCRIPT,
         json.dumps(digest, ensure_ascii=False),
         json.dumps(llm_ref, ensure_ascii=False),
         legacy],
        cwd=str(_project_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"node 渲染断言失败:\n{result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["stateLen"] > 200
