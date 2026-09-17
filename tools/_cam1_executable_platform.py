# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""OS permission inspection for native launch paths, including bootstrap Git.

No CAM application imports, subprocesses, permission changes, or PATH lookup.
Darwin declarations follow the system acl.h, membership.h, and mount.h APIs.
"""

from __future__ import annotations

import ctypes
import errno
import grp
import os
import sys
from functools import lru_cache

_MUTATING_ACL_RIGHTS = sum(1 << bit for bit in (2, 4, 5, 6, 8, 10, 12, 13))


@lru_cache(maxsize=1)
def trusted_admin_group() -> int | None:
    """Only macOS's local administrator group is inside the admin exclusion."""

    if sys.platform != "darwin":
        return None
    try:
        return grp.getgrnam("admin").gr_gid
    except KeyError:
        return None


class _DarwinStatFS(ctypes.Structure):
    _fields_ = [
        ("bsize", ctypes.c_uint32),
        ("iosize", ctypes.c_int32),
        ("counts", ctypes.c_uint64 * 5),
        ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("typename", ctypes.c_char * 16),
        ("mountpoint", ctypes.c_char * 1024),
        ("source", ctypes.c_char * 1024),
        ("extended", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


class _LinuxStatFS(ctypes.Structure):
    # Linux asm-generic/statfs.h on the supported LP64 architectures.
    _fields_ = [
        ("type", ctypes.c_long),
        ("bsize", ctypes.c_long),
        ("counts", ctypes.c_ulong * 5),
        ("fsid", ctypes.c_int * 2),
        ("namelen", ctypes.c_long),
        ("frsize", ctypes.c_long),
        ("flags", ctypes.c_long),
        ("spare", ctypes.c_long * 4),
    ]


@lru_cache(maxsize=1)
def _linux_libc() -> ctypes.CDLL:
    if ctypes.sizeof(ctypes.c_long) != 8 or os.uname().machine not in (
        "x86_64",
        "aarch64",
    ):
        raise OSError(
            "executable filesystem inspection supports Linux x86_64/aarch64 only"
        )
    lib = ctypes.CDLL(None, use_errno=True)
    lib.fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_LinuxStatFS)]
    lib.fstatfs.restype = ctypes.c_int
    return lib


@lru_cache(maxsize=1)
def _darwin_libc() -> ctypes.CDLL:
    lib = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    signatures = {
        "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
        "acl_valid": ([ctypes.c_void_p], ctypes.c_int),
        "acl_get_entry": (
            [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int,
        ),
        "acl_get_tag_type": (
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)],
            ctypes.c_int,
        ),
        "acl_get_permset_mask_np": (
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)],
            ctypes.c_int,
        ),
        "acl_get_qualifier": ([ctypes.c_void_p], ctypes.c_void_p),
        "acl_free": ([ctypes.c_void_p], ctypes.c_int),
        "mbr_uuid_to_id": (
            [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_int),
            ],
            ctypes.c_int,
        ),
        "fstatfs64": ([ctypes.c_int, ctypes.POINTER(_DarwinStatFS)], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    return lib


def _check(result: int) -> None:
    if result != 0:
        raise OSError(ctypes.get_errno(), "executable permission inspection failed")


def _trusted_acl_principal(lib: ctypes.CDLL, entry: ctypes.c_void_p) -> bool:
    qualifier = lib.acl_get_qualifier(entry)
    if not qualifier:
        raise OSError("executable ACL principal unavailable")
    try:
        identifier, kind = ctypes.c_uint32(), ctypes.c_int()
        result = lib.mbr_uuid_to_id(
            qualifier, ctypes.byref(identifier), ctypes.byref(kind)
        )
        if result != 0:
            return False
        if kind.value == 0:
            return identifier.value in (0, os.geteuid())
        return kind.value == 1 and identifier.value == trusted_admin_group()
    finally:
        _check(lib.acl_free(qualifier))


def has_untrusted_acl_mutation(fd: int) -> bool:
    """Deny-only/read ACLs are harmless; unresolved mutation grants are not."""

    if sys.platform == "linux":
        # POSIX access ACL_MASK is represented in the group mode bits. The
        # caller rejects group/other write, covering named user/group grants.
        return False
    lib = _darwin_libc()
    acl = lib.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return False
        raise OSError(ctypes.get_errno(), "executable ACL unavailable")
    try:
        _check(lib.acl_valid(acl))
        for index in range(129):
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = lib.acl_get_entry(acl, index, ctypes.byref(entry))
            if result != 0:
                if ctypes.get_errno() == errno.EINVAL:
                    return False  # Valid ACL, index past its final entry.
                _check(result)
            if index == 128:
                raise OSError("executable ACL exceeds system entry limit")
            tag, permissions = ctypes.c_int(), ctypes.c_uint64()
            _check(lib.acl_get_tag_type(entry, ctypes.byref(tag)))
            _check(lib.acl_get_permset_mask_np(entry, ctypes.byref(permissions)))
            if tag.value not in (1, 2):
                raise OSError("executable ACL has an unsupported entry type")
            known_rights = _MUTATING_ACL_RIGHTS | sum(
                1 << bit for bit in (1, 3, 7, 9, 11, 20)
            )
            if tag.value == 1 and permissions.value & ~known_rights:
                raise OSError("executable ACL has unsupported permission bits")
            if tag.value == 1 and permissions.value & _MUTATING_ACL_RIGHTS:
                if not _trusted_acl_principal(lib, entry):
                    return True
        raise OSError("executable ACL inspection incomplete")
    finally:
        _check(lib.acl_free(acl))


def require_permission_filesystem(fd: int) -> None:
    """Reject mounts known not to provide the permission model we inspect."""

    if sys.platform == "darwin":
        metadata = _DarwinStatFS()
        _check(_darwin_libc().fstatfs64(fd, ctypes.byref(metadata)))
        if not metadata.flags & 0x1000 or metadata.flags & 0x00200000:
            raise OSError("executable filesystem is nonlocal or ignores ownership")
        if bytes(metadata.typename) not in (b"apfs", b"hfs"):
            raise OSError("executable filesystem permission model is unsupported")
    elif sys.platform == "linux":
        metadata = _LinuxStatFS()
        _check(_linux_libc().fstatfs(fd, ctypes.byref(metadata)))
        # Local POSIX permission/ACL-mask filesystems. Do not infer the same
        # model for NFS, CIFS, FUSE, ownership-emulating or unknown filesystems.
        if metadata.type not in (
            0xEF53,
            0x9123683E,
            0x58465342,
            0x01021994,
            0x794C7630,
            0x858458F6,
            0xF15F,
            0xF2F52010,
        ):
            raise OSError("executable filesystem permission model is unsupported")
    else:
        raise OSError("native executable policy supports macOS and Linux only")
