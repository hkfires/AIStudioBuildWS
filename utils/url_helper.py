"""
URL处理辅助函数

提供URL解析和路径提取功能，用于导航验证中的域名无关匹配。
"""

from urllib.parse import urlparse


def extract_url_path(url: str) -> str:
    """
    提取URL的路径和查询参数部分，忽略协议和域名差异

    用于验证导航是否到达正确页面，允许域名重定向。

    Args:
        url: 完整URL字符串

    Returns:
        路径+查询参数+片段（例如："/apps/drive/123?param=value#section"）
        如果URL为空或无效，返回空字符串

    Examples:
        >>> extract_url_path("https://ai.studio/apps/drive/123?param=value")
        '/apps/drive/123?param=value'

        >>> extract_url_path("https://aistudio.google.com/apps/drive/123")
        '/apps/drive/123'

        >>> extract_url_path("https://example.com/path")
        '/path'
    """
    if not url:
        return ""

    try:
        parsed = urlparse(url)
        result = parsed.path
        if parsed.query:
            result += '?' + parsed.query
        if parsed.fragment:
            result += '#' + parsed.fragment
        return result
    except Exception:
        # 如果URL格式无效，返回空字符串
        return ""
