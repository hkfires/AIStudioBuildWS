import time
import os
import threading
from playwright.sync_api import Page, expect
from utils.paths import logs_dir
from utils.common import ensure_dir

def handle_untrusted_dialog(page: Page, logger=None):
    """
    检查并处理 "Last modified by..." 的弹窗。
    如果弹窗出现，则点击 "OK" 按钮。
    """
    ok_button_locator = page.get_by_role("button", name="OK")

    try:
        if ok_button_locator.is_visible(timeout=10000): # 等待最多10秒
            logger.info(f"检测到弹窗，正在点击 'OK' 按钮...")
            
            ok_button_locator.click(force=True)
            logger.info(f"'OK' 按钮已点击。")
            expect(ok_button_locator).to_be_hidden(timeout=1000)
            logger.info(f"弹窗已确认关闭。")
        else:
            logger.info(f"在10秒内未检测到弹窗，继续执行...")
    except Exception as e:
        logger.info(f"检查弹窗时发生意外：{e}，将继续执行...")

def handle_successful_navigation(page: Page, logger, cookie_file_config, shutdown_event=None, cookie_validator=None):
    """
    在成功导航到目标页面后，执行后续操作（处理弹窗、保持运行）。
    """
    logger.info("已成功到达目标页面。")
    page.click('body') # 给予页面焦点

    # 检查并处理 "Last modified by..." 的弹窗
    handle_untrusted_dialog(page, logger=logger)

    logger.info("实例将保持运行状态。每10秒点击一次页面以保持活动。")

    # 等待页面加载和渲染
    time.sleep(15)

    # 添加Cookie验证计数器
    click_counter = 0

    while True:
        # 检查是否收到关闭信号
        if shutdown_event and shutdown_event.is_set():
            logger.info("收到关闭信号，正在优雅退出保持活动循环...")
            # 停止Cookie验证器
            if cookie_validator:
                cookie_validator.stop_validation()
            break

        try:
            page.click('body')
            click_counter += 1

            # 每10次点击（约2分钟）执行一次完整的Cookie验证
            if cookie_validator and click_counter >= 10:  # 10 * 10秒 = 100秒 ≈ 2分钟
                is_valid = cookie_validator.validate_cookies_in_main_thread()

                if not is_valid:
                    cookie_validator.shutdown_instance_on_cookie_failure()
                    return

                click_counter = 0  # 重置计数器

            # 使用可中断的睡眠，每秒检查一次关闭信号
            for _ in range(10):  # 10秒 = 10次1秒检查
                if shutdown_event and shutdown_event.is_set():
                    logger.info("收到关闭信号，正在优雅退出保持活动循环...")
                    # 停止Cookie验证器
                    if cookie_validator:
                        cookie_validator.stop_validation()
                    return
                time.sleep(1)

        except Exception as e:
            logger.error(f"在保持活动循环中出错: {e}")
            # 停止Cookie验证器
            if cookie_validator:
                cookie_validator.stop_validation()
            # 在保持活动循环中出错时截屏
            try:
                screenshot_dir = logs_dir()
                ensure_dir(screenshot_dir)
                screenshot_filename = os.path.join(screenshot_dir, f"FAIL_keep_alive_error_{cookie_file_config}.png")
                page.screenshot(path=screenshot_filename, full_page=True)
                logger.info(f"已在保持活动循环出错时截屏: {screenshot_filename}")
            except Exception as screenshot_e:
                logger.error(f"在保持活动循环出错时截屏失败: {screenshot_e}")
            break # 如果页面关闭或出错，则退出循环
