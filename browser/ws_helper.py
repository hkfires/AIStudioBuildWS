import time
import random
from playwright.sync_api import Page, FrameLocator


class PageLocators:
    """
    缓存常用的 Locator 对象，避免每次保活循环都创建新对象导致内存泄漏。
    Playwright 的 Locator 是惰性的，可以安全复用。
    """
    
    def __init__(self, page: Page):
        self.page = page
        # 缓存常用的 locator
        self._modal = None
        self._iframe = None
        self._frame = None
        self._ws_status = None
        self._disconnect_btn = None
        self._connect_btn = None
    
    @property
    def modal(self):
        """interaction-modal 遮罩层"""
        if self._modal is None:
            self._modal = self.page.locator('div.interaction-modal')
        return self._modal
    
    @property
    def iframe(self):
        """Preview iframe 元素"""
        if self._iframe is None:
            self._iframe = self.page.locator('iframe[title="Preview"]')
        return self._iframe
    
    @property
    def frame(self):
        """Preview iframe 的 FrameLocator"""
        if self._frame is None:
            self._frame = self.page.frame_locator('iframe[title="Preview"]')
        return self._frame
    
    @property
    def ws_status(self):
        """WS 状态文本元素"""
        if self._ws_status is None:
            self._ws_status = self.frame.locator('text=/WS:\\s*(CONNECTED|IDLE|CONNECTING|RECONNECTING)/i').first
        return self._ws_status
    
    @property
    def disconnect_btn(self):
        """Disconnect 按钮"""
        if self._disconnect_btn is None:
            self._disconnect_btn = self.frame.locator('button:has-text("Disconnect")')
        return self._disconnect_btn
    
    @property
    def connect_btn(self):
        """Connect 按钮"""
        if self._connect_btn is None:
            self._connect_btn = self.frame.locator('button:has-text("Connect")')
        return self._connect_btn


def get_preview_frame(page: Page, logger=None) -> FrameLocator:
    """
    获取预览iframe的FrameLocator。
    """
    try:
        # 查找title为"Preview"的iframe
        frame = page.frame_locator('iframe[title="Preview"]')
        return frame
    except Exception as e:
        if logger:
            logger.warning(f"获取Preview iframe失败: {e}")
        return None


def get_ws_status(page: Page, logger=None, locators: PageLocators = None) -> str:
    """
    获取页面中WS连接状态（在iframe内部）。
    返回: CONNECTED, IDLE, CONNECTING, RECONNECTING 或 UNKNOWN
    
    Args:
        page: Playwright Page 对象
        logger: 日志记录器
        locators: 可选的 PageLocators 缓存对象，传入可避免重复创建 locator
    """
    try:
        if locators:
            status_element = locators.ws_status
        else:
            frame = get_preview_frame(page, logger)
            if not frame:
                return "UNKNOWN"
            status_element = frame.locator('text=/WS:\\s*(CONNECTED|IDLE|CONNECTING|RECONNECTING)/i').first
        
        if status_element.is_visible(timeout=3000):
            text = status_element.text_content()
            if text:
                if "RECONNECTING" in text.upper():
                    return "RECONNECTING"
                elif "CONNECTED" in text.upper():
                    return "CONNECTED"
                elif "IDLE" in text.upper():
                    return "IDLE"
                elif "CONNECTING" in text.upper():
                    return "CONNECTING"
        return "UNKNOWN"
    except Exception as e:
        if logger:
            logger.warning(f"获取WS状态时出错: {e}")
        return "UNKNOWN"


def click_disconnect(page: Page, logger=None, locators: PageLocators = None) -> bool:
    """
    点击Disconnect按钮断开WS连接（在iframe内部）。
    
    Args:
        page: Playwright Page 对象
        logger: 日志记录器
        locators: 可选的 PageLocators 缓存对象
    """
    try:
        if locators:
            disconnect_btn = locators.disconnect_btn
        else:
            frame = get_preview_frame(page, logger)
            if not frame:
                return False
            disconnect_btn = frame.locator('button:has-text("Disconnect")')
        
        if disconnect_btn.count() > 0 and disconnect_btn.first.is_visible(timeout=3000):
            disconnect_btn.first.click(timeout=5000)
            if logger:
                logger.info("已点击 Disconnect 按钮")
            time.sleep(1)
            return True
        if logger:
            logger.warning("未找到可见的 Disconnect 按钮")
        return False
    except Exception as e:
        if logger:
            logger.warning(f"点击 Disconnect 按钮失败: {e}")
        return False


def click_connect(page: Page, logger=None, locators: PageLocators = None) -> bool:
    """
    点击Connect按钮建立WS连接（在iframe内部）。
    
    Args:
        page: Playwright Page 对象
        logger: 日志记录器
        locators: 可选的 PageLocators 缓存对象
    """
    try:
        if locators:
            connect_btn = locators.connect_btn
        else:
            frame = get_preview_frame(page, logger)
            if not frame:
                return False
            connect_btn = frame.locator('button:has-text("Connect")')
        
        if connect_btn.count() > 0 and connect_btn.first.is_visible(timeout=3000):
            connect_btn.first.click(timeout=5000)
            if logger:
                logger.info("已点击 Connect 按钮")
            time.sleep(1)
            return True
        if logger:
            logger.warning("未找到可见的 Connect 按钮")
        return False
    except Exception as e:
        if logger:
            logger.warning(f"点击 Connect 按钮失败: {e}")
        return False


