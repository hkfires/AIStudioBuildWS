import os
import threading
import multiprocessing
import signal
import sys
import time

# 加载 .env 文件（仅在非 Docker 环境且文件存在时）
def load_env_file():
    """加载 .env 文件，不影响已存在的环境变量"""
    if os.environ.get("DOCKER_ENV") or os.path.exists("/.dockerenv"):
        return  # Docker 环境，跳过加载
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=False)  # override=False 不覆盖已有环境变量
    except ImportError:
        pass  # python-dotenv 未安装，跳过

load_env_file()

from browser.instance import run_browser_instance
from utils.logger import setup_logging
from utils.paths import cookies_dir, logs_dir
from utils.cookie_manager import CookieManager
from utils.common import clean_env_value, ensure_dir

# 全局变量
app_running = False
flask_app = None
# 使用 multiprocessing.Event 实现跨进程通信
shutdown_event = multiprocessing.Event()


class ProcessManager:
    """进程管理器，负责跟踪和管理浏览器进程"""

    def __init__(self):
        self.processes = {}  # {process_id: process_info}
        self.lock = threading.RLock()
        ensure_dir(logs_dir())
        self.logger = setup_logging(str(logs_dir() / 'app.log'), prefix="manager")

    def add_process(self, process, config=None):
        """添加进程到管理器"""
        with self.lock:
            pid = process.pid if process and hasattr(process, 'pid') else None

            # 允许添加PID为None的进程（可能还在启动中），但会记录这个情况
            if pid is None:
                # 使用临时ID作为key，等获得真实PID后再更新
                temp_id = f"temp_{len(self.processes)}"
                self.logger.warning(f"进程PID暂时为None，使用临时ID {temp_id}")
            else:
                temp_id = pid

            process_info = {
                'process': process,
                'config': config,
                'pid': pid,
                'is_alive': True,
                'start_time': time.time()
            }
            self.processes[temp_id] = process_info

    def update_temp_pids(self):
        """更新临时PID为真实PID"""
        with self.lock:
            temp_ids = [k for k in self.processes.keys() if isinstance(k, str) and k.startswith("temp_")]
            for temp_id in temp_ids:
                process_info = self.processes[temp_id]
                process = process_info['process']

                if process and hasattr(process, 'pid') and process.pid is not None:
                    # 更新为真实PID
                    self.processes[process.pid] = process_info
                    del self.processes[temp_id]
                    process_info['pid'] = process.pid

    def remove_process(self, pid):
        """从管理器中移除进程"""
        with self.lock:
            if pid in self.processes:
                del self.processes[pid]

    def get_alive_processes(self):
        """获取所有存活进程"""
        with self.lock:
            # 首先尝试更新临时PID
            self.update_temp_pids()

            alive = []
            dead_pids = []

            for pid, info in self.processes.items():
                process = info['process']
                try:
                    # 检查进程是否真实存在且是子进程
                    if process and hasattr(process, 'is_alive') and process.is_alive():
                        alive.append(process)
                    else:
                        dead_pids.append(pid)
                except (ValueError, ProcessLookupError) as e:
                    # 进程已经不存在
                    dead_pids.append(pid)
                    self.logger.warning(f"进程 {pid} 检查时出错: {e}")

            # 清理死进程记录
            for pid in dead_pids:
                self.remove_process(pid)

            return alive

    def terminate_all(self, timeout=10):
        """优雅地终止所有进程"""
        with self.lock:
            # logger = setup_logging(str(logs_dir() / 'app.log'), prefix="signal")
            # 直接使用 self.logger，避免重复 setup_logging

            # 首先更新临时PID
            self.update_temp_pids()

            if not self.processes:
                self.logger.info("没有活跃的进程需要关闭")
                return

            self.logger.info(f"开始关闭 {len(self.processes)} 个进程...")

            # 第一阶段：发送SIGTERM信号
            active_pids = []
            for pid, info in list(self.processes.items()):
                process = info['process']
                try:
                    # 检查进程对象是否有效且进程存活
                    if process and hasattr(process, 'is_alive') and process.is_alive() and pid is not None:
                        self.logger.info(f"发送SIGTERM给进程 {pid} (运行时长: {time.time() - info['start_time']:.1f}秒)")
                        process.terminate()
                        active_pids.append(pid)
                    else:
                        self.logger.info(f"进程 {pid if pid is not None else 'None'} 已经停止或无效")
                except (ValueError, ProcessLookupError, AttributeError) as e:
                    self.logger.warning(f"进程 {pid if pid is not None else 'None'} 访问出错: {e}")

            if not active_pids:
                self.logger.info("所有进程已经停止")
                return

            # 第二阶段：等待进程退出
            self.logger.info(f"等待 {len(active_pids)} 个进程优雅退出...")
            start_wait = time.time()
            while time.time() - start_wait < 5:  # 最多等待5秒
                still_alive = []
                for pid in active_pids:
                    if pid in self.processes:
                        process = self.processes[pid]['process']
                        try:
                            if process and hasattr(process, 'is_alive') and process.is_alive():
                                still_alive.append(pid)
                        except (ValueError, ProcessLookupError, AttributeError):
                                pass
                if not still_alive:
                    self.logger.info("所有进程已优雅退出")
                    return
                time.sleep(0.5)
            
            self.logger.info(f"仍有 {len(still_alive)} 个进程在运行，准备强制关闭...")

            # 第三阶段：强制杀死仍在运行的进程
            for pid in active_pids:
                if pid in self.processes and pid is not None:
                    process = self.processes[pid]['process']
                    try:
                        if process and hasattr(process, 'is_alive') and process.is_alive():
                            self.logger.warning(f"进程 {pid} 未响应SIGTERM，强制终止")
                            process.kill()
                    except (ValueError, ProcessLookupError, AttributeError) as e:
                        self.logger.info(f"进程 {pid} 已终止: {e}")

            self.logger.info("所有进程关闭完成")

    def get_count(self):
        """获取管理的进程总数"""
        with self.lock:
            return len(self.processes)

    def get_alive_count(self):
        """获取存活进程数"""
        return len(self.get_alive_processes())


