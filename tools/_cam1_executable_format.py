# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Bounded native-entrypoint inspection; not a loader or dependency attestation."""

from __future__ import annotations

import os
import struct


def _macho(fd: int, offset: int, size: int) -> bool:
    header = os.pread(fd, 32, offset)
    formats = {
        b"\xce\xfa\xed\xfe": ("<", 28),
        b"\xcf\xfa\xed\xfe": ("<", 32),
        b"\xfe\xed\xfa\xce": (">", 28),
        b"\xfe\xed\xfa\xcf": (">", 32),
    }
    shape = formats.get(header[:4])
    if shape is None:
        return False
    endian, header_size = shape
    if size < header_size or len(header) < header_size:
        return False
    file_type, commands, command_bytes = struct.unpack_from(endian + "III", header, 12)
    return (
        file_type == 2  # MH_EXECUTE, not a dylib or object file
        and 0 < commands <= 65_536
        and commands * 8 <= command_bytes <= size - header_size
    )


def _universal_macho(fd: int, size: int, header: bytes) -> bool:
    shapes = {
        b"\xca\xfe\xba\xbe": (">", False),
        b"\xbe\xba\xfe\xca": ("<", False),
        b"\xca\xfe\xba\xbf": (">", True),
        b"\xbf\xba\xfe\xca": ("<", True),
    }
    shape = shapes.get(header[:4])
    if shape is None or len(header) < 8:
        return False
    endian, wide = shape
    count = struct.unpack_from(endian + "I", header, 4)[0]
    entry_size = 32 if wide else 20
    table_end = 8 + count * entry_size
    if not 0 < count <= 64 or table_end > size:
        return False
    table = os.pread(fd, count * entry_size, 8)
    if len(table) != count * entry_size:
        return False
    intervals: list[tuple[int, int]] = []
    for index in range(count):
        offset, length, alignment = struct.unpack_from(
            endian + ("QQI" if wide else "III"), table, index * entry_size + 8
        )
        if (
            offset < table_end
            or length < 28
            or offset + length > size
            or alignment > 31
            or offset % (1 << alignment)
            or any(offset < end and start < offset + length for start, end in intervals)
            or not _macho(fd, offset, length)
        ):
            return False
        intervals.append((offset, offset + length))
    return True


def _elf(fd: int, size: int, header: bytes) -> bool:
    if len(header) < 52 or header[:4] != b"\x7fELF":
        return False
    elf_class, encoding, ident_version = header[4:7]
    if elf_class not in (1, 2) or encoding not in (1, 2) or ident_version != 1:
        return False
    endian = "<" if encoding == 1 else ">"
    wide = elf_class == 2
    header_size, entry_size = (64, 56) if wide else (52, 32)
    if len(header) < header_size or size < header_size:
        return False
    file_type, machine, version = struct.unpack_from(endian + "HHI", header, 16)
    ph_offset = struct.unpack_from(
        endian + ("Q" if wide else "I"), header, 32 if wide else 28
    )[0]
    eh_size, ph_size, ph_count = struct.unpack_from(
        endian + "HHH", header, 52 if wide else 40
    )
    if not (
        file_type in (2, 3)  # ET_EXEC or PIE (ET_DYN)
        and machine != 0
        and version == 1
        and eh_size == header_size
        and ph_size == entry_size
        and 0 < ph_count <= 1024
        and ph_offset >= header_size
        and ph_offset + ph_count * entry_size <= size
    ):
        return False
    table = os.pread(fd, ph_count * entry_size, ph_offset)
    if len(table) != ph_count * entry_size:
        return False
    loadable = False
    for index in range(ph_count):
        start = index * entry_size
        kind = struct.unpack_from(endian + "I", table, start)[0]
        file_offset = struct.unpack_from(
            endian + ("Q" if wide else "I"), table, start + (8 if wide else 4)
        )[0]
        file_size, memory_size = struct.unpack_from(
            endian + ("QQ" if wide else "II"), table, start + (32 if wide else 16)
        )
        if file_offset + file_size > size or (kind == 1 and file_size > memory_size):
            return False
        loadable |= kind == 1
    return loadable


def is_native_executable(fd: int, size: int, platform: str) -> bool:
    """Reject scripts and malformed headers without invoking the candidate."""

    header = os.pread(fd, 64, 0)
    if platform == "darwin":
        return _macho(fd, 0, size) or _universal_macho(fd, size, header)
    if platform == "linux":
        return _elf(fd, size, header)
    return False
