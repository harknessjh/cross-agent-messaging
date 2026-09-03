# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Deterministic identity for the local CAM validation toolchain."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from tools import _cam1_bootstrap

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE_FORMAT = "CAM-VALIDATION-PROFILE/1"
MAX_PROFILE_COMPONENT_BYTES = 4 * 1_048_576
MAX_PROFILE_TOTAL_BYTES = 16 * 1_048_576
GIT_EXECUTABLE_CANDIDATES = (
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/usr/bin/git",
)
REQUIRED_PROFILE_PATHS = (
    "cam-1.schema.json",
    "requirements.txt",
    "schemas/cam-journal-record-1.schema.json",
    "schemas/cam-project-binding-1.schema.json",
    "schemas/cam-product-executable-approval-1.schema.json",
    "tools/__init__.py",
    "tools/_cam1_bootstrap.py",
    "tools/_cam1_entry.py",
    "tools/cam1.py",
    "tools/cam1_project.py",
    "tools/cam1_transport.py",
    "tools/cam1_transport_native.py",
    "tools/cam1_transport_products.py",
    "tools/cam1lib/__init__.py",
    "tools/cam1lib/builders.py",
    "tools/cam1lib/cli.py",
    "tools/cam1lib/inbound.py",
    "tools/cam1lib/journal.py",
    "tools/cam1lib/lifecycle.py",
    "tools/cam1lib/participants.py",
    "tools/cam1lib/profile.py",
    "tools/cam1lib/project.py",
    "tools/cam1lib/product_approval_recovery.py",
    "tools/cam1lib/product_approval_recovery_evidence.py",
    "tools/cam1lib/product_approvals.py",
    "tools/cam1lib/product_executables.py",
    "tools/cam1lib/protocol.py",
    "tools/cam1lib/routing.py",
    "tools/cam1lib/state.py",
    "tools/cam1lib/transport_audit.py",
    "tools/cam1lib/validation.py",
)
REQUIRED_PROFILE_GLOBS = (
    "schemas/*.schema.json",
    "tools/**/*.py",
)
OPTIONAL_PROFILE_GLOBS = (
    "tools/**/*.pyc",
    "tools/**/*.pyo",
    "tools/**/*.so",
    "tools/**/*.pyd",
)
PROFILE_GLOBS = REQUIRED_PROFILE_GLOBS + OPTIONAL_PROFILE_GLOBS
PROFILE_TREE_ROOTS = (
    "cam-1.schema.json",
    "requirements.txt",
    "schemas",
    "tools",
)
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class ValidationProfileError(RuntimeError):
    """The running toolchain could not identify its own source bytes."""

    def __init__(self, code: str, detail: str):
        self.code = code[:80]
        self.detail = detail[:300]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class SourceControlState:
    """Best available source-control state for the complete CAM checkout."""

    kind: str
    git_head: str | None
    dirty: bool | None
    profile_paths_match_head: bool | None = None
    profile_bytes_match_head: bool | None = None
    profile_index_flags_clean: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "git_head": self.git_head,
            "dirty": self.dirty,
            "profile_paths_match_head": self.profile_paths_match_head,
            "profile_bytes_match_head": self.profile_bytes_match_head,
            "profile_index_flags_clean": self.profile_index_flags_clean,
        }


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    """Content digest and source state for all audited reference-tool surfaces."""

    validation_profile_sha256: str
    component_count: int
    source_control: SourceControlState
    python_implementation: str
    python_version: str
    jsonschema_version: str | None
    referencing_version: str | None
    rpds_py_version: str | None
    rfc3339_validator_version: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": PROFILE_FORMAT,
            "validation_profile_sha256": self.validation_profile_sha256,
            "component_count": self.component_count,
            "source_control": self.source_control.as_dict(),
            "runtime": {
                "python_implementation": self.python_implementation,
                "python": self.python_version,
                "jsonschema": self.jsonschema_version,
                "referencing": self.referencing_version,
                "rpds-py": self.rpds_py_version,
                "rfc3339-validator": self.rfc3339_validator_version,
            },
        }


