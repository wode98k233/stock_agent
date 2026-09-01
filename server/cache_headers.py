"""静态资源缓存头策略。"""

STATIC_HTML_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
STATIC_ASSET_CACHE_CONTROL = "private, max-age=600"
STATIC_VERSIONED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


def static_cache_control_for_path(path: str, query_string: bytes | str = b"") -> str:
    """根据路径和查询参数返回合适的 Cache-Control 值。"""
    clean_path = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if clean_path in ("", "index.html") or clean_path.endswith(".html"):
        return STATIC_HTML_CACHE_CONTROL
    if isinstance(query_string, str):
        query_bytes = query_string.encode("utf-8")
    else:
        query_bytes = query_string or b""
    if any(part.split(b"=", 1)[0] == b"v" for part in query_bytes.split(b"&") if part):
        return STATIC_VERSIONED_ASSET_CACHE_CONTROL
    return STATIC_ASSET_CACHE_CONTROL
