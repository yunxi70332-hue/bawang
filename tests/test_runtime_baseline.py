import json
from pathlib import Path


def test_runtime_baseline_has_no_raw_log_and_activity_started():
    records = sorted(Path('capture').glob('baseline-*.json'))
    logs = sorted(Path('capture').glob('*.sanitized.log'))
    if not records or not logs:
        return
    record = json.loads(records[-1].read_text(encoding='utf-8-sig'))
    text = logs[-1].read_text(encoding='utf-8', errors='replace')
    assert record['package_after']['package_fields']
    assert 'Displayed com.chagee.application.cn/.MainActivity' in text
    assert '隐私政策同意状态: false' in text
    assert not list(Path('capture').glob('*.raw.log'))
