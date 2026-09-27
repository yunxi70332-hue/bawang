from pathlib import Path
from scripts.apk_triage import apk_info

def test_triage_finds_flutter_application():
    apk = Path('apk/霸王茶姬.apk')
    if not apk.exists():
        return
    result = apk_info(apk)
    assert result['sha256'] == 'c4f2e82db1a51ae604a0ce05638fdc0a626a2de21fe5be5ded2a8a2b51e2959a'
    assert len(result['dex']) == 4
    assert result['flutter']['detected'] is True
    assert result['flutter']['contains_libapp'] is True
