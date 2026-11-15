"""兼容壳：配置实现已拆分至 config/ 包（2026-10-17）。

`from config import Config` 解析到 config/ 包（同名包优先于模块），
本文件仅作防御性转发保留；请勿在此新增配置。
"""

from config import Config, _ENV_MAP  # noqa: F401
