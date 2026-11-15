"""工作台 v3 · S3 接线测试（嵌套条件树）。

关键背景：编译器（json_compiler.py:669 `type:'group'`）早就支持嵌套，但旧构造器
遇到嵌套组显示「编辑暂不支持」且用 `_raw` 字符串原样带回——用户用旧 UI 编辑一次
带组的策略，结构虽然不会丢（_rulesToJson 会 JSON.parse 回去），但组内完全改不了。
S3 把树做成可编辑的。AST 同构的强验证在 Node 冒烟（test 输出记录于提交信息）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BT_JS = ROOT / "server/static/js/backtest.js"
BT_CSS = ROOT / "server/static/css/backtest.css"
INDEX = ROOT / "server/static/index.html"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_no_more_raw_groups():
    """旧行为：嵌套组整组序列化成字符串带回、显示「编辑暂不支持」——必须已移除。"""
    js = _text(BT_JS)
    assert "r._raw" not in js, "仍存在组兜底代码"
    assert "编辑暂不支持" not in js


def test_recursive_parse_and_serialize():
    js = _text(BT_JS)
    # 解析侧：递归进子组
    assert re.search(r"_parseRules\(\{ rules: r\.rules \}\)", js)
    # 序列化侧：组递归 + 空组跳过
    assert "r.type === 'group'" in js
    assert "if (!kids.length) return null;" in js


def test_path_addressing_exists():
    """增删改走 path 寻址（"0/1" = 根下第 0 组的第 1 条），不再用顶层下标。"""
    js = _text(BT_JS)
    for fn in ("_builderAddLeafAt", "_builderAddGroupAt", "_builderRemoveAt",
               "_builderRuleProp", "_builderGroupLogic", "_rulesArrAt", "_nodeAt"):
        assert fn in js, f"缺 {fn}"
    assert "^(buy|sell):([0-9/]*):(L|R)$" in js, "_resolvePrefix 未改为 path 寻址"


def test_depth_limit():
    js = _text(BT_JS)
    assert "depth < 2" in js, "子组深度上限（根 + 2 层）未实现"


def test_preview_containers_and_logic():
    html = _text(INDEX)
    assert 'id="btBuyPreview"' in html and 'id="btSellPreview"' in html
    js = _text(BT_JS)
    assert "_builderRenderPreview" in js
    assert "_countEmptyGroups" in js, "空组提示缺失（保存时静默忽略会让用户困惑）"
    assert "且" in js and "或" in js


def test_group_css_present():
    css = _text(BT_CSS)
    for cls in (".bt-builder-group", ".bt-builder-group-head",
                ".bt-builder-preview", '.bt-builder-group[data-depth="1"]'):
        assert cls in css, f"CSS 缺 {cls}"
