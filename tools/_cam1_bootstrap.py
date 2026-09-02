# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Load the CAM reference implementation from an exact source allowlist.

Public command facades execute this file explicitly after entering Python's
isolated mode.  CAM modules are compiled from source bytes captured once here;
normal path-based lookup and cached or native sibling modules are never used.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import os
import posix
import re
import subprocess
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Any

_MAX_SOURCE_BYTES = 4 * 1_048_576
_REGULAR_FILE_MASK = 0o170000
_REGULAR_FILE = 0o100000
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_GIT_EXECUTABLE_CANDIDATES = (
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/usr/bin/git",
)
_IMPORTABLE_SUFFIXES = (".py", ".pyc", ".pyo", ".so", ".pyd")

_SOURCE_PATHS = {
    "tools": "__init__.py",
    "tools._cam1_bootstrap": "_cam1_bootstrap.py",
    "tools._cam1_entry": "_cam1_entry.py",
    "tools.cam1": "cam1.py",
    "tools.cam1_project": "cam1_project.py",
    "tools.cam1_transport": "cam1_transport.py",
    "tools.cam1_transport_native": "cam1_transport_native.py",
    "tools.cam1_transport_retry": "cam1_transport_retry.py",
    "tools.cam1lib": "cam1lib/__init__.py",
    "tools.cam1lib.builders": "cam1lib/builders.py",
    "tools.cam1lib.cli": "cam1lib/cli.py",
    "tools.cam1lib.compatibility": "cam1lib/compatibility.py",
    "tools.cam1lib.compatibility_cli": "cam1lib/compatibility_cli.py",
    "tools.cam1lib.darwin_acl": "cam1lib/darwin_acl.py",
    "tools.cam1lib.errors": "cam1lib/errors.py",
    "tools.cam1lib.journal": "cam1lib/journal.py",
    "tools.cam1lib.journal_recovery": "cam1lib/journal_recovery.py",
    "tools.cam1lib.journal_types": "cam1lib/journal_types.py",
    "tools.cam1lib.lifecycle": "cam1lib/lifecycle.py",
    "tools.cam1lib.native_fs": "cam1lib/native_fs.py",
    "tools.cam1lib.participants": "cam1lib/participants.py",
    "tools.cam1lib.profile": "cam1lib/profile.py",
    "tools.cam1lib.project": "cam1lib/project.py",
    "tools.cam1lib.project_git": "cam1lib/project_git.py",
    "tools.cam1lib.protocol": "cam1lib/protocol.py",
    "tools.cam1lib.routing": "cam1lib/routing.py",
    "tools.cam1lib.secure_fs": "cam1lib/secure_fs.py",
    "tools.cam1lib.state": "cam1lib/state.py",
    "tools.cam1lib.state_projection": "cam1lib/state_projection.py",
    "tools.cam1lib.state_store": "cam1lib/state_store.py",
    "tools.cam1lib.transport_cli": "cam1lib/transport_cli.py",
    "tools.cam1lib.validation": "cam1lib/validation.py",
}
_PACKAGE_NAMES = frozenset({"tools", "tools.cam1lib"})

_tools_dir: str | None = None
_captured_sources: dict[str, tuple[str, bytes]] = {}
_finder: _ExactSourceFinder | None = None


