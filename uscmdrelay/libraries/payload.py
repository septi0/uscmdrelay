import json
from typing import Any

__all__ = ['PayloadError', 'parse', 'parse_env', 'serialize', 'lookup']

class PayloadError(Exception):
    pass

def parse(raw: bytes) -> tuple[str | None, dict]:
    """ Parse a client message, returning its format (None for an empty message) and its fields. """
    try:
        text = raw.decode('utf-8').strip()
    except UnicodeDecodeError:
        raise PayloadError('Message is not valid UTF-8')

    if not text:
        return None, {}

    if text.startswith('{'):
        return 'json', _parse_json(text)

    return 'env', parse_env(text)

def parse_env(text: str) -> dict[str, str]:
    data = {}

    for line in text.split('\n'):
        line = line.rstrip('\r')

        if not line.strip() or line.startswith('#'):
            continue

        name, separator, value = line.partition('=')

        if not separator or not name:
            raise PayloadError('Invalid line in message, expected KEY=VALUE')

        data[name] = value

    return data

def serialize(format: str, data: dict) -> bytes:
    if format == 'json':
        return json.dumps(data).encode('utf-8')

    return '\n'.join(f'{name}={value}' for name, value in data.items()).encode('utf-8')

def lookup(data: Any, path: str) -> Any:
    """ Find a field by its dotted path, where numbers index arrays. Raises KeyError if it does not exist. """
    value = data

    for part in path.split('.'):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(path)

    return value

def _parse_json(text: str) -> dict:
    # deeply nested documents raise RecursionError and huge numbers a plain ValueError
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise PayloadError('Message is not valid JSON')
