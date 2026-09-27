from pathlib import Path

from scripts.extract_aot_crypto_flow import blocks


def test_recovered_interceptor_has_crypto_flow_anchors():
    source = Path('so_analysis/blutter_out/asm/chagee_base_network/network/chagee_interceptor.dart')
    if not source.exists():
        return
    result = blocks(source.read_text(encoding='utf-8', errors='replace'))
    assert result['_handleRequestPostBody']['address'] == '0xb48518'
    assert 'requestEncryptFields' in result['_handleRequestPostBody']['literals']
    assert 'responseEncryptFields' in result['_decryptResponseIfNeeded']['literals']
    assert any('Encrypter::decrypt64' in call for call in result['decryptFieldsInResponse']['calls'])
