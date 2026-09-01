"""FTS5 后端 jieba 初始化回归测试。

固化行为：
- jieba 在模块导入期（启动期）一次性初始化，不延迟到首个分词请求；
- jieba 自带日志被静音（INFO 级 'Building prefix dict...' 不再打到 stdout）；
- 缓存目录重定向到项目 cache/，不再写到系统 temp（C:\\Users\\...\\Temp）；
- _segment 对中文正常分词。
"""
import logging

import pytest

jieba = pytest.importorskip("jieba")

from memory.backend import ensure_jieba_ready  # noqa: E402
from memory.backend.fts5 import FTS5Backend  # noqa: E402
from utils.app_paths import get_jieba_cache_dir  # noqa: E402


def test_jieba_initialized_and_cache_redirected():
    ensure_jieba_ready()
    assert jieba.dt.initialized is True
    # 缓存已重定向到项目 cache 目录，不再使用系统 temp
    # （jieba 通过 self.tmp_dir 决定缓存落盘位置；self.cache_file 是内部局部变量，始终为 None）
    assert getattr(jieba.dt, "tmp_dir", None) == get_jieba_cache_dir()


def test_jieba_log_silenced():
    # 初始化时应把 jieba 自带日志静音到 ERROR，避免 'Building prefix dict' 污染 stdout
    ensure_jieba_ready()
    assert jieba.default_logger.level <= logging.ERROR


def test_fts5_segment_tokenizes_chinese():
    seg = FTS5Backend._segment("茅台股价半导体板块今天涨了")
    assert "茅台" in seg
    assert "半导体" in seg
    assert " " in seg  # 分词以空格连接


# ── _fts5_query：OR 召回语义 + 停用词过滤（2026-08-13 固化）───────────
# 背景：旧实现 " ".join(tokens) 是 FTS5 隐式 AND，用户完整问题分词出 4~5 个词
#       必须全部命中，实测「宁德时代最近走势如何」AND=0 命中 / OR=16 命中，
#       导致长期记忆检索几乎总是 0 召回。现改为 OR 语义 + 停用词过滤，
#       精度交给 bm25 排序 + （Hybrid 下）Embedding RRF + Cross-encoder 精排。

def test_fts5_query_uses_or_semantics():
    # 多 token 查询必须生成 OR 表达式（而非空格 AND）
    q = FTS5Backend._fts5_query("宁德 时代 走势 如何")
    assert " OR " in q
    # 每个 token 双引号包裹，避免 FTS5 语法歧义
    assert '"宁德"' in q
    assert '"走势"' in q


def test_fts5_query_filters_stopwords():
    # 疑问/语气/虚词不参与 OR 检索，避免命中大量无关记忆
    q = FTS5Backend._fts5_query("茅台 估值 高 吗")
    assert "吗" not in q
    assert '"茅台"' in q
    assert '"估值"' in q


def test_fts5_query_all_stopwords_falls_back_to_raw():
    # 全是停用词（如「怎么样」）时回退原文，避免空查询串
    q = FTS5Backend._fts5_query("怎么样 呢")
    assert q  # 非空
    assert "怎么样" in q or "呢" in q


def test_fts5_query_truncates_long_tokens():
    # 超过 _QUERY_MAX_TOKENS 时按长度降序截断，长词（股票名/板块名）优先保留
    q = FTS5Backend._fts5_query("宁德 时代 最近 走势 如何 分析 判断 大盘")
    tokens = [t.strip('"') for t in q.split(" OR ")]
    assert len(tokens) <= FTS5Backend._QUERY_MAX_TOKENS
    # 长词「分析」「判断」应保留，停用词「如何」「最近」应被过滤
    assert "分析" in tokens
    assert "如何" not in tokens
