# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Descriptor-relative, owner-only filesystem primitives for CAM state."""

from __future__ import annotations

import json
import os
import pwd
import stat
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .darwin_acl import clear_fd_extended_acl, fd_has_extended_acl
from .errors import ProjectError
from .native_fs import rename_noreplace

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_PRIVATE_JSON_BYTES = 1_048_576

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


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
