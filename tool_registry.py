from tools import calculator, text_length, remove_whitespace, read_text_file
from validator import (
    validate_calculator_args,
    validate_text_length_args,
    validate_remove_whitespace_args,
    validate_read_text_file_args
)

TOOL_REGISTRY = {
    "calculator": {
        "function": calculator,
        "validator": validate_calculator_args
    },
    "text_length": {
        "function": text_length,
        "validator": validate_text_length_args
    },
    "remove_whitespace": {
        "function": remove_whitespace,
        "validator": validate_remove_whitespace_args
    },
    "read_text_file": {
        "function": read_text_file,
        "validator": validate_read_text_file_args
    }
}

def get_llm_tools(): # 用于获取所有注册的工具函数，在app.py中调用这个函数来获取工具列表，但不实际使用这些工具，只是为了让agent知道有哪些工具可用
    return [
        config["function"]
        for config in TOOL_REGISTRY.values()
    ]