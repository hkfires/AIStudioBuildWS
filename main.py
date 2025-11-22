import os
import threading
import multiprocessing
import signal
import sys
import time

from browser.instance import run_browser_instance, shutdown_event as instance_shutdown_event
from utils.logger import setup_logging
from utils.paths import cookies_dir, logs_dir
from utils.cookie_manager import CookieManager
from utils.common import clean_env_value, ensure_dir

# 全局变量
app_running = False
flask_app = None


class ProcessManager:
    """进程管理器，负责跟踪和管理浏览器进程"""

    def __init__(self):
        self.processes = {}  # {process_id: process_info}
        self.lock = threading.Lock()
        self.logger = setup_logging(str(logs_dir() / 'app.log'), prefix="manager")

    def add_process(self, process, config=None):
        """添加进程到管理器"""
        with self.lock:
            pid = process.pid if process and hasattr(process, 'pid') else None

            # 允许添加PID为None的进程（可能还在启动中），但会记录这个情况
            if pid is None:
                # 使用临时ID作为key，等获得真实PID后再更新
                temp_id = f"temp_{len(self.processes)}"
                logger = setup_logging(str(logs_dir() / 'app.log'), prefix="manager")
                logger.warning(f"进程PID暂时为None，使用临时ID {temp_id}")
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

    def mark_dead(self, pid):
        """标记进程为已死亡"""
        with self.lock:
            if pid in self.processes:
                self.processes[pid]['is_alive'] = False

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
                        # 记录详细信息用于调试
                        if process:
                            self.logger.warning(f"进程 {pid} 已死亡")
                        else:
                            self.logger.warning(f"进程对象 {pid} 为None")
                except (ValueError, ProcessLookupError) as e:
                    # 进程已经不存在
                    dead_pids.append(pid)
                    self.logger.warning(f"进程 {pid} 检查时出错: {e}")

            # 清理死进程记录
            for pid in dead_pids:
                self.remove_process(pid)
                if dead_pids:
                    self.logger.info(f"清理死进程记录: {dead_pids}")

            return alive

    def terminate_all(self, timeout=10):
        """优雅地终止所有进程"""
        with self.lock:
            logger = setup_logging(str(logs_dir() / 'app.log'), prefix="signal")

            # 首先更新临时PID
            self.update_temp_pids()

            if not self.processes:
                logger.info("没有活跃的进程需要关闭")
                return

            logger.info(f"开始关闭 {len(self.processes)} 个进程...")

            # 第一阶段：发送SIGTERM信号
            active_pids = []
            for pid, info in list(self.processes.items()):
                process = info['process']
                try:
                    # 检查进程对象是否有效且进程存活
                    if process and hasattr(process, 'is_alive') and process.is_alive() and pid is not None:
                        logger.info(f"发送SIGTERM给进程 {pid} (运行时长: {time.time() - info['start_time']:.1f}秒)")
                        process.terminate()
                        active_pids.append(pid)
                    else:
                        logger.info(f"进程 {pid if pid is not None else 'None'} 已经停止或无效")
                except (ValueError, ProcessLookupError, AttributeError) as e:
                    logger.warning(f"进程 {pid if pid is not None else 'None'} 访问出错: {e}")

            if not active_pids:
                logger.info("所有进程已经停止")
                return

            # 第二阶段：等待进程退出
            logger.info(f"等待 {len(active_pids)} 个进程优雅退出...")
            for i in range(5):  # 最多等待5秒
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
                    logger.info("所有进程已优雅退出")
                    return
                logger.info(f"仍有 {len(still_alive)} 个进程在运行，等待中... ({i+1}/5)")
                time.sleep(1)

            # 第三阶段：强制杀死仍在运行的进程
            for pid in active_pids:
                if pid in self.processes and pid is not None:
                    process = self.processes[pid]['process']
                    try:
                        if process and hasattr(process, 'is_alive') and process.is_alive():
                            logger.warning(f"进程 {pid} 未响应SIGTERM，强制终止")
                            process.kill()
                    except (ValueError, ProcessLookupError, AttributeError) as e:
                        logger.info(f"进程 {pid} 已终止: {e}")

            logger.info("所有进程关闭完成")

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
    使用CookieManager解析环境变量和cookies目录，为每个cookie来源创建独立的浏览器实例配置。
    """
    # 1. 读取所有实例共享的URL
    shared_url = clean_env_value(os.getenv("CAMOUFOX_INSTANCE_URL"))
    if not shared_url:
        logger.error("错误: 缺少环境变量 CAMOUFOX_INSTANCE_URL。所有实例需要一个共享的目标URL。")
        return None, None

    # 2. 读取全局设置
    global_settings = {
        "headless": clean_env_value(os.getenv("CAMOUFOX_HEADLESS")) or "virtual",
        "url": shared_url  # 所有实例都使用这个URL
    }

    proxy_value = clean_env_value(os.getenv("CAMOUFOX_PROXY"))
    if proxy_value:
        global_settings["proxy"] = proxy_value

    # 3. 使用CookieManager检测所有cookie来源
    cookie_manager = CookieManager(logger)
    sources = cookie_manager.detect_all_sources()

    # 检查是否有任何cookie来源
    if not sources:
        logger.error("错误: 未找到任何cookie来源（既没有JSON文件，也没有环境变量cookie）。")
        return None, None

    # 4. 为每个cookie来源创建实例配置
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
            instances.append({
                "cookie_file": None,
                "env_cookie_index": int(env_index),
                "cookie_source": source
            })

    logger.info(f"将启动 {len(instances)} 个浏览器实例")

    return global_settings, instances

def start_browser_instances():
    """启动浏览器实例的核心逻辑"""
    global app_running, process_manager

    log_dir = logs_dir()
    logger = setup_logging(str(log_dir / 'app.log'))
    logger.info("---------------------Camoufox 实例管理器开始启动---------------------")

    global_settings, instance_profiles = load_instance_configurations(logger)
    if not instance_profiles:
        logger.error("错误: 环境变量中未找到任何实例配置。")
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

        process = multiprocessing.Process(target=run_browser_instance, args=(final_config,))
        process.start()
        # 等待一小段时间让进程获得PID，然后再添加到管理器
        time.sleep(0.1)
        process_manager.add_process(process, final_config)

        # 如果不是最后一个实例，等待30秒再启动下一个实例，避免并发启动导致的高CPU占用
        if i < len(instance_profiles):
            logger.info(f"等待 30 秒后启动下一个实例...")
            time.sleep(30)

    # 所有实例启动完成，开始监控进程
    logger.info("所有浏览器实例启动完成，开始监控进程状态...")

    # 等待所有进程
    try:
        while app_running:
            alive_processes = process_manager.get_alive_processes()
            logger.info(f"当前存活进程数: {len(alive_processes)}")

            if not alive_processes:
                logger.info("所有浏览器进程已结束，主进程即将退出")
                app_running = False  # 确保退出循环
                break

            # 等待进程并清理死进程
            for process in alive_processes:
                try:
                    process.join(timeout=1)
                except:
                    pass

            time.sleep(1)

        # 最终检查：确保退出
        logger.info("浏览器实例管理器运行结束")
        sys.exit(0)

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

    start_browser_instances()

    # 确保函数结束时退出
    logger.info("独立模式运行完成")
    sys.exit(0)

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
    browser_thread = threading.Thread(target=start_browser_instances, daemon=True)
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
    global app_running, process_manager

    # 立即设置日志，确保能看到后续信息
    logger = setup_logging(str(logs_dir() / 'app.log'), prefix="signal")
    logger.info(f"接收到信号 {signum}，开始处理...")

    # 检查是否是主进程，防止子进程执行关闭逻辑
    current_pid = os.getpid()

    # 使用一个简单的方法来判断：如果是子进程，通常没有全局变量 process_manager 的控制权
    try:
        # 检查当前进程是否在 process_manager 中管理的进程之一
        if hasattr(process_manager, 'processes') and current_pid in process_manager.processes:
            # 当前进程是被管理的子进程，不应该执行关闭逻辑
            logger.info(f"子进程 {current_pid} 接收到信号 {signum}，忽略主进程信号处理逻辑")
            return
    except:
        # 如果检查失败，假设是主进程继续执行
        pass

    logger.info(f"主进程 {current_pid} 接收到信号 {signum}，正在关闭应用...")

    # 立即设置全局标志，阻止新的进程创建
    app_running = False

    # 立即设置实例关闭事件
    try:
        instance_shutdown_event.set()
        logger.info("已设置实例关闭事件")
    except Exception as e:
        logger.error(f"设置实例关闭事件时发生错误: {e}")

    # 极速关闭进程，最大限度减少在信号处理器中的时间
    try:
        logger.info("极速关闭模式启动...")

        # 跳过详细的进程检查，直接发送信号
        if hasattr(process_manager, 'processes') and process_manager.processes:
            logger.info(f"发现 {len(process_manager.processes)} 个进程，发送关闭信号...")
            # 直接遍历，不加锁以避免阻塞
            for pid, info in list(process_manager.processes.items()):
                process = info.get('process')
                if process and hasattr(process, 'terminate'):
                    try:
                        process.terminate()
                    except:
                        pass  # 忽略错误，继续处理下一个
            logger.info("关闭信号已发送")
        else:
            logger.info("没有需要关闭的进程")

    except Exception as e:
        logger.error(f"极速关闭时发生错误: {e}")

    # 立即强制退出，不做任何等待
    logger.info("应用立即强制退出...")
    os._exit(0)

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
