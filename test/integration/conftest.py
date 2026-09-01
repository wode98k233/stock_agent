"""
集成测试级 pytest fixtures
处理 API key 检查和网络依赖
"""
import sys
import os
import pytest
from dotenv import load_dotenv

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


# ── API Key 检查 ──────────────────────────────────────────

def _has_key(env_var: str) -> bool:
    return bool(os.getenv(env_var))


@pytest.fixture
def require_mx_key():
    """跳过需要 MX_APIKEY 的测试"""
    if not _has_key("MX_APIKEY"):
        pytest.skip("需要 MX_APIKEY")


@pytest.fixture
def require_tavily_key():
    """跳过需要 TAVILY_API_KEYS 的测试"""
    if not _has_key("TAVILY_API_KEYS"):
        pytest.skip("需要 TAVILY_API_KEYS")


@pytest.fixture
def require_openai_key():
    """跳过需要 OPENAI_API_KEY 的测试"""
    if not _has_key("OPENAI_API_KEY"):
        pytest.skip("需要 OPENAI_API_KEY")


@pytest.fixture
def require_hithink_key():
    """跳过需要 HITHINK_FINANCE_API_KEY 的测试"""
    if not _has_key("HITHINK_FINANCE_API_KEY"):
        pytest.skip("需要 HITHINK_FINANCE_API_KEY")


# ── 数据源重置 ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def ensure_fetcher_initialized():
    """确保数据源已注册，每个测试后清理缓存"""
    from tools.fetcher import _ensure_initialized
    _ensure_initialized()
    from tools.fetcher.data_cache import clear_all_cache
    yield
    try:
        clear_all_cache()
    except Exception:
        pass


# ── 通用 fixture ──────────────────────────────────────────

@pytest.fixture
def test_stock():
    """测试用股票代码（贵州茅台）"""
    return "600519"


@pytest.fixture
def test_stock_name():
    return "贵州茅台"
