import time
import os
from playwright.sync_api import Page, expect
from utils.paths import logs_dir
from utils.common import ensure_dir
from browser.ws_helper import reconnect_ws, get_ws_status, dismiss_interaction_modal, click_in_iframe, PageLocators

class KeepAliveError(Exception):
    pass

def handle_popup_dialog(page: Page, logger=None):
    """
    检查并处理弹窗。
    交替点击 Got it 和 Continue to the app 按钮直到没有弹窗。
    """
    logger.info("开始处理弹窗...")
    
    # 定义需要查找的按钮列表
    button_names = ["Got it", "Continue to the app"]
    max_iterations = 10  # 最多尝试10轮，防止死循环
    total_clicks = 0
    
    try:
        for iteration in range(max_iterations):
            clicked_in_round = False
            
            # 等待页面稳定
            time.sleep(1)
            
            # 每轮交替尝试点击所有按钮
            for button_name in button_names:
                try:
                    button_locator = page.locator(f'button:visible:has-text("{button_name}")')
                    if button_locator.count() > 0 and button_locator.first.is_visible():
                        # logger.info(f"检测到弹窗: 点击 '{button_name}'")
                        button_locator.first.click(force=True, timeout=2000)
                        total_clicks += 1
                        clicked_in_round = True
                        time.sleep(1)
                except:
                    pass
            
            if not clicked_in_round:
                break
        
        if total_clicks > 0:
            logger.info(f"弹窗处理完成, 共点击 {total_clicks} 次")
        else:
            logger.info("未检测到弹窗")
    except Exception as e:
        logger.info(f"检查弹窗时发生意外：{e}，将继续执行...")

def handle_successful_navigation(page: Page, logger, cookie_file_config, shutdown_event=None, cookie_validator=None):
    """
    在成功导航到目标页面后，执行后续操作（处理弹窗、保持运行）。
    """
    logger.info("已成功到达目标页面")
    page.click('body') # 给予页面焦点

    # 检查并处理弹窗
    handle_popup_dialog(page, logger=logger)

    # 保存登录成功截图
    try:
        from datetime import datetime
        screenshot_dir = logs_dir()
        ensure_dir(screenshot_dir)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = os.path.join(screenshot_dir, f"SUCCESS_{cookie_file_config}_{timestamp}.png")
        page.screenshot(path=screenshot_path)
        logger.info(f"已保存登录成功截图: {screenshot_path}")
    except Exception as e:
        logger.warning(f"保存截图失败: {e}")

    if cookie_validator:
        logger.info("Cookie验证器已创建，将定期验证Cookie有效性")

    logger.info("实例将保持运行状态。每10秒点击一次页面以保持活动")

    # 等待页面加载和渲染
    time.sleep(15)

    # 创建 PageLocators 缓存对象，复用 locator 避免内存泄漏
    locators = PageLocators(page)

    # 记录初始WS状态
    last_ws_status = get_ws_status(page, logger, locators)
    logger.info(f"初始WS状态: {last_ws_status}")

    # 添加Cookie验证计数器
    click_counter = 0

    while True:
        # 检查是否收到关闭信号
        if shutdown_event and shutdown_event.is_set():
            logger.info("收到关闭信号，正在优雅退出保持活动循环...")
            break

        try:
            # 检测并关闭interaction-modal遮罩层（如果出现）
            dismiss_interaction_modal(page, logger, locators)

            # 在iframe内随机移动并点击保活
            click_in_iframe(page, logger, locators)
            click_counter += 1

            # 检查WS状态是否发生变化
            current_ws_status = get_ws_status(page, logger, locators)
            if current_ws_status != last_ws_status:
                logger.warning(f"WS状态变更: {last_ws_status} -> {current_ws_status}")

                # 如果不是CONNECTED状态，尝试重连
                if current_ws_status != "CONNECTED":
                    logger.info("WS断开，尝试重连...")
                    reconnect_ws(page, logger, locators)
                    current_ws_status = get_ws_status(page, logger, locators)
                
                last_ws_status = current_ws_status

            # 每360次点击（1小时）执行一次完整的Cookie验证
            if cookie_validator and click_counter >= 360:  # 360 * 10秒 = 3600秒 = 1小时
                is_valid = cookie_validator.validate_cookies_in_main_thread()

                if not is_valid:
                    cookie_validator.shutdown_instance_on_cookie_failure()
                    return

                click_counter = 0  # 重置计数器

            # 使用可中断的睡眠，每秒检查一次关闭信号
            for _ in range(10):  # 10秒 = 10次1秒检查
                if shutdown_event and shutdown_event.is_set():
                    logger.info("收到关闭信号，正在优雅退出保持活动循环...")
                    return
                time.sleep(1)

        except Exception as e:
            logger.error(f"在保持活动循环中出错: {e}")
            # 在保持活动循环中出错时截屏
            try:
                screenshot_dir = logs_dir()
                ensure_dir(screenshot_dir)
                screenshot_filename = os.path.join(screenshot_dir, f"FAIL_keep_alive_error_{cookie_file_config}.png")
                page.screenshot(path=screenshot_filename, full_page=True)
                logger.info(f"已在保持活动循环出错时截屏: {screenshot_filename}")
            except Exception as screenshot_e:
                logger.error(f"在保持活动循环出错时截屏失败: {screenshot_e}")
            raise KeepAliveError(f"在保持活动循环时出错: {e}")
