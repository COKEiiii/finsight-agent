import os
from dotenv import load_dotenv
from ollama import Client
from tool_registry import get_llm_tools
from agent import run_agent
load_dotenv()

model = os.getenv("LLM_MODEL")
ollama_host = os.getenv("OLLAMA_HOST")

if not model:# 如果model没有值
    raise ValueError("LLM_MODEL not found")

client = Client(host=ollama_host)# 创建一个 Ollama 客户端对象

# message的创建放在循环外面，避免每次循环都创建新的message，使得上下文得以保留
messages = [
    {
        "role": "system",
        "content": "You are a helpful AI assistant. Please briefly reply to the user's question."
    }
]

while True:
    user_input = input("请输入您的问题：")
    if user_input.lower().strip() in ["exit", "quit"]:
        break


    result = run_agent(
        user_input=user_input,
        messages=messages,
        client=client,
        model=model,
        tools=get_llm_tools(), # 这里调用了get_llm_tools()函数来获取工具列表，但不实际使用这些工具，只是为了让agent知道有哪些工具可用
        max_llm_calls=10,
        max_tool_calls=8,
        think=True
    )
    if result["status"] == "completed":
        print("AI助手的回复：", result.get("content"))
    else:
        print("AI助手未能完成任务，停止原因：", result.get("stop_reason"))
    print("LLM调用次数：", result.get("llm_call_count"))
    print("工具调用次数：", result.get("tool_call_count"))