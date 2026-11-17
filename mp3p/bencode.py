"""Bencoding (BEP 3) — the serialisation used by .torrent files and the wire protocol."""

from __future__ import annotations

from typing import Any


class BencodeError(ValueError):
    pass


def decode(data: bytes | str, strict: bool = True) -> Any:
    if isinstance(data, str):
        data = data.encode("latin-1")
    value, index = _decode(data, 0, strict=strict)
    if strict and index != len(data):
        raise BencodeError(f"trailing data at offset {index}")
    return value


def decode_from(data: bytes, offset: int = 0, strict: bool = True) -> tuple[Any, int]:
    return _decode(data, offset, strict=strict)


def _decode(data: bytes, index: int, strict: bool) -> tuple[Any, int]:
    if index >= len(data):
        raise BencodeError("unexpected end of data")
    char = data[index:index + 1]

    if char == b"i":
        end = data.find(b"e", index + 1)
        if end < 0:
            raise BencodeError("unterminated integer")
        raw = data[index + 1:end]
        if not raw:
            raise BencodeError("empty integer")
        try:
            return int(raw), end + 1
        except ValueError as exc:
            raise BencodeError(f"bad integer {raw!r}") from exc

    if char == b"l":
        index += 1
        out = []
        while True:
            if index >= len(data):
                raise BencodeError("unterminated list")
            if data[index:index + 1] == b"e":
                return out, index + 1
            item, index = _decode(data, index, strict)
            out.append(item)

    if char == b"d":
        index += 1
        out_dict = {}
        while True:
            if index >= len(data):
                raise BencodeError("unterminated dict")
            if data[index:index + 1] == b"e":
                return out_dict, index + 1
            key, index = _decode(data, index, strict)
            if not isinstance(key, bytes):
                raise BencodeError("dict key must be a byte string")
            value, index = _decode(data, index, strict)
            out_dict[key] = value

    if char.isdigit():
        colon = data.find(b":", index)
        if colon < 0:
            raise BencodeError("unterminated string length")
        try:
            length = int(data[index:colon])
        except ValueError as exc:
            raise BencodeError("bad string length") from exc
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("string runs past end of data")
        return data[start:end], end

    raise BencodeError(f"unexpected byte {char!r} at offset {index}")


def encode(value: Any) -> bytes:
    out = bytearray()
    _encode(value, out)
    return bytes(out)


def _encode(value: Any, out: bytearray) -> None:
    if isinstance(value, bool):
        out += b"i1e" if value else b"i0e"
    elif isinstance(value, int):
        out += b"i%de" % value
    elif isinstance(value, bytes):
        out += b"%d:" % len(value) + value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
        out += b"%d:" % len(raw) + raw
    elif isinstance(value, (list, tuple)):
        out += b"l"
        for item in value:
            _encode(item, out)
        out += b"e"
    elif isinstance(value, dict):
        out += b"d"
        items = []
        for key, item in value.items():
            raw_key = key.encode("utf-8") if isinstance(key, str) else key
            items.append((raw_key, item))
        for raw_key, item in sorted(items, key=lambda kv: kv[0]):
            _encode(raw_key, out)
            _encode(item, out)
        out += b"e"
    elif value is None:
        raise BencodeError("cannot bencode None")
    else:
        raise BencodeError(f"cannot bencode {type(value)!r}")
