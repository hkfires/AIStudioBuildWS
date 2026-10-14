import time
from playwright.sync_api import TimeoutError, Error as PlaywrightError


class CookieValidator:
    """Cookie验证器，负责定期验证Cookie的有效性。"""

    def __init__(self, page, context, logger):
        """
        初始化Cookie验证器

        Args:
            page: 主页面实例
            context: 浏览器上下文
            logger: 日志记录器
        """
        self.page = page
        self.context = context
        self.logger = logger

    def _try_validate(self):
        """单次尝试验证Cookie。返回 (success, is_auth_failure)。"""
        validation_page = None
        try:
            validation_page = self.context.new_page()
            validation_url = "https://aistudio.google.com/apps"
            validation_page.goto(validation_url, wait_until='domcontentloaded', timeout=30000)
            validation_page.wait_for_timeout(2000)
            final_url = validation_page.url

            # 检查是否被重定向到登录页面 — 这是真正的Cookie失效
            if "accounts.google.com/v3/signin/identifier" in final_url:
                self.logger.error("Cookie验证失败: 被重定向到登录页面 (identifier)")
                return False, True
            if "accounts.google.com/v3/signin/accountchooser" in final_url:
                self.logger.error("Cookie验证失败: 被重定向到账户选择页面 (accountchooser)")
                return False, True

            self.logger.info("Cookie验证成功")
            return True, False

        except TimeoutError:
            self.logger.warning("Cookie验证: 页面加载超时 (网络问题，非Cookie失效)")
            return False, False
        except PlaywrightError as e:
            self.logger.warning(f"Cookie验证: 网络错误 - {e}")
            return False, False
        except Exception as e:
            self.logger.error(f"Cookie验证: 未知错误 - {e}")
            return False, False
        finally:
            if validation_page:
                try:
                    validation_page.close()
                except Exception:
                    pass

    def validate_cookies_in_main_thread(self, max_retries=3, retry_delay=10):
        """
        在主线程中执行Cookie验证，带重试机制。
        网络超时不等于Cookie失效，只有被重定向到Google登录页才算真正失效。

        Args:
            max_retries: 网络超时时的最大重试次数
            retry_delay: 重试间隔(秒)

        Returns:
            bool: Cookie是否有效

        Raises:
            SystemExit: 当所有重试均因网络超时而失败时，强制退出当前子进程，
                        防止带着失效Cookie无限重试导致内存泄漏。
        """
        for attempt in range(1, max_retries + 1):
            self.logger.info(f"开始Cookie验证... (第 {attempt}/{max_retries} 次)")
            success, is_auth_failure = self._try_validate()

            if success:
                return True

            if is_auth_failure:
                # 真的被重定向到登录页了 → Cookie确实失效
                return False

            # 网络超时/错误 → 重试
            if attempt < max_retries:
                self.logger.warning(f"Cookie验证网络超时，{retry_delay}秒后重试 ({attempt}/{max_retries})")
                time.sleep(retry_delay)
            else:
                # ====== 终极修复第二步：废除危险的"跳过验证假设有效"逻辑 ======
                # 旧逻辑已废弃：曾经会 return True 导致带着失效Cookie无限重试，现改为强制退出
                # 新代码：强制退出当前子进程，阻断雪崩式内存泄漏
                self.logger.error(
                    f"Cookie验证: {max_retries}次尝试均网络超时，"
                    f"判定为环境或Cookie异常，终止实例以防内存泄漏！"
                )
                raise SystemExit(1)
                # ====================================================

        return True
