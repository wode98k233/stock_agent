from config import Config, _ENV_MAP


def test_context_window_defaults():
    """类属性必须存在且类型正确；默认值的事实源是 _ENV_MAP，不在本用例断言。

    ⚠️ 不要断言 Config.CONTEXT_WINDOW_TOKENS == 1000000：Config 反映 .env + 环境变量，
    开发者调过窗口就会本地红（环境相关、时红时绿）。
    """
    assert isinstance(Config.CONTEXT_WINDOW_TOKENS, int)
    assert isinstance(Config.CONTEXT_WINDOW_BY_MODEL, str)


def test_context_window_in_env_map():
    # reload() 走 _ENV_MAP + getattr(cls, k)；只写类属性会静默失效
    assert "CONTEXT_WINDOW_TOKENS" in _ENV_MAP
    assert _ENV_MAP["CONTEXT_WINDOW_TOKENS"][1] == "1000000"
    assert _ENV_MAP["CONTEXT_WINDOW_TOKENS"][2] is int
    assert "CONTEXT_WINDOW_BY_MODEL" in _ENV_MAP
    assert _ENV_MAP["CONTEXT_WINDOW_BY_MODEL"][1] == ""   # 补上不对称：原来只断言了存在，没断言默认值
    assert _ENV_MAP["CONTEXT_WINDOW_BY_MODEL"][2] is str
