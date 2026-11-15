"""full 档的图级行为（PRD-A9 §2）—— 新默认档必须有护栏。

`test/unit/skills/test_tool_surface_full.py` 锁的是纯函数层
（`activate_skills(mutate_tools=False)` 指纹不变等）；本文件锁的是**装配层**：
DI 容器一次性装载、`SelectSkillsNode`/router/gap 不再换绑、
model 态下 router 不重复投递。

对位文件 `test_react_skill_disclosure.py` 等 pin 在 strict 档，锁回退档行为。
"""
import pytest

pytestmark = pytest.mark.usefixtures("full_surface")


def _log():
    """ReactDIContainer / 各节点在 logger=None 时 `ensure_radar(None)` 返回 None，
    容器内 `self._logger.info()` 会炸。生产路径总是传真 logger，测试同理。"""
    from utils.logger import get_logger
    return get_logger("full-surface-test")[0]


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = f"{name} 的描述"


class _FakeRegistry:
    """最小 SkillRegistry 替身。"""

    def __init__(self, n_tools=6, guides=None):
        self._tools = [_FakeTool(f"tool_{i}") for i in range(n_tools)]
        self._guides = guides if guides is not None else {"a": "## a 的用法\n口径说明。"}

    def get_all_tools(self):
        return list(self._tools)

    def get_tools(self, name, selected_skills=None):
        return [_FakeTool(f"{name}_tool")]

    def get_skill_usage_guide(self, name):
        return self._guides.get(name, "")

    def get_skill_catalog(self):
        return [{"skill_name": s} for s in self._guides]

    def get_skill_tools(self, name):
        # build_tools_detail_prompt 会读 description / param_schema，缺键会 KeyError
        return [{"tool_name": f"{name}_tool", "description": f"{name} 工具说明",
                 "param_schema": {"properties": {}}}]

    def get_skill_meta(self, name):
        return object()


class _NoMemory:
    """PrepareNode 会读 memory.get_history()；关闭即可返回空历史。"""

    enabled = False

    def get_history(self):
        return []


def _container(registry=None):
    from agents.react.di_container import ReactDIContainer
    return ReactDIContainer(llm=None, memory=None,
                            skill_registry=registry or _FakeRegistry(),
                            budget=None, exec_state=None, logger=_log())


# ── DI 容器：一次性全量装载 ────────────────────────────────

def test_container_preloads_all_tools_once():
    c = _container(_FakeRegistry(n_tools=6))
    assert c.full_surface is True
    # 6 个真实工具 + activate_skill（默认 model 投递方式）
    assert len(c.tools_ref) == 7
    assert [t.name for t in c.tools_ref][:2] == ["tool_0", "tool_1"]


def test_container_appends_activate_skill_in_model_mode():
    from agents.react.activate_skill_tool import TOOL_NAME
    c = _container(_FakeRegistry(n_tools=3))
    assert [t.name for t in c.tools_ref][-1] == TOOL_NAME


def test_container_skips_activate_skill_in_router_mode(full_surface_router):
    from agents.react.activate_skill_tool import TOOL_NAME
    c = _container(_FakeRegistry(n_tools=3))
    names = [t.name for t in c.tools_ref]
    assert TOOL_NAME not in names, "router 态不该挂 activate_skill（模型不自行激活）"
    assert len(names) == 3


def test_container_falls_back_when_registry_raises():
    """装载失败必须能回落 strict，而不是整个 ReAct 起不来。"""

    class _Boom(_FakeRegistry):
        def get_all_tools(self):
            raise RuntimeError("boom")

    c = _container(_Boom())
    assert c.full_surface is False
    assert c.tools_ref == []


def test_container_falls_back_when_no_tools():
    c = _container(_FakeRegistry(n_tools=0))
    assert c.full_surface is False


def test_container_not_preloaded_in_strict_mode(strict_surface):
    c = _container(_FakeRegistry(n_tools=6))
    assert c.full_surface is False
    assert c.tools_ref == []


# ── 节点透传 ──────────────────────────────────────────────

def test_nodes_receive_full_surface_flags():
    c = _container(_FakeRegistry(n_tools=2))
    assert c.create_sub_goal_router_node().full_surface is True
    assert c.create_reselect_node().full_surface is True
    assert c.create_skill_gap_node().full_surface is True
    assert c.create_sub_goal_router_node().activate_mode == "model"


def test_router_node_receives_router_mode(full_surface_router):
    c = _container(_FakeRegistry(n_tools=2))
    assert c.create_sub_goal_router_node().activate_mode == "router"


# ── PrepareNode：full 档不再重复拼「## 可用工具」───────────────────

