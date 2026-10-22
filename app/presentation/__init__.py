# -*- coding: utf-8 -*-
"""presentation 层：FastAPI REST + SSE 接口。

结构：

- ``server.py``：``create_app`` 与 lifespan，只挂载 routers；
- ``dependencies.py``：共享依赖（鉴权 / 限流组合、容器读取）；
- ``routers/``：按业务域拆分的路由（intents / orders / feedback / sessions /
  metrics / dashboard / events）；
- ``auth.py`` / ``rate_limit.py``：鉴权与限流依赖实现；
- ``dashboard.py`` / ``sessions.py``：对应域的聚合 / 读取纯函数。
"""
