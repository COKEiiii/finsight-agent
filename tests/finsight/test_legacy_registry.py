from agent import run_agent
from tool_executor import execute_tool


def test_injected_registry_is_isolated():
    registry = {"echo": {"function": lambda text: text, "validator": lambda args: args}}
    call = {"function": {"name": "echo", "arguments": {"text": "snapshot only"}}}
    assert execute_tool(call, registry)["content"] == "snapshot only"
    assert not execute_tool({"function": {"name": "calculator", "arguments": {}}}, registry)["success"]

    class Fake:
        calls = 0
        def chat(self, **kwargs):
            self.calls += 1
            return {"message": {"content": "", "tool_calls": [call]}} if self.calls == 1 else {"message": {"content": "done"}}
    result = run_agent("test", [], Fake(), "test", [], tool_registry=registry)
    assert result["status"] == "completed" and result["tool_call_count"] == 1
