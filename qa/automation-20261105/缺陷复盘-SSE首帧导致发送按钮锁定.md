# SSE 首帧缺失导致真实页面无法发送

发现日期：2026-11-05。来源：新增真实服务浏览器联调 LIVE-01～05，而非模拟 API/EventSource 用例。

## 问题与复现

通过本机 Vite 代理打开真实 React 页面，使用测试账户登录。后端已有 SSE 订阅队列，输入 `qa:create` 后，“发送”按钮始终 disabled；五条浏览器流程均在点击发送前超时。

预期：事件连接建立并可接收后，允许发送请求。实际：后端等待业务事件才发送响应体；前端等待 EventSource onopen 才允许发业务请求，两者相互等待。

## 定位过程

1. 失败 Trace、截图和 error-context 显示页面已登录，输入框有内容，发送按钮禁用，确认预览尚未出现。
2. 测试观察端点显示服务器有订阅，排除“没有建立任何订阅”的情况。
3. 查看前端：`eventsConnected` 只有 onopen 后才置 true，且是 canSend 的必要条件。
4. 查看后端：StreamingResponse 返回 broker 生成器；生成器注册队列后直接 `await queue.get()`，无业务事件时不发送首个响应体。
5. 在真实 HTTP 层新增首字节回归：200 后读取第一行应得到 ready 注释。修复前读取超时，红灯 JUnit 留在 `artifacts/initial-sse-failure/red-sse.xml`。

通过代理的浏览器路径中，响应头/连接就绪无法及时传达，而业务事件又依赖用户先发送请求。这解释了 mock EventSource 用例能够通过、真实联调全部卡住的差异。

## 实现

修改两个原有文件：

- `app/infrastructure/eventbroker.py`：新增 `subscribe_ready`，仍先把队列注册进订阅表，再立即发 `: connected\n\n` 注释；之后保持原业务帧格式。
- `app/presentation/routers/events.py`：真实 broker 优先使用 ready 订阅；简单 broker 适配器继续使用原有 subscribe。

先注册再发送 ready，避免客户端收到就绪信号后立刻发请求时丢首条事件。原有 subscribe 默认行为保留，继续让内部消费者从首条业务帧开始读取。

SSE 注释行会被浏览器作为非业务数据处理，格式依据 [MDN SSE 事件流说明](https://developer.mozilla.org/zh-CN/docs/Web/API/Server-sent_events/Using_server-sent_events)。本次只补连接首帧，不把它称为已经实现周期心跳、消息补偿或代理全场景兼容。

## 验证与证据

- 修复前：5 条真实服务浏览器用例都在 disabled 按钮处超时；首字节 HTTP 回归读取超时。
- 修复后：首帧相关回归和旧格式检查纳入 121 条 Python 套件，全部通过；5/5 真实服务 UI 和 8/8 模拟 UI 通过。详见 [最终汇总](./执行结果与覆盖矩阵.md)。
- 新增回归：真实 HTTP 的首行、ready 发送前订阅已注册、关闭后队列清理、旧订阅首帧行为兼容；保留原 test_api 中的 SSE 格式用例。
- 原始失败截图、Trace、HTML/JSON 和红灯 JUnit 保存在 `artifacts/initial-sse-failure/`；修复前的两个源文件保存在 `artifacts/before-sse-fix/`。

## 面试口述（约 45 秒）

> 我补真实浏览器联调时，发现模拟测试全部通过，但真实页面登录后一直不能发送。我先看 Trace，确认不是没找到按钮，而是它一直被禁用；再把前端连接状态与后端 SSE 生成器对照起来，发现前端等连接打开，服务端却等业务事件才发首个数据，两边形成等待。于是先写一个没有业务请求也必须收到 SSE 首字节的用例，确认修复前会超时，再让服务端注册订阅后立即发一个注释帧。最后同时验证首帧、旧事件格式和浏览器订单链路。这次让我意识到模拟测试能验证页面分支，但真实传输联调仍有独立价值。
