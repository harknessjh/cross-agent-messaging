# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Stable public facade and CLI for the offline CAM/1 reference tooling.

The implementation lives in small, dependency-ordered ``cam1lib`` modules.
This facade deliberately preserves the original constants, types, functions,
builders, private test seams, and command behavior.
"""

from __future__ import annotations

if __package__:
    from .cam1lib import builders as _builders
    from .cam1lib import cli as _cli
    from .cam1lib import protocol as _protocol
    from .cam1lib import validation as _validation
else:  # Direct execution: ``python tools/cam1.py``.
    from cam1lib import builders as _builders
    from cam1lib import cli as _cli
    from cam1lib import protocol as _protocol
    from cam1lib import validation as _validation

# Wire contract, limits, and result types.
ROOT = _protocol.ROOT
SCHEMA_PATH = _protocol.SCHEMA_PATH
MAX_ENVELOPE_BYTES = _protocol.MAX_ENVELOPE_BYTES
MAX_NESTING = _protocol.MAX_NESTING
DEFAULT_TTL_SECONDS = _protocol.DEFAULT_TTL_SECONDS
DEFAULT_MAX_TTL_SECONDS = _protocol.DEFAULT_MAX_TTL_SECONDS
DEFAULT_CLOCK_SKEW_SECONDS = _protocol.DEFAULT_CLOCK_SKEW_SECONDS
MAX_PROBLEMS = _protocol.MAX_PROBLEMS
UTC_PATTERN = _protocol.UTC_PATTERN
RECEIPT_TYPES = _protocol.RECEIPT_TYPES
REPLY_TYPES = _protocol.REPLY_TYPES
ACK_STATUSES = _protocol.ACK_STATUSES
STATELESS_REPLY_TRANSITIONS = _protocol.STATELESS_REPLY_TRANSITIONS
UUID_POINTERS = _protocol.UUID_POINTERS

CamUsageError = _protocol.CamUsageError
Problem = _protocol.Problem
ValidationResult = _protocol.ValidationResult
ValidationPolicy = _protocol.ValidationPolicy
DEFAULT_VALIDATION_POLICY = _protocol.DEFAULT_VALIDATION_POLICY
SemanticOutcome = _protocol.SemanticOutcome
CamValidationError = _protocol.CamValidationError
DuplicateKeyError = _protocol.DuplicateKeyError
CliError = _protocol.CliError

SCHEMA = _protocol.SCHEMA
VALIDATOR = _protocol.VALIDATOR
_load_schema = _protocol._load_schema
_pointer = _protocol._pointer
_unique_problems = _protocol._unique_problems
_reject_constant = _protocol._reject_constant
_finite_float = _protocol._finite_float
_object_without_duplicates = _protocol._object_without_duplicates
_scan_nesting = _protocol._scan_nesting
_find_surrogate = _protocol._find_surrogate
parse_exact_bytes = _protocol.parse_exact_bytes
_required_field_problems = _protocol._required_field_problems
_schema_problems = _protocol._schema_problems
_get = _protocol._get
_collection_limit_problems = _protocol._collection_limit_problems
serialize_envelope = _protocol.serialize_envelope

# Semantic validation and pairwise correlation.
_uuid_problem = _validation._uuid_problem
_uuid_values_equal = _validation._uuid_values_equal
_parse_timestamp = _validation._parse_timestamp
_nonce_problem = _validation._nonce_problem
_endpoint_matches = _validation._endpoint_matches
_identifier_problems = _validation._identifier_problems
_message_time_problems = _validation._message_time_problems
_authorization_time_problems = _validation._authorization_time_problems
_nonce_semantic_problems = _validation._nonce_semantic_problems
_body_hash_outcome = _validation._body_hash_outcome
_reply_semantic_problems = _validation._reply_semantic_problems
_callback_problems = _validation._callback_problems
_transition_problems = _validation._transition_problems
_correlation_outcome = _validation._correlation_outcome
_semantic_problems = _validation._semantic_problems
_normalize_now = _validation._normalize_now
validate_exact_bytes = _validation.validate_exact_bytes

# Envelope builders.
_utc_text = _builders._utc_text
_nonce = _builders._nonce
_body_digest = _builders._body_digest
_empty_scope = _builders._empty_scope
_safe_constraints = _builders._safe_constraints
build_hello = _builders.build_hello
build_ack = _builders.build_ack

# Local I/O and CLI surface.
read_envelope_file = _cli.read_envelope_file
_write_stdout = _cli._write_stdout
_write_output = _cli._write_output
_add_endpoint_arguments = _cli._add_endpoint_arguments
_add_output_arguments = _cli._add_output_arguments
_parser = _cli._parser
_emit_error = _cli._emit_error
main = _cli.main


if __name__ == "__main__":
    raise SystemExit(main())
