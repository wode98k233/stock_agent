# -*- coding: utf-8 -*-
"""
东方财富反爬补丁

原理：
1. 猴子补丁 requests.Session.request，拦截东方财富域名的请求
2. 获取 NID 授权令牌（东方财富的反爬验证）
3. 使用 fake_useragent 随机 User-Agent
4. 生成设备指纹（canvas/webgl/font/audio）
5. 随机休眠降低请求频率

使用方式：
    from tools.fetcher.patches.eastmoney_patch import eastmoney_patch
    eastmoney_patch()  # 启用补丁
"""
import hashlib
import random
import secrets
import threading
import time
import requests
import json
import uuid
import logging

logger = logging.getLogger("radar.fetcher.patch")

# 保存原始 request 方法
original_request = requests.Session.request

# 延迟加载 fake_useragent
_ua = None


def _get_ua():
    """延迟加载 UserAgent"""
    global _ua
    if _ua is None:
        try:
            from fake_useragent import UserAgent
            _ua = UserAgent()
        except ImportError:
            logger.warning("fake_useragent 未安装，使用内置 UA 池")
            _ua = None
    return _ua


# 内置 UA 池（fallback）
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def _get_random_ua():
    """获取随机 User-Agent"""
    ua = _get_ua()
    if ua:
        return ua.random
    return random.choice(_USER_AGENTS)


class AuthCache:
    """NID 授权令牌缓存"""
    def __init__(self):
        self.data = None
        self.expire_at = 0
        self.lock = threading.Lock()
        self.ttl = 20


_cache = AuthCache()


class PatchSign:
    """补丁状态标记"""
    def __init__(self):
        self.patched = False

    def set_patch(self, patched):
        self.patched = patched

    def is_patched(self):
        return self.patched


_patch_sign = PatchSign()


def _generate_uuid_md5():
    """生成 UUID 并对其进行 MD5 哈希处理"""
    unique_id = str(uuid.uuid4())
    md5_hash = hashlib.md5(unique_id.encode('utf-8')).hexdigest()
    return md5_hash


def _generate_st_nvi():
    """生成 st_nvi 值"""
    HASH_LENGTH = 4

    def generate_random_string(length=21):
        charset = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"
        return ''.join(secrets.choice(charset) for _ in range(length))

    def sha256(input_str):
        return hashlib.sha256(input_str.encode('utf-8')).hexdigest()

    random_str = generate_random_string()
    hash_prefix = sha256(random_str)[:HASH_LENGTH]
    return random_str + hash_prefix


def _get_nid(user_agent):
    """
    获取东方财富的 NID 授权令牌

    Args:
        user_agent: 用户代理字符串

    Returns:
        NID 授权令牌，失败返回 None
    """
    now = time.time()

    # 检查缓存
    if _cache.data and now < _cache.expire_at:
        return _cache.data

    with _cache.lock:
        try:
            url = "https://anonflow2.eastmoney.com/backend/api/webreport"

            # 随机屏幕分辨率
            screen_resolution = random.choice(['1920X1080', '2560X1440', '3840X2160'])

            payload = json.dumps({
                "osPlatform": "Windows",
                "sourceType": "WEB",
                "osversion": "Windows 10.0",
                "language": "zh-CN",
                "timezone": "Asia/Shanghai",
                "webDeviceInfo": {
                    "screenResolution": screen_resolution,
                    "userAgent": user_agent,
                    "canvasKey": _generate_uuid_md5(),
                    "webglKey": _generate_uuid_md5(),
                    "fontKey": _generate_uuid_md5(),
                    "audioKey": _generate_uuid_md5()
                }
            })

            headers = {
                'Cookie': f'st_nvi={_generate_st_nvi()}',
                'Content-Type': 'application/json'
            }

            response = requests.request("POST", url, headers=headers, data=payload, timeout=30)
            response.raise_for_status()

            data = response.json()
            nid = data['data']['nid']

            _cache.data = nid
            _cache.expire_at = now + _cache.ttl
            return nid

        except requests.exceptions.RequestException as e:
            logger.warning(f"请求东方财富授权接口失败: {e}")
            _cache.data = None
            _cache.expire_at = now + 5 * 60  # 失败后5分钟重试
            return None

        except (KeyError, json.JSONDecodeError) as e:
            logger.warning(f"解析东方财富授权接口响应失败: {e}")
            _cache.data = None
            _cache.expire_at = now + 5 * 60
            return None


def _is_eastmoney_domain(url: str) -> bool:
    """
    检查是否为东方财富域名

    支持：
    - push2.eastmoney.com
    - 82.push2.eastmoney.com
    - push2his.eastmoney.com
    - fund.eastmoney.com
    """
    if not url:
        return False

    # 检查是否包含 eastmoney.com
    if "eastmoney.com" not in url:
        return False

    # 检查是否为目标接口
    target_patterns = [
        "push2.eastmoney.com",
        "push2his.eastmoney.com",
        "fund.eastmoney.com",
    ]

    return any(pattern in url for pattern in target_patterns)


def eastmoney_patch():
    """
    启用东方财富反爬补丁

    原理：猴子补丁 requests.Session.request
    - 拦截东方财富域名的请求（支持子域名如 82.push2.eastmoney.com）
    - 添加 NID 授权令牌
    - 随机 User-Agent
    - 随机休眠 1-4 秒
    """
    if _patch_sign.is_patched():
        return

    def patched_request(self, method, url, **kwargs):
        # 拦截东方财富域名（包括子域名）
        if not _is_eastmoney_domain(url):
            return original_request(self, method, url, **kwargs)

        # 获取随机 User-Agent
        user_agent = _get_random_ua()

        # 处理 Headers
        headers = kwargs.get("headers", {})
        headers["User-Agent"] = user_agent

        # 获取 NID 授权令牌
        nid = _get_nid(user_agent)
        if nid:
            headers["Cookie"] = f"nid18={nid}"

        kwargs["headers"] = headers

        # 随机休眠
        sleep_time = random.uniform(1, 4)
        time.sleep(sleep_time)

        # 直连尝试，失败则走系统代理重试
        import urllib.request
        try:
            return original_request(self, method, url, **kwargs)
        except Exception as first_err:
            sys_proxies = urllib.request.getproxies()
            if sys_proxies:
                kwargs["proxies"] = {
                    "http": sys_proxies.get("http"),
                    "https": sys_proxies.get("https"),
                }
                time.sleep(random.uniform(1, 3))
                return original_request(self, method, url, **kwargs)
            raise

    # 全局替换
    requests.Session.request = patched_request
    _patch_sign.set_patch(True)
    logger.info("东方财富反爬补丁已启用（支持子域名）")


def is_patched() -> bool:
    """检查补丁是否已启用"""
    return _patch_sign.is_patched()
