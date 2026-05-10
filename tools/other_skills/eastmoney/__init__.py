"""
东方财富 Skills 集成模块
"""
from .config import EastMoneyConfig
from .installer import install_all_skills, check_installed_skills

__all__ = [
    "EastMoneyConfig",
    "install_all_skills",
    "check_installed_skills"
]
