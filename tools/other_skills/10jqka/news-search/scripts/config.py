#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻搜索技能配置文件
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class Config:
    """配置管理类"""
    
    DEFAULT_CONFIG = {
        "api": {
            "base_url": "https://openapi.iwencai.com",
            "endpoint": "/v1/comprehensive/search",
            "timeout": 30,
            "max_retries": 3,
            "retry_delay": 1.0,
        },
        "search": {
            "default_limit": 10,
            "default_days": 30,
            "min_articles_for_sufficient": 3,
        },
        "output": {
            "default_format": "text",
            "csv_encoding": "utf-8-sig",
            "json_indent": 2,
        },
        "logging": {
            "level": "INFO",
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        }
    }
    
    def __init__(self, config_file: Optional[str] = None):
        """
        初始化配置
        
        Args:
            config_file: 配置文件路径，如果为None则使用默认配置
        """
        self.config_file = config_file
        self.config = self.DEFAULT_CONFIG.copy()
        
        if config_file and Path(config_file).exists():
            self.load_config(config_file)
        else:
            # 尝试从环境变量加载配置
            self.load_from_env()
        
        logger.debug("配置初始化完成")
    
    def load_config(self, config_file: str) -> None:
        """
        从配置文件加载配置
        
        Args:
            config_file: 配置文件路径
        """
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
            
            # 深度合并配置
            self._merge_config(self.config, user_config)
            logger.info(f"已从配置文件加载配置: {config_file}")
            
        except json.JSONDecodeError as e:
            logger.error(f"配置文件格式错误: {config_file} - {str(e)}")
        except Exception as e:
            logger.error(f"加载配置文件失败: {config_file} - {str(e)}")
    
    def load_from_env(self) -> None:
        """从环境变量加载配置"""
        env_config = {}
        
        # API配置
        api_key = os.getenv("IWENCAI_API_KEY")
        if api_key:
            env_config["api_key"] = api_key
        
        # 搜索配置
        default_limit = os.getenv("NEWS_SEARCH_DEFAULT_LIMIT")
        if default_limit:
            try:
                env_config.setdefault("search", {})["default_limit"] = int(default_limit)
            except ValueError:
                logger.warning(f"无效的环境变量值: NEWS_SEARCH_DEFAULT_LIMIT={default_limit}")
        
        default_days = os.getenv("NEWS_SEARCH_DEFAULT_DAYS")
        if default_days:
            try:
                env_config.setdefault("search", {})["default_days"] = int(default_days)
            except ValueError:
                logger.warning(f"无效的环境变量值: NEWS_SEARCH_DEFAULT_DAYS={default_days}")
        
        # 日志配置
        log_level = os.getenv("NEWS_SEARCH_LOG_LEVEL")
        if log_level and log_level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            env_config.setdefault("logging", {})["level"] = log_level
        
        if env_config:
            self._merge_config(self.config, env_config)
            logger.debug("已从环境变量加载配置")
    
    def _merge_config(self, base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
        """
        深度合并两个配置字典
        
        Args:
            base: 基础配置
            update: 更新配置
            
        Returns:
            合并后的配置
        """
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                base[key] = self._merge_config(base[key], value)
            else:
                base[key] = value
        return base
    
    def save_config(self, config_file: Optional[str] = None) -> None:
        """
        保存配置到文件
        
        Args:
            config_file: 配置文件路径，如果为None则使用初始化时的路径
        """
        save_file = config_file or self.config_file
        if not save_file:
            logger.error("未指定配置文件路径")
            return
        
        try:
            # 确保目录存在
            Path(save_file).parent.mkdir(parents=True, exist_ok=True)
            
            with open(save_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            
            logger.info(f"配置已保存到: {save_file}")
            
        except Exception as e:
            logger.error(f"保存配置失败: {save_file} - {str(e)}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键，支持点号分隔，如 "api.base_url"
            default: 默认值
            
        Returns:
            配置值
        """
        keys = key.split('.')
        value = self.config
        
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def set(self, key: str, value: Any) -> None:
        """
        设置配置值
        
        Args:
            key: 配置键，支持点号分隔，如 "api.base_url"
            value: 配置值
        """
        keys = key.split('.')
        config = self.config
        
        # 遍历到倒数第二个键
        for k in keys[:-1]:
            if k not in config or not isinstance(config[k], dict):
                config[k] = {}
            config = config[k]
        
        # 设置最后一个键的值
        config[keys[-1]] = value
        logger.debug(f"配置已更新: {key} = {value}")
    
    def get_api_key(self) -> Optional[str]:
        """获取API密钥"""
        return os.getenv("IWENCAI_API_KEY") or self.get("api_key")
    
    def get_api_config(self) -> Dict[str, Any]:
        """获取API配置"""
        return self.get("api", {})
    
    def get_search_config(self) -> Dict[str, Any]:
        """获取搜索配置"""
        return self.get("search", {})
    
    def get_output_config(self) -> Dict[str, Any]:
        """获取输出配置"""
        return self.get("output", {})
    
    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self.get("logging", {})
    
    def setup_logging(self) -> None:
        """设置日志配置"""
        log_config = self.get_logging_config()
        level = getattr(logging, log_config.get("level", "INFO"))
        format_str = log_config.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        
        logging.basicConfig(
            level=level,
            format=format_str
        )
        
        logger.debug(f"日志配置已设置: level={level}")


# 全局配置实例
_config_instance: Optional[Config] = None


def get_config(config_file: Optional[str] = None) -> Config:
    """
    获取配置实例（单例模式）
    
    Args:
        config_file: 配置文件路径
        
    Returns:
        配置实例
    """
    global _config_instance
    
    if _config_instance is None:
        _config_instance = Config(config_file)
    
    return _config_instance


def create_default_config(config_file: str) -> None:
    """
    创建默认配置文件
    
    Args:
        config_file: 配置文件路径
    """
    config = Config()
    config.save_config(config_file)
    logger.info(f"已创建默认配置文件: {config_file}")


if __name__ == "__main__":
    # 测试配置类
    config = get_config()
    print("API配置:", config.get_api_config())
    print("搜索配置:", config.get_search_config())
    print("API密钥:", config.get_api_key())