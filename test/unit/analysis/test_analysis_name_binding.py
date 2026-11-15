"""静态检查：函数体内使用了未绑定的自由变量（NameError 类回归）

**事故背景（2026-10-13）**
PRD-A9 §3.1 给 ``_select_tool_indices`` 加了 ``template=`` 实参传递，但漏改函数签名，
函数体里的裸 ``template`` 从未绑定。运行时 ``NameError: name 'template' is not defined``
冒泡到 ``build()`` 的兜底 except，整篇 LLM 报告静默退化为规则化模板 —— 线上只留一行
``LLM 报告合成失败，回退到规则化报告`` WARNING。

**为什么单测没抓到**：该分支只在「证据量足够大、值得下钻」时才走到
（``REPORT_DRILLDOWN_MIN_TOOLS=8`` / ``MIN_CHARS=12000``），小样本用例全在门槛前就 return 了。

本测试用 AST 做全目录扫描，把这类错误挡在提交前。
"""
import ast
import builtins
from pathlib import Path

import pytest

TARGET_DIR = Path(__file__).resolve().parents[3] / "agents" / "analysis"

# 本目录跨函数传递最频繁的自由变量，历史上出过未绑定问题
WATCHED = {
    "template", "budget", "log", "logger", "metadata", "run_config",
    "slot_results", "user_input", "raw_result", "tool_evidence", "items",
    "content", "sections", "step_results", "config", "llm", "tool_calls",
}


def _bound_names(fn: ast.AST) -> set:
    """函数作用域内被绑定的名字：参数 + 赋值 + import + 嵌套定义 + except as + global。"""
    a = fn.args
    bound = {x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)}
    if a.vararg:
        bound.add(a.vararg.arg)
    if a.kwarg:
        bound.add(a.kwarg.arg)

    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                bound.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if getattr(node, "name", None):
                bound.add(node.name)
    return bound


def _module_level_names(tree: ast.Module) -> set:
    """模块级可见的名字：函数/类定义 + 模块级赋值（含 logger 这类全局单例）。"""
    names = {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    names.update(e.id for e in t.elts if isinstance(e, ast.Name))
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for al in stmt.names:
                names.add((al.asname or al.name).split(".")[0])
        elif isinstance(stmt, (ast.If, ast.Try)):
            # 条件导入 / try-import 的全局（logger = get_logger(...) 常裹在 try 里）
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    names.add(node.id)
    return names


def _unbound_watched_names(fn: ast.AST, module_names: set) -> set:
    bound = _bound_names(fn)
    used = {n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return (used & WATCHED) - bound - module_names - set(dir(builtins))


def _py_files():
    files = sorted(TARGET_DIR.glob("*.py"))
    if not files:
        pytest.skip(f"目录不存在：{TARGET_DIR}")
    return files


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_no_unbound_free_variables(path: Path):
    """每个函数体内被使用的 WATCHED 名字，必须在参数/局部/模块级有绑定。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_names = _module_level_names(tree)

    problems = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        missing = _unbound_watched_names(fn, module_names)
        if missing:
            problems.append(f"{path.name}:{fn.lineno} {fn.name}() -> {sorted(missing)}")

    assert not problems, (
        "函数体使用了未绑定的自由变量，运行时会 NameError：\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_module_parses(path: Path):
    """兜底：所有分析模块必须能被解析（签名与实参错位之外的语法级回归）。"""
    ast.parse(path.read_text(encoding="utf-8"))