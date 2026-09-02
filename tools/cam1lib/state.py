# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Compatibility facade for journal-backed CAM/1 state.

Projection and replay behavior lives in :mod:`state_projection`; mutation and
lifecycle planning live in :mod:`state_store`. Existing imports from this
module remain stable.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .compatibility import (
    COMPATIBILITY_FORMAT,
    COMPATIBILITY_GATE_ACTIVATED_EVENT,
    COMPATIBILITY_KERNEL_CAPABILITY,
    COMPATIBILITY_KERNEL_FEATURE_ID,
    COMPATIBILITY_KERNEL_FEATURE_VERSION,
    COMPATIBILITY_PLAN_EVENT,
    COMPATIBILITY_READINESS_EVENT,
    CURRENT_READER_EPOCH,
    SUPPORTED_READER_CAPABILITIES,
    CompatibilityEventError,
    CompatibilityGate,
    CompatibilityInspection,
    CompatibilityPlan,
    CompatibilityProjection,
    CompatibilityReadiness,
    CompatibilityUpgradeRequired,
)
from .journal import append_record, decode_exact_message
from .lifecycle import ROOT_TYPES, LifecycleEntry, LifecycleProjection, LifecycleState
from .participants import Participant, ParticipantRoster
from .project import (
    ProjectBinding,
    ProjectError,
    ProjectTransaction,
    project_transaction,
    replace_private_json,
    require_project_transaction,
)
from .protocol import (
    REPLY_TYPES,
    CamUsageError,
    CamValidationError,
    ValidationPolicy,
    parse_exact_bytes,
)
from .state_projection import (
    LIFECYCLE_EXPIRED_UNCONFIRMED,
    LIFECYCLE_REPLY_APPLIED,
    LIFECYCLE_ROOT_REGISTERED,
    PARTICIPANT_ADDED,
    PARTICIPANT_BOUND,
    PARTICIPANT_INVALIDATED,
    PARTICIPANT_RETIRED,
    PARTICIPANT_ROUTE_CONFIRMED,
    PARTICIPANT_ROUTE_OBSERVED,
    STATE_EVENT_TYPES,
    STATE_PROJECTION_NAME,
    LifecyclePlan,
    ProjectionRefreshError,
    StateError,
    StateSnapshot,
    inspect_compatibility,
    require_plan_freshness,
    state_projection_path,
)
from .state_store import StateStore, rebuild_state, validate_cancel_exact_bytes
from .validation import validate_exact_bytes

__all__ = [
    "COMPATIBILITY_FORMAT",
    "COMPATIBILITY_GATE_ACTIVATED_EVENT",
    "COMPATIBILITY_KERNEL_CAPABILITY",
    "COMPATIBILITY_KERNEL_FEATURE_ID",
    "COMPATIBILITY_KERNEL_FEATURE_VERSION",
    "COMPATIBILITY_PLAN_EVENT",
    "COMPATIBILITY_READINESS_EVENT",
    "CURRENT_READER_EPOCH",
    "LIFECYCLE_EXPIRED_UNCONFIRMED",
    "LIFECYCLE_REPLY_APPLIED",
    "LIFECYCLE_ROOT_REGISTERED",
    "PARTICIPANT_ADDED",
    "PARTICIPANT_BOUND",
    "PARTICIPANT_INVALIDATED",
    "PARTICIPANT_RETIRED",
    "PARTICIPANT_ROUTE_CONFIRMED",
    "PARTICIPANT_ROUTE_OBSERVED",
    "REPLY_TYPES",
    "ROOT_TYPES",
    "STATE_EVENT_TYPES",
    "STATE_PROJECTION_NAME",
    "SUPPORTED_READER_CAPABILITIES",
    "Any",
    "CamUsageError",
    "CamValidationError",
    "CompatibilityEventError",
    "CompatibilityGate",
    "CompatibilityInspection",
    "CompatibilityPlan",
    "CompatibilityProjection",
    "CompatibilityReadiness",
    "CompatibilityUpgradeRequired",
    "Iterator",
    "LifecycleEntry",
    "LifecyclePlan",
    "LifecycleProjection",
    "LifecycleState",
    "Mapping",
    "Participant",
    "ParticipantRoster",
    "Path",
    "ProjectBinding",
    "ProjectError",
    "ProjectTransaction",
    "ProjectionRefreshError",
    "StateError",
    "StateSnapshot",
    "StateStore",
    "ValidationPolicy",
    "append_record",
    "cast",
    "contextmanager",
    "dataclass",
    "decode_exact_message",
    "deepcopy",
    "dt",
    "field",
    "inspect_compatibility",
    "parse_exact_bytes",
    "project_transaction",
    "rebuild_state",
    "replace_private_json",
    "require_plan_freshness",
    "require_project_transaction",
    "state_projection_path",
    "uuid",
    "validate_cancel_exact_bytes",
    "validate_exact_bytes",
]
