import tempfile
from tools import read_text_file
import os
import pytest

def test_file_read():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8", delete=False) as f:
        f.write("这是临时文件")
    try:
        assert read_text_file(f.name) == "这是临时文件"
    finally:
        os.remove(f.name)

def test_file_read_empty():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8", delete=False) as f:
        pass
    try:
        with pytest.raises(ValueError, match="文件为空"): # 如果 read_text_file(f.name) 没有抛出 ValueError, 或者抛出的 ValueError 的信息不是 "文件为空"，那么测试就会失败
            read_text_file(f.name)
    finally:
        os.remove(f.name)

def test_file_not_found(tmp_path):
    file_path = tmp_path / "not_existing.txt"
    with pytest.raises(ValueError, match="未找到文件"):
        read_text_file(file_path)

def test_file_encoding_error(tmp_path): # tmp_path 是 pytest 提供的一个临时目录，用于在测试中创建临时文件
    file_path = tmp_path / "invalid.txt"
    with open(file_path, "wb") as f:
        f.write(b"\xff") # 以二进制写入一个无效的 UTF-8 字节序列
    with pytest.raises(ValueError, match="编码不匹配"):
        read_text_file(f.name)

        

