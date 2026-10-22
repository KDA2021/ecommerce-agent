# =============================================================================
# ecommerce-agent 前端生产镜像（P15-3）
#
# 两阶段：
#   1. node:22-alpine：按 package-lock.json 跑 npm ci 构建 dist（与 CI / Makefile
#      同一条命令 `npm run build`，避免「本地绿、镜像红」）；
#   2. nginx:alpine：只带 dist 静态文件 + 反代配置，不带 node / node_modules。
#
# 构建上下文是 frontend/（见 docker/docker-compose.yaml 的 frontend.build.context），
# 这样 npm 依赖层缓存不会被仓库其它目录的改动打散。
# 运行期：nginx 监听 80，静态资源直接吐；/api/* 反代到 app:8000（去掉 /api 前缀）。
# =============================================================================

FROM node:22-alpine AS builder

WORKDIR /app

# 先只 copy 依赖清单：源码改动不会让 npm ci 这一层重跑。
# 国内镜像加速 npm；缓存挂载让依赖不变时重建不重新下载。
ENV NPM_CONFIG_REGISTRY=https://registry.npmmirror.com

COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# 覆盖镜像自带的 default.conf：一个 server 同时负责静态托管与 /api 反代。
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# 探针打 /index.html 而不是 /：两者都命中同一个 location，
# 用文件路径表达「探的是静态资源是否就位」，语义更明确。
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD ["wget", "-q", "--spider", "http://127.0.0.1/index.html"]

CMD ["nginx", "-g", "daemon off;"]
