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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


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

FAIL_CLOSED_REASON_MALFORMED_CONTRACT = "malformed_machine_readable_contract"
FAIL_CLOSED_REASON_MISSING_SECTION = "missing_required_section"
FAIL_CLOSED_REASON_AMBIGUOUS_SIGNAL = "ambiguous_scope_signal"
FAIL_CLOSED_REASON_UNKNOWN_SCHEMA = "unknown_input_schema"
FAIL_CLOSED_REASON_INTERNAL_ERROR = "planner_internal_error"


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
    """Produce canonical JSON (sorted keys, compact)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _extract_sections(text: str) -> dict[str, str]:
    """Extract markdown sections (e.g., ## Outcome) from text."""
    sections = {}
    current_section = None
    current_content = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[3:].strip()
            current_content = []
        elif current_section:
            current_content.append(line)

    if current_section:
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
    classification = known_context.get("classification")
    if classification in ("feedback_update_required", "reframe_in_place"):
        return True
    if known_context.get("anchor_comment_url") and known_context.get("anchor_reframe", False):
        return True
    return False


def _detect_scope_signals(issue_body: str, known_context: dict | None) -> tuple[bool, str, list]:
    """
    Detect scope signals (new_in_scope_area, new_allowed_path_layer, new_unverifiable_ac).

    Returns (triggered, reason_code, evidence_spans)

    Precedence: new_unverifiable_ac > new_allowed_path_layer > new_in_scope_area > none
    """
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
    """Check if Outcome section is missing."""
    return "## Outcome" not in issue_body


def _check_missing_ac(issue_body: str) -> bool:
    """Check if Acceptance Criteria section is missing."""
    return "## Acceptance Criteria" not in issue_body


def _check_missing_vc(issue_body: str) -> bool:
    """Check if Verification Commands section is missing."""
    return "## Verification Commands" not in issue_body



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

        if _check_malformed_contract(issue_body):
            fail_closed_reasons.append(FAIL_CLOSED_REASON_MALFORMED_CONTRACT)

        if _check_missing_outcome(issue_body):
            fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)

        if fail_closed_reasons:
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
                    "excluded_by_anchor_reframe": scope_signal_triggered and scope_signal_reason == SCOPE_SIGNAL_REASON_ANCHOR_REFRAME,
                    "evidence_spans": scope_signal_evidence,
                },
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

        return plan, 0

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
        input_data = json.loads(input_text)
    except json.JSONDecodeError as e:
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
        print(json.dumps(fail_closed_plan, ensure_ascii=False, indent=2))
        sys.exit(2)

    plan, exit_code = plan_refinement_loop(input_data)

    # Output JSON to stdout (no prose/markdown)
    output_text = json.dumps(plan, ensure_ascii=False, indent=2)
    print(output_text)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
