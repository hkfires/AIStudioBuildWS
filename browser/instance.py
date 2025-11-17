import os
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils.logger import setup_logging
from utils.cookie_manager import CookieManager
from browser.navigation import handle_successful_navigation
from camoufox.sync_api import Camoufox
from utils.paths import logs_dir
from utils.common import parse_headless_mode, ensure_dir
from utils.url_helper import extract_url_path


def run_browser_instance(config):
    """
    根据最终合并的配置，启动并管理一个单独的 Camoufox 浏览器实例。
    使用CookieManager统一管理cookie加载，避免重复的扫描逻辑。
    """
    cookie_source = config.get('cookie_source')
    if not cookie_source:
        # 使用默认logger进行错误报告
        logger = setup_logging(os.path.join(logs_dir(), 'app.log'))
        logger.error("错误: 配置中缺少cookie_source对象")
        return

    instance_label = cookie_source.display_name
    logger = setup_logging(
        os.path.join(logs_dir(), 'app.log'), prefix=instance_label
    )
    diagnostic_tag = instance_label.replace(os.sep, "_")

    expected_url = config.get('url')
    proxy = config.get('proxy')
    headless_setting = config.get('headless', 'virtual')

    # 使用CookieManager加载cookie
    cookie_manager = CookieManager(logger)
    all_cookies = []

    try:
        # 直接使用CookieSource对象加载cookie
        cookies = cookie_manager.load_cookies(cookie_source)
        all_cookies.extend(cookies)

    except Exception as e:
        logger.error(f"从cookie来源加载时出错: {e}")
        return

    # 3. 检查是否有任何cookie可用
    if not all_cookies:
        logger.error("错误: 没有可用的cookie（既没有有效的JSON文件，也没有环境变量）")
        return

    cookies = all_cookies

    headless_mode = parse_headless_mode(headless_setting)
    launch_options = {"headless": headless_mode}
    if proxy:
        logger.info(f"使用代理: {proxy} 访问")
        launch_options["proxy"] = {"server": proxy, "bypass": "localhost, 127.0.0.1"}
    # 无需禁用图片加载, 因为图片很少, 禁用还可能导致风控增加
    # launch_options["block_images"] = True
    
    screenshot_dir = logs_dir()
    ensure_dir(screenshot_dir)

    try:
        with Camoufox(**launch_options) as browser:
            context = browser.new_context()
            context.add_cookies(cookies)
            page = context.new_page()
            
            # ####################################################################
            # ############ 增强的 page.goto() 错误处理和日志记录 ###############
            # ####################################################################
            
            response = None
            try:
                logger.info(f"正在导航到: {expected_url} (超时设置为 120 秒)")
                # page.goto() 会返回一个 response 对象，我们可以用它来获取状态码等信息
                response = page.goto(expected_url, wait_until='domcontentloaded', timeout=120000)
                
                # 检查HTTP响应状态码
                if response:
                    logger.info(f"导航初步成功，服务器响应状态码: {response.status} {response.status_text}")
                    if not response.ok: # response.ok 检查状态码是否在 200-299 范围内
                        logger.warning(f"警告：页面加载成功，但HTTP状态码表示错误: {response.status}")
                        # 即使状态码错误，也保存快照以供分析
                        page.screenshot(path=os.path.join(screenshot_dir, f"WARN_http_status_{response.status}_{diagnostic_tag}.png"))
                else:
                    # 对于非http/https的导航（如 about:blank），response可能为None
                    logger.warning("page.goto 未返回响应对象，可能是一个非HTTP导航。")

            except TimeoutError:
                # 这是最常见的错误：超时
                logger.error(f"导航到 {expected_url} 超时 (超过120秒)。")
                logger.error("可能原因：网络连接缓慢、目标网站服务器无响应、代理问题、或页面资源被阻塞。")
                # 尝试保存诊断信息
                try:
                    # 截图对于看到页面卡在什么状态非常有帮助（例如，空白页、加载中、Chrome错误页）
                    screenshot_path = os.path.join(screenshot_dir, f"FAIL_timeout_{diagnostic_tag}.png")
                    page.screenshot(path=screenshot_path, full_page=True)
                    logger.info(f"已截取超时时的屏幕快照: {screenshot_path}")
                    
                    # 保存HTML可以帮助分析DOM结构，即使在无头模式下也很有用
                    html_path = os.path.join(screenshot_dir, f"FAIL_timeout_{diagnostic_tag}.html")
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(page.content())
                    logger.info(f"已保存超时时的页面HTML: {html_path}")
                except Exception as diag_e:
                    logger.error(f"在尝试进行超时诊断（截图/保存HTML）时发生额外错误: {diag_e}")
                return # 超时后，后续操作无意义，直接终止

            except PlaywrightError as e:
                # 捕获其他Playwright相关的网络错误，例如DNS解析失败、连接被拒绝等
                error_message = str(e)
                logger.error(f"导航到 {expected_url} 时发生 Playwright 网络错误。")
                logger.error(f"错误详情: {error_message}")
                
                # Playwright的错误信息通常很具体，例如 "net::ERR_CONNECTION_REFUSED"
                if "net::ERR_NAME_NOT_RESOLVED" in error_message:
                    logger.error("排查建议：检查DNS设置或域名是否正确。")
                elif "net::ERR_CONNECTION_REFUSED" in error_message:
                    logger.error("排查建议：目标服务器可能已关闭，或代理/防火墙阻止了连接。")
                elif "net::ERR_INTERNET_DISCONNECTED" in error_message:
                    logger.error("排查建议：检查本机的网络连接。")
                
                # 同样，尝试截图，尽管此时页面可能完全无法访问
                try:
                    screenshot_path = os.path.join(screenshot_dir, f"FAIL_network_error_{diagnostic_tag}.png")
                    page.screenshot(path=screenshot_path)
                    logger.info(f"已截取网络错误时的屏幕快照: {screenshot_path}")
                except Exception as diag_e:
                    logger.error(f"在尝试进行网络错误诊断（截图）时发生额外错误: {diag_e}")
                return # 网络错误，终止

            # --- 如果导航没有抛出异常，继续执行后续逻辑 ---
            
            logger.info("页面初步加载完成，正在检查并处理初始弹窗...")
            page.wait_for_timeout(2000)
            
            final_url = page.url
            logger.info(f"导航完成。最终URL为: {final_url}")

            # ... 你原有的URL检查逻辑保持不变 ...
            if "accounts.google.com/v3/signin/identifier" in final_url:
                logger.error("检测到Google登录页面（需要输入邮箱）。Cookie已完全失效。")
                page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_identifier_page_{diagnostic_tag}.png"))
                return

            # 提取路径部分进行匹配（允许域名重定向）
            expected_path = extract_url_path(expected_url).split('?')[0]
            final_path = extract_url_path(final_url)

            if expected_path and expected_path in final_path:
                logger.info(f"URL验证通过。预期路径: {expected_path}, 最终URL: {final_url}")

                # --- NEW ROBUST STRATEGY: Wait for the loading spinner to disappear ---
                # This is the key to solving the race condition. The error message or
                # content will only appear AFTER the initial loading is done.
                spinner_locator = page.locator('mat-spinner')
                try:
                    logger.info("正在等待加载指示器 (spinner) 消失... (最长等待30秒)")
                    # We wait for the spinner to be 'hidden' or not present in the DOM.
                    spinner_locator.wait_for(state='hidden', timeout=30000)
                    logger.info("加载指示器已消失。页面已完成异步加载。")
                except TimeoutError:
                    logger.error("页面加载指示器在30秒内未消失。页面可能已卡住。")
                    page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_spinner_stuck_{diagnostic_tag}.png"))
                    return # Exit if the page is stuck loading

                # --- NOW, we can safely check for the error message ---
                # We use the most specific text possible to avoid false positives.
                auth_error_text = "authentication error"
                auth_error_locator = page.get_by_text(auth_error_text, exact=False)

                # We only need a very short timeout here because the page should be stable.
                if auth_error_locator.is_visible(timeout=2000):
                    logger.error(f"检测到认证失败的错误横幅: '{auth_error_text}'. Cookie已过期或无效。")
                    screenshot_path = os.path.join(screenshot_dir, f"FAIL_auth_error_banner_{diagnostic_tag}.png")
                    page.screenshot(path=screenshot_path)
                    
                    # html_path = os.path.join(screenshot_dir, f"FAIL_auth_error_banner_{diagnostic_tag}.html")
                    # with open(html_path, 'w', encoding='utf-8') as f:
                    #     f.write(page.content())
                    # logger.info(f"已保存包含错误信息的页面HTML: {html_path}")
                    return # Definitive failure, so we exit.

                # --- If no error, proceed to final confirmation (as a fallback) ---
                logger.info("未检测到认证错误横幅。进行最终确认。")
                login_button_cn = page.get_by_role('button', name='登录')
                login_button_en = page.get_by_role('button', name='Login')
                
                if login_button_cn.is_visible(timeout=1000) or login_button_en.is_visible(timeout=1000):
                    logger.error("页面上仍显示'登录'按钮。Cookie无效。")
                    page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_login_button_visible_{diagnostic_tag}.png"))
                    return

                # --- If all checks pass, we assume success ---
                logger.info("所有验证通过，确认已成功登录。")
                handle_successful_navigation(page, logger, diagnostic_tag)
            elif "accounts.google.com/v3/signin/accountchooser" in final_url:
                logger.warning("检测到Google账户选择页面。登录失败或Cookie已过期。")
                page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_chooser_click_failed_{diagnostic_tag}.png"))
                return
            else:
                logger.error(f"导航到了意外的URL。")
                logger.error(f"  预期路径: {expected_path}")
                logger.error(f"  最终URL: {final_url}")
                logger.error(f"  最终路径: {final_path}")
                page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_unexpected_url_{diagnostic_tag}.png"))
                return

    except KeyboardInterrupt:
        logger.info(f"用户中断，正在关闭...")
    except Exception as e:
        # 这是一个最终的捕获，用于捕获所有未预料到的错误
        logger.exception(f"运行 Camoufox 实例时发生未预料的严重错误: {e}")
