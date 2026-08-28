# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Secure local project identity and owner-only state-file primitives."""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from .darwin_acl import clear_fd_extended_acl, fd_has_extended_acl
from .native_fs import rename_noreplace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROJECT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "cam-project-binding-1.schema.json"
GIT_EXECUTABLE_CANDIDATES = (
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/usr/bin/git",
)
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_PRIVATE_JSON_BYTES = 1_048_576
MAX_GIT_OUTPUT_BYTES = 16_384
PROJECT_LOCK_TIMEOUT_SECONDS = 5.0
PROJECT_LOCK_POLL_SECONDS = 0.025
INITIALIZATION_LOCK_NAME = "initialization.lock"

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_LOCK_FLAGS = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
_LOCK_CREATE_FLAGS = _LOCK_FLAGS | os.O_CREAT | os.O_EXCL


def _default_git_executable() -> str:
    for candidate_text in GIT_EXECUTABLE_CANDIDATES:
        candidate = Path(candidate_text)
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and os.access(resolved, os.X_OK):
            return str(resolved)
    # Preserve a deterministic absolute failure target; discovery will emit a
    # bounded error rather than consulting PATH.
    return GIT_EXECUTABLE_CANDIDATES[-1]


DEFAULT_GIT_BIN = _default_git_executable()


