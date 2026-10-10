"""拥挤度面板渲染冒烟测试 —— Node + DOM 桩，真跑一遍渲染函数。

**存在理由**：`node --check` 只验证语法，抓不到运行时类型错误。拥挤度面板里全是
"读数可能是 None" 的取数（陈旧/待补数标的的 score、level、ext_dev 都是 None），
`num()` / `signed()` 里少一个 isFinite 判断就会整块「加载失败」。

因此这里不比对文案，而是**把渲染函数真正执行一遍**，只要抛异常就失败；
并顺带覆盖三个分支：正常 / 空库 / 扫描进行中，以及类型与档位筛选。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PAGE_JS = ROOT / "server" / "static" / "js" / "crowding.js"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const pageJs = fs.readFileSync(process.env.CROWDING_JS, 'utf8');
const fx = JSON.parse(fs.readFileSync(process.env.CROWDING_FIXTURE, 'utf8'));

function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, disabled: false,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 contains(c) { return this._s.has(c); } },
    addEventListener() {},
    querySelectorAll() { return []; },
  };
}
const els = {};
const getEl = (id) => (els[id] || (els[id] = makeEl(id)));

const document = {
  getElementById: getEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => makeEl(t),
  head: { appendChild() {} },
  body: { appendChild() {} },
};
const windowStub = {};
global.document = document;
global.window = windowStub;
global.fetch = (url) => {
  const u = String(url).replace(/^https?:\/\/[^/]+/, '');
  const body = fx[u] || fx[u.split('?')[0]] || fx['__default__'];
  if (!body) return Promise.reject(new Error('no fixture for ' + u));
  return Promise.resolve({ json: () => Promise.resolve(body) });
};

vm.runInThisContext(pageJs, { filename: 'crowding.js' });

const panel = windowStub.initCrowdingScan();
if (!panel) { console.error('NO_API'); process.exit(4); }

async function main() {
  await panel.load();
  const h = (id) => getEl(id).innerHTML;
  const t = (id) => getEl(id).textContent;
  const out = {
    meta: t('crMeta'),
    cards: h('crCards'),
    strip: h('crStrip'),
    warn: h('crWarn'),
    warnDisplay: getEl('crWarn').style.display || '',
    table: h('crTable'),
    foot: t('crFoot'),
    scanBtnDisabled: !!getEl('crBtnScan').disabled,
    scanBtnClass: getEl('crBtnScan').classList.contains('scanning'),
  };

  getEl('crFilterCat').value = 'board';
  panel._renderTable();
  out.tableBoardOnly = h('crTable');

  getEl('crFilterCat').value = 'index';
  panel._renderTable();
  out.tableIndexOnly = h('crTable');

  getEl('crFilterCat').value = 'all';
  getEl('crFilterLevel').value = 'hot';
  panel._renderTable();
  out.tableHotOnly = h('crTable');

  getEl('crFilterLevel').value = 'ok';
  panel._renderTable();
  out.tableOkOnly = h('crTable');

  getEl('crFilterLevel').value = 'pending';
  panel._renderTable();
  out.tablePending = h('crTable');

  getEl('crFilterLevel').value = 'all';
  panel._renderTable();
  out.tableAll = h('crTable');

  process.stdout.write(JSON.stringify(out));
}
main().catch((e) => {
  console.error('RENDER_ERROR: ' + ((e && e.stack) || e));
  process.exit(3);
});
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


def _item(key, name, category, score, level, status="ok", **over):
    base = {
        "key": key, "category": category, "code": key.split(":", 1)[-1], "name": name,
        "close": 12345.6, "ext_dev": 22.5, "ret20": 9.8, "ret60": 35.2,
        "amount_ratio": 1.9, "high_gap": -1.2, "vol20": 28.4,
        "score": score, "level": level, "status": status,
        "parts": {"ext": 78, "mom": 55, "vol": 78, "chase": 78},
        "detail": "明细 " + name, "asof": "2026-09-17", "rows": 130, "stale_days": 0,
    }
    base.update(over)
    return base


def _latest_payload():
    items = [
        _item("index:H30269", "中证酒", "index", 88.5, "过热"),
        _item("board:industry_半导体", "半导体", "board", 78.3, "拥挤"),
        _item("index:000300", "沪深300", "index", 62.0, "偏挤"),
        _item("board:industry_银行", "银行", "board", 41.0, "中性"),
        _item("board:industry_医药", "医药", "board", 55.0, None, status="stale",
              ext_dev=-2.0, ret20=None, ret60=None, amount_ratio=None, asof="2026-08-20"),
        _item("board:industry_传媒", "传媒", "board", None, None, status="insufficient",
              close=None, ext_dev=None, ret20=None, ret60=None, amount_ratio=None,
              rows=12, asof=None),
    ]
    return {"code": 0, "msg": "ok", "data": {
        "available": True, "trade_date": "2026-09-17",
        "scanned_at": "2026-09-17 21:00:00", "elapsed_ms": 4210,
        "pool_size": 6, "computed_count": 4, "insufficient_count": 1,
        "stale_count": 1, "hot_count": 2, "collected_count": 152,
        "pool": {"indices": 2, "boards": 4},
        "params": {"hot_score": 75.0, "ma_days": 250},
        "weights": {"ext": 0.30, "mom": 0.25, "vol": 0.25, "chase": 0.20},
        "labels": {"ext": "均线延伸", "mom": "20日动量", "vol": "放量比", "chase": "60日追高"},
        "levels": [[40, "舒适"], [60, "中性"], [75, "偏挤"], [85, "拥挤"], [101, "过热"]],
        "levels_order": ["舒适", "中性", "偏挤", "拥挤", "过热"],
        "hot_score": 75.0,
        "warnings": ["板块池有 496 个待补数，本轮先补 150 个（上游限流，按游标分批推进）"],
        "pool_problems": [], "hot": [],
        "counts_by_level": {"舒适": 0, "中性": 1, "偏挤": 2, "拥挤": 1, "过热": 1},
        "items": items, "item_total": 6,
    }}


def _empty_payload():
    return {"code": 0, "msg": "ok", "data": {
        "available": False, "trade_date": None, "items": [], "levels": [],
        "labels": {}, "levels_order": [], "counts_by_level": {},
        "pool_problems": ["池配置不存在（已用内置默认，可写一份到 data/crowding/pool.json）"],
        "pool": {"indices_max": 60, "boards": {}},
    }}


def _status_payload(running=False, summary=None):
    return {"code": 0, "msg": "ok", "data": {
        "jobs": [{"key": "crowding_scan", "name": "全市场拥挤度扫描", "running": running,
                  "run_count": 3, "fail_count": 0, "skip_count": 0,
                  "last_ok": True, "last_summary": summary,
                  "last_finished_at": "2026-09-17 20:00:00",
                  "next_run_at": "2026-09-18 20:00:00"}],
        "scheduled": ["crowding_scan"],
    }}


def _render(tmp_path, fixture: dict) -> dict:
    node = _node_bin()
    if not node:
        pytest.skip("未找到 node，跳过前端渲染冒烟测试")

    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    fx = tmp_path / "fixture.json"
    fx.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    env = dict(os.environ, CROWDING_JS=str(PAGE_JS), CROWDING_FIXTURE=str(fx))
    proc = subprocess.run([node, str(harness)], capture_output=True, env=env, timeout=90)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, "渲染失败:\n%s\n%s" % (err, out[:600])
    assert out, "渲染结果为空"
    return json.loads(out)


@pytest.fixture
def fixture_full():
    return {"/api/crowding/latest": _latest_payload(),
            "/api/scans/status": _status_payload(running=False)}


@pytest.fixture
def fixture_empty():
    return {"/api/crowding/latest": _empty_payload(),
            "/api/scans/status": _status_payload(running=False)}


@pytest.fixture
def fixture_scanning():
    return {"/api/crowding/latest": _latest_payload(),
            "/api/scans/status": _status_payload(running=True, summary="池 516 · 算分 3")}


class TestFullRender:
    def test_renders_without_runtime_error(self, tmp_path, fixture_full):
        """回归：score/level/读数全为 None 的标的不能把渲染打断。"""
        out = _render(tmp_path, fixture_full)
        assert "中证酒" in out["table"]
        assert "半导体" in out["table"]

    def test_meta_and_cards(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert "数据日期 2026-09-17" in out["meta"]
        assert "扫描于 2026-09-17 21:00:00" in out["meta"]
        assert "标的池" in out["cards"]
        assert "已算分" in out["cards"]
        assert "拥挤 + 过热" in out["cards"]
        assert "待补数" in out["cards"]
        assert "个（指数 2 + 板块 4）" in out["cards"]

    def test_level_strip_and_foot(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert "过热" in out["strip"]
        assert "20%" in out["strip"]          # 1 / 5 个算分标的
        assert "共 6 个标的" in out["foot"]
        assert "本轮补数 152" in out["foot"]

    def test_warnings_block_is_visible(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert out["warnDisplay"] == "block"
        assert "板块池有 496 个待补数" in out["warn"]

    def test_level_classes_mapping(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert 'class="cr-level lv-4"' in out["table"]     # 过热
        assert 'class="cr-level lv-3"' in out["table"]     # 拥挤
        assert 'class="cr-level lv-1"' in out["table"]     # 偏挤/中性

    def test_pending_rows_do_not_render_null_scores(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert "陈旧" in out["table"]
        assert "待补数" in out["table"]
        assert "null" not in out["table"]
        assert "undefined" not in out["table"]
        assert "NaN" not in out["table"]
        assert "—" in out["table"]        # 缺失读数走「—」而不是空

    def test_signed_readings(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert "+22.5%" in out["table"]
        assert "+9.8%" in out["table"]
        assert "+35.2%" in out["table"]

    def test_category_filter(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert "半导体" in out["tableBoardOnly"]
        assert "中证酒" not in out["tableBoardOnly"]
        assert "中证酒" in out["tableIndexOnly"]
        assert "半导体" not in out["tableIndexOnly"]
        assert "中证酒" in out["tableAll"]

    def test_level_filter(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert "中证酒" in out["tableHotOnly"]      # 88.5 ≥ 75
        assert "半导体" in out["tableHotOnly"]      # 78.3 ≥ 75
        assert "银行" not in out["tableHotOnly"]
        assert "银行" in out["tableOkOnly"]        # status=ok 但不够热
        assert "中证酒" in out["tableOkOnly"]
        assert "医药" not in out["tableOkOnly"]    # stale
        assert "医药" in out["tablePending"]
        assert "传媒" in out["tablePending"]
        assert "银行" not in out["tablePending"]

    def test_scan_button_idle_state(self, tmp_path, fixture_full):
        out = _render(tmp_path, fixture_full)
        assert out["scanBtnDisabled"] is False
        assert out["scanBtnClass"] is False


class TestEmptyRender:
    def test_not_available_branch(self, tmp_path, fixture_empty):
        out = _render(tmp_path, fixture_empty)
        assert out["meta"] == "尚未扫描"
        assert out["cards"] == ""
        assert out["strip"] == ""
        assert out["warnDisplay"] == "block"
        assert "池配置不存在" in out["warn"]
        assert "还没有扫描记录" in out["foot"]
        assert "没有符合条件的标的" in out["table"]


class TestScanningState:
    def test_running_scan_disables_button(self, tmp_path, fixture_scanning):
        out = _render(tmp_path, fixture_scanning)
        assert out["scanBtnDisabled"] is True
        assert out["scanBtnClass"] is True
        assert "扫描进行中" in out["foot"]
        assert "池 516 · 算分 3" in out["foot"]

    def test_renders_board_without_scan_state(self, tmp_path):
        """扫描状态接口失败不能影响榜单渲染（_scanState 内部已兜住异常）。"""
        out = _render(tmp_path, {"/api/crowding/latest": _latest_payload()})
        assert "中证酒" in out["table"]
        assert out["scanBtnDisabled"] is False
