import logging
import datetime
import os

def custom_timezone_converter(timestamp):
    """
    将时间戳转换为指定时区 (默认 UTC+8/Asia/Shanghai) 的 struct_time。
    时区通过环境变量 TZ_OFFSET (小时数) 配置。
    """
    
    # 尝试从环境变量获取偏移量，默认为 8 (北京时间)
    try:
        offset_hours = float(os.getenv('TZ_OFFSET', 8))
    except (ValueError, TypeError):
        offset_hours = 8

    # 创建时区对象
    target_timezone = datetime.timezone(datetime.timedelta(hours=offset_hours))
    
    # 转换时间
    dt_time = datetime.datetime.fromtimestamp(timestamp, target_timezone)
    return dt_time.timetuple()

def setup_logging(log_file, prefix=None, level=logging.INFO):
    """
    配置日志记录器，使其输出到文件和控制台。
    支持一个可选的前缀，用于标识日志来源。

    每次调用都会重新配置处理器，以适应多进程环境。
    
    时间显示默认为 UTC+8 (北京时间)，可通过环境变量 TZ_OFFSET 修改。

    :param log_file: 日志文件的路径。
    :param prefix: (可选) 要添加到每条日志消息开头的字符串前缀。
    :param level: 日志级别。
    """
    # 使用进程ID + 前缀作为 logger 名称，避免不同进程/实例的 logger 互相干扰
    logger_name = f'camoufox.{os.getpid()}'
    if prefix:
        logger_name += f'.{prefix}'
    
    logger = logging.getLogger(logger_name) 
    logger.setLevel(level)

    # 如果该 logger 已有 handlers，说明已初始化过，直接返回
    if logger.hasHandlers():
        return logger

    base_format = '%(asctime)s - %(process)d - %(levelname)s - %(message)s'

    if prefix:
        log_format = f'%(asctime)s - %(process)d - %(levelname)s - {prefix} - %(message)s'
    else:
        log_format = base_format

    fh = logging.FileHandler(log_file)
    fh.setLevel(level)

    ch = logging.StreamHandler()
    ch.setLevel(level)

    formatter = logging.Formatter(log_format)
    # 设置自定义的时间转换器
    formatter.converter = custom_timezone_converter

    fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)
    
    logger.propagate = False
    
    return logger