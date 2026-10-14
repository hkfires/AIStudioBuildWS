import time
import random
import re
from playwright.sync_api import Page, FrameLocator


_WS_STATUS_PATTERN = re.compile(
    r"\bWS:\s*(CONNECTED|IDLE|CONNECTING|RECONNECTING|DISCONNECTED|ERROR)\b",
    re.IGNORECASE,
)
_WS_ACTION_TIMEOUT_MS = 5000

def get_context(page: Page, logger=None):
    """
    获取元素查找上下文。如果存在 iframe[title="Preview"]，则返回其 FrameLocator，
    否则直接返回 page。这样兼容本地直连和 HuggingFace Spaces 嵌套。
    """
    try:
        if page.locator('iframe[title="Preview"]').count() > 0:
            return page.frame_locator('iframe[title="Preview"]')
    except Exception as e:
        if logger:
            logger.debug(f"获取上下文出错: {e}")
    return page

def get_ws_status(page: Page, logger=None) -> str:
    """
    获取页面中WS连接状态。
    返回完整的 WS 状态；没有可见状态元素时返回 UNKNOWN。
    """
    try:
        context = get_context(page, logger)
        
        status_elements = context.get_by_text(_WS_STATUS_PATTERN)
        for index in range(status_elements.count()):
            status_element = status_elements.nth(index)
            if not status_element.is_visible():
                continue
            match = _WS_STATUS_PATTERN.search(status_element.text_content() or "")
            if match:
                return match.group(1).upper()
        return "UNKNOWN"
    except Exception as e:
        if logger:
            logger.warning(f"获取WS状态时出错: {e}")
        return "UNKNOWN"

def _click_ws_action(page: Page, action: str, logger=None, timeout_ms=None) -> bool:
    """只点击准确命名的 WS 按钮，不绕过 disabled 或遮挡检测。"""
    try:
        context = get_context(page, logger)
        names = re.compile(rf"^{action}(?: WebSocket Proxy)?$", re.IGNORECASE)
        buttons = context.get_by_role("button", name=names)
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if not button.is_visible():
                continue
            button.click(timeout=_WS_ACTION_TIMEOUT_MS if timeout_ms is None else timeout_ms)
            if logger:
                logger.info(f"已点击 {action} 按钮")
            return True
        if logger:
            logger.warning(f"未找到可见的 {action} 按钮")
        return False
    except Exception as e:
        if logger:
            logger.warning(f"点击 {action} 按钮失败: {e}")
        return False

def click_disconnect(page: Page, logger=None) -> bool:
    return _click_ws_action(page, "Disconnect", logger)

def click_connect(page: Page, logger=None, timeout_ms=None) -> bool:
    return _click_ws_action(page, "Connect", logger, timeout_ms)

