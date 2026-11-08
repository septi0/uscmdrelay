import pytest
from uscmdrelay.libraries import payload

def test_detects_json():
    assert payload.parse(b'  {"a": 1}\n') == ('json', {'a': 1})

def test_detects_env():
    assert payload.parse(b'a=1\nb=x=y\n\n') == ('env', {'a': '1', 'b': 'x=y'})

def test_empty_message():
    assert payload.parse(b' \n') == (None, {})

def test_env_ignores_comments_and_handles_crlf():
    assert payload.parse(b'# note\r\na=1\r\n') == ('env', {'a': '1'})

def test_env_keeps_quotes():
    assert payload.parse(b'a="b"') == ('env', {'a': '"b"'})

@pytest.mark.parametrize('raw', [b'{not json}', b'no separator', b'=value', b'\xff\xfe', b'{"a":' + b'[' * 200000])
def test_rejects_malformed(raw):
    with pytest.raises(payload.PayloadError):
        payload.parse(raw)

def test_rejects_huge_number():
    with pytest.raises(payload.PayloadError):
        payload.parse(b'{"a": ' + b'1' * 10000 + b'}')

def test_lookup():
    data = {'a': {'b': [{'c': 'x'}]}}

    assert payload.lookup(data, 'a.b.0.c') == 'x'

@pytest.mark.parametrize('path', ['z', 'a.z', 'a.b.5', 'a.b.x', 'a.b.0.c.d'])
def test_lookup_missing(path):
    with pytest.raises(KeyError):
        payload.lookup({'a': {'b': [{'c': 'x'}]}}, path)