def _git_environment() -> dict[str, str]:
    """Return a minimal, noninteractive environment for read-only Git probes."""

    return {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_probe_prefix(git_bin: str, context: Path) -> list[str]:
    """Build the fixed, side-effect-minimized prefix for a Git query."""

    return [
        git_bin,
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(context),
    ]


class ProjectError(Exception):
    """Bounded project-state failure suitable for a machine-readable CLI."""

    def __init__(self, code: str, detail: str):
        self.code = code[:80]
        self.detail = detail[:300]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class GitContext:
    top_level: Path
    common_dir: Path
    git_dir: Path
    git_bin: str


@dataclass(frozen=True, slots=True)
class ProjectBinding:
    project_id: str
    display_name: str
    state_root: Path
    project_dir: Path
    identity_path: Path
    journal_path: Path
    transaction_lock_path: Path
    git_top_level: Path
    git_common_dir: Path
    git_dir: Path
    git_bin: str
    pointer_path: Path
    worktree_id: str
    worktree_id_path: Path

    def summary(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "state_root": str(self.state_root),
            "project_dir": str(self.project_dir),
            "journal_path": str(self.journal_path),
            "git_top_level": str(self.git_top_level),
            "git_common_dir": str(self.git_common_dir),
            "git_dir": str(self.git_dir),
            "git_bin": self.git_bin,
            "worktree_id": self.worktree_id,
        }


@dataclass(frozen=True, slots=True)
class ProjectTransaction:
    """Capability proving that this process holds one project's mutation lock."""

    project_id: str
    project_dir: Path
    lock_path: Path
    descriptor: int
    device: int
    inode: int
    _cache: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


_ACTIVE_TRANSACTION: ContextVar[ProjectTransaction | None] = ContextVar(
    "cam1_active_project_transaction", default=None
)


def _load_schema() -> dict[str, Any]:
    with PROJECT_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = cast(dict[str, Any], json.load(handle))
    Draft202012Validator.check_schema(schema)
    return schema


_PROJECT_VALIDATOR = Draft202012Validator(
    _load_schema(), format_checker=FormatChecker()
)


def _utc_text(value: dt.datetime | None = None) -> str:
    observed = value or dt.datetime.now(dt.UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ProjectError("project.timestamp", "timestamp must be timezone-aware")
    return (
        observed.astimezone(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_json_bytes(payload: Mapping[str, Any], *, max_bytes: int) -> bytes:
    try:
        raw = (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ProjectError(
            "state.json_invalid", "state document must contain finite JSON values"
        ) from None
    if len(raw) > max_bytes:
        raise ProjectError(
            "state.size_limit", f"state document exceeds {max_bytes} bytes"
        )
    return raw


def _normalize_local_path(path: Path | str) -> Path:
    """Return one lexical absolute path without resolving filesystem links."""

    candidate = Path(path)
    if ".." in candidate.parts:
        raise ProjectError(
            "path.component", "path must not contain parent-directory references"
        )
    try:
        expanded_candidate = candidate.expanduser()
        if ".." in expanded_candidate.parts:
            raise ProjectError(
                "path.component", "path must not contain parent-directory references"
            )
        expanded = os.path.abspath(os.fspath(expanded_candidate))
    except ProjectError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ProjectError("path.resolve", "directory could not be resolved") from None

    # These are fixed root-owned compatibility aliases on macOS. Normalize
    # only there; /tmp and /var are real directories on supported Linux CI.
    if sys.platform == "darwin" and (
        expanded in {"/tmp", "/var"} or expanded.startswith(("/tmp/", "/var/"))
    ):
        expanded = f"/private{expanded}"
    return Path(expanded)


def _split_local_file_path(
    path: Path | str, *, require_absolute: bool = False
) -> tuple[Path, str]:
    """Split a lexical local file path for descriptor-relative access."""

    candidate = Path(path)
    if require_absolute and not candidate.is_absolute():
        raise ProjectError("path.absolute", "state-file path must be absolute")
    normalized = _normalize_local_path(candidate)
    if normalized.name in {"", ".", ".."}:
        raise ProjectError("path.component", "path must name one file")
    return normalized.parent, normalized.name


def _canonical_existing_directory(path: Path) -> Path:
    candidate = _normalize_local_path(path)
    descriptor = _open_directory_fd(candidate)
    os.close(descriptor)
    return candidate


def _open_directory_fd(path: Path) -> int:
    """Open an absolute directory through no-follow component descriptors."""

    if not path.is_absolute():
        raise ProjectError("path.absolute", "path must be absolute")
    path = _normalize_local_path(path)
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            next_descriptor = _open_directory_component(descriptor, component)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProjectError("path.type", "path must resolve to a directory")
        return descriptor
    except FileNotFoundError:
        os.close(descriptor)
        raise ProjectError("path.missing", "directory does not exist") from None
    except OSError:
        os.close(descriptor)
        raise ProjectError(
            "path.open", "directory path must contain only accessible directories"
        ) from None
    except Exception:
        os.close(descriptor)
        raise


def _open_directory_component(parent_descriptor: int, component: str) -> int:
    """Open one no-follow path component relative to a verified directory."""

    try:
        return os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        try:
            metadata = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise error from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ProjectError(
                "path.symlink", "directory path must not contain symlinks"
            ) from None
        raise error


def _require_owned(metadata: os.stat_result, *, label: str) -> None:
    if metadata.st_uid != os.getuid():
        raise ProjectError(
            f"{label}.owner", f"{label} must be owned by the current user"
        )


def _require_no_extended_acl(descriptor: int, *, label: str) -> None:
    try:
        has_extended_acl = fd_has_extended_acl(descriptor)
    except OSError:
        raise ProjectError(
            f"{label}.acl_check", f"{label} extended ACL could not be inspected"
        ) from None
    if has_extended_acl:
        raise ProjectError(
            f"{label}.acl", f"{label} must not have a macOS extended ACL"
        )


def _clear_created_inode_acl(descriptor: int, *, label: str) -> None:
    try:
        clear_fd_extended_acl(descriptor)
    except OSError:
        raise ProjectError(
            f"{label}.acl_clear",
            f"{label} inherited extended ACL could not be cleared",
        ) from None


def _require_private_directory(descriptor: int, *, label: str) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectError(f"{label}.type", f"{label} must be a directory")
    _require_owned(metadata, label=label)
    if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
        raise ProjectError(f"{label}.mode", f"{label} must have mode 0700")
    _require_no_extended_acl(descriptor, label=label)


def _prepare_created_private_directory(descriptor: int, *, label: str) -> None:
    _require_owned(os.fstat(descriptor), label=label)
    os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
    _clear_created_inode_acl(descriptor, label=label)
    _require_private_directory(descriptor, label=label)


def _remove_matching_directory(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> bool:
    """Best-effort removal of one unchanged, empty directory we created."""

    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            return False
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except OSError:
        return False
    return True


def _open_private_directory(path: Path, *, label: str) -> int:
    descriptor = _open_directory_fd(path)
    try:
        _require_private_directory(descriptor, label=label)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _create_staged_private_directory(
    parent_descriptor: int, *, label: str
) -> tuple[str, int, tuple[int, int]]:
    """Create and prepare one unpredictable unpublished directory."""

    staged_name = f".cam1-directory-{uuid.uuid4().hex}.tmp"
    observed_identity: tuple[int, int] | None = None
    cleanup_identity: tuple[int, int] | None = None
    staged_descriptor: int | None = None
    try:
        os.mkdir(staged_name, PRIVATE_DIRECTORY_MODE, dir_fd=parent_descriptor)
        path_metadata = os.stat(
            staged_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        observed_identity = (path_metadata.st_dev, path_metadata.st_ino)
        staged_descriptor = os.open(
            staged_name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
        )
        opened_metadata = os.fstat(staged_descriptor)
        if observed_identity != (opened_metadata.st_dev, opened_metadata.st_ino):
            raise ProjectError(
                f"{label}.identity", f"{label} identity changed during creation"
            )
        _require_owned(opened_metadata, label=label)
        # Cleanup becomes eligible only after the opened inode is proven to
        # belong to this account. Never chmod, clear, or remove a foreign inode.
        cleanup_identity = (opened_metadata.st_dev, opened_metadata.st_ino)
        _prepare_created_private_directory(staged_descriptor, label=label)
        os.fsync(staged_descriptor)
        return staged_name, staged_descriptor, cleanup_identity
    except Exception:
        if staged_descriptor is not None:
            os.close(staged_descriptor)
        if cleanup_identity is not None:
            _remove_matching_directory(parent_descriptor, staged_name, cleanup_identity)
        raise


def _ensure_private_child(parent: Path, name: str, *, label: str) -> Path:
    if not name or name in {".", ".."} or "/" in name:
        raise ProjectError("path.component", "managed directory name is invalid")
    parent_descriptor = _open_directory_fd(parent)
    try:
        _require_owned(os.fstat(parent_descriptor), label=f"{label}.parent")
        try:
            child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        except FileNotFoundError:
            staged_name, child_descriptor, created_identity = (
                _create_staged_private_directory(parent_descriptor, label=label)
            )
            published = False
            try:
                try:
                    rename_noreplace(parent_descriptor, staged_name, name)
                except FileExistsError:
                    _remove_matching_directory(
                        parent_descriptor, staged_name, created_identity
                    )
                    winner_descriptor = os.open(
                        name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
                    )
                    try:
                        _require_private_directory(winner_descriptor, label=label)
                    finally:
                        os.close(winner_descriptor)
                    return parent / name
                published = True
                path_metadata = os.stat(
                    name, dir_fd=parent_descriptor, follow_symlinks=False
                )
                opened_metadata = os.fstat(child_descriptor)
                if created_identity != (
                    path_metadata.st_dev,
                    path_metadata.st_ino,
                ) or created_identity != (
                    opened_metadata.st_dev,
                    opened_metadata.st_ino,
                ):
                    raise ProjectError(
                        f"{label}.identity",
                        f"{label} identity changed during publication",
                    )
                _require_private_directory(child_descriptor, label=label)
                os.fsync(parent_descriptor)
                return parent / name
            except Exception:
                _remove_matching_directory(
                    parent_descriptor,
                    name if published else staged_name,
                    created_identity,
                )
                raise
            finally:
                os.close(child_descriptor)
        else:
            try:
                _require_private_directory(child_descriptor, label=label)
            finally:
                os.close(child_descriptor)
            return parent / name
    except OSError:
        raise ProjectError(
            f"{label}.create", f"{label} could not be created securely"
        ) from None
    finally:
        os.close(parent_descriptor)


def account_home() -> Path:
    """Return the current account's configured home without trusting ``$HOME``."""

    try:
        configured = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        raise ProjectError(
            "account.home", "current account has no configured home"
        ) from None
    return _canonical_existing_directory(Path(configured))


def resolve_state_root(
    state_root: Path | str | None = None, *, create: bool = False
) -> Path:
    """Resolve the owner-only journal root, optionally creating managed components."""

    if state_root is None:
        home = account_home()
        cam_path = home / "CAM"
        journals_path = cam_path / "Journals"
        if create:
            cam_path = _ensure_private_child(home, "CAM", label="state.cam_directory")
            journals_path = _ensure_private_child(
                cam_path, "Journals", label="state.root"
            )
        else:
            cam_path = _canonical_existing_directory(cam_path)
            journals_path = _canonical_existing_directory(journals_path)
        cam_descriptor = _open_private_directory(cam_path, label="state.cam_directory")
        os.close(cam_descriptor)
        descriptor = _open_private_directory(journals_path, label="state.root")
        os.close(descriptor)
        return journals_path

    supplied = Path(state_root)
    if not supplied.is_absolute():
        raise ProjectError("state.root_absolute", "state root must be an absolute path")
    if supplied.exists() or supplied.is_symlink():
        resolved = _canonical_existing_directory(supplied)
    else:
        if not create:
            raise ProjectError("state.root_missing", "state root is not initialized")
        parent = _canonical_existing_directory(supplied.parent)
        resolved = _ensure_private_child(parent, supplied.name, label="state.root")
    descriptor = _open_private_directory(resolved, label="state.root")
    os.close(descriptor)
    return resolved


def _validate_private_file_metadata(metadata: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ProjectError(f"{label}.type", f"{label} must be a regular file")
    _require_owned(metadata, label=label)
    if metadata.st_nlink != 1:
        raise ProjectError(f"{label}.links", f"{label} must have exactly one hard link")
    if stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
        raise ProjectError(f"{label}.mode", f"{label} must have mode 0600")


def _validate_private_file(descriptor: int, *, label: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    _validate_private_file_metadata(metadata, label=label)
    _require_no_extended_acl(descriptor, label=label)
    return metadata


def _prepare_created_private_file(descriptor: int, *, label: str) -> os.stat_result:
    _require_owned(os.fstat(descriptor), label=label)
    os.fchmod(descriptor, PRIVATE_FILE_MODE)
    _clear_created_inode_acl(descriptor, label=label)
    return _validate_private_file(descriptor, label=label)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise ProjectError("state.write", "state-file write did not make progress")
        view = view[written:]


def create_private_bytes(path: Path, raw: bytes) -> None:
    """Atomically publish one new owner-only regular file without replacement."""

    parent, name = _split_local_file_path(path, require_absolute=True)
    parent_descriptor = _open_private_directory(parent, label="state.parent")
    temporary_name = f".cam1-{uuid.uuid4().hex}.tmp"
    temporary_identity: tuple[int, int] | None = None
    temporary_created = False
    published = False
    succeeded = False
    try:
        try:
            descriptor = os.open(
                temporary_name,
                _CREATE_FLAGS,
                PRIVATE_FILE_MODE,
                dir_fd=parent_descriptor,
            )
        except OSError:
            raise ProjectError(
                "state.create", "state file could not be created securely"
            ) from None
        temporary_created = True
        try:
            metadata = os.fstat(descriptor)
            temporary_identity = (metadata.st_dev, metadata.st_ino)
            metadata = _prepare_created_private_file(
                descriptor, label="state.temporary"
            )
            try:
                _write_all(descriptor, raw)
                os.fsync(descriptor)
            except ProjectError:
                raise
            except OSError:
                raise ProjectError(
                    "state.write", "state file could not be written completely"
                ) from None
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise ProjectError(
                "state.create", "state file must not already exist"
            ) from None
        published = True
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_created = False
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
        try:
            final_metadata = _validate_private_file(descriptor, label="state.file")
        finally:
            os.close(descriptor)
        if temporary_identity != (final_metadata.st_dev, final_metadata.st_ino):
            raise ProjectError("state.create", "state file identity changed")
        try:
            os.fsync(parent_descriptor)
        except OSError:
            raise ProjectError(
                "state.create", "state file publication could not be synchronized"
            ) from None
        succeeded = True
    except ProjectError:
        raise
    except OSError:
        raise ProjectError(
            "state.create", "state file could not be created securely"
        ) from None
    finally:
        if not succeeded and published and temporary_identity is not None:
            _unlink_matching_entry(parent_descriptor, name, temporary_identity)
        if temporary_created and temporary_identity is not None:
            _unlink_matching_entry(
                parent_descriptor, temporary_name, temporary_identity
            )
        if not succeeded:
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass
        os.close(parent_descriptor)


def _unlink_matching_entry(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> bool:
    """Best-effort removal of an entry only when it is still our inode."""

    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != expected_identity:
            return False
        os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        return False
    return True


def create_private_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    max_bytes: int = MAX_PRIVATE_JSON_BYTES,
) -> None:
    create_private_bytes(path, _canonical_json_bytes(payload, max_bytes=max_bytes))


def read_private_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read a bounded owner-only regular file without following its final component."""

    parent, name = _split_local_file_path(path, require_absolute=True)
    parent_descriptor = _open_private_directory(parent, label="state.parent")
    try:
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
        except OSError:
            raise ProjectError("state.open", "state file could not be opened") from None
        try:
            _validate_private_file(descriptor, label="state.file")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
    if len(raw) > max_bytes:
        raise ProjectError("state.size_limit", f"state file exceeds {max_bytes} bytes")
    return raw


def require_private_file(path: Path) -> os.stat_result:
    """Validate one private regular file without reading or modifying it."""

    parent, name = _split_local_file_path(path, require_absolute=True)
    parent_descriptor = _open_private_directory(parent, label="state.parent")
    try:
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
        except OSError:
            raise ProjectError("state.open", "state file could not be opened") from None
        try:
            metadata = _validate_private_file(descriptor, label="state.file")
            return metadata
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def require_project_transaction(
    project: ProjectBinding, transaction: ProjectTransaction
) -> None:
    """Reject a stale transaction token or one belonging to another project."""

    if _ACTIVE_TRANSACTION.get() is not transaction:
        raise ProjectError(
            "transaction.inactive",
            "transaction is not the active lock capability in this context",
        )
    expected = project.project_dir / "transaction.lock"
    if (
        project.transaction_lock_path != expected
        or transaction.project_id != project.project_id
        or transaction.project_dir != project.project_dir
        or transaction.lock_path != expected
    ):
        raise ProjectError(
            "transaction.binding", "transaction does not match the project binding"
        )
    try:
        metadata = os.fstat(transaction.descriptor)
    except OSError:
        raise ProjectError(
            "transaction.inactive", "transaction is no longer active"
        ) from None
    _validate_private_file(transaction.descriptor, label="transaction.lock")
    if metadata.st_dev != transaction.device or metadata.st_ino != transaction.inode:
        raise ProjectError("transaction.inactive", "transaction lock identity changed")


def _transaction_cache(
    project: ProjectBinding, transaction: ProjectTransaction
) -> dict[str, Any]:
    """Return process-local caches only after revalidating the lock capability."""

    require_project_transaction(project, transaction)
    return transaction._cache


def current_project_transaction(
    project: ProjectBinding,
) -> ProjectTransaction | None:
    """Return and validate this context's transaction for ``project``, if any."""

    transaction = _ACTIVE_TRANSACTION.get()
    if transaction is not None:
        require_project_transaction(project, transaction)
    return transaction


@contextmanager
def project_transaction(project: ProjectBinding) -> Iterator[ProjectTransaction]:
    """Serialize replay, prospective validation, append, and projection refresh."""

    active = current_project_transaction(project)
    if active is not None:
        yield active
        return
    expected = project.project_dir / "transaction.lock"
    if project.transaction_lock_path != expected:
        raise ProjectError(
            "transaction.binding", "transaction lock does not match the project binding"
        )
    parent_descriptor = _open_private_directory(
        project.project_dir, label="project.directory"
    )
    try:
        try:
            descriptor = os.open(expected.name, _LOCK_FLAGS, dir_fd=parent_descriptor)
        except OSError:
            raise ProjectError(
                "transaction.open", "project transaction lock could not be opened"
            ) from None
        try:
            _validate_private_file(descriptor, label="transaction.lock")
            deadline = time.monotonic() + PROJECT_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise ProjectError(
                            "transaction.lock",
                            "project transaction lock could not be acquired",
                        ) from None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProjectError(
                            "transaction.busy",
                            "project journal is busy; retry this operation later",
                        ) from None
                    time.sleep(min(PROJECT_LOCK_POLL_SECONDS, remaining))
            locked_metadata = _validate_private_file(
                descriptor, label="transaction.lock"
            )
            try:
                path_metadata = os.stat(
                    expected.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                raise ProjectError(
                    "transaction.identity",
                    "project transaction lock identity changed",
                ) from None
            _validate_private_file_metadata(path_metadata, label="transaction.lock")
            if (
                path_metadata.st_dev != locked_metadata.st_dev
                or path_metadata.st_ino != locked_metadata.st_ino
            ):
                raise ProjectError(
                    "transaction.identity",
                    "project transaction lock identity changed",
                )
            transaction = ProjectTransaction(
                project_id=project.project_id,
                project_dir=project.project_dir,
                lock_path=expected,
                descriptor=descriptor,
                device=locked_metadata.st_dev,
                inode=locked_metadata.st_ino,
            )
            context_token = _ACTIVE_TRANSACTION.set(transaction)
            try:
                yield transaction
            finally:
                transaction._cache.clear()
                _ACTIVE_TRANSACTION.reset(context_token)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object member")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def read_private_json(
    path: Path, *, max_bytes: int = MAX_PRIVATE_JSON_BYTES
) -> dict[str, Any]:
    raw = read_private_bytes(path, max_bytes=max_bytes)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProjectError(
            "state.json_invalid", "state file is not strict JSON"
        ) from None
    if not isinstance(value, dict):
        raise ProjectError("state.json_type", "state document must be an object")
    return value


def replace_private_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    max_bytes: int = MAX_PRIVATE_JSON_BYTES,
) -> None:
    """Atomically create or replace a private projection in one managed directory."""

    raw = _canonical_json_bytes(payload, max_bytes=max_bytes)
    parent, name = _split_local_file_path(path, require_absolute=True)
    parent_descriptor = _open_private_directory(parent, label="state.parent")
    temporary_name = f".{name}.{uuid.uuid4()}.tmp"
    temporary_created = False
    temporary_identity: tuple[int, int] | None = None
    try:
        try:
            existing_descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        else:
            try:
                _validate_private_file(existing_descriptor, label="state.file")
            finally:
                os.close(existing_descriptor)
        descriptor = os.open(
            temporary_name,
            _CREATE_FLAGS,
            PRIVATE_FILE_MODE,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        try:
            metadata = os.fstat(descriptor)
            temporary_identity = (metadata.st_dev, metadata.st_ino)
            _prepare_created_private_file(descriptor, label="state.temporary")
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_created = False
        os.fsync(parent_descriptor)
    except OSError:
        raise ProjectError(
            "state.replace", "state projection could not be replaced"
        ) from None
    finally:
        if temporary_created and temporary_identity is not None:
            _unlink_matching_entry(
                parent_descriptor, temporary_name, temporary_identity
            )
        os.close(parent_descriptor)


def _validate_project_document(
    document: dict[str, Any], *, expected_format: str
) -> None:
    errors = list(_PROJECT_VALIDATOR.iter_errors(document))
    if errors or document.get("format") != expected_format:
        raise ProjectError(
            "project.document_invalid", "project state document failed validation"
        )


def _resolve_executable(path_text: str) -> str:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise ProjectError("git.path", "git executable path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise ProjectError("git.not_found", "git executable was not found") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise ProjectError("git.not_executable", "git path is not an executable file")
    return str(resolved)


def _git_output(git_bin: str, context: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [*_git_probe_prefix(git_bin, context), "rev-parse", *arguments],
            check=False,
            capture_output=True,
            env=_git_environment(),
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProjectError(
            "git.probe_failed", "git discovery did not complete"
        ) from None
    if completed.returncode != 0 or len(completed.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise ProjectError(
            "git.not_worktree", "project root must be inside a Git worktree"
        )
    try:
        value = completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        raise ProjectError(
            "git.output", "git discovery returned malformed UTF-8"
        ) from None
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ProjectError("git.output", "git discovery returned an invalid path")
    return value


def _prospective_state_root(state_root: Path | str | None) -> Path:
    """Resolve a state-root candidate without creating any filesystem state."""

    if state_root is None:
        candidate = account_home() / "CAM" / "Journals"
    else:
        candidate = Path(state_root)
        if not candidate.is_absolute():
            raise ProjectError(
                "state.root_absolute", "state root must be an absolute path"
            )
    try:
        return _normalize_local_path(candidate)
    except ProjectError:
        raise ProjectError(
            "state.root_resolve", "state root could not be resolved"
        ) from None


def _path_is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _require_external_state_root(candidate: Path, context: GitContext) -> None:
    """Reject journal roots in a worktree or Git administrative directory."""

    for boundary in (context.top_level, context.common_dir, context.git_dir):
        if _path_is_within(candidate, boundary):
            raise ProjectError(
                "state.root_git_overlap",
                "state root must remain outside Git worktrees and administrative directories",
            )

    probe = candidate
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    probe = _canonical_existing_directory(probe)
    try:
        completed = subprocess.run(
            [
                *_git_probe_prefix(context.git_bin, probe),
                "rev-parse",
                "--is-inside-work-tree",
                "--is-inside-git-dir",
            ],
            check=False,
            capture_output=True,
            env=_git_environment(),
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProjectError(
            "state.root_git_probe", "state-root Git containment check did not complete"
        ) from None
    if (
        len(completed.stdout) > MAX_GIT_OUTPUT_BYTES
        or len(completed.stderr) > MAX_GIT_OUTPUT_BYTES
    ):
        raise ProjectError(
            "state.root_git_probe", "state-root Git containment output was invalid"
        )
    if completed.returncode != 0:
        return
    try:
        flags = completed.stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError:
        raise ProjectError(
            "state.root_git_probe", "state-root Git containment output was invalid"
        ) from None
    if len(flags) != 2 or any(value not in {"true", "false"} for value in flags):
        raise ProjectError(
            "state.root_git_probe", "state-root Git containment output was invalid"
        )
    if "true" in flags:
        raise ProjectError(
            "state.root_git_overlap",
            "state root must remain outside Git worktrees and administrative directories",
        )


def discover_git_context(
    project_root: Path | str, *, git_bin: str = DEFAULT_GIT_BIN
) -> GitContext:
    context = _canonical_existing_directory(Path(project_root))
    executable = _resolve_executable(git_bin)
    if _git_output(executable, context, "--is-inside-work-tree") != "true":
        raise ProjectError(
            "git.not_worktree", "project root must be inside a Git worktree"
        )
    top_level = _canonical_existing_directory(
        Path(
            _git_output(
                executable, context, "--path-format=absolute", "--show-toplevel"
            )
        )
    )
    common_dir = _canonical_existing_directory(
        Path(
            _git_output(
                executable, context, "--path-format=absolute", "--git-common-dir"
            )
        )
    )
    git_dir = _canonical_existing_directory(
        Path(_git_output(executable, context, "--path-format=absolute", "--git-dir"))
    )
    for label, directory in (("git.common", common_dir), ("git.directory", git_dir)):
        descriptor = _open_directory_fd(directory)
        try:
            _require_owned(os.fstat(descriptor), label=label)
        finally:
            os.close(descriptor)
    return GitContext(
        top_level=top_level,
        common_dir=common_dir,
        git_dir=git_dir,
        git_bin=executable,
    )


def _display_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48].rstrip("-")
    return slug or "project"


def _cam_admin_dir(git_directory: Path) -> Path:
    return git_directory / "cam1"


def _ensure_cam_admin_dir(git_directory: Path) -> Path:
    return _ensure_private_child(
        git_directory, "cam1", label="project.git_admin_directory"
    )


def _existing_cam_admin_dir(git_directory: Path) -> Path:
    path = _canonical_existing_directory(_cam_admin_dir(git_directory))
    descriptor = _open_private_directory(path, label="project.git_admin_directory")
    os.close(descriptor)
    return path


@contextmanager
def _project_initialization_lock(context: GitContext) -> Iterator[Path]:
    """Serialize one Git common directory's project-binding initialization."""

    common_admin = _ensure_cam_admin_dir(context.common_dir)
    parent_descriptor = _open_private_directory(
        common_admin, label="project.git_admin_directory"
    )
    descriptor: int | None = None
    acquired = False
    try:
        created = False
        try:
            descriptor = os.open(
                INITIALIZATION_LOCK_NAME,
                _LOCK_CREATE_FLAGS,
                PRIVATE_FILE_MODE,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            try:
                descriptor = os.open(
                    INITIALIZATION_LOCK_NAME,
                    _LOCK_FLAGS,
                    dir_fd=parent_descriptor,
                )
            except OSError:
                raise ProjectError(
                    "project.initialization_open",
                    "project initialization lock could not be opened",
                ) from None
        except OSError:
            raise ProjectError(
                "project.initialization_create",
                "project initialization lock could not be created",
            ) from None
        else:
            created = True

        if created:
            created_metadata = os.fstat(descriptor)
            created_identity = (created_metadata.st_dev, created_metadata.st_ino)
            try:
                _prepare_created_private_file(
                    descriptor, label="project.initialization_lock"
                )
                os.fsync(descriptor)
                os.fsync(parent_descriptor)
            except Exception:
                if _unlink_matching_entry(
                    parent_descriptor,
                    INITIALIZATION_LOCK_NAME,
                    created_identity,
                ):
                    try:
                        os.fsync(parent_descriptor)
                    except OSError:
                        pass
                raise
        _validate_private_file(descriptor, label="project.initialization_lock")

        deadline = time.monotonic() + PROJECT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise ProjectError(
                        "project.initialization_lock",
                        "project initialization lock could not be acquired",
                    ) from None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProjectError(
                        "project.initialization_busy",
                        "project initialization is busy; retry later",
                    ) from None
                time.sleep(min(PROJECT_LOCK_POLL_SECONDS, remaining))

        # Validate after acquisition as well as before it. A lock on an
        # unlinked or substituted inode must not authorize initialization.
        locked_metadata = _validate_private_file(
            descriptor, label="project.initialization_lock"
        )
        try:
            path_metadata = os.stat(
                INITIALIZATION_LOCK_NAME,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise ProjectError(
                "project.initialization_identity",
                "project initialization lock identity changed",
            ) from None
        _validate_private_file_metadata(
            path_metadata, label="project.initialization_lock"
        )
        if (
            path_metadata.st_dev != locked_metadata.st_dev
            or path_metadata.st_ino != locked_metadata.st_ino
        ):
            raise ProjectError(
                "project.initialization_identity",
                "project initialization lock identity changed",
            )
        yield common_admin
    finally:
        if descriptor is not None:
            try:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        os.close(parent_descriptor)


def _create_worktree_binding(
    context: GitContext,
    *,
    project_id: str,
    created_at: str,
) -> tuple[str, Path]:
    admin_dir = _ensure_cam_admin_dir(context.git_dir)
    path = admin_dir / "worktree-id"
    worktree_id = str(uuid.uuid4())
    document = {
        "format": "CAM-WORKTREE/1",
        "project_id": project_id,
        "worktree_id": worktree_id,
        "bound_git_dir": str(context.git_dir),
        "created_at": created_at,
    }
    _validate_project_document(document, expected_format="CAM-WORKTREE/1")
    create_private_json(path, document)
    return worktree_id, path


def _read_worktree_binding(context: GitContext, *, project_id: str) -> tuple[str, Path]:
    path = _existing_cam_admin_dir(context.git_dir) / "worktree-id"
    document = read_private_json(path)
    _validate_project_document(document, expected_format="CAM-WORKTREE/1")
    if document["project_id"] != project_id or document["bound_git_dir"] != str(
        context.git_dir
    ):
        raise ProjectError(
            "project.worktree_mismatch",
            "worktree binding does not match the current Git worktree",
        )
    return cast(str, document["worktree_id"]), path


def _initialize_project_locked(
    project_root: Path | str,
    *,
    context: GitContext,
    root: Path,
    git_bin: str,
    now: dt.datetime | None,
    common_admin: Path,
) -> ProjectBinding:
    """Read or create project state while the common-directory lock is held."""

    pointer_path = common_admin / "project.json"
    if pointer_path.exists() or pointer_path.is_symlink():
        pointer = read_private_json(pointer_path)
        _validate_project_document(pointer, expected_format="CAM-PROJECT-POINTER/1")
        if pointer["bound_git_common_dir"] != str(context.common_dir):
            raise ProjectError(
                "project.binding_mismatch",
                "project pointer is bound to a different Git common directory",
            )
        if pointer["bound_state_root"] != str(root):
            raise ProjectError(
                "project.state_root_mismatch",
                "configured state root does not equal the Git-bound journal root",
            )
        project_id = cast(str, pointer["project_id"])
        _ensure_cam_admin_dir(context.git_dir)
        try:
            _read_worktree_binding(context, project_id=project_id)
        except ProjectError as error:
            if error.code == "state.open":
                _create_worktree_binding(
                    context,
                    project_id=project_id,
                    created_at=_utc_text(now),
                )
            else:
                raise
        return resolve_project(project_root, state_root=root, git_bin=git_bin)

    worktree_path = _cam_admin_dir(context.git_dir) / "worktree-id"
    if worktree_path.exists() or worktree_path.is_symlink():
        raise ProjectError(
            "project.partial_state",
            "worktree binding exists without a project pointer; no repair was attempted",
        )

    created_at = _utc_text(now)
    project_id = str(uuid.uuid4())
    display_name = context.top_level.name or "project"
    project_directory_name = f"{_display_slug(display_name)}--{project_id}"
    project_dir = _ensure_private_child(
        root, project_directory_name, label="project.directory"
    )
    identity_path = project_dir / "identity.json"
    journal_path = project_dir / "journal.jsonl"
    transaction_lock_path = project_dir / "transaction.lock"
    identity = {
        "format": "CAM-PROJECT-IDENTITY/1",
        "project_id": project_id,
        "display_name": display_name,
        "created_at": created_at,
        "bound_state_root": str(root),
        "bound_git_common_dir": str(context.common_dir),
        "initial_git_top_level": str(context.top_level),
    }
    _validate_project_document(identity, expected_format="CAM-PROJECT-IDENTITY/1")
    create_private_json(identity_path, identity)
    create_private_bytes(journal_path, b"")
    create_private_bytes(transaction_lock_path, b"")
    worktree_id, worktree_id_path = _create_worktree_binding(
        context, project_id=project_id, created_at=created_at
    )
    pointer = {
        "format": "CAM-PROJECT-POINTER/1",
        "project_id": project_id,
        "project_directory": project_directory_name,
        "bound_state_root": str(root),
        "bound_git_common_dir": str(context.common_dir),
        "created_at": created_at,
    }
    _validate_project_document(pointer, expected_format="CAM-PROJECT-POINTER/1")
    create_private_json(pointer_path, pointer)
    return ProjectBinding(
        project_id=project_id,
        display_name=display_name,
        state_root=root,
        project_dir=project_dir,
        identity_path=identity_path,
        journal_path=journal_path,
        transaction_lock_path=transaction_lock_path,
        git_top_level=context.top_level,
        git_common_dir=context.common_dir,
        git_dir=context.git_dir,
        git_bin=context.git_bin,
        pointer_path=pointer_path,
        worktree_id=worktree_id,
        worktree_id_path=worktree_id_path,
    )


def initialize_project(
    project_root: Path | str,
    *,
    state_root: Path | str | None = None,
    git_bin: str = DEFAULT_GIT_BIN,
    now: dt.datetime | None = None,
) -> ProjectBinding:
    """Create a project binding and empty required journal without overwriting state."""

    context = discover_git_context(project_root, git_bin=git_bin)
    candidate = _prospective_state_root(state_root)
    _require_external_state_root(candidate, context)
    root = resolve_state_root(state_root, create=True)
    _require_external_state_root(root, context)
    with _project_initialization_lock(context) as common_admin:
        # Nothing about pointer, project, or worktree state is trusted from a
        # pre-lock observation. The locked helper re-reads every binding.
        return _initialize_project_locked(
            project_root,
            context=context,
            root=root,
            git_bin=git_bin,
            now=now,
            common_admin=common_admin,
        )


def resolve_project(
    project_root: Path | str,
    *,
    state_root: Path | str | None = None,
    git_bin: str = DEFAULT_GIT_BIN,
) -> ProjectBinding:
    """Resolve and validate an existing project and worktree binding."""

    context = discover_git_context(project_root, git_bin=git_bin)
    root = resolve_state_root(state_root, create=False)
    _require_external_state_root(root, context)
    pointer_path = _existing_cam_admin_dir(context.common_dir) / "project.json"
    pointer = read_private_json(pointer_path)
    _validate_project_document(pointer, expected_format="CAM-PROJECT-POINTER/1")
    if pointer["bound_git_common_dir"] != str(context.common_dir):
        raise ProjectError(
            "project.binding_mismatch",
            "project pointer is bound to a different Git common directory",
        )
    if pointer["bound_state_root"] != str(root):
        raise ProjectError(
            "project.state_root_mismatch",
            "configured state root does not equal the Git-bound journal root",
        )
    project_id = cast(str, pointer["project_id"])
    directory_name = cast(str, pointer["project_directory"])
    if not directory_name.endswith(f"--{project_id}"):
        raise ProjectError(
            "project.directory_mismatch", "project directory does not match project ID"
        )
    project_dir = _canonical_existing_directory(root / directory_name)
    project_descriptor = _open_private_directory(project_dir, label="project.directory")
    os.close(project_descriptor)
    identity_path = project_dir / "identity.json"
    journal_path = project_dir / "journal.jsonl"
    transaction_lock_path = project_dir / "transaction.lock"
    identity = read_private_json(identity_path)
    _validate_project_document(identity, expected_format="CAM-PROJECT-IDENTITY/1")
    if (
        identity["project_id"] != project_id
        or identity["bound_state_root"] != str(root)
        or identity["bound_git_common_dir"] != str(context.common_dir)
    ):
        raise ProjectError(
            "project.identity_mismatch",
            "project identity does not match the Git binding",
        )
    # Opening the empty-or-populated journal here validates its file boundary only.
    require_private_file(journal_path)
    require_private_file(transaction_lock_path)
    worktree_id, worktree_id_path = _read_worktree_binding(
        context, project_id=project_id
    )
    return ProjectBinding(
        project_id=project_id,
        display_name=cast(str, identity["display_name"]),
        state_root=root,
        project_dir=project_dir,
        identity_path=identity_path,
        journal_path=journal_path,
        transaction_lock_path=transaction_lock_path,
        git_top_level=context.top_level,
        git_common_dir=context.common_dir,
        git_dir=context.git_dir,
        git_bin=context.git_bin,
        pointer_path=pointer_path,
        worktree_id=worktree_id,
        worktree_id_path=worktree_id_path,
    )
