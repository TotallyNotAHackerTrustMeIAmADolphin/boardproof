"""Hand-built (split) zip archives and a fake opener for the parts index refresh tests (issue 200).

Python's zipfile cannot write a split archive and no zip tool is assumed, so the bytes are laid out by hand
from the zip specification (APPNOTE 4.3 and 8.5): local header, data, central directory, end record; a split
archive starts with the 4-byte marker PK\\x07\\x08, its central directory entry names the disk a member starts
on and the offset on that disk, and the last disk ends with the end record. No real jlcparts file was seen.
"""

from __future__ import annotations

import io
import struct
import urllib.error
import zlib

BASE = "https://yaqwsx.github.io/jlcparts/data/"


def member_record(name: bytes, data: bytes, *, method: int = 8, flags: int = 0, crc: int | None = None,
                  usize: int | None = None) -> tuple[bytes, bytes, int, int]:
    """(local header + data, central directory entry without the offset, crc, compressed size)."""
    if method == 8:
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = co.compress(data) + co.flush()
    else:
        packed = data
    crc = zlib.crc32(data) if crc is None else crc
    usize = len(data) if usize is None else usize
    local = struct.pack("<4s5H3I2H", b"PK\x03\x04", 20, flags, method, 0, 0x21, crc, len(packed), usize, len(name), 0) + name + packed
    return local, name, crc, len(packed)


def central(name: bytes, flags: int, method: int, crc: int, csize: int, usize: int, disk_start: int, offset: int) -> bytes:
    return struct.pack("<4s6H3I5H2I", b"PK\x01\x02", 20, 20, flags, method, 0, 0x21, crc, csize, usize, len(name), 0, 0,
                       disk_start, 0, 0, offset) + name


def end_record(disk: int, cd_disk: int, entries: int, cd_size: int, cd_offset: int) -> bytes:
    return struct.pack("<4sHHHHIIH", b"PK\x05\x06", disk, cd_disk, entries, entries, cd_size, cd_offset, 0)


def single_zip(name: str, data: bytes, **kw) -> bytes:
    """An ordinary one-file zip."""
    local, raw, crc, csize = member_record(name.encode(), data, **{k: v for k, v in kw.items() if k in ("method", "flags", "crc")})
    usize = kw.get("usize", len(data))
    cd = central(raw, kw.get("flags", 0), kw.get("method", 8), crc, csize, usize, 0, 0)
    return local + cd + end_record(0, 0, 1, len(cd), len(local))


def split_zip(name: str, data: bytes, segment_size: int, *, marker_in_offsets: bool = True, entries: list | None = None,
              **kw) -> dict[str, bytes]:
    """{file name: bytes} as ``zip -s`` lays it out: cache.z01 ..., then cache.zip with the central directory.

    ``entries`` may add (name, data) members written after the first. The last segment holds only the
    central directory and the end record.
    """
    members = [(name, data)] + list(entries or [])
    stream = b"PK\x07\x08"
    cds = []
    for member_name, member_data in members:
        local, raw, crc, csize = member_record(member_name.encode(), member_data, **{k: v for k, v in kw.items() if k in ("method", "flags", "crc")})
        offset = len(stream) if marker_in_offsets else len(stream) - 4
        stream += local
        cds.append((raw, crc, csize, len(member_data), offset))
    parts = [stream[i:i + segment_size] for i in range(0, len(stream), segment_size)]
    cd = b"".join(central(raw, kw.get("flags", 0), kw.get("method", 8), crc, csize, usize if "usize" not in kw else kw["usize"], 0, off)
                  for raw, crc, csize, usize, off in cds)
    files = {f"cache.z{i + 1:02d}": part for i, part in enumerate(parts)}
    files["cache.zip"] = cd + end_record(len(parts), len(parts), len(cds), len(cd), 0)
    return files


def single_files(name: str, data: bytes, **kw) -> dict[str, bytes]:
    return {"cache.zip": single_zip(name, data, **kw)}


class FakeResponse:
    def __init__(self, url: str, body: bytes, *, declared: int | None | str = ..., final_url: str | None = None,
                 fail_after: int | None = None):
        self._body = io.BytesIO(body)
        self._url = final_url or url
        self._fail_after = fail_after
        self._served = 0
        length = len(body) if declared is ... else declared
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def read(self, n: int = -1) -> bytes:
        if self._fail_after is not None and self._served >= self._fail_after:
            raise ConnectionResetError("connection reset")
        if self._fail_after is not None:
            n = min(n, self._fail_after - self._served) if n >= 0 else self._fail_after - self._served
        data = self._body.read(n)
        self._served += len(data)
        return data

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Serves ``files`` (name -> bytes) under the jlcparts URL; a missing name answers HTTP 404."""

    def __init__(self, files: dict[str, bytes], *, overrides: dict | None = None):
        self.files = files
        self.overrides = overrides or {}
        self.calls: list[tuple[str, str]] = []

    def open(self, request, timeout=None):
        url = request.full_url
        method = request.get_method()
        self.calls.append((method, url))
        name = url.rsplit("/", 1)[-1]
        if name in self.overrides:
            kind = self.overrides[name]
            if callable(kind):
                return kind(url, method, self.files.get(name))
        if name not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        body = self.files[name]
        return FakeResponse(url, b"" if method == "HEAD" else body, declared=len(body))
