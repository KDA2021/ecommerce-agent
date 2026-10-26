"""Task context must carry references across planner and isolated subgraphs."""
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.application.agents.nodes.planner import make_planner_node
from app.application.agents.nodes.routes import route_after_planner, route_next_trade
from app.application.agents.subagents.base import PlannerDecision, SubTaskSpec


class CapturePlanner:
    def with_structured_output(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, messages):
        self.received = messages
        return PlannerDecision(route="trade", reasoning="buy selected item", tasks=[])


async def test_planner_receives_selected_product_and_summary():
    planner = CapturePlanner()
    node = make_planner_node(planner, SimpleNamespace(model_route_mode="llm"))
    await node({"messages": [
        HumanMessage(content="Find a suitcase"),
        AIMessage(content="First option: P1001 / P1001-S1"),
        HumanMessage(content="Buy the first one"),
    ], "summary": "Budget CNY200; destination CN"})
    text = "\n".join(str(m.content) for m in planner.received)
    assert "P1001-S1" in text
    assert "Budget CNY200" in text
    assert "Buy the first one" in text


async def test_trade_dispatch_carries_selected_item_context():
    task = SubTaskSpec(agent="trade_agent", instruction="Buy the first one")
    sends = await route_after_planner({
        "decision": PlannerDecision(route="trade", reasoning="buy", tasks=[task]).model_dump(),
        "messages": [AIMessage(content="First: P1001-S1")],
        "summary": "Ship to CN",
    })
    assert "P1001-S1" in sends[0].arg["summary"]
    assert "Ship to CN" in sends[0].arg["summary"]


async def test_next_trade_receives_previous_tool_result():
    sends = await route_next_trade({
        "trade_chain_step": {"task_id": "first", "chain_index": 0},
        "remaining_tasks": [{"agent": "trade_agent", "instruction": "Query the order just created"}],
        "task_failures": [],
        "messages": [ToolMessage(content='{"order_id":"GBX-000042"}', tool_call_id="create-1")],
    })
    assert "GBX-000042" in sends[0].arg["summary"]


async def test_main_synthesis_receives_original_user_request_after_child_answer():
    from app.application.agents.nodes.main_agent import make_main_agent_node

    class CaptureMain:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, messages):
            self.received = messages
            return AIMessage(content="P1001 costs CNY139")

    model = CaptureMain()
    node = make_main_agent_node(model)
    request = "Recommend a suitcase under CNY200"
    await node({
        "user_request": request,
        "decision": {"route": "search"},
        "messages": [HumanMessage(content=request), HumanMessage(content="Search travel category"),
                     AIMessage(content="Internal report: P1001 costs CNY139")],
    })
    assert model.received[-1].type == "human"
    assert request in model.received[-1].content
    assert any("Internal report: P1001 costs CNY139" in str(m.content) for m in model.received)
