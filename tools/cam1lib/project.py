# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Secure project binding and transaction orchestration.

Owner-only filesystem and Git-discovery implementations live in focused
modules. Their established imports remain available here for CLI and library
compatibility.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ProjectError
from .project_git import (
    DEFAULT_GIT_BIN,
    GIT_EXECUTABLE_CANDIDATES,
    MAX_GIT_OUTPUT_BYTES,
    GitContext,
    _git_environment,
    _git_probe_prefix,
    discover_git_context,
)
from .secure_fs import (
    MAX_PRIVATE_JSON_BYTES,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    _canonical_existing_directory,
    _ensure_private_child,
    _normalize_local_path,
    _open_private_directory,
    _prepare_created_private_file,
    _unlink_matching_entry,
    _validate_private_file,
    _validate_private_file_metadata,
    account_home,
    clear_fd_extended_acl,
    create_private_bytes,
    create_private_json,
    fd_has_extended_acl,
    read_private_bytes,
    read_private_json,
    rename_noreplace,
    replace_private_json,
    require_private_file,
    resolve_state_root,
)

__all__ = (
    "DEFAULT_GIT_BIN",
    "GIT_EXECUTABLE_CANDIDATES",
    "MAX_GIT_OUTPUT_BYTES",
    "MAX_PRIVATE_JSON_BYTES",
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "GitContext",
    "ProjectBinding",
    "ProjectError",
    "ProjectTransaction",
    "account_home",
    "clear_fd_extended_acl",
    "create_private_bytes",
    "create_private_json",
    "current_project_transaction",
    "discover_git_context",
    "fd_has_extended_acl",
    "initialize_project",
    "project_transaction",
    "read_private_bytes",
    "read_private_json",
    "rename_noreplace",
    "replace_private_json",
    "require_private_file",
    "require_project_transaction",
    "resolve_project",
    "resolve_state_root",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROJECT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "cam-project-binding-1.schema.json"
PROJECT_LOCK_TIMEOUT_SECONDS = 5.0
PROJECT_LOCK_POLL_SECONDS = 0.025
INITIALIZATION_LOCK_NAME = "initialization.lock"

_LOCK_FLAGS = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
_LOCK_CREATE_FLAGS = _LOCK_FLAGS | os.O_CREAT | os.O_EXCL


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


def _validate_project_document(
    document: dict[str, Any], *, expected_format: str
) -> None:
    errors = list(_PROJECT_VALIDATOR.iter_errors(document))
    if errors or document.get("format") != expected_format:
        raise ProjectError(
            "project.document_invalid", "project state document failed validation"
        )


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
