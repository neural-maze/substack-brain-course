"""Lightweight async Bolt v4.x wire-protocol fallback for Memgraph.

When the `neo4j` Python driver is installed, `kb/graph/client.py` uses
`neo4j.AsyncGraphDatabase` directly. When running in an existing `.venv`
before `uv sync` installs `neo4j`, this module provides a drop-in async
Bolt v4 client over `asyncio` sockets so real parameterized Cypher queries
against Memgraph (`bolt://localhost:7687`) work out of the box with zero
external dependencies.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any
from urllib.parse import urlsplit

BOLT_MAGIC = b"\x60\x60\xb0\x17"
# Propose Bolt 4.3, 4.1, 4.0, 3.0
BOLT_VERSIONS = b"\x00\x00\x03\x04\x00\x00\x01\x04\x00\x00\x00\x04\x00\x00\x00\x03"

TAG_HELLO = 0x01
TAG_GOODBYE = 0x02
TAG_RESET = 0x0F
TAG_RUN = 0x10
TAG_PULL = 0x3F
TAG_SUCCESS = 0x70
TAG_RECORD = 0x71
TAG_FAILURE = 0x7F


def _pack(value: Any) -> bytes:
    if value is None:
        return b"\xc0"
    if isinstance(value, bool):
        return b"\xc3" if value else b"\xc2"
    if isinstance(value, int):
        if -16 <= value <= 127:
            return struct.pack(">b", value)
        if -128 <= value <= -17:
            return b"\xc8" + struct.pack(">b", value)
        if -32768 <= value <= 32767:
            return b"\xc9" + struct.pack(">h", value)
        if -2147483648 <= value <= 2147483647:
            return b"\xca" + struct.pack(">i", value)
        return b"\xcb" + struct.pack(">q", value)
    if isinstance(value, float):
        return b"\xc1" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        n = len(raw)
        if n < 16:
            return bytes([0x80 | n]) + raw
        if n < 256:
            return b"\xd0" + struct.pack(">B", n) + raw
        if n < 65536:
            return b"\xd1" + struct.pack(">H", n) + raw
        return b"\xd2" + struct.pack(">I", n) + raw
    if isinstance(value, (list, tuple)):
        items = b"".join(_pack(v) for v in value)
        n = len(value)
        if n < 16:
            return bytes([0x90 | n]) + items
        if n < 256:
            return b"\xd4" + struct.pack(">B", n) + items
        if n < 65536:
            return b"\xd5" + struct.pack(">H", n) + items
        return b"\xd6" + struct.pack(">I", n) + items
    if isinstance(value, dict):
        pairs = b"".join(_pack(str(k)) + _pack(v) for k, v in value.items())
        n = len(value)
        if n < 16:
            return bytes([0xA0 | n]) + pairs
        if n < 256:
            return b"\xd8" + struct.pack(">B", n) + pairs
        if n < 65536:
            return b"\xd9" + struct.pack(">H", n) + pairs
        return b"\xda" + struct.pack(">I", n) + pairs
    return _pack(str(value))


def _pack_struct(tag: int, *fields: Any) -> bytes:
    n = len(fields)
    header = bytes([0xB0 | n, tag]) if n < 16 else b"\xdc" + struct.pack(">BB", n, tag)
    return header + b"".join(_pack(f) for f in fields)


class _Unpacker:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read_byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_bytes(self, n: int) -> bytes:
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def unpack(self) -> Any:
        marker = self.read_byte()
        if marker == 0xC0:
            return None
        if marker == 0xC2:
            return False
        if marker == 0xC3:
            return True
        if marker == 0xC1:
            return struct.unpack(">d", self.read_bytes(8))[0]
        if marker == 0xC8:
            return struct.unpack(">b", self.read_bytes(1))[0]
        if marker == 0xC9:
            return struct.unpack(">h", self.read_bytes(2))[0]
        if marker == 0xCA:
            return struct.unpack(">i", self.read_bytes(4))[0]
        if marker == 0xCB:
            return struct.unpack(">q", self.read_bytes(8))[0]
        if marker < 0x80 or marker >= 0xF0:
            return struct.unpack(">b", bytes([marker]))[0]
        # Strings
        if 0x80 <= marker <= 0x8F:
            return self.read_bytes(marker & 0x0F).decode("utf-8")
        if marker == 0xD0:
            n = struct.unpack(">B", self.read_bytes(1))[0]
            return self.read_bytes(n).decode("utf-8")
        if marker == 0xD1:
            n = struct.unpack(">H", self.read_bytes(2))[0]
            return self.read_bytes(n).decode("utf-8")
        if marker == 0xD2:
            n = struct.unpack(">I", self.read_bytes(4))[0]
            return self.read_bytes(n).decode("utf-8")
        # Lists
        if 0x90 <= marker <= 0x9F:
            return [self.unpack() for _ in range(marker & 0x0F)]
        if marker == 0xD4:
            n = struct.unpack(">B", self.read_bytes(1))[0]
            return [self.unpack() for _ in range(n)]
        if marker == 0xD5:
            n = struct.unpack(">H", self.read_bytes(2))[0]
            return [self.unpack() for _ in range(n)]
        if marker == 0xD6:
            n = struct.unpack(">I", self.read_bytes(4))[0]
            return [self.unpack() for _ in range(n)]
        # Maps
        if 0xA0 <= marker <= 0xAF:
            return {self.unpack(): self.unpack() for _ in range(marker & 0x0F)}
        if marker == 0xD8:
            n = struct.unpack(">B", self.read_bytes(1))[0]
            return {self.unpack(): self.unpack() for _ in range(n)}
        if marker == 0xD9:
            n = struct.unpack(">H", self.read_bytes(2))[0]
            return {self.unpack(): self.unpack() for _ in range(n)}
        if marker == 0xDA:
            n = struct.unpack(">I", self.read_bytes(4))[0]
            return {self.unpack(): self.unpack() for _ in range(n)}
        # Structs (Nodes, Relationships, Messages)
        if 0xB0 <= marker <= 0xBF:
            size = marker & 0x0F
            tag = self.read_byte()
            fields = [self.unpack() for _ in range(size)]
            return self._convert_struct(tag, fields)
        if marker == 0xDC:
            size = struct.unpack(">B", self.read_bytes(1))[0]
            tag = self.read_byte()
            fields = [self.unpack() for _ in range(size)]
            return self._convert_struct(tag, fields)
        raise ValueError(f"Unsupported PackStream marker: 0x{marker:02x}")

    @staticmethod
    def _convert_struct(tag: int, fields: list[Any]) -> Any:
        # Node struct ('N' == 0x4E): (id, labels, properties)
        if tag == 0x4E and len(fields) >= 3 and isinstance(fields[2], dict):
            return fields[2]
        # Relationship struct ('R' == 0x52): (id, start_id, end_id, type, properties)
        if tag == 0x52 and len(fields) >= 5 and isinstance(fields[4], dict):
            return fields[4]
        return (tag, fields)


async def _send_message(writer: asyncio.StreamWriter, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        chunk = view[offset : offset + 32767]
        writer.write(struct.pack(">H", len(chunk)) + bytes(chunk))
        offset += len(chunk)
    writer.write(b"\x00\x00")
    await writer.drain()


async def _read_message(reader: asyncio.StreamReader) -> tuple[int, list[Any]]:
    chunks = bytearray()
    while True:
        header = await reader.readexactly(2)
        (size,) = struct.unpack(">H", header)
        if size == 0:
            break
        chunks.extend(await reader.readexactly(size))
    result = _Unpacker(bytes(chunks)).unpack()
    if isinstance(result, tuple) and len(result) == 2:
        return int(result[0]), list(result[1])
    raise RuntimeError(f"Unexpected Bolt message frame: {result!r}")


class BoltResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def single(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class BoltSession:
    def __init__(self, host: str, port: int, user: str, password: str) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._major_version: int = 4

    async def __aenter__(self) -> BoltSession:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), timeout=5.0
        )
        self._reader, self._writer = reader, writer
        writer.write(BOLT_MAGIC + BOLT_VERSIONS)
        await writer.drain()
        version_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=5.0)
        self._major_version = version_bytes[3]
        if self._major_version == 0:
            raise RuntimeError("Memgraph Bolt handshake failed: no agreed protocol version.")

        auth: dict[str, Any] = {"user_agent": "substack-brain/0.1", "scheme": "none"}
        if self._user:
            auth = {
                "user_agent": "substack-brain/0.1",
                "scheme": "basic",
                "principal": self._user,
                "credentials": self._password,
            }
        await _send_message(writer, _pack_struct(TAG_HELLO, auth))
        tag, fields = await _read_message(reader)
        if tag != TAG_SUCCESS:
            raise RuntimeError(f"Memgraph HELLO failed: {fields}")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._writer is not None:
            try:
                await _send_message(self._writer, _pack_struct(TAG_GOODBYE))
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None, **kw: Any
    ) -> BoltResult:
        assert self._reader is not None and self._writer is not None
        params: dict[str, Any] = {**(parameters or {}), **kw}
        await _send_message(self._writer, _pack_struct(TAG_RUN, query, params, {}))
        tag, fields = await _read_message(self._reader)
        if tag == TAG_FAILURE:
            err = fields[0] if fields else {}
            await _send_message(self._writer, _pack_struct(TAG_RESET))
            await _read_message(self._reader)
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"Cypher error: {msg}")

        meta = fields[0] if fields and isinstance(fields[0], dict) else {}
        col_names: list[str] = list(meta.get("fields", []))

        pull_msg = (
            _pack_struct(TAG_PULL, {"n": -1})
            if self._major_version >= 4
            else _pack_struct(TAG_PULL)
        )
        await _send_message(self._writer, pull_msg)

        rows: list[dict[str, Any]] = []
        while True:
            r_tag, r_fields = await _read_message(self._reader)
            if r_tag == TAG_RECORD:
                values = r_fields[0] if r_fields and isinstance(r_fields[0], list) else []
                rows.append(dict(zip(col_names, values, strict=False)))
            elif r_tag == TAG_SUCCESS:
                break
            elif r_tag == TAG_FAILURE:
                err = r_fields[0] if r_fields else {}
                await _send_message(self._writer, _pack_struct(TAG_RESET))
                await _read_message(self._reader)
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(f"Cypher pull error: {msg}")
        return BoltResult(rows)


class FallbackBoltDriver:
    """AsyncDriver-compatible wrapper around `BoltSession`."""

    def __init__(self, uri: str, auth: tuple[str, str] = ("", "")) -> None:
        parts = urlsplit(uri)
        self._host = parts.hostname or "localhost"
        self._port = parts.port or 7687
        self._user, self._password = auth

    def session(self) -> BoltSession:
        return BoltSession(self._host, self._port, self._user, self._password)

    async def close(self) -> None:
        return None