def _profile_paths(root: Path) -> tuple[Path, ...]:
    paths = {root / relative for relative in REQUIRED_PROFILE_PATHS}
    for pattern in REQUIRED_PROFILE_GLOBS:
        matches = tuple(
            path
            for path in root.glob(pattern)
            if _profile_relative_path(path.relative_to(root).as_posix())
        )
        if not matches:
            raise ValidationProfileError(
                "profile.component_missing",
                f"validation profile pattern has no components: {pattern}",
            )
        paths.update(matches)
    for pattern in OPTIONAL_PROFILE_GLOBS:
        paths.update(
            path
            for path in root.glob(pattern)
            if _profile_relative_path(path.relative_to(root).as_posix())
        )
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def _component_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValidationProfileError(
            "profile.component_missing",
            "validation profile component is unavailable",
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValidationProfileError(
            "profile.component_type",
            "validation profile components must be regular non-symlink files",
        )
    if metadata.st_size > MAX_PROFILE_COMPONENT_BYTES:
        raise ValidationProfileError(
            "profile.component_size", "validation profile component is too large"
        )
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValidationProfileError(
            "profile.component_read", "validation profile component is unreadable"
        ) from error
    if len(raw) != metadata.st_size:
        raise ValidationProfileError(
            "profile.component_changed",
            "validation profile component changed while read",
        )
    return raw


def _profile_components(
    paths: tuple[Path, ...],
) -> tuple[tuple[Path, bytes], ...]:
    components: list[tuple[Path, bytes]] = []
    total_bytes = 0
    for path in paths:
        raw = _component_bytes(path)
        total_bytes += len(raw)
        if total_bytes > MAX_PROFILE_TOTAL_BYTES:
            raise ValidationProfileError(
                "profile.total_size", "validation profile components are too large"
            )
        components.append((path, raw))
    return tuple(components)


def _profile_digest(
    root: Path,
    components: tuple[tuple[Path, bytes], ...],
) -> str:
    framed_components: list[dict[str, Any]] = []
    for path, raw in components:
        framed_components.append(
            {
                "path": path.relative_to(root).as_posix(),
                "byte_length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    payload = {
        "format": PROFILE_FORMAT,
        "components": framed_components,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _git_executable() -> str | None:
    for candidate_text in GIT_EXECUTABLE_CANDIDATES:
        candidate = Path(candidate_text)
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and os.access(resolved, os.X_OK):
            return str(resolved)
    return None


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


def _run_git(
    git_bin: str,
    root: Path,
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
                str(root),
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
        raise ValidationProfileError(
            "profile.source_unavailable",
            "validation source-control probe failed",
        ) from error


def _single_git_line(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    if not text or any(character in text for character in "\r\n\x00"):
        return None
    return text


def _profile_relative_path(path_text: str) -> bool:
    if path_text in REQUIRED_PROFILE_PATHS:
        return True
    path = PurePosixPath(path_text)
    if (
        len(path.parts) == 2
        and path.parts[0] == "schemas"
        and path.name.endswith(".schema.json")
    ):
        return True
    if len(path.parts) < 2 or path.parts[0] != "tools":
        return False
    if path.suffix == ".py":
        return True
    if "__pycache__" in path.parts:
        return False
    return path.suffix in {".pyc", ".pyo", ".so", ".pyd"}


def _decode_git_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "validation source-control paths were not valid UTF-8",
        ) from error
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "validation source-control paths were malformed",
        )
    return value


def _head_profile_entries(
    git_bin: str,
    root: Path,
    git_head: str,
) -> dict[str, tuple[str, str]]:
    result = _run_git(
        git_bin,
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        git_head,
        "--",
        *PROFILE_TREE_ROOTS,
    )
    if result.returncode != 0:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "validation source-control tree could not be inspected",
        )
    entries: dict[str, tuple[str, str]] = {}
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_kind, raw_object_id = metadata.split(b" ", 2)
            mode = raw_mode.decode("ascii", errors="strict")
            kind = raw_kind.decode("ascii", errors="strict")
            object_id = raw_object_id.decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise ValidationProfileError(
                "profile.source_unavailable",
                "validation source-control tree output was malformed",
            ) from error
        path_text = _decode_git_path(raw_path)
        if not _profile_relative_path(path_text):
            continue
        if path_text in entries or _GIT_OBJECT_ID.fullmatch(object_id) is None:
            raise ValidationProfileError(
                "profile.source_unavailable",
                "validation source-control tree output was inconsistent",
            )
        entries[path_text] = (mode, kind, object_id)
    return entries


def _index_profile_tags(
    git_bin: str,
    root: Path,
) -> tuple[dict[str, str], bool]:
    result = _run_git(
        git_bin,
        root,
        "ls-files",
        "-v",
        "-z",
        "--",
        *PROFILE_TREE_ROOTS,
    )
    if result.returncode != 0:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "validation source-control index could not be inspected",
        )
    tags: dict[str, str] = {}
    duplicate = False
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise ValidationProfileError(
                "profile.source_unavailable",
                "validation source-control index output was malformed",
            )
        try:
            tag = record[:1].decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationProfileError(
                "profile.source_unavailable",
                "validation source-control index output was malformed",
            ) from error
        path_text = _decode_git_path(record[2:])
        if not _profile_relative_path(path_text):
            continue
        if path_text in tags:
            duplicate = True
        tags[path_text] = tag
    return tags, duplicate


def _git_blob_id(raw: bytes, expected_object_id: str) -> str:
    framed = b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw
    if len(expected_object_id) == 40:
        return hashlib.sha1(framed, usedforsecurity=False).hexdigest()
    if len(expected_object_id) == 64:
        return hashlib.sha256(framed).hexdigest()
    raise ValidationProfileError(
        "profile.source_unavailable",
        "validation source-control object format was unsupported",
    )


def _profile_head_state(
    git_bin: str,
    root: Path,
    git_head: str,
    components: tuple[tuple[Path, bytes], ...],
) -> tuple[bool, bool, bool]:
    working = {path.relative_to(root).as_posix(): raw for path, raw in components}
    head = _head_profile_entries(git_bin, root, git_head)
    head_paths_are_regular = all(
        mode in {"100644", "100755"} and kind == "blob"
        for mode, kind, _ in head.values()
    )
    paths_match = set(working) == set(head) and head_paths_are_regular

    index_tags, duplicate_index_paths = _index_profile_tags(git_bin, root)
    index_flags_clean = (
        not duplicate_index_paths
        and set(index_tags) == set(head)
        and all(tag == "H" for tag in index_tags.values())
    )

    bytes_match = paths_match and all(
        _git_blob_id(raw, head[path_text][2]) == head[path_text][2]
        for path_text, raw in working.items()
    )
    return paths_match, bytes_match, index_flags_clean


def _source_control_state(
    root: Path,
    components: tuple[tuple[Path, bytes], ...],
) -> SourceControlState:
    try:
        marker = root.joinpath(".git").lstat()
    except FileNotFoundError:
        return SourceControlState("not_git", None, None)
    except OSError:
        return SourceControlState("unavailable", None, None)
    if not (stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)):
        return SourceControlState("unavailable", None, None)

    git_bin = _git_executable()
    if git_bin is None:
        return SourceControlState("unavailable", None, None)

    try:
        top_level_result = _run_git(git_bin, root, "rev-parse", "--show-toplevel")
    except ValidationProfileError:
        return SourceControlState("unavailable", None, None)
    if top_level_result.returncode != 0:
        return SourceControlState("unavailable", None, None)
    top_level_text = _single_git_line(top_level_result.stdout)
    if top_level_text is None:
        return SourceControlState("unavailable", None, None)
    top_level = Path(top_level_text).resolve()
    if top_level != root:
        return SourceControlState("unavailable", None, None)

    try:
        head_result = _run_git(
            git_bin,
            root,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
    except ValidationProfileError:
        return SourceControlState("unavailable", None, None)
    if head_result.returncode != 0:
        return SourceControlState("git", None, None)
    git_head = _single_git_line(head_result.stdout)
    if git_head is None or _GIT_OBJECT_ID.fullmatch(git_head) is None:
        return SourceControlState("unavailable", None, None)

    try:
        status_result = _run_git(
            git_bin,
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        )
    except ValidationProfileError:
        return SourceControlState("unavailable", git_head, None)
    if status_result.returncode != 0:
        return SourceControlState("unavailable", git_head, None)
    try:
        paths_match, bytes_match, index_flags_clean = _profile_head_state(
            git_bin,
            root,
            git_head,
            components,
        )
    except ValidationProfileError:
        return SourceControlState("unavailable", git_head, None)
    dirty = bool(status_result.stdout) or not (
        paths_match and bytes_match and index_flags_clean
    )
    return SourceControlState(
        "git",
        git_head,
        dirty,
        profile_paths_match_head=paths_match,
        profile_bytes_match_head=bytes_match,
        profile_index_flags_clean=index_flags_clean,
    )


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def build_validation_profile(root: Path = REPOSITORY_ROOT) -> ValidationProfile:
    """Build a content profile without exposing the local checkout path."""

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "validation profile source root is unavailable",
        ) from error
    paths = _profile_paths(resolved_root)
    components = _profile_components(paths)
    return ValidationProfile(
        validation_profile_sha256=_profile_digest(resolved_root, components),
        component_count=len(components),
        source_control=_source_control_state(resolved_root, components),
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        jsonschema_version=_package_version("jsonschema"),
        referencing_version=_package_version("referencing"),
        rpds_py_version=_package_version("rpds-py"),
        rfc3339_validator_version=_package_version("rfc3339-validator"),
    )


