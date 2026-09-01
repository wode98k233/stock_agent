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


def get_resource_root() -> str:
    """获取打包资源根目录。

    开发模式：项目根目录
    打包模式：PyInstaller 的资源目录（_MEIPASS）
    """
    if getattr(sys, 'frozen', False):
        return getattr(sys, "_MEIPASS", get_app_dir())
    return get_app_dir()


def ensure_app_dirs():
    """确保所有必要的目录存在"""
    app_dir = get_app_dir()
    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(get_logs_dir(), exist_ok=True)
    os.makedirs(get_user_skills_dir(), exist_ok=True)


def get_db_path() -> str:
    """获取主数据库路径"""
    return os.path.join(get_app_dir(), 'stock_radar.db')


def get_baostock_cache_path() -> str:
    """获取 baostock 缓存数据库路径"""
    return os.path.join(get_app_dir(), 'baostock_cache.db')


def get_checkpoint_db_path() -> str:
    """获取 checkpoint 数据库路径"""
    return os.path.join(get_app_dir(), 'checkpoint.db')


def get_akshare_cache_path() -> str:
    """获取 akshare 缓存数据库路径"""
    return os.path.join(get_app_dir(), 'akshare_cache.db')


def get_logs_dir() -> str:
    """获取日志目录"""
    return os.path.join(get_app_dir(), 'logs')

def get_agents_dir() -> str:
    """获取 Agent 相关目录"""
    return os.path.join(get_resource_root(), 'agents')


def get_trace_db_path() -> str:
    """获取调用链追踪数据库路径"""
    return os.path.join(get_app_dir(), 'agent_trace.db')


def get_eval_db_path() -> str:
    """获取评测数据库路径"""
    return os.path.join(get_app_dir(), 'eval.db')


def get_skills_root() -> str:
    """获取项目根目录（用于加载 skills）"""
    return get_resource_root()


def get_internal_skills_dir() -> str:
    """获取内部 skills 目录"""
    return os.path.join(get_skills_root(), 'tools', 'skills')


def get_external_skills_dir() -> str:
    """获取外部 skills 目录"""
    return os.path.join(get_skills_root(), 'tools', 'other_skills')


def get_user_skills_dir() -> str:
    """获取用户可写技能目录（不在 _MEIPASS 内，打包态可写）。"""
    return os.path.join(get_app_dir(), 'user_skills')


def get_data_path() -> str:
    """获取数据文件目录"""
    return os.path.join(get_resource_root(), 'data')


def get_questions_pool_path() -> str:
    """获取推荐问题池路径"""
    return os.path.join(get_data_path(), 'suggested_questions.json')


def get_template_dir() -> str:
    """获取报告模板目录"""
    return os.path.join(get_resource_root(), 'agents', 'report_templates')


def get_server_static_dir() -> str:
    """获取 Web 静态资源目录"""
    return os.path.join(get_resource_root(), 'server', 'static')


def get_agent_trace_viewer_dir() -> str:
    """获取 agent_trace 追踪页面目录"""
    return os.path.join(get_resource_root(), 'utils', 'agent_trace')


def get_market_data_db_path() -> str:
    """获取行情数据库路径"""
    return os.path.join(get_app_dir(), 'market_data.db')


def get_fundamental_db_path() -> str:
    """获取基本面数据库路径"""
    return os.path.join(get_app_dir(), 'fundamental_cache.db')


def get_market_cache_db_path() -> str:
    """获取市场缓存数据库路径"""
    return os.path.join(get_app_dir(), 'market_cache.db')


def get_backtest_db_path() -> str:
    """获取回测数据库路径"""
    return os.path.join(get_app_dir(), 'backtest.db')


def get_signal_eval_db_path() -> str:
    """获取 AI 信号评测数据库路径"""
    return os.path.join(get_app_dir(), 'signal_eval.db')


def get_stock_memory_db_path() -> str:
    """获取记忆系统数据库路径（语义记忆 + 情景记忆 + 用户画像统计）"""
    return os.path.join(get_app_dir(), 'stock_memory.db')


def get_memory_config_path() -> str:
    """获取记忆系统用户配置路径（min_similarity 等运行时可调项，JSON）。

    与 stock_memory.db 同目录，便于一起备份/迁移。
    """
    return os.path.join(get_app_dir(), 'memory_config.json')


def get_jieba_cache_dir() -> str:
    """获取 jieba 分词缓存目录（项目内 cache/，避免写到系统 temp）。

    打包态 exe 同级目录可写且跨运行持久，开发态落在项目根 cache/。
    """
    d = os.path.join(get_app_dir(), 'cache')
    os.makedirs(d, exist_ok=True)
    return d


def get_chroma_data_path() -> str:
    """获取 ChromaDB 向量数据库持久化目录"""
    return os.path.join(get_app_dir(), 'chroma_data')


def get_user_profile_dir() -> str:
    """获取用户画像目录"""
    p = os.path.join(get_app_dir(), 'data', 'user_profile')
    os.makedirs(p, exist_ok=True)
    return p
