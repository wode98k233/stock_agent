"""
选股雷达 - 场景Agent集成测试
验证 ScenarioAgent 能正确注册、分类、执行
"""
import sys, os, importlib.util

# 项目根目录: test/unit/ → test/ → project_root
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _project_root)

# 预注册 agents 包，避免触发 agents/__init__.py 的重依赖链
sys.modules.setdefault("agents", type(sys)("agents"))
sys.modules["agents"].__path__ = [os.path.join(_project_root, "agents")]


def _load_module(name, rel_path):
    """辅助：用 importlib 直接加载模块"""
    full_path = os.path.join(_project_root, rel_path)
    spec = importlib.util.spec_from_file_location(name, full_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# 按依赖顺序加载模块
_base_mod = _load_module("agents.base", os.path.join("agents", "base.py"))
_factory_mod = _load_module("agents.factory", os.path.join("agents", "factory.py"))
_router_mod = _load_module("agents.scenario_router", os.path.join("agents", "scenario_router.py"))
_agent_mod = _load_module("agents.scenario_agent", os.path.join("agents", "scenario_agent.py"))

BaseAgent = _base_mod.BaseAgent
AgentFactory = _factory_mod.AgentFactory
Scenario = _router_mod.Scenario
classify_scenario = _router_mod.classify_scenario
ScenarioAgent = _agent_mod.ScenarioAgent

# 手动注册 ScenarioAgent 到工厂
AgentFactory.register(ScenarioAgent())


def test_scenario_agent_registration():
    """验证 ScenarioAgent 已注册到 AgentFactory"""
    agents = AgentFactory.list()
    assert "scenario" in agents, f"scenario 未注册，当前 agents: {agents}"
    print("  [OK] ScenarioAgent 已注册到 AgentFactory")


def test_scenario_agent_attributes():
    """验证 ScenarioAgent 属性"""
    agent = AgentFactory.get("scenario")
    assert agent.name == "scenario"
    assert "快速路径" in agent.description
    print("  [OK] ScenarioAgent 属性正确")


def test_scenario_agent_has_handlers():
    """验证 ScenarioAgent 注册了所有场景处理器"""
    agent = AgentFactory.get("scenario")
    for scenario in Scenario:
        assert scenario in agent._handlers, f"缺少处理器: {scenario}"
    print("  [OK] 所有场景处理器已注册")


def test_scenario_agent_inherits_base():
    """验证 ScenarioAgent 继承 BaseAgent"""
    assert issubclass(ScenarioAgent, BaseAgent)
    print("  [OK] ScenarioAgent 继承 BaseAgent")


def test_scenario_agent_has_run_method():
    """验证 ScenarioAgent 有 run 方法"""
    agent = AgentFactory.get("scenario")
    assert hasattr(agent, 'run')
    assert callable(agent.run)
    print("  [OK] ScenarioAgent 有 run 方法")


def test_scenario_agent_has_enrich():
    """验证 ScenarioAgent 有 _enrich_user_input 方法"""
    agent = AgentFactory.get("scenario")
    assert hasattr(agent, '_enrich_user_input')
    print("  [OK] ScenarioAgent 有 _enrich_user_input 方法")


def test_scenario_router_classify():
    """验证场景路由器能正确分类"""
    # 数据查询
    s, _ = classify_scenario("茅台现在多少钱")
    assert s == Scenario.DATA_QUERY or s == Scenario.STOCK_ANALYSIS

    # 选股
    s, _ = classify_scenario("帮我找几只低PE的消费股")
    assert s == Scenario.SCREENING

    # 市场概览
    s, _ = classify_scenario("今天大盘怎么样")
    assert s == Scenario.MARKET_OVERVIEW

    print("  [OK] 场景路由器分类正确")


def test_scenario_handler_count():
    """验证处理器数量与场景枚举一致"""
    agent = AgentFactory.get("scenario")
    assert len(agent._handlers) == len(Scenario), \
        f"处理器数量 {len(agent._handlers)} != 场景数量 {len(Scenario)}"
    print(f"  [OK] 处理器数量一致: {len(Scenario)} 个")


if __name__ == "__main__":
    try:
        test_scenario_agent_registration()
        test_scenario_agent_attributes()
        test_scenario_agent_has_handlers()
        test_scenario_agent_inherits_base()
        test_scenario_agent_has_run_method()
        test_scenario_agent_has_enrich()
        test_scenario_router_classify()
        test_scenario_handler_count()
        print("=" * 60)
        print("[PASS] test_scenario_integration 全部通过")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
