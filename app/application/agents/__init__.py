"""agents 子包：planner / main / search / trade LangGraph 图与上下文压缩。

P14 结构：
- ``state.py``：父图 / 子图 state schema 与 reducer；
- ``nodes/``：planner / compress / preference / main_agent / routes / trade_chain；
- ``subagents/``：search / trade 子图与子 Agent 容错节点工厂；
- ``graph.py``：主图装配（``build_main_graph``）；
- ``runtime.py``：``invoke_agent`` / ``invoke_agent_stream``；
- ``main_graph.py``：旧导入路径兼容 re-export。
"""
