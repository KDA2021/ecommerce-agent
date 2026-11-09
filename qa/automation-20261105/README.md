# 自动化测试覆盖补齐（2026-11-05）

已执行验证：**146 条通过，其中新增 28 条、复用 118 条**。查看 [分层执行结果](./执行结果与覆盖矩阵.md) 与 [真实 SSE 缺陷复盘](./缺陷复盘-SSE首帧导致发送按钮锁定.md)。报告保留初始失败及最终成功，不只展示最后一次截图。

## 本轮方案与边界

旧 34 条作为确认接口与模拟 UI 基线保留。本轮整合已有的交易/授权测试，并新增多行库存、金额守恒、完整下单/取消、写入失败恢复、订单/会话越权以及真实 HTTP/SSE 浏览器联调。

浏览器联调保留真实 React 页面、Cookie 鉴权、FastAPI 路由、EventBroker/SSE、确认服务、订单工具、证据核验和订单业务代码。仅模型调度改为确定性 `qa:create` / `qa:cancel:订单号` 驱动，使用内存仓储，不调用 LLM。

`qa_harness.py` 是测试装配，生产不加载。`/__qa/*` 为带控制凭据的测试观察/故障注入端点，仅存在于本机 QA 服务，不是对外业务 API。真实 MySQL 由独立套件验证，不能用内存回滚证明数据库事务正确。

## 风险—用例矩阵

| 风险 | 新增/复用证据 | 断言 |
|---|---|---|
| 未确认或超时创建订单 | HTTP flow-02；旧 UI 基线 | 订单为空、库存不变、过期 Token 不可复用 |
| 确认与执行结果脱节 | HTTP flow-01；LIVE-01 | VERIFIED、订单真实可查询、金额与明细一致 |
| 同会话重试重复扣库存 | flow-03；LIVE-03；原并发同键测试 | 同一订单、仅一次库存变化 |
| 多行处理中途失败 | business-02；原事务测试 | 整体回滚，不残留订单或库存变化 |
| 已写入后抛错 | flow-05；LIVE-04；原 MySQL 故障测试 | 回滚后允许恢复，最终一条订单 |
| 总额和库存不守恒 | business-03、04；flow-01 | 明细金额之和、取消回补、不同键合法独立下单 |
| 越权确认/查询/取消/读会话 | flow-04、06、07；LIVE-05 | 401/403/404/422 与业务状态不变 |
| 授权并发消费或跨实例竞争 | 原 test_action_authorization / distributed_confirmation | 单一胜出、身份/参数绑定、超时边界 |
| 库存超卖与数据库幂等 | 原 test_order_transactions 的内存及 MySQL 分层 | 最后一件只一单、重复取消不重复回补 |
| 浏览器和 SSE 替身掩盖联调错误 | LIVE-01～05，flow-08 | 不替换 fetch/EventSource，真实 Cookie/HTTP/SSE，重连后下一轮可收敛 |

## 套件

- `offline`：旧 26 个 API + 新业务/HTTP 用例 + 原有交易、授权、fakeredis 跨实例测试；明确排除 `mysql` 场景。
- `--include-web`：重跑旧 8 个模拟 UI + 新 5 个真实服务 UI；保留各层独立计数。
- `mysql`：原项目真实数据库事务、并发及会话归属测试。只允许本轮独立容器的 `127.0.0.1:33067`，每个 fixture 再创建随机临时库并回收；不使用 .env 的已有数据库。

数量以 pytest 实际收集及报告为准，参数化展开后才统计，不预写“全部通过”。空套件、失败、错误、跳过、flaky 都不能通过归档门禁。

本轮最终分层为 121 Python / 12 MySQL / 8 模拟 UI / 5 真实服务 UI。SSE 缺陷修复后 Python 已完整执行；后续只改浏览器断言及运行器临时目录配置时，通过 `--web-only` 核验 Python 源码/配置指纹未变后复用成功 JUnit，再执行两层浏览器，报告明确记录 reused_from。

## 运行前确认清单

1. 先向用户说明当前待执行套件与依赖，并获得本轮确认。
2. 本地服务只使用临时内存和临时目录；浏览器服务占用 8009/5180，结束后关闭。
3. 如运行 MySQL，新建单独测试容器并绑定 33067，可能需要首次下载 mysql:8.4；只停止本轮创建的容器。
4. 不调用模型，不连接现有 MySQL/Redis，不推送代码或触发远端 CI。

## 执行与证据

从仓库根目录使用现有 Python 环境：

```powershell
& .venv/Scripts/python.exe qa/automation-20261105/run_suite.py --suite offline --include-web
# 需要设置 QA_NODE / QA_PLAYWRIGHT_CLI / QA_PLAYWRIGHT_TEST 为已有 Playwright 运行库。
# 独立测试 MySQL 就绪后：
& .venv/Scripts/python.exe qa/automation-20261105/run_suite.py --suite mysql
```

每轮在 `artifacts/时间-套件/` 保存 JUnit、日志、浏览器报告、summary 与源码 SHA256，latest 指针定位最近一轮。旧报告保留。失败先定位是业务缺陷、测试装配还是断言错误，再修正并回归，不把测试驱动问题包装成产品 Bug。

后续仍可继续覆盖真实模型规划、真实 Redis/多 worker 进程崩溃、部署代理下 SSE、跨浏览器和性能负载；这些范围不属于本轮本地结果的隐含结论。
