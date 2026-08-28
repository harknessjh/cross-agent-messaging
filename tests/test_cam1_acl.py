# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.cam1lib import cli, darwin_acl, native_fs, project


class FakeAclLibrary:
    def __init__(
        self,
        *,
        get_result: int | None = None,
        get_errno: int = 0,
        init_result: int | None = 101,
        init_errno: int = 0,
        set_result: int = 0,
        set_errno: int = 0,
        free_result: int = 0,
        free_errno: int = 0,
    ) -> None:
        self.get_result = get_result
        self.get_errno = get_errno
        self.init_result = init_result
        self.init_errno = init_errno
        self.set_result = set_result
        self.set_errno = set_errno
        self.free_result = free_result
        self.free_errno = free_errno
        self.calls: list[tuple[object, ...]] = []

    def acl_get_fd_np(self, descriptor: int, acl_type: int) -> int | None:
        self.calls.append(("get", descriptor, acl_type))
        ctypes.set_errno(self.get_errno)
        return self.get_result

    def acl_init(self, count: int) -> int | None:
        self.calls.append(("init", count))
        ctypes.set_errno(self.init_errno)
        return self.init_result

    def acl_set_fd_np(self, descriptor: int, acl: int, acl_type: int) -> int:
        self.calls.append(("set", descriptor, acl, acl_type))
        ctypes.set_errno(self.set_errno)
        return self.set_result

    def acl_free(self, acl: int) -> int:
        self.calls.append(("free", acl))
        ctypes.set_errno(self.free_errno)
        return self.free_result


class FakeNativeFunction:
    def __init__(self, *, result: int = 0, error_number: int = 0) -> None:
        self.result = result
        self.error_number = error_number
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: tuple[object, ...] | None = None
        self.restype: object | None = None

    def __call__(self, *arguments: object) -> int:
        self.calls.append(arguments)
        ctypes.set_errno(self.error_number)
        return self.result


