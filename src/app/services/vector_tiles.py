"""A reader for Mapbox Vector Tiles (MVT 2.1), as much of one as reading a layer's features
takes.

The seafloor's coastline is the only caller (`services.seafloor`), and it wants one layer's
polygons and one property of each, so this is a protobuf walker over the tile's bytes rather
than a dependency: the format is three nested messages and a command stream. It reads what
`@mapbox/vector-tile` reads, the same way - the web app read these tiles with it first - and
skips every other layer without decoding it.
"""

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# Protobuf wire types.
_VARINT = 0
_FIXED64 = 1
_LENGTH = 2
_FIXED32 = 5

# Geometry commands.
_MOVE_TO = 1
_LINE_TO = 2
_CLOSE_PATH = 7

POLYGON = 3

type Value = str | float | int | bool | None


@dataclass(frozen=True, slots=True)
class Feature:
    # 0 unknown, 1 point, 2 line, 3 polygon.
    type: int
    properties: dict[str, Value]
    # The command stream, still encoded: most features a caller filters out are never drawn.
    _geometry: list[int]

    def rings(self) -> list[list[tuple[int, int]]]:
        """The feature's rings (or lines, or points), in the tile's pixel coordinates. A ring
        closed by `ClosePath` repeats its first point at the end, as `loadGeometry` does."""
        lines: list[list[tuple[int, int]]] = []
        line: list[tuple[int, int]] | None = None
        x = y = 0
        command = length = 0
        i = 0
        stream = self._geometry
        while i < len(stream):
            if length <= 0:
                command, length = stream[i] & 0x7, stream[i] >> 3
                i += 1
            length -= 1
            if command in (_MOVE_TO, _LINE_TO):
                if i + 1 >= len(stream):
                    raise ValueError("vector tile geometry ends inside a point")
                x += _zigzag(stream[i])
                y += _zigzag(stream[i + 1])
                i += 2
                if command == _MOVE_TO:
                    if line is not None:
                        lines.append(line)
                    line = []
                if line is not None:
                    line.append((x, y))
            elif command == _CLOSE_PATH:
                if line:
                    line.append(line[0])
            else:
                raise ValueError(f"unknown vector tile command {command}")
        if line is not None:
            lines.append(line)
        return lines


@dataclass(frozen=True, slots=True)
class Layer:
    name: str
    extent: int
    features: list[Feature]


def read_layer(data: bytes, name: str) -> Layer | None:
    """The tile's layer called `name`, or None when it has none, or none with a feature in it.
    Where two layers share the name, the later one wins."""
    found: Layer | None = None
    for field, wire, value in _fields(data, 0, len(data)):
        if field == 3 and wire == _LENGTH:
            start, end = value
            if _layer_name(data, start, end) == name:
                layer = _read_layer(data, start, end)
                if layer.features:
                    found = layer
    return found


def _layer_name(data: bytes, start: int, end: int) -> str | None:
    for field, wire, value in _fields(data, start, end):
        if field == 1 and wire == _LENGTH:
            return _text(data, value)
    return None


def _read_layer(data: bytes, start: int, end: int) -> Layer:
    name = ""
    extent = 4096
    keys: list[str] = []
    values: list[Value] = []
    raw_features: list[tuple[int, int]] = []
    for field, wire, value in _fields(data, start, end):
        if field == 1 and wire == _LENGTH:
            name = _text(data, value)
        elif field == 5 and wire == _VARINT:
            extent = value
        elif field == 2 and wire == _LENGTH:
            raw_features.append(value)
        elif field == 3 and wire == _LENGTH:
            keys.append(_text(data, value))
        elif field == 4 and wire == _LENGTH:
            values.append(_read_value(data, *value))
    features = [_read_feature(data, s, e, keys, values) for s, e in raw_features]
    return Layer(name=name, extent=extent, features=features)


def _read_feature(data: bytes, start: int, end: int, keys: list[str], values: list[Value]) -> Feature:
    kind = 0
    tags: list[int] = []
    geometry: list[int] = []
    for field, wire, value in _fields(data, start, end):
        if field == 2 and wire == _LENGTH:
            tags = _packed(data, *value)
        elif field == 3 and wire == _VARINT:
            kind = value
        elif field == 4 and wire == _LENGTH:
            geometry = _packed(data, *value)
    properties: dict[str, Value] = {}
    for i in range(0, len(tags) - 1, 2):
        if tags[i] < len(keys) and tags[i + 1] < len(values):
            properties[keys[tags[i]]] = values[tags[i + 1]]
    return Feature(type=kind, properties=properties, _geometry=geometry)


def _read_value(data: bytes, start: int, end: int) -> Value:
    value: Value = None
    for field, wire, raw in _fields(data, start, end):
        if field == 1 and wire == _LENGTH:
            value = _text(data, raw)
        elif field == 2 and wire == _FIXED32:
            value = struct.unpack("<f", raw)[0]
        elif field == 3 and wire == _FIXED64:
            value = struct.unpack("<d", raw)[0]
        elif field == 4 and wire == _VARINT:
            value = raw - (1 << 64) if raw >= 1 << 63 else raw
        elif field == 5 and wire == _VARINT:
            value = raw
        elif field == 6 and wire == _VARINT:
            value = _zigzag(raw)
        elif field == 7 and wire == _VARINT:
            value = bool(raw)
    return value


def _fields(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, Any]]:
    """`(field number, wire type, value)` for each field of the message in `data[start:end]`:
    an int for a varint, the bytes of a fixed32 or fixed64, and `(start, end)` of a
    length-delimited field's payload."""
    pos = start
    while pos < end:
        key, pos = _varint(data, pos)
        field, wire = key >> 3, key & 0x7
        if wire == _VARINT:
            value, pos = _varint(data, pos)
            yield field, wire, value
        elif wire == _LENGTH:
            length, pos = _varint(data, pos)
            if pos + length > end:
                raise ValueError("vector tile field runs past its message")
            yield field, wire, (pos, pos + length)
            pos += length
        elif wire == _FIXED32:
            if pos + 4 > end:
                raise ValueError("vector tile field runs past its message")
            yield field, wire, data[pos : pos + 4]
            pos += 4
        elif wire == _FIXED64:
            if pos + 8 > end:
                raise ValueError("vector tile field runs past its message")
            yield field, wire, data[pos : pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("vector tile ends inside a varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("vector tile varint is too long")


def _packed(data: bytes, start: int, end: int) -> list[int]:
    values = []
    pos = start
    while pos < end:
        value, pos = _varint(data, pos)
        values.append(value)
    return values


def _zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _text(data: bytes, span: tuple[int, int]) -> str:
    return data[span[0] : span[1]].decode("utf-8", errors="replace")
