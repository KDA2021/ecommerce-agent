"""Read completed results and verify evidence. Never executes tests."""

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ART = HERE / "artifacts"


def load_suite(name):
    pointer = json.loads((ART / f"latest-{name}.json").read_text(encoding="utf-8"))
    path = Path(pointer["report"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["passed_gate"], f"{name} gate failed: {path}"
    tree = ET.parse(path.parent / "pytest.xml").getroot()
    cases = tree.findall(".//testcase")
    assert cases and not any(tree.findall(".//" + kind) for kind in ["failure", "error", "skipped"])
    return path, data, cases


def main():
    offline, odata, ocases = load_suite("offline")
    mysql, mdata, mcases = load_suite("mysql")
    assert len(ocases) == 121 and len(mcases) == 12
    ui = odata["web"]
    assert len(ui) == 2 and [x["stats"]["expected"] for x in ui] == [8, 5]
    assert all(x["passed_gate"] for x in ui)
    new_python = sum(
        c.attrib.get("classname", "").split(".")[-1]
        in ["test_business_invariants", "test_http_lifecycle", "test_sse_ready"]
        for c in ocases
    )
    assert new_python == 23
    # Preserve older deliverables; changed production SSE files are intentional.
    old = json.loads(
        (ROOT / "qa/automation-20261104/artifacts/provenance.json").read_text(encoding="utf-8")
    )
    for p, sha in old["file_sha256"].items():
        assert hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == sha, p
    delivery = json.loads(
        (ROOT.parent / "求职/自动化测试定向简历-20261104/交付核对.json").read_text(encoding="utf-8")
    )
    for p, sha in delivery["preserved_file_sha256"].items():
        assert hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha, p
    source = (
        list(HERE.glob("*.py"))
        + list(HERE.glob("*.mjs"))
        + list((HERE / "web").glob("*.mjs"))
        + [
            HERE / "suites.json",
            ROOT / "app/infrastructure/eventbroker.py",
            ROOT / "app/presentation/routers/events.py",
        ]
    )
    reports = [
        offline,
        mysql,
        offline.parent / "pytest.xml",
        mysql.parent / "pytest.xml",
        ART / "browser.json",
        ART / "mock-browser.json",
        ART / "initial-sse-failure/red-sse.xml",
    ]
    result = {
        "date": "2026-11-05",
        "offline_python": 121,
        "real_mysql": 12,
        "mock_browser": 8,
        "live_service_browser": 5,
        "total_passed": 146,
        "newly_written_cases": 28,
        "reused_cases": 118,
        "failure_regression": "SSE ready first-byte: red before fix, green after fix",
        "llm_calls": 0,
        "live_browser_storage": "in-memory; deterministic model driver",
        "mysql_boundary": "dedicated mysql:8.4 container on loopback 33067; random temporary databases",
        "remote_ci_executed": False,
        "previous_artifacts_unchanged": True,
        "reports": {"offline": str(offline), "mysql": str(mysql)},
        "sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source + reports
        },
    }
    (ART / "verified-summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HERE / "执行结果与覆盖矩阵.md").write_text(
        f"""# 补齐后的自动化验证（2026-11-05）

本轮实际通过 **146 条**：新增 28 条，复用已有 118 条，按独立测试项计数。没有把所有测试都写成新增成果。

| 层次 | 实际通过 | 真实依赖与范围 |
|---|---:|---|
| Python 业务/HTTP/SSE/授权 | 121 | 真路由、工具和业务；内存仓储及 fakeredis |
| MySQL 事务/并发/归属 | 12 | 独立 mysql:8.4 容器，每个 fixture 使用随机临时库 |
| 原有模拟浏览器回归 | 8 | 真 React/Chrome，API/EventSource 替身 |
| 新真实服务浏览器联调 | 5 | 真 React/Chrome/Cookie/HTTP/SSE/工具/业务，确定性模型驱动及内存仓储 |

各套件无失败、错误、跳过或 flaky。按配置排除的 MySQL/内存测试列在原日志的 deselected 中，不纳入通过分母。完整测试项与输入在各 JUnit 和 JSON 中可追溯。

## 新增内容与复用

- 新增 Python 23 条：9 条多行库存/金额与输入不变量、11 条真实 HTTP/SSE 业务流程、3 条 SSE 首帧/生命周期/旧接口兼容。
- 新增真实服务浏览器联调 5 条：确认创建查询取消、拒绝预览、同会话幂等重试、写入失败恢复、跨用户保护和登出。
- 复用原有确认 API、模拟 UI、并发库存/幂等、事务回滚、一次性授权、跨实例确认和真实 MySQL 归属等测试，执行范围见 suites.json。

## 实际发现并修复的缺陷

真实联调发现 SSE 在业务请求之前没有首个响应体，页面等待 onopen 导致发送按钮持续禁用。五条浏览器流程均能复现，独立首字节回归修复前超时。修复为先注册订阅再发送 ready 注释；修复后首字节、旧格式、完整业务与两层 UI 全部通过。

详细复盘：[SSE 首帧缺陷闭环](./缺陷复盘-SSE首帧导致发送按钮锁定.md)。初始失败 Trace、截图、JSON/HTML 和红灯 JUnit 保留在 artifacts/initial-sse-failure，未以新结果覆盖。

## 报告

- Python：[本轮汇总]({offline.relative_to(HERE).as_posix()})；[JUnit]({(offline.parent / "pytest.xml").relative_to(HERE).as_posix()})。
- MySQL：[本轮汇总]({mysql.relative_to(HERE).as_posix()})；[JUnit]({(mysql.parent / "pytest.xml").relative_to(HERE).as_posix()})。
- 浏览器：[模拟 UI HTML](./artifacts/mock-browser-report/index.html)、[真实服务 UI HTML](./artifacts/browser-report/index.html)。
- 总计和源码/结果指纹：[verified-summary.json](./artifacts/verified-summary.json)。

本轮不调用真实模型；真实服务浏览器仍使用内存数据，真实 MySQL 在独立套件验证。未运行远端 CI、真实 Redis 多进程故障、跨浏览器或性能负载。测试容器和临时服务按授权在结束时关闭；收尾状态另由系统端口与容器核对确认。

## 简历可用描述

> 围绕订单确认、创建、查询、取消及用户隔离建立分层回归，新增 28 条用例并整合验证 146 条；覆盖参数边界、并发幂等、事务回滚与真实 HTTP/SSE 浏览器联调。通过独立 MySQL 测试容器核验事务和归属；定位并修复 SSE 首帧缺失导致发送按钮锁定的问题，保留失败 Trace 与修复后回归证据。

该描述限定为 2026.11 补充实践；个人学习进度和本人负责程度需按实际掌握情况表达。
""",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ["sha256", "reports"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
