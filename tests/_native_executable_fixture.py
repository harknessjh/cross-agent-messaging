# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Native bytes for inspection-only fixtures; product execution stays mocked."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def native_bytes() -> bytes:
    return Path("/bin/echo").resolve(strict=True).read_bytes()


def write_native(path: Path, tag: str = "") -> None:
    # Trailing tags change fingerprints without changing executable headers.
    # Tagged copies are inspection fixtures, not signed/runnable test products.
    path.write_bytes(native_bytes() + tag.encode("utf-8"))
    path.chmod(0o700)
