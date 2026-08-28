# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Descriptor-native extended-ACL operations for Darwin private files."""

from __future__ import annotations

import ctypes
import errno
import functools
import sys

ACL_TYPE_EXTENDED = 0x00000100
_LIBSYSTEM_PATH = "/usr/lib/libSystem.dylib"


def _is_darwin() -> bool:
    return sys.platform == "darwin"


@functools.lru_cache(maxsize=1)
def _libsystem() -> ctypes.CDLL:
    library = ctypes.CDLL(_LIBSYSTEM_PATH, use_errno=True)
    try:
        library.acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_int)
        library.acl_get_fd_np.restype = ctypes.c_void_p
        library.acl_init.argtypes = (ctypes.c_int,)
        library.acl_init.restype = ctypes.c_void_p
        library.acl_set_fd_np.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        library.acl_set_fd_np.restype = ctypes.c_int
        library.acl_free.argtypes = (ctypes.c_void_p,)
        library.acl_free.restype = ctypes.c_int
    except AttributeError:
        raise OSError(
            errno.ENOTSUP, "Darwin descriptor ACL API is unavailable"
        ) from None
    return library


def _acl_error(operation: str, error_number: int | None = None) -> OSError:
    number = error_number or ctypes.get_errno() or errno.EIO
    return OSError(number, f"Darwin {operation} failed")


def fd_has_extended_acl(descriptor: int) -> bool:
    """Return whether ``descriptor`` has a Darwin extended ACL."""

    if not _is_darwin():
        return False
    library = _libsystem()
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(descriptor, ACL_TYPE_EXTENDED)
    if not acl:
        error_number = ctypes.get_errno()
        if error_number == errno.ENOENT:
            return False
        raise _acl_error("ACL query", error_number)
    ctypes.set_errno(0)
    free_result = library.acl_free(acl)
    if free_result != 0:
        raise _acl_error("ACL release")
    return True


def clear_fd_extended_acl(descriptor: int) -> None:
    """Replace a newly created inode's inherited Darwin ACL with an empty ACL."""

    if not _is_darwin():
        return
    library = _libsystem()
    ctypes.set_errno(0)
    acl = library.acl_init(0)
    if not acl:
        raise _acl_error("empty ACL allocation")

    ctypes.set_errno(0)
    set_result = library.acl_set_fd_np(descriptor, acl, ACL_TYPE_EXTENDED)
    set_error = ctypes.get_errno()
    ctypes.set_errno(0)
    free_result = library.acl_free(acl)
    free_error = ctypes.get_errno()
    if set_result != 0:
        raise _acl_error("ACL clear", set_error)
    if free_result != 0:
        raise _acl_error("ACL release", free_error)
