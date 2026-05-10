"""内部配置（简单类属性，由 ConfigManager 统一管理）

注意：此类的属性为模块级默认值。运行时的统一管理由
ConfigManager.datasource 负责，此处保持向后兼容。
"""
import os


class Config:
    DATASOURCE_MAX_FAILS = int(os.getenv("DATASOURCE_MAX_FAILS", "5"))
    DATASOURCE_RECOVERY_SECS = int(os.getenv("DATASOURCE_RECOVERY_SECS", "600"))
    TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
