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
SCHEMA_RUNTIME_ASSERTION_BINDING_COVERAGE = "RUNTIME_ASSERTION_BINDING_COVERAGE_RESULT_V1"

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
    """Raised when the policy YAML cannot be loaded, does not parse to a
    mapping, or does not satisfy the cheap structural contract this module
    depends on (PR #2335 OWNER review fix_delta, Issue #2290 P1-2).

    This is intentionally NOT a full ``jsonschema.validate()`` run against
    ``docs/dev/extension-surface-runtime-policy.schema.json`` (that already
    happens separately in
    ``docs/dev/tests/test_extension_surface_runtime_policy_schema.py``).
    This is a cheap, targeted check of only the fields this module's own
    logic reads, so that a malformed/incompatible policy file fails closed
    with an explicit, distinguishable error instead of silently degrading
    to "no match" behaviour that looks identical to a legitimately clean
    Allowed Paths set.
    """


# `resolution` values this module's evaluation logic actually implements.
# A policy YAML declaring any other `resolution.multiple_matches` /
# `resolution.final_decision` value is not safely interpretable by this
# evaluator and must fail closed rather than silently mis-evaluate.
_SUPPORTED_RESOLUTION_MULTIPLE_MATCHES = {"evaluate_all"}
_SUPPORTED_RESOLUTION_FINAL_DECISION = {"most_restrictive"}


def _validate_policy_contract(data: dict[str, Any], path: Path) -> None:
    """Cheap structural validation of the fields this module depends on.

    Raises ``PolicyLoadError`` (not a bare assertion) so callers can
    distinguish "policy unavailable" from a normal "no candidate match"
    result (Issue #2290 P1-2).
    """
    if data.get("schema_version") != "v2":
        raise PolicyLoadError(
            f"policy yaml at {path} has unsupported schema_version "
            f"{data.get('schema_version')!r} (expected 'v2')"
        )

    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise PolicyLoadError(f"policy yaml at {path} has an empty or missing 'rules' list")

    resolution = data.get("resolution")
    if not isinstance(resolution, dict):
        raise PolicyLoadError(f"policy yaml at {path} is missing a 'resolution' mapping")
    multiple_matches = resolution.get("multiple_matches")
    if multiple_matches not in _SUPPORTED_RESOLUTION_MULTIPLE_MATCHES:
        raise PolicyLoadError(
            f"policy yaml at {path} declares unsupported "
            f"resolution.multiple_matches {multiple_matches!r} "
            f"(supported: {sorted(_SUPPORTED_RESOLUTION_MULTIPLE_MATCHES)})"
        )
    final_decision_strategy = resolution.get("final_decision")
    if final_decision_strategy not in _SUPPORTED_RESOLUTION_FINAL_DECISION:
        raise PolicyLoadError(
            f"policy yaml at {path} declares unsupported "
            f"resolution.final_decision {final_decision_strategy!r} "
            f"(supported: {sorted(_SUPPORTED_RESOLUTION_FINAL_DECISION)})"
        )


# `unknown_surface_policy.decision` / `.gate` values this module's evaluator
# actually implements. Mirrors `docs/dev/extension-surface-runtime-policy
# .schema.json`'s `unknown_surface_policy.decision` (`enum: [human_judgment]`)
# and `.gate` (narrowed to `const: advisory`, Issue #2339 PR #2370 OWNER
# review fix_delta P1-3 -- `gate: block` is no longer a supported production
# value; a policy declaring any other value is not safely interpretable and
# must fail closed rather than silently mis-evaluate or be read as dead
# metadata).
_EXPECTED_UNKNOWN_SURFACE_POLICY_DECISION = "human_judgment"
_EXPECTED_UNKNOWN_SURFACE_POLICY_GATE = "advisory"


