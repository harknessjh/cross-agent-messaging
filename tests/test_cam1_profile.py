# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.cam1lib import profile

ROOT = Path(__file__).resolve().parents[1]


class ValidationProfileTests(unittest.TestCase):
    def assert_live_profile_error(
        self,
        snapshot: profile.ValidationProfile,
        expected_code: str,
        *,
        allow_dirty: bool,
    ) -> None:
        expected_sha256 = snapshot.validation_profile_sha256 if allow_dirty else None
        with (
            mock.patch.object(
                profile, "current_validation_profile", return_value=snapshot
            ),
            self.assertRaises(profile.ValidationProfileError) as context,
        ):
            profile.require_live_profile(
                allow_dirty=allow_dirty,
                expected_sha256=expected_sha256,
            )
        self.assertEqual(context.exception.code, expected_code)

    def copied_profile_root(self, destination: Path) -> Path:
        for relative in profile.REQUIRED_PROFILE_PATHS:
            source = ROOT / relative
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for pattern in profile.PROFILE_GLOBS:
            for source in ROOT.glob(pattern):
                if not profile._profile_relative_path(
                    source.relative_to(ROOT).as_posix()
                ):
                    continue
                target = destination / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        return destination

    def initialized_git_profile_root(self, destination: Path) -> tuple[Path, str]:
        git_bin = profile._git_executable()
        if git_bin is None:
            self.skipTest("no trusted Git executable is available")
        copied = self.copied_profile_root(destination)
        subprocess.run(
            [git_bin, "-C", str(copied), "init", "--quiet"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [git_bin, "-C", str(copied), "add", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                git_bin,
                "-C",
                str(copied),
                "-c",
                "user.name=CAM Tests",
                "-c",
                "user.email=cam-tests@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )
        return copied, git_bin

    def test_profile_digest_changes_with_validation_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = self.copied_profile_root(Path(directory))
            before = profile.build_validation_profile(copied)
            validation_path = copied / "tools" / "cam1lib" / "validation.py"
            validation_path.write_bytes(validation_path.read_bytes() + b"\n# changed\n")
            after = profile.build_validation_profile(copied)

        self.assertNotEqual(
            before.validation_profile_sha256,
            after.validation_profile_sha256,
        )
        self.assertEqual(before.source_control.kind, "not_git")
        self.assertEqual(after.source_control.kind, "not_git")

    def test_profile_includes_every_top_level_cam_tool(self) -> None:
        profiled = {
            path.relative_to(ROOT).as_posix() for path in profile._profile_paths(ROOT)
        }
        expected = {
            path.relative_to(ROOT).as_posix() for path in ROOT.glob("tools/cam1*.py")
        }

        self.assertTrue(expected)
        self.assertLessEqual(expected, profiled)

    def test_profile_includes_compatibility_kernel_inputs(self) -> None:
        profiled = {
            path.relative_to(ROOT).as_posix() for path in profile._profile_paths(ROOT)
        }
        self.assertIn("tools/cam1lib/compatibility.py", profiled)
        self.assertIn("schemas/cam-compatibility-event-1.schema.json", profiled)
        self.assertEqual(
            profile._cam1_bootstrap._SOURCE_PATHS["tools.cam1lib.compatibility"],
            "cam1lib/compatibility.py",
        )

    def test_profile_reports_runtime_outside_the_source_digest(self) -> None:
        report = profile.validation_profile_report()
        self.assertTrue(report["available"])
        self.assertRegex(report["validation_profile_sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertIn("python", report["runtime"])
        self.assertIn("python_implementation", report["runtime"])
        self.assertIn("jsonschema", report["runtime"])

    def test_current_profile_rejects_source_changed_after_bootstrap(self) -> None:
        profile.current_validation_profile.cache_clear()
        try:
            with (
                mock.patch.object(
                    profile._cam1_bootstrap,
                    "verify_captured_sources",
                    side_effect=profile._cam1_bootstrap.BootstrapError(
                        "synthetic source change"
                    ),
                ),
                self.assertRaises(profile.ValidationProfileError) as context,
            ):
                profile.current_validation_profile()
        finally:
            profile.current_validation_profile.cache_clear()
        self.assertEqual(context.exception.code, "profile.bootstrap_changed")

    def test_blob_identity_matches_git_hash_object(self) -> None:
        git_bin = profile._git_executable()
        if git_bin is None:
            self.skipTest("no trusted Git executable is available")
        for raw in (b"", b"CAM/1\n", b"embedded\x00byte\n"):
            with self.subTest(raw=raw):
                result = subprocess.run(
                    [git_bin, "hash-object", "--stdin"],
                    input=raw,
                    check=True,
                    capture_output=True,
                )
                expected = result.stdout.decode("ascii").strip()
                self.assertEqual(profile._git_blob_id(raw, expected), expected)

    def test_profile_digest_is_independent_of_installation_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = profile.build_validation_profile(
                self.copied_profile_root(base / "first")
            )
            second = profile.build_validation_profile(
                self.copied_profile_root(base / "different" / "second")
            )

        self.assertEqual(
            first.validation_profile_sha256,
            second.validation_profile_sha256,
        )

    def test_actual_git_checkout_reports_clean_tracked_and_untracked_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, _ = self.initialized_git_profile_root(Path(directory))
            clean = profile.build_validation_profile(copied)
            self.assertEqual(clean.source_control.kind, "git")
            self.assertFalse(clean.source_control.dirty)
            self.assertTrue(clean.source_control.profile_paths_match_head)
            self.assertTrue(clean.source_control.profile_bytes_match_head)
            self.assertTrue(clean.source_control.profile_index_flags_clean)
            with mock.patch.object(
                profile, "current_validation_profile", return_value=clean
            ):
                self.assertIs(profile.require_live_profile(allow_dirty=False), clean)

            validation_path = copied / "tools" / "cam1lib" / "validation.py"
            original = validation_path.read_bytes()
            validation_path.write_bytes(original + b"\n# tracked change\n")
            tracked = profile.build_validation_profile(copied)
            self.assertTrue(tracked.source_control.dirty)
            self.assertTrue(tracked.source_control.profile_paths_match_head)
            self.assertFalse(tracked.source_control.profile_bytes_match_head)
            self.assertTrue(tracked.source_control.profile_index_flags_clean)
            with mock.patch.object(
                profile, "current_validation_profile", return_value=tracked
            ):
                self.assertIs(
                    profile.require_live_profile(
                        allow_dirty=True,
                        expected_sha256=tracked.validation_profile_sha256,
                    ),
                    tracked,
                )

            validation_path.write_bytes(original)
            (copied / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            untracked = profile.build_validation_profile(copied)
            self.assertTrue(untracked.source_control.dirty)
            self.assertTrue(untracked.source_control.profile_paths_match_head)
            self.assertTrue(untracked.source_control.profile_bytes_match_head)
            self.assertTrue(untracked.source_control.profile_index_flags_clean)

    def test_missing_head_is_never_accepted_for_live_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = self.copied_profile_root(Path(directory))
            git_bin = profile._git_executable()
            if git_bin is None:
                self.skipTest("no trusted Git executable is available")
            subprocess.run(
                [git_bin, "-C", str(copied), "init", "--quiet"],
                check=True,
                capture_output=True,
            )
            (copied / ".git" / "info" / "exclude").write_text("*\n", encoding="utf-8")
            unborn = profile.build_validation_profile(copied)

        self.assertEqual(unborn.source_control.kind, "git")
        self.assertIsNone(unborn.source_control.git_head)
        self.assertIsNone(unborn.source_control.dirty)
        for allow_dirty in (False, True):
            with self.subTest(allow_dirty=allow_dirty):
                self.assert_live_profile_error(
                    unborn,
                    "profile.source_unversioned",
                    allow_dirty=allow_dirty,
                )

    def test_unrelated_head_with_ignored_profile_files_is_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = self.copied_profile_root(Path(directory))
            git_bin = profile._git_executable()
            if git_bin is None:
                self.skipTest("no trusted Git executable is available")
            unrelated = copied / "unrelated.txt"
            unrelated.write_text("unrelated\n", encoding="utf-8")
            subprocess.run(
                [git_bin, "-C", str(copied), "init", "--quiet"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [git_bin, "-C", str(copied), "add", "unrelated.txt"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    git_bin,
                    "-C",
                    str(copied),
                    "-c",
                    "user.name=CAM Tests",
                    "-c",
                    "user.email=cam-tests@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "unrelated fixture",
                ],
                check=True,
                capture_output=True,
            )
            (copied / ".git" / "info" / "exclude").write_text(
                "cam-1.schema.json\nrequirements.txt\nschemas/\ntools/\n",
                encoding="utf-8",
            )
            unrelated_head = profile.build_validation_profile(copied)

        self.assertTrue(unrelated_head.source_control.dirty)
        self.assertFalse(unrelated_head.source_control.profile_paths_match_head)
        self.assert_live_profile_error(
            unrelated_head,
            "profile.path_set_mismatch",
            allow_dirty=True,
        )

    def test_ignored_profile_addition_is_not_a_live_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, _ = self.initialized_git_profile_root(Path(directory))
            relative = Path("tools/cam1lib/ignored_extension.py")
            (copied / relative).write_text("IGNORED = True\n", encoding="utf-8")
            (copied / ".git" / "info" / "exclude").write_text(
                f"{relative.as_posix()}\n", encoding="utf-8"
            )
            hidden = profile.build_validation_profile(copied)

        self.assertTrue(hidden.source_control.dirty)
        self.assertFalse(hidden.source_control.profile_paths_match_head)
        self.assert_live_profile_error(
            hidden,
            "profile.path_set_mismatch",
            allow_dirty=True,
        )

    def test_ignored_python_shadow_module_is_not_a_live_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, _ = self.initialized_git_profile_root(Path(directory))
            relative = Path("tools/platform.py")
            (copied / relative).write_text(
                "def python_implementation():\n"
                "    return 'shadow'\n\n"
                "def python_version():\n"
                "    return '0'\n",
                encoding="utf-8",
            )
            (copied / ".git" / "info" / "exclude").write_text(
                f"{relative.as_posix()}\n", encoding="utf-8"
            )
            hidden = profile.build_validation_profile(copied)

        self.assertTrue(hidden.source_control.dirty)
        self.assertFalse(hidden.source_control.profile_paths_match_head)
        self.assert_live_profile_error(
            hidden,
            "profile.path_set_mismatch",
            allow_dirty=True,
        )

    def test_supported_entry_points_ignore_adjacent_profile_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, _ = self.initialized_git_profile_root(Path(directory))
            profile_path = copied / "tools" / "cam1lib" / "profile.py"
            marker = copied / "poison-executed"
            original = profile_path.read_bytes()
            metadata = profile_path.stat()
            payload = (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "raise RuntimeError('poisoned profile bytecode executed')\n"
            ).encode()
            self.assertLess(len(payload) + 2, len(original))
            payload += b"#" + b" " * (len(original) - len(payload) - 2) + b"\n"
            self.assertEqual(len(payload), len(original))

            cache_path = Path(importlib.util.cache_from_source(str(profile_path)))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_bytes(payload)
            os.utime(profile_path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            py_compile.compile(
                str(profile_path),
                cfile=str(cache_path),
                doraise=True,
            )
            profile_path.write_bytes(original)
            os.utime(profile_path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

            environment = os.environ.copy()
            environment.pop("PYTHONPYCACHEPREFIX", None)
            commands = (
                ("tools/cam1.py", "validation-profile"),
                ("tools/cam1_project.py", "--help"),
                ("tools/cam1_transport.py", "--help"),
            )
            for command in commands:
                with self.subTest(entry_point=command[0]):
                    result = subprocess.run(
                        [sys.executable, *command],
                        cwd=copied,
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(marker.exists())
            report = json.loads(
                subprocess.run(
                    [sys.executable, "tools/cam1.py", "validation-profile"],
                    cwd=copied,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertTrue(report["available"])

    def test_ignored_stdlib_shadow_cannot_run_before_live_profile_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, git_bin = self.initialized_git_profile_root(Path(directory))
            marker = copied / "shadow-executed"
            relative = Path("tools/hashlib.py")
            (copied / relative).write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "raise RuntimeError('ignored hashlib shadow executed')\n",
                encoding="utf-8",
            )
            (copied / ".git" / "info" / "exclude").write_text(
                f"{relative.as_posix()}\n", encoding="utf-8"
            )
            status = subprocess.run(
                [git_bin, "-C", str(copied), "status", "--porcelain"],
                check=True,
                capture_output=True,
            )
            self.assertEqual(status.stdout, b"")

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(copied / "tools")
            commands = (
                (
                    "doctor",
                    [
                        "--claude-bin",
                        "/not/a/claude/executable",
                        "--codex-bin",
                        "/not/a/codex/executable",
                        "doctor",
                    ],
                ),
                (
                    "claude-list",
                    [
                        "--claude-bin",
                        "/not/a/claude/executable",
                        "claude-list",
                    ],
                ),
                (
                    "claude-preflight",
                    [
                        "--claude-bin",
                        "/not/a/claude/executable",
                        "claude-preflight",
                        "--participant",
                        "example",
                    ],
                ),
                ("claude-send", ["claude-send"]),
                ("codex-send", ["codex-send"]),
                ("codex-reply", ["codex-reply"]),
            )
            for command, arguments in commands:
                with self.subTest(command=command):
                    completed = subprocess.run(
                        [sys.executable, "tools/cam1_transport.py", *arguments],
                        cwd=copied,
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    output = completed.stdout or completed.stderr
                    payload = json.loads(output)
                    code = payload.get("error", {}).get("code") or payload.get(
                        "checks", {}
                    ).get("validation_profile", {}).get("code")
                    self.assertEqual(code, "profile.path_set_mismatch")
                    self.assertFalse(marker.exists())

    def test_legacy_profile_bytecode_cannot_replace_hidden_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, git_bin = self.initialized_git_profile_root(Path(directory))
            marker = copied / "legacy-bytecode-executed"
            source = copied / "poison_profile.py"
            legacy = copied / "tools" / "cam1lib" / "profile.pyc"
            source.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "raise RuntimeError('legacy profile bytecode executed')\n",
                encoding="utf-8",
            )
            py_compile.compile(str(source), cfile=str(legacy), doraise=True)
            source.unlink()
            relative_source = "tools/cam1lib/profile.py"
            subprocess.run(
                [
                    git_bin,
                    "-C",
                    str(copied),
                    "update-index",
                    "--skip-worktree",
                    relative_source,
                ],
                check=True,
                capture_output=True,
            )
            (copied / relative_source).unlink()
            (copied / ".git" / "info" / "exclude").write_text(
                "tools/cam1lib/profile.pyc\n", encoding="utf-8"
            )
            status = subprocess.run(
                [
                    git_bin,
                    "-C",
                    str(copied),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(status.stdout, b"")

            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/cam1_transport.py",
                    "--claude-bin",
                    "/not/a/claude/executable",
                    "--codex-bin",
                    "/not/a/codex/executable",
                    "doctor",
                ],
                cwd=copied,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(
                json.loads(completed.stderr)["error"]["code"],
                "bootstrap.invalid",
            )
            self.assertFalse(marker.exists())

    def test_concealed_allowlisted_source_cannot_execute_before_git_check(
        self,
    ) -> None:
        cases = (
            (None, "profile.executable_source_dirty", False),
            ("--assume-unchanged", "profile.index_concealment", True),
        )
        for index_flag, expected_code, expect_clean_status in cases:
            with (
                self.subTest(index_flag=index_flag),
                tempfile.TemporaryDirectory() as directory,
            ):
                copied, git_bin = self.initialized_git_profile_root(Path(directory))
                marker = copied / "allowlisted-source-executed"
                relative = "tools/cam1lib/errors.py"
                target = copied / relative
                target.write_bytes(
                    target.read_bytes()
                    + (
                        "\nfrom pathlib import Path as _MarkerPath\n"
                        f"_MarkerPath({str(marker)!r}).write_text("
                        "'executed', encoding='utf-8')\n"
                    ).encode()
                )
                if index_flag is not None:
                    subprocess.run(
                        [
                            git_bin,
                            "-C",
                            str(copied),
                            "update-index",
                            index_flag,
                            relative,
                        ],
                        check=True,
                        capture_output=True,
                    )
                status = subprocess.run(
                    [git_bin, "-C", str(copied), "status", "--porcelain"],
                    check=True,
                    capture_output=True,
                )
                self.assertEqual(status.stdout == b"", expect_clean_status)

                completed = subprocess.run(
                    [
                        sys.executable,
                        "tools/cam1_transport.py",
                        "--claude-bin",
                        "/not/a/claude/executable",
                        "--codex-bin",
                        "/not/a/codex/executable",
                        "doctor",
                    ],
                    cwd=copied,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stderr)["error"]["code"], expected_code
                )
                self.assertFalse(marker.exists())

    def test_offline_non_git_facades_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = self.copied_profile_root(Path(directory))
            profile_result = subprocess.run(
                [sys.executable, "tools/cam1.py", "validation-profile"],
                cwd=copied,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            project_help = subprocess.run(
                [sys.executable, "tools/cam1_project.py", "--help"],
                cwd=copied,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )

        self.assertEqual(profile_result.returncode, 0, profile_result.stderr)
        report = json.loads(profile_result.stdout)
        self.assertTrue(report["available"])
        self.assertEqual(report["source_control"]["kind"], "not_git")
        self.assertEqual(project_help.returncode, 0, project_help.stderr)

    def test_profile_index_concealment_is_not_a_live_override(self) -> None:
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                copied, git_bin = self.initialized_git_profile_root(Path(directory))
                relative = Path("tools/cam1lib/profile.py")
                subprocess.run(
                    [git_bin, "-C", str(copied), "update-index", flag, str(relative)],
                    check=True,
                    capture_output=True,
                )
                target = copied / relative
                target.write_bytes(target.read_bytes() + b"\n# concealed\n")
                hidden = profile.build_validation_profile(copied)

                self.assertTrue(hidden.source_control.dirty)
                self.assertTrue(hidden.source_control.profile_paths_match_head)
                self.assertFalse(hidden.source_control.profile_bytes_match_head)
                self.assertFalse(hidden.source_control.profile_index_flags_clean)
                self.assert_live_profile_error(
                    hidden,
                    "profile.index_concealment",
                    allow_dirty=True,
                )

    def test_sparse_profile_omission_is_not_a_live_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, git_bin = self.initialized_git_profile_root(Path(directory))
            relative = Path("tools/cam1lib/errors.py")
            subprocess.run(
                [
                    git_bin,
                    "-C",
                    str(copied),
                    "update-index",
                    "--skip-worktree",
                    str(relative),
                ],
                check=True,
                capture_output=True,
            )
            (copied / relative).unlink()
            sparse = profile.build_validation_profile(copied)

        self.assertTrue(sparse.source_control.dirty)
        self.assertFalse(sparse.source_control.profile_paths_match_head)
        self.assertFalse(sparse.source_control.profile_index_flags_clean)
        self.assert_live_profile_error(
            sparse,
            "profile.path_set_mismatch",
            allow_dirty=True,
        )

    def test_missing_required_schema_and_unusable_git_metadata_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            missing = self.copied_profile_root(base / "missing")
            (missing / "schemas" / "cam-journal-record-1.schema.json").unlink()
            with self.assertRaises(profile.ValidationProfileError) as missing_error:
                profile.build_validation_profile(missing)
            self.assertEqual(missing_error.exception.code, "profile.component_missing")

            unusable = self.copied_profile_root(base / "unusable")
            (unusable / ".git").write_text("not valid git metadata\n", encoding="utf-8")
            unavailable = profile.build_validation_profile(unusable)
            self.assertEqual(unavailable.source_control.kind, "unavailable")
            with (
                mock.patch.object(
                    profile, "current_validation_profile", return_value=unavailable
                ),
                self.assertRaises(profile.ValidationProfileError) as blocked,
            ):
                profile.require_live_profile(allow_dirty=False)
            self.assertEqual(blocked.exception.code, "profile.source_unavailable")

    def test_dirty_source_requires_an_exact_explicit_override(self) -> None:
        dirty = profile.ValidationProfile(
            validation_profile_sha256="a" * 64,
            component_count=1,
            source_control=profile.SourceControlState(
                "git",
                "b" * 40,
                True,
                profile_paths_match_head=True,
                profile_bytes_match_head=False,
                profile_index_flags_clean=True,
            ),
            python_implementation="TestPython",
            python_version="3.test",
            jsonschema_version="test",
            referencing_version="test",
            rpds_py_version="test",
            rfc3339_validator_version="test",
        )
        with mock.patch.object(
            profile, "current_validation_profile", return_value=dirty
        ):
            with self.assertRaises(profile.ValidationProfileError) as blocked:
                profile.require_live_profile(allow_dirty=False)
            self.assertEqual(blocked.exception.code, "profile.dirty_source")

            with self.assertRaises(profile.ValidationProfileError) as unpinned:
                profile.require_live_profile(allow_dirty=True)
            self.assertEqual(unpinned.exception.code, "profile.override_unpinned")

            with self.assertRaises(profile.ValidationProfileError) as mismatch:
                profile.require_live_profile(
                    allow_dirty=True,
                    expected_sha256="c" * 64,
                )
            self.assertEqual(mismatch.exception.code, "profile.digest_mismatch")

            accepted = profile.require_live_profile(
                allow_dirty=True,
                expected_sha256="a" * 64,
            )
            self.assertIs(accepted, dirty)

    def test_unverifiable_and_non_git_sources_fail_live(self) -> None:
        unavailable = profile.ValidationProfile(
            validation_profile_sha256="a" * 64,
            component_count=1,
            source_control=profile.SourceControlState("unavailable", None, None),
            python_implementation="TestPython",
            python_version="3.test",
            jsonschema_version=None,
            referencing_version=None,
            rpds_py_version=None,
            rfc3339_validator_version=None,
        )
        non_git = profile.ValidationProfile(
            validation_profile_sha256="a" * 64,
            component_count=1,
            source_control=profile.SourceControlState("not_git", None, None),
            python_implementation="TestPython",
            python_version="3.test",
            jsonschema_version=None,
            referencing_version=None,
            rpds_py_version=None,
            rfc3339_validator_version=None,
        )
        with mock.patch.object(
            profile, "current_validation_profile", return_value=unavailable
        ):
            with self.assertRaises(profile.ValidationProfileError) as context:
                profile.require_live_profile(allow_dirty=False)
            self.assertEqual(context.exception.code, "profile.source_unavailable")
        with mock.patch.object(
            profile, "current_validation_profile", return_value=non_git
        ):
            with self.assertRaises(profile.ValidationProfileError) as context:
                profile.require_live_profile(allow_dirty=False)
            self.assertEqual(context.exception.code, "profile.source_unversioned")
            with self.assertRaises(profile.ValidationProfileError) as overridden:
                profile.require_live_profile(
                    allow_dirty=True,
                    expected_sha256="a" * 64,
                )
            self.assertEqual(
                overridden.exception.code,
                "profile.source_unversioned",
            )


if __name__ == "__main__":
    unittest.main()
