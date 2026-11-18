# 服务器版：一次构建，定时执行 run.sh
#
# 构建：
#   docker build -f deploy/Dockerfile -t node-subs .
#
# 跑一轮（产物落在宿主的 ./out）：
#   docker run --rm -v "$PWD/out:/out" -e PUBLISH=none node-subs
#
# 常驻每 3 小时跑一次（用 cron 表达式，纯容器内，不依赖宿主 cron）：
#   docker run -d --name node-subs -v "$PWD/out:/out" \
#     -e PUBLISH=none -e RUN_EVERY_SECONDS=10800 node-subs daemon
#
# 环境变量见 deploy/env.example。

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tar git tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 产物写到 /out（建议挂卷），这样容器重建不丢
ENV PUBLISH=none \
    SINGBOX_BIN=/app/sing-box \
    NO_PROXY="*"

RUN chmod +x run.sh

# 默认跑一轮；传 daemon 则循环执行（间隔 RUN_EVERY_SECONDS，默认 3 小时）
ENTRYPOINT ["/app/run.sh"]
CMD []
