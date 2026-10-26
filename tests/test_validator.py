from validator import (
    validate_calculator_args,
    validate_remove_whitespace_args,
    validate_text_length_args,
    validate_read_text_file_args
)
import pytest
from pathlib import Path
import validator

def test_validate_calculator_args_valid():
    result = validate_calculator_args(
        {
            "a": "10",
            "b": "5",
            "operation": "*"
        }
    )

    assert result == {
        "a": 10.0,
        "b": 5.0,
        "operation": "*"
    }

def test_validate_text_length_args_valid():
    result = validate_text_length_args(
        {
            "text": "Hello, world!"
        }
    )

    assert result == {
        "text": "Hello, world!"
    }

def test_validate_remove_whitespace_args_valid():
    result = validate_remove_whitespace_args(
        {
            "text": "   Hello, world!   "
        }
    )

    assert result == {
        "text": "   Hello, world!   "
    }

def test_validate_calculator_args_invalid_operation():
    with pytest.raises(ValueError): # 我预期下面这段代码必须抛出 ValueError
        validate_calculator_args(
            {
                "a": "10",
                "b": "5",
                "operation": "%"
            }
        )

def test_validate_calculator_args_invalid_a():
    with pytest.raises(ValueError):
        validate_calculator_args(
            {
                "a": "abc",
                "b": "5",
                "operation": "*"
            }
        )

def test_validate_read_text_file_args_valid():
    args = {"file_path": "example.txt"}
    result = validate_read_text_file_args(args)

    actual_path = Path(result["file_path"])
    assert actual_path.is_absolute()

    project_root = Path(__file__).resolve().parent.parent
    expected_path = (project_root/"data"/"example.txt").resolve()
    assert actual_path == expected_path

def test_validate_read_text_file_args_path_traversal():
    args = {"file_path": "../.env"}

    with pytest.raises(ValueError, match="must be within"):
        validate_read_text_file_args(args)

def test_validate_read_text_file_args_absolute_path():
    args = {"file_path": "/etc/passwd"}

    with pytest.raises(ValueError, match="must be within"):
        validate_read_text_file_args(args)

def test_validate_read_text_file_args_empty():
    args = {"file_path": ""}

    with pytest.raises(ValueError, match="cannot be empty"):
        validate_read_text_file_args(args)

def test_validate_read_text_file_args_invalid_type():
    args = {"file_path": 123}

    with pytest.raises(ValueError, match="must be a string"):
        validate_read_text_file_args(args)

def test_validate_read_text_file_args_whitespace():
    args = {"file_path": "   "}

    with pytest.raises(ValueError, match="cannot be empty"):
        validate_read_text_file_args(args)

def test_symlink_resolution(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("secret", encoding="utf-8")

    link_path = data_dir / "shortcut.txt"
    link_path.symlink_to(secret_path)
    actual_path = link_path.resolve()
    assert not actual_path.is_relative_to(data_dir)
    assert actual_path == secret_path

def test_validate_read_text_file_args_symlink(tmp_path, monkeypatch):

    # 1. 临时改变 validator.__file__
    fake_file = tmp_path / "validator.py"
    monkeypatch.setattr(validator, "__file__", str(fake_file))
    # 2. 在 tmp_path 下创建 data 目录
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 3. 在 tmp_path 下创建 secret.txt
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("secret", encoding="utf-8")
    # 4. 在 data 目录下创建 shortcut.txt 符号链接
    link_path = data_dir / "shortcut.txt"
    link_path.symlink_to(secret_path)
    # 5. 构造 LLM 参数
    args = {"file_path": "shortcut.txt"}

    # 6. 验证 Validator 抛出 ValueError
    with pytest.raises(ValueError, match="must be within"):
        validate_read_text_file_args(args)