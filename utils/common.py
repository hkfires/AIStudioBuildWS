"""
通用工具函数
提供项目中常用的基础功能
"""

import os
from pathlib import Path

def clean_env_value(value):
    """
    清理环境变量值，去除首尾空白字符

    Args:
        value: 环境变量的原始值

    Returns:
        str or None: 清理后的值，如果为空或None则返回None
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_headless_mode(headless_setting):
    """
    解析headless模式配置

    Args:
        headless_setting: headless配置值

    Returns:
        bool or str: True表示headless，False表示有界面，'virtual'表示虚拟模式
    """
    if str(headless_setting).lower() == 'true':
        return True
    elif str(headless_setting).lower() == 'false':
        return False
    else:
        return 'virtual'


def ensure_dir(path):
    """
    确保目录存在，如果不存在则创建

    Args:
        path: 目录路径（可以是字符串或Path对象）
    """
    if isinstance(path, str):
        path = Path(path)
    os.makedirs(path, exist_ok=True)


def mask_url(url):
    """
    对URL进行脱敏处理，隐藏敏感的ID和参数

    Args:
        url (str): 原始URL

    Returns:
        str: 脱敏后的URL
    """
    if not url:
        return url

    # 提取域名部分
    if "://" not in url:
        return url

    # 分割URL
    parts = url.split('://')
    if len(parts) != 2:
        return url

    protocol = parts[0]
    rest = parts[1]

    # 分割域名和路径
    if '/' not in rest:
        return url

    domain = rest.split('/')[0]
    path = '/' + '/'.join(rest.split('/')[1:]) if len(rest.split('/')) > 1 else ''

    # 处理常见域名的敏感路径
    if 'aistudio.google.com' in domain or 'ai.studio' in domain:
        # 对应用ID进行脱敏
        if '/apps/drive/' in path:
            path_parts = path.split('/apps/drive/')
            if len(path_parts) > 1:
                app_id = path_parts[1].split('?')[0]  # 移除查询参数
                # 保留前3个字符，后面用星号替换
                if len(app_id) > 3:
                    masked_id = app_id[:3] + '*' * (len(app_id) - 3)
                    path = f'/apps/drive/{masked_id}'
                else:
                    path = f'/apps/drive/***'

        # 对其他敏感路径进行脱敏
        elif '/apps/' in path:
            path_parts = path.split('/apps/')
            if len(path_parts) > 1:
                rest_path = path_parts[1].split('?')[0]  # 移除查询参数
                if '/' in rest_path:
                    # 有子路径的情况
                    sub_parts = rest_path.split('/')
                    masked_parts = []
                    for part in sub_parts:
                        if len(part) > 6:
                            masked_parts.append(part[:3] + '*' * (len(part) - 3))
                        elif len(part) > 0:
                            masked_parts.append('*' * len(part))
                    path = f'/apps/' + '/'.join(masked_parts)
                else:
                    # 单个路径段
                    if len(rest_path) > 6:
                        path = f'/apps/' + rest_path[:3] + '*' * (len(rest_path) - 3)
                    else:
                        path = '/apps/***'

    # 对其他域名的通用处理
    else:
        # 对查询参数进行脱敏
        if '?' in path:
            path_parts = path.split('?')
            if len(path_parts) > 1:
                base_path = path_parts[0]
                query = path_parts[1]
                # 对查询参数值进行脱敏
                if '=' in query:
                    params = query.split('&')
                    masked_params = []
                    for param in params:
                        if '=' in param:
                            key, value = param.split('=', 1)
                            if len(value) > 3:
                                masked_value = value[:2] + '*' * (len(value) - 2)
                            else:
                                masked_value = '*' * len(value)
                            masked_params.append(f"{key}={masked_value}")
                        else:
                            masked_params.append(param)
                    path = base_path + '?' + '&'.join(masked_params)

    return f"{protocol}://{domain}{path}"