# 全局进程管理器
process_manager = ProcessManager()


def load_instance_configurations(logger):
    """
    使用CookieManager解析环境变量和Cookies目录，为每个Cookie来源创建独立的浏览器实例配置。
    """
    # 1. 读取所有实例共享的URL
    shared_url = clean_env_value(os.getenv("CAMOUFOX_INSTANCE_URL"))
    if not shared_url:
        logger.error("错误: 缺少环境变量 CAMOUFOX_INSTANCE_URL。所有实例需要一个共享的目标URL")
        return None, None

    # 2. 读取全局设置
    global_settings = {
        "headless": clean_env_value(os.getenv("CAMOUFOX_HEADLESS")) or "virtual",
        "url": shared_url  # 所有实例都使用这个URL
    }

    proxy_value = clean_env_value(os.getenv("CAMOUFOX_PROXY"))
    if proxy_value:
        global_settings["proxy"] = proxy_value

    # 3. 使用CookieManager检测所有Cookie来源
    cookie_manager = CookieManager(logger)
    sources = cookie_manager.detect_all_sources()

    # 检查是否有任何Cookie来源
    if not sources:
        logger.error("错误: 未找到任何Cookie来源（既没有JSON文件，也没有环境变量Cookie）")
        return None, None

    # 4. 为每个Cookie来源创建实例配置
    instances = []
    for source in sources:
        if source.type == "file":
            instances.append({
                "cookie_file": source.identifier,
                "cookie_source": source
            })
        elif source.type == "env_var":
            # 从环境变量名中提取索引，如 "USER_COOKIE_1" -> 1
            env_index = source.identifier.split("_")[-1]
            try:
                env_cookie_index = int(env_index)
            except ValueError:
                logger.warning(f"警告: 无法解析 Cookie 环境变量索引，跳过来源: {source.identifier}")
                continue

            instance_config = {
                "cookie_file": None,
                "env_cookie_index": env_cookie_index,
                "cookie_source": source
            }

            # 支持每实例独立代理：CAMOUFOX_PROXY_N 对应 USER_COOKIE_N
            instance_proxy_var = f"CAMOUFOX_PROXY_{env_cookie_index}"
            instance_proxy_value = clean_env_value(os.getenv(instance_proxy_var))
            if instance_proxy_value:
                instance_config["proxy"] = instance_proxy_value
                logger.info(f"检测到 {source.display_name} 的独立代理配置: {instance_proxy_var}")

            instances.append(instance_config)

    logger.info(f"将启动 {len(instances)} 个浏览器实例")

    return global_settings, instances

