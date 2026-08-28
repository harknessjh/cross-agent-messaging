# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import multiprocessing as mp
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.cam1lib import journal, project, secure_fs

ROOT = Path(__file__).resolve().parents[1]
PROJECT_TOOL = ROOT / "tools" / "cam1_project.py"
NOW = dt.datetime(2026, 8, 27, 17, 30, tzinfo=dt.UTC)
CODEX_PARTICIPANT = "00000000-0000-4000-8000-000000000101"
CLAUDE_PARTICIPANT = "00000000-0000-4000-8000-000000000102"
CODEX_SESSION = "00000000-0000-4000-8000-000000000201"
CLAUDE_SESSION = "00000000-0000-4000-8000-000000000202"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _initialize_project_process(
    project_root: str,
    state_root: str,
    hold_locked_initializer: bool,
    entered: object,
    release: object,
    results: object,
) -> None:
    """Initialize in a spawned process, optionally pausing inside the lock."""

    if hold_locked_initializer:
        original = project._initialize_project_locked

        def delayed_initializer(
            *args: object, **kwargs: object
        ) -> project.ProjectBinding:
            entered.set()  # type: ignore[attr-defined]
            if not release.wait(10):  # type: ignore[attr-defined]
                raise RuntimeError("timed out waiting to release initialization")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        project._initialize_project_locked = delayed_initializer  # type: ignore[assignment]
    try:
        binding = project.initialize_project(
            project_root,
            state_root=state_root,
            now=NOW,
        )
    except Exception as error:  # noqa: BLE001 - child reports failures to parent
        results.put(  # type: ignore[attr-defined]
            {
                "status": "error",
                "type": type(error).__name__,
                "code": getattr(error, "code", None),
                "detail": str(error),
            }
        )
        return
    results.put(  # type: ignore[attr-defined]
        {
            "status": "ok",
            "project_id": binding.project_id,
            "project_dir": str(binding.project_dir),
            "git_common_dir": str(binding.git_common_dir),
            "git_dir": str(binding.git_dir),
            "worktree_id": binding.worktree_id,
        }
    )


class ProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "example project"
        self.repo.mkdir(mode=0o700)
        self.git("init", "--quiet")
        self.state_root = self.base / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(
        self, *arguments: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(cwd or self.repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def initialize(self) -> project.ProjectBinding:
        return project.initialize_project(
            self.repo,
            state_root=self.state_root,
            now=NOW,
        )

    def tool_command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(PROJECT_TOOL),
            "--project-root",
            str(self.repo),
            "--state-root",
            str(self.state_root),
            *arguments,
        ]

    def run_tool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.tool_command(*arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )


class ProjectBindingTests(ProjectTestCase):
    def test_project_init_is_external_private_and_idempotent(self) -> None:
        binding = self.initialize()
        original = {
            path: path.read_bytes()
            for path in (
                binding.pointer_path,
                binding.worktree_id_path,
                binding.identity_path,
                binding.journal_path,
                binding.transaction_lock_path,
            )
        }

        repeated = project.initialize_project(
            self.repo,
            state_root=self.state_root,
            now=NOW + dt.timedelta(hours=1),
        )

        self.assertEqual(repeated.project_id, binding.project_id)
        self.assertEqual(repeated.worktree_id, binding.worktree_id)
        self.assertEqual(
            binding.project_dir,
            self.state_root.resolve() / f"example-project--{binding.project_id}",
        )
        self.assertEqual(binding.journal_path.read_bytes(), b"")
        self.assertFalse((self.repo / ".cam1").exists())
        for directory in (binding.state_root, binding.project_dir):
            self.assertEqual(mode(directory), 0o700)
        for path, contents in original.items():
            self.assertEqual(mode(path), 0o600)
            self.assertEqual(path.read_bytes(), contents)

    def test_git_status_snapshot_parses_identity_and_dirty_atomically(self) -> None:
        head = "a" * 40
        parsed = journal._parse_git_status_snapshot(
            (f"# branch.oid {head}\n# branch.head main\n? untracked-file\n").encode()
        )
        self.assertEqual(parsed, (head, "main", True))
        self.assertEqual(
            journal._parse_git_status_snapshot(
                b"# branch.oid (initial)\n# branch.head master\n"
            ),
            (None, "master", False),
        )
        self.assertEqual(
            journal._parse_git_status_snapshot(
                f"# branch.oid {head}\n# branch.head (detached)\n".encode()
            ),
            (head, None, False),
        )
        with self.assertRaises(journal.JournalError):
            journal._parse_git_status_snapshot(b"# branch.head main\n")

    def test_git_provenance_uses_one_status_snapshot_plus_immutable_tree(self) -> None:
        binding = self.initialize()
        tracked = self.repo / "tracked.txt"
        tracked.write_text("stable\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git(
            "-c",
            "user.name=CAM Test",
            "-c",
            "user.email=cam-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        )

        with mock.patch.object(
            journal,
            "_git_probe",
            wraps=journal._git_probe,
        ) as probe:
            record = journal.append_record(binding, event_type="note.provenance")

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args_list[0].args[1], "status")
        self.assertEqual(probe.call_args_list[1].args[1], "rev-parse")
        provenance = record["provenance"]
        self.assertEqual(
            provenance["head_sha"], self.git("rev-parse", "HEAD").stdout.strip()
        )
        self.assertFalse(provenance["dirty"])

    def test_resolve_from_nested_path_and_project_summary(self) -> None:
        binding = self.initialize()
        nested = self.repo / "nested" / "directory"
        nested.mkdir(parents=True)

        resolved = project.resolve_project(nested, state_root=self.state_root)

        self.assertEqual(resolved, binding)
        self.assertEqual(resolved.summary()["project_id"], binding.project_id)
        self.assertNotIn("identity_path", resolved.summary())

    def test_two_same_named_repositories_do_not_collide(self) -> None:
        first = self.initialize()
        second_repo = self.base / "other" / self.repo.name
        second_repo.mkdir(parents=True, mode=0o700)
        self.git("init", "--quiet", cwd=second_repo)

        second = project.initialize_project(
            second_repo,
            state_root=self.state_root,
            now=NOW,
        )

        self.assertNotEqual(first.project_id, second.project_id)
        self.assertNotEqual(first.project_dir, second.project_dir)
        self.assertTrue(first.project_dir.name.startswith("example-project--"))
        self.assertTrue(second.project_dir.name.startswith("example-project--"))

    def test_linked_worktrees_share_project_and_get_distinct_ids(self) -> None:
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "CAM test")
        self.git("commit", "--quiet", "--allow-empty", "-m", "initial")
        primary = self.initialize()
        linked_path = self.base / "linked"
        self.git("worktree", "add", "--quiet", "-b", "linked", str(linked_path))

        linked = project.initialize_project(
            linked_path,
            state_root=self.state_root,
            now=NOW,
        )

        self.assertEqual(linked.project_id, primary.project_id)
        self.assertEqual(linked.project_dir, primary.project_dir)
        self.assertEqual(linked.git_common_dir, primary.git_common_dir)
        self.assertNotEqual(linked.git_dir, primary.git_dir)
        self.assertNotEqual(linked.worktree_id, primary.worktree_id)
        self.assertNotEqual(linked.worktree_id_path, primary.worktree_id_path)

    def test_concurrent_linked_worktree_initialization_is_serialized(self) -> None:
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "CAM test")
        self.git("commit", "--quiet", "--allow-empty", "-m", "initial")
        linked_path = self.base / "linked-concurrent"
        self.git(
            "worktree",
            "add",
            "--quiet",
            "-b",
            "linked-concurrent",
            str(linked_path),
        )

        context = mp.get_context("spawn")
        entered = context.Event()
        release = context.Event()
        results = context.Queue()
        first = context.Process(
            target=_initialize_project_process,
            args=(
                str(self.repo),
                str(self.state_root),
                True,
                entered,
                release,
                results,
            ),
        )
        second = context.Process(
            target=_initialize_project_process,
            args=(
                str(linked_path),
                str(self.state_root),
                False,
                entered,
                release,
                results,
            ),
        )
        processes = (first, second)
        first.start()
        try:
            self.assertTrue(entered.wait(10), "first initializer never held the lock")
            second.start()
            second.join(timeout=0.3)
            self.assertTrue(
                second.is_alive(),
                "second initializer did not wait for the common-directory lock",
            )
        finally:
            release.set()
            for process_handle in processes:
                if process_handle.pid is not None:
                    process_handle.join(timeout=15)
                    if process_handle.is_alive():
                        process_handle.terminate()
                        process_handle.join(timeout=5)

        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        outcomes = [results.get(timeout=5), results.get(timeout=5)]
        self.assertEqual([item["status"] for item in outcomes], ["ok", "ok"])
        self.assertEqual(len({item["project_id"] for item in outcomes}), 1)
        self.assertEqual(len({item["project_dir"] for item in outcomes}), 1)
        self.assertEqual(len({item["git_common_dir"] for item in outcomes}), 1)
        self.assertEqual(len({item["git_dir"] for item in outcomes}), 2)
        self.assertEqual(len({item["worktree_id"] for item in outcomes}), 2)

        primary = project.resolve_project(self.repo, state_root=self.state_root)
        linked = project.resolve_project(linked_path, state_root=self.state_root)
        self.assertEqual(primary.project_id, linked.project_id)
        self.assertNotEqual(primary.worktree_id, linked.worktree_id)
        initialization_lock = (
            primary.git_common_dir / "cam1" / project.INITIALIZATION_LOCK_NAME
        )
        self.assertEqual(mode(initialization_lock), 0o600)
        self.assertEqual(len(tuple(self.state_root.iterdir())), 1)

    def test_private_child_tolerates_a_concurrent_directory_creator(self) -> None:
        parent = self.base / "managed-parent"
        parent.mkdir(mode=0o700)

        def publish_competitor_then_report_collision(
            parent_descriptor: int,
            _source_name: str,
            destination_name: str,
        ) -> None:
            os.mkdir(destination_name, 0o700, dir_fd=parent_descriptor)
            raise FileExistsError(destination_name)

        with mock.patch.object(
            secure_fs,
            "rename_noreplace",
            side_effect=publish_competitor_then_report_collision,
        ):
            child = project._ensure_private_child(
                parent,
                "managed-child",
                label="test.concurrent_child",
            )

        self.assertEqual(child, parent / "managed-child")
        self.assertEqual(mode(child), 0o700)

    def test_private_child_never_prepares_publish_collision(self) -> None:
        parent = self.base / "managed-parent"
        parent.mkdir(mode=0o700)
        child = parent / "managed-child"

        def publish_substitute_then_report_collision(
            _parent_descriptor: int,
            _source_name: str,
            _destination_name: str,
        ) -> None:
            child.mkdir(mode=0o700)
            child.chmod(0o777)
            raise FileExistsError(child.name)

        with (
            mock.patch.object(
                secure_fs,
                "rename_noreplace",
                side_effect=publish_substitute_then_report_collision,
            ),
            self.assertRaises(project.ProjectError) as context,
        ):
            project._ensure_private_child(
                parent,
                child.name,
                label="test.substituted_child",
            )

        self.assertEqual(context.exception.code, "test.substituted_child.mode")
        self.assertEqual(mode(child), 0o777)
        self.assertEqual(list(parent.glob(".cam1-directory-*.tmp")), [])

    def test_private_file_operations_reject_symlinked_ancestor_directory(self) -> None:
        real_parent = self.base / "real-private"
        nested = real_parent / "nested"
        nested.mkdir(parents=True, mode=0o700)
        existing = nested / "existing.json"
        project.create_private_json(existing, {"generation": 1})
        alias = self.base / "private-alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        redirected_existing = alias / "nested" / existing.name
        redirected_new = alias / "nested" / "new.json"

        operations = (
            ("create", lambda: project.create_private_bytes(redirected_new, b"new")),
            (
                "read",
                lambda: project.read_private_bytes(
                    redirected_existing, max_bytes=project.MAX_PRIVATE_JSON_BYTES
                ),
            ),
            ("require", lambda: project.require_private_file(redirected_existing)),
            (
                "replace",
                lambda: project.replace_private_json(
                    redirected_existing, {"generation": 2}
                ),
            ),
        )
        for label, operation in operations:
            with self.subTest(operation=label):
                with self.assertRaises(project.ProjectError) as context:
                    operation()
                self.assertEqual(context.exception.code, "path.symlink")

        self.assertFalse(nested.joinpath("new.json").exists())
        self.assertEqual(project.read_private_json(existing), {"generation": 1})

        safe_parent = self.base / "safe-private"
        safe_parent.mkdir(mode=0o700)
        erased_symlink = alias / ".." / safe_parent.name / "erased-link.json"
        with self.assertRaises(project.ProjectError) as parent_context:
            project.create_private_bytes(erased_symlink, b"new")
        self.assertEqual(parent_context.exception.code, "path.component")
        self.assertFalse(safe_parent.joinpath("erased-link.json").exists())

    def test_create_private_bytes_never_publishes_interrupted_output(self) -> None:
        parent = self.base / "atomic-create"
        parent.mkdir(mode=0o700)

        partial_target = parent / "partial.bin"

        def partial_write(descriptor: int, raw: bytes) -> None:
            os.write(descriptor, raw[:4])
            raise OSError("injected interrupted write")

        with (
            mock.patch.object(secure_fs, "_write_all", side_effect=partial_write),
            self.assertRaises(project.ProjectError) as partial_context,
        ):
            project.create_private_bytes(partial_target, b"exact-bytes")
        self.assertEqual(partial_context.exception.code, "state.write")
        self.assertFalse(partial_target.exists())
        self.assertEqual(list(parent.glob(".cam1-*.tmp")), [])

        file_sync_target = parent / "file-sync.bin"
        with (
            mock.patch.object(secure_fs.os, "fsync", side_effect=OSError("injected")),
            self.assertRaises(project.ProjectError) as file_sync_context,
        ):
            project.create_private_bytes(file_sync_target, b"exact-bytes")
        self.assertEqual(file_sync_context.exception.code, "state.write")
        self.assertFalse(file_sync_target.exists())
        self.assertEqual(list(parent.glob(".cam1-*.tmp")), [])

        directory_sync_target = parent / "directory-sync.bin"
        with (
            mock.patch.object(
                secure_fs.os,
                "fsync",
                side_effect=(None, OSError("injected"), None),
            ),
            self.assertRaises(project.ProjectError) as directory_sync_context,
        ):
            project.create_private_bytes(directory_sync_target, b"exact-bytes")
        self.assertEqual(directory_sync_context.exception.code, "state.create")
        self.assertFalse(directory_sync_target.exists())
        self.assertEqual(list(parent.glob(".cam1-*.tmp")), [])

    def test_local_path_normalization_only_maps_macos_compatibility_aliases(
        self,
    ) -> None:
        expected_tmp = (
            Path("/private/tmp/cam1-path")
            if sys.platform == "darwin"
            else Path("/tmp/cam1-path")
        )
        expected_var = (
            Path("/private/var/cam1-path")
            if sys.platform == "darwin"
            else Path("/var/cam1-path")
        )
        self.assertEqual(project._normalize_local_path("/tmp/cam1-path"), expected_tmp)
        self.assertEqual(project._normalize_local_path("/var/cam1-path"), expected_var)
        self.assertEqual(
            project._normalize_local_path("/etc/cam1-path"),
            Path("/etc/cam1-path"),
        )

    def test_initialization_lock_rejects_substitution_and_hard_links(self) -> None:
        context = project.discover_git_context(self.repo)
        admin = project._ensure_cam_admin_dir(context.common_dir)
        lock_path = admin / project.INITIALIZATION_LOCK_NAME
        target = self.base / "substitution-target"
        project.create_private_bytes(target, b"")
        lock_path.symlink_to(target)

        with self.assertRaises(project.ProjectError) as symlink_context:
            self.initialize()
        self.assertEqual(
            symlink_context.exception.code,
            "project.initialization_open",
        )
        self.assertFalse((admin / "project.json").exists())

        lock_path.unlink()
        project.create_private_bytes(lock_path, b"")
        alias = self.base / "initialization-lock-alias"
        os.link(lock_path, alias)
        with self.assertRaises(project.ProjectError) as link_context:
            self.initialize()
        self.assertEqual(
            link_context.exception.code,
            "project.initialization_lock.links",
        )
        self.assertFalse((admin / "project.json").exists())

        alias.unlink()
        binding = self.initialize()
        self.assertTrue(binding.pointer_path.is_file())
        self.assertEqual(mode(lock_path), 0o600)

    def test_non_git_directory_is_rejected_without_state_creation(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()

        with self.assertRaises(project.ProjectError) as context:
            project.initialize_project(plain, state_root=self.state_root)

        self.assertEqual(context.exception.code, "git.not_worktree")
        self.assertFalse(self.state_root.exists())

    def test_state_root_inside_any_git_worktree_is_rejected_before_creation(
        self,
    ) -> None:
        inside_target = self.repo / "private-cam-state"
        with self.assertRaises(project.ProjectError) as target_context:
            project.initialize_project(self.repo, state_root=inside_target)
        self.assertEqual(target_context.exception.code, "state.root_git_overlap")
        self.assertFalse(inside_target.exists())
        self.assertFalse((self.repo / ".git" / "cam1").exists())

        inside_admin = self.repo / ".git" / "private-cam-state"
        with self.assertRaises(project.ProjectError) as admin_context:
            project.initialize_project(self.repo, state_root=inside_admin)
        self.assertEqual(admin_context.exception.code, "state.root_git_overlap")
        self.assertFalse(inside_admin.exists())
        self.assertFalse((self.repo / ".git" / "cam1").exists())

        other_repo = self.base / "other-repository"
        other_repo.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(other_repo)],
            check=True,
            capture_output=True,
        )
        inside_other = other_repo / "private-cam-state"
        with self.assertRaises(project.ProjectError) as other_context:
            project.initialize_project(self.repo, state_root=inside_other)
        self.assertEqual(other_context.exception.code, "state.root_git_overlap")
        self.assertFalse(inside_other.exists())
        self.assertFalse((self.repo / ".git" / "cam1").exists())

    def test_project_symlink_and_permission_substitution_are_rejected(self) -> None:
        binding = self.initialize()
        moved = binding.project_dir.with_name(f"{binding.project_dir.name}-moved")
        binding.project_dir.rename(moved)
        binding.project_dir.symlink_to(moved, target_is_directory=True)

        with self.assertRaises(project.ProjectError) as symlink_context:
            project.resolve_project(self.repo, state_root=self.state_root)
        self.assertEqual(symlink_context.exception.code, "path.symlink")

        binding.project_dir.unlink()
        moved.rename(binding.project_dir)
        binding.journal_path.chmod(0o644)
        with self.assertRaises(project.ProjectError) as mode_context:
            project.resolve_project(self.repo, state_root=self.state_root)
        self.assertEqual(mode_context.exception.code, "state.file.mode")

    def test_project_pointer_rejects_a_copied_alternate_journal_root(self) -> None:
        binding = self.initialize()
        alternate_root = self.base / "alternate-state"
        alternate_root.mkdir(mode=0o700)
        shutil.copytree(
            binding.project_dir,
            alternate_root / binding.project_dir.name,
        )

        with self.assertRaises(project.ProjectError) as context:
            project.resolve_project(self.repo, state_root=alternate_root)

        self.assertEqual(context.exception.code, "project.state_root_mismatch")

    def test_journal_fifo_is_rejected_without_waiting_for_a_writer(self) -> None:
        binding = self.initialize()
        binding.journal_path.unlink()
        os.mkfifo(binding.journal_path, mode=0o600)

        with self.assertRaises(project.ProjectError) as context:
            journal.verify_journal(binding)

        self.assertEqual(context.exception.code, "journal.file.type")

    def test_journal_hard_link_is_rejected(self) -> None:
        binding = self.initialize()
        alias = self.base / "journal-alias"
        os.link(binding.journal_path, alias)

        with self.assertRaises(project.ProjectError) as context:
            journal.verify_journal(binding)

        self.assertEqual(context.exception.code, "journal.file.links")

    def test_malformed_pointer_is_not_overwritten_or_repaired(self) -> None:
        binding = self.initialize()
        malformed = b'{"format":"not-a-binding"}\n'
        binding.pointer_path.write_bytes(malformed)
        binding.pointer_path.chmod(0o600)

        with self.assertRaises(project.ProjectError):
            self.initialize()

        self.assertEqual(binding.pointer_path.read_bytes(), malformed)

    def test_private_projection_replace_is_atomic_and_rejects_symlinks(self) -> None:
        binding = self.initialize()
        projection_path = binding.project_dir / "projection.json"
        project.create_private_json(projection_path, {"generation": 1})
        project.replace_private_json(projection_path, {"generation": 2})

        self.assertEqual(project.read_private_json(projection_path), {"generation": 2})
        self.assertEqual(mode(projection_path), 0o600)
        target = binding.project_dir / "target.json"
        project.create_private_json(target, {"untouched": True})
        projection_path.unlink()
        projection_path.symlink_to(target)
        with self.assertRaises(project.ProjectError):
            project.replace_private_json(projection_path, {"generation": 3})
        self.assertEqual(project.read_private_json(target), {"untouched": True})

    def test_private_projection_cleanup_preserves_substituted_temp(self) -> None:
        binding = self.initialize()
        projection_path = binding.project_dir / "projection.json"
        moved = binding.project_dir / "owned-projection-temp"
        foreign = b"foreign-substitution"

        def substitute_then_fail(_descriptor: int, *, label: str) -> None:
            self.assertEqual(label, "state.temporary")
            temporary = next(binding.project_dir.glob(".projection.json.*.tmp"))
            temporary.rename(moved)
            temporary.write_bytes(foreign)
            temporary.chmod(0o600)
            raise project.ProjectError("proof.failure", "forced failure")

        with (
            mock.patch.object(
                secure_fs,
                "_prepare_created_private_file",
                side_effect=substitute_then_fail,
            ),
            self.assertRaises(project.ProjectError) as context,
        ):
            project.replace_private_json(projection_path, {"generation": 1})

        self.assertEqual(context.exception.code, "proof.failure")
        temporary = next(binding.project_dir.glob(".projection.json.*.tmp"))
        self.assertEqual(temporary.read_bytes(), foreign)
        self.assertTrue(moved.exists())

    def test_account_home_does_not_trust_home_environment_variable(self) -> None:
        fake_home = self.base / "untrusted-home"
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            resolved = project.account_home()
        self.assertNotEqual(resolved, fake_home)
        self.assertTrue(resolved.is_absolute())


if __name__ == "__main__":
    unittest.main()
