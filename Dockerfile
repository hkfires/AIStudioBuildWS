# 使用一个轻量的 Python 官方镜像作为基础
FROM python:3.11-slim-bookworm

# 设置工作目录，后续的命令都在这个目录下执行
WORKDIR /app

# 安装运行 Playwright 所需的最小系统依赖集
# 在同一层中清理 apt 缓存以减小镜像体积
RUN apt-get update && apt-get install -y --no-install-recommends \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libxrender1 libxtst6 ca-certificates \
    fonts-liberation libasound2 libpangocairo-1.0-0 libpango-1.0-0 libu2f-udev xvfb \
    && rm -rf /var/lib/apt/lists/*

# 拷贝并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 camoufox
RUN camoufox fetch

# 将项目中的所有文件拷贝到工作目录
COPY . .

# 设置容器启动时要执行的命令
CMD ["python", "run_camoufox.py"]