class DarwinAclPortableTests(unittest.TestCase):
    def tearDown(self) -> None:
        darwin_acl._libsystem.cache_clear()

    def test_non_darwin_operations_are_noops(self) -> None:
        with (
            mock.patch.object(darwin_acl, "_is_darwin", return_value=False),
            mock.patch.object(darwin_acl, "_libsystem") as loader,
        ):
            self.assertFalse(darwin_acl.fd_has_extended_acl(17))
            darwin_acl.clear_fd_extended_acl(17)
        loader.assert_not_called()

    def test_absent_acl_is_not_an_error(self) -> None:
        library = FakeAclLibrary(get_errno=errno.ENOENT)
        with (
            mock.patch.object(darwin_acl, "_is_darwin", return_value=True),
            mock.patch.object(darwin_acl, "_libsystem", return_value=library),
        ):
            self.assertFalse(darwin_acl.fd_has_extended_acl(18))
        self.assertEqual(
            library.calls,
            [("get", 18, darwin_acl.ACL_TYPE_EXTENDED)],
        )

    def test_present_acl_is_released_and_reported(self) -> None:
        library = FakeAclLibrary(get_result=101)
        with (
            mock.patch.object(darwin_acl, "_is_darwin", return_value=True),
            mock.patch.object(darwin_acl, "_libsystem", return_value=library),
        ):
            self.assertTrue(darwin_acl.fd_has_extended_acl(19))
        self.assertEqual(
            library.calls,
            [
                ("get", 19, darwin_acl.ACL_TYPE_EXTENDED),
                ("free", 101),
            ],
        )

    def test_acl_query_error_fails_closed(self) -> None:
        library = FakeAclLibrary(get_errno=errno.EIO)
        with (
            mock.patch.object(darwin_acl, "_is_darwin", return_value=True),
            mock.patch.object(darwin_acl, "_libsystem", return_value=library),
            self.assertRaises(OSError) as context,
        ):
            darwin_acl.fd_has_extended_acl(20)
        self.assertEqual(context.exception.errno, errno.EIO)

    def test_clear_installs_and_releases_empty_acl(self) -> None:
        library = FakeAclLibrary(init_result=202)
        with (
            mock.patch.object(darwin_acl, "_is_darwin", return_value=True),
            mock.patch.object(darwin_acl, "_libsystem", return_value=library),
        ):
            darwin_acl.clear_fd_extended_acl(21)
        self.assertEqual(
            library.calls,
            [
                ("init", 0),
                ("set", 21, 202, darwin_acl.ACL_TYPE_EXTENDED),
                ("free", 202),
            ],
        )

    def test_clear_error_still_releases_acl_and_fails_closed(self) -> None:
        library = FakeAclLibrary(
            init_result=303,
            set_result=-1,
            set_errno=errno.EACCES,
        )
        with (
            mock.patch.object(darwin_acl, "_is_darwin", return_value=True),
            mock.patch.object(darwin_acl, "_libsystem", return_value=library),
            self.assertRaises(OSError) as context,
        ):
            darwin_acl.clear_fd_extended_acl(22)
        self.assertEqual(context.exception.errno, errno.EACCES)
        self.assertEqual(library.calls[-1], ("free", 303))

    def test_missing_descriptor_acl_api_fails_closed(self) -> None:
        library = mock.Mock(spec=[])
        with (
            mock.patch.object(darwin_acl.ctypes, "CDLL", return_value=library),
            self.assertRaises(OSError) as context,
        ):
            darwin_acl._libsystem.cache_clear()
            darwin_acl._libsystem()
        self.assertEqual(context.exception.errno, errno.ENOTSUP)

    def test_project_validator_maps_acl_query_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            path.write_bytes(b"message")
            path.chmod(0o600)
            descriptor = os.open(path, os.O_RDONLY)
            try:
                with (
                    mock.patch.object(
                        project,
                        "fd_has_extended_acl",
                        side_effect=OSError(errno.EIO, "query failed"),
                    ),
                    self.assertRaises(project.ProjectError) as context,
                ):
                    project._validate_private_file(descriptor, label="test.file")
            finally:
                os.close(descriptor)
        self.assertEqual(context.exception.code, "test.file.acl_check")

    def test_failed_new_directory_acl_clear_removes_only_created_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            child = parent / "managed"
            with (
                mock.patch.object(
                    project,
                    "clear_fd_extended_acl",
                    side_effect=OSError(errno.EIO, "clear failed"),
                ),
                self.assertRaises(project.ProjectError) as context,
            ):
                project._ensure_private_child(
                    parent, child.name, label="test.managed_directory"
                )
            self.assertFalse(child.exists())
        self.assertEqual(context.exception.code, "test.managed_directory.acl_clear")

    def test_failed_new_initialization_lock_acl_clear_leaves_no_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            common_directory = Path(directory).resolve()
            common_admin = project._ensure_cam_admin_dir(common_directory)
            context_value = project.GitContext(
                top_level=common_directory,
                common_dir=common_directory,
                git_dir=common_directory,
                git_bin=project.DEFAULT_GIT_BIN,
            )
            with (
                mock.patch.object(
                    project,
                    "clear_fd_extended_acl",
                    side_effect=OSError(errno.EIO, "clear failed"),
                ),
                self.assertRaises(project.ProjectError) as context,
            ):
                with project._project_initialization_lock(context_value):
                    self.fail("initialization lock must not be yielded")
            self.assertFalse((common_admin / project.INITIALIZATION_LOCK_NAME).exists())
        self.assertEqual(
            context.exception.code, "project.initialization_lock.acl_clear"
        )

    def test_staged_directory_never_prepares_or_removes_unowned_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with (
                    mock.patch.object(
                        project,
                        "_require_owned",
                        side_effect=project.ProjectError(
                            "test.staged.owner", "foreign owner"
                        ),
                    ),
                    mock.patch.object(
                        project, "_prepare_created_private_directory"
                    ) as prepare,
                    mock.patch.object(project, "_remove_matching_directory") as remove,
                    self.assertRaises(project.ProjectError) as context,
                ):
                    project._create_staged_private_directory(
                        parent_descriptor, label="test.staged"
                    )
            finally:
                os.close(parent_descriptor)
            self.assertEqual(len(list(parent.glob(".cam1-directory-*.tmp"))), 1)
        self.assertEqual(context.exception.code, "test.staged.owner")
        prepare.assert_not_called()
        remove.assert_not_called()


