import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_context_var_initial():
    print("=" * 60)
    print("TC-Retry-01: 上下文变量初始为 None")
    print("=" * 60)

    from tools.fetcher.base import _current_source

    assert _current_source.get() is None
    print("[OK] 上下文变量初始为 None")


def test_context_var_set_and_get():
    print("\n" + "=" * 60)
    print("TC-Retry-02: 上下文变量 set/get")
    print("=" * 60)

    from tools.fetcher.base import _current_source

    token = _current_source.set("test_source")
    assert _current_source.get() == "test_source"
    _current_source.reset(token)
    assert _current_source.get() is None
    print("[OK] 上下文变量 set/get 正确")


def test_get_current_source_with_context():
    print("\n" + "=" * 60)
    print("TC-Retry-03: get_current_source 优先使用上下文变量")
    print("=" * 60)

    from tools.fetcher.base import _current_source, DataSourceManager

    token = _current_source.set("mock_source")
    source = DataSourceManager.get_current_source()
    assert source == "mock_source"
    _current_source.reset(token)
    print("[OK] get_current_source 优先使用上下文变量")


if __name__ == "__main__":
    try:
        test_context_var_initial()
        test_context_var_set_and_get()
        test_get_current_source_with_context()
        print("\n" + "=" * 60)
        print("[PASS] All retry context tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
