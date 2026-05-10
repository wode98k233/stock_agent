"""
集成测试级 pytest fixtures
处理 API key 检查和网络依赖
"""
import sys
import os
import pytest

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _has_api_key():
    """检查是否有 LLM API key"""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    return bool(os.getenv("OPENAI_API_KEY"))


def _has_mx_api_key():
    """检查是否有 MX Data API key"""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    return bool(os.getenv("MX_APIKEY"))


@pytest.fixture
def skip_if_no_api_key():
    """如果没有 API key 则跳过测试"""
    if not _has_api_key():
        pytest.skip("需要 OPENAI_API_KEY")


@pytest.fixture
def skip_if_no_mx_api_key():
    """如果没有 MX API key 则跳过测试"""
    if not _has_mx_api_key():
        pytest.skip("需要 MX_APIKEY")


@pytest.fixture
def agent_context(skip_if_no_api_key):
    """
    构建 Agent 测试上下文
    自动跳过无 API key 的测试
    """
    from tools.skill_register import SkillRegister
    from tools.skills import SkillRegistry
    from utils.logger import get_logger
    from utils.memory import MemoryManager

    skill_register = SkillRegister()
    skill_register.auto_discover()
    logger, uuid, ctx = get_logger("integration-test")
    memory = MemoryManager(logger)
    registry = SkillRegistry(logger, memory, skill_register)

    return {
        "skill_register": skill_register,
        "logger": logger,
        "memory": memory,
        "registry": registry,
    }
