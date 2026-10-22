# -*- coding: utf-8 -*-
"""P14 架构约束测试：用 AST 静态分析守住分层依赖方向。

约束来源：``docs/superpowers/specs/2026-10-15-ecommerce-agent-p14-design.md`` 第 4 节。

本文件只做 **AST import 分析**，不 import 被测模块，因此不会拉起
FastAPI / LangGraph / Chroma / MySQL，运行快且不受运行环境影响。

被锁死的规则：

1. ``app.domain`` 不得 import
   ``fastapi`` / ``langchain`` / ``langgraph`` / ``chromadb`` / ``sqlalchemy``；
2. ``app.domain`` 只允许依赖标准库和 ``app.domain`` 自身，保证领域层是
   框架无关的业务内核；
3. ``app.presentation`` 不得直接 import 具体持久化实现
   （``app.infrastructure.persistence.mysql_*`` / ``in_memory_repositories``），
   只能通过容器 / 用例 / 端口访问数据；
4. ``app.application`` 不得反向依赖 ``app.presentation``；
5. 重构后的入口结构保持稳定：Agents 层 ``state.py`` / ``nodes`` / ``subagents`` /
   ``graph.py``，表现层 ``routers/`` + ``dependencies.py``，且
   ``main_graph.py`` 仍是纯兼容 re-export。
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"

#: 领域层禁止出现的框架根包（覆盖子模块，如 ``langchain_core.messages``）。
FORBIDDEN_DOMAIN_FRAMEWORKS = (
    "fastapi",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "chromadb",
    "sqlalchemy",
)

#: 表现层禁止直接依赖的具体持久化模块（前缀匹配到子模块）。
FORBIDDEN_PRESENTATION_PERSISTENCE = (
    "app.infrastructure.persistence.mysql_repositories",
    "app.infrastructure.persistence.mysql_task_store",
    "app.infrastructure.persistence.mysql_feedback_store",
    "app.infrastructure.persistence.mysql_metrics_store",
    "app.infrastructure.persistence.mysql_preference_store",
    "app.infrastructure.persistence.in_memory_repositories",
)


def _iter_python_files(directory: pathlib.Path) -> list[pathlib.Path]:
    """返回目录下所有非 ``__pycache__`` 的 ``.py`` 文件（稳定排序）。"""
    return sorted(
        path
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _module_package_parts(path: pathlib.Path) -> tuple[str, ...]:
    """返回该文件所属的绝对包路径，用于解析相对 import。

    例：``app/domain/catalog/sku.py`` -> ``("app", "domain", "catalog")``；
    包内 ``__init__.py`` 的包路径与所在目录相同。
    """
    relative = path.resolve().relative_to(REPO_ROOT.resolve())
    return tuple(relative.parts[:-1])


def _resolve_import_from(path: pathlib.Path, node: ast.ImportFrom) -> str:
    """把 ``from ... import ...`` 解析成绝对模块名。"""
    if node.level == 0:
        return node.module or ""

    package = _module_package_parts(path)
    # level == 1 表示当前包；每多一级少取一段。
    keep = len(package) - (node.level - 1)
    base = package[: max(keep, 0)]
    if node.module:
        return ".".join((*base, *node.module.split(".")))
    return ".".join(base)


def _collect_imports(path: pathlib.Path) -> list[tuple[str, int]]:
    """收集一个文件里的全部 import 目标 ``(绝对模块名, 行号)``。

    同时覆盖模块级、函数内、``TYPE_CHECKING`` 分支里的 import；星号 import
    也按模块名记录。
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from(path, node)
            if module:
                imports.append((module, node.lineno))
    return imports


def _matches(module: str, prefixes: tuple[str, ...]) -> str | None:
    """返回第一个命中的前缀；``a.b`` 命中 ``a``，但 ``ab`` 不命中。"""
    for prefix in prefixes:
        if module == prefix or module.startswith(f"{prefix}."):
            return prefix
    return None


def _violations(
    directory: pathlib.Path,
    predicate,
    *,
    skip_module: str | None = None,
) -> list[str]:
    """对目录内每个文件执行 ``predicate(path, module) -> 违规原因 | None``。"""
    problems: list[str] = []
    for path in _iter_python_files(directory):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for module, lineno in _collect_imports(path):
            if skip_module and (module == skip_module or module.startswith(f"{skip_module}.")):
                continue
            reason = predicate(module)
            if reason:
                problems.append(f"{rel}:{lineno} imports `{module}` -> {reason}")
    return problems


def _assert_no_violations(label: str, problems: list[str]) -> None:
    assert not problems, (
        f"{label}\n"
        "架构约束被破坏（P14 design 第 4 节）：\n  - "
        + "\n  - ".join(problems)
    )


