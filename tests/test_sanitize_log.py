from pathlib import Path
from scripts.sanitize_log import redact

def test_redacts_query_token_and_phone():
    text = 'GET /x?token=abc&phone=13800138000 Bearer abc.def ID 123e4567-e89b-12d3-a456-426614174000'
    clean = redact(text)
    assert 'abc.def' not in clean
    assert '13800138000' not in clean
    assert '<UUID>' in clean

def test_keeps_high_level_event():
    assert 'DioException [unknown]' in redact('DioException [unknown]: null')