def _extract_unknown_surface_policy(data: dict[str, Any]) -> dict[str, Any]:
    """Cheap structural validation + extraction of the full
    ``unknown_surface_policy`` mapping (``decision`` / ``gate`` /
    ``project_candidate_path_globs``) (Issue #2339 AC9; PR #2370 OWNER
    review fix_delta P1-1/P1-3).

    Called from ``evaluate_allowed_paths`` for *every* policy source (both
    the default ``load_policy()`` path and a ``policy=`` dict passed
    directly by a caller/test), so a malformed ``unknown_surface_policy``
    fails closed with a distinguishable ``PolicyLoadError`` regardless of
    how the policy mapping was constructed -- it must never silently
    degrade to "no candidate perimeter" (which would look identical to a
    legitimately clean/empty candidate perimeter and risk silently
    approving what should have been an advisory finding, Issue #2339 AC9).

    ``unknown_surface_policy`` is REQUIRED here (PR #2370 OWNER review
    fix_delta P1-1 -- the previous "optional at this cheap-validation layer"
    design let a missing/``None`` ``unknown_surface_policy`` or a missing
    ``project_candidate_path_globs`` key silently fall through to "no
    candidate perimeter" instead of failing closed, contradicting
    ``docs/dev/extension-surface-runtime-policy.schema.json`` which already
    declares all three of ``decision`` / ``gate`` /
    ``project_candidate_path_globs`` as required). Every fixture policy
    passed through this function -- including synthetic/minimal test
    fixtures -- must declare a structurally valid ``unknown_surface_policy``;
    this is intentionally NOT made optional again to "fix" a failing
    fixture (fixtures are the ones that must be updated, not this
    validation).

    ``decision`` and ``gate`` are validated against the single production
    value each currently supports (see the module-level
    ``_EXPECTED_UNKNOWN_SURFACE_POLICY_*`` constants above) rather than left
    unread as dead metadata (P1-3): an unsupported value is exactly as
    unsafe to silently ignore as a malformed
    ``project_candidate_path_globs`` list.
    """
    unknown_surface_policy = data.get("unknown_surface_policy")
    if not isinstance(unknown_surface_policy, dict):
        raise PolicyLoadError(
            f"policy declares 'unknown_surface_policy' as "
            f"{type(unknown_surface_policy).__name__ if unknown_surface_policy is not None else 'missing/None'}, "
            "expected a mapping (Issue #2339 AC9 / PR #2370 P1-1: a missing or non-mapping "
            "'unknown_surface_policy' must fail closed as policy-unavailable, not silently "
            "degrade to 'no candidate perimeter')"
        )

    decision = unknown_surface_policy.get("decision")
    if decision != _EXPECTED_UNKNOWN_SURFACE_POLICY_DECISION:
        raise PolicyLoadError(
            f"policy declares 'unknown_surface_policy.decision' as {decision!r}, expected "
            f"{_EXPECTED_UNKNOWN_SURFACE_POLICY_DECISION!r} (PR #2370 P1-1: an unsupported/missing "
            "decision value must fail closed rather than be silently unread)"
        )

    gate = unknown_surface_policy.get("gate")
    if gate != _EXPECTED_UNKNOWN_SURFACE_POLICY_GATE:
        raise PolicyLoadError(
            f"policy declares 'unknown_surface_policy.gate' as {gate!r}, expected "
            f"{_EXPECTED_UNKNOWN_SURFACE_POLICY_GATE!r} (PR #2370 P1-1/P1-3: an unsupported/missing "
            "gate value must fail closed rather than be silently unread)"
        )

    raw_globs = unknown_surface_policy.get("project_candidate_path_globs")
    if not isinstance(raw_globs, list) or not raw_globs:
        raise PolicyLoadError(
            "policy declares 'unknown_surface_policy.project_candidate_path_globs' as "
            f"{raw_globs!r}, expected a non-empty list of non-empty, matcher-v2-valid glob "
            "strings (Issue #2339 AC9: malformed unknown_surface_policy must fail closed as "
            "policy-unavailable, not silently approve)"
        )
    for glob in raw_globs:
        if not isinstance(glob, str) or not glob:
            raise PolicyLoadError(
                "policy declares an entry in 'unknown_surface_policy.project_candidate_path_globs' "
                f"as {glob!r}, expected a non-empty string (PR #2370 P1-1: fail closed rather than "
                "silently skip an invalid candidate glob)"
            )
        if AllowedPathsMatcher.normalize_allowed_pattern(glob) is None:
            raise PolicyLoadError(
                f"policy declares 'unknown_surface_policy.project_candidate_path_globs' entry "
                f"{glob!r} which is not a valid matcher-v2 glob (PR #2370 P1-1: an invalid glob "
                "must fail closed rather than be silently read as 'continue'/never match)"
            )

    return {
        "decision": decision,
        "gate": gate,
        "project_candidate_path_globs": raw_globs,
    }


def load_policy(policy_path: Optional[Path] = None) -> dict[str, Any]:
    """Load, parse, and cheaply validate the extension-surface risk-trigger
    policy YAML. Raises ``PolicyLoadError`` (never returns a partially
    unusable mapping) if the file cannot be read/parsed, does not parse to
    a mapping, or fails the cheap structural contract in
    ``_validate_policy_contract`` (Issue #2290 P1-2)."""
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
    _validate_policy_contract(data, path)
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


_DEFAULT_ISSUE_TIME_ENFORCEMENT = "hard"


def _project_path_globs(rule: dict[str, Any]) -> list[tuple[str, str]]:
    """``(path_glob, issue_time_enforcement)`` pairs from a rule's
    ``selectors[].source_scope: project`` entries.

    ``runtime_resolved_only`` selectors (user/managed/plugin/session/cli
    source_scope) carry no ``path_globs`` and are intentionally excluded --
    they cannot be evaluated against repository-relative Allowed Paths
    (Issue #2290 In Scope: ``selectors[].source_scope: project`` only).

    Each project selector may declare its own ``issue_time_enforcement``
    (``hard`` / ``advisory``, Issue #2356). A selector that omits the field
    is treated as ``hard`` -- the required runtime fallback for existing
    rules that predate this field (e.g. ``claude-gpt-lifecycle-invocation-change``).
    Pairing the enforcement value with each glob (instead of returning a
    flat ``list[str]``) preserves selector identity so callers can derive
    advisory/hard per-glob rather than relying on a separate hardcoded
    constant (Issue #2356; supersedes the removed ``CANDIDATE_ONLY_PATH_GLOBS``
    frozenset from Issue #2290 / PR #2335).
    """
    pairs: list[tuple[str, str]] = []
    for selector in rule.get("selectors", []) or []:
        if selector.get("source_scope") != "project":
            continue
        raw_issue_time_enforcement = selector.get("issue_time_enforcement")
        if raw_issue_time_enforcement is None:
            issue_time_enforcement = _DEFAULT_ISSUE_TIME_ENFORCEMENT
        elif raw_issue_time_enforcement in ("hard", "advisory"):
            issue_time_enforcement = raw_issue_time_enforcement
        else:
            raise PolicyLoadError(
                "project selector declares unsupported issue_time_enforcement "
                f"{raw_issue_time_enforcement!r} (expected 'hard', 'advisory', or omitted; "
                "a malformed value must fail closed rather than silently degrade to "
                "'advisory', PR #2359 OWNER review fix_delta, Issue #2356)"
            )
        for path_glob in selector.get("path_globs", []) or []:
            pairs.append((path_glob, issue_time_enforcement))
    return pairs


def _selector_remainder_after_prefix(normalized_selector: str, prefix_len: int) -> list[str]:
    """Segments of ``normalized_selector`` after its first ``prefix_len`` segments."""
    return normalized_selector.split("/")[prefix_len:]


