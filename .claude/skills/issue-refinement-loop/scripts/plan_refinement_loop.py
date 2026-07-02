#!/usr/bin/env python3
"""
plan_refinement_loop.py

Read-only CLI that analyzes an Issue body, comments, and known_context
to produce a REFINEMENT_LOOP_PLAN_V1 JSON payload describing policy decisions
and evidence extraction.

Usage:
    python3 plan_refinement_loop.py < input.json

Input (stdin): REFINEMENT_LOOP_PLANNER_INPUT_V1 JSON
Output (stdout): REFINEMENT_LOOP_PLAN_V1 JSON
Logs (stderr): diagnostic messages

Exit codes:
    0 - success (may include fail_closed.required=true)
    2 - invalid input schema
    3 - internal error
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import yaml as _yaml_module
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

try:
    from scope_signal_delta import compute_scope_signal_delta
except ImportError:  # pragma: no cover - subprocess/CLI fallback path
    compute_scope_signal_delta = None


class ScopeSignalDeltaError(RuntimeError):
    """Raised when scope_signal_delta_input exists but cannot be consumed safely."""


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "refinement_loop_plan/v1"

# Confidence levels
CONFIDENCE_DETERMINISTIC = "deterministic"
CONFIDENCE_UNKNOWN = "unknown"

# Reason codes
INVESTIGATION_REASON_TARGET_PATHS_PRESENT = "target_paths_present"
INVESTIGATION_REASON_REPO_FACT_CLAIM = "repo_fact_claim_in_outcome_or_ac_or_vc"
INVESTIGATION_REASON_SAME_FILE_ROLLUP = "same_file_scope_rollup_conflict"
INVESTIGATION_REASON_REVIEWER_REQUESTED = "reviewer_requested_repo_fact_check"
INVESTIGATION_REASON_ANCHOR_COMMENT = "anchor_comment_requires_fact_check"
INVESTIGATION_REASON_NO_REPO_FACT = "no_repo_fact_claim"
INVESTIGATION_REASON_UNKNOWN_SCHEMA = "unknown_input_schema"

WEB_RESEARCH_REASON_EXTERNAL_SPEC = "critical_external_spec_claim"
WEB_RESEARCH_REASON_HUMAN_REQUESTED = "human_requested_web_verification"
WEB_RESEARCH_REASON_CLI_API_BEHAVIOR = "current_cli_api_auth_or_migration_behavior"
WEB_RESEARCH_REASON_NO_CLAIM = "no_critical_external_claim"
WEB_RESEARCH_REASON_UNKNOWN_SCHEMA = "unknown_input_schema"

SCOPE_SIGNAL_REASON_NEW_IN_SCOPE = "new_in_scope_area"
SCOPE_SIGNAL_REASON_NEW_PATH_LAYER = "new_allowed_path_layer"
SCOPE_SIGNAL_REASON_NEW_UNVERIFIABLE_AC = "new_unverifiable_ac"
SCOPE_SIGNAL_REASON_ANCHOR_REFRAME = "anchor_reframe_exclusion"
SCOPE_SIGNAL_REASON_NO_SIGNAL = "no_scope_signal"

# ---------------------------------------------------------------------------
# SCOPE_SIGNAL_GUARD_DECISION_V2 (#1090) -- escalation lane split
# ---------------------------------------------------------------------------

PATH_LAYER_RUNTIME = "runtime"
PATH_LAYER_DOCS = "docs"
PATH_LAYER_SKILL = "skill"
PATH_LAYER_HOOK = "hook"
PATH_LAYER_AGENT = "agent"
PATH_LAYER_TEST_FIXTURE = "test_fixture"
PATH_LAYER_UNKNOWN = "unknown"

SCOPE_ROUTE_PROCEED_WITH_NOTES = "proceed_with_notes"
SCOPE_ROUTE_HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"
SCOPE_ROUTE_SECURITY_RISK_GATE_REQUIRED = "security_risk_gate_required"
SCOPE_ROUTE_INVALID_SCOPE_DELTA_APPROVAL = "invalid_scope_delta_approval"
SCOPE_ROUTE_NOT_TRIGGERED = "not_triggered"

TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# #558 owns the security gate itself; this is a narrow deterministic
# fail-closed check applied regardless of scope delta approval.
SECURITY_SENSITIVE_PATH_PREFIXES = (
    ".claude/hooks/",
    ".github/workflows/",
)
SECURITY_SENSITIVE_TERMS = ("secret", "token", "permission", "credential")

SUGGESTED_CONTRACT_PATCH_TEMPLATE = (
    "OWNER/MEMBER/COLLABORATOR being a trusted author must post an "
    "ANCHOR_SCOPE_REFRAME (or `Scope Delta Approval` / "
    "`Allowed Paths Expansion Rationale`) comment on this issue. "
    "See references/scope-signal-guard.md."
)

FAIL_CLOSED_REASON_MALFORMED_CONTRACT = "malformed_machine_readable_contract"
FAIL_CLOSED_REASON_MISSING_SECTION = "missing_required_section"
FAIL_CLOSED_REASON_MISSING_PARENT_SECTION = "missing_required_parent_section"
FAIL_CLOSED_REASON_AMBIGUOUS_SIGNAL = "ambiguous_scope_signal"
FAIL_CLOSED_REASON_UNKNOWN_SCHEMA = "unknown_input_schema"
FAIL_CLOSED_REASON_INTERNAL_ERROR = "planner_internal_error"
FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE = "template_required_sections_unavailable"
FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND = "unknown_issue_kind"
FAIL_CLOSED_REASON_MISSING_CONTRACT_KEY = "missing_required_contract_key"
FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR = "contract_schema_parse_error"
FAIL_CLOSED_REASON_ISSUE_KIND_POLICY_LOAD_ERROR = "issue_kind_policy_load_error"
FAIL_CLOSED_REASON_CHECKER_INTERNAL_ERROR = "checker_internal_error"
FAIL_CLOSED_REASON_TEMPLATE_RESOLUTION_ERROR = "template_resolution_error"

# Override policy: which fail_closed reason codes can be overridden by human_decision_reframe
# AC7: missing_required_section and missing_required_contract_key only
# unknown_issue_kind, issue_kind_policy_load_error, contract_schema_parse_error,
# template_resolution_error, checker_internal_error are never overridable.
OVERRIDE_POLICY = {
    "allowed_reason_codes": [
        FAIL_CLOSED_REASON_MISSING_SECTION,
        FAIL_CLOSED_REASON_MISSING_CONTRACT_KEY,
    ],
    "never_override_reason_codes": [
        FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND,
        FAIL_CLOSED_REASON_ISSUE_KIND_POLICY_LOAD_ERROR,
        FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR,
        FAIL_CLOSED_REASON_TEMPLATE_RESOLUTION_ERROR,
        FAIL_CLOSED_REASON_CHECKER_INTERNAL_ERROR,
    ],
}

# Required machine-readable contract keys that must be present when a YAML contract exists
REQUIRED_CONTRACT_KEYS = [
    "contract_schema_version",
    "issue_kind",
]

# Standard required sections for implementation issues
STANDARD_REQUIRED_SECTIONS = [
    "Outcome",
    "Acceptance Criteria",
    "Verification Commands",
    "Allowed Paths",
]

# ---------------------------------------------------------------------------
# ISSUE_KIND_POLICY_V1 SSOT loader
# ---------------------------------------------------------------------------
# Canonical source: docs/dev/github-ops.md ## ISSUE_KIND_POLICY_V1
# Local ISSUE_KIND_ALLOWLIST definition is prohibited (SSOT single-source rule).
# Consumer MUST call _get_issue_kind_allowlist() to get the allowlist.

_ISSUE_KIND_POLICY_CACHE: "dict | None" = None


class IssueKindPolicyLoadError(RuntimeError):
    """Raised when ISSUE_KIND_POLICY_V1 cannot be loaded from SSOT.

    Fail-closed: callers must not silently substitute a hardcoded fallback.
    """


def _load_issue_kind_policy(repo_root: "Path | None" = None) -> dict:
    """Load ISSUE_KIND_POLICY_V1 from docs/dev/github-ops.md.

    Returns a dict with keys:
      - canonical_kinds: frozenset[str]
      - aliases: dict[str, str]
      - unknown_kind_policy: str  ("block")
      - unknown_kind_reason_code: str

    Raises IssueKindPolicyLoadError if the SSOT file is missing, the
    ISSUE_KIND_POLICY_V1 block cannot be found/parsed, or the yaml library
    is unavailable.  No silent fallback — callers must handle the error.
    """
    global _ISSUE_KIND_POLICY_CACHE
    if _ISSUE_KIND_POLICY_CACHE is not None:
        return _ISSUE_KIND_POLICY_CACHE

    if repo_root is None:
        repo_root = _find_repo_root()

    ssot_path = repo_root / "docs" / "dev" / "github-ops.md"
    if not ssot_path.exists():
        raise IssueKindPolicyLoadError(
            f"SSOT file not found: {ssot_path}. "
            "Cannot load ISSUE_KIND_POLICY_V1 — fail-closed."
        )

    if not _YAML_AVAILABLE:
        raise IssueKindPolicyLoadError(
            "PyYAML is not available; cannot parse ISSUE_KIND_POLICY_V1."
        )

    try:
        text = ssot_path.read_text(encoding="utf-8")
        # Extract the ISSUE_KIND_POLICY_V1 YAML block
        match = re.search(r"```yaml\s*\nISSUE_KIND_POLICY_V1:(.*?)```", text, re.DOTALL)
        if not match:
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 fenced YAML block not found in {ssot_path}. "
                "Ensure the block starts with ```yaml on a line followed by 'ISSUE_KIND_POLICY_V1:'."
            )

        yaml_content = "ISSUE_KIND_POLICY_V1:" + match.group(1)
        parsed = _yaml_module.safe_load(yaml_content)
        if not isinstance(parsed, dict) or "ISSUE_KIND_POLICY_V1" not in parsed:
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 YAML parse produced unexpected structure in {ssot_path}."
            )

        policy = parsed["ISSUE_KIND_POLICY_V1"]
        if not isinstance(policy, dict):
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 value is not a mapping in {ssot_path}."
            )

        canonical_kinds = frozenset(policy.get("canonical_kinds") or [])
        aliases_raw = policy.get("aliases") or {}
        aliases = {str(k): str(v) for k, v in aliases_raw.items()} if isinstance(aliases_raw, dict) else {}
        unknown_kind_policy = str(policy.get("unknown_kind_policy", "block"))
        unknown_kind_reason_code = str(policy.get("unknown_kind_reason_code", "unknown_issue_kind"))

        result: dict = {
            "canonical_kinds": canonical_kinds,
            "aliases": aliases,
            "unknown_kind_policy": unknown_kind_policy,
            "unknown_kind_reason_code": unknown_kind_reason_code,
        }
        _ISSUE_KIND_POLICY_CACHE = result
        return result
    except IssueKindPolicyLoadError:
        raise
    except Exception as exc:
        raise IssueKindPolicyLoadError(
            f"Unexpected error while loading ISSUE_KIND_POLICY_V1 from {ssot_path}: {exc}"
        ) from exc


def _get_issue_kind_allowlist() -> frozenset:
    """Return the canonical_kinds frozenset from SSOT (docs/dev/github-ops.md).

    Raises IssueKindPolicyLoadError if SSOT cannot be loaded.
    """
    return _load_issue_kind_policy()["canonical_kinds"]


def _normalize_issue_kind(kind: str) -> "str | None":
    """Normalize an issue_kind string against SSOT canonical_kinds and aliases.

    Returns:
      - The canonical kind if ``kind`` is already canonical.
      - The alias target if ``kind`` is in SSOT aliases (e.g. "design" → "research").
      - None if ``kind`` is unknown (not canonical and not an alias).
      - None if the SSOT cannot be loaded (IssueKindPolicyLoadError).

    Callers should treat None as FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND.
    """
    try:
        policy = _load_issue_kind_policy()
    except IssueKindPolicyLoadError:
        return None

    canonical_kinds = policy["canonical_kinds"]
    aliases = policy["aliases"]

    if kind in canonical_kinds:
        return kind
    if kind in aliases:
        target = aliases[kind]
        # Only return the alias target if it is itself canonical
        if target in canonical_kinds:
            return target
    return None


def _clear_issue_kind_policy_cache() -> None:
    """Clear the SSOT cache (for testing only)."""
    global _ISSUE_KIND_POLICY_CACHE
    _ISSUE_KIND_POLICY_CACHE = None


# ---------------------------------------------------------------------------
# Template load result
# ---------------------------------------------------------------------------


@dataclass
class TemplateLoadResult:
    """Result of loading required section labels from a template file.

    Blocker 2: distinguishes success (error=None) from failure (error=reason_code).
    An empty required_labels with error=None means the template genuinely has 0
    required sections — that is valid. An error means we cannot trust the result.
    """

    required_labels: list[str]
    error: Optional[str]  # None = success; str = fail_closed reason code


# ---------------------------------------------------------------------------
# Repository root resolution
# ---------------------------------------------------------------------------

def _find_repo_root() -> Path:
    """Find repository root by walking up to find .git directory."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume we are in .claude/skills/issue-refinement-loop/scripts/
    return Path(__file__).resolve().parent.parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Path extraction: matches `.claude/`, `docs/`, `scripts/`, `.github/workflows/`, `src/`, `tests/`
