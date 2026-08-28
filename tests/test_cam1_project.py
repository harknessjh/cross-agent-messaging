# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import hashlib
import json
import multiprocessing as mp
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools import cam1_project
from tools.cam1lib import builders, journal, project, state
from tools.cam1lib.participants import RouteStatus

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


class ProjectJournalTests(unittest.TestCase):
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
            project,
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
                project,
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
            mock.patch.object(project, "_write_all", side_effect=partial_write),
            self.assertRaises(project.ProjectError) as partial_context,
        ):
            project.create_private_bytes(partial_target, b"exact-bytes")
        self.assertEqual(partial_context.exception.code, "state.write")
        self.assertFalse(partial_target.exists())
        self.assertEqual(list(parent.glob(".cam1-*.tmp")), [])

        file_sync_target = parent / "file-sync.bin"
        with (
            mock.patch.object(project.os, "fsync", side_effect=OSError("injected")),
            self.assertRaises(project.ProjectError) as file_sync_context,
        ):
            project.create_private_bytes(file_sync_target, b"exact-bytes")
        self.assertEqual(file_sync_context.exception.code, "state.write")
        self.assertFalse(file_sync_target.exists())
        self.assertEqual(list(parent.glob(".cam1-*.tmp")), [])

        directory_sync_target = parent / "directory-sync.bin"
        with (
            mock.patch.object(
                project.os,
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
                project,
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

    def test_journal_preserves_exact_bytes_and_builds_hash_chain(self) -> None:
        binding = self.initialize()
        first_bytes = b'\x00not utf-8: \xff\n{"body":"exact"}\n'
        first = journal.append_record(
            binding,
            event_type="transport.sent",
            exact_message=first_bytes,
            attributes={"route": "claude", "private_note": "do not print"},
            now=NOW,
        )
        second = journal.append_record(
            binding,
            event_type="application.received",
            exact_message=b"reply",
            attributes={"correlated": True},
            now=NOW + dt.timedelta(seconds=1),
        )

        verification = journal.verify_journal(binding)
        records = journal.replay_records(binding)
        self.assertEqual(verification.record_count, 2)
        self.assertEqual(verification.last_sequence, 2)
        self.assertEqual(verification.last_record_sha256, second["record_sha256"])
        self.assertEqual(first["previous_record_sha256"], None)
        self.assertEqual(second["previous_record_sha256"], first["record_sha256"])
        self.assertEqual(first["worktree_id"], binding.worktree_id)
        self.assertEqual(first["provenance"]["git_top_level"], str(self.repo))
        self.assertIsNone(first["provenance"]["head_sha"])
        self.assertIsNone(first["provenance"]["head_tree_sha"])
        self.assertIn(first["provenance"]["branch"], {"main", "master"})
        self.assertFalse(first["provenance"]["dirty"])
        self.assertEqual(journal.decode_exact_message(records[0]), first_bytes)
        self.assertEqual(journal.decode_exact_message(records[1]), b"reply")
        self.assertEqual(mode(binding.journal_path), 0o600)
        self.assertEqual(
            journal.replay_records(binding, event_types={"transport.sent"}),
            (records[0],),
        )

    def test_project_transaction_token_scopes_an_append(self) -> None:
        binding = self.initialize()
        with (
            project.project_transaction(binding) as transaction,
            project.project_transaction(binding) as nested,
        ):
            self.assertIs(nested, transaction)
            journal.append_record(
                binding,
                event_type="message.sent",
                now=NOW,
            )
        with self.assertRaises(project.ProjectError) as context:
            journal.append_record(
                binding,
                event_type="message.received",
                now=NOW,
                transaction=transaction,
            )
        self.assertEqual(context.exception.code, "transaction.inactive")

    def test_transaction_verifies_journal_once_and_advances_cached_chain(self) -> None:
        binding = self.initialize()
        original_verify = journal._verify_records

        with mock.patch.object(
            journal, "_verify_records", wraps=original_verify
        ) as verify_records:
            with project.project_transaction(binding) as transaction:
                self.assertEqual(journal.replay_records(binding), ())
                first = journal.append_record(
                    binding,
                    event_type="message.sent",
                    now=NOW,
                    transaction=transaction,
                )
                second = journal.append_record(
                    binding,
                    event_type="message.received",
                    now=NOW + dt.timedelta(seconds=1),
                    transaction=transaction,
                )
                self.assertEqual(
                    journal.verify_journal(binding).last_record_sha256,
                    second["record_sha256"],
                )
                self.assertEqual(
                    [record["record_id"] for record in journal.replay_records(binding)],
                    [first["record_id"], second["record_id"]],
                )
                first["event_type"] = "message.changed-by-caller"
                self.assertEqual(
                    journal.replay_records(binding)[0]["event_type"],
                    "message.sent",
                )
                self.assertEqual(verify_records.call_count, 1)

            with project.project_transaction(binding):
                journal.verify_journal(binding)
                journal.replay_records(binding)
                self.assertEqual(verify_records.call_count, 2)

    def test_append_detaches_nested_attributes_before_digest_and_cache(self) -> None:
        binding = self.initialize()
        attributes = {"nested": {"values": ["preserved"]}}
        original_digest = journal._record_digest

        def digest_then_mutate(record: dict[str, object]) -> str:
            digest = original_digest(record)
            attributes["nested"]["values"].append("caller mutation")
            return digest

        with (
            mock.patch.object(
                journal, "_record_digest", side_effect=digest_then_mutate
            ),
            project.project_transaction(binding) as transaction,
        ):
            appended = journal.append_record(
                binding,
                event_type="note.nested-attributes",
                attributes=attributes,
                now=NOW,
                transaction=transaction,
            )
            replayed = journal.replay_records(binding)[0]

        expected = {"nested": {"values": ["preserved"]}}
        self.assertEqual(appended["attributes"], expected)
        self.assertEqual(replayed["attributes"], expected)
        self.assertGreater(len(attributes["nested"]["values"]), 1)
        self.assertEqual(journal.verify_journal(binding).record_count, 1)

    def test_append_rejects_generated_record_with_invalid_self_digest(self) -> None:
        binding = self.initialize()
        original_digest = journal._record_digest
        calls = 0

        def wrong_then_actual(record: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "0" * 64
            return original_digest(record)

        with (
            mock.patch.object(journal, "_record_digest", side_effect=wrong_then_actual),
            self.assertRaises(journal.JournalError) as context,
        ):
            journal.append_record(binding, event_type="note.invalid-generated-digest")

        self.assertEqual(context.exception.code, "journal.record_digest")
        self.assertEqual(binding.journal_path.read_bytes(), b"")

    def test_full_replay_checks_each_record_self_digest_once(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.digest-count", now=NOW)
        original_digest = journal._record_digest

        with mock.patch.object(
            journal, "_record_digest", wraps=original_digest
        ) as record_digest:
            self.assertEqual(journal.verify_journal(binding).record_count, 1)

        self.assertEqual(record_digest.call_count, 1)

    def test_transaction_cache_rejects_journal_identity_substitution(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="message.sent", now=NOW)

        with project.project_transaction(binding):
            journal.replay_records(binding)
            original = binding.journal_path.read_bytes()
            binding.journal_path.rename(binding.project_dir / "journal-replaced.jsonl")
            project.create_private_bytes(binding.journal_path, original)

            with self.assertRaises(journal.JournalError) as context:
                journal.replay_records(binding)

        self.assertEqual(context.exception.code, "journal.changed")

    def test_project_transaction_lock_contention_is_bounded(self) -> None:
        binding = self.initialize()

        def contended_flock(_descriptor: int, operation: int) -> None:
            if operation & project.fcntl.LOCK_NB:
                raise BlockingIOError(project.errno.EAGAIN, "injected contention")

        with (
            mock.patch.object(project, "PROJECT_LOCK_TIMEOUT_SECONDS", 0.0),
            mock.patch.object(project.fcntl, "flock", side_effect=contended_flock),
            self.assertRaises(project.ProjectError) as context,
            project.project_transaction(binding),
        ):
            self.fail("contended transaction unexpectedly acquired the lock")

        self.assertEqual(context.exception.code, "transaction.busy")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_project_transaction_rejects_lock_path_substitution_after_acquire(
        self,
    ) -> None:
        binding = self.initialize()
        original_flock = project.fcntl.flock
        substituted = False

        def substitute_after_acquire(descriptor: int, operation: int) -> None:
            nonlocal substituted
            original_flock(descriptor, operation)
            if (
                not substituted
                and operation & project.fcntl.LOCK_EX
                and operation & project.fcntl.LOCK_NB
            ):
                substituted = True
                orphaned = binding.project_dir / "orphaned-transaction.lock"
                binding.transaction_lock_path.rename(orphaned)
                project.create_private_bytes(binding.transaction_lock_path, b"")

        with (
            mock.patch.object(
                project.fcntl,
                "flock",
                side_effect=substitute_after_acquire,
            ),
            self.assertRaises(project.ProjectError) as context,
            project.project_transaction(binding),
        ):
            self.fail("substituted transaction lock unexpectedly authorized mutation")

        self.assertEqual(context.exception.code, "transaction.identity")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_caller_constructed_transaction_token_is_rejected(self) -> None:
        binding = self.initialize()
        descriptor = os.open(binding.transaction_lock_path, os.O_RDWR)
        try:
            metadata = os.fstat(descriptor)
            forged = project.ProjectTransaction(
                project_id=binding.project_id,
                project_dir=binding.project_dir,
                lock_path=binding.transaction_lock_path,
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
            with self.assertRaises(project.ProjectError) as context:
                journal.append_record(
                    binding,
                    event_type="message.observed",
                    transaction=forged,
                )
        finally:
            os.close(descriptor)

        self.assertEqual(context.exception.code, "transaction.inactive")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_tail_is_bounded_and_redacts_message_and_attributes(self) -> None:
        binding = self.initialize()
        journal.append_record(
            binding,
            event_type="message.received",
            exact_message=b"MESSAGE-SECRET",
            attributes={"token": "ATTRIBUTE-SECRET"},
            now=NOW,
        )

        tail = journal.tail_records(binding, limit=1)
        rendered = json.dumps(tail)

        self.assertNotIn("MESSAGE-SECRET", rendered)
        self.assertNotIn("ATTRIBUTE-SECRET", rendered)
        self.assertEqual(tail[0]["message"]["content"], "<redacted>")
        self.assertEqual(tail[0]["attributes"]["redacted"], True)
        full_tail = journal.tail_records(binding, limit=1, redact=False)
        self.assertEqual(journal.decode_exact_message(full_tail[0]), b"MESSAGE-SECRET")
        self.assertEqual(full_tail[0]["attributes"]["token"], "ATTRIBUTE-SECRET")
        with self.assertRaises(journal.JournalError) as context:
            journal.tail_records(binding, limit=journal.MAX_TAIL_RECORDS + 1)
        self.assertEqual(context.exception.code, "journal.tail_limit")

    def test_tamper_and_partial_record_fail_closed_without_repair(self) -> None:
        binding = self.initialize()
        journal.append_record(
            binding,
            event_type="message.sent",
            attributes={"value": 1},
            now=NOW,
        )
        valid = binding.journal_path.read_bytes()
        record = json.loads(binding.journal_path.read_text(encoding="utf-8"))
        record["attributes"]["value"] = 2
        tampered = (
            json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        binding.journal_path.write_bytes(tampered)
        binding.journal_path.chmod(0o600)

        with self.assertRaises(journal.JournalError) as verify_context:
            journal.verify_journal(binding)
        self.assertEqual(verify_context.exception.code, "journal.record_digest")
        with self.assertRaises(journal.JournalError):
            journal.append_record(binding, event_type="message.received")
        self.assertEqual(binding.journal_path.read_bytes(), tampered)

        binding.journal_path.write_bytes(valid + b"{")
        binding.journal_path.chmod(0o600)
        partial = binding.journal_path.read_bytes()
        with self.assertRaises(journal.JournalError) as partial_context:
            journal.verify_journal(binding)
        self.assertEqual(partial_context.exception.code, "journal.partial_record")
        self.assertEqual(binding.journal_path.read_bytes(), partial)

    def test_partial_tail_recovery_archives_exact_bytes_and_preserves_prefix(
        self,
    ) -> None:
        binding = self.initialize()
        original_record = journal.append_record(
            binding,
            event_type="note.before-recovery",
            attributes={"value": 1},
            now=NOW,
        )
        verified_prefix = binding.journal_path.read_bytes()
        damaged = verified_prefix + b'{"incomplete":"record"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)
        expected_digest = hashlib.sha256(damaged).hexdigest()

        report = journal.inspect_partial_tail(binding)
        self.assertEqual(report.journal_sha256, expected_digest)
        self.assertEqual(report.verified_prefix_bytes, len(verified_prefix))
        self.assertEqual(report.partial_tail_bytes, len(damaged) - len(verified_prefix))
        self.assertEqual(report.prefix_verification.record_count, 1)

        recovery = journal.recover_partial_tail(
            binding,
            expected_journal_sha256=expected_digest,
            confirm_project_id=binding.project_id,
            reason="Injected incomplete write",
            operator_reference="Local operator approved this exact digest",
            now=NOW + dt.timedelta(seconds=1),
        )

        archive_path = Path(recovery.archive_path)
        self.assertEqual(archive_path.read_bytes(), damaged)
        self.assertEqual(mode(archive_path), 0o600)
        self.assertEqual(archive_path.parent.name, "recovery")
        self.assertEqual(mode(archive_path.parent), 0o700)
        recovered_bytes = binding.journal_path.read_bytes()
        self.assertTrue(recovered_bytes.startswith(verified_prefix))
        records = journal.replay_records(binding)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["record_sha256"], original_record["record_sha256"])
        self.assertEqual(records[1]["event_type"], "journal.recovered_partial_tail")
        self.assertEqual(
            records[1]["previous_record_sha256"], original_record["record_sha256"]
        )
        attributes = records[1]["attributes"]
        self.assertEqual(attributes["archive_sha256"], expected_digest)
        self.assertEqual(attributes["partial_tail_sha256"], report.partial_tail_sha256)
        self.assertEqual(journal.verify_journal(binding).record_count, 2)
        self.assertEqual(state.StateStore(binding).snapshot().journal_sequence, 2)
        with self.assertRaises(journal.JournalError) as repeated_context:
            journal.inspect_partial_tail(binding)
        self.assertEqual(repeated_context.exception.code, "journal.recovery_not_needed")

    def test_partial_tail_recovery_reseeds_active_transaction_caches(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.before-recovery", now=NOW)
        store = state.StateStore(binding)

        with project.project_transaction(binding) as transaction:
            self.assertEqual(journal.replay_records(binding)[0]["sequence"], 1)
            self.assertEqual(
                store.snapshot(transaction=transaction).journal_sequence,
                1,
            )
            damaged = binding.journal_path.read_bytes() + b'{"partial"'
            binding.journal_path.write_bytes(damaged)
            binding.journal_path.chmod(0o600)
            recovery = journal.recover_partial_tail(
                binding,
                expected_journal_sha256=hashlib.sha256(damaged).hexdigest(),
                confirm_project_id=binding.project_id,
                reason="Injected in-transaction incomplete write",
                operator_reference="Local operator approved this exact digest",
                now=NOW + dt.timedelta(seconds=1),
                transaction=transaction,
            )

            self.assertEqual(recovery.verification.record_count, 2)
            self.assertEqual(journal.verify_journal(binding).record_count, 2)
            self.assertEqual(
                store.snapshot(transaction=transaction).journal_sequence,
                2,
            )

    def test_partial_tail_recovery_refuses_wrong_digest_and_complete_corruption(
        self,
    ) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.valid", now=NOW)
        valid = binding.journal_path.read_bytes()

        with self.assertRaises(journal.JournalError) as clean_context:
            journal.inspect_partial_tail(binding)
        self.assertEqual(clean_context.exception.code, "journal.recovery_not_needed")

        partial = valid + b"{"
        binding.journal_path.write_bytes(partial)
        binding.journal_path.chmod(0o600)
        with self.assertRaises(journal.JournalError) as digest_context:
            journal.recover_partial_tail(
                binding,
                expected_journal_sha256="0" * 64,
                confirm_project_id=binding.project_id,
                reason="Wrong digest test",
                operator_reference="Test operator",
                now=NOW,
            )
        self.assertEqual(
            digest_context.exception.code, "journal.recovery_digest_mismatch"
        )
        self.assertEqual(binding.journal_path.read_bytes(), partial)
        self.assertFalse((binding.project_dir / "recovery").exists())

        record = json.loads(valid.decode("utf-8"))
        record["event_type"] = "note.tampered"
        complete_corruption = (
            json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        binding.journal_path.write_bytes(complete_corruption)
        binding.journal_path.chmod(0o600)
        with self.assertRaises(journal.JournalError) as complete_context:
            journal.inspect_partial_tail(binding)
        self.assertEqual(complete_context.exception.code, "journal.record_digest")

    def test_partial_tail_recovery_replace_failure_keeps_original_and_archive(
        self,
    ) -> None:
        binding = self.initialize()
        damaged = b'{"incomplete"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)
        digest = hashlib.sha256(damaged).hexdigest()

        with (
            mock.patch.object(journal.os, "replace", side_effect=OSError("injected")),
            self.assertRaises(journal.JournalError) as context,
        ):
            journal.recover_partial_tail(
                binding,
                expected_journal_sha256=digest,
                confirm_project_id=binding.project_id,
                reason="Replacement failure test",
                operator_reference="Test operator",
                now=NOW,
            )

        self.assertEqual(context.exception.code, "journal.recovery_replace")
        self.assertEqual(binding.journal_path.read_bytes(), damaged)
        recovery_files = list(
            (binding.project_dir / "recovery").glob("damaged-*.jsonl")
        )
        self.assertEqual(len(recovery_files), 1)
        self.assertEqual(recovery_files[0].read_bytes(), damaged)
        self.assertFalse(list(binding.project_dir.glob(".journal-recovery-*.tmp")))

    def test_recovery_cleanup_preserves_substituted_temporary_entries(self) -> None:
        binding = self.initialize()
        damaged = b'{"incomplete"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)
        report = journal.inspect_partial_tail(binding)
        source_descriptor = os.open(binding.journal_path, os.O_RDONLY)
        source_metadata = os.fstat(source_descriptor)
        token = uuid.UUID("00000000-0000-4000-8000-000000000901")
        foreign = b"foreign-substitution"
        try:
            recovery_directory = binding.project_dir / "recovery"
            pending = recovery_directory / f".pending-{token}.jsonl"
            moved_archive = recovery_directory / "owned-pending"

            def substitute_archive(_descriptor: int, *, label: str) -> None:
                self.assertEqual(label, "journal.recovery_archive")
                pending.rename(moved_archive)
                pending.write_bytes(foreign)
                pending.chmod(0o600)
                raise journal.JournalError("proof.failure", "forced failure")

            with (
                mock.patch.object(journal.uuid, "uuid4", return_value=token),
                mock.patch.object(
                    journal,
                    "_prepare_created_private_file",
                    side_effect=substitute_archive,
                ),
                self.assertRaises(journal.JournalError) as archive_context,
            ):
                journal._create_recovery_archive(
                    binding,
                    source_descriptor=source_descriptor,
                    report=report,
                )
            self.assertEqual(archive_context.exception.code, "proof.failure")
            self.assertEqual(pending.read_bytes(), foreign)
            self.assertTrue(moved_archive.exists())

            replacement = binding.project_dir / f".journal-recovery-{token}.tmp"
            moved_replacement = binding.project_dir / "owned-replacement"

            def substitute_replacement(_descriptor: int, *, label: str) -> None:
                self.assertEqual(label, "journal.recovery_replacement")
                replacement.rename(moved_replacement)
                replacement.write_bytes(foreign)
                replacement.chmod(0o600)
                raise journal.JournalError("proof.failure", "forced failure")

            with (
                mock.patch.object(journal.uuid, "uuid4", return_value=token),
                mock.patch.object(
                    journal,
                    "_prepare_created_private_file",
                    side_effect=substitute_replacement,
                ),
                self.assertRaises(journal.JournalError) as replacement_context,
            ):
                journal._replace_partial_journal(
                    binding,
                    source_descriptor=source_descriptor,
                    source_metadata=source_metadata,
                    report=report,
                    recovery_record_raw=b"{}\n",
                )
            self.assertEqual(replacement_context.exception.code, "proof.failure")
            self.assertEqual(replacement.read_bytes(), foreign)
            self.assertTrue(moved_replacement.exists())
        finally:
            os.close(source_descriptor)

    def test_partial_tail_recovery_refuses_a_full_verified_prefix(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.only-slot", now=NOW)
        damaged = binding.journal_path.read_bytes() + b"{"
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)

        with (
            mock.patch.object(journal, "MAX_JOURNAL_RECORDS", 1),
            self.assertRaises(journal.JournalError) as context,
        ):
            journal.recover_partial_tail(
                binding,
                expected_journal_sha256=hashlib.sha256(damaged).hexdigest(),
                confirm_project_id=binding.project_id,
                reason="Record limit regression",
                operator_reference="Test operator",
                now=NOW,
            )

        self.assertEqual(context.exception.code, "journal.record_limit")
        self.assertEqual(binding.journal_path.read_bytes(), damaged)
        self.assertFalse((binding.project_dir / "recovery").exists())

    def test_cli_partial_tail_recovery_round_trip(self) -> None:
        initialized = self.run_tool("project", "init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        binding = project.resolve_project(self.repo, state_root=self.state_root)
        journal.append_record(binding, event_type="note.before-cli-recovery", now=NOW)
        damaged = binding.journal_path.read_bytes() + b'{"partial"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)

        status = self.run_tool("journal", "recovery-status")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        expected_digest = hashlib.sha256(damaged).hexdigest()
        self.assertEqual(report["recovery"]["journal_sha256"], expected_digest)

        recovered = self.run_tool(
            "journal",
            "recover-partial-tail",
            "--expected-journal-sha256",
            expected_digest,
            "--confirm-project-id",
            binding.project_id,
            "--reason",
            "CLI recovery regression",
            "--operator-reference",
            "Local test operator confirmed the digest",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        payload = json.loads(recovered.stdout)
        self.assertEqual(payload["status"], "recovered_partial_tail")
        self.assertEqual(payload["recovery"]["original_sha256"], expected_digest)
        self.assertEqual(journal.verify_journal(binding).record_count, 2)

    def test_invalid_attributes_and_full_journal_are_rejected(self) -> None:
        binding = self.initialize()
        with self.assertRaises(journal.JournalError) as key_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={1: "coerced"},  # type: ignore[dict-item]
            )
        self.assertEqual(key_context.exception.code, "journal.json_invalid")
        with self.assertRaises(journal.JournalError) as value_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={"tuple": (1, 2)},  # type: ignore[dict-item]
            )
        self.assertEqual(value_context.exception.code, "journal.json_invalid")
        with self.assertRaises(journal.JournalError) as unicode_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={"surrogate": "\ud800"},
            )
        self.assertEqual(unicode_context.exception.code, "journal.json_invalid")
        nested: object = "leaf"
        for _ in range(journal.MAX_ATTRIBUTE_NESTING + 1):
            nested = [nested]
        with self.assertRaises(journal.JournalError) as depth_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={"nested": nested},  # type: ignore[dict-item]
            )
        self.assertEqual(depth_context.exception.code, "journal.attributes_nesting")

        journal.append_record(binding, event_type="message.sent", now=NOW)
        with (
            mock.patch.object(journal, "MAX_JOURNAL_RECORDS", 1),
            self.assertRaises(journal.JournalError) as full_context,
        ):
            journal.append_record(binding, event_type="message.sent", now=NOW)
        self.assertEqual(full_context.exception.code, "journal.record_limit")
        self.assertEqual(journal.verify_journal(binding).record_count, 1)

    def test_cli_round_trip_and_concurrent_appends(self) -> None:
        init = self.run_tool("project", "init")
        self.assertEqual(init.returncode, 0, init.stderr)
        initialized = json.loads(init.stdout)
        self.assertEqual(initialized["journal"]["record_count"], 0)
        exchange = self.base / "exchange"
        exchange.mkdir(mode=0o700)
        message_path = exchange / "message.bin"
        message_path.write_bytes(b"CLI-MESSAGE-SECRET")
        message_path.chmod(0o600)
        attributes_path = exchange / "attributes.json"
        attributes_path.write_text(
            '{"token":"CLI-ATTRIBUTE-SECRET"}\n', encoding="utf-8"
        )
        attributes_path.chmod(0o600)

        append = self.run_tool(
            "journal",
            "append",
            "--event-type",
            "note.sent",
            "--message",
            str(message_path),
            "--attributes-file",
            str(attributes_path),
        )
        self.assertEqual(append.returncode, 0, append.stderr)
        self.assertNotIn("CLI-MESSAGE-SECRET", append.stdout)
        self.assertNotIn("CLI-ATTRIBUTE-SECRET", append.stdout)

        reserved = self.run_tool(
            "journal",
            "append",
            "--event-type",
            "state.typo",
        )
        self.assertEqual(reserved.returncode, 2)
        self.assertEqual(
            json.loads(reserved.stderr)["error"]["code"],
            "journal.event_reserved",
        )
        for reserved_event in (
            "message.outbound.intent",
            "message.inbound.observed",
            "transport.not_accepted",
            "journal.recovered_partial_tail",
        ):
            reserved = self.run_tool(
                "journal",
                "append",
                "--event-type",
                reserved_event,
            )
            self.assertEqual(reserved.returncode, 2)
            self.assertEqual(
                json.loads(reserved.stderr)["error"]["code"],
                "journal.event_reserved",
            )

        command = self.tool_command("journal", "append", "--event-type", "worker.event")
        first = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, first_stderr or first_stdout)
        self.assertEqual(second.returncode, 0, second_stderr or second_stdout)

        verify = self.run_tool("journal", "verify")
        tail = self.run_tool("journal", "tail", "--limit", "3")
        self.assertEqual(json.loads(verify.stdout)["journal"]["record_count"], 3)
        self.assertEqual(len(json.loads(tail.stdout)["records"]), 3)
        self.assertNotIn("CLI-MESSAGE-SECRET", tail.stdout)
        self.assertNotIn("CLI-ATTRIBUTE-SECRET", tail.stdout)
        full_tail = self.run_tool("journal", "tail", "--limit", "3", "--show-content")
        self.assertIn("CLI-MESSAGE-SECRET", full_tail.stdout)
        self.assertIn("CLI-ATTRIBUTE-SECRET", full_tail.stdout)

    def test_participant_cli_codex_binding_is_ready_and_list_is_redacted(self) -> None:
        self.initialize()
        added = self.run_tool(
            "participant",
            "add",
            "--common-name",
            "bob-implementer",
            "--display-name",
            "BoB phase implementer",
            "--role",
            "implementation",
            "--vendor",
            "codex",
            "--participant-id",
            CODEX_PARTICIPANT,
        )
        self.assertEqual(added.returncode, 0, added.stderr)

        bound = self.run_tool(
            "participant",
            "bind",
            "--participant",
            "bob-implementer",
            "--session-id",
            CODEX_SESSION,
            "--session-label",
            "BoB phase-04 implementer",
            "--session-kind",
            "interactive",
            "--operator-reference",
            "operator inspected the active local session",
        )
        self.assertEqual(bound.returncode, 0, bound.stderr)
        bound_payload = json.loads(bound.stdout)
        self.assertEqual(
            bound_payload["participant"]["route"]["status"],
            "operator_correlated",
        )
        self.assertEqual(
            bound_payload["participant"]["binding"]["session_id"], "redacted"
        )

        redacted = self.run_tool("participant", "list")
        self.assertEqual(redacted.returncode, 0, redacted.stderr)
        self.assertNotIn(CODEX_SESSION, redacted.stdout)
        listed = json.loads(redacted.stdout)["roster"]["participants"][0]
        self.assertEqual(listed["binding"]["session_id"], "redacted")
        self.assertEqual(listed["route"]["address"], "redacted")

        revealed = self.run_tool("participant", "list", "--show-identifiers")
        self.assertEqual(revealed.returncode, 0, revealed.stderr)
        self.assertIn(CODEX_SESSION, revealed.stdout)
        full = json.loads(revealed.stdout)["roster"]["participants"][0]
        self.assertEqual(full["route"]["address"], CODEX_SESSION)

        binding = project.resolve_project(self.repo, state_root=self.state_root)
        event_types = [
            record["event_type"] for record in journal.replay_records(binding)
        ]
        self.assertEqual(
            event_types,
            [
                state.PARTICIPANT_ADDED,
                state.PARTICIPANT_BOUND,
                state.PARTICIPANT_ROUTE_OBSERVED,
                state.PARTICIPANT_ROUTE_CONFIRMED,
            ],
        )

        invalidated = self.run_tool(
            "participant",
            "invalidate",
            "--participant",
            "bob-implementer",
            "--reason",
            "session restarted",
        )
        retired = self.run_tool(
            "participant",
            "retire",
            "--participant",
            "bob-implementer",
            "--reason",
            "workstream ended",
        )
        self.assertEqual(invalidated.returncode, 0, invalidated.stderr)
        self.assertEqual(retired.returncode, 0, retired.stderr)
        self.assertEqual(json.loads(retired.stdout)["status"], "retired")

        status_result = self.run_tool("state", "status")
        rebuild_result = self.run_tool("state", "rebuild")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        self.assertEqual(rebuild_result.returncode, 0, rebuild_result.stderr)
        self.assertEqual(
            json.loads(status_result.stdout)["state"]["participant_count"], 1
        )
        self.assertEqual(json.loads(rebuild_result.stdout)["status"], "rebuilt")

    def test_participant_cli_confirms_an_observed_claude_route(self) -> None:
        binding = self.initialize()
        add = self.run_tool(
            "participant",
            "add",
            "--common-name",
            "bob-reviewer",
            "--display-name",
            "Example code review",
            "--role",
            "review",
            "--vendor",
            "claude-code",
            "--participant-id",
            CLAUDE_PARTICIPANT,
        )
        bind = self.run_tool(
            "participant",
            "bind",
            "--participant",
            "bob-reviewer",
            "--session-id",
            CLAUDE_SESSION,
            "--session-label",
            "Example code review",
            "--session-kind",
            "interactive",
            "--operator-reference",
            "operator matched Claude status output",
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertEqual(bind.returncode, 0, bind.stderr)

        store = state.StateStore(binding)
        observed = dt.datetime.now(dt.UTC)
        observed_text = observed.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        store.participant_observe_route(
            "bob-reviewer",
            transport="claude_send_message",
            address="example-code-review [abc123]",
            source="agent_view_and_list_agents",
            observed_at=observed_text,
            agent_view_id="00000000",
            list_agents_name="example-code-review",
            list_agents_ref="abc123",
            product_state="idle",
            now=observed,
        )

        confirmed = self.run_tool(
            "participant",
            "confirm-route",
            "--participant",
            "bob-reviewer",
            "--expected-address",
            "example-code-review [abc123]",
            "--operator-reference",
            "operator confirmed the discovered local route",
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        snapshot = store.snapshot()
        participant = snapshot.roster.participants[CLAUDE_PARTICIPANT]
        self.assertEqual(participant.route.status, RouteStatus.OPERATOR_CORRELATED)

    def test_message_ingest_retains_malformed_bytes_before_rejection(self) -> None:
        binding = self.initialize()
        raw = b'{"protocol":"CAM/1",not-valid-json\n\xff'
        message_path = self.private_message_file("malformed.cam1.json", raw)

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(message_path),
            "--as-participant",
            "local-receiver",
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "rejected")
        self.assertLessEqual(len(payload["error"]["problem_codes"]), 16)
        records = journal.replay_records(binding)
        self.assertEqual(
            [record["event_type"] for record in records],
            ["message.inbound.observed", "message.inbound.rejected"],
        )
        self.assertEqual(journal.decode_exact_message(records[0]), raw)
        self.assertIsNone(records[1]["message"])
        self.assertEqual(
            records[1]["attributes"]["observed_record_id"],
            records[0]["record_id"],
        )
        self.assertTrue(records[1]["attributes"]["validation_profile"]["available"])
        self.assertTrue(payload["validation_profile"]["available"])
        self.assertEqual(state.StateStore(binding).snapshot().lifecycle.entries, {})

    def test_message_ingest_commits_valid_root_and_reply_lifecycle(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        root_result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("request.cam1.json", root)),
            "--as-participant",
            "bob-reviewer",
        )
        self.assertEqual(root_result.returncode, 0, root_result.stderr)
        root_payload = json.loads(root_result.stdout)
        self.assertEqual(root_payload["status"], "validated")
        self.assertFalse(root_payload["authorization_evaluated"])
        self.assertFalse(root_payload["action_authorized"])

        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
            now=dt.datetime.now(dt.UTC),
        )
        reply_result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("ack.cam1.json", reply)),
            "--as-participant",
            "project-coordinator",
        )
        self.assertEqual(reply_result.returncode, 0, reply_result.stderr)
        self.assertEqual(
            json.loads(reply_result.stdout)["lifecycle"]["state"], "received"
        )

        records = tuple(
            record
            for record in journal.replay_records(binding)
            if record["event_type"].startswith("message.")
            or record["event_type"].startswith("state.lifecycle.")
        )
        self.assertEqual(
            [record["event_type"] for record in records],
            [
                "message.inbound.observed",
                state.LIFECYCLE_ROOT_REGISTERED,
                "message.inbound.validated",
                "message.inbound.observed",
                state.LIFECYCLE_REPLY_APPLIED,
                "message.inbound.validated",
            ],
        )
        self.assertEqual(journal.decode_exact_message(records[0]), root)
        self.assertEqual(journal.decode_exact_message(records[1]), root)
        self.assertEqual(journal.decode_exact_message(records[3]), reply)
        self.assertEqual(journal.decode_exact_message(records[4]), reply)
        for record in (records[2], records[5]):
            self.assertTrue(record["attributes"]["validation_profile"]["available"])
        self.assertTrue(root_payload["validation_profile"]["available"])

    def test_message_ingest_marks_exact_root_and_reply_retransmissions_duplicate(
        self,
    ) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        root_path = self.private_message_file("duplicate-request.cam1.json", root)
        state.StateStore(binding).lifecycle_root(root, now=dt.datetime.now(dt.UTC))
        first_root = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(root_path),
            "--as-participant",
            "bob-reviewer",
        )
        duplicate_root = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(root_path),
            "--as-participant",
            "bob-reviewer",
        )
        self.assertEqual(first_root.returncode, 0, first_root.stderr)
        self.assertEqual(duplicate_root.returncode, 0, duplicate_root.stderr)
        root_payload = json.loads(duplicate_root.stdout)
        self.assertEqual(root_payload["status"], "duplicate")
        self.assertTrue(root_payload["duplicate"])

        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
            now=dt.datetime.now(dt.UTC),
        )
        reply_path = self.private_message_file("duplicate-ack.cam1.json", reply)
        state.StateStore(binding).lifecycle_reply(reply, now=dt.datetime.now(dt.UTC))
        first_reply = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(reply_path),
            "--as-participant",
            "project-coordinator",
        )
        duplicate_reply = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(reply_path),
            "--as-participant",
            "project-coordinator",
        )
        self.assertEqual(first_reply.returncode, 0, first_reply.stderr)
        self.assertEqual(duplicate_reply.returncode, 0, duplicate_reply.stderr)
        reply_payload = json.loads(duplicate_reply.stdout)
        self.assertEqual(reply_payload["status"], "duplicate")
        self.assertTrue(reply_payload["duplicate"])

        records = journal.replay_records(binding)
        self.assertEqual(
            sum(
                record["event_type"] == state.LIFECYCLE_ROOT_REGISTERED
                for record in records
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["event_type"] == state.LIFECYCLE_REPLY_APPLIED
                for record in records
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["event_type"] == "message.inbound.duplicate"
                for record in records
            ),
            2,
        )
        for record in records:
            if record["event_type"] == "message.inbound.duplicate":
                self.assertTrue(record["attributes"]["validation_profile"]["available"])

    def test_message_ingest_survives_projection_refresh_failure(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        path = self.private_message_file("projection-failure.cam1.json", raw)

        with mock.patch.object(
            state,
            "replace_private_json",
            side_effect=project.ProjectError("state.replace", "injected failure"),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(path),
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        self.assertEqual(return_code, 0, payload)
        self.assertEqual(payload["status"], "validated")
        self.assertFalse(payload["state_projection"]["current"])
        self.assertTrue(payload["state_projection"]["rebuild_required"])
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(binding)[-3:]],
            [
                "message.inbound.observed",
                state.LIFECYCLE_ROOT_REGISTERED,
                "message.inbound.validated",
            ],
        )
        rebuilt = state.StateStore(binding).rebuild()
        self.assertEqual(len(rebuilt.lifecycle.entries), 1)

    def test_message_ingest_rejects_non_private_file_before_journaling(self) -> None:
        binding = self.initialize()
        message_path = self.base / "public-message.json"
        message_path.write_bytes(b"{}")
        message_path.chmod(0o644)

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(message_path),
            "--as-participant",
            "local-receiver",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "state.file.mode")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_message_ingest_rejects_wrong_local_recipient_after_retention(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session="00000000-0000-4000-8000-000000000999",
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("wrong-recipient.cam1.json", raw)),
            "--as-participant",
            "bob-reviewer",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["code"],
            "roster.recipient_mismatch",
        )
        records = journal.replay_records(binding)
        self.assertEqual(
            [record["event_type"] for record in records[-2:]],
            ["message.inbound.observed", "message.inbound.rejected"],
        )
        self.assertEqual(journal.decode_exact_message(records[-2]), raw)

    def test_message_ingest_reports_wrapper_validation_before_roster_mismatch(
        self,
    ) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        envelope = json.loads(
            builders.build_request(
                sender_vendor="codex",
                sender_name="project-coordinator",
                sender_session=CODEX_SESSION,
                recipient_vendor="claude-code",
                recipient_name="bob-reviewer",
                recipient_session=CLAUDE_SESSION,
                reply_transport="codex_queue",
                reply_address=CODEX_SESSION,
                risk_class="informational",
                operation="review_structure",
                intent="Request one local structure review",
                body="Review the project structure without making changes.",
                authorization_basis="none",
                now=dt.datetime.now(dt.UTC),
            )
        )
        del envelope["action"]["operation"]
        envelope["recipient"]["agent_name"] = "wrong-recipient"
        raw = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
        path = self.private_message_file("invalid-before-roster.cam1.json", raw)

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(path),
            "--as-participant",
            "bob-reviewer",
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["code"], "validation.failed")
        self.assertIn("schema.required", payload["error"]["problem_codes"])

    def test_message_ingest_records_expired_root_as_rejected_not_accepted(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
        )

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("expired.cam1.json", raw)),
            "--as-participant",
            "bob-reviewer",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["code"], "state.root_expired"
        )
        snapshot = state.StateStore(binding).snapshot()
        entry = next(iter(snapshot.lifecycle.entries.values()))
        self.assertEqual(entry.state.value, "expired_unconfirmed")
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(binding)[-3:]],
            [
                "message.inbound.observed",
                state.LIFECYCLE_ROOT_REGISTERED,
                "message.inbound.rejected",
            ],
        )

    def test_message_ingest_rejects_expired_duplicate_of_accepted_root(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        store = state.StateStore(binding)
        store.lifecycle_root(raw, now=NOW + dt.timedelta(seconds=1))
        accepted = builders.build_ack(
            raw,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=NOW + dt.timedelta(seconds=2),
        )
        store.lifecycle_reply(accepted, now=NOW + dt.timedelta(seconds=3))
        path = self.private_message_file("expired-duplicate.cam1.json", raw)
        after_expiry = NOW + dt.timedelta(minutes=11)

        with (
            mock.patch.object(
                cam1_project,
                "_utc_now",
                return_value=(
                    after_expiry,
                    after_expiry.isoformat().replace("+00:00", "Z"),
                ),
            ),
            mock.patch.object(
                state,
                "_current_utc_time",
                return_value=after_expiry,
            ),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(path),
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        self.assertEqual(return_code, 2)
        self.assertEqual(payload["error"]["code"], "lifecycle.duplicate_expired")
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(binding)[-2:]],
            ["message.inbound.observed", "message.inbound.rejected"],
        )
        root_id = json.loads(raw)["message_id"]
        self.assertEqual(
            store.snapshot().lifecycle.entries[root_id].state.value, "accepted"
        )

    def test_message_ingest_checks_expiry_after_journal_append(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        path = self.private_message_file("expires-during-journal.cam1.json", raw)
        before_expiry = NOW + dt.timedelta(minutes=9, seconds=59)
        after_expiry = NOW + dt.timedelta(minutes=10, seconds=1)

        def observed(value: dt.datetime) -> tuple[dt.datetime, str]:
            return value, value.isoformat().replace("+00:00", "Z")

        with mock.patch.object(
            cam1_project,
            "_utc_now",
            side_effect=[
                observed(before_expiry),
                observed(before_expiry),
                observed(after_expiry),
                observed(after_expiry),
            ],
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(path),
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        self.assertEqual(return_code, 2)
        self.assertEqual(payload["error"]["code"], "state.root_expired")

    def test_first_recipient_ingest_rechecks_precommitted_reply_expiry(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        store = state.StateStore(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        store.lifecycle_root(root, now=NOW)
        before_expiry = NOW + dt.timedelta(minutes=9, seconds=59)
        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=before_expiry,
        )
        store.lifecycle_reply(reply, now=before_expiry)
        reply_path = self.private_message_file("late-precommitted-ack.json", reply)
        after_expiry = NOW + dt.timedelta(minutes=10, seconds=1)

        with mock.patch.object(
            cam1_project,
            "_utc_now",
            return_value=(
                after_expiry,
                after_expiry.isoformat().replace("+00:00", "Z"),
            ),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(reply_path),
                as_participant="project-coordinator",
                renewal_of=None,
            )

        self.assertEqual(return_code, 2)
        self.assertEqual(
            payload["error"]["code"],
            "lifecycle.root_expired_before_reply",
        )
        self.assertNotIn(
            "message.inbound.validated",
            [record["event_type"] for record in journal.replay_records(binding)],
        )

    def test_late_callback_accepts_prior_locally_delivered_reply(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        store = state.StateStore(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        store.lifecycle_root(root, now=NOW)
        before_expiry = NOW + dt.timedelta(minutes=9, seconds=59)
        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=before_expiry,
        )
        intent = journal.append_record(
            binding,
            event_type="message.outbound.intent",
            exact_message=reply,
            attributes={"message_id": json.loads(reply)["message_id"]},
            now=before_expiry,
        )
        store.lifecycle_reply(reply, now=before_expiry)
        journal.append_record(
            binding,
            event_type="transport.accepted",
            attributes={
                "intent_record_id": intent["record_id"],
                "message_id": json.loads(reply)["message_id"],
                "lifecycle_state_committed": True,
            },
            now=before_expiry,
        )
        reply_path = self.private_message_file("delivered-late-ack.json", reply)
        after_expiry = NOW + dt.timedelta(minutes=10, seconds=1)

        with (
            mock.patch.object(
                cam1_project,
                "_utc_now",
                return_value=(
                    after_expiry,
                    after_expiry.isoformat().replace("+00:00", "Z"),
                ),
            ),
            mock.patch.object(
                state,
                "_current_utc_time",
                return_value=after_expiry,
            ),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(reply_path),
                as_participant="project-coordinator",
                renewal_of=None,
            )

        self.assertEqual(return_code, 0, payload)
        self.assertEqual(payload["status"], "validated")
        self.assertEqual(payload["lifecycle"]["state"], "accepted")

    def private_message_file(self, name: str, raw: bytes) -> Path:
        path = self.base / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def bind_ingest_participants(self, binding: project.ProjectBinding) -> None:
        store = state.StateStore(binding)
        observed = dt.datetime.now(dt.UTC)
        timestamp = observed.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for participant_id, common_name, display_name, vendor, session_id in (
            (
                CODEX_PARTICIPANT,
                "project-coordinator",
                "Project coordinator",
                "codex",
                CODEX_SESSION,
            ),
            (
                CLAUDE_PARTICIPANT,
                "bob-reviewer",
                "Bob reviewer",
                "claude-code",
                CLAUDE_SESSION,
            ),
        ):
            store.participant_add(
                participant_id=participant_id,
                common_name=common_name,
                display_name=display_name,
                role="CAM test participant",
                vendor=vendor,
                now=observed,
            )
            store.participant_bind(
                common_name,
                session_id=session_id,
                session_label=display_name,
                session_kind="interactive",
                operator_reference="test operator correlation",
                bound_at=timestamp,
                now=observed,
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


if __name__ == "__main__":
    unittest.main()
