from tool_executor import execute_tool


def test_execute_calculator_success():
    tool_call = {
        "function": {
            "name": "calculator",
            "arguments": {
                "a": "10",
                "b": "5",
                "operation": "*"
            }
        }
    }

    result = execute_tool(tool_call)

    assert result["success"] is True
    assert result["content"] == "50.0"

def test_execute_calculator_divide_by_zero():
    tool_call = {
        "function": {
            "name": "calculator",
            "arguments": {
                "a": "10",
                "b": "0",
                "operation": "/"
            }
        }
    }

    result = execute_tool(tool_call)

    assert result["success"] is False
    assert result["content"] == "Cannot divide by zero."

def test_execute_unknown_tool():
    tool_call = {
        "function": {
            "name": "unknown_tool",
            "arguments": {}
        }
    }

    result = execute_tool(tool_call)

    assert result["success"] is False
    assert result["content"] == "Tool 'unknown_tool' not found."