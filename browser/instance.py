import os
import signal
import time
import json
import subprocess
import gc
from urllib.parse import urlparse
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils.logger import setup_logging
from utils.cookie_manager import CookieManager
from browser.navigation import handle_successful_navigation, KeepAliveError
from browser.dialogs import wait_for_app_ready, AppReadinessError, PreviewAuthenticationError
from browser.cookie_validator import CookieValidator
from camoufox.sync_api import Camoufox
from utils.paths import logs_dir
from utils.common import parse_headless_mode, ensure_dir, clean_env_value
from utils.url_helper import extract_url_path, mask_url_for_logging, mask_path_for_logging
from camoufox.utils import launch_options as generate_launch_options
from browserforge.fingerprints import Screen
from browser.window_placer import place_window


def run_browser_instance(config, shutdown_event=None):
    """
    根据最终合并的配置，启动并管理一个单独的 Camoufox 浏览器实例。
    使用CookieManager统一管理Cookie加载，避免重复的扫描逻辑。
    """
    # 重置信号处理器，确保子进程能响应 SIGTERM
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    # 忽略 SIGINT (Ctrl+C)，让主进程统一处理
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    cookie_source = config.get('cookie_source')
    if not cookie_source:
        # 使用默认logger进行错误报告
        logger = setup_logging(os.path.join(logs_dir(), 'app.log'))
        logger.error("错误: 配置中缺少cookie_source对象")
        return

    instance_label = cookie_source.display_name
    logger = setup_logging(
        os.path.join(logs_dir(), 'app.log'),
        prefix=instance_label
    )
    diagnostic_tag = instance_label.replace(os.sep, "_")

    expected_url = config.get('url')
    proxy = config.get('proxy')
    headless_setting = config.get('headless', 'virtual')

    # 使用CookieManager加载Cookie
    cookie_manager = CookieManager(logger)
    all_cookies = []

    try:
        # 直接使用CookieSource对象加载Cookie
        cookies = cookie_manager.load_cookies(cookie_source)
        all_cookies.extend(cookies)

    except Exception as e:
        logger.error(f"从Cookie来源加载时出错: {e}")
        # 这里先不立刻 return，如果是Profile模式可能根本不需要加载成功

    # 【关键修改】：放宽限制，允许空 Cookie，因为我们要读本地 Profile
    if not all_cookies:
        logger.info(f"未检测到环境变量传入的 Cookie，将完全依赖本地 Profile ({diagnostic_tag}) 进行免密登录。")
        # return 

    cookies = all_cookies

    headless_mode = parse_headless_mode(headless_setting)
    launch_options = {"headless": headless_mode}
    # launch_options["block_images"] = True  # 禁用图片加载

    if proxy:
        logger.info(f"使用代理: {proxy} 访问")
        launch_options["proxy"] = {"server": proxy, "bypass": "localhost, 127.0.0.1"}

    screenshot_dir = logs_dir()
    ensure_dir(screenshot_dir)

    # ================= [新增代码：多用户 Profile 和指纹管理] =================
    profiles_base_dir = "/app/camoufox_profiles"
    ensure_dir(profiles_base_dir)

    # 使用 identifier (如 USER_COOKIE_1) 作为文件夹名，实现多用户完全隔离
    profile_dir = os.path.join(profiles_base_dir, diagnostic_tag)
    ensure_dir(profile_dir)

    fingerprint_file = os.path.join(profile_dir, "fingerprint.json")
    if os.path.exists(fingerprint_file):
        with open(fingerprint_file, "r") as f:
            fingerprint_opts = json.load(f)
        logger.info(f"已加载现有的环境指纹和 Profile: {profile_dir}")
    else:
        # ====== 新增/修改代码开始 ======
        # 必须显式传入 screen 和 window 参数！
        # 根因：Docker 中 get_screen_cons() 内部调用 get_monitors() 会静默失败
        # （需要 xrandr，容器没装），导致 screen=None 传给 BrowserForge，
        # 其默认 Windows 桌面分布的众数是 1680x1050，每次新生成的指纹都确定性偏大。
        # screen 约束生成的 screen 尺寸上限，window 精确控制窗口大小。
        fingerprint_opts = generate_launch_options(
            user_data_dir=profile_dir,
            os="windows",
            screen=Screen(max_width=1440, max_height=900),
            window=(1440, 900),
        )
        # ====== 新增/修改代码结束 ======
        with open(fingerprint_file, "w") as f:
            json.dump(fingerprint_opts, f, indent=4)
        logger.info(f"已生成并锁定全新环境指纹: {profile_dir}")

    # 将指纹和持久化设置合并到 Camoufox 启动选项中
    launch_options["from_options"] = fingerprint_opts
    launch_options["persistent_context"] = True
    launch_options["user_data_dir"] = profile_dir

    # [新增] 强制 Firefox 禁用后台资源冻结、标签页休眠和遮挡跟踪 (Occlusion Tracking)
    # 这对多窗口/多标签页在无头环境下能否持续运行至关重要
    launch_options["firefox_user_prefs"] = {
        #"browser.tabs.unloadOnLowMemory": False,  # 禁用低内存卸载
        "dom.min_background_timeout_value": 100,  # 维持后台计时器频率
        "network.websocket.timeout": 0,  # 禁用WS超时
        "page_visibility.dont_suspend_inactive": True,  # 防止非活动页面挂起
        "dom.timeout.enable_budget_timer_fallback": False,
        "widget.windows.window_occlusion_tracking.enabled": False,  # 禁用遮挡跟踪（如果窗口被遮挡，原本会挂起渲染）
        #"gfx.webrender.dcomp-win.enabled": False,  # 关闭可能导致黑屏或不渲染的硬件加速遮挡        
        # 1. 限制内存缓存容量（而非禁用）
        # 禁用 browser.cache.memory 会同时关掉 Firefox 的 memory-pressure 回收机制，
        # 反而导致 GC 变懒、RSS 只增不还；保留缓存但限制容量更稳
        "browser.cache.memory.capacity": 65536,  # 内存缓存上限 64MB
        #"browser.cache.disk.enable": False,       # 甚至可以禁止磁盘缓存，防止磁盘I/O导致延迟
        # 2. 砍掉页面历史记录 (Session History)
        # 非常关键！默认浏览器会记住50个页面的状态(为了按后退键能够秒开)，极其占内存
        "browser.sessionhistory.max_entries": 2,  # 仅保留2个前进后退记录
        "browser.sessionstore.max_tabs_undo": 0,  # 关闭"恢复关闭的标签页"功能
        # 3. 强制激进的垃圾回收 (Garbage Collection & JS Memory)
        "javascript.options.mem.max": 102400,     # 限制 JS 引擎最大使用内存阈值 (单位KB)
        "javascript.options.mem.high_water_mark": 32, # 更低的水位线，更早触发GC
        # 4. 优化图片与媒体内存消耗（如果你不需要看高清图片）
        "image.mem.decodeondraw": True,           # 只有真正画出来的时候才解码图片
        "image.mem.discardable": True,            # 允许释放掉未显示的图片内存
        "image.mem.max_decoded_image_kb": 10240,  # 限制单张解码图片的最大内存 (10MB)
        # 5. 严格限制子进程数量（防止它悄悄开多个辅助进程）
        "dom.ipc.processCount": 1,                # 强制将网页内容进程数量限制在1个
        "dom.ipc.processCount.extension": 1,      # 限制插件进程数
        # 6. 禁用不需要的遥测和后台服务 (有效减少闲置内存占用)
        "toolkit.telemetry.enabled": False,
        "browser.ping-centre.telemetry": False,
        "network.prefetch-next": False,           # 禁用链接预读取
        "network.dns.disablePrefetch": True,      # 禁用 DNS 预解析
        # 已回退：media.autoplay.default=5 和 image.animation_mode=none
        # 这两条会让浏览器媒体/动图行为偏离真实用户，Google 反爬的行为指纹
        # 一致性校验可能因此判 403，而 AI Studio 页面并无自动播放媒体，收益≈0
    }

    # =========================================================================
    # 重启控制变量
    max_retries = int(os.getenv("MAX_RESTART_RETRIES", "5"))
    # 人工干预等待超时（秒），通过环境变量配置，默认 10 分钟
    manual_login_timeout = int(os.getenv("MANUAL_LOGIN_TIMEOUT", "600"))
    retry_count = 0
    base_delay = 3

    while True:
        # 检查是否收到全局关闭信号
        if shutdown_event and shutdown_event.is_set():
            logger.info("检测到全局关闭事件，浏览器实例不再启动，准备退出")
            return

        # ====== 启动前强制清理当前实例的僵尸进程和回收内存 ======
        try:
            # 强制 Python 垃圾回收，释放上一轮残留的 Playwright 对象
            gc.collect()

            # 【安全修复】只清理属于当前 profile 的孤儿进程，避免误杀其他用户实例
            # camoufox 启动命令中包含 -profile /app/camoufox_profiles/USER_COOKIE_X
            # 注意：正则必须精确匹配完整目录名，否则 USER_COOKIE_1 会误杀
            # USER_COOKIE_10/11（前缀匹配），引发多账户级联重启和 403
            escaped_profile_dir = profile_dir.replace("/", "\\/")
            subprocess.run(
                f"pkill -f 'camoufox-bin.*-profile {escaped_profile_dir}( |$)' || true",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(2)  # 给 OS 一点时间回收内存
            logger.debug(f"已完成启动前清理（垃圾回收 + {diagnostic_tag} 僵尸进程清理）")
        except Exception:
            pass
        # ====================================================

        try:
            # === [修改代码：由于开启了持久化，Camoufox 返回的是 BrowserContext] ===
            with Camoufox(**launch_options) as context:
                # 获取持久化上下文中已有的默认页面，如果没有则新建
                page = context.pages[0] if context.pages else context.new_page()

                # 依然兼容你现有的环境变量/JSON导入逻辑（当作初始凭证注入）
                if cookies:
                    context.add_cookies(cookies)

                # ====== 注入稳定的 provider 名称，供 AI Studio App 注册 CLIProxyAPI 时使用 ======
                # App 代码在 Preview iframe 内通过 window.__PROVIDER_NAME__ 读取该值，
                # 拼到 WS 连接 URL 的 provider_name 参数上；带 ProviderFactory 补丁的
                # CLIProxyAPI 会以该名注册 provider，重连/重启时替换同名旧会话，
                # 而不是每次产生新的随机 aistudio-XXXX，保持用量统计归属稳定。
                # add_init_script 对 context 内所有 frame（含跨域 iframe）的每次导航生效。
                # 默认取 Cookie 来源名（USER_COOKIE_N / 文件名去 .json），与账户一一对应；
                # 可用 PROVIDER_NAME_<来源名大写> 环境变量覆盖（如 PROVIDER_NAME_USER_COOKIE_1=alice）。
                provider_name = cookie_source.display_name
                if provider_name.lower().endswith(".json"):
                    provider_name = provider_name[:-5]
                provider_name_override_env = "PROVIDER_NAME_" + "".join(
                    c if c.isalnum() else "_" for c in provider_name.upper()
                )
                provider_name = clean_env_value(os.getenv(provider_name_override_env)) or provider_name
                context.add_init_script(f"window.__PROVIDER_NAME__ = {json.dumps(provider_name)};")
                logger.info(f"已注入 provider 名称: {provider_name}（可用环境变量 {provider_name_override_env} 覆盖）")

                # 创建Cookie验证器 (原有代码不需要改，context 对象完美兼容)
                cookie_validator = CookieValidator(page, context, logger)

                # =============================================================
                # 下方的 page.goto() 等业务逻辑完全不需要改动！！！

                response = None
                try:
                    logger.info(f"正在导航到: {mask_url_for_logging(expected_url)} (超时设置为 90 秒)")
                    # page.goto() 会返回一个 response 对象，我们可以用它来获取状态码等信息
                    response = page.goto(expected_url, wait_until='domcontentloaded', timeout=90000)

                    # 检查HTTP响应状态码
                    if response:
                        logger.info(f"导航初步成功，服务器响应状态码: {response.status} {response.status_text}")
                        if not response.ok:  # response.ok 检查状态码是否在 200-299 范围内
                            logger.warning(f"警告：页面加载成功，但HTTP状态码表示错误: {response.status}")
                            # 即使状态码错误，也保存快照以供分析
                            page.screenshot(path=os.path.join(screenshot_dir, f"WARN_http_status_{response.status}_{diagnostic_tag}.png"))
                    else:
                        # 对于非http/https的导航（如 about:blank），response可能为None
                        logger.warning("page.goto 未返回响应对象，可能是一个非HTTP导航")

                except TimeoutError:
                    # 这是最常见的错误：超时
                    logger.error(f"导航到 {mask_url_for_logging(expected_url)} 超时 (超过90秒)")
                    logger.error("可能原因：网络连接缓慢、目标网站服务器无响应、代理问题、或页面资源被阻塞")
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

                    # 不要直接 return 终止进程，抛出 KeepAliveError 交给外部循环重试 (即刷新页面)
                    raise KeepAliveError(f"页面加载超时: {expected_url}")

                except PlaywrightError as e:
                    # 捕获其他Playwright相关的网络错误，例如DNS解析失败、连接被拒绝等
                    error_message = str(e)
                    logger.error(f"导航到 {mask_url_for_logging(expected_url)} 时发生 Playwright 网络错误")
                    logger.error(f"错误详情: {error_message}")

                    # Playwright的错误信息通常很具体，例如 "net::ERR_CONNECTION_REFUSED"
                    if "net::ERR_NAME_NOT_RESOLVED" in error_message:
                        logger.error("排查建议：检查DNS设置或域名是否正确")
                    elif "net::ERR_CONNECTION_REFUSED" in error_message:
                        logger.error("排查建议：目标服务器可能已关闭，或代理/防火墙阻止了连接")
                    elif "net::ERR_INTERNET_DISCONNECTED" in error_message:
                        logger.error("排查建议：检查本机的网络连接")

                    # 同样尝试截图，尽管此时页面可能完全无法访问
                    try:
                        screenshot_path = os.path.join(screenshot_dir, f"FAIL_network_error_{diagnostic_tag}.png")
                        page.screenshot(path=screenshot_path)
                        logger.info(f"已截取网络错误时的屏幕快照: {screenshot_path}")
                    except Exception as diag_e:
                        logger.error(f"在尝试进行网络错误诊断（截图）时发生额外错误: {diag_e}")

                    # 网络错误也应该重试刷新，而不是直接终止
                    raise KeepAliveError(f"网络错误: {error_message}")

                # --- 如果导航没有抛出异常，继续执行后续逻辑 ---
                logger.info("页面初步加载完成，正在检查并处理初始弹窗...")
                page.wait_for_timeout(2000)

                expected_path = extract_url_path(expected_url).split('?')[0]

                # 1. 目标页面等待逻辑（支持通过环境变量 MANUAL_LOGIN_TIMEOUT 配置超时时间）
                # 目标页域名白名单：ai.studio 与 aistudio.google.com 互为别名（启动时 ai.studio
                # 会 301 到 aistudio.google.com），到达其中任一域名才算"到了目标页"。
                # 关键：不能只比路径！Google 登录页的 URL 长这样
                #   https://accounts.google.com/v3/signin/identifier?continue=https://aistudio.google.com/apps/<ID>
                # continue 参数（未编码时）原文包含目标路径，纯路径子串匹配会把登录页
                # 误判成"已到达目标页"，跳过人工登录等待，随后 iframe 等待超时 ->
                # 重启循环重建页面，人工根本来不及登录（403 清 Profile 后必现）。
                target_hosts = ("ai.studio", "aistudio.google.com")

                def is_at_target_url():
                    host = urlparse(page.url).hostname or ""
                    if not any(host == h or host.endswith("." + h) for h in target_hosts):
                        return False
                    current_path = extract_url_path(page.url)
                    return bool(expected_path) and expected_path in current_path

                if not is_at_target_url():
                    logger.warning(f"[{diagnostic_tag}] 尚未到达目标页面！当前在: {mask_url_for_logging(page.url)}")
                    logger.warning(f"[{diagnostic_tag}] 可能是遇到了登录、Passkey提示、或安全检查...")
                    logger.warning(f"[{diagnostic_tag}] >>> 请立即前往 VNC 桌面 (http://IP:6080) 手动完成操作！")
                    logger.warning(f"[{diagnostic_tag}] >>> 脚本将在此挂起等待，最多等待 {manual_login_timeout} 秒 (可通过 MANUAL_LOGIN_TIMEOUT 环境变量调整)...")
                    wait_time = 0
                    while not is_at_target_url() and wait_time < manual_login_timeout:
                        page.wait_for_timeout(5000)
                        wait_time += 5
                    if not is_at_target_url():
                        logger.error(f"[{diagnostic_tag}] {manual_login_timeout}秒内未到达目标页面，退出并放弃该实例。")
                        page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_manual_action_timeout_{diagnostic_tag}.png"))
                        return
                    else:
                        logger.info(f"[{diagnostic_tag}] 人工操作完成，成功到达目标页面！")

                logger.info(f"URL验证通过。目标路径: {mask_path_for_logging(expected_path)}")

                # 初始化和保活共享弹窗处理；无 spinner 并不代表弹窗已关闭。
                auth_error_locator = page.get_by_text("authentication error", exact=False)
                if auth_error_locator.first.is_visible():
                    logger.error("检测到认证失败错误。Cookie已过期或无效")
                    page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_auth_error_{diagnostic_tag}.png"))
                    return

                logger.info("正在等待 AI Studio 弹窗清理、Preview 加载和 WS 连接...")
                try:
                    wait_for_app_ready(page, logger, timeout=30)
                except PreviewAuthenticationError:
                    try:
                        page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_preview_auth_{diagnostic_tag}.png"))
                    except Exception as screenshot_error:
                        logger.warning(f"保存 Preview 认证失败截图时出错: {screenshot_error}")
                    raise
                except AppReadinessError as error:
                    logger.error(str(error))
                    page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_app_not_ready_{diagnostic_tag}.png"))
                    raise KeepAliveError(str(error)) from error

                # ====== 401 致命错误拦截：仅在页面完全加载后才开始监听 ======
                # 页面初始化阶段 (goto + 弹窗处理 + iframe加载) 会有大量正常的 401
                # (alkalimakersuite-pa 等 API 在 token 建立前会返回 401，这是正常行为)
                # 所以我们只在所有初始化完成后才注册监听器，用于后续的保活阶段
                auth_401_count = [0]  # 各端点"连续失败数"的最大值，供保活循环轮询
                AUTH_401_THRESHOLD = int(os.getenv("AUTH_FAILURE_THRESHOLD", "3"))  # 同一端点连续认证失败阈值（默认3次，可环境变量调整）
                auth_fail_by_method = {}  # rpc方法名 -> 该方法连续失败次数

                def _rpc_method_name(url):
                    """提取 API 方法名（去掉 query，避免泄露 key/token 等参数）"""
                    tail = url.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
                    # $rpc 全路径较长（如 google.internal...MakerSuiteService.CountTokens），只保留方法名
                    return tail.rsplit('.', 1)[-1] if '.' in tail else tail

                def on_response_post_init(response):
                    """页面完全加载后的 API 认证失败/成功监听"""
                    url = response.url
                    # 只关注真正的 Gemini 推理 API 端点
                    if any(api_pattern in url for api_pattern in [
                        "generativelanguage.googleapis.com",
                        "alkalimakersuite-pa.clients6.google.com",
                    ]):
                        req_method = response.request.method
                        # CORS 预检 OPTIONS 恒返回 200，不代表真实 API 成功，必须忽略——
                        # 否则每次真实请求 403 前都有一个预检 200 把计数器清零，阈值永远到不了
                        if req_method in ("OPTIONS", "HEAD"):
                            return
                        method_name = _rpc_method_name(url)
                        if response.status == 401 or response.status == 403:
                            auth_fail_by_method[method_name] = auth_fail_by_method.get(method_name, 0) + 1
                            count = auth_fail_by_method[method_name]
                            auth_401_count[0] = max(auth_fail_by_method.values())
                            # 401 = 令牌/登录态失效；403 = 权限、配额或来源风控（均非网络故障）
                            hint = "登录态/令牌失效" if response.status == 401 else "权限/配额/来源限制"
                            # 首次失败时抓取响应体（Google 返回 JSON error，含 PERMISSION_DENIED/QUOTA 等具体原因）
                            body_snippet = ""
                            if count == 1:
                                try:
                                    ctype = response.headers.get("content-type", "")
                                    if "json" in ctype or "text" in ctype:
                                        body_snippet = f" | 响应体: {response.text()[:300]}"
                                except Exception:
                                    pass
                            logger.warning(
                                f"[{diagnostic_tag}] API 认证失败 [{method_name}] ({count}/{AUTH_401_THRESHOLD}): "
                                f"{req_method} {url.split('?')[0][:120]} 状态码: {response.status} ({hint}){body_snippet}"
                            )
                            if count >= AUTH_401_THRESHOLD:
                                # 不在回调里抛异常（Playwright 事件回调的异常不会传播到主循环），
                                # 仅打日志；由 handle_successful_navigation 的保活循环轮询
                                # auth_401_count 并抛 KeepAliveError 重启自愈
                                logger.error(
                                    f"[{diagnostic_tag}] 端点 [{method_name}] 连续 {AUTH_401_THRESHOLD} 次 API 认证失败，"
                                    f"等待保活循环重建会话"
                                )
                        elif 200 <= response.status < 300:
                            # 只重置同一方法的连续失败计数——同主机上其他端点的成功
                            # 不能掩盖本端点的持续失败（否则阈值同样永远到不了）
                            if auth_fail_by_method.get(method_name, 0) > 0:
                                logger.info(
                                    f"[{diagnostic_tag}] API 请求成功 [{method_name}] ({response.status})，"
                                    f"重置该方法失败计数"
                                )
                                auth_fail_by_method[method_name] = 0
                                auth_401_count[0] = max(auth_fail_by_method.values())

                page.on("response", on_response_post_init)
                # ====================================================

                logger.info("初始化验证通过，进入最终就绪检查和保活")

                # 防止窗口被其他实例 100% 盖死（会触发 Firefox occlusion sleep）。
                # 根因：persistent profile 的 xulstore.json 会让 Firefox 带着
                # _NET_CURRENT_DESKTOP 标记复活窗口，fluxbox 的 CascadePlacement
                # 对这类窗口直接跳过摆放，所以重启后窗口会叠在上次的位置。
                # 在页面完全加载后调用（此时窗口已稳定存在，重启瞬间窗口/标题
                # 可能还没就绪，太早调用会找不到窗口而归位失败）。把窗口摆到
                # 本实例的确定性槽位，保证和其它实例错开。
                place_window(diagnostic_tag, logger)

                handle_successful_navigation(page, logger, diagnostic_tag, shutdown_event, cookie_validator, expected_path, expected_url, auth_401_count, AUTH_401_THRESHOLD)

                # 移除 response 监听器
                try:
                    page.remove_listener("response", on_response_post_init)
                except Exception:
                    pass

                # 如果运行到这里且没有异常，表示实例正常结束（例如收到关闭信号）
                # 正常结束时重置重试计数器
                retry_count = 0
                return

        except PreviewAuthenticationError as error:
            logger.error(str(error))
            logger.error("停止自动重试此实例；请更新 Cookie 来源后重新创建容器")
            return
        except KeepAliveError as e:
            # 如果是 API 连续认证失败引发的重启，自动清理损坏的 Profile 缓存
            # 注意：保留 fingerprint.json 保持指纹一致，只清理 session/cookies 缓存
            if "API 连续认证失败" in str(e):
                logger.warning(f"检测到 [{diagnostic_tag}] 凭证已失效，准备自动清空 Profile 缓存以重新初始化...")
                try:
                    import shutil
                    for item in os.listdir(profile_dir):
                        if item != "fingerprint.json":
                            item_path = os.path.join(profile_dir, item)
                            if os.path.isdir(item_path):
                                shutil.rmtree(item_path, ignore_errors=True)
                            else:
                                try:
                                    os.remove(item_path)
                                except Exception:
                                    pass
                    logger.info(f"成功清理 [{diagnostic_tag}] Profile 缓存 (指纹特征已保留)")
                except Exception as clean_e:
                    logger.error(f"清理 [{diagnostic_tag}] Profile 缓存失败: {clean_e}")

            retry_count += 1
            if retry_count > max_retries:
                logger.error(f"重试次数已达上限 ({max_retries})，实例不再重启，退出")
                return
            # 指数退避：3秒、6秒、12秒、24秒...最长60秒
            delay = min(base_delay * (2 ** (retry_count - 1)), 60)
            logger.error(f"浏览器实例出现错误 (重试 {retry_count}/{max_retries})，将在 {delay} 秒后重启浏览器实例: {e}")
            time.sleep(delay)
            continue
        except KeyboardInterrupt:
            logger.info(f"用户中断，正在关闭...")
            return
        except SystemExit as e:
            # 捕获Cookie验证失败时的系统退出
            if e.code == 1:
                logger.error("Cookie验证失败，关闭进程实例")
            else:
                logger.info(f"实例正常退出，退出码: {e.code}")
            return
        except Exception as e:
            # 这是一个最终的捕获，用于捕获所有未预料到的错误
            logger.exception(f"运行 Camoufox 实例时发生未预料的严重错误: {e}")
            return
