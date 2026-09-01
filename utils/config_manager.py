"""
配置管理器 - Web 配置面板的辅助工具

职责：
  1. generate_diff() — 对比当前 Config 值与用户提交的变更，返回 diff
  2. write_env_changes() — 将变更写入 .env 文件（保留注释和未变更行）
  3. reload() — 重新加载 .env 并刷新 Config 类属性

注意：本模块不维护独立的配置状态。所有配置值统一由 config.Config 类属性持有。
"""
import os
from typing import Any, Dict, List
from threading import Lock


class ConfigManager:
    """配置管理器（线程安全单例）"""

    _instance: "ConfigManager | None" = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def generate_diff(self, changes: Dict[str, Any]) -> List[Dict[str, str]]:
        """对比当前 Config 值和 proposed changes，返回变更列表"""
        from config import Config
        diff = []
        for key, new_value in changes.items():
            if not hasattr(Config, key):
                continue
            old_value = getattr(Config, key)
            old_str = str(old_value) if old_value is not None else ""
            new_str = str(new_value) if new_value is not None else ""
            if old_str != new_str:
                diff.append({"key": key, "old": old_str, "new": new_str})
        return diff

    @staticmethod
    def write_env_changes(changes: Dict[str, Any], env_path: str) -> bool:
        """读取现有 .env 文件，更新变更的键值对，保留未变更的行（包括注释），写回"""
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        def _normalize_value(value):
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0]
                if key in changes:
                    new_lines.append(f"{key}={_normalize_value(changes[key])}\n")
                    updated_keys.add(key)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        for key, value in changes.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={_normalize_value(value)}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return True

    def reload(self) -> set:
        """重新从 .env 加载并更新 Config 类属性，返回变更的 key 集合"""
        from config import Config
        return Config.reload()


def create_config_manager() -> ConfigManager:
    """创建独立的配置管理器实例（用于测试隔离）"""
    ConfigManager._instance = None
    return ConfigManager()
