# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Isolated child used to prove approval-ledger process serialization."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cam1lib import product_approvals  # noqa: E402


def main() -> int:
    if len(sys.argv) != 6:
        raise SystemExit(
            "usage: _product_approval_process.py ACCOUNT_HOME GATE "
            "VENDOR PRODUCT_BIN FINGERPRINT"
        )
    account_home, gate, vendor, product_bin, fingerprint = sys.argv[1:]
    deadline = time.monotonic() + 10
    while not Path(gate).exists():
        if time.monotonic() >= deadline:
            raise SystemExit("concurrency gate was not opened")
        time.sleep(0.005)
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