class BootstrapError(ImportError):
    """The canonical CAM source graph could not be loaded safely."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        if detail is None:
            detail = code
            code = "bootstrap.invalid"
        self.code = code[:80]
        self.detail = detail[:300]
        super().__init__(self.detail)


def _read_regular_source(path: str) -> bytes:
    no_follow = getattr(posix, "O_NOFOLLOW", None)
    if no_follow is None:
        raise BootstrapError("this POSIX runtime does not support no-follow opens")
    flags = posix.O_RDONLY | no_follow
    flags |= getattr(posix, "O_CLOEXEC", 0)
    try:
        descriptor = posix.open(path, flags)
    except OSError as error:
        raise BootstrapError("a required CAM source file is unavailable") from error
    try:
        before = posix.fstat(descriptor)
        if (
            before.st_mode & _REGULAR_FILE_MASK != _REGULAR_FILE
            or before.st_size > _MAX_SOURCE_BYTES
        ):
            raise BootstrapError("CAM source files must be bounded regular files")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = posix.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise BootstrapError("a CAM source file changed while it was read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if posix.read(descriptor, 1):
            raise BootstrapError("a CAM source file changed while it was read")
        after = posix.fstat(descriptor)
    finally:
        posix.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise BootstrapError("a CAM source file changed while it was read")
    return b"".join(chunks)


def _executable_inventory(tools_dir: str) -> frozenset[str]:
    inventory: set[str] = set()
    pending = [("", tools_dir)]
    while pending:
        relative_dir, absolute_dir = pending.pop()
        try:
            entries = tuple(os.scandir(absolute_dir))
        except OSError as error:
            raise BootstrapError(
                "profile.source_unavailable",
                "CAM source inventory could not be inspected",
            ) from error
        for entry in entries:
            relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
            if entry.is_symlink():
                raise BootstrapError(
                    "profile.component_type",
                    "CAM source inventory cannot contain symbolic links",
                )
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as error:
                raise BootstrapError(
                    "profile.source_unavailable",
                    "CAM source inventory changed while inspected",
                ) from error
            if is_directory:
                if entry.name != "__pycache__":
                    pending.append((relative, entry.path))
                continue
            if is_file and relative.endswith(_IMPORTABLE_SUFFIXES):
                inventory.add(relative)
    return frozenset(inventory)


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _git_executable() -> str:
    for candidate in _GIT_EXECUTABLE_CANDIDATES:
        try:
            resolved = os.path.realpath(candidate)
            metadata = posix.stat(resolved)
        except OSError:
            continue
        if metadata.st_mode & _REGULAR_FILE_MASK == _REGULAR_FILE and posix.access(
            resolved, posix.X_OK
        ):
            return resolved
    raise BootstrapError(
        "profile.source_unavailable",
        "a trusted absolute Git executable is unavailable",
    )


def _run_git(
    git_bin: str,
    repository_root: str,
    *arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [
                git_bin,
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                repository_root,
                *arguments,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError(
            "profile.source_unavailable",
            "CAM bootstrap source-control inspection failed",
        ) from error


def _single_git_line(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise BootstrapError(
            "profile.source_unavailable",
            "CAM bootstrap source-control output was malformed",
        ) from error
    if not text or any(character in text for character in "\r\n\x00"):
        raise BootstrapError(
            "profile.source_unavailable",
            "CAM bootstrap source-control output was malformed",
        )
    return text


def _git_blob_id(raw: bytes, expected_object_id: str) -> str:
    framed = b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw
    if len(expected_object_id) == 40:
        return hashlib.sha1(framed, usedforsecurity=False).hexdigest()
    if len(expected_object_id) == 64:
        return hashlib.sha256(framed).hexdigest()
    raise BootstrapError(
        "profile.source_unavailable",
        "CAM bootstrap encountered an unsupported Git object format",
    )


def _head_source_entries(
    git_bin: str,
    repository_root: str,
    git_head: str,
) -> dict[str, tuple[str, str, str]]:
    result = _run_git(
        git_bin,
        repository_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        git_head,
        "--",
        "tools",
    )
    if result.returncode != 0:
        raise BootstrapError(
            "profile.source_unavailable",
            "CAM bootstrap could not inspect Git HEAD",
        )
    entries: dict[str, tuple[str, str, str]] = {}
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_kind, raw_object_id = metadata.split(b" ", 2)
            mode = raw_mode.decode("ascii", errors="strict")
            kind = raw_kind.decode("ascii", errors="strict")
            object_id = raw_object_id.decode("ascii", errors="strict")
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise BootstrapError(
                "profile.source_unavailable",
                "CAM bootstrap Git tree output was malformed",
            ) from error
        relative = path.removeprefix("tools/")
        if (
            path == relative
            or path.startswith("/")
            or ".." in relative.split("/")
            or "\x00" in relative
        ):
            raise BootstrapError(
                "profile.source_unavailable",
                "CAM bootstrap Git tree paths were malformed",
            )
        if relative.endswith(_IMPORTABLE_SUFFIXES):
            if relative in entries or _GIT_OBJECT_ID.fullmatch(object_id) is None:
                raise BootstrapError(
                    "profile.source_unavailable",
                    "CAM bootstrap Git tree was inconsistent",
                )
            entries[relative] = (mode, kind, object_id)
    return entries


def _index_source_tags(
    git_bin: str,
    repository_root: str,
) -> dict[str, str]:
    result = _run_git(
        git_bin,
        repository_root,
        "ls-files",
        "-v",
        "-z",
        "--",
        "tools",
    )
    if result.returncode != 0:
        raise BootstrapError(
            "profile.source_unavailable",
            "CAM bootstrap could not inspect the Git index",
        )
    tags: dict[str, str] = {}
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise BootstrapError(
                "profile.source_unavailable",
                "CAM bootstrap Git index output was malformed",
            )
        try:
            tag = record[:1].decode("ascii", errors="strict")
            path = record[2:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise BootstrapError(
                "profile.source_unavailable",
                "CAM bootstrap Git index output was malformed",
            ) from error
        relative = path.removeprefix("tools/")
        if path != relative and relative.endswith(_IMPORTABLE_SUFFIXES):
            if relative in tags:
                raise BootstrapError(
                    "profile.source_unavailable",
                    "CAM bootstrap Git index was inconsistent",
                )
            tags[relative] = tag
    return tags


class _CapturedSourceLoader(importlib.abc.Loader):
    """Compile one already-captured source file without consulting bytecode."""

    def __init__(self, fullname: str, path: str, raw: bytes) -> None:
        self.fullname = fullname
        self.path = path
        self.raw = raw

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        module.__file__ = self.path
        module.__cached__ = None
        code = compile(self.raw, self.path, "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def get_code(self, fullname: str) -> Any:
        if fullname != self.fullname:
            raise BootstrapError("CAM loader received an unexpected module name")
        return compile(self.raw, self.path, "exec", dont_inherit=True)

    def get_source(self, fullname: str) -> str:
        if fullname != self.fullname:
            raise BootstrapError("CAM loader received an unexpected module name")
        return self.raw.decode("utf-8")


class _ExactSourceFinder(importlib.abc.MetaPathFinder):
    """Resolve every CAM module to one captured source file and nothing else."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        captured = _captured_sources.get(fullname)
        if captured is None:
            if fullname.startswith("tools."):
                raise ModuleNotFoundError(
                    f"{fullname!r} is not in the CAM source allowlist",
                    name=fullname,
                )
            return None
        source_path, raw = captured
        loader = _CapturedSourceLoader(fullname, source_path, raw)
        is_package = fullname in _PACKAGE_NAMES
        spec = importlib.machinery.ModuleSpec(
            fullname,
            loader,
            origin=source_path,
            is_package=is_package,
        )
        if is_package:
            spec.submodule_search_locations = []
        return spec


