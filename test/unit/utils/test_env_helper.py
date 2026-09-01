# -*- coding: utf-8 -*-
"""环境变量辅助工具单元测试。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestParseEnvKeys:
    """解析 env 文件键名测试"""

    def test_parse_basic(self):
        """测试基本解析"""
        from utils.env_helper import _parse_env_keys

        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False, encoding='utf-8') as f:
            f.write("KEY1=value1\n")
            f.write("KEY2=value2\n")
            f.write("# 注释\n")
            f.write("\n")
            f.write("KEY3=\n")
            temp_path = Path(f.name)

        try:
            keys = _parse_env_keys(temp_path)
            assert keys == {"KEY1", "KEY2", "KEY3"}
        finally:
            temp_path.unlink()

    def test_parse_empty(self):
        """测试空文件"""
        from utils.env_helper import _parse_env_keys

        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False, encoding='utf-8') as f:
            f.write("")
            temp_path = Path(f.name)

        try:
            keys = _parse_env_keys(temp_path)
            assert keys == set()
        finally:
            temp_path.unlink()

    def test_parse_not_exists(self):
        """测试不存在的文件"""
        from utils.env_helper import _parse_env_keys

        keys = _parse_env_keys(Path("/nonexistent/.env"))
        assert keys == set()


class TestGenerateEnvFromTemplate:
    """从模板生成 .env 测试"""

    def test_generate_new(self):
        """测试生成新文件"""
        from utils.env_helper import generate_env_from_template

        with tempfile.TemporaryDirectory() as tmpdir:
            template = Path(tmpdir) / ".env.example"
            output = Path(tmpdir) / ".env"

            template.write_text(
                "# 配置\n"
                "KEY1=default1\n"
                "KEY2=\n"
                "OPENAI_API_KEY=your_key_here\n",
                encoding='utf-8'
            )

            added = generate_env_from_template(template, output, keep_existing=False)

            assert output.exists()
            content = output.read_text(encoding='utf-8')
            assert "KEY1=default1" in content
            assert "KEY2=" in content
            assert "OPENAI_API_KEY=your_key_here" in content
            # your_key_here 不算新增
            assert "OPENAI_API_KEY" not in added

    def test_keep_existing(self):
        """测试保留现有配置"""
        from utils.env_helper import generate_env_from_template

        with tempfile.TemporaryDirectory() as tmpdir:
            template = Path(tmpdir) / ".env.example"
            output = Path(tmpdir) / ".env"

            template.write_text(
                "KEY1=default1\n"
                "KEY2=default2\n"
                "KEY3=default3\n",
                encoding='utf-8'
            )

            # 现有 .env 有自己的值
            output.write_text(
                "KEY1=user_value1\n"
                "KEY2=user_value2\n",
                encoding='utf-8'
            )

            added = generate_env_from_template(template, output, keep_existing=True)

            content = output.read_text(encoding='utf-8')
            # 保留用户值
            assert "KEY1=user_value1" in content
            assert "KEY2=user_value2" in content
            # 新增配置项
            assert "KEY3=default3" in content
            assert "KEY3" in added

    def test_template_not_found(self):
        """测试模板不存在"""
        from utils.env_helper import generate_env_from_template

        with pytest.raises(FileNotFoundError):
            generate_env_from_template(Path("/nonexistent/.env.example"))


class TestSyncEnvWithTemplate:
    """同步 .env 测试"""

    def test_sync(self):
        """测试同步"""
        from utils.env_helper import sync_env_with_template

        with tempfile.TemporaryDirectory() as tmpdir:
            template = Path(tmpdir) / ".env.example"
            output = Path(tmpdir) / ".env"

            template.write_text(
                "OLD_KEY=old_value\n"
                "NEW_KEY=new_value\n",
                encoding='utf-8'
            )

            output.write_text(
                "OLD_KEY=my_value\n",
                encoding='utf-8'
            )

            added = sync_env_with_template(template, output)

            content = output.read_text(encoding='utf-8')
            assert "OLD_KEY=my_value" in content  # 保留
            assert "NEW_KEY=new_value" in content  # 新增
            assert "NEW_KEY" in added
