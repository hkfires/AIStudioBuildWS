def convert_cookie_editor_to_playwright(cookies_from_editor, logger=None):
    """
    将从 Cookie-Editor 插件导出的 Cookie 列表转换为 Playwright 兼容的格式。
    """
    playwright_cookies = []

    for cookie in cookies_from_editor:
        pw_cookie = {}
        for key in ['name', 'value', 'domain', 'path', 'httpOnly', 'secure']:
            if key in cookie:
                pw_cookie[key] = cookie[key]
        if cookie.get('session', False):
            pw_cookie['expires'] = -1
        elif 'expirationDate' in cookie:
            if cookie['expirationDate'] is not None:
                pw_cookie['expires'] = int(cookie['expirationDate'])
            else:
                pw_cookie['expires'] = -1
        elif 'expires' in cookie:
            pw_cookie['expires'] = float(cookie['expires']) if cookie['expires'] is not None else -1

        if 'sameSite' in cookie and cookie['sameSite'] is not None:
            same_site_value = str(cookie['sameSite']).lower()
            if same_site_value in ('no_restriction', 'none'):
                pw_cookie['sameSite'] = 'None'
            elif same_site_value in ['lax', 'strict']:
                pw_cookie['sameSite'] = same_site_value.capitalize()
            elif same_site_value == 'unspecified':
                pw_cookie['sameSite'] = 'Lax'

        if all(key in pw_cookie for key in ['name', 'value', 'domain', 'path']):
            playwright_cookies.append(pw_cookie)
        else:
            if logger:
                logger.warning(f"跳过一个格式不完整的 Cookie: {cookie}")

    return playwright_cookies


def convert_kv_to_playwright(kv_string, default_domain=".google.com", logger=None):
    """
    将键值对格式的 Cookie 字符串转换为 Playwright 兼容的格式。

    Args:
        kv_string (str): 包含 Cookie 的键值对字符串，格式为 "name1=value1; name2=value2; ..."
        default_domain (str): 默认域名，默认为".google.com"
        logger: 日志记录器

    Returns:
        list: Playwright 兼容的 Cookie 列表
    """
    playwright_cookies = []
    if logger:
        logger.warning(
            "KV Cookie 字符串不包含原始 domain/path/SameSite 属性；"
            "iframe Preview 认证失败时，请改用已登录浏览器导出的 JSON Cookie 数组以保留这些属性"
        )

    # 按分号分割 Cookie
    cookie_pairs = kv_string.split(';')

    for pair in cookie_pairs:
        pair = pair.strip()  # 去除首尾空白字符

        if not pair:  # 跳过空字符串
            continue

        # 跳过无效的 Cookie（不包含等号）
        if '=' not in pair:
            if logger:
                logger.warning(f"跳过无效的 Cookie 格式: '{pair}'")
            continue

        # 分割name和value
        name, value = pair.split('=', 1)  # 只分割第一个等号
        name = name.strip()
        value = value.strip()

        if not name:  # 跳过空名称
            if logger:
                logger.warning(f"跳过空名称的 Cookie: '{pair}'")
            continue

        # 构造 Playwright 格式的 Cookie
        pw_cookie = {
            'name': name,
            'value': value,
            'domain': default_domain,
            'path': '/',
            'expires': -1,  # 默认为会话 Cookie
            'httpOnly': False,  # KV 格式无法确定 httpOnly 状态，默认为 False
            'secure': True,     # 假设为安全 Cookie
            'sameSite': 'Lax'   # 默认 SameSite 策略
        }

        playwright_cookies.append(pw_cookie)

        if logger:
            logger.debug(f"成功转换 Cookie: {name} -> domain={default_domain}")

    return playwright_cookies


def auto_convert_to_playwright(cookie_data, default_domain=".google.com", logger=None):
    """
    自动识别 Cookie 数据格式并转换为 Playwright 兼容格式。
    支持两种输入格式:
    1. JSON 数组 (Cookie-Editor 导出格式)
    2. KV 字符串 (键值对格式: "name1=value1; name2=value2; ...")

    Args:
        cookie_data: Cookie 数据，可以是 list (JSON格式) 或 str (KV格式)
        default_domain (str): KV格式使用的默认域名，默认为".google.com"
        logger: 日志记录器

    Returns:
        list: Playwright 兼容的 Cookie 列表

    Raises:
        ValueError: 当格式无法识别时抛出异常
    """
    # 格式1: JSON 数组格式 (Cookie-Editor 导出格式)
    if isinstance(cookie_data, list):
        if logger:
            logger.debug(f"检测到 JSON 数组格式的 Cookie 数据，共 {len(cookie_data)} 个条目")
        return convert_cookie_editor_to_playwright(cookie_data, logger=logger)

    # 格式2: KV 字符串格式
    if isinstance(cookie_data, str):
        # 去除首尾空白字符
        cookie_str = cookie_data.strip()

        if not cookie_str:
            if logger:
                logger.warning("收到空的 Cookie 字符串")
            return []

        if logger:
            logger.debug(f"检测到 KV 字符串格式的 Cookie 数据")

        return convert_kv_to_playwright(
            cookie_str,
            default_domain=default_domain,
            logger=logger
        )

    # 无法识别的格式
    error_msg = f"无法识别的 Cookie 数据格式: {type(cookie_data).__name__}"
    if logger:
        logger.error(error_msg)
    raise ValueError(error_msg)
