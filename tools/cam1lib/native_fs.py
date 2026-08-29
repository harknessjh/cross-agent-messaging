# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Small native filesystem operations not exposed portably by :mod:`os`."""

from __future__ import annotations

import ctypes
import errno
import functools
import os
import sys
from collections.abc import Callable

_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_RENAME_NOREPLACE = 0x00000001


def _platform_name() -> str:
    return sys.platform


@functools.lru_cache(maxsize=1)
def _rename_api() -> tuple[Callable[..., int], int]:
    platform = _platform_name()
    if platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
        try:
            function = library.renameatx_np
        except AttributeError:
            raise OSError(
                errno.ENOTSUP, "atomic no-replace rename is unavailable"
            ) from None
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        return function, _DARWIN_RENAME_EXCL
    if platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        try:
            function = library.renameat2
        except AttributeError:
            raise OSError(
                errno.ENOTSUP, "atomic no-replace rename is unavailable"
            ) from None
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        return function, _LINUX_RENAME_NOREPLACE
    raise OSError(errno.ENOTSUP, "atomic no-replace rename is unsupported")


def _encoded_component(name: str) -> bytes:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise OSError(errno.EINVAL, "rename operand must be one path component")
    return os.fsencode(name)


def rename_noreplace(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    """Atomically rename one sibling entry only when the destination is absent."""

    source = _encoded_component(source_name)
    destination = _encoded_component(destination_name)
    function, flags = _rename_api()
    ctypes.set_errno(0)
    result = function(
        parent_descriptor,
        source,
        parent_descriptor,
        destination,
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, "atomic no-replace rename failed")
