import time
import os
import sys
import threading
from playwright.sync_api import Page, TimeoutError, Error as PlaywrightError
from utils.logger import setup_logging


class CookieValidator:
    """
    Cookie验证器，负责定期验证Cookie的有效性
    """

    def __init__(self, page, context, target_url, logger, instance_label):
        """
        初始化Cookie验证器

        Args:
            page: 主页面实例
            context: 浏览器上下文
            target_url: 目标URL
            logger: 日志记录器
            instance_label: 实例标签
        """
        self.page = page
        self.context = context
        self.target_url = target_url
        self.logger = logger
        self.instance_label = instance_label
        self.shutdown_event = threading.Event()
        self.diagnostic_tag = instance_label.replace(os.sep, "_")

    
    def validate_cookies_in_main_thread(self):
        """
        在主线程中执行Cookie验证（由主线程调用）

        Returns:
            bool: Cookie是否有效
        """
        validation_page = None
        try:
            # 创建新标签页（在主线程中执行）
            self.logger.info("开始Cookie验证...")
            validation_page = self.context.new_page()

            # 访问验证URL
            validation_url = "https://aistudio.google.com/apps"
            response = validation_page.goto(validation_url, wait_until='domcontentloaded', timeout=30000)

            # 等待页面加载
            validation_page.wait_for_timeout(2000)

            # 获取最终URL
            final_url = validation_page.url

            # 检查是否被重定向到登录页面
            if "accounts.google.com/v3/signin/identifier" in final_url:
                self.logger.error("Cookie验证失败: 被重定向到登录页面")
                return False

            if "accounts.google.com/v3/signin/accountchooser" in final_url:
                self.logger.error("Cookie验证失败: 被重定向到账户选择页面")
                return False

            # 如果没有跳转到登录页面，就算成功
            self.logger.info("Cookie验证成功")
            return True

        except TimeoutError:
            self.logger.error("Cookie验证失败: 页面加载超时")
            return False

        except PlaywrightError as e:
            self.logger.error(f"Cookie验证失败: {e}")
            return False

        except Exception as e:
            self.logger.error(f"Cookie验证失败: {e}")
            return False

        finally:
            # 关闭验证标签页
            if validation_page:
                try:
                    validation_page.close()
                except Exception:
                    pass  # 忽略关闭错误

    
    
    
    def start_validation(self):
        """
        启动Cookie验证（现在在主线程中执行）
        """
        self.logger.info("Cookie验证将在主线程中执行，无需启动独立线程")
        # 验证现在由主线程的navigation.py处理

    def stop_validation(self):
        """
        停止Cookie验证（现在在主线程中执行）
        """
        self.shutdown_event.set()

    def shutdown_instance_on_cookie_failure(self):
        """
        因Cookie失效而关闭实例
        """
        self.logger.error("Cookie失效，关闭实例")
        self.shutdown_event.set()
        time.sleep(1)
        sys.exit(1)