@lru_cache(maxsize=1)
def current_validation_profile() -> ValidationProfile:
    """Return one immutable profile snapshot for this process."""

    try:
        _cam1_bootstrap.verify_captured_sources()
    except _cam1_bootstrap.BootstrapError as error:
        raise ValidationProfileError(
            "profile.bootstrap_changed",
            "CAM source changed after the exact-source bootstrap capture",
        ) from error
    return build_validation_profile()


def validation_profile_report() -> dict[str, Any]:
    """Return a bounded profile result suitable for any CLI outcome."""

    try:
        return {"available": True, **current_validation_profile().as_dict()}
    except ValidationProfileError as error:
        return {
            "available": False,
            "format": PROFILE_FORMAT,
            "error": {"code": error.code, "detail": error.detail},
        }


def require_live_profile(
    *,
    allow_dirty: bool,
    expected_sha256: str | None = None,
) -> ValidationProfile:
    """Require a versioned CAM checkout for every live transport operation."""

    profile = current_validation_profile()
    if expected_sha256 is not None:
        if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise ValidationProfileError(
                "profile.digest_invalid",
                "expected validation profile must be 64 lowercase hexadecimal characters",
            )
        if expected_sha256 != profile.validation_profile_sha256:
            raise ValidationProfileError(
                "profile.digest_mismatch",
                "expected validation profile does not match the running tool source",
            )
    source = profile.source_control
    if source.kind == "unavailable":
        raise ValidationProfileError(
            "profile.source_unavailable",
            "live CAM operations cannot verify this checkout's source-control state",
        )
    if source.kind != "git":
        raise ValidationProfileError(
            "profile.source_unversioned",
            "live CAM operations require a Git checkout with verifiable source state",
        )
    if source.git_head is None:
        raise ValidationProfileError(
            "profile.source_unversioned",
            "live CAM operations require a Git checkout with a resolvable HEAD commit",
        )
    if source.profile_paths_match_head is None:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "live CAM operations cannot verify profile membership in Git HEAD",
        )
    if not source.profile_paths_match_head:
        raise ValidationProfileError(
            "profile.path_set_mismatch",
            "live CAM profile paths must match regular tracked blobs in Git HEAD",
        )
    if source.profile_index_flags_clean is None:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "live CAM operations cannot verify profile index flags",
        )
    if not source.profile_index_flags_clean:
        raise ValidationProfileError(
            "profile.index_concealment",
            "live CAM profile paths cannot use concealed or sparse index flags",
        )
    if source.profile_bytes_match_head is None:
        raise ValidationProfileError(
            "profile.source_unavailable",
            "live CAM operations cannot compare profile bytes with Git HEAD",
        )
    if allow_dirty and expected_sha256 is None:
        raise ValidationProfileError(
            "profile.override_unpinned",
            "dirty-source override requires the exact validation profile digest",
        )
    if source.kind == "git" and source.dirty and not allow_dirty:
        raise ValidationProfileError(
            "profile.dirty_source",
            "live CAM operations refuse a dirty CAM checkout by default",
        )
    return profile
