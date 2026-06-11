from mymod import process_data, validate_schema


def test_process_data_returns_input():
    result = process_data([1, 2, 3])
    assert result == [1, 2, 3]


def test_validate_schema_returns_true():
    assert validate_schema({"col": []}) is True
