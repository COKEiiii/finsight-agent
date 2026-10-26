import csv

def calculator(a: float, b: float, operation: str) -> float:
    """
    Perform arithmetic calculations on numeric values.
    Only use this tool when the user explicitly requests a mathematical calculation.
    Do not use this tool for general conversation, memory, names, or non-mathematical questions.

    Args:
        a: The first number.
        b: The second number.
        operation: One of "+", "-", "*", "/".
    """
    if operation == '+':
        return a + b
    elif operation == '-':
        return a - b
    elif operation == '*':
        return a * b
    elif operation == '/':
        if b != 0:
            return a / b
        else:
            raise ValueError("Cannot divide by zero.")
    else:
        raise ValueError("Invalid operation. Please choose from '+', '-', '*', or '/'.")

def text_length(text: str) -> int:
    """
    Calculate the length of a given text string.
    Only use this tool when the user explicitly requests to know the length of a text.
    Do not use this tool for general conversation, memory, names, or non-text-length-related questions.

    Args:
        text: The input text string.
    """
    return len(text)

def remove_whitespace(text: str) -> str:
    """
    Remove all whitespace characters from a given text string.
    Only use this tool when the user explicitly requests to remove whitespace from a text.
    Do not use this tool for general conversation, memory, names, or non-whitespace-related questions.

    Args:
        text: The input text string.
    """
    return ''.join(text.split())

def read_text_file(file_path: str) -> str:
    """
    Read the contents of a UTF-8 encoded text file.
    Only use this tool when the user explicitly requests to read or inspect a local text file.
    Do not use this tool for general conversation or to access files outside the project's data directory.

    Args:
        file_path: The relative path to a file within the project's data directory,
            such as "example.txt" or "reports/notes.txt". Do not include the "data/" prefix.
    """

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read()
            if not content:
                raise ValueError("文件为空")
        return content
    except FileNotFoundError:
        raise ValueError("未找到文件")
    except UnicodeDecodeError:
        raise ValueError("编码不匹配,无法解码")

def analyze_csv(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        # 1. 读取表头
        headers = next(reader, None)
        if not headers:
            raise ValueError("CSV文件为空或缺少表头")
        # 2. 计算列数
        column_count = len(headers)
        # 3. 遍历剩余的数据行，统计行数
        row_count = 0
        for row in reader:
            if len(row) != column_count:
                raise ValueError(
                    f"CSV列数不一致：预期{column_count}列，实际{len(row)}列"
                )
            row_count += 1
    # 4. 返回包含三个字段的字典
    return {
        "row_count" : row_count,
        "column_count" : column_count,
        "columns" : headers
    }