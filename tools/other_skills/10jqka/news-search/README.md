# 新闻搜索技能

财经领域资讯搜索引擎，调用同花顺问财的财经资讯搜索接口。

## 功能特点

- **财经资讯搜索**: 搜索各类财经新闻和资讯
- **多关键词处理**: 自动拆解复杂查询为多个简单查询
- **结果过滤**: 按时间、相关性等条件过滤结果
- **数据导出**: 支持将搜索结果导出为CSV、JSON等格式
- **批量处理**: 支持从文件读取多个查询并批量处理
- **错误处理**: 完善的错误处理和重试机制
- **日志记录**: 详细的运行日志和调试信息

## 数据来源

**所有搜索结果均来源于同花顺问财财经资讯搜索接口**，使用时请注明数据来源。

## 安装要求

- Python 3.7+
- requests库

## 快速开始

### 1. 设置API密钥

```bash
# 设置环境变量
export IWENCAI_API_KEY="your_api_key_here"
```

### 2. 基本使用

```bash
# 搜索财经新闻
python news_search.py -q "人工智能"

# 搜索最近7天的新闻
python news_search.py -q "芯片行业" -d 7

# 限制返回结果数量
python news_search.py -q "新能源汽车" -l 5

# 导出为CSV格式
python news_search.py -q "人工智能" -o results.csv -f csv

# 导出为JSON格式
python news_search.py -q "人工智能" -o results.json -f json
```

### 3. 批量处理

```bash
# 创建查询文件
echo "人工智能" > queries.txt
echo "芯片行业" >> queries.txt
echo "新能源汽车" >> queries.txt

# 批量处理并导出到目录
python news_search.py -i queries.txt -o output/ -f csv

# 批量处理并合并到一个文件
python news_search.py -i queries.txt -o all_results.csv -f csv
```

### 4. 管道操作

```bash
# 从管道读取查询
echo "人工智能" | python news_search.py

# 从文件读取并管道处理
cat queries.txt | xargs -I {} python news_search.py -q "{}"
```

## 命令行参数

```
usage: news_search.py [-h] (-q QUERY | -i INPUT) [-o OUTPUT] [-f {csv,json,text}] [-l LIMIT] [-d DAYS] [--api-key API_KEY] [--debug]

财经新闻搜索工具 - 调用同花顺问财的财经资讯搜索接口

optional arguments:
  -h, --help            显示帮助信息
  -q QUERY, --query QUERY
                        搜索关键词，支持中文
  -i INPUT, --input INPUT
                        输入文件路径，每行一个查询词（批量处理）
  -o OUTPUT, --output OUTPUT
                        输出文件路径或目录
  -f {csv,json,text}, --format {csv,json,text}
                        输出格式 (默认: text)
  -l LIMIT, --limit LIMIT
                        每查询返回的最大文章数量 (默认: 10)
  -d DAYS, --days DAYS  搜索最近多少天内的文章 (默认: 30)
  --api-key API_KEY     API密钥，如果不提供则从环境变量 IWENCAI_API_KEY 获取
  --debug               启用调试模式
```

## 配置说明

### 环境变量

- `IWENCAI_API_KEY`: API密钥（必需）
- `NEWS_SEARCH_DEFAULT_LIMIT`: 默认返回结果数量
- `NEWS_SEARCH_DEFAULT_DAYS`: 默认搜索天数
- `NEWS_SEARCH_LOG_LEVEL`: 日志级别（DEBUG, INFO, WARNING, ERROR, CRITICAL）

### 配置文件

技能支持JSON格式的配置文件，可以创建 `config.json` 文件：

```json
{
  "api": {
    "base_url": "https://openapi.iwencai.com",
    "endpoint": "/v1/comprehensive/search",
    "timeout": 30,
    "max_retries": 3
  },
  "search": {
    "default_limit": 10,
    "default_days": 30
  }
}
```

## 代码示例

### Python API调用

```python
from news_search import NewsSearchAPI, NewsProcessor

# 初始化API客户端
api = NewsSearchAPI()

# 搜索新闻
articles = api.search("人工智能")

# 处理结果
processor = NewsProcessor()
filtered_articles = processor.filter_by_date(articles, days=7)
sorted_articles = processor.sort_by_date(filtered_articles)

# 保存结果
processor.save_to_csv(sorted_articles, "results.csv")
```

### 查询拆解示例

```python
from news_search import QueryProcessor

# 复杂查询拆解
query = "人工智能和芯片行业最新动态"
sub_queries = QueryProcessor.split_complex_query(query)
# 返回: ["人工智能最新动态", "芯片行业最新动态"]
```

## 错误处理

技能包含完善的错误处理机制：

1. **网络异常**: 自动重试机制
2. **API认证失败**: 清晰的错误提示
3. **请求频率限制**: 指数退避重试
4. **数据解析错误**: 优雅降级处理

## 输出格式

### CSV格式
```
title,summary,url,publish_date,source
文章标题,文章摘要,文章网址,发布时间,同花顺问财
```

### JSON格式
```json
[
  {
    "title": "文章标题",
    "summary": "文章摘要",
    "url": "文章网址",
    "publish_date": "发布时间",
    "source": "同花顺问财"
  }
]
```

### 文本格式
```
============================================================
查询: 人工智能
找到 8 篇文章 (最近 30 天)
============================================================
数据来源: 同花顺问财财经资讯搜索
============================================================

1. 人工智能助力金融行业数字化转型
   摘要: 近日，多家金融机构宣布采用人工智能技术优化风控系统...
   发布时间: 2024-01-15 10:30:00
   链接: https://example.com/article/123
   数据来源: 同花顺问财
```

## 使用场景

### 财经新闻搜索
```bash
python news_search.py -q "央行货币政策"
```

### 行业趋势分析
```bash
python news_search.py -q "人工智能行业发展趋势" -d 90 -l 20
```

### 企业信息查询
```bash
python news_search.py -q "腾讯公司最新动态" -o tencent_news.csv -f csv
```

### 批量数据收集
```bash
# 收集多个行业的新闻
echo "人工智能" > industries.txt
echo "新能源汽车" >> industries.txt
echo "生物医药" >> industries.txt
python news_search.py -i industries.txt -o industry_news/ -f json
```

## 注意事项

1. **API密钥安全**: API密钥应从环境变量获取，不要硬编码在代码中
2. **请求频率限制**: 注意API提供商的请求频率限制
3. **数据使用规范**: 引用数据时必须注明来源：同花顺问财
4. **商业用途**: 不得将数据用于商业用途或违反相关法律法规

## 故障排除

### 常见问题

1. **API认证失败**
   ```
   错误: API认证失败，请检查API密钥
   解决方案: 检查 IWENCAI_API_KEY 环境变量是否正确设置
   ```

2. **网络连接问题**
   ```
   错误: 连接错误
   解决方案: 检查网络连接，或使用 --debug 参数查看详细错误
   ```

3. **无结果返回**
   ```
   未找到相关文章
   解决方案: 尝试调整查询关键词，或增加搜索天数 (-d 参数)
   ```

### 调试模式

使用 `--debug` 参数启用详细日志：

```bash
python news_search.py -q "测试" --debug
```

## 更新日志

### v1.0.0
- 实现基础财经资讯搜索功能
- 支持查询拆解和合并
- 实现完整错误处理机制
- 添加数据来源标注功能
- 提供详细的使用文档

## 许可证

MIT License

## 技术支持

如有问题或建议，请参考代码注释或联系开发者。

---

**数据来源声明**: 本技能所有搜索结果均来源于同花顺问财财经资讯搜索接口，使用时请务必注明数据来源。