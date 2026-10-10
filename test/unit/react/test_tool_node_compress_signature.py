"""回归：所有 ToolNode 子类的 compress_tool_output 签名必须与基类一致。

2026-09-21 事故：`agents/react/tool_node_base.py::BaseToolNode._run_one` 以
`self.compress_tool_output(tool_name, content, user_goal=...)` 调用，但
`agents/common_react/nodes.py::SubToolNode` 的覆写只收 `(tool_name, content)` →
Unified Plan 子 ReAct 的**每次工具调用**都抛 TypeError，输出被替换成
「执行失败: ...unexpected keyword argument 'user_goal'」，
下游报告据此写下 13 处「数据缺失 / 未返回」（成分股权重、行情、均线、资金流全被吞掉）。
"""
import ast
import pathlib
from unittest.mock import patch

import pytest

from agents.common_react.nodes import SubToolNode, build_sub_tool_node
from agents.react.tool_node_base import BaseToolNode

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class TestCompressSignatureContract:
    def test_base_accepts_user_goal(self):
        import inspect

        sig = inspect.signature(BaseToolNode.compress_tool_output)
        assert "user_goal" in sig.parameters, "基类必须收 user_goal（_run_one 会传）"

    def test_subtoolnode_accepts_user_goal(self):
        import inspect

        sig = inspect.signature(SubToolNode.compress_tool_output)
        assert "user_goal" in sig.parameters, (
            "SubToolNode 覆写必须收 user_goal，否则子 ReAct 工具输出全变 TypeError"
        )

    def test_all_overrides_in_repo_accept_user_goal(self):
        """AST 扫描全仓：任何 compress_tool_output 定义都必须收 user_goal。

        只遍历源码目录（不 glob 整个仓库）——直接在仓库根 `glob("**/*.py")` 会走
        `.venv`（数万文件），单测因此白花 3s+。
        """
        bad = []
        for root_name in ("agents", "tools", "utils", "server", "memory", "eval", "cli", "backtest"):
            root = REPO_ROOT / root_name
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "compress_tool_output":
                        args = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
                        if "user_goal" not in args:
                            bad.append("%s:%d" % (path.relative_to(REPO_ROOT).as_posix(), node.lineno))
        assert not bad, "这些 compress_tool_output 定义缺少 user_goal 参数: %s" % bad


class TestSubToolNodeCompressBehavior:
    def test_accepts_user_goal_kwarg_and_forwards_it(self):
        node = build_sub_tool_node(user_goal_ref={"goal": "子目标A"})
        captured = {}

        async def fake(tool_name, content, logger, user_goal=""):
            captured["tool_name"] = tool_name
            captured["user_goal"] = user_goal
            return "compressed"

        with patch("agents.common_react.nodes._compress_tool_output", side_effect=fake):
            import asyncio

            out = asyncio.run(node.compress_tool_output("mx_data_query", "大表格", user_goal="显式目标"))
        assert out == "compressed"
        assert captured["user_goal"] == "显式目标", "显式传入的 user_goal 应优先"

    def test_falls_back_to_user_goal_ref_when_not_passed(self):
        node = build_sub_tool_node(user_goal_ref={"goal": "子目标B"})
        captured = {}

        async def fake(tool_name, content, logger, user_goal=""):
            captured["user_goal"] = user_goal
            return "ok"

        with patch("agents.common_react.nodes._compress_tool_output", side_effect=fake):
            import asyncio

            asyncio.run(node.compress_tool_output("mx_data_query", "内容"))
        assert captured["user_goal"] == "子目标B", "未传时回退到 _user_goal_ref"

    def test_run_one_call_shape_does_not_raise(self):
        """复刻 _run_one 的调用形态（位置参数 + user_goal 关键字）。"""
        node = build_sub_tool_node(user_goal_ref={"goal": ""})

        async def fake(tool_name, content, logger, user_goal=""):
            return "ok"

        with patch("agents.common_react.nodes._compress_tool_output", side_effect=fake):
            import asyncio

            # 修复前这里会 TypeError: unexpected keyword argument 'user_goal'
            assert asyncio.run(node.compress_tool_output("mx_xuangu_filter", "{}", user_goal="g")) == "ok"
