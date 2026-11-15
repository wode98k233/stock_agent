"""红线守卫：**名称→代码的映射是数据，不是代码**。

为什么要有这个测试（2026-11-01）
--------------------------------
用户指出「名称→代码不许写死在代码里」。先把 `agents/deep_report/resolver.py` 和
`backtest/pool_scope.py` 的两张表迁到了 `market_data.db.symbol_alias`，但当时的守卫
只扫 resolver.py **一个文件** —— 属于「假达标」：同一批别名在 `backtest/pool_scope.py`
（`_INDEX_ALIASES`）、`signal_eval/symbols.py`（`_ETf_ALIASES`）、
`agents/scenario_router.py`（`_well_known_stocks`）里各抄了一遍，守卫全都没看见。
第一版全库守卫又只扫了 5 个目录、且只能抓「字典字面量」写法（P2：`dict(名='码')`、
`MAP['名']='码'`、条件表达式、类体枚举、嵌套 tuple 五种写法都能绕过）。

本测试**扫描整个仓库的业务代码与脚本**，只要再出现「别名 → 代码」的解析表就失败。
别名唯一事实源是 `data/symbol_aliases.json`（运营直接改）→
`scripts/ops/seed_symbol_aliases.py --apply`（或查询层自动补种）→
`market_data.db.symbol_alias` → 代码只许通过 `backtest/symbol_alias.py` 查询。

扫描范围（实测约 745 个 .py）
---------------------------
从仓库根遍历 `*.py`，排除：隐藏目录（`.git` / `.venv` / `.trae` …）、`venv/`、
`test/`（测试夹具可以合法构造别名假数据）、运行时产物（`output/` `logs/`）、
`node_modules/`；白名单只有数据侧两处：`data/`（数据文件本身）与
`scripts/ops/seed_*.py`（灌库脚本）。`utils/ thesis/ cli/ scripts/` 与仓库根入口文件
都在扫描范围内（P2：之前漏掉，导致「全库扫描」名不副实）。

判定用 AST 而不是正则（正则会把注释/文档里的「沪深300」也算上）：
A. 字典字面量里出现「中文名 → 代码」（值里带代码即可，容忍 `{'名': ('码',)}`）；
B. `dict(中文名='代码')` 这种关键字实参写法；
C. 赋值写死：`MAP['中文名'] = '代码'`、类体/模块级 `中文名 = '代码'`；
D. 变量名像别名表（alias / synonym / nickname / 别名 / 俗称）且值里 ≥2 个代码；
E. 列表/元组里 ≥1 组「(中文名, 代码)」**有序对**（含嵌套，如 `('沪深300', ('000300',))`）。
   名字在前＝解析表才判违规；`('000300', '沪深300')` 这种 code→展示名的采集清单不判；
F. if/elif/条件表达式：判断里出现 seed 里已知的别名，而分支里出现它对应的代码
   —— 抓 `if '沪深300' in text: target = '000300'` 这种「绕过字典」的写法。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "data" / "symbol_aliases.json"

# kind 语义见 data/symbol_aliases.json 的 _comment（index/etf/stock/synonym）
ALLOWED_KINDS = {"index", "etf", "stock", "synonym"}

# 扫描排除：测试夹具 / 虚拟环境 / 运行时产物 / 数据目录
SKIP_PARTS = {"venv", "__pycache__", "output", "logs", "test", "data", "node_modules"}
# 白名单（数据侧两处）：数据目录 + 灌库脚本
WHITELIST_PREFIXES = ("data/", "scripts/ops/seed_")

_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")
# A 股 6 位（可带 sh/sz/bj 前缀或 .SH/.SZ/.BJ 后缀）；港股 5 位（00700/09988）
_CODE_RE = re.compile(r"^(?:(?:sh|sz|bj)\.?)?\d{5,6}(?:\.(?:SH|SZ|BJ|HK))?$", re.I)
_SIX_DIGIT_RE = re.compile(r"^\d{6}$")
_ALIAS_NAME_RE = re.compile(r"(alias|synonym|nickname|aka|别名|俗称)", re.I)


# ── 数据侧 ─────────────────────────────────────────────────────────

def load_seed() -> List[dict]:
    with open(SEED_PATH, encoding="utf-8") as f:
        return list(json.load(f).get("aliases") or [])


def alias_codes() -> Dict[str, Set[str]]:
    """seed 里已知的 别名 → {对应代码}（规则 F 用；只认 6 位 A 股码）。"""
    out: Dict[str, Set[str]] = {}
    for r in load_seed():
        alias, code = str(r["alias"]), str(r["code"])
        if _SIX_DIGIT_RE.match(code.strip()):
            out.setdefault(alias, set()).add(code.strip())
    return out


# ── AST 取字面量 ───────────────────────────────────────────────────

def _const_str(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _const_code(node) -> str:
    text = _const_str(node).strip()
    return text if text and _CODE_RE.match(text) else ""


def _codes_under(node) -> List[str]:
    """子树里所有「像代码」的字符串字面量（含嵌套容器）。"""
    return [c for c in (_const_code(n) for n in ast.walk(node)) if c]


def _cjk_strings_under(node) -> List[str]:
    return [s for s in (_const_str(n) for n in ast.walk(node)) if s and _CJK_RE.search(s)]


# ── 扫描实现 ───────────────────────────────────────────────────────

def scan_source(source: str, known: Dict[str, Set[str]]) -> List[Tuple[int, str]]:
    """源码 → [(行号, 违规原因)]。纯函数，自测用同一份实现。"""
    tree = ast.parse(source)
    hits: List[Tuple[int, str]] = []

    for node in ast.walk(tree):
        # A. 字典字面量：中文名 → 代码（值里带代码就算，容忍再包一层容器）
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                name = _const_str(key)
                if name and _CJK_RE.search(name) and _codes_under(value):
                    hits.append((node.lineno, f"字典里出现「{name} → 代码」"))

        # B. dict(中文名='代码')
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "dict"):
            for kw in node.keywords:
                if kw.arg and _CJK_RE.search(kw.arg) and _codes_under(kw.value):
                    hits.append((node.lineno, f"dict() 关键字「{kw.arg}」→ 代码"))

        # C/D. 赋值写死
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            vcode = _const_code(value) if value is not None else ""
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript):
                    name = _const_str(target.slice)
                    if name and _CJK_RE.search(name) and vcode:
                        hits.append((node.lineno, f"下标赋值「{name}」→ {vcode}"))
                elif isinstance(target, ast.Name) and _CJK_RE.search(target.id) and vcode:
                    hits.append((node.lineno, f"中文标识符 {target.id} = {vcode}"))
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if any(_ALIAS_NAME_RE.search(nm) for nm in names) and len(_codes_under(value)) >= 2:
                hits.append((node.lineno, f"别名常量 {names} 里塞了多个代码"))

        # E. 「(中文名, 代码)」有序对（含嵌套）
        if isinstance(node, (ast.List, ast.Tuple)):
            for el in node.elts:
                if not isinstance(el, (ast.Tuple, ast.List)) or len(el.elts) < 2:
                    continue
                name = _const_str(el.elts[0])
                if not name or not _CJK_RE.search(name):
                    continue
                if any(_codes_under(item) for item in el.elts[1:]):
                    hits.append((node.lineno, f"名单里出现「({name}, 代码)」对"))
                    break

        # F. if / elif / 条件表达式：已知别名 + 分支里对应代码
        tests: List[ast.expr] = []
        branches: List[ast.AST] = []
        if isinstance(node, ast.If):
            tests, branches = [node.test], list(node.body) + list(node.orelse)
        elif isinstance(node, ast.IfExp):
            tests, branches = [node.test], [node.body, node.orelse]
        for test in tests:
            branch_codes: Set[str] = set()
            for branch in branches:
                branch_codes.update(c for c in _codes_under(branch) if _SIX_DIGIT_RE.match(c))
            for name in _cjk_strings_under(test):
                for code in known.get(name.strip(), ()):
                    if code in branch_codes:
                        hits.append((node.lineno, f"判断「{name}」→ 分支里写死 {code}"))

    return sorted(set(hits))


def iter_scan_files() -> Iterable[Path]:
    """要扫描的 .py：整仓业务代码 + 运维脚本（白名单与排除项见模块 docstring）。"""
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        parts = Path(rel).parts
        if any(part.startswith(".") for part in parts):
            continue                       # .git/.venv/.trae/... 隐藏目录
        if set(parts[:-1]) & SKIP_PARTS:
            continue                       # 测试夹具 / 虚拟环境 / 运行时产物 / 数据目录
        if rel.startswith(WHITELIST_PREFIXES):
            continue                       # 白名单：数据文件与灌库脚本
        yield path


def scan_file(path: Path, known: Dict[str, Set[str]]) -> List[Tuple[int, str]]:
    return scan_source(path.read_text(encoding="utf-8"), known)


# ── 守卫本体 ───────────────────────────────────────────────────────

def test_no_hardcoded_symbol_alias_tables():
    """全仓扫描：业务代码里不许再出现名称 → 代码的表。"""
    files = list(iter_scan_files())
    # 扫描范围自检：路径改了/白名单写宽了，不能变成「什么都没扫」还绿
    assert len(files) > 600, f"扫描范围异常：只找到 {len(files)} 个 .py，检查 iter_scan_files"

    known = alias_codes()
    assert len(known) > 60, f"seed 别名解析异常（{len(known)} 条）"

    problems: List[str] = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            hits = scan_file(path, known)
        except SyntaxError as e:           # 解析不了就等于没守住，直接报出来
            problems.append(f"{rel}:{e.lineno}  源码无法解析，守卫无法判定")
            continue
        problems.extend(f"{rel}:{lineno}  {why}" for lineno, why in hits)

    assert not problems, (
        "红线：名称→代码的映射是数据，不是代码。\n"
        "请把别名加进 data/symbol_aliases.json 后跑 "
        "`python scripts/ops/seed_symbol_aliases.py --apply`，代码里改成查 "
        "backtest/symbol_alias.py（lookup_one / lookup_group / list_aliases / "
        "find_alias_in_text）。\n命中：\n  " + "\n  ".join(problems))


def test_guard_covers_whole_repo():
    """回归护栏：曾经写死过别名表的文件必须在扫描范围内，且不能只扫某几个目录。"""
    scanned = {p.relative_to(ROOT).as_posix() for p in iter_scan_files()}
    for rel in ("agents/deep_report/resolver.py",
                "backtest/pool_scope.py",
                "backtest/symbol_alias.py",
                "signal_eval/symbols.py",
                "agents/scenario_router.py"):
        assert rel in scanned, f"{rel} 没被扫描（白名单放太宽？）"
    # P2：之前只扫 5 个目录，utils/thesis/cli/scripts 全漏
    for prefix in ("utils/", "thesis/", "cli/", "scripts/", "server/", "tools/"):
        assert any(rel.startswith(prefix) for rel in scanned), f"{prefix} 没被扫描"
    # 扫描范围里不该混进测试夹具/虚拟环境
    assert not any(rel.startswith("test/") for rel in scanned)
    assert not any(rel.startswith("venv/") for rel in scanned)


# ── 守卫自测：确保它真的能抓到东西（不是又一个假达标）──────────────

BAD_SNIPPETS = {
    "字典表": "_INDEX_ALIASES = {'沪深300': '000300'}\n",
    "单个别名对": "MAP = {'中证500': '000905'}\n",
    "列表元组表": (
        "_ETf_ALIASES = [('红利ETF', '510880'), ('消费ETF', '159928')]\n"),
    "别名常量": "_ALIASES = ('红利ETF', '510880', '消费ETF', '159928')\n",
    "if 旁路映射": (
        "def _scope(raw):\n"
        "    if '沪深300' in raw:\n"
        "        return 'index', '000300'\n"
        "    return None\n"),
    "elif 旁路映射": (
        "def _scope(raw):\n"
        "    if '混合' in raw:\n"
        "        return 'mixed'\n"
        "    elif '中证500' in raw:\n"
        "        return 'index', '000905'\n"
        "    return None\n"),
    # ↓ P2 复审查出的 5 种绕过写法，一个都不许再漏
    "dict() 关键字实参": "M = dict(沪深300='000300', 中证500='000905')\n",
    "下标赋值": "MAP = {}\nMAP['沪深300'] = '000300'\n",
    "条件表达式": "VAL = '000300' if '沪深300' in text else '000905'\n",
    "类体枚举": "class Idx:\n    沪深300 = '000300'\n",
    "嵌套 tuple": "T = [('沪深300', ('000300',))]\n",
}

GOOD_SNIPPETS = {
    "查库": (
        "def resolve(text):\n"
        "    from backtest.symbol_alias import lookup_one\n"
        "    return lookup_one(text, kind='index')\n"),
    "采集清单（code → 展示名）": (
        "DEFAULT_INDICES = [('000300', '沪深300'), ('000905', '中证500')]\n"),
    "任务定义（dict 里 code/name 各一格）": (
        "INDEX_TASKS = [{'code': '000300', 'name': '沪深300', 'category': '规模'}]\n"),
    "散落字面量": (
        "BENCH_FALLBACK = '000300'  # 沪深300 兜底\n"),
    "纯中文标签": (
        "LABELS = {'指数': '规模', '债券': '固收'}\n"),
    "日志里念名字": (
        "def log_it(logger, name):\n"
        "    logger.info('范围：%s', name)\n"),
}


@pytest.mark.parametrize("snippet", BAD_SNIPPETS.values(), ids=list(BAD_SNIPPETS))
def test_guard_flags_known_bad_snippets(snippet):
    """守卫自测：以前真实出现过、以及评审实测能绕过的写法，一个都不能漏。"""
    assert scan_source(snippet, alias_codes()), "守卫漏了已知的违规写法"


@pytest.mark.parametrize("snippet", GOOD_SNIPPETS.values(), ids=list(GOOD_SNIPPETS))
def test_guard_allows_legit_patterns(snippet):
    """守卫不能把「查库」「code→展示名清单」「纯中文标签」误判成别名表。"""
    assert scan_source(snippet, alias_codes()) == [], "守卫误报合法写法"


# ── 数据文件本身的契约（运营改的就是这个文件）──────────────────────

def _seed_rows() -> List[dict]:
    rows = load_seed()
    assert rows, "data/symbol_aliases.json 里 aliases 为空"
    return rows


def test_seed_rows_are_well_formed():
    for r in _seed_rows():
        assert set(r) >= {"alias", "code", "kind"}, f"缺字段: {r}"
        assert str(r["alias"]).strip(), f"alias 为空: {r}"
        assert str(r["kind"]).strip().lower() in ALLOWED_KINDS, f"kind 非法: {r}"
        assert _CODE_RE.match(str(r["code"]).strip()), f"code 不像代码: {r}"


def test_seed_has_no_duplicate_rows():
    seen = set()
    for r in _seed_rows():
        key = (str(r["alias"]), str(r["code"]))
        assert key not in seen, f"重复行: {key}"
        seen.add(key)


def test_seed_alias_unambiguous_per_kind():
    """同一 (alias, kind) 只能有一个代码——否则 lookup_one 取哪条就是撞运气。

    一组代码的统称（kind=synonym）例外：同 alias 多行是它的正常形态。
    """
    mapping: Dict[Tuple[str, str], Set[str]] = {}
    for r in _seed_rows():
        kind = str(r["kind"]).strip().lower()
        if kind == "synonym":
            continue
        mapping.setdefault((str(r["alias"]), kind), set()).add(str(r["code"]))
    ambiguous = {k: v for k, v in mapping.items() if len(v) > 1}
    assert not ambiguous, f"同一别名指向多个代码，需人工定夺: {ambiguous}"


def test_seed_etf_codes_are_tradable_etf():
    """kind=etf 的代码必须能被 etf_symbol_key 认出来（5/1 开头的 6 位）。"""
    from signal_eval.symbols import etf_symbol_key

    for r in _seed_rows():
        if str(r["kind"]).strip().lower() != "etf":
            continue
        assert etf_symbol_key(str(r["code"])), f"ETF 代码不合法: {r}"


def test_seed_keeps_migrated_aliases():
    """迁移回归：两张旧表（索引别名 / ETF 简称 / 个股名）的关键条目不能丢。"""
    rows = [(str(r["alias"]), str(r["code"]), str(r["kind"]).lower())
            for r in _seed_rows()]
    must_have = [
        ("沪深300", "000300", "index"),
        ("沪深三百", "000300", "index"),
        ("hs300", "000300", "index"),
        ("中证500", "000905", "index"),
        ("zz500", "000905", "index"),
        ("沪深300", "510300", "etf"),
        ("红利ETF", "510880", "etf"),
        ("科创50", "588000", "etf"),        # 冲突已定：统一取规模最大的华夏 588000
        ("科创50ETF", "588000", "etf"),
        ("通信ETF", "515880", "etf"),
        ("贵州茅台", "600519", "stock"),
        ("易中天", "300502", "synonym"),
    ]
    for item in must_have:
        assert item in rows, f"迁移丢了别名: {item}"
    # 冲突项在数据文件里写明了（运营看得见）
    text = SEED_PATH.read_text(encoding="utf-8")
    assert "_conflicts" in text and "588000" in text and "588080" in text
