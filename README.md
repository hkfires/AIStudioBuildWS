> **请注意：** 本教程的部署方案需配合 `CLIProxyAPI` 使用。在开始之前，请确保你已有一个正在运行的 `CLIProxyAPI` 实例。

CLIProxyAPI 自 v6.3.x 版本起，开始支持通过 WebSocket 方式接入 AI Provider，并首个支持了 AIStudio。

然而，这种方式需要一个始终开启的浏览器来运行 AIStudioBuild 上的 WebSocket 通信程序，这总归有些不便。如果选择将其部署在 VPS 上，又会面临 VPS 内存要求较高的问题。

为了解决这个问题，我花了点时间尝试多种无头浏览器方案。最终，我选择使用 Docker 在 HuggingFace 上进行部署，此举能充分利用 HuggingFace 免费实例的大内存优势，实现零成本部署。

### 第一步：配置 AIStudioBuild 应用

需要根据你的 `CLIProxyAPI` 的设置，配置好 AIStudioBuild 上的 WebSocket 通信程序：打开官方提供的[示例程序](https://aistudio.google.com/apps/drive/1CPW7FpWGsDZzkaYgYOyXQ_6FWgxieLmL)，复制该程序后**需**修改图中红框处的两个地方。其中，如果 `CLIProxyAPI` 中设置了 `wsauth` 为 `true`，那么就需要设置 `JWT_TOKEN`为 `CLIProxyAPI` 中的拟用于鉴权的 `api-keys` 值；设置 `WEBSOCKET_PROXY_URL` 为 `CLIProxyAPI` 所在的地址，例如：`wss://mycap.example.com/v1/ws`。设置完成后保存，并记录这个应用的链接备用。

![](https://img.072899.xyz/2025/11/359a2572d0206c20dba7fe12a136d6e8.png)

多账户使用时，需要多操作一个步骤，将该应用访问权限设置为 `Public`。

![](https://img.072899.xyz/2025/11/69c6395d1a98c38c68bc6c8dd46b3014.png)

**安全警告：** 设置为 `Public` 后，请务必妥善保管你的链接。**切勿**将此链接公开分享，以免导致授权信息泄露。

### 第二步：准备 AIStudio Cookie

推荐使用已登录浏览器导出的 **JSON Cookie 数组**，保留原始 `domain`、`path`、`secure` 和 `sameSite` 属性。先在该浏览器中确认目标应用的 Preview 可以正常加载，再导出并替换对应的 `USER_COOKIE_1`（或其他账号变量）。普通 `name=value` 字符串不包含这些属性，本程序会使用 `.google.com` 和 `SameSite=Lax` 默认值，可能影响 iframe Preview 认证。

如果 AI Studio 编辑器可以打开，但 Preview 显示 Google 的 `401` 错误页，请先更新上述 Cookie 来源。Docker 部署更新环境变量后运行 `docker compose up -d --force-recreate`，重新加载凭证。

Cookie 可以通过两种方式获取，两种方式选一种即可，推荐使用指纹浏览器方式获取：

第一种方式：使用 AdsPower 指纹浏览器，登录 https://aistudio.google.com/ ，退出后编辑浏览器环境，复制 Cookie 内容，具体如下图所示：

![](https://img.072899.xyz/2025/11/c60399120703a24bdd450d38e31052a5.png)

第二种方式：通过 Chrome 等普通浏览器的隐私模式，登录 https://aistudio.google.com/ ，在浏览器的开发者工具中复制 Cookie 内容，具体位置如下图所示：

![](https://img.072899.xyz/2025/11/51f860bf363cab01aa4c3fd5181b7f72.png)

### 第三步（1）：部署 HuggingFace Space

打开 https://huggingface.co/spaces/hkfires/AIStudioBuildWS ，复制该 Space。在 `CAMOUFOX_INSTANCE_URL` 处填入第一步准备的程序的链接，在 `USER_COOKIE_1` 处填入第二步准备的 Cookie，点击 Duplicate Space。

![](https://img.072899.xyz/2025/11/04e84ce3b0f2abe7ae9e717ac8b5aa0b.png)

等待 HuggingFace 构建完成，出现如下日志，即部署成功：

![](https://img.072899.xyz/2025/11/e818f38cfb272c1fc10ca97c2ef23c6b.png)

如果有多个账户，参考 `USER_COOKIE_1`，在 HuggingFace Space 的设置中依次增加 `USER_COOKIE_2`、`USER_COOKIE_3` 等环境变量即可。

**可选配置：** 如果需要修改日志显示时区，可以添加 `TZ_OFFSET` 环境变量，例如设置为 `8` 表示 UTC+8（北京时间，默认值），设置为 `0` 表示 UTC。

**重要提醒：** Cookie 属于敏感信息，请**务必使用 "Secrets"** (而不是 "Variables") 来存储，以防止 Cookie 外泄。

### 第三步（2）：服务器 Docker 部署

如果你拥有自己的服务器（VPS），也可以使用 Docker Compose 进行部署。

1.  **下载代码**
    ```bash
    git clone https://github.com/hkfires/AIStudioBuildWS.git
    cd AIStudioBuildWS
    ```

2.  **配置环境变量**
    复制 `.env.example` 为 `.env`，并填入必要信息（`CAMOUFOX_INSTANCE_URL` 和 `USER_COOKIE_1` 等）。
    
    也可以在 `cookies` 目录下放置 JSON 格式的 Cookie 文件（文件名任意），程序会自动读取。
    ```bash
    cp .env.example .env
    nano .env
    ```

3.  **启动服务**
    ```bash
    docker compose up -d --build
    ```

部署成功后，我们应该在 `CLIProxyAPI` 的中看到类似如下的日志。至此，整个部署全部完成。

![](https://img.072899.xyz/2025/11/e0db39f81a3bbb956cbe9364e656a76f.png)