class NativeRenamePortableTests(unittest.TestCase):
    def tearDown(self) -> None:
        native_fs._rename_api.cache_clear()

    def test_linux_api_uses_renameat2_noreplace(self) -> None:
        function = FakeNativeFunction()
        library = mock.Mock(renameat2=function)
        with (
            mock.patch.object(native_fs, "_platform_name", return_value="linux"),
            mock.patch.object(native_fs.ctypes, "CDLL", return_value=library) as loader,
        ):
            native_fs._rename_api.cache_clear()
            selected, flags = native_fs._rename_api()
        self.assertIs(selected, function)
        self.assertEqual(flags, native_fs._LINUX_RENAME_NOREPLACE)
        loader.assert_called_once_with(None, use_errno=True)

    def test_missing_platform_primitive_fails_closed(self) -> None:
        library = mock.Mock(spec=[])
        with (
            mock.patch.object(native_fs, "_platform_name", return_value="darwin"),
            mock.patch.object(native_fs.ctypes, "CDLL", return_value=library),
            self.assertRaises(OSError) as context,
        ):
            native_fs._rename_api.cache_clear()
            native_fs._rename_api()
        self.assertEqual(context.exception.errno, errno.ENOTSUP)

    def test_wrapper_passes_sibling_components_and_maps_collision(self) -> None:
        function = FakeNativeFunction(result=-1, error_number=errno.EEXIST)
        with (
            mock.patch.object(
                native_fs,
                "_rename_api",
                return_value=(function, native_fs._LINUX_RENAME_NOREPLACE),
            ),
            self.assertRaises(FileExistsError),
        ):
            native_fs.rename_noreplace(23, "staged", "published")
        self.assertEqual(
            function.calls,
            [
                (
                    23,
                    b"staged",
                    23,
                    b"published",
                    native_fs._LINUX_RENAME_NOREPLACE,
                )
            ],
        )

    def test_wrapper_rejects_non_component_names_before_native_call(self) -> None:
        with (
            mock.patch.object(native_fs, "_rename_api") as loader,
            self.assertRaises(OSError) as context,
        ):
            native_fs.rename_noreplace(24, "nested/source", "published")
        self.assertEqual(context.exception.errno, errno.EINVAL)
        loader.assert_not_called()


@unittest.skipUnless(sys.platform == "darwin", "requires Darwin extended ACLs")
class DarwinAclIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_acl(self, path: Path, rule: str) -> None:
        subprocess.run(
            ["/bin/chmod", "+a", rule, str(path)],
            check=True,
            capture_output=True,
        )

    def has_acl(self, path: Path) -> bool:
        flags = os.O_RDONLY
        if path.is_dir():
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            return darwin_acl.fd_has_extended_acl(descriptor)
        finally:
            os.close(descriptor)

    def test_existing_private_directory_and_file_acl_are_rejected_not_cleared(
        self,
    ) -> None:
        private_directory = self.base / "private"
        private_directory.mkdir(mode=0o700)
        private_file = private_directory / "message.cam1.json"
        private_file.write_bytes(b"{}\n")
        private_file.chmod(0o600)
        self.add_acl(private_directory, "everyone allow list")
        self.add_acl(private_file, "everyone allow read")

        with self.assertRaises(project.ProjectError) as directory_context:
            project._open_private_directory(private_directory, label="test.directory")
        self.assertEqual(directory_context.exception.code, "test.directory.acl")

        # Use a clean containing directory so the file's ACL is the rejection.
        clean_directory = self.base / "clean"
        clean_directory.mkdir(mode=0o700)
        clean_file = clean_directory / "message.cam1.json"
        private_file.replace(clean_file)
        with self.assertRaises(project.ProjectError) as file_context:
            project.read_private_bytes(clean_file, max_bytes=1024)
        self.assertEqual(file_context.exception.code, "state.file.acl")
        with self.assertRaises(cli.CliError) as live_context:
            cli.read_private_envelope_file(str(clean_file))
        self.assertEqual(live_context.exception.code, "input.private")

        self.assertTrue(self.has_acl(private_directory))
        self.assertTrue(self.has_acl(clean_file))

    def test_created_managed_inodes_drop_inherited_acl_before_validation(
        self,
    ) -> None:
        inheriting_parent = self.base / "inheriting"
        inheriting_parent.mkdir(mode=0o700)
        self.add_acl(
            inheriting_parent,
            "everyone allow list,search,readattr,readextattr,readsecurity,"
            "file_inherit,directory_inherit",
        )

        child = project._ensure_private_child(
            inheriting_parent, "managed", label="test.managed_directory"
        )
        self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o700)
        self.assertFalse(self.has_acl(child))

        inherited_file = inheriting_parent / "created-file"
        descriptor = os.open(
            inherited_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            self.assertTrue(darwin_acl.fd_has_extended_acl(descriptor))
            project._prepare_created_private_file(descriptor, label="test.created_file")
        finally:
            os.close(descriptor)
        self.assertEqual(stat.S_IMODE(inherited_file.stat().st_mode), 0o600)
        self.assertFalse(self.has_acl(inherited_file))

    def test_native_directory_publication_is_no_replace(self) -> None:
        source = self.base / "source"
        destination = self.base / "destination"
        source.mkdir(mode=0o700)
        parent_descriptor = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            native_fs.rename_noreplace(parent_descriptor, source.name, destination.name)
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_dir())

            second_source = self.base / "second-source"
            second_source.mkdir(mode=0o700)
            with self.assertRaises(FileExistsError):
                native_fs.rename_noreplace(
                    parent_descriptor, second_source.name, destination.name
                )
            self.assertTrue(second_source.is_dir())
            self.assertTrue(destination.is_dir())
        finally:
            os.close(parent_descriptor)


if __name__ == "__main__":
    unittest.main()
