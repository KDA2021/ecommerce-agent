# syntax=docker/dockerfile:1

# =============================================================================
# ecommerce-agent 后端镜像（P15-3 收口）
#
# 两个阶段：
#   1. builder：用 uv 官方镜像按 uv.lock 装依赖。COPY 顺序刻意先 pyproject/lock
#      再业务代码，业务改动不会让依赖层缓存失效；
#   2. runtime：只带 /app/.venv + 业务代码，不带 uv、不带编译工具链。
#
# 运行期约定（与 docker/docker-compose.yaml 对齐）：
#   - 监听 0.0.0.0:8000；配置全部来自环境变量（compose 的 env_file + environment）；
#   - 可写目录只有 /app/data（named volume：Chroma 索引、JSONL trace、模型缓存）；
#   - HEALTHCHECK 走 /health（P11-3 起该路由不鉴权、不限流，永远开放）。
# =============================================================================

FROM python:3.11-slim AS builder

# uv 官方镜像：builder 只负责用 lockfile 安装依赖，不把 uv 带进 runtime。
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_HTTP_TIMEOUT=300

WORKDIR /app

COPY pyproject.toml uv.lock ./
# uv 缓存挂到构建缓存层：依赖版本不变时，重建不会重新下载 torch/transformers 等大包。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 业务代码与静态资产（P15 新增的 logging_config / routers 都在 app/ 内）。
# knowledge/ 必须在镜像里：bootstrap 会把 knowledge/*.md 切块写进 Chroma，
# 用 PROJECT_ROOT/knowledge 定位，缺了会打出「品类知识不可用」的降级路径。
COPY app ./app
COPY knowledge ./knowledge


FROM python:3.11-slim AS runtime

# torch / onnxruntime 依赖 libgomp；slim 基础镜像默认不带。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# HOME 指向 /app，让 huggingface / sentence-transformers / chromadb 这些库的默认缓存根
# （~/.cache）落在可预测位置；再把三类缓存显式收进 data 卷，保证「精排权重」与
# 「Chroma 默认 embedding 的 onnx 模型」这类大文件只下载一次、重启不丢：
#   ~/.cache/huggingface  → /app/data/.cache/huggingface（BGE reranker weights）
#   ~/.cache/chroma       → /app/data/.cache/chroma     （all-MiniLM-L6-v2 onnx）
# 两处都要给：只改 HF_HOME 的话，Chroma 的 embedding 模型仍会落在镜像层里，
# 容器重建就又得下一遍。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/app \
    HF_HOME=/app/data/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/data/.cache/huggingface

WORKDIR /app
COPY --from=builder /app /app

# data 是唯一可写目录：Chroma 索引 / traces/*.jsonl / badcases.jsonl / 模型缓存。
# compose 里挂 named volume（app-data）持久化，容器内预先建好并给足权限，
# 这样「不挂卷直接 docker run」也不会因为目录缺失在启动阶段报错。
RUN mkdir -p /app/data/chroma /app/data/traces \
        /app/data/.cache/huggingface /app/data/.cache/chroma \
    && chmod -R 777 /app/data

EXPOSE 8000

# 健康检查只用标准库 urllib：镜像里没有 curl（也不想为一次探针多装一个包）。
# /health 是一条不鉴权、不查库、不限流的静态路由，适合做存活/就绪判据。
# start-period 给到 120s：冷启动要预热 BGE reranker 并在首次运行时下载权重。
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read()"]

CMD ["uvicorn", "app.presentation.server:app", "--host", "0.0.0.0", "--port", "8000"]
