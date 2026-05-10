"""
东方财富 Skills 配置
"""
import os
from dotenv import load_dotenv

load_dotenv()


class EastMoneyConfig:
    """东方财富配置"""
    
    # API Key
    MX_APIKEY = os.getenv("MX_APIKEY", "")
    
    # Skills 安装路径
    SKILLS_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    
    # 各个 Skill 的子目录
    SKILL_DIRS = {
        "data": "mx_data",
        "search": "mx_search",
        "xuangu": "mx_xuangu",
        "zixuan": "mx_zixuan",
        "moni": "mx_moni",
    }
    
    # 下载链接
    DOWNLOAD_URLS = {
        "data": "https://marketing.dfcfw.com/res/download/A620260331IHX67H.zip",
        "search": "https://marketing.dfcfw.com/res/download/A620260331K5WDTK.zip",
        "xuangu": "https://marketing.dfcfw.com/res/download/A620260331NXBVEY.zip",
        "zixuan": "https://marketing.dfcfw.com/res/download/A6202603314TMGR1.zip",
        "moni": "https://marketing.dfcfw.com/res/download/A620260402S10QIM.zip",
    }
    
    @classmethod
    def is_available(cls):
        """检查是否配置了 API Key"""
        return bool(cls.MX_APIKEY)
    
    @classmethod
    def get_skill_path(cls, skill_name):
        """获取 Skill 完整路径"""
        return os.path.join(cls.SKILLS_BASE_DIR, cls.SKILL_DIRS.get(skill_name, skill_name))
    
    @classmethod
    def get_headers(cls):
        """获取 API 请求头"""
        return {
            "apikey": cls.MX_APIKEY,
            "Content-Type": "application/json"
        }
