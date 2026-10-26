import pytest
from tools import analyze_csv

def test_analyze_csv_valid(tmp_path):
    # 1. 使用 tmp_path 构造 students.csv 文件路径
    file_path = tmp_path / "students.csv"
    # 2. 使用 write_text() 写入包含表头和两行数据的 CSV
    file_path.write_text(
        "name,age,gpa\nAlice,22,3.8\nBob,23,3.9\n",
        encoding="utf-8"
    )
    # 3. 调用 analyze_csv()
    result = analyze_csv(file_path)
    # 4. assert 返回的字典是否符合预期
    assert result == {
        "row_count" : 2,
        "column_count" : 3,
        "columns" : ['name', 'age', 'gpa']
    }

def test_analyze_csv_empty(tmp_path):
    file_path = tmp_path / "empty_file.csv"
    file_path.write_text(
        "",
        encoding="utf-8"
    )
    with pytest.raises(ValueError, match="CSV文件为空"):
        analyze_csv(file_path)

def test_analyze_csv_inconsistent_columns(tmp_path):
    file_path = tmp_path / "inconsistent_columns_file.csv"
    file_path.write_text(
        "name,age,gpa\nAlice,22,3.8\nBob,23\n",
        encoding="utf-8"
    )
    with pytest.raises(ValueError, match="CSV列数不一致"):
        analyze_csv(file_path)

def test_analyze_csv_header_only(tmp_path):
    file_path = tmp_path / "header_only.csv"
    file_path.write_text(
        "name,age,gpa",
        encoding="utf-8"
    )
    result = analyze_csv(file_path)
    assert result == {
        "row_count": 0,
        "column_count": 3,
        "columns": ["name", "age", "gpa"]
    }

def test_analyze_csv_quoted_comma(tmp_path):
    file_path = tmp_path / "csv_quoted.csv"
    file_path.write_text(
        "name,city,gpa\nAlice,\"New York, USA\",3.8\nBob,Singapore,3.9\n",
        encoding="utf-8"
    )
    result = analyze_csv(file_path)
    assert result == {
        "row_count": 2,
        "column_count": 3,
        "columns": ["name", "city", "gpa"]
    }