def _is_single_file_at_any_depth_selector(normalized_selector: str, selector_prefix: list[str]) -> bool:
    """True if, past its static prefix, ``normalized_selector`` is exactly one
    ``**`` segment followed by exactly one literal terminal segment and
    nothing else (e.g. ``.claude/skills/**/SKILL.md``).

    This pattern shape means "a specific named file, located directly under
    whatever the ``**`` expands to" -- by this repository's own Claude Code
    skill convention (see this module's docstring: ``SKILL.md`` always sits
    directly under ``.claude/skills/<name>/``, never nested inside a further
    named subdirectory such as ``tests/``, ``fixtures/`` or ``schemas/``).
    Selectors with a *different* trailing shape (e.g.
    ``.claude/skills/**/scripts/**``, which ends in another ``**`` and thus
    describes an entire subtree, not a single fixed-depth file) do not
    qualify.
    """
    remainder = _selector_remainder_after_prefix(normalized_selector, len(selector_prefix))
    return len(remainder) == 2 and remainder[0] == "**" and remainder[1] not in ("*", "**")


def match_allowed_path_entry(entry: str, rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return a match-info dict if ``entry`` is a candidate for ``rule``, else ``None``.

    A rule may declare more than one project selector, each pairing its
    ``path_globs`` with its own ``issue_time_enforcement`` (e.g.
    ``skill-invocation-procedure-or-contract-change`` declares a ``hard``
    selector for ``.claude/skills/**/SKILL.md`` and a separate ``advisory``
    selector for ``.claude/skills/**/scripts/**``, Issue #2356). For a
    *wildcard* Allowed Path entry, the conservative static-prefix comparison
    can match several of the rule's globs at once with equal confidence
    (e.g. an entry whose prefix is ``.claude/skills/foo`` is an equally
    valid conservative match against both globs above). Picking only the
    first glob encountered is an ordering artifact, not a judgment -- if
    picking the "wrong" glob silently turns an advisory (candidate-discovery-
    only) match into a hard-block match, the gate self-contradicts on its
    own declared Allowed Paths (PR #2335 OWNER review fix_delta, Issue
    #2290 P0 re-fix). To stay on the documented "conservative" side (false
    negatives are acceptable, false positives are not; but a hard block
    triggered purely by glob-iteration order is itself a false positive
    here), a wildcard entry that ambiguously matches both an advisory glob
    and a hard glob within the same rule is resolved to the advisory match.
    An entry that matches only advisory globs, or only hard globs, is
    unambiguous and keeps its natural classification.
    """
    normalized_entry_pattern = AllowedPathsMatcher.normalize_allowed_pattern(entry)
    if normalized_entry_pattern is None:
        return None
    is_wildcard = "*" in normalized_entry_pattern

    matches: list[dict[str, Any]] = []
    for path_glob, issue_time_enforcement in _project_path_globs(rule):
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
                # An exact repo-relative path is unambiguous: it cannot
                # simultaneously live under two structurally distinct
                # globs of the same rule in practice, so the first match
                # found is authoritative (unaffected by the wildcard
                # ambiguity handling below).
                return {
                    "match_kind": "exact",
                    "allowed_path_entry": entry,
                    "path_glob": path_glob,
                    "issue_time_enforcement": issue_time_enforcement,
                }
            continue

        # Wildcard Allowed Path: conservative literal/static-prefix
        # candidate detection only -- NOT a full glob-intersection engine
        # (Issue #2290 In Scope, bullet 1). Either prefix being a prefix of
        # the other means the two path spaces *could* overlap once the
        # wildcard segments are expanded; this over-flags rather than
        # silently missing a candidate. Unlike the exact-path case above,
        # evaluate every glob in the rule (not just the first hit) so
        # ambiguous multi-glob matches can be detected below.
        entry_prefix = _static_prefix_segments(normalized_entry_pattern)
        selector_prefix = _static_prefix_segments(normalized_selector)
        if not _one_is_prefix_of_other(entry_prefix, selector_prefix):
            continue

        if len(entry_prefix) > len(selector_prefix) + 1 and _is_single_file_at_any_depth_selector(
            normalized_selector, selector_prefix
        ):
            # The entry's static prefix drills more than one segment past
            # this selector's own static prefix, while the selector names a
            # single specific file located directly under whatever its
            # "**" expands to (e.g. "SKILL.md" at the skill root). Per this
            # repository's Claude Code skill convention, such files are
            # never nested inside a further named subdirectory (tests/,
            # fixtures/, schemas/, ...), so a wildcard entry scoped to such
            # a subdirectory cannot conservatively be a candidate for this
            # selector -- unlike ``.claude/skills/**/scripts/**``-shaped
            # selectors (an entire-subtree pattern, unaffected by this
            # check), which is why this only applies to single-file-shaped
            # selectors (PR #2335 second OWNER review fix_delta, Issue
            # #2290 P0 re-fix).
            continue

        matches.append(
            {
                "match_kind": "conservative_wildcard_prefix",
                "allowed_path_entry": entry,
                "path_glob": path_glob,
                "issue_time_enforcement": issue_time_enforcement,
            }
        )

    if not matches:
        return None

    advisory_hits = [m for m in matches if m["issue_time_enforcement"] == "advisory"]
    hard_hits = [m for m in matches if m["issue_time_enforcement"] == "hard"]
    if advisory_hits and hard_hits:
        # Ambiguous: the same wildcard entry's conservative prefix matched
        # both an advisory selector and a hard selector within this rule,
        # with no basis to prefer one over the other. Resolve to the
        # advisory match rather than the hard match to avoid a
        # glob-iteration-order-dependent hard block.
        return advisory_hits[0]

    return matches[0]


def _matches_candidate_perimeter(entry: str, candidate_path_globs: list[str]) -> Optional[str]:
    """Return the first ``unknown_surface_policy.project_candidate_path_globs``
    entry that ``entry`` is a conservative candidate for, else ``None``.

    Mirrors ``match_allowed_path_entry``'s exact-match / conservative
    static-prefix-overlap comparison against a rule's ``path_globs``, but
    against the flat candidate-perimeter glob list instead of a rule's
    selectors (the candidate perimeter has no ``source_scope`` /
    ``issue_time_enforcement`` concept -- Issue #2339 AC11: it is a
    project-local-only, repository-relative glob list, never resolved
    against user/managed/plugin/session/cli source_scope surfaces).
    """
    normalized_entry_pattern = AllowedPathsMatcher.normalize_allowed_pattern(entry)
    if normalized_entry_pattern is None:
        return None
    is_wildcard = "*" in normalized_entry_pattern

    for glob in candidate_path_globs:
        normalized_glob = AllowedPathsMatcher.normalize_allowed_pattern(glob)
        if normalized_glob is None:
            continue

        if not is_wildcard:
            normalized_file = AllowedPathsMatcher.normalize_path(entry)
            if normalized_file is None:
                continue
            if AllowedPathsMatcher.matches_pattern(normalized_file, normalized_glob):
                return glob
            continue

        entry_prefix = _static_prefix_segments(normalized_entry_pattern)
        glob_prefix = _static_prefix_segments(normalized_glob)
        if _one_is_prefix_of_other(entry_prefix, glob_prefix):
            return glob

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

    Issue #2339: each declared Allowed Path entry is additionally
    classified into exactly one of ``matched_rule`` / ``unclassified_candidate``
    / ``ordinary`` (``path_classifications``), and any ``unclassified_candidate``
    entry (a ``unknown_surface_policy.project_candidate_path_globs`` match
    with no known rule selector match) contributes a non-blocking
    ``advisories`` message. ``unclassified_candidate`` entries never
    contribute to ``final_decision`` -- only ``matched_rule`` entries with
    ``enforcement: hard`` do (unchanged from Issue #2290/#2356 semantics).
    """
    policy_data = policy if policy is not None else load_policy()
    rules = policy_data.get("rules", []) or []
    unknown_surface_policy = _extract_unknown_surface_policy(policy_data)
    candidate_path_globs = unknown_surface_policy["project_candidate_path_globs"]
    # PR #2370 P1-3: `decision` / `gate` are read (not dead metadata) and
    # surfaced verbatim on every `unclassified_candidate` classification
    # below via `policy_action`, but never merged into `final_decision`
    # (Issue #2339 AC8) -- `human_judgment` never appears as a
    # `final_decision` value.
    policy_action = {"decision": unknown_surface_policy["decision"], "gate": unknown_surface_policy["gate"]}

    matched_rules: list[dict[str, Any]] = []
    verification_profiles: set[str] = set()
    final_decision: Optional[str] = None
    entries_with_rule_hit: set[str] = set()

    for rule in rules:
        match_hits: list[dict[str, Any]] = []
        for entry in allowed_path_entries:
            hit = match_allowed_path_entry(entry, rule)
            if hit is not None:
                match_hits.append(hit)
                entries_with_rule_hit.add(entry)
        if not match_hits:
            continue

        hard_hits = [hit for hit in match_hits if hit["issue_time_enforcement"] == "hard"]
        enforcement = "hard" if hard_hits else "advisory"

        rule_decision = rule.get("default_decision")
        rule_profile = rule.get("verification_profile")
        matched_rules.append(
            {
                "rule_id": rule.get("id"),
                "default_decision": rule_decision,
                "verification_profile": rule_profile,
                "matches": match_hits,
                "enforcement": enforcement,
            }
        )
        if rule_profile:
            verification_profiles.add(rule_profile)
        # Advisory-only matches (every hit's selector declares
        # issue_time_enforcement: advisory) are candidate discovery
        # signals, not a hard block -- they never contribute to
        # final_decision (Issue #2290 P0 fix delta, PR #2335; derivation
        # migrated from the removed CANDIDATE_ONLY_PATH_GLOBS constant to
        # selector-declared issue_time_enforcement by Issue #2356).
        if enforcement == "hard" and rule_decision in DECISION_RANK:
            if final_decision is None or DECISION_RANK[rule_decision] > DECISION_RANK[final_decision]:
                final_decision = rule_decision

    # Issue #2339: classify every declared entry that matched no rule as
    # either `unclassified_candidate` (a project_candidate_path_globs hit)
    # or `ordinary` (no candidate glob hit either). `matched_rule` entries
    # keep their existing `matched_rules` representation above; this pass
    # only adds the three-way per-entry classification view, it does not
    # change any existing matched-rule semantics.
    path_classifications: list[dict[str, Any]] = []
    advisories: list[str] = []
    for entry in allowed_path_entries:
        if entry in entries_with_rule_hit:
            path_classifications.append({"entry": entry, "classification": "matched_rule"})
            continue
        candidate_glob = _matches_candidate_perimeter(entry, candidate_path_globs)
        if candidate_glob is not None:
            path_classifications.append(
                {
                    "entry": entry,
                    "classification": "unclassified_candidate",
                    "candidate_path_glob": candidate_glob,
                    "policy_action": policy_action,
                }
            )
            advisories.append(
                f"Allowed Path entry '{entry}' matches the project-local extension candidate "
                f"perimeter glob '{candidate_glob}' (unknown_surface_policy.project_candidate_path_globs) "
                "but no known extension-surface risk-trigger rule selector. This is a non-blocking "
                "advisory -- verdict: approve, final_decision unaffected -- surfaced so a human can "
                "confirm whether this is a genuine new extension surface (Issue #2339)."
            )
        else:
            path_classifications.append({"entry": entry, "classification": "ordinary"})

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
        "path_classifications": path_classifications,
        "advisories": advisories,
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
        # Issue #2339: `unclassified_candidate` advisories (non-blocking --
        # never contribute to `reasons`/`verdict`; verdict stays `approve`
        # when reasons is empty even if advisories is non-empty, AC6).
        "advisories": policy_evaluation.get("advisories", []),
    }


# ---------------------------------------------------------------------------
# Runtime Verification profile assertion binding coverage (Issue #2771)
# ---------------------------------------------------------------------------
#
# This gate guarantees STRUCTURAL COMPLETENESS ONLY (structural_completeness_
# not_semantic_sufficiency): it verifies that every hard-required
# (verification_profile_id, assertion_id) composite pair -- derived purely
# from `matched_rules[].enforcement == "hard"`, never from an advisory-only
# match (PR #2370's non-blocking advisory semantics are not re-introduced as
# a blocker from this new angle) -- has an explicit `runtime_assertion_
# bindings` declaration in the Issue's Runtime Verification Applicability
# section. Whether the *bound* VC/AC actually proves the assertion's
# semantic postcondition (semantic sufficiency) is explicitly NOT judged
# here and remains the responsibility of existing semantic review
# (`pr-review-judge` etc.) -- see Issue #2771 Outcome / AC10.
#
# `.claude/skills/review-issue/scripts/check_issue_contract.py` and
# `.claude/skills/issue-contract-review/scripts/contract_readiness_check.py`
# both call the functions below (never reimplement composite-identity
# derivation independently) so the two consumers cannot drift (Issue #2771
# In Scope, "review-issue と issue-contract-review の双方で同一 shared
# evaluator を使い parity を維持する").


def derive_required_runtime_assertions(
    matched_rules: list[dict[str, Any]],
    policy: dict[str, Any],
) -> set[tuple[str, str]]:
    """Derive the required ``(verification_profile_id, assertion_id)``
    composite set from ``matched_rules`` (as returned by
    ``evaluate_allowed_paths()``), restricted to profiles that have at least
    one ``enforcement == "hard"`` matched rule (Issue #2771 AC1/AC2).

    Grouping is by ``verification_profile`` id, not by individual matched
    rule: if the *same* profile is reached by more than one matched rule
    (possible when a policy fixture declares two distinct rules that both
    reference the same profile), the profile's assertions are required as
    soon as *any* one of those rules is ``enforcement == "hard"`` -- an
    all-advisory set of rules for a profile keeps that profile entirely out
    of the required set (AC2). This is a profile-level decision; there is no
    per-assertion partial-hard/partial-advisory split within a single
    profile's own ``assertions[]`` list.

    Raises ``PolicyLoadError`` -- the same exception class this module
    already uses to fail closed on a malformed policy document -- for a
    *policy* integrity defect: a matched rule referencing a
    ``verification_profile`` id that is not defined in the policy's
    top-level ``verification_profiles`` mapping (a dangling profile
    reference), or a profile whose ``assertions[]`` list declares the same
    ``id`` more than once. Both are defects in
    ``docs/dev/extension-surface-runtime-policy.yaml`` itself, not in the
    Issue being reviewed, and Issue #2771 AC6 requires this module to keep
    that distinction explicit rather than silently reporting a policy defect
    as if it were an author-fixable Issue ``needs_fix``.
    """
    profiles = policy.get("verification_profiles")
    if not isinstance(profiles, dict):
        raise PolicyLoadError(
            "policy yaml is missing a 'verification_profiles' mapping (required to derive "
            "hard-required runtime assertion coverage, Issue #2771)"
        )

    referenced_profile_ids: set[str] = set()
    hard_profile_ids: set[str] = set()
    for rule in matched_rules:
        profile_id = rule.get("verification_profile")
        if not profile_id:
            continue
        referenced_profile_ids.add(profile_id)
        if rule.get("enforcement") == "hard":
            hard_profile_ids.add(profile_id)

    required: set[tuple[str, str]] = set()
    for profile_id in referenced_profile_ids:
        profile = profiles.get(profile_id)
        if not isinstance(profile, dict):
            raise PolicyLoadError(
                f"matched rule references verification_profile {profile_id!r} which is not "
                "defined in the policy's 'verification_profiles' mapping (dangling profile "
                "reference -- a policy integrity failure, not an Issue defect, Issue #2771 AC6)"
            )
        assertions = profile.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise PolicyLoadError(
                f"verification_profile {profile_id!r} declares no non-empty 'assertions' list "
                "(policy integrity failure, Issue #2771 AC6)"
            )
        seen_assertion_ids: set[str] = set()
        assertion_ids: list[str] = []
        for assertion in assertions:
            assertion_id = assertion.get("id") if isinstance(assertion, dict) else None
            if not assertion_id:
                raise PolicyLoadError(
                    f"verification_profile {profile_id!r} declares a malformed assertion entry "
                    "missing a non-empty 'id' (policy integrity failure, Issue #2771 AC6)"
                )
            if assertion_id in seen_assertion_ids:
                raise PolicyLoadError(
                    f"verification_profile {profile_id!r} declares duplicate assertion id "
                    f"{assertion_id!r} (policy integrity failure -- not an Issue defect, "
                    "Issue #2771 AC6)"
                )
            seen_assertion_ids.add(assertion_id)
            assertion_ids.append(assertion_id)

        if profile_id in hard_profile_ids:
            for assertion_id in assertion_ids:
                required.add((profile_id, assertion_id))

    return required


def _line_indent(line: str) -> int:
    """Count of leading whitespace characters (spaces/tabs) on ``line``."""
    return len(line) - len(line.lstrip(" \t"))


def _strip_unquoted_yaml_comment(text: str) -> str:
    """Strip a trailing unquoted YAML comment (``# ...``) from ``text``.

    Issue #2771 PR #2780 OWNER F1 review: a field key line's trailing
    explanatory comment (``runtime_assertion_bindings: # このACで両方を検証
    する``) must never be mistaken for that field's actual inline value --
    the previous implementation treated the raw, un-stripped remainder of
    the line as the inline value, so a comment-only suffix made a normal
    block-form binding disappear (parsed as a single truncated line instead
    of the field plus its nested block).

    Only a ``#`` that starts the string or is preceded by whitespace, and
    that is not inside a single- or double-quoted string, begins a comment
    (the same informal subset of the YAML comment rule PyYAML itself
    applies to plain scalars). This is intentionally a narrow, local
    heuristic scoped to this line-based field extractor -- not a general
    YAML tokenizer.
    """
    in_single = False
    in_double = False
    for index, ch in enumerate(text):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if index == 0 or text[index - 1] in (" ", "\t"):
                return text[:index]
    return text


def _extract_rva_yaml_field(rva_section_text: str, field_name: str) -> Optional[Any]:
    """Best-effort extraction of a single top-level RVA field's value.

    The ``## Runtime Verification Applicability`` section is not guaranteed
    to be a single well-formed YAML document as a whole -- existing Issue
    bodies mix bullet-list prose lines (``- decision: immediate``) with bare
    ``key: value`` lines, both inside and outside a ```` ```yaml ```` fence
    (e.g. ``- decision: immediate`` followed by ``applicable_acs: [AC1]`` at
    the same indentation is not one valid YAML document). To stay robust
    against this without inventing a new markup convention, this function
    locates only ``field_name``'s own line plus its nested block (lines
    indented strictly deeper than the field, blank lines included) and
    parses *that* isolated slice as its own tiny YAML document -- never the
    whole section. Returns ``None`` if the field is absent or its isolated
    slice fails to parse (never raises -- a missing/malformed field degrades
    to "absent", the same as every other RVA field check in this codebase).

    Issue #2771 PR #2780 OWNER F1 review fixed 3 false-negative patterns
    beyond the original "strictly deeper indent" design:

    - A trailing YAML comment on the field's own key line
      (``field_name: # comment``) is stripped before deciding whether an
      inline value is present (see ``_strip_unquoted_yaml_comment``).
    - A ``field_name:`` key followed by a block sequence at the *same*
      indentation (the shape PyYAML's own ``yaml.safe_dump()`` emits for a
      mapping value that is a list -- valid YAML; a sequence item does not
      need to be indented deeper than its mapping key) is now recognised as
      that field's nested block, provided the key line itself was matched
      in its plain (non-bullet) form. This same-indent continuation is
      deliberately NOT applied when the key itself was bullet-prefixed
      (``- field_name:``), because in that authoring style a *sibling*
      field at the same indentation is also written as a ``- other_field:``
      bullet -- applying the same-indent rule there would swallow the next,
      unrelated field into this one's value.
    - A ``field_name`` key that is itself written as a Markdown/YAML bullet
      item (``- field_name: value``), matching the existing authoring
      convention already used for other RVA fields such as ``- decision:``
      / ``- reason:``.
    """
    if not rva_section_text:
        return None
    lines = rva_section_text.splitlines()
    escaped_field = re.escape(field_name)
    plain_pattern = re.compile(rf"^([ \t]*){escaped_field}\s*:[ \t]*(.*)$")
    bullet_pattern = re.compile(rf"^([ \t]*)-[ \t]+{escaped_field}\s*:[ \t]*(.*)$")
    for index, line in enumerate(lines):
        is_bullet = False
        match = plain_pattern.match(line)
        if match is None:
            match = bullet_pattern.match(line)
            if match is None:
                continue
            is_bullet = True
        indent = len(match.group(1))
        inline_value = _strip_unquoted_yaml_comment(match.group(2)).strip()
        if inline_value:
            candidate = f"{field_name}: {inline_value}"
        else:
            # The key line itself is reconstructed fresh (never the raw
            # ``line``) so a bullet prefix (``- field_name:``) or a
            # comment-only suffix on the key line can never leak into the
            # isolated candidate parsed below -- only the nested nxt lines'
            # own (already comment-free by construction) indentation is
            # preserved verbatim.
            block_lines = [f"{field_name}:"]
            for nxt in lines[index + 1:]:
                if not nxt.strip():
                    block_lines.append(nxt)
                    continue
                nxt_indent = _line_indent(nxt)
                if nxt_indent > indent:
                    block_lines.append(nxt)
                    continue
                if (
                    not is_bullet
                    and nxt_indent == indent
                    and nxt.lstrip(" \t").startswith("- ")
                ):
                    block_lines.append(nxt)
                    continue
                break
            candidate = "\n".join(block_lines)
        try:
            parsed = yaml.safe_load(candidate)
        except yaml.YAMLError:
            return None
        if isinstance(parsed, dict) and field_name in parsed:
            return parsed[field_name]
        return None
    return None


_AC_TOKEN_RE = re.compile(r"^AC(\d+)$")


def extract_applicable_acs(rva_section_text: str) -> set[str]:
    """Digit-only AC numbers declared in the RVA section's ``applicable_acs``
    field (e.g. ``{"3", "5"}`` for ``applicable_acs: [AC3, AC5]``). Returns
    an empty set if the field is absent or not a list of ``AC<N>`` strings."""
    raw = _extract_rva_yaml_field(rva_section_text, "applicable_acs")
    if not isinstance(raw, list):
        return set()
    result: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            m = _AC_TOKEN_RE.match(item.strip())
            if m:
                result.add(m.group(1))
    return result


_AC_TASK_LIST_ITEM_RE = re.compile(r"^[ \t]*-[ \t]*\[[ xX]\][ \t]*AC(\d+)\b")


def extract_ac_numbers(ac_section_text: str) -> set[str]:
    """Digit-only AC numbers actually DECLARED as a task-list item's own AC
    label (e.g. ``- [ ] AC3: ...``) in the Acceptance Criteria section text.

    Issue #2771 PR #2780 OWNER F3 review: an earlier version of this
    function used a body-wide ``AC(\\d+)`` regex, which also matched
    ``AC<N>``-shaped substrings appearing in prose explaining a *previous*
    proposal (e.g. ``旧案のAC99は参考情報であり...``), a URL fragment, a
    filename, or a fenced code example -- none of which declare a real
    Acceptance Criterion. That made a binding's referential-validity check
    (``ac_not_found``) pass for a nonexistent AC as long as its number
    happened to appear anywhere in the section's text.

    This is the same known false-positive shape independently tracked for
    the ``review-issue`` C5 checker's own AC-number collection (Issue
    #1712) -- this fix is intentionally scoped ONLY to this function's own
    local AC-number extraction, not a fix to C5 itself and not a new
    general-purpose Markdown parser.

    Only the first ``AC<N>``-shaped token immediately following a
    task-list checkbox marker (``- [ ]`` / ``- [x]`` / ``- [X]``) on a
    non-fenced line counts as that item's own declared AC number -- any
    further ``AC<N>``-shaped text later in the same line (prose, examples)
    is ignored, and lines inside fenced code blocks are excluded entirely
    (a fenced example illustrating what an AC line looks like does not
    itself declare a real AC).
    """
    result: set[str] = set()
    in_fence = False
    for line in (ac_section_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _AC_TASK_LIST_ITEM_RE.match(line)
        if match:
            result.add(match.group(1))
    return result


_RUNTIME_VERIFICATION_TAG_RE = re.compile(r"<!--\s*runtime-verification:\s*true\s*-->")


def decision_level_runtime_verification_tag_consistent(
    declared_decision: Optional[str], ac_section_text: str
) -> bool:
    """Mirrors ``check_c11_decision_tag_consistency``'s existing decision-vs-
    tag pass/fail semantics (a *decision-level*, whole-Acceptance-Criteria-
    section aggregate: ``decision: immediate`` requires at least one
    ``<!-- runtime-verification: true -->`` tag somewhere in the section;
    ``not_applicable``/``deferred`` require none). Centralised here so the
    runtime assertion binding coverage gate reuses this exact existing tag
    mechanism (Issue #2771 AC5 point 3) instead of introducing a new
    per-assertion tagging convention."""
    has_rv_tag = bool(_RUNTIME_VERIFICATION_TAG_RE.search(ac_section_text or ""))
    if declared_decision == "immediate":
        return has_rv_tag
    if declared_decision in ("not_applicable", "deferred"):
        return not has_rv_tag
    return True


_RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS = frozenset({"profile", "assertion", "ac"})


def parse_runtime_assertion_bindings(
    rva_section_text: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Parse the canonical ``runtime_assertion_bindings`` wire format list.

    Canonical shape (Issue #2771 In Scope): a list of mappings, each with
    exactly the three keys ``profile`` / ``assertion`` / ``ac`` -- never a
    duplicated ``vc:``/command-text field (the binding only records *which*
    AC owns the assertion; the VC command text itself lives in the existing
    ``## Verification Commands`` section and is cross-checked separately by
    ``derive_required_runtime_assertions``'s caller via the existing C5 /
    ``parse_verification_commands_section`` parser).

    Returns ``(bindings, malformed_entry_descriptions)``. Each returned
    binding dict has ``profile`` / ``assertion`` (verbatim strings) and
    ``ac`` (digit-only, e.g. ``"3"`` for ``ac: AC3``). A raw
    ``runtime_assertion_bindings`` value that is present but not a list, or
    an individual entry that is not a mapping, declares an unexpected key,
    or is missing/malformed ``profile``/``assertion``/``ac``, is reported as
    a human-readable string in ``malformed_entry_descriptions`` rather than
    silently skipped (an Issue-side declaration defect, Issue #2771 AC3/AC4)
    -- never raised as a policy integrity failure, since a malformed
    *declaration* is always something the Issue author can fix.
    """
    raw = _extract_rva_yaml_field(rva_section_text, "runtime_assertion_bindings")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [
            "'runtime_assertion_bindings' is present but is not a list "
            f"(got {type(raw).__name__})"
        ]

    bindings: list[dict[str, str]] = []
    malformed: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            malformed.append(f"runtime_assertion_bindings[{index}] is not a mapping")
            continue
        extra_keys = set(entry.keys()) - _RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS
        if extra_keys:
            malformed.append(
                f"runtime_assertion_bindings[{index}] declares unexpected key(s) "
                f"{sorted(extra_keys)} (canonical shape is profile/assertion/ac only, Issue #2771)"
            )
            continue
        profile = entry.get("profile")
        assertion = entry.get("assertion")
        ac = entry.get("ac")
        if not isinstance(profile, str) or not profile.strip():
            malformed.append(f"runtime_assertion_bindings[{index}] is missing a non-empty 'profile'")
            continue
        if not isinstance(assertion, str) or not assertion.strip():
            malformed.append(f"runtime_assertion_bindings[{index}] is missing a non-empty 'assertion'")
            continue
        if not isinstance(ac, str) or not _AC_TOKEN_RE.match(ac.strip()):
            malformed.append(
                f"runtime_assertion_bindings[{index}] declares 'ac' as {ac!r}, expected 'AC<N>' form"
            )
            continue
        bindings.append(
            {
                "profile": profile.strip(),
                "assertion": assertion.strip(),
                "ac": _AC_TOKEN_RE.match(ac.strip()).group(1),
            }
        )
    return bindings, malformed


def evaluate_runtime_assertion_binding_coverage(
    allowed_path_entries: list[str],
    rva_section_text: str,
    ac_section_text: str,
    ac_vc_refs: set[str],
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """High-level verdict shared verbatim by both consumers (Issue #2771,
    mirrors ``evaluate_issue_risk_trigger``'s existing cross-consumer parity
    pattern for Issue #2290).

    This function performs STRUCTURAL COMPLETENESS checking only
    (structural_completeness_not_semantic_sufficiency): it verifies that
    every hard-required ``(verification_profile_id, assertion_id)`` pair is
    explicitly bound to a real, referentially-valid Acceptance Criterion via
    ``runtime_assertion_bindings``. It does not, and cannot, judge whether
    the bound VC actually *proves* the assertion's semantic postcondition
    (that remains existing semantic review's responsibility, Issue #2771
    Outcome / AC9 / AC10).

    ``ac_vc_refs`` is the caller-supplied, digit-only set of AC numbers that
    the ``## Verification Commands`` section references via a canonical
    ``# AC<N>`` comment (the same set already produced by the existing
    ``vc_contract_syntax.parse_verification_commands_section`` / C5 parser
    both consumers already import) -- passed in rather than re-parsed here
    so this module does not need a new cross-directory import dependency on
    top of its existing ``changed_file_matcher`` sibling import.

    Raises ``PolicyLoadError`` (propagated from
    ``derive_required_runtime_assertions``) for a policy integrity defect;
    callers MUST catch this separately from the returned ``verdict`` so an
    Issue-side declaration defect (``needs_fix``, returned normally) is
    never confused with a policy-side defect the Issue author cannot fix
    (Issue #2771 AC6).
    """
    policy_data = policy if policy is not None else load_policy()
    policy_evaluation = evaluate_allowed_paths(allowed_path_entries, policy=policy_data)
    required = derive_required_runtime_assertions(policy_evaluation["matched_rules"], policy_data)

    declared_decision: Optional[str] = None
    decision_match = re.search(r"decision:\s*(\S+)", rva_section_text or "")
    if decision_match:
        declared_decision = decision_match.group(1).strip()

    bindings, malformed_entries = parse_runtime_assertion_bindings(rva_section_text)

    ac_numbers = extract_ac_numbers(ac_section_text)
    applicable_acs = extract_applicable_acs(rva_section_text)
    tag_consistent = decision_level_runtime_verification_tag_consistent(
        declared_decision, ac_section_text
    )

    declared_key_counts: dict[tuple[str, str], int] = {}
    for binding in bindings:
        key = (binding["profile"], binding["assertion"])
        declared_key_counts[key] = declared_key_counts.get(key, 0) + 1
    declared_keys = set(declared_key_counts.keys())

    missing = sorted(required - declared_keys)
    unknown = sorted(declared_keys - required)
    duplicate = sorted(key for key, count in declared_key_counts.items() if count > 1)

    invalid_ac_bindings: list[dict[str, Any]] = []
    for binding in bindings:
        ac_digit = binding["ac"]
        reasons: list[str] = []
        if ac_digit not in ac_numbers:
            reasons.append("ac_not_found")
        if ac_digit not in applicable_acs:
            reasons.append("ac_not_in_applicable_acs")
        if not tag_consistent:
            reasons.append("runtime_verification_tag_inconsistent")
        if ac_digit not in ac_vc_refs:
            reasons.append("ac_missing_vc_reference")
        if reasons:
            invalid_ac_bindings.append(
                {
                    "profile": binding["profile"],
                    "assertion": binding["assertion"],
                    "ac": f"AC{ac_digit}",
                    "reasons": reasons,
                }
            )

    reasons_out: list[str] = []
    if missing:
        reasons_out.append(
            "missing runtime_assertion_bindings for hard-required (profile, assertion) pairs: "
            + ", ".join(f"{p}/{a}" for p, a in missing)
        )
    if unknown:
        reasons_out.append(
            "declared runtime_assertion_bindings reference (profile, assertion) pairs that are "
            "not hard-required: " + ", ".join(f"{p}/{a}" for p, a in unknown)
        )
    if duplicate:
        reasons_out.append(
            "duplicate runtime_assertion_bindings declared for the same (profile, assertion) key "
            "(1 key = 1 ac; declaring the same key more than once is invalid regardless of whether "
            "the bound ac matches): " + ", ".join(f"{p}/{a}" for p, a in duplicate)
        )
    reasons_out.extend(malformed_entries)
    for entry in invalid_ac_bindings:
        reasons_out.append(
            f"runtime_assertion_bindings entry {entry['profile']}/{entry['assertion']} -> "
            f"{entry['ac']} is invalid: {', '.join(entry['reasons'])}"
        )

    verdict = "needs_fix" if reasons_out else "approve"

    return {
        "schema": SCHEMA_RUNTIME_ASSERTION_BINDING_COVERAGE,
        "verdict": verdict,
        "required_assertions": sorted(f"{p}/{a}" for p, a in required),
        "declared_bindings": [
            {"profile": b["profile"], "assertion": b["assertion"], "ac": f"AC{b['ac']}"}
            for b in bindings
        ],
        "missing": [f"{p}/{a}" for p, a in missing],
        "unknown": [f"{p}/{a}" for p, a in unknown],
        "duplicate": [f"{p}/{a}" for p, a in duplicate],
        "malformed_binding_entries": malformed_entries,
        "invalid_ac_bindings": invalid_ac_bindings,
        "reasons": reasons_out,
    }
