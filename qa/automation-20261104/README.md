# 电商项目自动化回归（2026-11-04 增补）

## 目标与分层

为项目补充 JD 相关的实际测试实践，使用 Python + pytest + requests 验证 HTTP 接口，Playwright 操作真实 React 页面。接口层使用项目真实 FastAPI 路由和内存确认管理器；Web 层替换 API/SSE 依赖，稳定复现错误与乱序。两者均不装配真实 Agent、数据库或 Redis，不消耗模型额度，不创建真实订单。

此目录是后续新增测试实践，不能追溯说成 2026.05-10 当时已完成。运行结果以 artifacts 为准，编写代码不代表测试通过。没有开展移动端自动化或压测。

## 场景矩阵

| ID | 方法 | 场景与判定 |
|---|---|---|
| API-01 | 冒烟 | health 的状态码与 JSON |
| API-02/03 | 等价类 | 确认/取消、默认动作、空白大小写归一化 |
| API-04/05 | 等价类/异常 | 缺字段、null、错误类型、非法动作、未知/空 Token；拒绝后合法预览仍可操作 |
| API-06 | 状态迁移 | 重复确认、确认后取消、取消后确认，首次决议不可覆盖 |
| API-07 | 错误推测 | 确认依赖缺失返回 503 |
| API-08 | 边界值 | X-Request-ID 长度 1/128/129，超限重新生成 |
| API-09 | 权限矩阵 | 无凭据/错凭据 401、他人 Token 404、本人可操作；拒绝不得消耗本人预览 |
| WEB-01 | 状态迁移 | 确认成功只表示等待执行，不表示订单成功 |
| WEB-02 | 故障注入 | 500 后错误可见、同 Token 重试 |
| WEB-03 | 状态迁移 | 挂起响应 + 重放预览，按钮不解锁、只发一次请求 |
| WEB-04/05 | 异常/分支 | Token 过期退出、取消动作正确 |
| WEB-06/07 | 事件关联 | 旧任务事件隔离、任务失败后迟到成功不能覆盖 |
| WEB-08 | 权限/隐私 | 登录前无事件连接、退出后会话不可见、密钥不写 localStorage |

## 执行（先征求项目所有者确认）

仓库根目录已有 `.venv`、前端依赖和本机 Chrome 时：

```powershell
# API：真实本机 HTTP + 内存业务状态
& .venv/Scripts/python.exe -m pytest -c qa/automation-20261104/pytest.ini qa/automation-20261104/api -q --junitxml=qa/automation-20261104/artifacts/api-junit.xml

# Web：在本目录 npm install 后 npm run test:web
# Codex 当前机器也可通过 QA_PLAYWRIGHT_TEST 指向已有运行库的 test.js，
# 使用对应 playwright/cli.js test，无需安装依赖或下载浏览器。
```

Playwright 自动启动仅绑定 127.0.0.1:5179 的 Vite，结束后关闭；API fixture 使用随机本机端口，结束后回收。外部 HTTP 在用例中禁止；Web 的 API 没有未配置的隐式兜底。

## 报告与定位

- API：JUnit XML，失败断言含 HTTP/JSON 差异。
- Web：JSON + HTML，失败保留 trace 与截图；禁止重试掩盖不稳定用例。
- 每个用例可按 ID 关联预期、断言、复现步骤。缺陷记录应填写实际复现、原因、修复和复测，不能以设计场景充当已发现 Bug。
- 本地通过不能等同真实 DB/LLM 集成通过、线上稳定性或 CI 已运行成功。

官方参考：[Playwright mock](https://playwright.dev/docs/mock)、[webServer](https://playwright.dev/docs/test-webserver)、[配置](https://playwright.dev/docs/test-configuration)、[pytest 参数化](https://docs.pytest.org/en/stable/how-to/parametrize.html)、[requests](https://requests.readthedocs.io/en/latest/user/quickstart/)。
