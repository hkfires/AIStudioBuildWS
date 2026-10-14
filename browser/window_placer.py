import os
import re
import subprocess
import time
import zlib

# Xvfb 虚拟屏幕尺寸（与 start.sh 中 Xvfb 的 -screen 0 1920x1080x24 对应）
_SCREEN_W = 1920
_SCREEN_H = 1080

# 浏览器外窗实际尺寸（含标题栏）。
# 注意：指纹里自报的 1440x900 只是伪装值，Camoufox 真实外窗约 1280x772，
# 从运行中的容器 wmctrl -l -G 实测得到。布局按真实尺寸算。
_WIN_W = 1280
_WIN_H = 772

# 匹配浏览器主窗口标题的片段（Camoufox 标题后缀是 "— Camoufox"，
# 普通 Firefox 是 "— Mozilla Firefox"，两者都兼容）。
# 空标题（页面还没加载出来的瞬间）也接受——反正归属靠 PID cmdline 精确匹配，
# 标题只是用来排除明显的非浏览器窗口，重启早期标题可能是空的。
_TITLE_HINTS = ("— Camoufox", "— Mozilla Firefox")

# 一次最多摆放的实例槽位数（超过则回绕，一般不会超过）
_MAX_SLOTS = 10


def _cascade_step() -> tuple:
    """
    相邻槽位的错开步长。fluxbox 原生 cascade 步长是标题栏高（约 28px），
    这里用 CASCADE_SCALE（默认 1.5，可用环境变量调）放大，
    并限制在"任何窗口都不会被 100% 盖死"的安全范围内。
    """
    try:
        scale = float(os.getenv("CASCADE_SCALE", "1.5"))
        if scale <= 0:
            raise ValueError
    except (ValueError, TypeError):
        scale = 1.5
    titlebar = 28
    margin_x, margin_y = 90, 45  # 必须露出的页边像素
    step_x = max(1, min(int(titlebar * scale), _SCREEN_W - _WIN_W - margin_x))
    step_y = max(1, min(int(titlebar * scale), _SCREEN_H - _WIN_H - margin_y))
    return step_x, step_y


def slot_for(source_name: str) -> int:
    """
    从实例名（USER_COOKIE_12 / xxx.json）提取末尾序号作为布局槽位（0 起）。
    没有序号的来源按名字 crc32 映射。槽位即对角线第几个位置，与启动顺序无关，
    每个实例每次启动/重启都回到同一个固定坐标，保证任意两窗口 x、y 都错开。
    """
    m = re.search(r'(\d+)', source_name)
    if m:
        return (int(m.group(1)) - 1) % _MAX_SLOTS
    return zlib.crc32(source_name.encode()) % _MAX_SLOTS


# 布局整体下压量：wmctrl 读回的 y 含标题栏，但发命令时 y 要减去标题栏高做
# 补偿，槽位 0 会变成负数、被 fluxbox 顶到屏幕外（标题栏超出桌面顶部）。
# 把所有槽位整体下移一个标题栏高，保证最上面窗口的外框也完整落在桌面内。
_TOP_INSET = 39


def _slot_position(slot: int, step_x: int, step_y: int,
                   win_w: int, win_h: int) -> tuple:
    """
    槽位 -> 屏幕坐标。沿对角线铺开 (slot*step_x, _TOP_INSET + slot*step_y)，
    到达右/下边界后折回并留页边，保证任何槽位都不会被其它槽位 100% 盖死。
    """
    max_x = max(_SCREEN_W - win_w, 0)
    max_y = max(_SCREEN_H - win_h, 0)
    x = slot * step_x
    y = _TOP_INSET + slot * step_y
    if x > max_x:
        x = max_x - (x - max_x) % 60
    if y > max_y:
        y = max_y - (y - max_y) % 60
    return max(0, x), max(_TOP_INSET, y)