def wait_for_ws_connected(page: Page, logger=None, timeout: int = 30, locators: PageLocators = None) -> bool:
    """
    等待WS状态变为CONNECTED。
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        status = get_ws_status(page, logger, locators)
        if status == "CONNECTED":
            return True
        time.sleep(1)
    return False


def reconnect_ws(page: Page, logger=None, locators: PageLocators = None) -> str:
    """
    执行断开再连接的流程，并返回最终WS状态。
    流程：关闭遮罩 -> Disconnect -> 等待IDLE -> Connect -> 等待CONNECTED -> 获取状态
    
    Args:
        page: Playwright Page 对象
        logger: 日志记录器
        locators: 可选的 PageLocators 缓存对象
    """
    if logger:
        logger.info("开始执行WS重连流程: Disconnect -> Connect")
    
    # 先关闭 interaction-modal 遮罩层（如果存在）
    dismiss_interaction_modal(page, logger, locators)
    
    # 先断开连接
    click_disconnect(page, logger, locators)
    time.sleep(2)
    
    # 检查是否变为IDLE
    status = get_ws_status(page, logger, locators)
    if logger:
        logger.info(f"断开后WS状态: {status}")
    
    # 再连接
    click_connect(page, logger, locators)
    time.sleep(2)
    
    # 等待连接成功
    if wait_for_ws_connected(page, logger, timeout=15, locators=locators):
        status = get_ws_status(page, logger, locators)
        if logger:
            logger.info(f"重连后WS状态: {status}")
        return status
    else:
        status = get_ws_status(page, logger, locators)
        if logger:
            logger.warning(f"WS重连超时，当前状态: {status}")
        return status


def dismiss_interaction_modal(page: Page, logger=None, locators: PageLocators = None) -> bool:
    """
    检测并关闭 interaction-modal 遮罩层。
    通过在 iframe 区域内模拟鼠标移动来触发遮罩层关闭。
    
    Args:
        page: Playwright Page 对象
        logger: 日志记录器
        locators: 可选的 PageLocators 缓存对象
    
    返回: True 如果成功关闭遮罩，False 如果未找到遮罩或关闭失败
    """
    try:
        if locators:
            modal = locators.modal
            iframe = locators.iframe
        else:
            modal = page.locator('div.interaction-modal')
            iframe = page.locator('iframe[title="Preview"]')
        
        if modal.count() == 0 or not modal.first.is_visible(timeout=500):
            return False
        
        if logger:
            logger.info("检测到 interaction-modal 遮罩层，尝试关闭...")
        
        if iframe.count() > 0:
            iframe_box = iframe.first.bounding_box()
            if iframe_box:
                # 随机起点
                curr_x = iframe_box['x'] + random.randint(50, int(iframe_box['width']) - 50)
                curr_y = iframe_box['y'] + random.randint(50, int(iframe_box['height']) - 50)
                
                # 持续连续移动直到遮罩关闭，最多尝试30次
                for i in range(30):
                    # 从当前位置随机移动一段距离
                    delta_x = random.randint(-30, 30)
                    delta_y = random.randint(-20, 20)
                    curr_x = max(iframe_box['x'] + 20, min(iframe_box['x'] + iframe_box['width'] - 20, curr_x + delta_x))
                    curr_y = max(iframe_box['y'] + 20, min(iframe_box['y'] + iframe_box['height'] - 20, curr_y + delta_y))
                    
                    page.mouse.move(curr_x, curr_y)
                    time.sleep(0.05)
                    
                    # 每次移动后检查遮罩是否关闭
                    if modal.count() == 0 or not modal.first.is_visible(timeout=100):
                        if logger:
                            logger.info("已成功关闭 interaction-modal 遮罩层")
                        return True
        
        return False
    except Exception as e:
        if logger:
            logger.debug(f"关闭 interaction-modal 时出错: {e}")
        return False


def click_in_iframe(page: Page, logger=None, locators: PageLocators = None) -> bool:
    """
    在 iframe 内随机移动鼠标并点击一次，用于保活。
    避开顶部（状态栏和按钮区域）和右侧区域。
    
    Args:
        page: Playwright Page 对象
        logger: 日志记录器
        locators: 可选的 PageLocators 缓存对象
    
    返回: True 如果成功点击，False 如果失败
    """
    try:
        if locators:
            iframe = locators.iframe
        else:
            iframe = page.locator('iframe[title="Preview"]')
        
        if iframe.count() == 0:
            return False
        
        iframe_box = iframe.first.bounding_box()
        if not iframe_box:
            return False
        
        # 安全区域：避开顶部80像素（状态栏+按钮）和右侧200像素（按钮区域）
        safe_left = iframe_box['x'] + 50
        safe_right = iframe_box['x'] + iframe_box['width'] - 200
        safe_top = iframe_box['y'] + 80
        safe_bottom = iframe_box['y'] + iframe_box['height'] - 50
        
        # 确保安全区域有效
        if safe_right <= safe_left or safe_bottom <= safe_top:
            return False
        
        # 随机起点（在安全区域内）
        curr_x = random.randint(int(safe_left), int(safe_right))
        curr_y = random.randint(int(safe_top), int(safe_bottom))
        
        # 随机移动几步（保持在安全区域内）
        for _ in range(random.randint(3, 6)):
            delta_x = random.randint(-30, 30)
            delta_y = random.randint(-20, 20)
            curr_x = max(int(safe_left), min(int(safe_right), curr_x + delta_x))
            curr_y = max(int(safe_top), min(int(safe_bottom), curr_y + delta_y))
            page.mouse.move(curr_x, curr_y)
            time.sleep(0.05)
        
        # 点击当前位置
        page.mouse.click(curr_x, curr_y)
        return True
    except Exception as e:
        if logger:
            logger.debug(f"在 iframe 内点击失败: {e}")
        return False
