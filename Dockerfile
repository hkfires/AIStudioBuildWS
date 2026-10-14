# 使用一个轻量的 Python 官方镜像作为基础
FROM python:3.11-slim-bookworm

# 设置工作目录，后续的命令都在这个目录下执行
WORKDIR /app

# 安装运行 Playwright 所需的最小系统依赖集
# 安装必要的依赖，包括 VNC, 窗口管理器, x11检测工具，以及 NoVNC (用于网页访问)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libxrender1 libxtst6 ca-certificates \
    fonts-liberation libasound2 libpangocairo-1.0-0 libpango-1.0-0 libu2f-udev \
    xvfb x11vnc fluxbox novnc websockify x11-utils xdotool wmctrl \
    && rm -rf /var/lib/apt/lists/*

# 拷贝并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 camoufox
RUN camoufox fetch

# 将项目中的所有文件拷贝到工作目录
COPY . .
# ====== 新增：配置 Fluxbox ======
# 告诉窗口管理器：使用层叠（Cascade）摆放策略，每次新窗口向右下角偏移一点点
# 这样保证没有任何一个窗口会被100%完全遮挡！Firefox 就不会进入 Occlusion Sleep！
RUN mkdir -p /root/.fluxbox && \
    echo 'session.screen0.windowPlacement: CascadePlacement' > /root/.fluxbox/init && \
    echo 'session.screen0.rowPlacement: TopBottom' >> /root/.fluxbox/init && \
    echo 'session.screen0.colPlacement: LeftRight' >> /root/.fluxbox/init
# 修改启动脚本：启动 Xvfb -> VNC -> NoVNC网页服务 -> Python脚本
# 修改启动脚本：增加 rm 锁文件 和 Xvfb 的 -ac 参数，以及完备的主动状态检测机制
RUN echo '#!/bin/bash\n\
export DISPLAY=:99\n\
\n\
# 禁用 fbsetbg 以防止找不到壁纸设置器的弹窗\n\
mkdir -p ~/.fluxbox\n\
echo "background: none" > ~/.fluxbox/overlay\n\
# 创建一个空的 fbsetbg 脚本以彻底禁用它\n\
mkdir -p /usr/local/bin\n\
echo "#!/bin/sh" > /usr/local/bin/fbsetbg\n\
echo "exit 0" >> /usr/local/bin/fbsetbg\n\
chmod +x /usr/local/bin/fbsetbg\n\
\n\
# 清理可能残留的 X11 锁文件，防止容器重启后报错\n\
rm -f /tmp/.X99-lock\n\
\n\
# 1. 启动虚拟显示器 (加上 -ac 禁用访问控制，非常重要！)\n\
Xvfb :99 -ac -screen 0 1920x1080x24 -nolisten tcp &\n\
sleep 2\n\
\n\
# 2. 启动窗口管理器\n\
fluxbox &\n\
\n\
# 3. 启动 VNC 服务\n\
x11vnc -display :99 -nopw -listen localhost -xkb -forever &\n\
\n\
# 4. 启动 NoVNC (将 VNC 转为网页 WebSocket)\n\
websockify --web /usr/share/novnc/ 6080 localhost:5900 &\n\
\n\
echo "可以通过 http://localhost:6080/vnc.html 访问桌面了！"\n\
echo "等待桌面环境完全初始化..."\n\
\n\
# 主动检测桌面就绪状态，而不是盲目 sleep\n\
# 检查项:\n\
#   1. X Server 可连接\n\
#   2. fluxbox 窗口管理器正在运行且有响应\n\
#   3. XFIXES 扩展已注册 (x11vnc 光标追踪依赖)\n\
#   4. DAMAGE 扩展已注册 (x11vnc 画面更新依赖)\n\
MAX_CHECKS=10\n\
CHECK_INTERVAL=3\n\
for i in $(seq 1 $MAX_CHECKS); do\n\
    ALL_READY=true\n\
\n\
    # 检查 X Server\n\
    if ! xdpyinfo -display :99 > /dev/null 2>&1; then\n\
        echo "  [$i/$MAX_CHECKS] X Server 尚未就绪..."\n\
        ALL_READY=false\n\
    fi\n\
\n\
    # 检查 fluxbox 焦点管理是否就绪\n\
    if ! xdotool search --onlyvisible --name ".*" > /dev/null 2>&1 && ! xdotool getactivewindow > /dev/null 2>&1; then\n\
        echo "  [$i/$MAX_CHECKS] fluxbox 窗口管理器尚未完全接管根窗口..."\n\
        ALL_READY=false\n\
    fi\n\
\n\
    # 检查 XFIXES 扩展\n\
    if ! xdpyinfo -display :99 -queryExtensions 2>/dev/null | grep -q "XFIXES"; then\n\
        echo "  [$i/$MAX_CHECKS] XFIXES 扩展尚未注册..."\n\
        ALL_READY=false\n\
    fi\n\
\n\
    # 检查 DAMAGE 扩展\n\
    if ! xdpyinfo -display :99 -queryExtensions 2>/dev/null | grep -q "DAMAGE"; then\n\
        echo "  [$i/$MAX_CHECKS] DAMAGE 扩展尚未注册..."\n\
        ALL_READY=false\n\
    fi\n\
\n\
    if [ "$ALL_READY" = true ]; then\n\
        echo "  [$i/$MAX_CHECKS] 桌面环境已完全就绪 (X Server + fluxbox + XFIXES + DAMAGE)!"\n\
        break\n\
    fi\n\
\n\
    if [ $i -eq $MAX_CHECKS ]; then\n\
        echo "  [$i/$MAX_CHECKS] 等待超时，强制继续 (部分环境可能未完全初始化)"\n\
    else\n\
        sleep $CHECK_INTERVAL\n\
    fi\n\
done\n\
\n\
# 等待桌面完全稳定后再启动主程序，特别是给第一个浏览器实例（User 1）一个完全就绪的桌面环境\n\
sleep 30\n\
echo "启动主程序..."\n\
exec python main.py\n\
' > /app/start.sh

RUN chmod +x /app/start.sh

# 暴露 NoVNC 网页端口
EXPOSE 6080
# 暴露 Hugging Face Spaces 期望的端口（仅在服务器模式下使用）
EXPOSE 7860

# 设置容器启动时要执行的命令
CMD ["/app/start.sh"]
#CMD ["python", "main.py"]