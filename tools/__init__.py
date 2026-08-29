# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Source-only package boundary for the CAM reference tools."""

from __future__ import annotations

import posix as _posix
import sys as _sys


def _load_bootstrap() -> object:
    name = "tools._cam1_bootstrap"
    existing = _sys.modules.get(name)
    if existing is not None:
        return existing
    tools_dir = __file__.rsplit("/", 1)[0]
    path = f"{tools_dir}/_cam1_bootstrap.py"
    flags = _posix.O_RDONLY | getattr(_posix, "O_CLOEXEC", 0)
    no_follow = getattr(_posix, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ImportError("CAM bootstrap requires no-follow file opens")
    descriptor = _posix.open(path, flags | no_follow)
    try:
        metadata = _posix.fstat(descriptor)
        if metadata.st_mode & 0o170000 != 0o100000 or metadata.st_size > 524_288:
            raise ImportError("CAM bootstrap must be a bounded regular file")
        raw = _posix.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise ImportError("CAM bootstrap changed while it was read")
    finally:
        _posix.close(descriptor)
    module = type(_sys)(name)
    module.__file__ = path
    module.__package__ = "tools"
    _sys.modules[name] = module
    try:
        exec(compile(raw, path, "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        _sys.modules.pop(name, None)
        raise
    return module


_bootstrap = _load_bootstrap()
_bootstrap.install(__file__.rsplit("/", 1)[0])

del _bootstrap