def _run(cmd, timeout=10):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def _pid_matches_profile(pid: str, source_name: str) -> bool:
    """
    判断窗口所属进程是不是本实例：读 /proc/<pid>/cmdline，
    匹配 camoufox 启动参数里的 -profile .../<source_name>。
    路径结尾加边界匹配，避免 USER_COOKIE_1 误配 USER_COOKIE_10。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
        return re.search(
            r'-profile\s+\S*camoufox_profiles/' + re.escape(source_name) + r'(?:\s|$)',
            cmdline,
        ) is not None
    except Exception:
        return False


def _find_own_window(source_name: str):
    """
    在顶层窗口里找到属于本实例的浏览器主窗口 ID。
    归属判断靠 PID 的 cmdline 匹配 profile 路径（所有 Camoufox 实例标题相同，
    靠标题无法区分；Playwright 的 persistent context 也不暴露 browser PID）。
    标题只做宽松过滤：重启早期页面没加载时标题可能是空/辅助名，不能因此漏配。
    """
    out = _run(["wmctrl", "-l", "-p"])
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        win_id, _desktop, pid, _host, title = parts
        # 归属是第一位的：只要 PID 属于本实例的 profile，就是我们要的窗口
        if _pid_matches_profile(pid, source_name):
            return win_id
    return None


def _window_geometry(win_id: str):
    """返回 (x, y, w, h)，找不到返回 None。"""
    out = _run(["wmctrl", "-l", "-G"])
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) >= 8 and parts[0] == win_id:
            return int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
    return None


def place_window(source_name: str, logger, win_w=_WIN_W, win_h=_WIN_H,
                 attempts=45, interval=2):
    """
    把当前实例的浏览器窗口摆到它自己的确定性槽位，保证和其它实例错开。

    为什么必须显式摆正，而不能靠 fluxbox 的 CascadePlacement：
    persistent profile 的 xulstore.json 让 Firefox 复活窗口时带
    _NET_CURRENT_DESKTOP 标记，fluxbox 对这类窗口跳过摆放逻辑——于是重启后
    窗口会被 fluxbox 的边框吸附吸到同一行（实测全部落在 y=107，只往右错开、
    上下不动），多个实例叠在同一水平带，缺乏纵向冗余。而 Camoufox 把
    window.screenX/screenY 指纹锁成常量，真实位置只能我们在 WM 层显式管理。

    之前尝试过"先 xdotool 命中测试判断是否被盖死、只在盖死时才挪"，但
    xdotool 取屏幕栈顶窗口不可靠，会漏判/误判。所以改为无条件归位：
    反正目标坐标是本实例的确定性槽位，摆一次和摆多次结果一样（幂等）。

    策略：找到本实例窗口（按 PID cmdline 匹配 profile 路径）-> 计算槽位
    坐标 -> wmctrl 移动 -> 读回几何确认到位（fluxbox 有边框吸附，容差放宽）。
    """
    slot = slot_for(source_name)
    step_x, step_y = _cascade_step()
    x, y = _slot_position(slot, step_x, step_y, win_w, win_h)

    # fluxbox 把 wmctrl -e 传入的 y 当作外框上缘（不含标题栏），而 wmctrl -l -G
    # 读回的 y 含 ~39px 标题栏。为了让读回坐标精确落在槽位上，发命令时 y 减去
    # 标题栏高做补偿（实测偏移恒为 39，槽位0 也不会出屏，fluxbox 会夹在 0）。
    _TITLEBAR = 39
    cmd_y = y - _TITLEBAR

    for attempt in range(attempts):
        try:
            win_id = _find_own_window(source_name)
            if win_id is None:
                time.sleep(interval)
                continue

            geo = _window_geometry(win_id)
            if geo is None:
                time.sleep(interval)
                continue

            # 已经在槽位附近就不再动（幂等）。容差要小于标题栏补偿量 39，
            # 否则会把"差一个标题栏高"的窗口误判成已到位而跳过修正；
            # 取 15px（大于 fluxbox 正常吸附的 1-2px）。
            if abs(geo[0] - x) <= 15 and abs(geo[1] - y) <= 15:
                logger.debug(f"窗口已在槽位 {slot} 附近 ({geo[0]}, {geo[1]})，无需调整")
                return True

            _run(["wmctrl", "-i", "-r", win_id, "-e", f"0,{x},{cmd_y},-1,-1"])
            time.sleep(0.5)

            new_geo = _window_geometry(win_id)
            if new_geo and abs(new_geo[0] - x) <= 15 and abs(new_geo[1] - y) <= 15:
                logger.info(f"窗口已归位到槽位 {slot}: ({new_geo[0]}, {new_geo[1]})")
                return True
            # fluxbox 吸附/夹取导致没到位，下一轮重试
        except FileNotFoundError:
            logger.warning("未找到 wmctrl，跳过窗口归位（交给 fluxbox 自行摆放）")
            return False
        except Exception as e:
            logger.debug(f"窗口归位第 {attempt + 1} 次尝试出错: {e}")

        time.sleep(interval)

    logger.warning(f"窗口归位失败：{attempts} 次尝试后仍未完成")
    return False