def _new_tools_package(tools_dir: str) -> ModuleType:
    package = ModuleType("tools")
    package.__file__ = f"{tools_dir}/__init__.py"
    package.__package__ = "tools"
    package.__path__ = []  # type: ignore[attr-defined]
    package.__spec__ = importlib.machinery.ModuleSpec(
        "tools",
        loader=None,
        origin=package.__file__,
        is_package=True,
    )
    package.__spec__.submodule_search_locations = []
    return package


def install(tools_dir: str) -> None:
    """Capture and install the one allowed local module graph."""

    global _finder, _tools_dir
    if not tools_dir.startswith("/") or "\x00" in tools_dir:
        raise BootstrapError("the CAM tools directory must be an absolute path")
    if _tools_dir is not None:
        if _tools_dir != tools_dir:
            raise BootstrapError("multiple CAM source roots cannot share one process")
        return

    captured: dict[str, tuple[str, bytes]] = {}
    for fullname, relative_path in _SOURCE_PATHS.items():
        source_path = f"{tools_dir}/{relative_path}"
        captured[fullname] = (source_path, _read_regular_source(source_path))
    _captured_sources.update(captured)
    _tools_dir = tools_dir

    package = sys.modules.get("tools")
    expected_init = f"{tools_dir}/__init__.py"
    if package is None:
        package = _new_tools_package(tools_dir)
        sys.modules["tools"] = package
    elif getattr(package, "__file__", None) != expected_init:
        raise BootstrapError("a conflicting tools package is already loaded")
    else:
        package.__path__ = []  # type: ignore[attr-defined]
        if package.__spec__ is not None:
            package.__spec__.submodule_search_locations = []

    current = sys.modules.get(__name__)
    if current is not None:
        sys.modules["tools._cam1_bootstrap"] = current

    _finder = _ExactSourceFinder()
    sys.meta_path.insert(0, _finder)


