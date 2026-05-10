"""
CLI 模块
统一导出入口
"""
from cli.bootstrap import bootstrap
from cli.router import router

__all__ = ["bootstrap", "router"]
