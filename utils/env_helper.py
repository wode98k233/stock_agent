"""
环境变量文件管理
检查并创建 .env 文件
"""
import os
from dotenv import load_dotenv

from utils.app_paths import get_app_dir


def get_env_example_path() -> str:
    """获取 .env.example 文件路径"""
    return os.path.join(get_app_dir(), '.env.example')


def ensure_env_file():
    """确保 .env 文件存在，不存在则从 .env.example 复制"""
    env_file = os.path.join(get_app_dir(), '.env')
    env_example = get_env_example_path()

    if not os.path.exists(env_file):
        if os.path.exists(env_example):
            with open(env_example, 'r', encoding='utf-8') as f:
                content = f.read()
            with open(env_file, 'w', encoding='utf-8') as f:
                f.write(content)
            print("\n!!! 已创建 .env 文件，请编辑填入配置 !!!\n")
        else:
            with open(env_file, 'w', encoding='utf-8') as f:
                f.write("""# 选股雷达配置
OPENAI_API_KEY=your_api_key_here
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_MODEL_NAME=gpt-4o

# 东方财富妙想Skills 配置
MX_APIKEY=your_mx_apikey_here
TUSHARE_TOKEN=your_tushare_token_here

# ========== Plan & Solve 模式 ==========
# 规划步数上限
PLAN_MAX_STEPS=5
# Executor单步重试次数
PLAN_EXECUTOR_MAX_RETRIES=3
# Executor内部ReAct工具调用上限（LangGraph recursion_limit，每个 LLM + 工具调用各算 1 步）
PLAN_EXECUTOR_TOOL_CALLS=15

# ========== ReAct 模式 ==========
# 工具调用上限（LangGraph recursion_limit，每个 LLM + 工具调用各算 1 步）
REACT_TOOL_CALLS=25

# ========== 通用 Agent 配置 ==========
# 批量分析股票时，每次处理的数量
BATCH_SIZE=5
# 记忆压缩的最大Token数
MEMORY_MAX_TOKENS=8000

# ========== 缓存配置 ==========
# 缓存过期时间
CACHE_EXPIRE_HOURS=24

# ========== 日志/调试 ==========
LOG_LEVEL=INFO
# 在 DEBUG 级别下，每个 step 执行前需要手动确认
DEBUG_STEP_CONFIRM=false
""")
            print("\n!!! 已创建 .env 文件，请编辑填入 API Key !!!\n")
        load_dotenv(env_file)
    else:
        load_dotenv(env_file)
