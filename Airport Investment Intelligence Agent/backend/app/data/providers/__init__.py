"""Source adapters: one module per public source, each producing normalized tables.

Shared rules for every adapter: archives are read in memory or as streams by expected member
names only (nothing is extracted to paths taken from an archive), declared and actual
uncompressed sizes are limited, and the structure of each file is validated before any row is
returned. Missing source values stay missing (NaN/None) and are never replaced by zero.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pandas as pd


class SourceFormatError(Exception):
    """A downloaded file does not have the structure the adapter expects."""


@dataclass
class NormalizedTable:
    """Rows for one storage table (without `file_id`, which the ingestion adds)."""

    table: str
    frame: pd.DataFrame


@dataclass
class ParsedFile:
    """Everything an adapter extracted from one source file."""

    tables: list[NormalizedTable]
    dataset_version: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return sum(len(table.frame) for table in self.tables)


class _LimitedReader(io.RawIOBase):
    """Stream wrapper that fails once more than `limit` bytes were read (zip-bomb guard)."""

    def __init__(self, raw: IO[bytes], limit: int, name: str) -> None:
        self._raw = raw
        self._limit = limit
        self._name = name
        self._read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        data = self._raw.read(len(buffer))
        self._read += len(data)
        if self._read > self._limit:
            raise SourceFormatError(f"Archive member {self._name} is larger than its size limit.")
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        self._raw.close()
        super().close()


def _member_info(archive: zipfile.ZipFile, name: str, max_bytes: int) -> zipfile.ZipInfo:
    matches = [info for info in archive.infolist() if info.filename == name]
    if not matches:
        raise SourceFormatError(f"The archive does not contain the expected file {name}.")
    if len(matches) > 1:
        raise SourceFormatError(f"The archive contains {name} more than once.")
    info = matches[0]
    if info.is_dir() or info.flag_bits & 0x1:
        raise SourceFormatError(f"Archive member {name} is not a readable file.")
    if info.file_size > max_bytes:
        raise SourceFormatError(
            f"Archive member {name} declares {info.file_size:,} bytes, above the {max_bytes:,} byte limit."
        )
    return info


@contextmanager
def open_zip_member(path: Path, name: str, max_bytes: int) -> Iterator[IO[bytes]]:
    """Open one expected member as a size-limited binary stream."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SourceFormatError("The downloaded file is not a valid ZIP archive.") from exc
    with archive:
        info = _member_info(archive, name, max_bytes)
        with archive.open(info) as raw:
            reader = io.BufferedReader(_LimitedReader(raw, max_bytes, name), buffer_size=1024 * 1024)
            try:
                yield reader
            except zipfile.BadZipFile as exc:
                raise SourceFormatError(f"Archive member {name} is corrupt.") from exc


def read_zip_member(path: Path, name: str, max_bytes: int) -> bytes:
    """Read one expected member fully into memory (for small members such as Excel workbooks)."""
    with open_zip_member(path, name, max_bytes) as stream:
        try:
            return stream.read()
        except zipfile.BadZipFile as exc:
            raise SourceFormatError(f"Archive member {name} is corrupt.") from exc


def zip_member_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise SourceFormatError("The downloaded file is not a valid ZIP archive.") from exc


def require_columns(frame: pd.DataFrame, columns: list[str], what: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise SourceFormatError(f"{what} is missing the expected columns: {', '.join(missing)}.")
