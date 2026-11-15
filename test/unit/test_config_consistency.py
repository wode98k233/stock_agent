"""配置一致性自检（2026-10-17 配置体检产出的回归护栏）。

堵三类已发生过的 bug：
1. `_ENV_MAP` 内重复键 —— dict 字面量后者覆盖前者，前者成死行；
   曾致 CONTEXT_SUMMARY_MODE 默认值双写漂移（standalone vs fork，实际 fork 生效）。
2. `_ENV_MAP` 有键但 `Config` 缺对应类属性 —— reload() 时静默跳过，面板改了不生效。
3. `_ENV_MAP` 转换器与类属性类型不一致 —— 曾致 STOCK_MEMORY_RERANKER_PRELOAD
   reload 后变字符串，"false" 为 truthy → preload 恒真。
"""
import inspect
import os
import re
from collections import Counter
from unittest.mock import patch

import config as config_module
import config._env_map as env_map_module
from config import Config

_ENV_MAP = config_module._ENV_MAP

_MISSING = object()


def _source_env_map_keys():
    """从源码提取 _ENV_MAP 字面量的键（dict 解析后重复键不可见，必须回源码）。"""
    src = inspect.getsource(env_map_module)
    start = src.index("_ENV_MAP = {")
    end = src.index("\n}", start)
    chunk = src[start:end]
    return re.findall(r'^\s+"([A-Z][A-Z0-9_]*)":', chunk, re.M)


def _is_bool_converter(conv) -> bool:
    """识别 `lambda v: v.lower() == "true"` 型布尔转换器。"""
    try:
        return conv("true") is True and conv("false") is False and conv("False") is False
    except Exception:
        return False


def _type_group(value):
    if isinstance(value, bool):  # bool 是 int 子类，必须先判
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


class TestEnvMapNoDuplicates:
    def test_no_duplicate_keys_in_source(self):
        keys = _source_env_map_keys()
        dup = [k for k, c in Counter(keys).items() if c > 1]
        assert not dup, f"_ENV_MAP 存在重复键（后者覆盖前者，前者是死行）: {dup}"

    def test_env_map_non_empty(self):
        # 2026-10-17 删档 10 键后为 198；阈值留余量但仍要求成规模
        assert len(_ENV_MAP) > 190


class TestClassAttrPresence:
    def test_every_env_map_key_has_class_attr(self):
        missing = [k for k in _ENV_MAP if getattr(Config, k, _MISSING) is _MISSING]
        assert not missing, f"_ENV_MAP 有键但 Config 缺类属性（reload 静默跳过）: {missing}"


class TestTypeContract:
    def test_bool_converters_hold_bool_attr(self):
        """bool 转换器的键，类属性必须是真 bool（防 'false' truthy 复发）。"""
        bad = []
        for key, (_env, _default, conv) in _ENV_MAP.items():
            if _is_bool_converter(conv) and not isinstance(getattr(Config, key), bool):
                bad.append(key)
        assert not bad, f"以下键 _ENV_MAP 是 bool 转换器但类属性不是 bool: {bad}"

    def test_converter_type_matches_class_attr(self):
        """转换器产物与类属性类型组一致（bool/int/float/str，防默认值漂移复发）。"""
        bad = []
        for key, (env, default, conv) in _ENV_MAP.items():
            if env in os.environ:
                # 环境已显式配置时类属性来自 env，不代表默认值；只对比类型
                expected = conv(default)
            else:
                expected = conv(default)
            actual = getattr(Config, key)
            if _type_group(expected) != _type_group(actual):
                bad.append(f"{key}: conv({default!r})={expected!r} vs attr={actual!r}")
        assert not bad, "转换器与类属性类型不一致:\n" + "\n".join(bad)


class TestDefaultContract:
    def test_defaults_match_when_env_unset(self, monkeypatch):
        """出厂默认契约：每个域 mixin 的类属性缺省 == _ENV_MAP 登记的缺省。

        实现方式：清空相关环境变量 + 屏蔽 load_dotenv 后，把 config/ 下各域
        文件作为**独立模块副本**逐个执行，读其纯缺省值，与 _ENV_MAP 对比。
        这正是 MEMORY_RETRIEVAL_TOP_K（类属性 5 vs _ENV_MAP 8）那类漂移的
        直接探针；不依赖「os.environ 里键未设置」——同会话先跑的测试一旦
        触发过 Config.reload()，load_dotenv 会把 .env 全量灌进 environ，
        按 environ 判断会假阴性/假阳性。
        """
        import glob
        import importlib.util

        for env, *_ in _ENV_MAP.values():
            monkeypatch.delenv(env, raising=False)
        # 隔离副本不调用 load_dotenv（域文件没有），但双保险 patch 源头
        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)

        pkg_dir = os.path.dirname(env_map_module.__file__)
        drift = []
        for path in sorted(glob.glob(os.path.join(pkg_dir, "*.py"))):
            base = os.path.basename(path)[:-3]
            if base.startswith("_"):  # _env_map 无类属性
                continue
            spec = importlib.util.spec_from_file_location(f"isolated_cfg_{base}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mixin = next(v for k, v in vars(mod).items()
                         if isinstance(v, type) and k.endswith("Mixin"))
            for attr, value in vars(mixin).items():
                if attr.startswith("_") or callable(value) or isinstance(
                        value, (classmethod, staticmethod, type)):
                    continue
                entry = _ENV_MAP.get(attr)
                if entry is None:
                    continue  # 固化常量（删档），不再受 _ENV_MAP 约束
                expected = entry[2](entry[1])
                if value != expected or _type_group(value) != _type_group(expected):
                    drift.append(f"{base}.{attr}: attr={value!r} vs map={expected!r}")
        assert not drift, "类属性缺省与 _ENV_MAP 缺省不一致:\n" + "\n".join(drift)


# 有意「删档」的固化常量：不再走 _ENV_MAP（运行时不可配），登记见 config/agent.py 等。
# 新增类属性若既不在此表、也不在 _ENV_MAP → 即「双写漏行」：reload() 时被静默跳过，
# 面板改了不生效。本表是**显式白名单**——确属固化常量须显式登记，否则测试失败。
_DELISTED_FIXED = frozenset({
    "CONTEXT_SUMMARY_MODE", "LLM_CACHE_CATEGORY_MAP", "LLM_CACHE_ENABLED",
    "LLM_CACHE_TTL_MAP", "LLM_ROUTING_ENABLED", "LLM_ROUTING_MAP",
    "LLM_TIER_FAST_MODEL", "LLM_TIER_STANDARD_MODEL", "LLM_TIER_STRONG_MODEL",
    "SKILL_ACTIVATE_TOOL_MODE", "SKILL_DETAIL_DELIVERY",
    "SKILL_ROUTER_SKIP_FIRST", "SKILL_SELECTOR_FORK", "SKILL_SELECTOR_PROMPT_MODE",
    "TOOL_SURFACE_MODE",
})


class TestReverseDrift:
    """反向双写：类属性有、_ENV_MAP 漏行 → reload() 静默跳过（面板改不生效）。"""

    def test_every_class_attr_is_declared(self):
        attrs = {a for a in dir(Config) if a.isupper() and not a.startswith("_")}
        undeclared = sorted(attrs - set(_ENV_MAP) - _DELISTED_FIXED)
        assert not undeclared, (
            "以下 Config 类属性既不在 _ENV_MAP 也不在 _DELISTED_FIXED 白名单（双写漏行 "
            "→ reload 不生效；确属固化常量请显式登记白名单）: " + str(undeclared))