@pytest.mark.asyncio
async def test_prepare_omits_tool_list_in_full_mode(full_surface):
    """full 档下该段与 tools 数组一字不差，重复 4,435 字符 ≈ 2,957 token/调用。"""
    from agents.react.nodes import PrepareNode
    from utils.logger import get_logger

    reg = _FakeRegistry(n_tools=3)
    node = PrepareNode(memory=_NoMemory(), skill_registry=None, tools=list(reg.get_all_tools()),
                       logger=get_logger("prepare-test")[0])
    out = await node({"user_input": "查询贵州茅台", "loaded_skills": []})

    sysmsg = out["messages"][0][1] if isinstance(out["messages"][0], tuple) else out["messages"][0].content
    assert "## 可用工具" not in sysmsg, "full 档仍在重复输出工具清单"
    assert "tool_0" not in sysmsg, "工具名不应再以文本形式出现"


@pytest.mark.asyncio
async def test_prepare_keeps_tool_list_in_strict_mode(strict_surface):
    """strict 档 tools 不在数组里（只按批绑定），文本清单是模型唯一的工具来源，必须保留。"""
    from agents.react.nodes import PrepareNode
    from utils.logger import get_logger

    tools = [_FakeTool("mx_data_query"), _FakeTool("mx_search_news")]
    node = PrepareNode(memory=_NoMemory(), skill_registry=None, tools=tools,
                       logger=get_logger("prepare-test")[0])
    out = await node({"user_input": "查询贵州茅台", "loaded_skills": []})

    sysmsg = out["messages"][0][1] if isinstance(out["messages"][0], tuple) else out["messages"][0].content
    assert "## 可用工具" in sysmsg
    assert "mx_data_query" in sysmsg


@pytest.mark.asyncio
async def test_prepare_still_injects_loaded_skill_detail(full_surface):
    """去掉工具清单后，技能语义（真正不重复的那部分）必须仍然注入。"""
    from agents.react.nodes import PrepareNode
    from tools.skills import SkillPromptBuilder
    from utils.logger import get_logger

    reg = _FakeRegistry(n_tools=3)
    node = PrepareNode(memory=_NoMemory(), skill_registry=reg, tools=list(reg.get_all_tools()),
                       logger=get_logger("prepare-test")[0])
    out = await node({"user_input": "查询贵州茅台", "loaded_skills": ["a"]})

    sysmsg = out["messages"][0][1] if isinstance(out["messages"][0], tuple) else out["messages"][0].content
    assert "## 可用工具" not in sysmsg
    # PRD-A11 FR-11：技能详情移到 messages **尾部**（不再拼进 system 中段）——
    # 中段注入会让 [REACT system] 之后立刻与 select_skills / skill-router / planner
    # 的前缀分叉，实测 agent 首调只命中 4,096/11,806。
    tail = out["messages"][-1][1] if isinstance(out["messages"][-1], tuple) else out["messages"][-1].content
    assert "口径说明" in tail, "初始批的业务口径必须在（只是位置改到尾部）"
    assert "口径说明" not in sysmsg, "技能详情不应再出现在 system 中段"


# ── router：model 态不投递，router 态投递 ─────────────────

def _router(registry, activate_mode, tools_ref=None):
    from agents.react.skill_nodes import SubGoalRouterNode
    return SubGoalRouterNode(llm=None, logger=_log(), skill_registry=registry,
                             tools_ref=tools_ref if tools_ref is not None else [],
                             full_surface=True, activate_mode=activate_mode)


@pytest.mark.asyncio
async def test_router_model_mode_does_not_deliver_guide():
    """model 态由模型自己调 activate_skill，router 再投一次是重复且抢决策权。"""
    node = _router(_FakeRegistry(), "model")
    out = await node({"candidate_skills": ["a"], "loaded_skills": []})
    assert "messages" not in out, "model 态 router 不应投递 usage_guide"
    assert out["loaded_skills"] == ["a"]


@pytest.mark.asyncio
async def test_router_router_mode_delivers_guide():
    node = _router(_FakeRegistry(), "router")
    out = await node({"candidate_skills": ["a"], "loaded_skills": []})
    assert "messages" in out
    assert "口径说明" in out["messages"][0][1]


@pytest.mark.asyncio
async def test_router_does_not_rebind_tools_in_full_mode():
    tools_ref = [_FakeTool(f"tool_{i}") for i in range(6)]
    node = _router(_FakeRegistry(), "model", tools_ref=tools_ref)
    await node({"candidate_skills": ["a"], "loaded_skills": []})
    assert len(tools_ref) == 6, "full 档下 router 改写了 tools_ref"
    assert [t.name for t in tools_ref][:3] == ["tool_0", "tool_1", "tool_2"]


@pytest.mark.asyncio
async def test_skill_gap_does_not_rebind_tools_in_full_mode():
    from agents.react.skill_nodes import SkillGapNode

    tools_ref = [_FakeTool(f"tool_{i}") for i in range(6)]
    node = SkillGapNode(llm=None, logger=_log(), skill_registry=_FakeRegistry(),
                        tools_ref=tools_ref, full_surface=True, activate_mode="model")
    await node({"candidate_skills": ["a"], "active_skill": "z", "skill_gap_count": 0})
    assert len(tools_ref) == 6