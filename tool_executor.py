from tool_registry import TOOL_REGISTRY


def execute_tool(tool_call, registry=None):
    registry = TOOL_REGISTRY if registry is None else registry
    tool_name = tool_call["function"]["name"]
    tool_args = tool_call["function"]["arguments"]
    tool_function = registry.get(tool_name, {}).get("function")
    tool_validator = registry.get(tool_name, {}).get("validator")

    if tool_function is None or tool_validator is None:
        return {
            "success": False,
            "content": f"Tool '{tool_name}' not found."
        }
    
    try:
        validated_args = tool_validator(tool_args) # 返回dict
        result = tool_function(**validated_args) # 将上面返回的dict展开作为关键字参数传入函数
        return {
            "success": True,
            "content": str(result)  # 将结果转换为字符串返回
        }

    except (ValueError, KeyError, TypeError) as e:
        return {
            "success": False,
            "content": str(e)  # 将异常信息转换为字符串返回
        }
