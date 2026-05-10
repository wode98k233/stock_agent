"""
选股雷达 - 技能构建器

核心设计：
- @skill_tool 装饰器：统一处理工具包装、异常处理、JSON 序列化
- SkillBuilder 基类：统一管理依赖注入和工具构建
- 兼容现有接口：支持渐进式迁移
"""
from functools import wraps
from typing import Callable, Any, Dict, Type, Optional
from langchain_core.tools import tool
import json
import traceback
import logging
import inspect

logger = logging.getLogger("radar.skill_builder")


# 用于标记工具函数的装饰器
_skill_tool_mark = "__is_skill_tool__"


def skill_tool(func: Callable) -> Callable:
    """
    技能工具装饰器，统一处理异常和序列化
    
    功能：
    1. 标记函数为技能工具
    2. 自动捕获异常并返回标准错误格式
    3. 自动序列化返回结果为 JSON
    4. 自动记录工具调用日志
    
    示例：
        @skill_tool
        def get_stock_realtime(self, symbol: str) -> dict:
            \"\"\"获取个股实时行情\"\"\"
            return _get_stock_realtime(symbol, self.logger)
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs) -> str:
        try:
            result = func(self, *args, **kwargs)
            
            # 记录成功日志
            if hasattr(self, 'logger') and self.logger is not None:
                self.logger.debug(f"工具 {func.__name__} 执行成功")
            
            # 序列化结果 - 如果已经是字符串则直接返回
            if isinstance(result, str):
                return result
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False, default=str)
            return str(result) if result is not None else ""
            
        except Exception as e:
            # 记录错误日志
            error_msg = f"工具 {func.__name__} 执行失败: {str(e)}"
            if hasattr(self, 'logger') and self.logger is not None:
                self.logger.error(error_msg, exc_info=True)
            
            # 返回标准错误格式（兼容现有错误格式）
            return f"错误: {str(e)}"
    
    # 保留原始函数的文档字符串
    wrapper.__doc__ = func.__doc__
    # 标记这是一个技能工具
    setattr(wrapper, _skill_tool_mark, True)
    return wrapper


class SkillBuilder:
    """
    技能构建器基类
    
    功能：
    1. 统一管理 logger 和 memory_mgr 依赖
    2. 提供工具注册和构建功能
    3. 自动生成 LangChain 工具列表
    4. 兼容旧的 TOOL_REGISTRY 接口
    
    示例：
        class MySkill(SkillBuilder):
            def __init__(self, logger, memory_mgr):
                super().__init__(logger, memory_mgr)
                self._init_core()
            
            def _init_core(self):
                from tools.stock_data import get_stock_realtime
                self._get_stock_realtime = get_stock_realtime
            
            @skill_tool
            def get_stock_realtime(self, symbol: str) -> dict:
                \"\"\"获取个股实时行情\"\"\"
                return self._get_stock_realtime(symbol, self.logger)
        
        def build_tools(logger, memory_mgr):
            skill = MySkill(logger, memory_mgr)
            return skill.build_langchain_tools()
    """
    
    def __init__(self, logger, memory_mgr=None):
        self.logger = logger
        self.memory_mgr = memory_mgr
        self._tools_cache = None
        self._tool_registry_cache = None
    
    def build_langchain_tools(self) -> list:
        """
        构建所有标记了 @skill_tool 装饰器的方法为 LangChain 工具
        
        返回：
            LangChain 工具列表
        """
        if self._tools_cache is not None:
            return self._tools_cache
        
        tools = []
        
        # 遍历实例的所有方法，找到被 @skill_tool 装饰的方法
        for attr_name in dir(self):
            if attr_name.startswith('_'):
                continue
                
            attr = getattr(self, attr_name)
            if not callable(attr):
                continue
                
            # 检查是否被 @skill_tool 装饰
            if hasattr(attr, _skill_tool_mark):
                # 创建 LangChain 工具
                langchain_tool = tool(attr)
                tools.append(langchain_tool)
        
        self._tools_cache = tools
        if self.logger is not None:
            self.logger.info(f"构建了 {len(tools)} 个工具: {[t.name for t in tools]}")
        return tools
    
    def get_tool_registry(self) -> Dict[str, tuple]:
        """
        获取工具注册表（兼容旧接口）
        
        返回：
            {工具名: (工具函数, 参数模型类)} 字典
        """
        if self._tool_registry_cache is not None:
            return self._tool_registry_cache
        
        registry = {}
        
        for attr_name in dir(self):
            if attr_name.startswith('_'):
                continue
                
            attr = getattr(self, attr_name)
            if not callable(attr) or not hasattr(attr, _skill_tool_mark):
                continue
            
            # 从函数签名中提取参数信息
            sig = inspect.signature(attr.__wrapped__ if hasattr(attr, '__wrapped__') else attr)
            params = {}
            
            for param_name, param in sig.parameters.items():
                if param_name == 'self':
                    continue
                
                param_type = param.annotation if param.annotation != inspect.Parameter.empty else str
                has_default = param.default != inspect.Parameter.empty
                params[param_name] = (param_type, has_default, param.default)
            
            # 创建动态参数模型（如果需要的话）
            # 实际上我们不需要动态创建模型，只需要返回原始函数
            registry[attr_name] = (attr, None)
        
        self._tool_registry_cache = registry
        return registry
