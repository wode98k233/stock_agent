import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_config_validate_no_api_key():
    print("=" * 60)
    print("TC-Cfg-01: 缺少 API_KEY 报错")
    print("=" * 60)

    from config import Config
    old_key = Config.OPENAI_API_KEY
    Config.OPENAI_API_KEY = ""
    try:
        Config.validate()
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        print(f"  正确抛出: {e}")
        assert "OPENAI_API_KEY" in str(e)
    finally:
        Config.OPENAI_API_KEY = old_key
    print("[OK] 缺少 API_KEY 正确报错")


def test_config_validate_bad_json():
    print("\n" + "=" * 60)
    print("TC-Cfg-02: JSON 格式错误报错")
    print("=" * 60)

    from config import Config
    old = Config.OPENAI_HEADERS
    Config.OPENAI_HEADERS = "{invalid json"
    try:
        Config.validate()
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        print(f"  正确抛出: {e}")
        assert "JSON 格式错误" in str(e)
    finally:
        Config.OPENAI_HEADERS = old
    print("[OK] JSON 格式错误正确报错")


def test_config_validate_ok():
    print("\n" + "=" * 60)
    print("TC-Cfg-03: 正常配置通过校验")
    print("=" * 60)

    from config import Config
    old_key = Config.OPENAI_API_KEY
    old_headers = Config.OPENAI_HEADERS
    old_params = Config.OPENAI_DEFAULT_PARAMS
    Config.OPENAI_API_KEY = "sk-test-key"
    Config.OPENAI_HEADERS = ""
    Config.OPENAI_DEFAULT_PARAMS = ""
    try:
        Config.validate()
        print("  校验通过")
    finally:
        Config.OPENAI_API_KEY = old_key
        Config.OPENAI_HEADERS = old_headers
        Config.OPENAI_DEFAULT_PARAMS = old_params
    print("[OK] 正常配置通过校验")


if __name__ == "__main__":
    try:
        test_config_validate_no_api_key()
        test_config_validate_bad_json()
        test_config_validate_ok()
        print("\n" + "=" * 60)
        print("[PASS] All Config validate tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
