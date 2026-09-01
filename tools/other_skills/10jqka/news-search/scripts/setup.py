#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻搜索技能安装脚本
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="news-search-skill",
    version="1.0.0",
    author="News Search Skill",
    author_email="",
    description="财经领域资讯搜索引擎，调用同花顺问财的财经资讯搜索接口",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.7",
    install_requires=[
        "requests>=2.25.0",
    ],
    entry_points={
        "console_scripts": [
            "news-search=.trae.skills.新闻搜索.news_search:main",
        ],
    },
)