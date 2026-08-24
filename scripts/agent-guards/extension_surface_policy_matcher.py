#!/usr/bin/env python3
"""
extension_surface_policy_matcher.py

Shared evaluator (Issue #2290) that reads
``docs/dev/extension-surface-runtime-policy.yaml`` (schema_version v2) and
determines whether an Issue's declared ``## Allowed Paths`` are syntactic
candidates for one or more of the policy's risk-trigger rules.

Scope (Issue #2290 In Scope / Out of Scope -- see Issue body):

- This module performs **candidate discovery only** -- a deterministic,
  syntactic comparison between the declared Allowed Paths and the policy's
  ``selectors[].source_scope: project`` ``path_globs``. It never performs a
  semantic diff judgment of actual PR changes (that consumer is explicitly
  Out of Scope for Issue #2290; see the ``pr-review-judge`` / impl-review-loop
  follow-up referenced in the Issue body).
- Exact Allowed Path entries (no ``*`` character) are matched directly
  against a rule's ``path_globs`` using the same matcher-v2 segment grammar
  as ``scripts/agent-guards/changed_file_matcher.py``'s ``AllowedPathsMatcher``
  (``*`` = one path segment, ``**`` = zero or more segments).
- Wildcard Allowed Path entries are intentionally **not** run through a full
  glob-intersection engine. Instead this module performs a conservative
  literal/static-prefix comparison: it is not permissible to design this in
  a way that risks missing a real risk-trigger candidate (false negative);
  over-flagging a candidate that a human later dismisses (false positive) is
  the accepted, safe side of the trade-off.

Consumers (``.claude/skills/review-issue/scripts/check_issue_contract.py`` and
``.claude/skills/issue-contract-review/scripts/contract_readiness_check.py``)
MUST dynamically load this module via ``importlib`` -- mirroring the existing
pattern in
``.claude/skills/issue-contract-review/scripts/declared_path_overlap.py``,
which dynamically loads ``scripts/agent-guards/changed_file_matcher.py`` the
same way -- rather than introducing a new static import boundary / Python
package (Issue #2290 "Notes for Reviewer").
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

_MODULE_DIR = Path(__file__).resolve().parent
# parents: [0]=scripts, [1]=<repo root>
_REPO_ROOT = _MODULE_DIR.parents[1]
_DEFAULT_POLICY_YAML_PATH = _REPO_ROOT / "docs" / "dev" / "extension-surface-runtime-policy.yaml"

if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from changed_file_matcher import AllowedPathsMatcher  # noqa: E402

SCHEMA_POLICY_EVALUATION = "EXTENSION_SURFACE_POLICY_EVALUATION_V1"
SCHEMA_RISK_TRIGGER_VERDICT = "EXTENSION_SURFACE_RISK_TRIGGER_VERDICT_V1"

# Mirrors `_RVA_IMMEDIATE_REQUIRED_FIELDS` in
# `.claude/skills/issue-contract-review/scripts/contract_readiness_check.py`'s
# `check_rva_immediate_fields()` -- centralised here so
# `check_issue_contract.py` (review-issue) does not reimplement the field
# list independently (Issue #2290 Current Validated Scope, 4th bullet).
RVA_IMMEDIATE_REQUIRED_FIELDS = [
    "applicable_acs",
    "execution_environment",
    "skip_conditions",
    "fallback_policy",
    "artifact_requirements",
]

# `resolution.final_decision: most_restrictive` in the policy YAML resolves
# to this fixed rank ordering (see the YAML's own `resolution:` comment):
# immediate > deferred > not_applicable, most restrictive first.
DECISION_RANK: dict[str, int] = {
    "not_applicable": 0,
    "deferred": 1,
    "immediate": 2,
}


class PolicyLoadError(RuntimeError):
    """Raised when the policy YAML cannot be loaded or does not parse to a mapping."""


def load_policy(policy_path: Optional[Path] = None) -> dict[str, Any]:
    """Load and parse the extension-surface risk-trigger policy YAML."""
    path = policy_path or _DEFAULT_POLICY_YAML_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise PolicyLoadError(f"failed to read policy yaml at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"failed to parse policy yaml at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyLoadError(f"policy yaml at {path} did not parse to a mapping")
    return data


def _static_prefix_segments(normalized_glob: str) -> list[str]:
    """Segments of a normalized path/glob before its first wildcard segment."""
    prefix: list[str] = []
    for segment in normalized_glob.split("/"):
        if segment in ("*", "**"):
            break
        prefix.append(segment)
    return prefix


def _one_is_prefix_of_other(a: list[str], b: list[str]) -> bool:
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def _project_path_globs(rule: dict[str, Any]) -> list[str]:
    """All ``path_globs`` from a rule's ``selectors[].source_scope: project`` entries.

    ``runtime_resolved_only`` selectors (user/managed/plugin/session/cli
    source_scope) carry no ``path_globs`` and are intentionally excluded --
    they cannot be evaluated against repository-relative Allowed Paths
    (Issue #2290 In Scope: ``selectors[].source_scope: project`` only).
    """
    globs: list[str] = []
    for selector in rule.get("selectors", []) or []:
        if selector.get("source_scope") != "project":
            continue
        globs.extend(selector.get("path_globs", []) or [])
    return globs


def match_allowed_path_entry(entry: str, rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return a match-info dict if ``entry`` is a candidate for ``rule``, else ``None``."""
    normalized_entry_pattern = AllowedPathsMatcher.normalize_allowed_pattern(entry)
    if normalized_entry_pattern is None:
        return None
    is_wildcard = "*" in normalized_entry_pattern

    for path_glob in _project_path_globs(rule):
        normalized_selector = AllowedPathsMatcher.normalize_allowed_pattern(path_glob)
        if normalized_selector is None:
            continue

        if not is_wildcard:
            # Exact Allowed Path: direct matcher-v2 file-vs-pattern match
            # against the selector glob (Issue #2290 In Scope, bullet 1).
            normalized_file = AllowedPathsMatcher.normalize_path(entry)
            if normalized_file is None:
                continue
            if AllowedPathsMatcher.matches_pattern(normalized_file, normalized_selector):
                return {
                    "match_kind": "exact",
                    "allowed_path_entry": entry,
                    "path_glob": path_glob,
                }
            continue

        # Wildcard Allowed Path: conservative literal/static-prefix
        # candidate detection only -- NOT a full glob-intersection engine
        # (Issue #2290 In Scope, bullet 1). Either prefix being a prefix of
        # the other means the two path spaces *could* overlap once the
        # wildcard segments are expanded; this over-flags rather than
        # silently missing a candidate.
        entry_prefix = _static_prefix_segments(normalized_entry_pattern)
        selector_prefix = _static_prefix_segments(normalized_selector)
        if _one_is_prefix_of_other(entry_prefix, selector_prefix):
            return {
                "match_kind": "conservative_wildcard_prefix",
                "allowed_path_entry": entry,
                "path_glob": path_glob,
            }

    return None


def evaluate_allowed_paths(
    allowed_path_entries: list[str],
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Evaluate declared Allowed Paths against every rule in the policy.

    Applies the policy's ``resolution.multiple_matches: evaluate_all`` /
    ``resolution.final_decision: most_restrictive`` semantics (Issue #2290
    In Scope, bullet 2) and derives the union of required
    ``verification_profiles`` from the matched rules (Issue #2290 In Scope,
    bullet 3).
    """
    policy_data = policy if policy is not None else load_policy()
    rules = policy_data.get("rules", []) or []

    matched_rules: list[dict[str, Any]] = []
    verification_profiles: set[str] = set()
    final_decision: Optional[str] = None

    for rule in rules:
        match_hits: list[dict[str, Any]] = []
        for entry in allowed_path_entries:
            hit = match_allowed_path_entry(entry, rule)
            if hit is not None:
                match_hits.append(hit)
        if not match_hits:
            continue

        rule_decision = rule.get("default_decision")
        rule_profile = rule.get("verification_profile")
        matched_rules.append(
            {
                "rule_id": rule.get("id"),
                "default_decision": rule_decision,
                "verification_profile": rule_profile,
                "matches": match_hits,
            }
        )
        if rule_profile:
            verification_profiles.add(rule_profile)
        if rule_decision in DECISION_RANK:
            if final_decision is None or DECISION_RANK[rule_decision] > DECISION_RANK[final_decision]:
                final_decision = rule_decision

    return {
        "schema": SCHEMA_POLICY_EVALUATION,
        "matched_rules": matched_rules,
        "has_match": bool(matched_rules),
        "final_decision": final_decision,
        "verification_profiles": sorted(verification_profiles),
        "resolution": {
            "multiple_matches": (policy_data.get("resolution") or {}).get("multiple_matches"),
            "final_decision_strategy": (policy_data.get("resolution") or {}).get("final_decision"),
        },
    }


def find_missing_rva_immediate_fields(rva_section_text: str) -> list[str]:
    """Return required-field names missing from an ``immediate`` RVA section.

    Mirrors the field-presence semantics of
    ``.claude/skills/issue-contract-review/scripts/contract_readiness_check.py``'s
    ``check_rva_immediate_fields()`` (simple ``^\\s*<field>:`` regex match per
    required field), centralised here so callers do not reimplement it
    independently (Issue #2290 Current Validated Scope, 4th bullet).
    """
    missing: list[str] = []
    for field_name in RVA_IMMEDIATE_REQUIRED_FIELDS:
        pattern = re.compile(rf"^\s*{re.escape(field_name)}\s*:", re.MULTILINE)
        if not pattern.search(rva_section_text or ""):
            missing.append(field_name)
    return missing


def evaluate_issue_risk_trigger(
    allowed_path_entries: list[str],
    declared_decision: Optional[str],
    rva_section_text: str,
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """High-level verdict shared verbatim by both consumers.

    Both ``check_issue_contract.py`` (review-issue) and
    ``contract_readiness_check.py`` (issue-contract-review) call this single
    function so that, given the same fixture body, they return the same
    verdict (Issue #2290 AC7 parity requirement) -- the parity is structural
    (same function, same inputs), not independently re-derived.

    ``verdict`` is ``needs_fix`` when either:
      - the declared Allowed Paths have a policy candidate match whose
        ``final_decision`` (most-restrictive) outranks the Issue's declared
        Runtime Verification Applicability ``decision`` (AC1 / AC2), or
      - ``declared_decision == "immediate"`` but the RVA section is missing
        one or more of the required immediate fields (AC6).
    """
    policy_evaluation = evaluate_allowed_paths(allowed_path_entries, policy=policy)
    reasons: list[str] = []

    declared_rank = DECISION_RANK.get(declared_decision) if declared_decision else None
    final_decision = policy_evaluation["final_decision"]

    if policy_evaluation["has_match"] and final_decision is not None:
        final_rank = DECISION_RANK[final_decision]
        if declared_rank is None or declared_rank < final_rank:
            matched_rule_ids = [r["rule_id"] for r in policy_evaluation["matched_rules"]]
            reasons.append(
                "declared Allowed Paths overlap with extension-surface risk-trigger "
                f"policy rule(s) {matched_rule_ids} whose most-restrictive default_decision "
                f"is '{final_decision}', but the Issue declares "
                f"decision: '{declared_decision}'."
            )

    missing_fields: list[str] = []
    if declared_decision == "immediate":
        missing_fields = find_missing_rva_immediate_fields(rva_section_text)
        if missing_fields:
            reasons.append(
                "decision: immediate but the Runtime Verification Applicability "
                "contract's required immediate fields are missing: "
                + ", ".join(missing_fields)
            )

    verdict = "needs_fix" if reasons else "approve"
    return {
        "schema": SCHEMA_RISK_TRIGGER_VERDICT,
        "verdict": verdict,
        "reasons": reasons,
        "policy_evaluation": policy_evaluation,
        "missing_rva_immediate_fields": missing_fields,
    }
