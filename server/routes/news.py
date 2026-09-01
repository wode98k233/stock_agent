"""热点新闻 API

提供市场热点新闻和个股新闻查询接口。
市场热点新闻有缓存（1小时/4小时），避免频繁调用外部 API。
"""
import logging
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=['news'])

# 分类 → 搜索问句映射
_CATEGORY_QUERIES = {
    "全部": "今日A股市场热点新闻",
    "政策": "最新财经政策 央行 国务院 监管政策 降准降息",
    "行业": "行业板块热点 半导体 新能源 消费 医药 科技",
    "公司": "上市公司最新公告 业绩 增持减持 回购 分红",
    "宏观": "宏观经济 GDP CPI PMI 美联储 汇率 利率",
}


@router.get("/api/news/hot")
async def get_hot_news(
    category: str = Query("全部", description="新闻分类"),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
):
    """获取市场热点新闻（带缓存）"""
    try:
        from utils.cache.api import get_market_news_cache, set_market_news_cache

        # 1. 查缓存（按分类缓存）
        cached = get_market_news_cache(category)
        if cached is not None:
            items = cached if isinstance(cached, list) else []
            if limit and len(items) > limit:
                items = items[:limit]
            return {"items": items, "total": len(items), "cached": True}

        # 2. 缓存未命中，构造 query 调 fetcher
        query = _CATEGORY_QUERIES.get(category, _CATEGORY_QUERIES["全部"])
        from tools.fetcher import ak_market_news
        df = ak_market_news(query=query, limit=200)  # 多取一些，后面截断
        if df is None or df.empty:
            return {"items": [], "total": 0, "cached": False}

        # 按时间倒序
        if "publish_time" in df.columns:
            df = df.sort_values("publish_time", ascending=False)

        items = []
        for _, row in df.iterrows():
            items.append({
                "title": str(row.get("title", "")),
                "summary": str(row.get("summary", "")),
                "source": str(row.get("source", "")),
                "publish_time": str(row.get("publish_time", "")),
                "url": str(row.get("url", "")),
            })

        # 3. 写缓存（存完整列表，limit 在读取时截断）
        set_market_news_cache(category, items)

        if limit and len(items) > limit:
            items = items[:limit]
        return {"items": items, "total": len(items), "cached": False}
    except Exception as e:
        logger.error(f"获取热点新闻失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取热点新闻失败: {e}")


@router.get("/api/news/stock/{code}")
async def get_stock_news(
    code: str,
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
):
    """获取个股新闻（带缓存）"""
    try:
        from utils.cache.api import get_news_cache, set_news_cache

        # 1. 查缓存
        cached = get_news_cache(code)
        if cached is not None:
            items = cached if isinstance(cached, list) else []
            if limit and len(items) > limit:
                items = items[:limit]
            return {"items": items, "total": len(items), "cached": True}

        # 2. 缓存未命中，调 fetcher
        from tools.fetcher import ak_stock_news
        df = ak_stock_news(symbol=code)
        if df is None or df.empty:
            return {"items": [], "total": 0, "cached": False}

        # 统一列名
        col_map = {}
        for col in df.columns:
            cl = col.lower()
            if "title" in cl or "标题" in cl:
                col_map[col] = "title"
            elif "content" in cl or "摘要" in cl or "内容" in cl:
                col_map[col] = "summary"
            elif "source" in cl or "来源" in cl:
                col_map[col] = "source"
            elif "time" in cl or "date" in cl or "时间" in cl or "日期" in cl:
                col_map[col] = "publish_time"
            elif "url" in cl or "link" in cl or "链接" in cl:
                col_map[col] = "url"
        df = df.rename(columns=col_map)

        # 按时间倒序
        if "publish_time" in df.columns:
            df = df.sort_values("publish_time", ascending=False)

        items = []
        for _, row in df.iterrows():
            items.append({
                "title": str(row.get("title", "")),
                "summary": str(row.get("summary", "")),
                "source": str(row.get("source", "")),
                "publish_time": str(row.get("publish_time", "")),
                "url": str(row.get("url", "")),
            })

        # 3. 写缓存
        set_news_cache(code, items)

        if limit and len(items) > limit:
            items = items[:limit]
        return {"items": items, "total": len(items), "cached": False}
    except Exception as e:
        logger.error(f"获取个股新闻失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取个股新闻失败: {e}")
