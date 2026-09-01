"""
🏭 Agent 工厂 — 懒实例化
启动时只注册工厂函数，第一次 get() 时才 import + 实例化，
避免启动时加载 langgraph（~15s）。
"""
from typing import Callable
import threading
from agents.base import BaseAgent


class AgentFactory:
    """管理所有已注册的 Agent，支持懒实例化。"""

    _factories: dict = {}        # name → Callable[[], BaseAgent]
    _instances: dict = {}        # name → BaseAgent（懒加载缓存）
    _current = "react_stock"

    # 并发安全：首次实例化会 import 整条依赖树（~55s），必须用锁 + 在途事件保护，
    # 否则「后台预热线程」与「首请求线程」并发首调用会重复 import 同一模块，
    # 拿到半初始化的模块状态（竞态）。详见 server/deps.py::require_agent_ready 的 await 改造。
    _init_lock = threading.Lock()
    _ready: dict = {}            # name → threading.Event（标记该 agent 是否已完成首次实例化）

    @classmethod
    def register(cls, agent: BaseAgent | Callable[[], BaseAgent], name: str = None):
        """注册 Agent 实例或工厂函数。传实例=立即注册，传 callable=懒加载。"""
        if isinstance(agent, BaseAgent):
            cls._instances[agent.name] = agent
            cls._factories[agent.name] = lambda a=agent: a
        else:
            key = name
            if key is None:
                raise ValueError("注册懒加载 Agent 必须提供 name")
            cls._factories[key] = agent
            cls._instances.pop(key, None)  # 替换旧实例

    @classmethod
    def get(cls, name=None) -> BaseAgent:
        """获取 Agent 实例，首次访问时触发懒加载（约 15s）。

        线程安全：并发的首调用只有一个线程真正执行 import，其余线程等待其完成后
        直接取缓存实例，绝不重复 import。已缓存的实例无锁直接返回，不阻塞。
        """
        name = name or cls._current
        if name not in cls._factories:
            available = ", ".join(cls._factories.keys()) or "(无已注册 Agent)"
            raise ValueError(f"未找到 Agent: '{name}'，可用: {available}")

        # 快路径：已缓存，无锁直接返回（不阻塞其他调用）
        inst = cls._instances.get(name)
        if inst is not None:
            return inst

        # 慢路径：可能需 import，需并发安全
        with cls._init_lock:
            evt = cls._ready.get(name)
            if evt is None:
                # 本线程负责首次实例化：先建事件再释放锁，import 在锁外执行，
                # 避免持锁 ~55s 阻塞其他（已缓存）get() 调用
                evt = cls._ready.setdefault(name, threading.Event())
                do_init = True
            else:
                # 已有其他线程在初始化，本线程只等待
                do_init = False

        if do_init:
            try:
                inst = cls._factories[name]()
            except Exception:
                evt.set()  # 即使失败也放行等待者，避免永久阻塞；异常向上抛出
                raise
            cls._instances[name] = inst
            evt.set()
            return inst
        else:
            evt.wait()  # 等待负责初始化的线程完成
            inst = cls._instances.get(name)
            if inst is None:
                # 初始化线程失败，且无缓存实例
                raise RuntimeError(f"Agent '{name}' 初始化失败，请查看启动日志")
            return inst

    @classmethod
    def list(cls) -> list:
        """列出所有已注册 Agent 名称（不触发实例化）。"""
        return list(cls._factories.keys())

    @classmethod
    def set_default(cls, name: str):
        """
        ⚙️ 设置默认 Agent
        📥 name: Agent 名称
        """
        if name not in cls._factories:
            available = ", ".join(cls._factories.keys()) or "(无已注册 Agent)"
            raise ValueError(f"未找到 Agent: '{name}'，可用: {available}")
        cls._current = name