# --------------------------------------------------------------------------- #
# 规则 1 & 2：domain 框架无关，且只依赖标准库 + domain 自身
# --------------------------------------------------------------------------- #
def test_domain_does_not_import_forbidden_frameworks() -> None:
    """``app.domain`` 不得出现 fastapi/langchain/langgraph/chromadb/sqlalchemy。"""
    problems = _violations(
        APP_ROOT / "domain",
        lambda module: (
            f"使用了框架 `{_matches(module, FORBIDDEN_DOMAIN_FRAMEWORKS)}`"
            if _matches(module, FORBIDDEN_DOMAIN_FRAMEWORKS)
            else None
        ),
    )
    _assert_no_violations("app.domain 必须保持框架无关。", problems)


def test_domain_only_depends_on_stdlib_and_itself() -> None:
    """``app.domain`` 只允许 import 标准库与 ``app.domain`` 自身。"""
    stdlib = set(sys.stdlib_module_names)

    def _check(module: str) -> str | None:
        root = module.split(".", 1)[0]
        if module == "app" or module.startswith("app."):
            if module == "app.domain" or module.startswith("app.domain."):
                return None
            return "领域层不得依赖 app.domain 之外的应用内模块"
        if root in stdlib:
            return None
        return "领域层不得依赖第三方库"

    problems = _violations(APP_ROOT / "domain", _check)
    _assert_no_violations("app.domain 必须只依赖标准库与自身。", problems)


# --------------------------------------------------------------------------- #
# 规则 3：presentation 不得直接 import 具体持久化实现
# --------------------------------------------------------------------------- #
def test_presentation_does_not_import_concrete_persistence() -> None:
    """``app.presentation`` 不得直接 import 具体仓储 / 存储实现。"""
    problems = _violations(
        APP_ROOT / "presentation",
        lambda module: (
            f"绕过端口直接依赖具体实现 `{_matches(module, FORBIDDEN_PRESENTATION_PERSISTENCE)}`"
            if _matches(module, FORBIDDEN_PRESENTATION_PERSISTENCE)
            else None
        ),
    )
    _assert_no_violations("app.presentation 必须通过端口 / 容器访问持久化。", problems)


def test_presentation_does_not_import_mysql_repositories_or_task_store() -> None:
    """brief 里点名的两个具体实现必须被单独锁死。"""
    named = (
        "app.infrastructure.persistence.mysql_repositories",
        "app.infrastructure.persistence.mysql_task_store",
    )
    problems = _violations(
        APP_ROOT / "presentation",
        lambda module: (
            f"直接 import 了具名具体实现 `{_matches(module, named)}`"
            if _matches(module, named)
            else None
        ),
    )
    _assert_no_violations("表现层不得直接依赖具体 MySQL 仓储 / 任务存储。", problems)


# --------------------------------------------------------------------------- #
# 规则 4：application 不得反向依赖 presentation
# --------------------------------------------------------------------------- #
def test_application_does_not_import_presentation() -> None:
    problems = _violations(
        APP_ROOT / "application",
        lambda module: (
            "application 反向依赖了 presentation"
            if _matches(module, ("app.presentation",))
            else None
        ),
    )
    _assert_no_violations("app.application 不得依赖 app.presentation。", problems)


# --------------------------------------------------------------------------- #
# 规则 5：P14 结构稳定
# --------------------------------------------------------------------------- #
def test_p14_agents_layer_structure_is_in_place() -> None:
    """Agents 层拆分为 state / nodes / subagents / graph，且兼容入口仍在。"""
    expected = [
        "state.py",
        "graph.py",
        "main_graph.py",
        "runtime.py",
        "single_agent.py",
        "nodes/__init__.py",
        "nodes/planner.py",
        "nodes/compress.py",
        "nodes/preference.py",
        "nodes/main_agent.py",
        "nodes/routes.py",
        "nodes/trade_chain.py",
        "subagents/__init__.py",
        "subagents/base.py",
        "subagents/search.py",
        "subagents/trade.py",
    ]
    agents = APP_ROOT / "application" / "agents"
    missing = [name for name in expected if not (agents / name).is_file()]
    assert not missing, f"Agents 层结构缺失：{missing}"


def test_p14_presentation_layer_structure_is_in_place() -> None:
    """表现层拆分为 routers + dependencies，且只保留装配入口。"""
    expected = [
        "dependencies.py",
        "routers/__init__.py",
        "routers/intents.py",
        "routers/orders.py",
        "routers/feedback.py",
        "routers/sessions.py",
        "routers/metrics.py",
        "routers/dashboard.py",
        "routers/events.py",
    ]
    presentation = APP_ROOT / "presentation"
    missing = [name for name in expected if not (presentation / name).is_file()]
    assert not missing, f"表现层结构缺失：{missing}"


def test_main_graph_is_a_pure_compatibility_reexport() -> None:
    """``main_graph.py`` 不再定义实现，只 re-export 新模块里的公开名字。"""
    path = APP_ROOT / "application" / "agents" / "main_graph.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    leaked = sorted(defined & {
        "build_main_graph",
        "route_after_planner",
        "route_next_trade",
        "invoke_agent",
        "invoke_agent_stream",
        "MainState",
        "TradeSubState",
    })
    assert not leaked, (
        "main_graph.py 应保持纯兼容 re-export，但定义了实现："
        f"{leaked}"
    )
