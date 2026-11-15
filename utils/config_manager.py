"""
配置管理器 - Web 配置面板的辅助工具

职责：
  1. generate_diff() — 对比当前 Config 值与用户提交的变更，返回 diff
  2. write_env_changes() — 将变更写入 .env 文件（保留注释和未变更行）
  3. reload() — 重新加载 .env 并刷新 Config 类属性

注意：本模块不维护独立的配置状态。所有配置值统一由 config.Config 类属性持有。
"""
import io
import os
from typing import Any, Dict, List
from threading import Lock

from dotenv import dotenv_values


def env_roundtrip_safe(value: Any) -> bool:
    """判断一个值写成 `KEY=value` 后能否被 dotenv **原样**读回。

    2026-11-15：面板改密钥后直接写 .env，而 dotenv 的解析器有几处会静默改值——
    `${VAR}` 会被变量替换（哪怕在单/双引号里）、` #` 之后会被当注释截断、
    首尾空白会被 strip、换行会破坏整个文件结构。密钥被悄悄改掉 = 保存成功但鉴权失败，
    所以写入前用同一个解析器做一次往返校验，读不回来就拒绝并提示。
    """
    s = "" if value is None else str(value)
    if "\n" in s or "\r" in s:
        return False
    try:
        parsed = dotenv_values(stream=io.StringIO(f"__PROBE__={s}"))
    except Exception:  # noqa: BLE001 —— 解析器自己都炸了，一律当不安全
        return False
    return parsed.get("__PROBE__") == s


class ConfigManager:
    """配置管理器（线程安全单例）"""

    _instance: "ConfigManager | None" = None
    _lock = Lock()
    # .env 是「读-改-写」：两个 apply 并发会互相覆盖掉对方的行，写入串行化
    _write_lock = Lock()

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
        """读取现有 .env 文件，更新变更的键值对，保留未变更的行（包括注释），写回

        ⚠️ 值必须是 dotenv 能原样读回的（见 `env_roundtrip_safe`）：密钥由路由层先校验，
        这里不再替用户改写（不加引号、不做转义）——静默改值比报错更糟。
        """
        with ConfigManager._write_lock:
            return ConfigManager._write_env_changes_locked(changes, env_path)

    @staticmethod
    def _write_env_changes_locked(changes: Dict[str, Any], env_path: str) -> bool:
        lines = []
        if os.path.exists(env_path):
            # newline="" 保留原始行尾：Windows 上的 .env 常是 CRLF，
            # 之前统一写 "\n" 会把改动的那几行变成 LF（整文件混行尾、diff 噪声大）
            with open(env_path, "r", encoding="utf-8", newline="") as f:
                lines = f.readlines()

        newline = os.linesep
        if lines:
            newline = "\r\n" if lines[0].endswith("\r\n") else "\n"

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
                    new_lines.append(f"{key}={_normalize_value(changes[key])}{newline}")
                    updated_keys.add(key)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        for key, value in changes.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={_normalize_value(value)}{newline}")

        with open(env_path, "w", encoding="utf-8", newline="") as f:
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