def start_browser_instances(run_mode="standalone"):
    """启动浏览器实例的核心逻辑"""
    global app_running, process_manager, shutdown_event

    log_dir = logs_dir()
    logger = setup_logging(str(log_dir / 'app.log'))
    logger.info("---------------------Camoufox 实例管理器开始启动---------------------")
    start_delay = int(os.getenv("INSTANCE_START_DELAY", "30"))
    logger.info(f"运行模式: {run_mode}; 实例启动间隔: {start_delay} 秒")

    global_settings, instance_profiles = load_instance_configurations(logger)
    if not instance_profiles:
        logger.error("错误: 环境变量中未找到任何实例配置")
        return

    for i, profile in enumerate(instance_profiles, 1):
        if not app_running:
            break

        final_config = global_settings.copy()
        final_config.update(profile)

        if 'url' not in final_config:
            logger.warning(f"警告: 跳过一个无效的配置项 (缺少 url): {profile}")
            continue

        cookie_source = final_config.get('cookie_source')

        if cookie_source:
            if cookie_source.type == "file":
                logger.info(
                    f"正在启动第 {i}/{len(instance_profiles)} 个浏览器实例 (file: {cookie_source.display_name})..."
                )
            elif cookie_source.type == "env_var":
                logger.info(
                    f"正在启动第 {i}/{len(instance_profiles)} 个浏览器实例 (env: {cookie_source.display_name})..."
                )
        else:
            logger.error(f"错误: 配置中缺少cookie_source对象")
            continue

        # 传递 shutdown_event 给子进程
        process = multiprocessing.Process(target=run_browser_instance, args=(final_config, shutdown_event))
        process.start()
        # 等待一小段时间让进程获得PID，然后再添加到管理器
        time.sleep(0.1)
        process_manager.add_process(process, final_config)

        # 等待配置的时间，避免并发启动导致的高CPU占用
        # 即使是最后一个实例，也等待一段时间让其初始化，然后再进入主循环
        time.sleep(start_delay)

    # 等待所有进程
    previous_count = None
    last_log_time = 0
    try:
        while app_running:
            alive_processes = process_manager.get_alive_processes()
            current_count = len(alive_processes)

            # 仅在数量变化或间隔一段时间后再记录，避免过于频繁的日志
            now = time.time()
            if current_count != previous_count or now - last_log_time >= 600:
                logger.info(f"当前运行的浏览器实例数: {current_count}")
                previous_count = current_count
                last_log_time = now

            if not alive_processes:
                logger.info("所有浏览器进程已结束，主进程即将退出")
                break

            # 等待进程并清理死进程
            for process in alive_processes:
                try:
                    process.join(timeout=1)
                except:
                    pass

            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("捕获到键盘中断信号，等待信号处理器完成关闭...")
        # 不在这里关闭进程，让信号处理器统一处理
        pass

    # 确保在所有进程结束后退出
    logger.info("浏览器实例管理器运行结束")

def run_standalone_mode():
    """独立模式"""
    global app_running
    app_running = True

    start_browser_instances(run_mode="standalone")