def wait_for_ws_connected(page: Page, logger=None, timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = get_ws_status(page, logger)
        if status == "CONNECTED":
            return True
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    return False

def reconnect_ws(page: Page, logger=None) -> str:
    if logger:
        logger.info("开始执行WS重连流程...")

    dismiss_interaction_modal(page, logger)

    current_status = get_ws_status(page, logger)
    if logger:
        logger.info(f"重连前WS状态: {current_status}")

    if current_status in ("UNKNOWN", "CONNECTING", "RECONNECTING"):
        if logger:
            logger.info(f"检测到过渡态 {current_status}，等待3秒观察是否自行恢复...")
        time.sleep(3)
        current_status = get_ws_status(page, logger)
        if logger:
            logger.info(f"等待后WS状态: {current_status}")
        if current_status == "CONNECTED":
            if logger:
                logger.info("WS状态已自行恢复为CONNECTED，跳过重连")
            return current_status

    if current_status == "IDLE":
        if logger:
            logger.info("当前已是IDLE状态，跳过Disconnect，直接Connect...")
        click_connect(page, logger)
        time.sleep(2)
    else:
        disconnected = click_disconnect(page, logger)
        if not disconnected:
            if current_status == "CONNECTED":
                if logger:
                    logger.info("Disconnect按钮不存在但状态为CONNECTED，视为误报，跳过重连")
                return current_status
            if logger:
                logger.info("Disconnect按钮不存在，直接尝试Connect...")
        else:
            time.sleep(2)

        status = get_ws_status(page, logger)
        if logger:
            logger.info(f"断开后WS状态: {status}")
        click_connect(page, logger)
        time.sleep(2)

    if wait_for_ws_connected(page, logger, timeout=15):
        status = get_ws_status(page, logger)
        if logger:
            logger.info(f"WS重连成功，当前状态: {status}")
        return status
    else:
        status = get_ws_status(page, logger)
        if logger:
            logger.warning(f"WS重连超时，当前状态: {status}")
        return status

def dismiss_interaction_modal(page: Page, logger=None) -> bool:
    try:
        # 首先尝试通用的弹窗清理，这是导致偏移和遮挡的主要原因
        try:
            from browser.navigation import handle_popup_dialog
            handle_popup_dialog(page, logger=logger)
        except Exception:
            pass

        modal = page.locator('div.interaction-modal')
        if modal.count() == 0 or not modal.first.is_visible(timeout=500):
            return False
        
        if logger:
            logger.info("检测到 interaction-modal 遮罩层，尝试关闭...")
        
        iframe = page.locator('iframe[title="Preview"]')
        if iframe.count() > 0:
            iframe_box = iframe.first.bounding_box()
            if iframe_box:
                curr_x = iframe_box['x'] + random.randint(50, int(iframe_box['width']) - 50)
                curr_y = iframe_box['y'] + random.randint(50, int(iframe_box['height']) - 50)

                for i in range(30):
                    delta_x = random.randint(-30, 30)
                    delta_y = random.randint(-20, 20)
                    curr_x = max(iframe_box['x'] + 20, min(iframe_box['x'] + iframe_box['width'] - 20, curr_x + delta_x))
                    curr_y = max(iframe_box['y'] + 20, min(iframe_box['y'] + iframe_box['height'] - 20, curr_y + delta_y))

                    page.bring_to_front()
                    page.mouse.move(curr_x, curr_y)
                    time.sleep(0.05)

                    if modal.count() == 0 or not modal.first.is_visible(timeout=100):
                        if logger:
                            logger.info("已成功关闭 interaction-modal 遮罩层")
                        return True
        
        return False
    except Exception as e:
        if logger:
            logger.debug(f"关闭 interaction-modal 时出错: {e}")
        return False

def click_in_iframe(page: Page, logger=None) -> bool:
    try:
        if logger:
            logger.debug("开始执行 click_in_iframe 保活检测...")

        # 移动之前，再次尝试清理所有弹窗 (兜底)
        try:
            from browser.navigation import handle_popup_dialog
            handle_popup_dialog(page, logger=logger)
        except Exception:
            pass

        # ================= 调整：父页面保活 (不点击，仅移动/按键) =================
        # 点击 iframe 外部会导致 iframe 失去焦点，从而可能引发 websocket 1001 断开错误。
        # 因此这里只移动鼠标、发送无害按键和滚动来保活父页面。
        try:
            page.bring_to_front()
            viewport = page.viewport_size
            if viewport:
                move_x = random.randint(10, int(viewport['width']) - 10)
                move_y = random.randint(10, int(viewport['height']) - 10)
                page.mouse.move(move_x, move_y)
                # 滚动鼠标滚轮
                page.mouse.wheel(0, random.choice([-100, 100]))
                # 发送无害按键作为活跃信号
                page.keyboard.press("Shift")

                if logger:
                    logger.debug(f"已在父页面移动鼠标并触发按键保活，避免点击导致 1001 错误")
        except Exception as e:
            if logger:
                logger.debug(f"父页面保活操作失败: {e}")
        # =======================================================

        iframe = page.locator('iframe[title="Preview"]')
        
        if iframe.count() == 0:
            # 没有 iframe，本地直连模式
            viewport = page.viewport_size
            if not viewport:
                return False
            safe_left = 50
            safe_right = viewport['width'] - 200
            safe_top = 120
            safe_bottom = viewport['height'] - 50
        else:
            iframe_box = iframe.first.bounding_box()
            if not iframe_box:
                if logger:
                    logger.warning("click_in_iframe: 失败，iframe 存在但 bounding_box 返回 None")
                return False
            safe_left = iframe_box['x'] + 50
            safe_right = iframe_box['x'] + iframe_box['width'] - 200
            safe_top = iframe_box['y'] + 120
            safe_bottom = iframe_box['y'] + iframe_box['height'] - 50

        if safe_right <= safe_left or safe_bottom <= safe_top:
            if logger:
                logger.warning(f"click_in_iframe: 安全区域计算无效 (left:{safe_left}, right:{safe_right}, top:{safe_top}, bottom:{safe_bottom})")
            return False

        curr_x = random.randint(int(safe_left), int(safe_right))
        curr_y = random.randint(int(safe_top), int(safe_bottom))

        for _ in range(random.randint(2, 4)):
            delta_x = random.randint(-30, 30)
            delta_y = random.randint(-20, 20)
            curr_x = max(int(safe_left), min(int(safe_right), curr_x + delta_x))
            curr_y = max(int(safe_top), min(int(safe_bottom), curr_y + delta_y))

            page.bring_to_front()
            page.mouse.move(curr_x, curr_y)
            time.sleep(0.1)

        page.bring_to_front()
        page.mouse.click(curr_x, curr_y)
        if logger:
            logger.debug("click_in_iframe: 成功完成鼠标移动和点击")
        return True
    except Exception as e:
        if logger:
            logger.error(f"在页面/iframe内点击失败: {e}")
        return False
