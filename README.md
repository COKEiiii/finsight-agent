# AI Agent Basics

这是一个从零开始学习 AI Agent 的练习项目。

当前阶段只完成环境配置，暂时不包含 Agent 代码。后续会从最简单的模型调用开始，逐步学习：

1. 调用 LLM
2. 理解 messages
3. 设计 prompt
4. 让模型调用一个 Python function
5. 建立最简单的 Agent Loop

## 环境

- Python 3.11+
- Ollama
- 本地模型：`llama3.1:8b-instruct-q4_K_M`

## 使用环境

```bash
cd /Users/zhaolei/Developer/AI_agent_basics
source .venv/bin/activate
```

## 验证依赖

```bash
python -c "import ollama, dotenv; print('environment ready')"
```

## 当前状态

这是一个干净的起点。下一步将创建第一个最小 Python 文件，只完成一次 LLM 调用，不加入 tools、Agent Loop 或复杂抽象。
