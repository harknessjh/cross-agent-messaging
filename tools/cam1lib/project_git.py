# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Read-only, absolute-path Git discovery for CAM project bindings."""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import ProjectError
from .secure_fs import (
    _canonical_existing_directory,
    _open_directory_fd,
    _require_owned,
)

GIT_EXECUTABLE_CANDIDATES = (
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/usr/bin/git",
)
MAX_GIT_OUTPUT_BYTES = 16_384


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


@dataclass(frozen=True, slots=True)
class GitContext:
    top_level: Path
    common_dir: Path
    git_dir: Path
    git_bin: str


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