# Avoids matching paths inside fenced code blocks (handled separately)
PATH_PATTERN = re.compile(
    r"(?P<path>(?:"
    r"\.claude/[^\s\)`]+|"
    r"docs/[^\s\)`]+|"
    r"scripts/[^\s\)`]+|"
    r"\.github/workflows/[^\s\)`]+|"
    r"src/[^\s\)`]+|"
    r"tests?/[^\s\)`]+"
    r"))"
)

# Markers for unmaterialized child slots
UNMATERIALIZED_MARKER_PATTERN = re.compile(
    r"(?:（未起票）|（未起票）|\(未起票\)|unmaterialized|TBD)"
)

# Keywords indicating critical external claims (B7: more specific keywords to reduce false positives)
CRITICAL_EXTERNAL_KEYWORDS = {
    "official",
    "api",
    "cli",
    "auth",
    "migration",
}

# Keywords for human-requested web verification in comments
HUMAN_WEB_VERIFICATION_KEYWORDS = {
    "webで確認",
    "web確認",
    "verify externally",
    "external verification",
    "公式 docs",
    "official docs",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """Compute SHA256 of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    """Produce canonical JSON (sorted keys, compact, NaN-safe)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _extract_sections(text: str) -> dict[str, str]:
    """Extract markdown sections (e.g., ## Outcome) from text.

    AC4: fenced-code-aware — headings inside ``` or ~~~ fences are NOT treated
    as section headings. Only ## lines outside any fence are recognized.

    Per CommonMark spec, the opening fence marker is the run of backticks (or
    tildes) at the start of the line.  The closing fence must use the same
    character and have at least as many characters.  We capture the full
    opening-marker length so that a fence opened with `````markdown` (4 backticks)
    is NOT closed by a bare ` ``` ` (3 backticks) that appears inside it.
    """
    sections = {}
    current_section = None
    current_content = []
    in_fence = False
    fence_char = ""      # The backtick/tilde character used to open the fence
    fence_len = 0        # Minimum number of chars needed to close the fence

    for line in text.splitlines():
        # Detect fence open/close (``` or ~~~, optionally followed by language)
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                # Capture the full run of the fence character (could be 3+)
                fc = stripped[0]
                run_len = 0
                for ch in stripped:
                    if ch == fc:
                        run_len += 1
                    else:
                        break
                fence_char = fc
                fence_len = run_len
                in_fence = True
                if current_section is not None:
                    current_content.append(line)
                continue
        else:
            # Inside fence: look for a closing fence (same char, >= same length,
            # with no trailing non-whitespace chars other than the fence char itself).
            if stripped and all(c == fence_char for c in stripped) and len(stripped) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section is not None:
                    current_content.append(line)
                continue
            # Inside fence: pass line through without heading detection
            if current_section is not None:
                current_content.append(line)
            continue

        # Outside fence: detect headings
        if line.startswith("## "):
            if current_section is not None:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[3:].strip()
            current_content = []
        elif current_section is not None:
            current_content.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def _extract_section_lines(text: str, section_name: str) -> list[str]:
    """Extract lines from a specific section."""
    sections = _extract_sections(text)
    content = sections.get(section_name, "")
    return content.splitlines() if content else []


def _remove_fenced_code(text: str) -> str:
    """Remove fenced code blocks from text."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"~~~[\s\S]*?~~~", "", text)
    return text


def _extract_paths_from_text(text: str, exclude_fenced: bool = True) -> frozenset[str]:
    """Extract file paths from text, optionally excluding fenced code blocks."""
    if exclude_fenced:
        text = _remove_fenced_code(text)

    paths = []
    for match in PATH_PATTERN.finditer(text):
        path = match.group("path").rstrip("/").replace("\\", "/")
        # Clean up any trailing pipe or other markdown chars
        path = path.rstrip("|").strip()
        if path:
            paths.append(path)

    return frozenset(paths)


def _extract_paths_from_outcome_ac_vc(issue_body: str) -> frozenset[str]:
    """Extract target_paths from Outcome, InScope, AC, VC sections."""
    sections = _extract_sections(issue_body)
    combined_text = ""

    for section_name in ["Outcome", "In Scope", "Acceptance Criteria", "Verification Commands"]:
        combined_text += sections.get(section_name, "") + "\n"

    return _extract_paths_from_text(combined_text, exclude_fenced=True)


def _extract_repo_claims(issue_body: str) -> list[str]:
    """Extract claims about repo facts (commands, skills, schemas, paths)."""
    claims = []
    sections = _extract_sections(issue_body)

    # Include Allowed Paths section as per B6
    for section_name in ["Outcome", "In Scope", "Acceptance Criteria", "Verification Commands", "Allowed Paths"]:
        content = sections.get(section_name, "")
        # B6: Also exclude fenced code for repo_claims
        content = _remove_fenced_code(content)

        for line in content.splitlines():
            # Only extract lines that mention concrete repo elements (paths, scripts)
            # NOT generic section headers like "## Verification Commands"
            if any(
                keyword in line.lower()
                for keyword in [
                    ".claude/",
                    "src/",
                    "docs/",
                    "tests/",
                    "scripts/",
                    ".github/",
                    "$ ",  # Shell commands
                ]
            ):
                stripped = line.strip()
                if stripped and not stripped.startswith("# "):  # Exclude headers
                    claims.append(stripped)

    return claims


def _extract_critical_external_claims(
    issue_body: str, comments: Optional[list[dict[str, Any]]] = None
) -> list[dict[str, Any]]:
    """Extract critical external claims from issue body and comments."""
    claims = []
    sections = _extract_sections(issue_body)

    # Check each section for external claims in issue body
    for section_name in ["Outcome", "In Scope", "Acceptance Criteria", "Verification Commands", "Out of Scope"]:
        content = sections.get(section_name, "")
        for line in content.splitlines():
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in CRITICAL_EXTERNAL_KEYWORDS):
                claim = line.strip()
                if claim:
                    claims.append(
                        {
                            "claim": claim,
                            "affects": _infer_affects_section(section_name),
                            "source_hint": None,
                        }
                    )

    # B4: Check comments for human-requested web verification
    if comments and isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict):
                comment_body = comment.get("body", "")
                comment_id = comment.get("id", comment.get("comment_id"))

                for keyword in HUMAN_WEB_VERIFICATION_KEYWORDS:
                    if keyword in comment_body.lower():
                        # Found human request for web verification in comments
                        claim_text = comment_body[:100].strip() if comment_body else "Human requested web verification"
                        claims.append(
                            {
                                "claim": claim_text,
                                "affects": "VC",
                                "source_hint": f"comment_{comment_id}" if comment_id else "comment",
                            }
                        )
                        break  # Only add once per comment

    # B7: Stable sort and dedupe by claim text
    claims_by_text = {}
    for claim in claims:
        key = claim["claim"]
        if key not in claims_by_text:
            claims_by_text[key] = claim

    # Return sorted deduped claims
    return sorted(claims_by_text.values(), key=lambda c: c["claim"])


def _infer_affects_section(section_name: str) -> str:
    """Map section name to 'affects' enum value."""
    mapping = {
        "Outcome": "Outcome",
        "In Scope": "InScope",
        "Acceptance Criteria": "AC",
        "Verification Commands": "VC",
        "Out of Scope": "StopCondition",
    }
    return mapping.get(section_name, "AC")


def _extract_unmaterialized_slots(issue_body: str) -> list[dict[str, Any]]:
    """Extract unmaterialized child slots from issue body."""
    slots = []
    lines = issue_body.splitlines()

    for line_no, line in enumerate(lines, start=1):
        if UNMATERIALIZED_MARKER_PATTERN.search(line):
            # Extract title hint from line
            title_hint = re.sub(
                r"(?:（未起票）|（未起票）|\(未起票\)|unmaterialized|TBD)",
                "",
                line,
            ).strip()

            # Determine marker type
            if "未起票" in line:
                marker = "未起票"
            elif "unmaterialized" in line:
                marker = "unmaterialized"
            else:
                marker = "TBD"

            slots.append(
                {
                    "child_title_hint": title_hint,
                    "marker": marker,
                    "body_line": line_no,
                }
            )

    return slots


def _extract_follow_up_candidates(issue_body: str) -> list[dict[str, Any]]:
    """Extract follow-up issue candidates from Out of Scope or Stop Conditions."""
    candidates = []
    sections = _extract_sections(issue_body)

    for section_name in ["Out of Scope", "Stop Conditions"]:
        content = sections.get(section_name, "")
        lines = content.splitlines()

        for line_no, line in enumerate(lines, start=1):
            if "follow-up" in line.lower() or "別 issue" in line.lower():
                summary = line.strip()
                if summary:
                    # Create dedupe_key as first 16 chars of sha256(summary)
                    dedupe_key = _sha256(summary)[:16]
                    candidates.append(
                        {
                            "dedupe_key": dedupe_key,
                            "summary": summary,
                            "source_evidence": {
                                "source": "issue_body",
                                "source_ref": None,
                                "start_line": line_no,
                                "end_line": line_no,
                                "text_sha256": _sha256(summary),
                            },
                        }
                    )

    # Dedupe by dedupe_key
    seen_keys = set()
    deduped = []
    for candidate in candidates:
        key = candidate["dedupe_key"]
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(candidate)

    return deduped


def _is_anchor_reframe_context(known_context: dict | None) -> bool:
    """Check if known_context indicates anchor reframe exclusion."""
    if not known_context:
        return False
    has_scope_signal_delta_input = "scope_signal_delta_input" in known_context
    scope_delta_decision = known_context.get("scope_delta_decision")
    if isinstance(scope_delta_decision, dict):
        if (
            scope_delta_decision.get("status") == "approved_by_trusted_anchor"
            and scope_delta_decision.get("implementation_go") is False
            and scope_delta_decision.get("required_rerun")
        ):
            return True
    if has_scope_signal_delta_input:
        return False
    classification = known_context.get("classification")
    if classification in ("feedback_update_required", "reframe_in_place"):
        return True
    if known_context.get("anchor_comment_url") and known_context.get("anchor_reframe", False):
        return True
    return False


def _delta_projection_to_evidence_spans(delta_result: dict[str, Any]) -> list[dict[str, Any]]:
    projection = delta_result.get("legacy_scope_signal_guard", {})
    source_refs = (delta_result.get("inputs") or {}).get("source_refs") or {}
    evidence_spans = []
    for line in projection.get("triggering_lines", []):
        body_version = line["body_version"]
        source_ref = source_refs.get(body_version)
        evidence_spans.append(
            {
                "source": f"scope_signal_delta_{body_version}_body",
                "source_ref": source_ref,
                "start_line": line["start_line"],
                "end_line": line["end_line"],
                "text_sha256": line["text_sha256"].removeprefix("sha256:"),
            }
        )
    return evidence_spans


def _detect_scope_signals(issue_body: str, known_context: dict | None) -> tuple[bool, str, list]:
    """
    Detect scope signals (new_in_scope_area, new_allowed_path_layer, new_unverifiable_ac).

    Returns (triggered, reason_code, evidence_spans)

    Precedence: new_unverifiable_ac > new_allowed_path_layer > new_in_scope_area > none
    """
    if known_context and "scope_signal_delta_input" in known_context:
        if compute_scope_signal_delta is None:
            raise ScopeSignalDeltaError("scope_signal_delta helper is unavailable")
        if not isinstance(known_context.get("scope_signal_delta_input"), dict):
            raise ScopeSignalDeltaError("scope_signal_delta_input must be an object")
        try:
            delta_result = compute_scope_signal_delta(known_context["scope_signal_delta_input"])
            projection = delta_result.get("legacy_scope_signal_guard", {})
            evidence_spans = _delta_projection_to_evidence_spans(delta_result)
            if projection.get("triggered"):
                if _is_anchor_reframe_context(known_context):
                    return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
                return (
                    True,
                    projection.get("reason_code", SCOPE_SIGNAL_REASON_NO_SIGNAL),
                    evidence_spans,
                )
            return (False, SCOPE_SIGNAL_REASON_NO_SIGNAL, [])
        except Exception as exc:
            raise ScopeSignalDeltaError(f"scope_signal_delta_input is invalid: {exc}") from exc

    evidence_spans = []
    sections = _extract_sections(issue_body)

    in_scope = sections.get("In Scope", "")
    allowed_paths = sections.get("Allowed Paths", "")
    acceptance_criteria = sections.get("Acceptance Criteria", "")

    # Subjective keywords indicating unverifiable AC
    subjective_keywords = [
        "適切に", "品質を改善", "安定する", "改善する", "最適化", "高品質に",
        "improve", "enhance", "optimize", "stabilize", "appropriately"
    ]

    # 1. Check new_unverifiable_ac (highest precedence)
    for line_num, line in enumerate(acceptance_criteria.splitlines(), start=1):
        if line.lstrip().startswith("- [ ]") or line.lstrip().startswith("- [x]"):
            if any(kw in line for kw in subjective_keywords):
                evidence_spans.append({
                    "source": "issue_body",
                    "source_ref": None,
                    "start_line": line_num,
                    "end_line": line_num,
                    "text_sha256": _sha256(line),
                })
                # Check exclusion
                excluded = _is_anchor_reframe_context(known_context)
                if excluded:
                    return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
                return (True, SCOPE_SIGNAL_REASON_NEW_UNVERIFIABLE_AC, evidence_spans)

    # 2. Check new_allowed_path_layer
    top_level_prefixes = set()
    for line in allowed_paths.splitlines():
        match = re.search(r'`([^/`]+)/', line)
        if match:
            top_level_prefixes.add(match.group(1))

    if len(top_level_prefixes) >= 2:
        # Find evidence line
        for line_num, line in enumerate(allowed_paths.splitlines(), start=1):
            if any(p in line for p in top_level_prefixes):
                evidence_spans.append({
                    "source": "issue_body",
                    "source_ref": None,
                    "start_line": line_num,
                    "end_line": line_num,
                    "text_sha256": _sha256(line),
                })
                break
        excluded = _is_anchor_reframe_context(known_context)
        if excluded:
            return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
        return (True, SCOPE_SIGNAL_REASON_NEW_PATH_LAYER, evidence_spans)

    # 3. Check new_in_scope_area
    layer_prefixes_in_scope = set()
    for line in in_scope.splitlines():
        for prefix in [".claude/", "docs/", "src/", "scripts/", "tests/", ".github/"]:
            if prefix in line:
                layer_prefixes_in_scope.add(prefix)

    if len(layer_prefixes_in_scope) >= 2:
        # Find evidence line
        for line_num, line in enumerate(in_scope.splitlines(), start=1):
            if any(p in line for p in layer_prefixes_in_scope):
                evidence_spans.append({
                    "source": "issue_body",
                    "source_ref": None,
                    "start_line": line_num,
                    "end_line": line_num,
                    "text_sha256": _sha256(line),
                })
                break
        excluded = _is_anchor_reframe_context(known_context)
        if excluded:
            return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
        return (True, SCOPE_SIGNAL_REASON_NEW_IN_SCOPE, evidence_spans)

    return (False, SCOPE_SIGNAL_REASON_NO_SIGNAL, [])


def _classify_path_layer(path: str) -> str:
    """AC1: classify an Allowed Path entry into a coarse scope_context.path_layer.

    Precedence matters: more specific prefixes (.claude/hooks, .claude/agents,
    .claude/skills) are checked before the generic tests/fixtures bucket.
    """
    normalized = (path or "").strip().replace("\\", "/").rstrip("/")
    if normalized.startswith(".claude/hooks/") or normalized == ".claude/hooks":
        return PATH_LAYER_HOOK
    if normalized.startswith(".claude/agents/") or normalized == ".claude/agents":
        return PATH_LAYER_AGENT
    if normalized.startswith(".claude/skills/") or normalized == ".claude/skills":
        return PATH_LAYER_SKILL
    if normalized.startswith("docs/"):
        return PATH_LAYER_DOCS
    if normalized.startswith("src/"):
        return PATH_LAYER_RUNTIME
    if (
        normalized.startswith("tests/")
        or normalized.startswith("fixtures/")
        or "/tests/" in normalized
        or "/fixtures/" in normalized
    ):
        return PATH_LAYER_TEST_FIXTURE
    return PATH_LAYER_UNKNOWN


def _is_security_sensitive_scope_delta(added_paths: list[str], rationale_text: "str | None") -> bool:
    """AC13: deterministic security-sensitive gate, not overridable by approval."""
    for path in added_paths or []:
        normalized = (path or "").strip()
        for prefix in SECURITY_SENSITIVE_PATH_PREFIXES:
            if normalized.startswith(prefix):
                return True
        lower_path = normalized.lower()
        if any(term in lower_path for term in SECURITY_SENSITIVE_TERMS):
            return True
    if rationale_text:
        lower_text = rationale_text.lower()
        if any(term in lower_text for term in SECURITY_SENSITIVE_TERMS):
            return True
    return False


def _extract_scope_delta_approval_evidence(known_context: "dict | None") -> "dict | None":
    """AC10: read normalized (non-raw) approval evidence from known_context.

    The evidence dict is produced upstream (orchestrator/checker) from the
    ANCHOR_SCOPE_REFRAME comment; this function never parses raw comment body.
    """
    if not known_context:
        return None
    evidence = known_context.get("scope_delta_approval_evidence")
    if not isinstance(evidence, dict):
        return None
    return evidence


def _validate_scope_delta_approval(evidence: "dict | None", current_issue_number: "int | None") -> dict:
    """AC2/AC8/AC9: validate scope delta approval evidence, fail-closed.

    Returns a dict with: present, valid, status, missing_approval_field,
    suggested_contract_patch, comment_id, comment_url, body_sha256,
    author_association, created_at, issue_url.
    """
    base = {
        "present": False,
        "valid": False,
        "status": "missing",
        "missing_approval_field": True,
        "suggested_contract_patch": SUGGESTED_CONTRACT_PATCH_TEMPLATE,
        "comment_id": None,
        "comment_url": None,
        "body_sha256": None,
        "author_association": None,
        "created_at": None,
        "issue_url": None,
    }
    if evidence is None:
        return base

    base["present"] = True
    base["comment_id"] = evidence.get("comment_id")
    base["comment_url"] = evidence.get("comment_url")
    base["body_sha256"] = evidence.get("body_sha256")
    base["author_association"] = evidence.get("author_association")
    base["created_at"] = evidence.get("created_at")
    base["issue_url"] = evidence.get("issue_url")

    marker_present = bool(evidence.get("marker_present"))
    if not marker_present:
        base["status"] = "missing_marker"
        base["missing_approval_field"] = True
        return base

    # AC8: approval only valid for the current issue's own comment URL.
    target_issue_number = evidence.get("target_issue_number")
    if current_issue_number is not None and target_issue_number != current_issue_number:
        base["status"] = "invalid_scope_delta_approval"
        base["missing_approval_field"] = False
        base["suggested_contract_patch"] = None
        return base

    # AC9: only OWNER/MEMBER/COLLABORATOR are trusted authors.
    author_association = evidence.get("author_association")
    if author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
        base["status"] = "invalid_scope_delta_approval"
        base["missing_approval_field"] = False
        base["suggested_contract_patch"] = None
        return base

    base["valid"] = True
    base["status"] = "approved"
    base["missing_approval_field"] = False
    base["suggested_contract_patch"] = None
    return base


def _decide_scope_signal_route(
    triggered: bool,
    security_sensitive: bool,
    approval: dict,
) -> str:
    """AC2/AC3/AC4/AC8/AC9/AC13: decide the escalation lane.

    status="missing" (no evidence at all) and status="missing_marker"
    (evidence present but no ANCHOR_SCOPE_REFRAME/Scope Delta Approval/
    Allowed Paths Expansion Rationale marker) both mean "no reframe" (AC3)
    -> human_judgment_required. status="invalid_scope_delta_approval"
    means a reframe was attempted but failed the target-issue (AC8) or
    trusted-author (AC9) check -> invalid_scope_delta_approval.
    """
    if not triggered:
        return SCOPE_ROUTE_NOT_TRIGGERED
    if security_sensitive:
        # AC13: security-sensitive fail-closed gate is never overridden by approval.
        return SCOPE_ROUTE_SECURITY_RISK_GATE_REQUIRED
    if approval["status"] in ("missing", "missing_marker"):
        # AC3: no reframe present at all.
        return SCOPE_ROUTE_HUMAN_JUDGMENT_REQUIRED
    if approval["status"] == "invalid_scope_delta_approval":
        # AC8/AC9: reframe present but invalid (wrong issue / untrusted author).
        return SCOPE_ROUTE_INVALID_SCOPE_DELTA_APPROVAL
    # AC2/AC4/AC12: valid trusted anchor approval -- proceed with contract-review rerun required.
    return SCOPE_ROUTE_PROCEED_WITH_NOTES


def _build_scope_signal_guard_decision_v2(
    scope_signal_triggered: bool,
    scope_signal_reason: str,
    added_paths: list[str],
    known_context: "dict | None",
    issue_number: "int | None",
) -> dict:
    """AC1/AC10: build SCOPE_SIGNAL_GUARD_DECISION_V2 artifact."""
    path_layers = sorted({_classify_path_layer(p) for p in (added_paths or [])})
    evidence = _extract_scope_delta_approval_evidence(known_context)
    approval = _validate_scope_delta_approval(evidence, issue_number)
    rationale_text = evidence.get("rationale") if evidence else None
    security_sensitive = _is_security_sensitive_scope_delta(added_paths, rationale_text)
    route = _decide_scope_signal_route(scope_signal_triggered, security_sensitive, approval)

    return {
        "schema_version": "SCOPE_SIGNAL_GUARD_DECISION_V2",
        "raw_signal": {
            "triggered": scope_signal_triggered,
            "reason_code": scope_signal_reason,
        },
        "scope_context": {
            "path_layer": path_layers,
        },
        "scope_delta_approval": {
            "present": approval["present"],
            "valid": approval["valid"],
            "status": approval["status"],
            "missing_approval_field": approval["missing_approval_field"],
            "suggested_contract_patch": approval["suggested_contract_patch"],
            "comment_id": approval["comment_id"],
            "comment_url": approval["comment_url"],
            "body_sha256": approval["body_sha256"],
            "author_association": approval["author_association"],
            "created_at": approval["created_at"],
            "issue_url": approval["issue_url"],
        },
        "security_sensitive": security_sensitive,
        "route": route,
    }


def _check_malformed_contract(issue_body: str) -> bool:
    """Check if machine-readable contract is malformed (has YAML but missing required fields)."""
    # If there's no YAML block, it's not malformed - just missing
    if "```yaml" not in issue_body:
        return False

    # If there's a YAML block, check it has required fields
    yaml_match = re.search(r"```yaml\n(.*?)\n```", issue_body, re.DOTALL)
    if not yaml_match:
        return False

    yaml_content = yaml_match.group(1)
    # If YAML block exists but is missing contract_schema_version, it's malformed
    if "contract_schema_version:" not in yaml_content:
        return True

    return False


def _check_missing_outcome(issue_body: str) -> bool:
    """Check if Outcome section is missing (fence-aware heading detection)."""
    sections = _extract_sections(issue_body)
    return "Outcome" not in sections


def _check_missing_ac(issue_body: str) -> bool:
    """Check if Acceptance Criteria section is missing (fence-aware heading detection)."""
    sections = _extract_sections(issue_body)
    return "Acceptance Criteria" not in sections


def _check_missing_vc(issue_body: str) -> bool:
    """Check if Verification Commands section is missing (fence-aware heading detection)."""
    sections = _extract_sections(issue_body)
    return "Verification Commands" not in sections


# Heading pattern for extracting headings (AC4: fenced-code-aware)
HEADING_RE = re.compile(r"^[ \t]{0,3}##[ \t]+(?P<title>.+?)[ \t#]*$", re.MULTILINE)


def _extract_machine_contract(issue_body: str) -> dict | None:
    """
    Extract Machine-Readable Contract from issue body.

    Finds the fenced YAML block that is the value of the
    'Machine-Readable Contract' section and parses it with yaml.safe_load.

    Returns None if:
    - No Machine-Readable Contract section found
    - No YAML fenced block found in that section
    - yaml.safe_load fails
    - Result is not a dict (AC3: must be dict)
    - yaml module is unavailable

    AC4: Only parses the YAML in the Machine-Readable Contract section,
    not any arbitrary fenced code block.
    """
    if not _YAML_AVAILABLE:
        return None

    sections = _extract_sections(issue_body)
    contract_section = sections.get("Machine-Readable Contract", "")
    if not contract_section:
        return None

    # Extract the first fenced yaml block from the contract section
    yaml_match = re.search(r"```yaml\n([\s\S]*?)\n```", contract_section)
    if not yaml_match:
        return None

    yaml_content = yaml_match.group(1)
    try:
        parsed = _yaml_module.safe_load(yaml_content)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def resolve_issue_template(issue_kind: str, repo_root: Path) -> Path | None:
    """
    Resolve the issue template file for the given issue_kind.

    Blocker 3: issue_kind is validated against ISSUE_KIND_ALLOWLIST before
    being used in path construction. Unknown values return None (caller must
    handle as unknown_issue_kind).

    Returns the Path to .github/ISSUE_TEMPLATE/<issue_kind>.yml,
    or None if the file does not exist or issue_kind is not in the allowlist.
    """
    try:
        allowlist = _get_issue_kind_allowlist()
    except IssueKindPolicyLoadError:
        return None
    if issue_kind not in allowlist:
        return None

    template_dir = (repo_root / ".github" / "ISSUE_TEMPLATE").resolve()
    template_path = (template_dir / f"{issue_kind}.yml").resolve()

    # Path traversal guard: resolved template must be inside template_dir
    try:
        template_path.relative_to(template_dir)
    except ValueError:
        return None

    if template_path.exists():
        return template_path
    return None


def load_required_section_labels(template_path: Path) -> TemplateLoadResult:
    """
    Load required section labels from an issue template YAML file.

    Returns TemplateLoadResult with:
    - required_labels: list of label strings where validations.required == True
    - error: None on success; FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE on failure

    Blocker 2: failure modes are distinguished from "zero required sections".
    - yaml unavailable → error=template_required_sections_unavailable
    - file missing / unreadable → error=template_required_sections_unavailable
    - parse error → error=template_required_sections_unavailable
    - body not a list → error=template_required_sections_unavailable
    - template exists with 0 required items → error=None, required_labels=[]

    AC5: Must not hardcode section names — derive from template.
    AC6: When template changes, detection changes without script modification.
    """
    if not _YAML_AVAILABLE:
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    try:
        with open(template_path, encoding="utf-8") as f:
            data = _yaml_module.safe_load(f)
    except Exception:
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    if not isinstance(data, dict):
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    body = data.get("body", [])
    if not isinstance(body, list):
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    required_labels = []
    for item in body:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            continue
        label = attrs.get("label")
        validations = item.get("validations", {})
        if not isinstance(validations, dict):
            continue
        if label and validations.get("required") is True:
            required_labels.append(label)

    return TemplateLoadResult(required_labels=required_labels, error=None)


def _check_missing_sections_from_template(
    issue_body: str,
    required_labels: list[str],
) -> list[str]:
    """
    Check which required sections (from template labels) are missing in issue body.

    Blocker 4: Uses the heading map from _extract_sections() for exact-match
    detection instead of substring search. This prevents false-positives where
    a label name appears in the body text (not as a ## heading).

    Returns list of missing section labels.
    Skips 'Machine-Readable Contract' label (it's checked separately).
    """
    # Build a set of known heading titles from the body (fence-aware)
    sections = _extract_sections(issue_body)
    present_headings = set(sections.keys())

    missing = []
    for label in required_labels:
        # Machine-Readable Contract is validated separately
        if label == "Machine-Readable Contract":
            continue
        # Exact-match against heading map (Blocker 4: no partial match)
        if label not in present_headings:
            missing.append(label)
    return missing


def _validate_input_schema(data: Any) -> bool:
    """Validate input matches REFINEMENT_LOOP_PLANNER_INPUT_V1 schema."""
    if not isinstance(data, dict):
        return False

    required_fields = ["schema_version", "issue"]
    for field in required_fields:
        if field not in data:
            return False

    if data.get("schema_version") != "refinement_loop_planner_input/v1":
        return False

    issue = data.get("issue", {})
    issue_required = ["number", "title", "body", "labels"]
    for field in issue_required:
        if field not in issue:
            return False

    return True


def _create_evidence_span(
    source: str, text: str, start_line: int, end_line: int, source_ref: Optional[str] = None
) -> dict[str, Any]:
    """Create an EvidenceSpan object."""
    return {
        "source": source,
        "source_ref": source_ref,
        "start_line": start_line,
        "end_line": end_line,
        "text_sha256": _sha256(text),
    }


def _stable_sort_dedupe(items: list[str]) -> list[str]:
    """Sort items stably and dedupe."""
    return sorted(set(items))


def _extract_missing_contract_keys(issue_body: str) -> list[str]:
    """
    Extract missing required contract keys from the Machine-Readable Contract.

    Returns a list of required keys that are absent from the parsed contract dict.
    If the Machine-Readable Contract section exists but cannot be parsed, returns
    all required keys so downstream rewrite constraints can flag contract parse gaps.
    If the section is absent, returns an empty list and leaves section detection
    to a separate caller path.
    """
    if not _YAML_AVAILABLE:
        return []

    sections = _extract_sections(issue_body)
    # Only check for missing keys if a Machine-Readable Contract section exists
    if "Machine-Readable Contract" not in sections:
        return []

    machine_contract = _extract_machine_contract(issue_body)
    if machine_contract is None:
        # Contract section exists but YAML couldn't be parsed — schema parse error
        # Return all required keys as missing so the caller knows what to add
        return list(REQUIRED_CONTRACT_KEYS)

    missing = []
    for key in REQUIRED_CONTRACT_KEYS:
        if key not in machine_contract or machine_contract[key] is None:
            missing.append(key)
    return missing



def _check_contract_parse_error(issue_body: str) -> "str | None":
    """
    AC2: Check whether the Machine-Readable Contract section has a YAML parse error.

    Precondition: the 'Machine-Readable Contract' section MUST exist in the body
    (caller is responsible for checking this first).

    Returns:
      - FAIL_CLOSED_REASON_MALFORMED_CONTRACT if no YAML block found in section
      - FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR if YAML is present but cannot
        be parsed OR the parsed result is not a dict
      - None if YAML is parseable and returns a dict (keys may still be missing)
    """
    if not _YAML_AVAILABLE:
        return FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR

    sections = _extract_sections(issue_body)
    contract_section = sections.get("Machine-Readable Contract", "")

    # No YAML block at all → malformed
    yaml_match = re.search(r"```yaml\n([\s\S]*?)\n```", contract_section)
    if not yaml_match:
        return FAIL_CLOSED_REASON_MALFORMED_CONTRACT

    yaml_content = yaml_match.group(1)
    try:
        parsed = _yaml_module.safe_load(yaml_content)
    except Exception:
        return FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR

    if not isinstance(parsed, dict):
        return FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR

    return None


def _strict_json_loads(text: str) -> object:
    """
    AC5: Strict JSON loading that rejects NaN / Infinity / -Infinity.

    Python's json.loads accepts NaN and Infinity by default (parse_constant).
    This function uses a parse_constant hook to reject them explicitly.
    """

    def _reject_nan(value: str) -> float:
        raise ValueError(
            f"Strict JSON reject: NaN/Infinity/−Infinity values are not allowed. "
            f"Got: {value!r}"
        )

    # Python 3.8+ json.loads does NOT expose parse_constant in a portable way.
    # The most portable approach: decode then verify no float nan/inf survived.
    result = json.loads(text)
    _check_no_nan(result)
    return result


def _check_no_nan(obj: object) -> None:
    """AC5: Recursively reject NaN/Infinity in a decoded JSON structure."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError(
                f"Strict JSON reject: NaN/Infinity value encountered: {obj!r}"
            )
    elif isinstance(obj, dict):
        for v in obj.values():
            _check_no_nan(v)
    elif isinstance(obj, list):
        for item in obj:
            _check_no_nan(item)


def _build_fail_closed_rewrite_constraints(
    fail_closed_reasons: list[str],
    missing_sections: list[str],
    missing_contract_keys: list[str],
) -> dict[str, Any]:
    """
    Build FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 payload.

    AC1/AC2/AC8: Returned when fail_closed is triggered, to guide issue-author
    rewrite toward deterministic repair instead of freeform editing.
    """
    # Determine which reasons are overridable (AC7)
    overridable_reasons = [
        r for r in fail_closed_reasons
        if r in OVERRIDE_POLICY["allowed_reason_codes"]
    ]
    non_overridable_reasons = [
        r for r in fail_closed_reasons
        if r in OVERRIDE_POLICY["never_override_reason_codes"]
    ]

    return {
        "schema_version": "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1",
        "required_sections": missing_sections,
        "required_contract_keys": missing_contract_keys,
        "rewrite_constraints": {
            "must_add_sections": missing_sections,
            "must_add_contract_keys": missing_contract_keys,
            "freeform_rewrite_forbidden": True,
        },
        "override_policy": {
            "allowed_reason_codes": OVERRIDE_POLICY["allowed_reason_codes"],
            "never_override_reason_codes": OVERRIDE_POLICY["never_override_reason_codes"],
            "overridable_in_current_result": overridable_reasons,
            "non_overridable_in_current_result": non_overridable_reasons,
        },
        "max_rewrite_attempts": 2,
        "no_progress_route": "human_judgment_required",
    }


def plan_refinement_loop(input_data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """
    Main planner function.

    Returns (REFINEMENT_LOOP_PLAN_V1 dict, exit_code).
    exit_code: 0 = success, 2 = invalid input, 3 = internal error.

    B2: Supports optional 'now' parameter for deterministic generated_at timestamp.
    """
    # Validate input schema
    if not _validate_input_schema(input_data):
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": None,
                "issue_body_sha256": None,
                "comments_sha256": None,
                "known_context_sha256": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {},
            "fail_closed": {
                "required": True,
                "reason_codes": [FAIL_CLOSED_REASON_UNKNOWN_SCHEMA],
                "human_message": "Invalid input schema: missing required fields",
            },
        }
        return fail_closed_plan, 2

    try:
        issue = input_data.get("issue", {})
        comments = input_data.get("comments")
        known_context = input_data.get("known_context")

        issue_number = issue.get("number")
        issue_body = issue.get("body", "")
        issue_title = issue.get("title", "")

        # Compute hashes
        issue_body_sha256 = _sha256(issue_body)
        # B4: Handle comments_sha256 - empty list is different from None
        if comments is None:
            comments_sha256 = None
        elif isinstance(comments, list):
            if len(comments) == 0:
                comments_sha256 = _sha256("[]")
            else:
                comments_sha256 = _sha256(_canonical_json(comments))
        else:
            comments_sha256 = None

        known_context_sha256 = _canonical_json(known_context) if known_context else None
        if known_context_sha256:
            known_context_sha256 = _sha256(known_context_sha256)

        # B2: Support optional 'now' parameter for deterministic timestamp
        if "now" in input_data and input_data["now"]:
            generated_at = input_data["now"]
        else:
            generated_at = datetime.now(timezone.utc).isoformat()

        # Check for malformations
        fail_closed_reasons = []
        # Track missing sections and contract keys for FAIL_CLOSED_REWRITE_CONSTRAINTS_V1
        accumulated_missing_sections: list[str] = []
        accumulated_missing_contract_keys: list[str] = []

        if _check_malformed_contract(issue_body):
            fail_closed_reasons.append(FAIL_CLOSED_REASON_MALFORMED_CONTRACT)

        # Extract machine contract to determine issue_kind and parent_mode
        machine_contract = _extract_machine_contract(issue_body)
        raw_issue_kind = machine_contract.get("issue_kind") if machine_contract else None
        parent_mode = machine_contract.get("parent_mode") if machine_contract else None

        # Check for missing required contract keys (AC2/AC11)
        section_headings = _extract_sections(issue_body)
        has_contract_section = "Machine-Readable Contract" in section_headings
        if not has_contract_section:
            # AC1: Machine-Readable Contract section is absent → missing_required_section,
            # NOT missing_required_contract_key. The section itself is missing.
            accumulated_missing_sections.extend(["Machine-Readable Contract"])
            fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)
        else:
            # AC2: Section present — distinguish parse error from key absence.
            _contract_parse_error = _check_contract_parse_error(issue_body)
            if _contract_parse_error:
                # YAML parse error or malformed block — separate path from missing key.
                fail_closed_reasons.append(_contract_parse_error)
                # required_contract_keys is empty when we can't parse the YAML at all
            else:
                missing_contract_keys = _extract_missing_contract_keys(issue_body)
                if missing_contract_keys:
                    accumulated_missing_contract_keys.extend(missing_contract_keys)
                    fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_CONTRACT_KEY)

        # Apply alias normalization (design→research, tracking→parent, etc.)
        # _normalize_issue_kind returns None for unknown/unresolvable kinds.
        issue_kind = _normalize_issue_kind(raw_issue_kind) if raw_issue_kind else None

        # Determine if this is a parent delivery-rollup (AC1: exempt from Outcome check)
        is_parent_delivery_rollup = (
            issue_kind == "parent" and parent_mode == "delivery-rollup"
        )

        repo_root = _find_repo_root()

        if raw_issue_kind and not is_parent_delivery_rollup:
            # Apply alias normalization: unknown/unresolvable → fail_closed
            if issue_kind is None:
                fail_closed_reasons.append(FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND)
            else:
                # For known non-parent-delivery-rollup issue kinds: use template-derived required sections
                template_path = resolve_issue_template(issue_kind, repo_root)
                if template_path:
                    load_result = load_required_section_labels(template_path)
                    if load_result.error:
                        # Blocker 2: template load failure → fail_closed
                        fail_closed_reasons.append(load_result.error)
                    else:
                        missing = _check_missing_sections_from_template(issue_body, load_result.required_labels)
                        if missing:
                            accumulated_missing_sections.extend(missing)
                            fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)
                else:
                    # Template not found: fall back to Outcome check
                    if _check_missing_outcome(issue_body):
                        accumulated_missing_sections.extend(["Outcome"])
                        fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)
        elif is_parent_delivery_rollup:
            # AC1: parent delivery-rollup — check parent template sections (not Outcome)
            # AC7: Use separate reason_code for missing parent sections
            template_path = resolve_issue_template("parent", repo_root)
            if template_path:
                load_result = load_required_section_labels(template_path)
                if load_result.error:
                    # Blocker 2: template load failure → fail_closed
                    fail_closed_reasons.append(load_result.error)
                else:
                    missing = _check_missing_sections_from_template(issue_body, load_result.required_labels)
                    if missing:
                        accumulated_missing_sections.extend(missing)
                        fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_PARENT_SECTION)
        else:
            # No machine contract or unknown issue_kind: fall back to Outcome check
            if _check_missing_outcome(issue_body):
                accumulated_missing_sections.extend(["Outcome"])
                fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)

        # Normalize accumulated constraints once all checks complete.
        accumulated_missing_sections = _stable_sort_dedupe(accumulated_missing_sections)
        accumulated_missing_contract_keys = _stable_sort_dedupe(accumulated_missing_contract_keys)

        # Deduplicate reason_codes while preserving stable order.
        if fail_closed_reasons:
            fail_closed_reasons = list(dict.fromkeys(fail_closed_reasons))

        if fail_closed_reasons:
            # Build FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 for AC1/AC2/AC8
            rewrite_constraints = _build_fail_closed_rewrite_constraints(
                fail_closed_reasons,
                accumulated_missing_sections,
                accumulated_missing_contract_keys,
            )
            # B3: Return schema-valid decisions with unknown confidence even in fail_closed
            return (
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "issue_number": issue_number,
                        "issue_body_sha256": issue_body_sha256,
                        "comments_sha256": comments_sha256,
                        "known_context_sha256": known_context_sha256,
                        "generated_at": generated_at,
                    },
                    "decisions": {
                        "investigation_policy": {
                            "required": False,
                            "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                            "target_paths": [],
                            "repo_claims": [],
                            "evidence_spans": [],
                            "confidence": CONFIDENCE_UNKNOWN,
                        },
                        "web_research_policy": {
                            "required": False,
                            "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                            "critical_external_claims": [],
                            "evidence_spans": [],
                            "confidence": CONFIDENCE_UNKNOWN,
                        },
                        "scope_delta_decision": None,
                        "scope_signal_guard": {
                            "triggered": False,
                            "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                            "excluded_by_anchor_reframe": False,
                            "evidence_spans": [],
                        },
                        "delivery_rollup": {
                            "applicable": False,
                            "unmaterialized_slots": [],
                            "evidence_spans": [],
                        },
                        "follow_up_materialization": {
                            "candidates": [],
                        },
                    },
                    "fail_closed": {
                        "required": True,
                        "reason_codes": fail_closed_reasons,
                        "human_message": f"Issue contract malformation detected: {', '.join(fail_closed_reasons)}",
                        "rewrite_constraints": rewrite_constraints,
                    },
                },
                0,
            )

        # Extract evidence
        target_paths = _extract_paths_from_outcome_ac_vc(issue_body)
        repo_claims = _extract_repo_claims(issue_body)
        critical_external_claims = _extract_critical_external_claims(issue_body, comments)
        unmaterialized_slots = _extract_unmaterialized_slots(issue_body)
        follow_up_candidates = _extract_follow_up_candidates(issue_body)

        # Determine investigation policy
        investigation_required = bool(target_paths) or bool(repo_claims)
        investigation_reason = (
            INVESTIGATION_REASON_TARGET_PATHS_PRESENT
            if target_paths
            else (
                INVESTIGATION_REASON_REPO_FACT_CLAIM
                if repo_claims
                else INVESTIGATION_REASON_NO_REPO_FACT
            )
        )

        investigation_evidence = []
        # B6: Add evidence if investigation is required (either from paths OR repo_claims)
        if investigation_required:
            investigation_evidence.append(
                _create_evidence_span(
                    "issue_body",
                    issue_body,  # Use full body for consistent hashing
                    1,
                    len(issue_body.splitlines()),
                    source_ref=None,
                )
            )

        # Determine web research policy
        web_research_required = bool(critical_external_claims)
        web_research_reason = (
            WEB_RESEARCH_REASON_EXTERNAL_SPEC
            if critical_external_claims
            else WEB_RESEARCH_REASON_NO_CLAIM
        )

        web_research_evidence = []
        if critical_external_claims:
            web_research_evidence.append(
                _create_evidence_span(
                    "issue_body",
                    issue_body,  # Use full body for consistent hashing
                    1,
                    len(issue_body.splitlines()),
                    source_ref=None,
                )
            )

        # Determine scope signal guard using _detect_scope_signals
        scope_signal_triggered, scope_signal_reason, scope_signal_evidence = _detect_scope_signals(
            issue_body, known_context
        )

        # AC1/AC10 (#1090): opt-in SCOPE_SIGNAL_GUARD_DECISION_V2 lane split.
        # Only computed when known_context carries scope_signal_delta_input,
        # since that is the only source of a normalized added-paths list;
        # this keeps all pre-existing golden fixtures byte-identical.
        scope_signal_guard_decision_v2 = None
        if (
            known_context
            and isinstance(known_context.get("scope_signal_delta_input"), dict)
            and compute_scope_signal_delta is not None
        ):
            try:
                _delta_for_v2 = compute_scope_signal_delta(known_context["scope_signal_delta_input"])
                _added_paths = (
                    _delta_for_v2.get("sections", {}).get("allowed_paths", {}).get("added", [])
                )
                scope_signal_guard_decision_v2 = _build_scope_signal_guard_decision_v2(
                    scope_signal_triggered,
                    scope_signal_reason,
                    _added_paths,
                    known_context,
                    issue_number,
                )
            except Exception:
                scope_signal_guard_decision_v2 = None

        # Build output
        plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": issue_number,
                "issue_body_sha256": issue_body_sha256,
                "comments_sha256": comments_sha256,
                "known_context_sha256": known_context_sha256,
                "generated_at": generated_at,
            },
            "decisions": {
                "investigation_policy": {
                    "required": investigation_required,
                    "reason_code": investigation_reason,
                    "target_paths": _stable_sort_dedupe(list(target_paths)),
                    "repo_claims": _stable_sort_dedupe(repo_claims),
                    "evidence_spans": investigation_evidence,
                    "confidence": CONFIDENCE_DETERMINISTIC if investigation_required else CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": web_research_required,
                    "reason_code": web_research_reason,
                    "critical_external_claims": critical_external_claims,
                    "evidence_spans": web_research_evidence,
                    "confidence": CONFIDENCE_DETERMINISTIC if web_research_required else CONFIDENCE_UNKNOWN,
                },
                "scope_signal_guard": {
                    "triggered": scope_signal_triggered,
                    "reason_code": scope_signal_reason,
                    "excluded_by_anchor_reframe": scope_signal_reason == SCOPE_SIGNAL_REASON_ANCHOR_REFRAME,
                    "evidence_spans": scope_signal_evidence,
                },
                "scope_delta_decision": known_context.get("scope_delta_decision") if known_context else None,
                "delivery_rollup": {
                    "applicable": bool(unmaterialized_slots),
                    "unmaterialized_slots": unmaterialized_slots,
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": follow_up_candidates,
                },
            },
            "fail_closed": {
                "required": False,
                "reason_codes": [],
                "human_message": "",
            },
        }

        if scope_signal_guard_decision_v2 is not None:
            plan["scope_signal_guard_decision_v2"] = scope_signal_guard_decision_v2

        return plan, 0

    except ScopeSignalDeltaError as e:
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": issue_number if "issue_number" in locals() else None,
                "issue_body_sha256": issue_body_sha256 if "issue_body_sha256" in locals() else None,
                "comments_sha256": comments_sha256 if "comments_sha256" in locals() else None,
                "known_context_sha256": known_context_sha256 if "known_context_sha256" in locals() else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {
                "investigation_policy": {
                    "required": False,
                    "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                    "target_paths": [],
                    "repo_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": False,
                    "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                    "critical_external_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "scope_delta_decision": None,
                "scope_signal_guard": {
                    "triggered": False,
                    "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                    "excluded_by_anchor_reframe": False,
                    "evidence_spans": [],
                },
                "delivery_rollup": {
                    "applicable": False,
                    "unmaterialized_slots": [],
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": [],
                },
            },
            "fail_closed": {
                "required": True,
                "reason_codes": [FAIL_CLOSED_REASON_AMBIGUOUS_SIGNAL],
                "human_message": str(e),
            },
        }
        return fail_closed_plan, 0

    except Exception as e:
        # Internal error - B3: Return schema-valid decisions with unknown confidence
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": issue_number if "issue_number" in locals() else None,
                "issue_body_sha256": issue_body_sha256 if "issue_body_sha256" in locals() else None,
                "comments_sha256": None,
                "known_context_sha256": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {
                "investigation_policy": {
                    "required": False,
                    "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                    "target_paths": [],
                    "repo_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": False,
                    "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                    "critical_external_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "scope_delta_decision": None,
                "scope_signal_guard": {
                    "triggered": False,
                    "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                    "excluded_by_anchor_reframe": False,
                    "evidence_spans": [],
                },
                "delivery_rollup": {
                    "applicable": False,
                    "unmaterialized_slots": [],
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": [],
                },
            },
            "fail_closed": {
                "required": True,
                "reason_codes": [FAIL_CLOSED_REASON_INTERNAL_ERROR],
                "human_message": f"Internal error: {str(e)}",
            },
        }
        return fail_closed_plan, 3


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Main CLI entry point."""
    try:
        input_text = sys.stdin.read()
        input_data = _strict_json_loads(input_text)
    except (json.JSONDecodeError, ValueError) as e:
        # B3: Return schema-valid decisions even for JSON decode errors
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": None,
                "issue_body_sha256": None,
                "comments_sha256": None,
                "known_context_sha256": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {
                "investigation_policy": {
                    "required": False,
                    "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                    "target_paths": [],
                    "repo_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": False,
                    "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                    "critical_external_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "scope_delta_decision": None,
                "scope_signal_guard": {
                    "triggered": False,
                    "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                    "excluded_by_anchor_reframe": False,
                    "evidence_spans": [],
                },
                "delivery_rollup": {
                    "applicable": False,
                    "unmaterialized_slots": [],
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": [],
                },
            },
            "fail_closed": {
                "required": True,
                "reason_codes": [FAIL_CLOSED_REASON_UNKNOWN_SCHEMA],
                "human_message": f"Invalid JSON input: {str(e)}",
            },
        }
        print(json.dumps(fail_closed_plan, ensure_ascii=False, indent=2, allow_nan=False))
        sys.exit(2)

    plan, exit_code = plan_refinement_loop(input_data)

    # Output JSON to stdout (no prose/markdown)
    output_text = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False)
    print(output_text)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