def require_live_import_sources() -> None:
    """Verify executable source against Git before importing CAM commands."""

    if _tools_dir is None or not _captured_sources:
        raise BootstrapError(
            "bootstrap.invalid", "the CAM source bootstrap was not installed"
        )
    expected_paths = frozenset(_SOURCE_PATHS.values())
    if _executable_inventory(_tools_dir) != expected_paths:
        raise BootstrapError(
            "profile.path_set_mismatch",
            "live CAM executable paths must match the exact source allowlist",
        )

    repository_root = _tools_dir.rsplit("/", 1)[0]
    git_bin = _git_executable()
    top_level = _run_git(git_bin, repository_root, "rev-parse", "--show-toplevel")
    if top_level.returncode != 0 or os.path.realpath(
        _single_git_line(top_level.stdout)
    ) != os.path.realpath(repository_root):
        raise BootstrapError(
            "profile.source_unavailable",
            "live CAM source must be the root of a verifiable Git checkout",
        )
    head_result = _run_git(
        git_bin,
        repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    )
    if head_result.returncode != 0:
        raise BootstrapError(
            "profile.source_unversioned",
            "live CAM source requires a resolvable Git HEAD commit",
        )
    git_head = _single_git_line(head_result.stdout)
    if _GIT_OBJECT_ID.fullmatch(git_head) is None:
        raise BootstrapError(
            "profile.source_unavailable",
            "live CAM source returned a malformed Git HEAD",
        )

    head = _head_source_entries(git_bin, repository_root, git_head)
    if set(head) != expected_paths or any(
        mode not in {"100644", "100755"} or kind != "blob"
        for mode, kind, _object_id in head.values()
    ):
        raise BootstrapError(
            "profile.path_set_mismatch",
            "live CAM executable paths must be regular blobs in Git HEAD",
        )
    index = _index_source_tags(git_bin, repository_root)
    if set(index) != expected_paths or any(tag != "H" for tag in index.values()):
        raise BootstrapError(
            "profile.index_concealment",
            "live CAM executable paths cannot use concealed or sparse index flags",
        )

    captured_by_path = {
        path.removeprefix(f"{_tools_dir}/"): raw
        for path, raw in _captured_sources.values()
    }
    if set(captured_by_path) != expected_paths or any(
        _git_blob_id(captured_by_path[path], head[path][2]) != head[path][2]
        for path in expected_paths
    ):
        raise BootstrapError(
            "profile.executable_source_dirty",
            "live CAM executable source must match Git HEAD before import",
        )


def verify_captured_sources() -> None:
    """Require the source files at the live gate to equal the executed capture."""

    if _tools_dir is None or not _captured_sources:
        raise BootstrapError("the CAM source bootstrap was not installed")
    for source_path, captured in _captured_sources.values():
        if _read_regular_source(source_path) != captured:
            raise BootstrapError("CAM source changed after bootstrap capture")
