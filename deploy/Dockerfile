# 物流追踪家 - 云端服务镜像
# 关键: UPS 只放行"有头模式", headless 会 HTTP2_PROTOCOL_ERROR, 故容器内用 Xvfb 虚拟屏
FROM docker.m.daocloud.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    TZ=Asia/Shanghai

# 国内镜像: apt -> 清华, pip -> 清华, playwright 浏览器 -> npmmirror
# Debian 13 用 DEB822 (/etc/apt/sources.list.d/debian.sources), 必须一起换掉, 否则仍在打 deb.debian.org
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie main\ndeb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie-updates main\ndeb https://mirrors.tuna.tsinghua.edu.cn/debian-security/ trixie-security main\n" > /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        fonts-liberation fonts-noto-cjk \
        ca-certificates curl \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/requirements.txt

# 浏览器: 代码只用 patchright(playwright 反检测分支, CLI 相同), 只装一份 chromium
# 官方 cdn.playwright.dev 在国内 ~50KB/s(177MB 要 1 小时), 走 npmmirror
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN python -m patchright install --with-deps chromium

# xray-core: 容器内本地代理(SS 节点 -> 美国出口), 供 UPS 抓取走海外线路
# 云端直连 GitHub 慢/断, 走 ghfast.top 镜像(实测 21MB/17s)
ARG XRAY_VERSION=v26.3.27
RUN curl -fsSL --retry 3 --max-time 300 -o /tmp/xray.zip \
        "https://ghfast.top/https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/xray.zip').extract('xray', '/usr/local/bin')" \
    && chmod +x /usr/local/bin/xray && rm /tmp/xray.zip \
    && xray version | head -1

# 服务代码
COPY . /app
WORKDIR /app

# vertu-cli 入口: 代码里 shell 调用 "vertu-cli ...", 这里做 shim 指向 node 包(鉴权靠 APP_KEY, 二进制本身无敏感信息)
RUN printf "#!/bin/sh\nexec node /app/bin/vertu-cli.cjs \"\$@\"\n" > /usr/local/bin/vertu-cli \
    && chmod +x /usr/local/bin/vertu-cli \
    && chmod +x /app/deploy/entrypoint.sh

# 健康状态(docker ps 可见): watcher 心跳 10 分钟内有更新
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
  CMD STALE_MIN="${WATCHER_STALE_MIN:-10}" \
      && case "$STALE_MIN" in ''|*[!0-9]*|0) exit 1 ;; esac \
      && test -f /app/data/.watcher-heartbeat \
      && STALE_FILE="$(find /app/data/.watcher-heartbeat -mmin +"$STALE_MIN" -print -quit)" \
      && test -z "$STALE_FILE" \
      && { [ -z "$ADMIN_TOKEN" ] || curl -fsS --max-time 3 http://127.0.0.1:${ADMIN_PORT:-8080}/healthz >/dev/null; }

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
