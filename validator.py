from pathlib import Path

def validate_calculator_args(args):
    try:
        a = float(args.get("a"))
        b = float(args.get("b"))
        operation = args.get("operation")
        if operation not in ["+", "-", "*", "/"]:
            raise ValueError("Invalid operation. Must be one of: +, -, *, /.")
        return {"a": a, "b": b, "operation": operation}
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid calculator arguments: {e}")

def validate_text_length_args(args):
    text = args.get("text")
    if not isinstance(text, str): # 判断 text 是否为字符串类型
        raise ValueError("Invalid text_length argument: 'text' must be a string.")
    return {"text": text}

def validate_remove_whitespace_args(args):
    text = args.get("text")
    if not isinstance(text, str):
        raise ValueError("Invalid remove_whitespace argument: 'text' must be a string.")
    return {"text": text}

def validate_read_text_file_args(args):
    base_dir = (Path(__file__).resolve().parent / "data").resolve() # 获取当前文件所在目录的绝对路径，并在其下创建一个名为 "data" 的子目录的绝对路径
    file_path = args.get("file_path")
    if not isinstance(file_path, str):
        raise ValueError("Invalid read text argument: 'file_path' must be a string.")
    if not file_path.strip():
        raise ValueError("Invalid read text argument: 'file_path' cannot be empty.")
    
    target_path = (base_dir/file_path).resolve() # 获取目标文件的绝对路径

    if not target_path.is_relative_to(base_dir):
        raise ValueError("Invalid read text argument: 'file_path' must be within the 'data' directory.")
    return {"file_path": str(target_path)} 
    
