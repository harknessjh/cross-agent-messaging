# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import ctypes
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._native_executable_fixture import write_native
from tools import _cam1_executable as policy
from tools import _cam1_executable_format as formats
from tools import _cam1_executable_platform as platform_policy


def macho_bytes(*, file_type: int = 2) -> bytes:
    return (
        struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, file_type, 1, 8, 0, 0)
        + b"\0" * 8
    )


def elf_bytes(*, file_type: int = 3, wide: bool = True, endian: str = "<") -> bytes:
    size, ph_size = (64, 56) if wide else (52, 32)
    data = bytearray(size + ph_size)
    data[:7] = b"\x7fELF" + bytes((2 if wide else 1, 1 if endian == "<" else 2, 1))
    struct.pack_into(endian + "HHI", data, 16, file_type, 62, 1)
    struct.pack_into(endian + ("Q" if wide else "I"), data, 32 if wide else 28, size)
    struct.pack_into(endian + "HHH", data, 52 if wide else 40, size, ph_size, 1)
    struct.pack_into(endian + "I", data, size, 1)  # PT_LOAD
    return bytes(data)


class ExecutablePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.base.chmod(0o700)
        self.binary = self.base / "native"
        write_native(self.binary)

    def test_native_control_and_symlink_return_canonical_target(self) -> None:
        alias = self.base / "alias"
        alias.symlink_to(self.binary)
        with mock.patch("subprocess.run") as spawn:
            self.assertEqual(policy.require_native_executable(alias), str(self.binary))
        spawn.assert_not_called()

    def test_relative_path_and_missing_target_fail(self) -> None:
        for path, code in (("relative", "path"), (self.base / "missing", "not_found")):
            with (
                self.subTest(path=path),
                self.assertRaises(policy.ExecutablePolicyError) as error,
            ):
                policy.require_native_executable(path)
            self.assertEqual(error.exception.code, code)

    def test_scripts_and_text_are_rejected_without_running(self) -> None:
        for data in (
            b"#!/bin/sh\nexit 0\n",
            b"#!/usr/bin/env node\n",
            b"print('hello')\n",
            b"\x7fELF",
        ):
            self.binary.write_bytes(data)
            with self.subTest(data=data), mock.patch("subprocess.run") as spawn:
                with self.assertRaises(policy.ExecutablePolicyError) as error:
                    policy.require_native_executable(self.binary)
                self.assertEqual(error.exception.code, "native_required")
                spawn.assert_not_called()

    def test_fifo_is_rejected_before_leaf_open(self) -> None:
        fifo = self.base / "fifo"
        os.mkfifo(fifo)
        real_open = os.open

        def guarded_open(path: object, *args: object, **kwargs: object) -> int:
            self.assertNotEqual(path, "fifo")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=guarded_open):
            with self.assertRaises(policy.ExecutablePolicyError) as error:
                policy.require_native_executable(fifo)
        self.assertEqual(error.exception.code, "file_type")

    def test_leaf_and_ancestor_writability_fail_on_every_call(self) -> None:
        for target, mode in (
            (self.binary, 0o702),
            (self.binary, 0o720),
            (self.base, 0o777),
            (self.base, 0o770),
        ):
            with self.subTest(target=target, mode=mode):
                target.chmod(mode)
                try:
                    with self.assertRaises(policy.ExecutablePolicyError) as error:
                        policy.require_native_executable(self.binary)
                    self.assertEqual(error.exception.code, "writable")
                finally:
                    target.chmod(0o700)
        self.assertEqual(
            policy.require_native_executable(self.binary), str(self.binary)
        )

    def test_sticky_trusted_parent_is_not_a_blanket_write_exception(self) -> None:
        self.base.chmod(0o1777)
        self.assertEqual(
            policy.require_native_executable(self.binary), str(self.binary)
        )
        self.binary.chmod(0o777)
        with self.assertRaises(policy.ExecutablePolicyError) as error:
            policy.require_native_executable(self.binary)
        self.assertEqual(error.exception.code, "writable")

    def test_foreign_owner_rejected_even_without_write_bits(self) -> None:
        metadata = SimpleNamespace(
            st_uid=os.geteuid() + 100_000,
            st_gid=os.getgid(),
            st_mode=stat.S_IFDIR | 0o555,
        )
        with self.assertRaises(policy.ExecutablePolicyError) as error:
            policy._require_permissions(-1, metadata, directory=True)
        self.assertEqual(error.exception.code, "owner")

    def test_only_the_explicit_trusted_admin_group_gets_mode_exception(self) -> None:
        metadata = SimpleNamespace(
            st_uid=os.geteuid(), st_gid=7777, st_mode=stat.S_IFDIR | 0o775
        )
        with mock.patch.object(
            policy, "has_untrusted_acl_mutation", return_value=False
        ):
            with mock.patch.object(policy, "trusted_admin_group", return_value=7777):
                policy._require_permissions(-1, metadata, directory=True)
            with mock.patch.object(policy, "trusted_admin_group", return_value=None):
                with self.assertRaises(policy.ExecutablePolicyError):
                    policy._require_permissions(-1, metadata, directory=True)

    def test_acl_and_filesystem_inspection_fail_closed(self) -> None:
        for function in ("has_untrusted_acl_mutation", "require_permission_filesystem"):
            with (
                self.subTest(function=function),
                mock.patch.object(policy, function, side_effect=OSError("unavailable")),
            ):
                with self.assertRaises(policy.ExecutablePolicyError) as error:
                    policy.require_native_executable(self.binary)
                self.assertEqual(error.exception.code, "inspection")

    @unittest.skipUnless(sys.platform == "darwin", "requires Darwin ACLs")
    def test_real_darwin_acl_read_and_deny_allow_but_mutation_rejects(self) -> None:
        for target, entry, allowed in (
            (self.binary, "everyone deny delete", True),
            (self.binary, "everyone allow read", True),
            (self.binary, "everyone allow write", False),
            (self.binary, "everyone allow writesecurity", False),
            (self.base, "everyone allow delete_child", False),
        ):
            with self.subTest(entry=entry):
                subprocess.run(
                    ["/bin/chmod", "+a", entry, str(target)],
                    check=True,
                    capture_output=True,
                )
                try:
                    if allowed:
                        self.assertEqual(
                            policy.require_native_executable(self.binary),
                            str(self.binary),
                        )
                    else:
                        with self.assertRaises(policy.ExecutablePolicyError) as error:
                            policy.require_native_executable(self.binary)
                        self.assertIn(error.exception.code, ("acl", "writable"))
                finally:
                    # Only disposable test fixtures; never installed paths.
                    subprocess.run(
                        ["/bin/chmod", "-N", str(target)],
                        check=True,
                        capture_output=True,
                    )

    def test_formats_reject_truncation_libraries_and_bad_architecture_tables(
        self,
    ) -> None:
        valid_macho = macho_bytes()
        fat = (
            struct.pack(
                ">IIiiIII", 0xCAFEBABE, 1, 0x0100000C, 0, 32, len(valid_macho), 2
            )
            + b"\0" * 4
            + valid_macho
        )
        cases = [
            ("darwin", valid_macho, True),
            ("darwin", fat, True),
            ("darwin", macho_bytes(file_type=6), False),
            ("darwin", b"\xca\xfe\xba\xbe\0\0\0\x34" + b"java", False),
            ("darwin", fat[:-1], False),
            ("darwin", valid_macho[:4], False),
            ("linux", elf_bytes(), True),
            ("linux", elf_bytes(file_type=2), True),
            ("linux", elf_bytes(wide=False, endian=">"), True),
            ("linux", elf_bytes(file_type=1), False),
            ("linux", elf_bytes()[:-1], False),
            ("linux", b"\x7fELF" + b"\0" * 64, False),
            ("unknown", valid_macho, False),
        ]
        for target, data, expected in cases:
            with self.subTest(platform=target, data=data[:16], expected=expected):
                self.binary.write_bytes(data)
                with self.binary.open("rb") as stream:
                    self.assertEqual(
                        formats.is_native_executable(
                            stream.fileno(), len(data), target
                        ),
                        expected,
                    )

    def test_linux_permission_filesystem_allowlist_and_unknown_refusal(self) -> None:
        def library_for(magic: int) -> mock.Mock:
            def statfs(_fd: int, pointer: object) -> int:
                ctypes.cast(
                    pointer, ctypes.POINTER(platform_policy._LinuxStatFS)
                ).contents.type = magic
                return 0

            return mock.Mock(fstatfs=statfs)

        with mock.patch.object(platform_policy.sys, "platform", "linux"):
            for magic in (0xEF53, 0x01021994, 0x794C7630):
                with mock.patch.object(
                    platform_policy, "_linux_libc", return_value=library_for(magic)
                ):
                    platform_policy.require_permission_filesystem(0)
            for magic in (0x6969, 0x65735546, 0xFF534D42, 0):
                with mock.patch.object(
                    platform_policy, "_linux_libc", return_value=library_for(magic)
                ):
                    with self.assertRaises(OSError):
                        platform_policy.require_permission_filesystem(0)


if __name__ == "__main__":
    unittest.main()
