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


_db_migration_done = False


def get_db_dir() -> str:
    """获取数据库统一目录（全部 *.db 集中存放于此）"""
    _ensure_db_migration()
    d = os.path.join(get_app_dir(), 'db')
    os.makedirs(d, exist_ok=True)
    return d


def _ensure_db_migration():
    """启动兜底：把 app 根目录残留的旧位置 *.db 迁入 db/（含 -wal/-shm 附属）。

    仅执行一次；目标已存在则跳过，防止覆盖更新的文件。Windows 下若文件被占用
    （os.replace 抛 OSError）则静默失败，下次启动再试。
    """
    global _db_migration_done
    if _db_migration_done:
        return
    _db_migration_done = True
    app_dir = get_app_dir()
    db_dir = os.path.join(app_dir, 'db')
    if not os.path.isdir(app_dir):
        return
    try:
        os.makedirs(db_dir, exist_ok=True)
        for fname in os.listdir(app_dir):
            if not (fname.endswith('.db') or fname.endswith('.db-wal') or fname.endswith('.db-shm')):
                continue
            if fname == 'data_monitor.db':
                continue
            src = os.path.join(app_dir, fname)
            dst = os.path.join(db_dir, fname)
            if os.path.exists(dst):
                continue
            try:
                os.replace(src, dst)
            except OSError:
                pass
    except OSError:
        pass


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
    return os.path.join(get_db_dir(), 'stock_radar.db')


def get_baostock_cache_path() -> str:
    """获取 baostock 缓存数据库路径"""
    return os.path.join(get_db_dir(), 'baostock_cache.db')


def get_checkpoint_db_path() -> str:
    """获取 checkpoint 数据库路径"""
    return os.path.join(get_db_dir(), 'checkpoint.db')


def get_akshare_cache_path() -> str:
    """获取 akshare 缓存数据库路径"""
    return os.path.join(get_db_dir(), 'akshare_cache.db')


def get_logs_dir() -> str:
    """获取日志目录"""
    return os.path.join(get_app_dir(), 'logs')

def get_agents_dir() -> str:
    """获取 Agent 相关目录"""
    return os.path.join(get_resource_root(), 'agents')


def get_trace_db_path() -> str:
    """获取调用链追踪主库路径（仅 reviews；runs/steps/messages 按月拆到 get_trace_dir()）。"""
    return os.path.join(get_db_dir(), 'agent_trace.db')


def get_trace_dir() -> str:
    """获取调用链追踪月库目录（db/agent_trace/agent_trace_YYYYMM.db）。"""
    d = os.path.join(get_db_dir(), 'agent_trace')
    os.makedirs(d, exist_ok=True)
    return d


def get_eval_db_path() -> str:
    """获取评测数据库路径"""
    return os.path.join(get_db_dir(), 'eval.db')


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
    return os.path.join(get_db_dir(), 'market_data.db')


def get_fundamental_db_path() -> str:
    """获取基本面数据库路径"""
    return os.path.join(get_db_dir(), 'fundamental_cache.db')


def get_market_cache_db_path() -> str:
    """获取市场缓存数据库路径"""
    return os.path.join(get_db_dir(), 'market_cache.db')


def get_backtest_db_path() -> str:
    """获取回测数据库路径"""
    return os.path.join(get_db_dir(), 'backtest.db')


def get_signal_eval_db_path() -> str:
    """获取 AI 信号评测数据库路径"""
    return os.path.join(get_db_dir(), 'signal_eval.db')


def get_monitor_alerts_db_path() -> str:
    """获取盯盘告警数据库路径（规则 + 告警历史，独立于 stock_radar.db 零锁冲突）"""
    return os.path.join(get_db_dir(), 'monitor_alerts.db')


def get_stock_memory_db_path() -> str:
    """获取记忆系统数据库路径（语义记忆 + 情景记忆 + 用户画像统计）"""
    return os.path.join(get_db_dir(), 'stock_memory.db')


def get_data_monitor_db_path() -> str:
    """获取数据监控快照库路径（快照 + 资产元数据，独立于业务库）"""
    return os.path.join(get_db_dir(), 'data_monitor.db')


def get_shortline_db_path() -> str:
    """获取短线情绪观测库路径（每日情绪温度快照，独立于业务库）"""
    return os.path.join(get_db_dir(), 'shortline.db')


def get_thesis_db_path() -> str:
    """获取研究台账库路径（台账定义留痕 + 读数快照 + 变更告警，独立于业务库）"""
    return os.path.join(get_db_dir(), 'thesis.db')


def get_crowding_db_path() -> str:
    """获取拥挤度扫描库路径（扫描批次 + 标的明细 + 采集游标，独立于业务库）"""
    return os.path.join(get_db_dir(), 'crowding.db')


def get_crowding_pool_path() -> str:
    """获取拥挤度标的池配置路径（可手改、进版本管理；缺失时用内置默认池）"""
    return os.path.join(get_app_dir(), 'data', 'crowding', 'pool.json')


def get_data_monitor_config_path() -> str:
    """获取数据监控配置文件路径（扫描路径/保留期/清理阈值）"""
    return os.path.join(get_app_dir(), 'data_monitor_config.json')


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
