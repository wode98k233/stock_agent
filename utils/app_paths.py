"""
选股雷达 - 应用路径管理
统一管理所有运行时路径，支持开发/打包/测试多种模式
onedir 模式：数据目录与 exe 同级
"""
import os
import sys


def get_app_dir() -> str:
    """
    获取应用根目录（exe 所在目录 for onedir）
    开发模式：项目根目录
    打包模式：exe 所在目录
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_app_dirs():
    """确保所有必要的目录存在"""
    app_dir = get_app_dir()
    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(get_logs_dir(), exist_ok=True)


def get_db_path() -> str:
    """获取主数据库路径"""
    return os.path.join(get_app_dir(), 'stock_radar.db')


def get_baostock_cache_path() -> str:
    """获取 baostock 缓存数据库路径"""
    return os.path.join(get_app_dir(), 'baostock_cache.db')


def get_akshare_cache_path() -> str:
    """获取 akshare 缓存数据库路径"""
    return os.path.join(get_app_dir(), 'akshare_cache.db')


def get_logs_dir() -> str:
    """获取日志目录"""
    return os.path.join(get_app_dir(), 'logs')


def get_trace_db_path() -> str:
    """获取调用链追踪数据库路径"""
    return os.path.join(get_app_dir(), 'agent_trace.db')


def get_skills_root() -> str:
    """获取项目根目录（用于加载 skills）"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    else:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_internal_skills_dir() -> str:
    """获取内部 skills 目录"""
    return os.path.join(get_skills_root(), 'tools', 'skills')


def get_external_skills_dir() -> str:
    """获取外部 skills 目录"""
    return os.path.join(get_skills_root(), 'tools', 'other_skills')


def get_data_path() -> str:
    """获取数据文件目录"""
    return os.path.join(get_app_dir(), 'data')


def get_questions_pool_path() -> str:
    """获取推荐问题池路径"""
    return os.path.join(get_app_dir(), 'data', 'suggested_questions.json')
