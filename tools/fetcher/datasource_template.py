"""
选股雷达 - 数据源模板
展示如何添加新的数据源
"""
import pandas as pd
from tools.fetcher import DataSource, DataSourceManager


class ExampleDataSource(DataSource):
    """示例数据源（可作为模板使用）"""
    name: str = "example"  # 数据源名称，必须唯一
    priority: int = 90      # 优先级，数字越大优先级越高
    enabled: bool = True    # 是否启用
    
    # 如果这个数据源需要额外的依赖，可以在这里导入
    # _client = None
    
    @classmethod
    def is_available(cls) -> bool:
        """
        检查数据源是否可用
        可以在这里检查：
        - 依赖是否已安装
        - API key 是否配置
        - 网络连接是否正常
        """
        try:
            # 例如：检查特定库是否已安装
            # import some_lib
            return cls.enabled
        except ImportError:
            cls.enabled = False
            return False
    
    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """获取个股历史K线"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_stock_hist 方法")
    
    @classmethod
    def get_spot_em(cls) -> pd.DataFrame:
        """获取全部A股实时行情"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_spot_em 方法")
    
    @classmethod
    def get_board_industry_cons(cls, symbol: str) -> pd.DataFrame:
        """行业板块成分股"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_board_industry_cons 方法")
    
    @classmethod
    def get_board_concept_cons(cls, symbol: str) -> pd.DataFrame:
        """概念板块成分股"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_board_concept_cons 方法")
    
    @classmethod
    def get_board_industry_list(cls) -> pd.DataFrame:
        """行业板块列表"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_board_industry_list 方法")
    
    @classmethod
    def get_board_concept_list(cls) -> pd.DataFrame:
        """概念板块列表"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_board_concept_list 方法")
    
    @classmethod
    def get_stock_news(cls, symbol: str) -> pd.DataFrame:
        """个股新闻"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_stock_news 方法")
    
    @classmethod
    def get_stock_rating(cls, symbol: str) -> pd.DataFrame:
        """机构评级"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_stock_rating 方法")
    
    @classmethod
    def get_financial_abstract(cls, symbol: str) -> pd.DataFrame:
        """财务摘要"""
        # 实现你的数据源调用逻辑
        raise NotImplementedError("请实现 get_financial_abstract 方法")


# 注册新的数据源（取消下面的注释来启用）
# DataSourceManager.register_source(ExampleDataSource)
