#!/usr/bin/env python3
"""Closed, agent-neutral policy contract for worktree mutations (Issue #1670).

Runtime adapters own vendor DTO parsing.  This module accepts only normalized
intent and binding contracts; it deliberately has no dependency on hook payload
field names such as ``tool_input`` or ``tool_name``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

INTENT_SCHEMA = "WORKTREE_MUTATION_INTENT_V1"
BINDING_SCHEMA = "WORKTREE_SCOPE_BINDING_V1"
DECISION_SCHEMA = "WORKTREE_POLICY_DECISION_V1"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BINDING_STATES = frozenset({"bound", "verified_unbound", "unknown", "ambiguous", "resolution_failed"})
_MUTATION_KINDS = frozenset({"write", "apply_patch", "bash"})
_PATH_FLAVORS = frozenset({"posix", "windows"})
_MAX_TARGETS = 32
_MAX_PATH_LENGTH = 4096


class ContractValidationError(ValueError):
    """Raised when a closed policy-domain input is malformed or unsupported."""


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ContractValidationError(f"{label} keys must be closed; missing={sorted(expected - actual)!r} extra={sorted(actual - expected)!r}")


def _require_string(value: object, label: str, *, maximum: int = _MAX_PATH_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ContractValidationError(f"{label} must be a non-empty bounded string without NUL")
    return value


def _require_digest(value: object, label: str) -> str:
    digest = _require_string(value, label, maximum=80)
    if not _DIGEST_RE.fullmatch(digest):
        raise ContractValidationError(f"{label} must be a sha256 digest")
    return digest


def _is_absolute(path: str, path_flavor: str) -> bool:
    if path_flavor == "windows":
        return bool(re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"))
    return path.startswith("/")


def _inside(expected: str, target: str, path_flavor: str) -> bool:
    separator = "\\" if path_flavor == "windows" else "/"
    normalized_expected = expected.replace("/", separator).rstrip(separator)
    normalized_target = target.replace("/", separator)
    if path_flavor == "windows":
        normalized_expected = normalized_expected.lower()
        normalized_target = normalized_target.lower()
    return normalized_target == normalized_expected or normalized_target.startswith(normalized_expected + separator)


def make_intent(
    *,
    runtime: str,
    runtime_version: str,
    tool_identity: str,
    canonical_identity: str,
    mutation_kind: str,
    target_paths: list[str],
    path_flavor: str,
    capture_digest: str,
) -> dict[str, object]:
    """Build and validate an adapter output, never accepting a vendor DTO."""
    return validate_intent(
        {
            "schema": INTENT_SCHEMA,
            "runtime": runtime,
            "runtime_version": runtime_version,
            "tool_identity": tool_identity,
            "canonical_identity": canonical_identity,
            "mutation_kind": mutation_kind,
            "target_paths": target_paths,
            "path_flavor": path_flavor,
            "capture_digest": capture_digest,
        }
    )


def validate_intent(value: object) -> dict[str, object]:
    intent = _require_mapping(value, "intent")
    _require_exact_keys(
        intent,
        frozenset({"schema", "runtime", "runtime_version", "tool_identity", "canonical_identity", "mutation_kind", "target_paths", "path_flavor", "capture_digest"}),
        "intent",
    )
    if intent["schema"] != INTENT_SCHEMA:
        raise ContractValidationError("unsupported intent schema")
    runtime = _require_string(intent["runtime"], "runtime", maximum=32)
    version = _require_string(intent["runtime_version"], "runtime_version", maximum=64)
    identity = _require_string(intent["tool_identity"], "tool_identity", maximum=64)
    canonical = _require_string(intent["canonical_identity"], "canonical_identity", maximum=64)
    kind = _require_string(intent["mutation_kind"], "mutation_kind", maximum=32)
    flavor = _require_string(intent["path_flavor"], "path_flavor", maximum=16)
    if kind not in _MUTATION_KINDS or flavor not in _PATH_FLAVORS:
        raise ContractValidationError("unsupported mutation kind or path flavor")
    # Exact supported capture matrix from #1663. Aliases are explicit, rather
    # than silently treated as canonical identities.
    supported = {
        ("claude", "2.1.216", "Write", "Write", "write"),
        ("claude", "2.1.216", "Edit", "Write", "write"),
        ("claude", "2.1.216", "MultiEdit", "Write", "write"),
        ("codex", "0.145.0", "apply_patch", "apply_patch", "apply_patch"),
    }
    if (runtime, version, identity, canonical, kind) not in supported:
        raise ContractValidationError("unsupported runtime version or tool identity")
    raw_targets = intent["target_paths"]
    if not isinstance(raw_targets, list) or not raw_targets or len(raw_targets) > _MAX_TARGETS:
        raise ContractValidationError("target_paths must be a non-empty bounded list")
    targets = [_require_string(target, "target_path") for target in raw_targets]
    if any(not _is_absolute(target, flavor) for target in targets):
        raise ContractValidationError("target_paths must be adapter-resolved absolute paths")
    return {
        "schema": INTENT_SCHEMA,
        "runtime": runtime,
        "runtime_version": version,
        "tool_identity": identity,
        "canonical_identity": canonical,
        "mutation_kind": kind,
        "target_paths": targets,
        "path_flavor": flavor,
        "capture_digest": _require_digest(intent["capture_digest"], "capture_digest"),
    }


def make_binding(
    *, state: str, expected_worktree: str | None, path_flavor: str, resolver_digest: str
) -> dict[str, object]:
    return validate_binding(
        {
            "schema": BINDING_SCHEMA,
            "state": state,
            "expected_worktree": expected_worktree,
            "path_flavor": path_flavor,
            "resolver_digest": resolver_digest,
        }
    )


def validate_binding(value: object) -> dict[str, object]:
    binding = _require_mapping(value, "binding")
    _require_exact_keys(binding, frozenset({"schema", "state", "expected_worktree", "path_flavor", "resolver_digest"}), "binding")
    if binding["schema"] != BINDING_SCHEMA:
        raise ContractValidationError("unsupported binding schema")
    state = _require_string(binding["state"], "binding state", maximum=32)
    flavor = _require_string(binding["path_flavor"], "binding path_flavor", maximum=16)
    if state not in _BINDING_STATES or flavor not in _PATH_FLAVORS:
        raise ContractValidationError("unsupported binding state or path flavor")
    expected = binding["expected_worktree"]
    if state == "bound":
        expected = _require_string(expected, "expected_worktree")
        if not _is_absolute(expected, flavor):
            raise ContractValidationError("bound expected_worktree must be absolute")
    elif expected is not None:
        raise ContractValidationError("unbound/indeterminate binding must not carry expected_worktree")
    return {
        "schema": BINDING_SCHEMA,
        "state": state,
        "expected_worktree": expected,
        "path_flavor": flavor,
        "resolver_digest": _require_digest(binding["resolver_digest"], "resolver_digest"),
    }


def policy_decide(intent_value: object, binding_value: object) -> dict[str, str]:
    """Evaluate normalized contracts and return a closed policy decision."""
    intent = validate_intent(intent_value)
    binding = validate_binding(binding_value)
    if intent["path_flavor"] != binding["path_flavor"]:
        return _decision("deny", "path_flavor_mismatch")
    state = binding["state"]
    if state == "bound":
        expected = str(binding["expected_worktree"])
        if all(_inside(expected, str(target), str(intent["path_flavor"])) for target in intent["target_paths"]):
            return _decision("allow", "mutation_inside_worktree")
        return _decision("deny", "target_outside_worktree")
    if state == "verified_unbound":
        # Compatibility fixed by #1663: Write/Edit deny, while the established
        # Codex apply_patch and Bash no-Issue behavior remains explicitly allow.
        if intent["mutation_kind"] == "write":
            return _decision("deny", "issue_context_required")
        return _decision("allow", "no_issue_no_scope")
    return _decision("deny", f"binding_{state}")


def _decision(decision: str, reason_code: str) -> dict[str, str]:
    return {"schema": DECISION_SCHEMA, "decision": decision, "reason_code": reason_code}


def digest_for_resolver(state: str, expected_worktree: str | None) -> str:
    """Create the bounded provenance digest passed from an adapter to policy."""
    raw = f"{state}\0{expected_worktree or ''}".encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()
