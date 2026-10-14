import time
import os
from datetime import datetime, timezone, timedelta
from playwright.sync_api import Page
from utils.paths import logs_dir
from utils.common import ensure_dir
from browser.ws_helper import reconnect_ws, get_ws_status, dismiss_interaction_modal, click_in_iframe
from browser.dialogs import dismiss_popups, has_visible_dialog, wait_for_app_ready, AppReadinessError, PreviewAuthenticationError

class KeepAliveError(Exception):
    pass


def _daily_restart_due(last_restart_ts: float, stagger_key: str = "") -> bool:
    """
    检查是否到达每日定时重启时间（用于回收长跑浏览器的内存）。

    时间由环境变量 DAILY_RESTART_TIME 配置（默认 "03:33"），
    时区沿用日志的 TZ_OFFSET（默认 UTC+8）。
    为避免多账户同一秒集中重启导致 CPU 峰值，按实例名做确定性抖动，
    在配置时间后 0~10 分钟内错开。
    """
    restart_at = os.getenv("DAILY_RESTART_TIME", "03:33")
    try:
        hh, mm = (int(x) for x in restart_at.split(":"))
    except (ValueError, TypeError):
        hh, mm = 3, 33

    try:
        offset = float(os.getenv("TZ_OFFSET", 8))
    except (ValueError, TypeError):
        offset = 8
    tz = timezone(timedelta(hours=offset))

    # 每个实例确定性错开 0~600 秒，避免多账户同时重启
    # 用 crc32 而非 hash()：hash() 受 PYTHONHASHSEED 影响，多进程下每次启动结果不同
    if stagger_key:
        import zlib
        jitter = zlib.crc32(stagger_key.encode()) % 600
    else:
        jitter = 0

    now = datetime.now(tz)
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(seconds=jitter)
    return now >= slot and last_restart_ts < slot.timestamp()

def handle_popup_dialog(page: Page, logger=None):
    """启动和保活使用同一套弹窗处理，避免 UI 更新后行为不一致。"""
    return dismiss_popups(page, logger)

def handle_successful_navigation(page: Page, logger, cookie_file_config, shutdown_event=None, cookie_validator=None, expected_path=None, expected_url=None, auth_failure_count=None, auth_failure_threshold=3):
    """
    在成功导航到目标页面后，执行后续操作（处理弹窗、保持运行）。
    """
    # 在截图前再次验证，处理初始化后异步出现的 onboarding。
    try:
        last_ws_status = wait_for_app_ready(page, logger)
    except PreviewAuthenticationError:
        try:
            screenshot_dir = logs_dir()
            ensure_dir(screenshot_dir)
            page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_preview_auth_{cookie_file_config}.png"))
        except Exception as error:
            logger.warning(f"保存 Preview 认证失败截图时出错: {error}")
        raise
    except AppReadinessError as error:
        raise KeepAliveError(str(error)) from error
    logger.info("AI Studio 弹窗已清理，Preview 已加载且 WS 已连接")

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

    logger.info(f"初始WS状态: {last_ws_status}")
    next_reconnect_at = 0

    # 每日定时重启基线：本次会话的启动时间
    last_restart_ts = time.time()

    # 添加Cookie验证计数器
    click_counter = 0

    while True:
        # 检查是否收到关闭信号
        if shutdown_event and shutdown_event.is_set():
            logger.info("收到关闭信号，正在优雅退出保持活动循环...")
            break

        # 每日定时内存回收：到达配置时间（默认 UTC+8 03:33）后主动重启实例，
        # 由外层 KeepAliveError 循环重建浏览器，释放长跑累积的内存
        if _daily_restart_due(last_restart_ts, cookie_file_config):
            logger.info(
                f"到达每日重启时间 ({os.getenv('DAILY_RESTART_TIME', '03:33')} UTC+{os.getenv('TZ_OFFSET', '8')})，"
                f"主动重启实例以回收内存"
            )
            raise KeepAliveError("每日定时内存回收重启")

        # API 认证连续失败自愈：页面登录态正常但后端会话已断（如多账户级联重启
        # 打断 token 建立），表现为持续性 401/403，重启浏览器实例重建会话
        if auth_failure_count is not None and auth_failure_count[0] >= auth_failure_threshold:
            logger.error(
                f"API 连续认证失败已达阈值 ({auth_failure_count[0]}/{auth_failure_threshold})，"
                f"重启浏览器实例重建会话"
            )
            raise KeepAliveError("API 连续认证失败，重建会话")

        try:
            # 强制页面唤醒以防由于遮挡被引擎休眠 (Occlusion sleep)
            page.bring_to_front()

            # 【URL守护】：检查是否偏离了目标页面（如误触导航到了Terms页等）
            if expected_path:
                current_url = page.url
                if expected_path not in current_url:
                    logger.warning(f"检测到页面偏离！当前URL: {current_url}，预期路径: {expected_path}")
                    logger.info("尝试导航回目标页面...")
                    try:
                        # 基于当前域名重建目标URL
                        parsed = current_url.split('/')
                        target_url = f"{parsed[0]}//{parsed[2]}/{expected_path.lstrip('/')}"
                        logger.info(f"导航到: {target_url}")
                        page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
                        time.sleep(3)
                        handle_popup_dialog(page, logger=logger)
                    except Exception as nav_e:
                        logger.warning(f"URL守护导航失败: {nav_e}")

            # 检测并关闭interaction-modal遮罩层（如果出现）
            dismiss_interaction_modal(page, logger)

            # 遇到尚未识别的弹窗时，避免随机点击其内容。
            blocked = has_visible_dialog(page)
            if blocked:
                logger.warning("AI Studio 仍有阻挡弹窗，暂缓 Preview 点击")
            else:
                click_in_iframe(page, logger)
            click_counter += 1

            # 检查WS状态是否发生变化
            current_ws_status = get_ws_status(page, logger)
            if current_ws_status != last_ws_status:
                logger.warning(f"WS状态变更: {last_ws_status} -> {current_ws_status}")
            # 持续 UNKNOWN/IDLE 同样需要重试，不能只在状态变化时恢复。
            if not blocked and current_ws_status != "CONNECTED" and time.monotonic() >= next_reconnect_at:
                logger.info("WS未连接，尝试重连...")
                current_ws_status = reconnect_ws(page, logger)
                next_reconnect_at = time.monotonic() + 30
            last_ws_status = current_ws_status

            # 每360次点击（1小时）执行一次完整的Cookie验证
            if cookie_validator and click_counter >= 360:  # 360 * 10秒 = 3600秒 = 1小时
                is_valid = cookie_validator.validate_cookies_in_main_thread()

                if not is_valid:
                    # Cookie确实失效（被重定向到登录页），抛异常让外层重启实例
                    logger.error("Cookie验证失败: 被重定向到Google登录页，Cookie已失效")
                    raise KeepAliveError("Cookie失效，需要重启浏览器实例")

                # 安全网：每小时顺带确认窗口还在自己的槽位（防止运行中漂移/
                # 被 fluxbox 重新吸附导致纵向重叠），place_window 是幂等的，
                # 已在槽位则直接返回，开销极小。
                try:
                    from browser.window_placer import place_window
                    place_window(cookie_file_config, logger, attempts=2, interval=1)
                except Exception:
                    pass

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
