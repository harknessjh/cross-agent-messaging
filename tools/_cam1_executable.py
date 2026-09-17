# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Shared launch eligibility, including the pre-import Git trust boundary.

This module and its two helpers are captured bootstrap sources. They must not
import ordinary CAM application modules or execute candidates during inspection.
Current-user/root (and macOS local administrators) are trusted principals.
This policy does not attest the interpreter running CAM or native dependencies.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from tools._cam1_executable_format import is_native_executable
from tools._cam1_executable_platform import (
    has_untrusted_acl_mutation,
    require_permission_filesystem,
    trusted_admin_group,
)


class ExecutablePolicyError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(detail)


def _require_permissions(fd: int, metadata: os.stat_result, *, directory: bool) -> None:
    if metadata.st_uid not in (0, os.geteuid()):
        raise ExecutablePolicyError("owner", "executable path has an untrusted owner")
    # A sticky, trusted-owned parent protects a trusted-owned next component
    # from other UIDs. Every next component is checked separately; ACL mutation
    # grants are never exempted by the sticky bit.
    sticky = directory and bool(metadata.st_mode & stat.S_ISVTX)
    group_write = metadata.st_mode & stat.S_IWGRP
    trusted_group = metadata.st_gid == trusted_admin_group()
    if not sticky and (
        metadata.st_mode & stat.S_IWOTH or (group_write and not trusted_group)
    ):
        raise ExecutablePolicyError(
            "writable", "executable path is writable by an untrusted principal"
        )
    if has_untrusted_acl_mutation(fd):
        raise ExecutablePolicyError(
            "acl", "executable path grants untrusted ACL mutation rights"
        )


def require_native_executable(path: str | Path) -> str:
    """Return a checked canonical launch path; never return the caller's alias.

    Walk all components with no-follow descriptors. No permission result is
    cached across calls: parent chmod/ACL drift need not change the leaf tuple.
    """

    candidate = Path(path)
    if not candidate.is_absolute():
        raise ExecutablePolicyError("path", "executable path must be absolute")
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ExecutablePolicyError(
            "not_found", "executable path could not be resolved"
        ) from error
    if canonical == Path("/"):
        raise ExecutablePolicyError("file_type", "executable must be a regular file")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptor: int | None = None
    devices: set[int] = set()
    try:
        descriptor = os.open("/", directory_flags)
        for index, part in enumerate(canonical.parts):
            if index:
                last = index == len(canonical.parts) - 1
                before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if last and not stat.S_ISREG(before.st_mode):
                    raise ExecutablePolicyError(
                        "file_type", "executable must be a regular file"
                    )
                opened = os.open(
                    part, file_flags if last else directory_flags, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = opened
                metadata = os.fstat(descriptor)
                if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ExecutablePolicyError(
                        "changed", "executable path changed during inspection"
                    )
            else:
                last = False
                metadata = os.fstat(descriptor)
            if metadata.st_dev not in devices:
                require_permission_filesystem(descriptor)
                devices.add(metadata.st_dev)
            _require_permissions(descriptor, metadata, directory=not last)
        if not metadata.st_mode & 0o111 or not os.access(canonical, os.X_OK):
            raise ExecutablePolicyError(
                "not_executable", "executable file has no execute permission"
            )
        if not is_native_executable(descriptor, metadata.st_size, sys.platform):
            raise ExecutablePolicyError(
                "native_required",
                "CAM requires a native Mach-O (macOS) or ELF (Linux) executable; script launchers are unsupported",
            )
        after = os.fstat(descriptor)
        if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ExecutablePolicyError(
                "changed", "executable changed during inspection"
            )
        return str(canonical)
    except OSError as error:
        raise ExecutablePolicyError(
            "inspection",
            "executable ownership, permissions, ACLs, or filesystem could not be verified",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
