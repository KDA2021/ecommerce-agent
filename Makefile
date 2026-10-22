# ecommerce-agent 开发者入口（P15-1）
#
# 需要 GNU Make（Windows 用 w64devkit / MSYS2 / chocolatey 的 make，或 Git Bash 自带的
# make）。所有 recipe 用 POSIX sh 语法，Windows 下也走 MSYS / Git Bash 的 sh。
#
# 常用：
#   make            # 等价 make help
#   make test       # 全量 pytest（含 MySQL 集成；未起库时自动 skip）
#   make lint       # ruff check + ruff format --check
#   make build      # 前端生产构建
#   make run        # 启动 FastAPI（http://localhost:8000）
#   make docker-up  # docker compose 起 mysql + redis + app + frontend
#
# 变量都可在命令行覆盖，例如：
#   make test ARGS="-k preference -x"
#   make lint FILES="app/application"
#   make test TEST_MYSQL_URL=            # 强制跳过 MySQL 集成用例
#   make run PORT=8080

SHELL := /bin/sh

# uv：默认用 PATH 上的 uv；也支持 `make UV=/path/to/uv test`。
UV ?= uv
NPM ?= npm

# Makefile 位于仓库根，所有 recipe 的 cwd 就是仓库根，这里显式声明便于阅读。
ROOT := .

# 本地 MySQL 集成用例（docker/docker-compose.p1.yaml 映射到 3307）。
TEST_MYSQL_URL ?= mysql+asyncmy://ecommerce:ecommerce@localhost:3307/ecommerce?charset=utf8mb4
# 工作区内的临时目录：Windows 上默认 /tmp 不可写会让 chromadb 报错。
# 刻意不读宿主的 TMP/TEMP（Windows 上它们常是 `C:\...` 反斜杠路径，进 sh 会踩转义），
# 默认固定为工作区内目录；要换用 `make test TMPDIR=/tmp/ecommerce`。
TMPDIR ?=
TEST_TMP = $(if $(TMPDIR),$(TMPDIR),$(CURDIR)/.tmp-make)

PORT ?= 8000
HOST ?= 0.0.0.0
COMPOSE_FILE ?= docker/docker-compose.yaml
IMAGE ?= ecommerce-agent:0.1.0

# 追加参数：make test ARGS="-k redis" / make lint FILES="app"
ARGS ?=
FILES ?=
LINT_PATHS ?= app tests scripts migrations eval
ARGS_SEP = $(if $(strip $(ARGS)), ,)

.DEFAULT_GOAL := help
.PHONY: help test lint build run docker-build docker-up docker-down db-up

help: ## 列出所有目标
	@echo "ecommerce-agent make targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

test: ## 全量 pytest（TEST_MYSQL_URL 指向本地 3307，未起库时相关用例 skip）
	@mkdir -p "$(TEST_TMP)"
	@TMP="$(TEST_TMP)" TEMP="$(TEST_TMP)" TEST_MYSQL_URL="$(TEST_MYSQL_URL)" \
		$(UV) run --no-sync pytest -q$(ARGS_SEP)$(ARGS)

lint: ## ruff check + ruff format --check（FILES="..." 可只检查指定路径）
	$(UV) run --no-sync ruff check $(if $(FILES),$(FILES),$(LINT_PATHS))
	$(UV) run --no-sync ruff format --check $(if $(FILES),$(FILES),$(LINT_PATHS))

build: ## 前端生产构建（npm ci 后 vite build；产物在 frontend/dist）
	cd frontend && $(NPM) ci && $(NPM) run build

run: ## 启动 FastAPI（默认 0.0.0.0:8000）
	$(UV) run --no-sync python -m uvicorn app.presentation.server:app --host $(HOST) --port $(PORT)

docker-build: ## 构建后端镜像（IMAGE 可覆盖 tag）
	docker build -t $(IMAGE) -f Dockerfile .

docker-up: ## docker compose 起 mysql + redis + app + frontend
	docker compose -f $(COMPOSE_FILE) up -d

docker-down: ## 停 compose 栈（保留数据卷；要清库加 docker-down ARGS="-v"）
	docker compose -f $(COMPOSE_FILE) down $(ARGS)

db-up: ## 只起隔离 MySQL（3307，给 make test 的集成用例用）
	docker compose -f docker/docker-compose.p1.yaml up -d
