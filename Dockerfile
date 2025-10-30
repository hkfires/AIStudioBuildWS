# 使用一个轻量的 Python 官方镜像作为基础
FROM python:3.11-slim-bookworm

# 设置工作目录，后续的命令都在这个目录下执行
WORKDIR /app

# 将依赖文件拷贝到工作目录
COPY requirements.txt .

# 安装依赖，包括 Playwright 的浏览器引擎
# 使用 --no-cache-dir 减小镜像体积
# 使用 playwright install --with-deps 安装浏览器及其系统依赖
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps

# 将项目中的所有文件拷贝到工作目录
COPY . .

# 设置容器启动时要执行的命令
CMD ["python", "run_camoufox.py"]
