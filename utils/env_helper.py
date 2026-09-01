"""环境变量辅助工具

解决打包后 .env 路径问题：
- 开发模式：直接读取项目根目录的 .env
- 打包模式：读取 PyInstaller 解压目录的 .env

支持从模板生成 .env 文件。
"""
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Set
from dotenv import load_dotenv, dotenv_values

from utils.app_paths import get_resource_root, get_app_dir


def _is_frozen() -> bool:
    """是否为 PyInstaller 打包环境"""
    return getattr(sys, 'frozen', False)


def get_env_path() -> Path:
    """获取 .env 文件路径（用户可写，打包态在 exe 同级目录）"""
    root = Path(get_app_dir())
    return root / '.env'


def get_template_path() -> Path:
    """获取 .env.example 模板文件路径"""
    root = Path(get_resource_root())
    return root / '.env.example'


def load_env() -> dict:
    """加载 .env 并返回所有键值对

    Returns:
        dict: 环境变量字典，键全大写
    """
    env_path = get_env_path()
    if env_path.exists():
        load_dotenv(env_path, override=True)
        return dotenv_values(env_path)
    return {}


def get_env(key: str, default: str = "") -> str:
    """获取环境变量值（优先 .env，其次系统环境变量）"""
    # 先检查系统环境变量（已被 load_dotenv 注入）
    value = os.getenv(key)
    if value is not None:
        return value

    # 再检查 .env 文件
    env_path = get_env_path()
    if env_path.exists():
        values = dotenv_values(env_path)
        return values.get(key, default)

    return default


def get_env_diff() -> dict:
    """检测 .env 与当前进程环境变量的差异

    Returns:
        dict: {key: (file_value, process_value)} 的差异字典
    """
    env_path = get_env_path()
    if not env_path.exists():
        return {}

    file_values = dotenv_values(env_path)
    diff = {}
    for key, file_val in file_values.items():
        process_val = os.getenv(key)
        if process_val != file_val:
            diff[key] = (file_val, process_val)
    return diff


def _parse_env_keys(file_path: Path) -> Set[str]:
    """从 env 文件中解析所有键名"""
    keys = set()
    if not file_path.exists():
        return keys

    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith('#'):
                continue
            # 解析 KEY=VALUE 格式
            match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match:
                keys.add(match.group(1))

    return keys


def generate_env_from_template(
    template_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    keep_existing: bool = True,
) -> Dict[str, str]:
    """从模板生成 .env 文件

    Args:
        template_path: 模板文件路径，默认为 .env.example
        output_path: 输出文件路径，默认为 .env
        keep_existing: 是否保留现有 .env 中的用户配置

    Returns:
        dict: 新增的配置项 {key: default_value}
    """
    template_path = template_path or get_template_path()
    output_path = output_path or get_env_path()

    if not template_path.exists():
        raise FileNotFoundError(f"模板文件不存在: {template_path}")

    # 读取模板内容
    with open(template_path, 'r', encoding='utf-8') as f:
        template_lines = f.readlines()

    # 解析模板中的键
    template_keys = _parse_env_keys(template_path)

    # 读取现有 .env（如果存在且需要保留）
    existing_values: Dict[str, str] = {}
    if keep_existing and output_path.exists():
        existing_values = dotenv_values(output_path)

    # 生成新内容
    new_lines = []
    added_keys: Dict[str, str] = {}

    for line in template_lines:
        stripped = line.strip()

        # 注释和空行直接保留
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue

        # 解析 KEY=VALUE
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', stripped)
        if match:
            key = match.group(1)
            default_value = match.group(2).strip()

            # 如果现有 .env 中有该键，使用现有值
            if key in existing_values:
                user_value = existing_values[key]
                new_lines.append(f"{key}={user_value}\n")
            else:
                # 使用模板默认值
                new_lines.append(line)
                if default_value and not default_value.startswith('your_'):
                    added_keys[key] = default_value
        else:
            new_lines.append(line)

    # 写入输出文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    return added_keys


def sync_env_with_template(
    template_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Dict[str, str]:
    """同步 .env 与模板（添加新配置项，保留现有值）

    Args:
        template_path: 模板文件路径，默认为 .env.example
        output_path: 输出文件路径，默认为 .env

    Returns:
        dict: 新增的配置项 {key: default_value}
    """
    return generate_env_from_template(
        template_path=template_path,
        output_path=output_path,
        keep_existing=True,
    )
