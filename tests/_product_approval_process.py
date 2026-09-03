# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Isolated child used to prove approval-ledger process serialization."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cam1lib import product_approvals


def _wait_for(path: Path, *, detail: str) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() >= deadline:
            raise SystemExit(detail)
        time.sleep(0.005)


def _run_create_lock_timeout() -> int:
    if len(sys.argv) != 8:
        raise SystemExit(
            "usage: _product_approval_process.py create-lock-timeout ACCOUNT_HOME "
            "PUBLISHED CONTINUE VENDOR PRODUCT_BIN FINGERPRINT"
        )
    account_home, published, proceed, vendor, product_bin, fingerprint = sys.argv[2:]
    real_lock = product_approvals._recovery.acquire_registry_lock

    def coordinated_lock(
        descriptor: int,
        *,
        exclusive: bool,
        timeout_seconds: float,
        poll_seconds: float,
    ) -> None:
        Path(published).touch(mode=0o600)
        _wait_for(Path(proceed), detail="creator continuation gate was not opened")
        real_lock(
            descriptor,
            exclusive=exclusive,
            timeout_seconds=0.05,
            poll_seconds=min(poll_seconds, 0.005),
        )

    with (
        mock.patch.object(
            product_approvals,
            "account_home",
            return_value=Path(account_home),
        ),
        mock.patch.object(
            product_approvals._recovery,
            "acquire_registry_lock",
            side_effect=coordinated_lock,
        ),
    ):
        try:
            product_approvals.approve_candidate(
                vendor=vendor,
                product_bin=product_bin,
                expected_fingerprint_sha256=fingerprint,
                operator_reference="direct creation-race test confirmation",
            )
        except product_approvals.ProductApprovalError as error:
            print(json.dumps({"code": error.code}, sort_keys=True))
            return 0
    raise SystemExit("creator unexpectedly acquired the registry lock")


def _run_hold_registry_lock() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: _product_approval_process.py hold-registry-lock "
            "ACCOUNT_HOME READY RELEASE"
        )
    account_home, ready, release = sys.argv[2:]
    registry = (
        Path(account_home) / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
    )
    descriptor = os.open(
        registry,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        Path(ready).touch(mode=0o600)
        _wait_for(Path(release), detail="registry-lock release gate was not opened")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    print(json.dumps({"status": "released"}, sort_keys=True))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "create-lock-timeout":
        return _run_create_lock_timeout()
    if len(sys.argv) > 1 and sys.argv[1] == "hold-registry-lock":
        return _run_hold_registry_lock()
    if len(sys.argv) != 6:
        raise SystemExit(
            "usage: _product_approval_process.py ACCOUNT_HOME GATE "
            "VENDOR PRODUCT_BIN FINGERPRINT"
        )
    account_home, gate, vendor, product_bin, fingerprint = sys.argv[1:]
    _wait_for(Path(gate), detail="concurrency gate was not opened")
    with mock.patch.object(
        product_approvals,
        "account_home",
        return_value=Path(account_home),
    ):
        result = product_approvals.approve_candidate(
            vendor=vendor,
            product_bin=product_bin,
            expected_fingerprint_sha256=fingerprint,
            operator_reference="same direct cross-process confirmation",
        )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
