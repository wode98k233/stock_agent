import memory.sdk as sdk_module
from unittest.mock import MagicMock


def test_set_sdk_controls_module_singleton(monkeypatch):
    monkeypatch.setattr(sdk_module, "_build_embedding_fn", lambda: None)
    sdk = object()

    sdk_module.set_sdk(sdk)
    assert sdk_module.get_sdk() is sdk

    sdk_module.set_sdk(None)
    assert sdk_module.get_sdk() is not sdk
    sdk_module.set_sdk(None)


def test_web_memory_initialization_registers_shared_sdk(monkeypatch):
    import memory.backend
    import server.bootstrap as bootstrap
    from server.routes import meta

    class FakeSDK:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(bootstrap, "_memory_sdk_built", False)
    monkeypatch.setattr(bootstrap, "_memory_sdk_instance", None)
    monkeypatch.setattr(bootstrap.Config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(bootstrap, "_build_embedding_fn", lambda: None)
    monkeypatch.setattr(bootstrap, "_warmup_memory_async", lambda sdk: None)
    monkeypatch.setattr(memory.backend, "ensure_jieba_ready", lambda logger: None)
    monkeypatch.setattr(sdk_module, "MemorySDK", FakeSDK)
    monkeypatch.setattr(sdk_module, "load_memory_config", lambda: {})
    monkeypatch.setattr(meta, "set_memory_enabled", lambda enabled: None)
    monkeypatch.setattr(meta, "set_memory_warming", lambda warming: None)
    sdk_module.set_sdk(None)

    web_sdk = bootstrap._init_memory_sdk()

    assert sdk_module.get_sdk() is web_sdk
    sdk_module.set_sdk(None)


def test_sdk_clean_expired_delegates_to_backend():
    sdk = sdk_module.MemorySDK(sdk_module.MemoryConfig())
    sdk._backend = MagicMock()
    sdk._backend.clean_expired.return_value = 3
    sdk._initialized = True

    assert sdk.clean_expired("2026-07-17T00:00:00") == 3
    sdk._backend.clean_expired.assert_called_once_with("2026-07-17T00:00:00")
