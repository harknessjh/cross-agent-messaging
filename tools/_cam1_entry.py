# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Isolated one-shot dispatcher for the three public CAM command facades."""

from __future__ import annotations

import posix
import sys

_COMMANDS = {
    "cam1": "tools.cam1",
    "cam1_project": "tools.cam1_project",
    "cam1_transport": "tools.cam1_transport",
}
_LIVE_TRANSPORT_COMMANDS = frozenset(
    {
        "doctor",
        "product-discover",
        "product-approve",
        "product-status",
        "product-revoke",
        "claude-list",
        "claude-preflight",
        "claude-send",
        "codex-send",
        "codex-reply",
    }
)
_LIVE_PROJECT_COMMANDS = frozenset(
    {
        ("onboarding", "inspect-self"),
        ("onboarding", "prepare"),
        ("onboarding", "confirm"),
    }
)
_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "--claude-bin",
        "--codex-bin",
        "--project-root",
        "--state-root",
        "--git-bin",
        "--timeout-seconds",
        "--expected-validation-profile-sha256",
    }
)
_REGULAR_FILE_MASK = 0o170000
_REGULAR_FILE = 0o100000


def _emit_bootstrap_error(detail: str, *, code: str = "bootstrap.invalid") -> int:
    sys.stderr.write(
        f'{{"error":{{"code":"{code}","detail":"{detail}"}},"ok":false}}\n'
    )
    return 2


def _read_regular_source(path: str) -> bytes:
    no_follow = getattr(posix, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ImportError("no-follow file opens are unavailable")
    descriptor = posix.open(
        path,
        posix.O_RDONLY | no_follow | getattr(posix, "O_CLOEXEC", 0),
    )
    try:
        before = posix.fstat(descriptor)
        if (
            before.st_mode & _REGULAR_FILE_MASK != _REGULAR_FILE
            or before.st_size > 524_288
        ):
            raise ImportError("the tools package initializer is not a bounded file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = posix.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise ImportError("the tools package initializer changed while read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if posix.read(descriptor, 1):
            raise ImportError("the tools package initializer changed while read")
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
        raise ImportError("the tools package initializer changed while read")
    return b"".join(chunks)


def _load_tools_package(tools_dir: str) -> None:
    path = f"{tools_dir}/__init__.py"
    raw = _read_regular_source(path)
    package = type(sys)("tools")
    package.__file__ = path
    package.__package__ = "tools"
    package.__path__ = []
    sys.modules["tools"] = package
    try:
        exec(compile(raw, path, "exec", dont_inherit=True), package.__dict__)
    except BaseException:
        sys.modules.pop("tools", None)
        raise


def _transport_command(arguments: list[str]) -> str | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            return arguments[index] if index < len(arguments) else None
        if argument == "--allow-dirty-validator":
            index += 1
            continue
        option = argument.split("=", 1)[0]
        if option in _GLOBAL_VALUE_OPTIONS:
            index += 1 if "=" in argument else 2
            continue
        if argument.startswith("-"):
            return None
        return argument
    return None


def _project_command(arguments: list[str]) -> tuple[str | None, str | None]:
    """Return the project domain and command without importing project code."""

    positionals: list[str] = []
    index = 0
    while index < len(arguments) and len(positionals) < 2:
        argument = arguments[index]
        if argument == "--":
            positionals.extend(arguments[index + 1 : index + 3])
            break
        option = argument.split("=", 1)[0]
        if option in _GLOBAL_VALUE_OPTIONS:
            index += 1 if "=" in argument else 2
            continue
        if argument.startswith("-"):
            return None, None
        positionals.append(argument)
        index += 1
    domain = positionals[0] if positionals else None
    command = positionals[1] if len(positionals) > 1 else None
    return domain, command


def _requires_live_transport_sources(arguments: list[str]) -> bool:
    """Fail safe when malformed or abbreviated arguments still name live work."""

    return _transport_command(arguments) in _LIVE_TRANSPORT_COMMANDS or any(
        argument in _LIVE_TRANSPORT_COMMANDS for argument in arguments
    )


def _requires_live_project_sources(arguments: list[str]) -> bool:
    """Recognize live onboarding even when another argument is malformed."""

    if _project_command(arguments) in _LIVE_PROJECT_COMMANDS:
        return True
    return "onboarding" in arguments and any(
        command in arguments for _domain, command in _LIVE_PROJECT_COMMANDS
    )


def main() -> int:
    if not sys.flags.isolated:
        return _emit_bootstrap_error("isolated Python mode is required")
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        return _emit_bootstrap_error("the public CAM command was not recognized")
    tools_dir = __file__.rsplit("/", 1)[0]
    try:
        _load_tools_package(tools_dir)
        command = sys.argv[1]
        arguments = sys.argv[2:]
        if command == "cam1_transport" and _requires_live_transport_sources(arguments):
            sys.modules["tools._cam1_bootstrap"].require_live_import_sources()
        if command == "cam1_project" and _requires_live_project_sources(arguments):
            sys.modules["tools._cam1_bootstrap"].require_live_import_sources()
        module = __import__(_COMMANDS[command], fromlist=("main",))
        sys.argv = [f"{tools_dir}/{command}.py", *arguments]
        return int(module.main(arguments))
    except ModuleNotFoundError as error:
        if error.name and not error.name.startswith("tools"):
            return _emit_bootstrap_error(
                "a declared Python dependency is not installed",
                code="bootstrap.dependency_missing",
            )
        return _emit_bootstrap_error("the canonical CAM source graph could not load")
    except (ImportError, OSError, SyntaxError) as error:
        return _emit_bootstrap_error(
            getattr(error, "detail", "the canonical CAM source graph could not load"),
            code=getattr(error, "code", "bootstrap.invalid"),
        )


if __name__ == "__main__":
    raise SystemExit(main())
