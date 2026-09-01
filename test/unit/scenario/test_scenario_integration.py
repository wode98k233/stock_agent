"""
选股雷达 - 场景Agent集成测试
验证 ScenarioAgent 能正确注册、分类、执行
"""
import sys, os, importlib.util

# 项目根目录: test/unit/scenario/ → test/unit/ → test/ → project_root
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _project_root)

# agents/__init__.py 顶层只有懒加载 __getattr__，import 本身不拉重依赖；
# 直接 import 拿到真实 agents 包（带 __getattr__），后续 `from agents import
# AgentFactory` 才能正常工作。切勿用 type(sys)("agents") 替换 —— 那会丢掉
# __getattr__，导致依赖 `from agents import ...` 的测试全部 ImportError。
import agents
_agents_pkg = sys.modules["agents"]
if not hasattr(_agents_pkg, "__path__") or not _agents_pkg.__path__:
    _agents_pkg.__path__ = [os.path.join(_project_root, "agents")]


def _load_module(name, rel_path):
    """辅助：用 importlib 直接加载模块。

    若模块已在 sys.modules 中（被前置测试真实 import 过），直接复用，
    避免用副本覆盖 —— 副本会让 sys.modules[name] 与 parent.attr 指向不同
    模块对象，导致 monkeypatch("agents.factory.X") 设在副本、而
    `from agents.factory import X` 取真实模块（或反之），mock 失效。
    """
    if name in sys.modules:
        return sys.modules[name]
    full_path = os.path.join(_project_root, rel_path)
    spec = importlib.util.spec_from_file_location(name, full_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    # 同步子模块到父包属性，保持 sys.modules[name] 与 parent.attr 一致
    parts = name.split(".")
    if len(parts) > 1:
        parent = sys.modules.get(parts[0])
        if parent is not None:
            setattr(parent, parts[-1], mod)
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


def test_enrich_user_input_adds_time_context():
    """_enrich_user_input 应注入时间上下文，不再硬编码工具偏好"""
    agent = AgentFactory.get("scenario")
    enriched = agent._enrich_user_input("分析茅台")
    # 应包含时间信息
    assert "时间" in enriched or "当前" in enriched or len(enriched) > len("分析茅台")
    # 不应包含已移除的硬编码工具偏好
    assert "优先使用 mx_" not in enriched


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
