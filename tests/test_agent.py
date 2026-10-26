from ollama import ChatResponse, Message

from agent import run_agent
from tools import calculator


class FakeClient:
    def chat(self, model, messages, tools):
        return {
            "message": {
                "content": "Hello!",
                "tool_calls": []
            }
        }
def test_agent_without_tool_call():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    result = run_agent(
        user_input="Hello",
        messages=messages,
        client=FakeClient(),
        model="fake-model",
        tools=[]
    )

    assert result["status"] == "completed"
    assert result["content"] == "Hello!"
    assert result["llm_call_count"] == 1
    assert result["tool_call_count"] == 0

class FakeToolClient:
    def __init__(self):
        self.call_count = 0
        self.received_messages = []
    def chat(self, model, messages, tools):
        self.received_messages.append(list(messages))
        self.call_count += 1
        if self.call_count == 1:
            return { # 这里相当于模拟LLM第一次调用返回的结果，里面包含了一个工具调用的请求-->calculator(a=10, b=5, operation="*")。Fake LLM 假装自己判断：“这个任务需要 calculator”。
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "calculator",
                            "arguments": {"a": "10", "b": "5", "operation": "*"}
                        }
                    }]
                }
            }
        else:
            return {
                "message": {
                    "content": "The result is 50.",
                    "tool_calls": []
                }
            }
def test_agent_with_tool_call():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    fake_tool_client = FakeToolClient()

    result = run_agent(
        user_input="Hello", # 模拟加入用户输入消息
        messages=messages,
        client=fake_tool_client,
        model="fake-model",
        tools=[calculator]
    )
    first_call_messages = fake_tool_client.received_messages[0]
    second_call_messages = fake_tool_client.received_messages[1]
    assistant_message = second_call_messages[-2]
    tool_args = assistant_message["tool_calls"][0]["function"]["arguments"]

    assert result["status"] == "completed"
    assert result["content"] == "The result is 50."
    assert result["llm_call_count"] == 2
    assert result["tool_call_count"] == 1
    assert fake_tool_client.call_count == 2

    assert second_call_messages[-1]["role"] == "tool"
    assert second_call_messages[-1]["content"] == "50.0"
    assert second_call_messages[-1]["tool_name"] == "calculator"
    assert first_call_messages[-1]["role"] == "user"
    assert first_call_messages[-1]["content"] == "Hello"

    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == ""
    assert assistant_message["tool_calls"][0]["function"]["name"] == "calculator"
    assert tool_args["a"] == "10"
    assert tool_args["b"] == "5"
    assert tool_args["operation"] == "*"

class FakeLoopClient:
    def __init__(self):
        self.call_count = 0
    def chat(self, model, messages, tools, **kwargs):
        self.call_count += 1
        return {
            "message": {
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "calculator",
                        "arguments": {"a": "10", "b": "5", "operation": "*"}
                    }
                }]
            }
        }

def test_agent_max_tool_calls_reached():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    fake_loop_client = FakeLoopClient()

    result = run_agent(
        user_input="Hello",
        messages=messages,
        client=fake_loop_client,
        model="fake-model",
        tools=[calculator],
        max_llm_calls=3,
        max_tool_calls=2
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "max_tool_calls_reached"
    assert result["llm_call_count"] == 3
    assert result["tool_call_count"] == 2
    assert fake_loop_client.call_count == 3

def test_agent_max_llm_calls_reached():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    fake_loop_client = FakeLoopClient()

    result = run_agent(
        user_input="Hello",
        messages=messages,
        client=fake_loop_client,
        model="fake-model",
        tools=[calculator],
        max_llm_calls=3,
        max_tool_calls=10
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "max_llm_calls_reached"
    assert result["llm_call_count"] == 3
    assert result["tool_call_count"] == 3
    assert fake_loop_client.call_count == 3

def test_agent_max_same_tool_repeats_reached():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    fake_loop_client = FakeLoopClient()

    result = run_agent(
        user_input="Hello",
        messages=messages,
        client=fake_loop_client,
        model="fake-model",
        tools=[calculator],
        max_llm_calls=4,
        max_tool_calls=10,
        max_same_tool_repeats=3
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "max_same_tool_repeats_reached"
    assert result["llm_call_count"] == 4
    assert result["tool_call_count"] == 3
    assert fake_loop_client.call_count == 4

class FakeErrorClient:
    def __init__(self):
        self.call_count = 0
    def chat(self, model, messages, tools, **kwargs):
        self.call_count += 1
        return {
            "message": {
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "calculator",
                        "arguments": {"a": "abc", "b": "5", "operation": "%"}
                    }
                }]
            }
        }
    
def test_agent_max_consecutive_tool_errors_reached():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    fake_error_client = FakeErrorClient()

    result = run_agent(
        user_input="Hello",
        messages=messages,
        client=fake_error_client,
        model="fake-model",
        tools=[calculator],
        max_llm_calls=4,
        max_tool_calls=10,
        max_same_tool_repeats=4,
        max_consecutive_tool_errors=3
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "max_consecutive_tool_errors_reached"
    assert result["llm_call_count"] == 3
    assert result["tool_call_count"] == 3
    assert fake_error_client.call_count == 3

class FakeInvalidResponseClient:
    def chat(self, model, messages, tools, **kwargs):
        return {
            "message": None
        }

def test_agent_handles_none_message():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    fake_invalid_response_client = FakeInvalidResponseClient()

    result = run_agent(
        user_input="Hello",
        messages=messages,
        client=fake_invalid_response_client,
        model="fake-model",
        tools=[calculator],
        max_llm_calls=3,
        max_tool_calls=10
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "invalid_llm_response"
    assert result["llm_call_count"] == 1
    assert result["tool_call_count"] == 0

class FakeInvalidRootResponseClient:
    def chat(self, model, messages, tools, **kwargs):
        return None

def test_agent_handles_invalid_root_response():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    fake_invalid_root_response_client = FakeInvalidRootResponseClient()

    result = run_agent(
        user_input="Hello",
        messages=messages,
        client=fake_invalid_root_response_client,
        model="fake-model",
        tools=[calculator],
        max_llm_calls=3,
        max_tool_calls=10
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "invalid_llm_response"
    assert result["llm_call_count"] == 1
    assert result["tool_call_count"] == 0


class FakeOllamaClient:
    def __init__(self):
        self.call_count = 0

    def chat(self, model, messages, tools, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            return ChatResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        Message.ToolCall(
                            function=Message.ToolCall.Function(
                                name="calculator",
                                arguments={"a": "10", "b": "5", "operation": "*"}
                            )
                        )
                    ]
                )
            )
        return ChatResponse(
            message=Message(role="assistant", content="The result is 50.")
        )


def test_agent_accepts_real_ollama_sdk_response_types():
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        }
    ]

    result = run_agent(
        user_input="What is 10 times 5?",
        messages=messages,
        client=FakeOllamaClient(),
        model="fake-model",
        tools=[calculator]
    )

    assert result["status"] == "completed"
    assert result["content"] == "The result is 50."
    assert result["llm_call_count"] == 2
    assert result["tool_call_count"] == 1
    assert messages[-1] == {
        "role": "assistant",
        "content": "The result is 50."
    }