def run_server_mode():
    """服务器模式"""
    global app_running, flask_app

    log_dir = logs_dir()
    server_logger = setup_logging(str(log_dir / 'app.log'), prefix="server")

    # 动态导入 Flask（只在需要时）
    try:
        from flask import Flask, jsonify
        flask_app = Flask(__name__)
    except ImportError:
        server_logger.error("错误: 服务器模式需要 Flask，请安装: pip install flask")
        return

    app_running = True

    # 在后台线程中启动浏览器实例
    browser_thread = threading.Thread(target=lambda: start_browser_instances(run_mode="server"), daemon=True)
    browser_thread.start()

    # 定义路由
    @flask_app.route('/health')
    def health_check():
        """健康检查端点"""
        global process_manager
        running_count = process_manager.get_alive_count()
        total_count = process_manager.get_count()
        return jsonify({
            'status': 'healthy',
            'browser_instances': total_count,
            'running_instances': running_count,
            'message': f'Application is running with {running_count} active browser instances'
        })

    @flask_app.route('/')
    def index():
        """主页端点"""
        global process_manager
        running_count = process_manager.get_alive_count()
        total_count = process_manager.get_count()
        return jsonify({
            'status': 'running',
            'browser_instances': total_count,
            'running_instances': running_count,
            'run_mode': 'server',
            'message': 'Camoufox Browser Automation is running in server mode'
        })

    # 禁用 Flask 的默认日志
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    # 启动 Flask 服务器
    try:
        flask_app.run(host='0.0.0.0', port=7860, debug=False)
    except KeyboardInterrupt:
        server_logger.info("服务器正在关闭...")

def signal_handler(signum, frame):
    """统一的信号处理器 - 只有主进程应该执行这个逻辑"""
    global app_running, process_manager, shutdown_event

    # 立即设置日志，确保能看到后续信息
    logger = setup_logging(str(logs_dir() / 'app.log'), prefix="signal")
    logger.info(f"接收到信号 {signum}，开始处理...")

    # 检查是否是主进程，防止子进程执行关闭逻辑
    current_pid = os.getpid()

    # 使用一个简单的方法来判断：如果是子进程，通常没有全局变量 process_manager 的控制权
    # 或者通过判断 multiprocessing.current_process().name
    if multiprocessing.current_process().name != 'MainProcess':
         # 子进程接收到信号，通常应该由主进程来管理，或者子进程会因为主进程发送的SIGTERM而终止
         # 这里我们选择忽略，让主进程通过terminate来管理，或者子进程通过shutdown_event来退出
         logger.info(f"子进程 {current_pid} 接收到信号 {signum}，忽略主进程信号处理逻辑")
         return

    logger.info(f"主进程 {current_pid} 接收到信号 {signum}，正在关闭应用...")

    # 1. 立即设置全局标志，阻止新的进程创建
    app_running = False

    # 2. 设置跨进程关闭事件，通知所有子进程优雅退出
    try:
        shutdown_event.set()
        logger.info("已设置全局关闭事件 (shutdown_event)")
    except Exception as e:
        logger.error(f"设置关闭事件时发生错误: {e}")

    # 3. 调用进程管理器的优雅终止方法
    try:
        process_manager.terminate_all(timeout=10)
    except Exception as e:
        logger.error(f"调用 terminate_all 时发生错误: {e}")

    logger.info("应用关闭流程结束，主进程退出")
    sys.exit(0)

def main():
    """主入口函数"""
    # 初始化必要的目录
    ensure_dir(logs_dir())
    ensure_dir(cookies_dir())

    # 注册信号处理器 - 添加更多信号的捕获
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    # 在某些环境中可能还有其他信号
    try:
        signal.signal(signal.SIGQUIT, signal_handler)
    except (ValueError, AttributeError):
        pass
    try:
        signal.signal(signal.SIGHUP, signal_handler)
    except (ValueError, AttributeError):
        pass

    # 检查运行模式环境变量
    hg_mode = os.getenv('HG', '').lower()

    if hg_mode == 'true':
        run_server_mode()
    else:
        run_standalone_mode()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
