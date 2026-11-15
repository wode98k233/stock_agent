"""顶层面板（overlay）不得互相嵌套。

事故（2026-10-18，用户发现）：插入「池子研究」面板时**多吃掉了一个 </div>**，
导致 #factorOverlay 没闭合，把后面的 #backtestOverlay 与 #poolStudyOverlay 都变成了
它的子节点。而 factorOverlay 关闭时是 display:none ——
于是「策略回测」和「池子研究」点开时 class 明明加上了 visible、display 也是 flex，
**高度却是 0**，用户侧就是「点了没反应」。

这类 bug 代码审查看不出来（标签数差一个），必须在测试里锁住：
任何一个 overlay 都不允许出现在另一个 overlay 的祖先链里。
"""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INDEX = ROOT / "server/static/index.html"

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


class _Ancestry(HTMLParser):
    """记录每个带 id 的元素，其祖先链上出现过哪些 id。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # [(tag, id)]
        self.ancestors = {}      # id -> [祖先 id...]
        self.depth = {}          # id -> 深度

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        eid = d.get("id")
        if eid:
            self.ancestors[eid] = [i for _, i in self.stack if i]
            self.depth[eid] = len(self.stack)
        if tag not in _VOID:
            self.stack.append((tag, eid or ""))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _parse():
    p = _Ancestry()
    p.feed(INDEX.read_text(encoding="utf-8"))
    return p


def _overlay_ids() -> list:
    """index.html 里所有「顶层弹层」的 id：class 含 overlay 的 div。"""
    html = INDEX.read_text(encoding="utf-8")
    return re.findall(r'<div[^>]*\bid="([^"]+)"[^>]*class="[^"]*\boverlay\b', html)


def test_overlay_ids_found():
    ids = _overlay_ids()
    assert len(ids) >= 5, f"只找到 {len(ids)} 个 overlay，解析或写法变了？{ids}"
    for must in ("backtestOverlay", "factorOverlay", "poolStudyOverlay"):
        assert must in ids, f"缺少 {must}"


def test_no_overlay_nested_in_another():
    """核心断言：overlay 之间不得互为祖先。"""
    p = _parse()
    ids = set(_overlay_ids())
    bad = []
    for oid in sorted(ids):
        for anc in p.ancestors.get(oid, []):
            if anc in ids:
                bad.append(f"{oid} 嵌在 {anc} 里面")
    assert not bad, ("面板互相嵌套 —— 外层关闭时内层永远打不开（点了没反应）：\n  "
                     + "\n  ".join(bad))


def test_overlays_are_siblings():
    """三个对话框必须**同级**且祖先链上没有任何 id（= 直接挂在 body 下）。

    不去断言具体深度数字：解析器把 html/body 也算进栈，数字会随写法漂移。
    要锁的是「它们彼此同级、且没被任何带 id 的容器包住」——弹层被容器包住就会被
    那个容器的 display/overflow/transform 影响（本次事故正是被 display:none 的外层藏住）。
    """
    p = _parse()
    ids = ("backtestOverlay", "factorOverlay", "poolStudyOverlay")
    depths = {i: p.depth.get(i) for i in ids}
    assert len(set(depths.values())) == 1, f"三个弹层不同级：{depths}"
    for i in ids:
        assert p.ancestors.get(i) == [], \
            f"{i} 被带 id 的容器包住了：祖先={p.ancestors.get(i)}"


def test_div_tags_balanced_in_body():
    """<div> 开合必须平衡 —— 少一个 </div> 就是本次事故的根因。"""
    html = INDEX.read_text(encoding="utf-8")
    opens = len(re.findall(r"<div\b", html))
    closes = len(re.findall(r"</div>", html))
    assert opens == closes, f"<div> 不平衡：开 {opens} / 合 {closes}"
