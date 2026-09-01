import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_trace_viewer_static_assets_are_revalidatable():
    server_py = (ROOT / "utils" / "agent_trace" / "server.py").read_text(encoding="utf-8")

    assert "def end_headers" in server_py
    assert "Cache-Control" in server_py
    assert "no-cache, max-age=0, must-revalidate" in server_py
    assert "private, max-age=600" in server_py


def test_trace_viewer_llm_messages_use_step_output_boundary():
    render_js = (ROOT / "utils" / "agent_trace" / "js" / "render.js").read_text(encoding="utf-8")
    analysis_css = (ROOT / "utils" / "agent_trace" / "css" / "analysis.css").read_text(encoding="utf-8")

    assert "function outputMessageSignatures" in render_js
    assert "function splitMessagesByStepOutput" in render_js
    assert "messageSignature(m)" in render_js
    assert "msgs.length - 1" in render_js
    assert "renderDrawerMessagesContent(s)" in render_js
    assert "输入上下文" in render_js
    assert "本次输出" in render_js
    assert "inputRoles" not in render_js
    assert "outputRoles" not in render_js
    assert "消息重复" in render_js
    assert "段内" in render_js

    assert ".msg-group-body-nested" in analysis_css
    assert ".msg-group-layer-input" in analysis_css
    assert ".msg-group-layer-output" in analysis_css


def test_trace_viewer_formats_fenced_json_as_json_block():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const escapeHtml = (value) => String(value)
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;');

global.document = {
  createElement() {
    return {
      _textContent: '',
      innerHTML: '',
      set textContent(value) {
        this._textContent = String(value);
        this.innerHTML = escapeHtml(value);
      },
      get textContent() {
        return this._textContent;
      }
    };
  }
};

vm.runInThisContext(fs.readFileSync('utils/agent_trace/js/utils.js', 'utf8'));
vm.runInThisContext(fs.readFileSync('utils/agent_trace/js/format.js', 'utf8'));

const samples = [
  '```json\n{"steps":[{"step":1,"skill":"mx_data"}]}\n```',
  '```json {"steps":[{"step":1,"skill":"mx_data"}]} ```',
  '说明\n```json\n{"steps":[{"step":1,"skill":"mx_data"}]}\n```\n结束'
];

for (const raw of samples) {
  const html = renderFormattedContent(raw);
  if (!html.includes('fmt-header json')) {
    throw new Error(`expected fenced json to render as JSON block, got: ${html}`);
  }
  if (!html.includes('json-key')) {
    throw new Error(`expected JSON syntax highlight classes, got: ${html}`);
  }
  if (html.includes('md-codeblock')) {
    throw new Error(`expected fenced json not to render as plain markdown code block, got: ${html}`);
  }
}
"""

    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
