#!/usr/bin/env python3
"""
Contract Readiness Check (ISSUE_CONTRACT_READINESS_RESULT_V1)

Issue body の contract readiness を検証し、review-issue / issue-author / edit-issue が
消費できる structured feedback JSON を返す mutation-free helper。

Exit codes:
  0: status: go (all checks pass)
  1: status: needs_fix (body-author-fixable errors)
  2: status: human_judgment (env/tool/runtime issues needing human attention)
  3: input/runtime error

Modes:
  --mode static  (default): VC syntax, section, schema only. No VC execution. No network.
  --mode preflight-static: Same as static. Alias for review-issue / issue-reviewer callers.
    Detects compound_command_disallowed statically (no execution).
    unexpected_pass detection requires --mode execute (execution-only signal).
  --mode execute: Invokes baseline_vc_preflight.py to run VCs. May have side effects.

Inputs:
  --body-file <path>   : Read issue body from file (static mode preferred)
  --issue <N> --repo <owner/repo> : Fetch from GitHub (requires gh auth)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# Locate sibling scripts (relative to this file)
_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_VALIDATE_ISSUE_BODY_PY = (
    _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts" / "validate_issue_body.py"
)
_BASELINE_VC_PREFLIGHT_PY = _SCRIPTS_DIR / "baseline_vc_preflight.py"

# AC10 (#1346): share heading detection with prose_boundary_policy.py's HEADING_POLICY
# so RDR001 section extraction recognises the same accepted forms (incl. Japanese
# headings) as the prose-boundary guard, instead of an independent English-only regex.
_CREATE_ISSUE_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(_CREATE_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS_DIR))

from prose_boundary_policy import HEADING_POLICY  # noqa: E402

# Required fields for `decision: immediate` in Runtime Verification Applicability section
_RVA_IMMEDIATE_REQUIRED_FIELDS = [
    "applicable_acs",
    "execution_environment",
    "skip_conditions",
    "fallback_policy",
    "artifact_requirements",
]


# ---------------------------------------------------------------------------
# Body acquisition
# ---------------------------------------------------------------------------


def read_body_file(path: str) -> tuple[Optional[str], Optional[str]]:
    """Read body from file. Returns (body, error_code)."""
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "body_file_not_found"
    except Exception:
        return None, "body_parse_error"


def fetch_body_from_github(issue: int, repo: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch issue body from GitHub. Returns (body, error_code)."""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout).get("body"), None
        stderr = result.stderr.lower()
        if "not authenticated" in stderr or "authentication failed" in stderr:
            return None, "gh_auth_failed"
        if "not found" in stderr or "could not resolve" in stderr:
            return None, "gh_repo_not_found"
        return None, "gh_other_error"
    except subprocess.TimeoutExpired:
        return None, "gh_timeout"
    except json.JSONDecodeError:
        return None, "gh_json_parse_error"
    except Exception:
        return None, "gh_other_error"


# ---------------------------------------------------------------------------
# SHA-256
# ---------------------------------------------------------------------------


def sha256_of(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# validate_issue_body.py integration
# ---------------------------------------------------------------------------


def run_validate_issue_body(body: str) -> dict[str, Any]:
    """
    Run validate_issue_body.py via subprocess with --body-file.
    Returns parsed JSON output (loop_body_lint/v1 schema).
    --mode static: no network, no execution beyond python subprocess.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(_VALIDATE_ISSUE_BODY_PY), "--body-file", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout:
            return json.loads(result.stdout)
        # Empty output or non-zero exit 2+ — validator internal error (not body-author-fixable)
        return {
            "schema": "loop_body_lint/v1",
            "status": "validator_internal_error",
            "errors": [
                {
                    "rule_id": "VALIDATOR_INTERNAL",
                    "severity": "error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "message": result.stderr or "no output from validate_issue_body",
                    "minimal_context": [],
                    "context_truncated": False,
                    "fix_hint": "validator 実行環境を確認してください",
                    "autofixable": False,
                }
            ],
        }
    except subprocess.TimeoutExpired:
        # Tool-level failure — not fixable by body author
        return {
            "schema": "loop_body_lint/v1",
            "status": "validator_tool_error",
            "errors": [
                {
                    "rule_id": "VALIDATOR_TIMEOUT",
                    "severity": "error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "message": "validate_issue_body timed out",
                    "minimal_context": [],
                    "context_truncated": False,
                    "fix_hint": "validator 実行環境を確認してください",
                    "autofixable": False,
                }
            ],
        }
    except json.JSONDecodeError as exc:
        # JSON decode error — validator internal error (not body-author-fixable)
        return {
            "schema": "loop_body_lint/v1",
            "status": "validator_internal_error",
            "errors": [
                {
                    "rule_id": "VALIDATOR_JSON_ERROR",
                    "severity": "error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "message": f"json decode error: {exc}",
                    "minimal_context": [],
                    "context_truncated": False,
                    "fix_hint": "validator 実行環境を確認してください",
                    "autofixable": False,
                }
            ],
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def map_validate_errors_to_readiness_errors(validate_result: dict) -> list[dict]:
    """Convert loop_body_lint/v1 errors into ISSUE_CONTRACT_READINESS_RESULT_V1 errors[]."""
    errors = []
    for e in validate_result.get("errors", []):
        errors.append(
            {
                "rule_id": e.get("rule_id", "LP000"),
                "severity": e.get("severity", "error"),
                "source_check": "validate_issue_body",
                "category": "body_lint",
                "section": e.get("section", ""),
                "line_start": e.get("line_start", 0),
                "line_end": e.get("line_end", 0),
                "minimal_context": e.get("minimal_context", []),
                "fix_hint": e.get("fix_hint", ""),
                "autofixable": e.get("autofixable", False),
            }
        )
    return errors


# ---------------------------------------------------------------------------
# baseline_vc_preflight.py integration (execute mode only)
# ---------------------------------------------------------------------------


def run_baseline_vc_preflight(body: str) -> tuple[dict, int]:
    """
    Run baseline_vc_preflight.py via subprocess.
    Returns (parsed_json, exit_code).
    Only called in --mode execute.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(_BASELINE_VC_PREFLIGHT_PY), "--strict", "--body-file", tmp_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        exit_code = result.returncode
        if result.stdout:
            return json.loads(result.stdout), exit_code
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "blocked",
                "results": [],
                "errors": [result.stderr or "no output"],
            },
            exit_code,
        )
    except subprocess.TimeoutExpired:
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "blocked",
                "results": [],
                "errors": ["timeout"],
            },
            1,
        )
    except json.JSONDecodeError as exc:
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "blocked",
                "results": [],
                "errors": [f"json decode: {exc}"],
            },
            1,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# Status mapping contract:
# compound_command_disallowed → needs_fix (VC body fix resolves it)
# unexpected_pass → needs_fix (VC tightening resolves it)
# file_not_found_unrunnable → needs_fix (body refers to missing script)
# no_commands / extraction_error (body structure) → needs_fix
# env_missing_dep → human_judgment (not fixable by body author alone)
# regression_gate fail → human_judgment (env/implementation issue)
# human_judgment → human_judgment (MUST NOT collapse)
# timeout → human_judgment
_PREFLIGHT_CATEGORY_TO_READINESS: dict[str, str] = {
    "compound_command_disallowed": "needs_fix",
    "expected_baseline_fail": "go",
    "file_not_found_expected": "go",
    "env_missing_dep": "human_judgment",
    "file_not_found_unrunnable": "needs_fix",
    "timeout": "human_judgment",
    "unexpected_pass": "needs_fix",
    "unknown": "human_judgment",
    "no_commands_extracted": "needs_fix",
    # Issue #889: baseline-expect annotation mappings
    # baseline_expect_pass: VC annotated baseline-expect: pass and exited 0 → go
    "baseline_expect_pass": "go",
    # baseline_regression_failed: VC annotated baseline-expect: pass but exited non-0
    "baseline_regression_failed": "human_judgment",
    # AC: classify no-TTY pnpm classification as human_judgment（失敗分類を維持）
    "package_manager_no_tty_prompt": "human_judgment",
    # Issue #899: strict-mode annotation violations are body-author-fixable
    "inline_baseline_expect_invalid_placement": "needs_fix",
    "missing_baseline_expect_for_new_allowed_path": "needs_fix",
    # PR #1366 review (Blocker 1) / Issue #1347: existing-file missing node-id is a
    # non-canonical VC shape that the Issue body author can fix by rewriting the VC
    # to reference a not-yet-created test file instead → needs_fix (not human_judgment).
    "existing_file_missing_node_id_noncanonical": "needs_fix",
    # Issue #1406 (Blocker 2, PR #1412 review): a Verification Command with an
    # unbounded `rg` search path (no explicit path argument, so it recurses
    # over the whole repo) is a body-author-fixable VC-scope problem, not an
    # environment/tooling issue -> needs_fix (not human_judgment).
    "broad_search_path_unbounded": "needs_fix",
}


def map_preflight_result_to_errors(
    preflight_result: dict,
) -> tuple[list[dict], str]:
    """
    Map baseline_vc_preflight/v1 result into readiness errors[].

    Status priority: human_judgment > needs_fix > go.
    human_judgment from preflight MUST NOT be collapsed to needs_fix.

    Returns (errors_list, aggregate_readiness_status).
    """
    errors: list[dict] = []
    aggregate = "go"

    overall_status = preflight_result.get("status", "blocked")

    # Sentinel for absent "message" key (AC5: distinguish absent from None)
    _MISSING = object()

    # blocked with no results = body-structure issue (needs_fix)
    if overall_status == "blocked" and not preflight_result.get("results"):
        for err_msg in preflight_result.get("errors", []):
            # B6: handle both structured dict errors and legacy plain strings
            if isinstance(err_msg, dict):
                # AC5: fallback to str(err_msg) when "message" key is absent
                raw_msg = err_msg.get("message", _MISSING)
                msg = str(err_msg) if raw_msg is _MISSING else str(raw_msg)
                # AC6: normalize minimal_context — flatten list to avoid nested list
                raw_mc = err_msg.get("minimal_context", "")
                if isinstance(raw_mc, list):
                    mc_items: list[str] = [str(x) for x in raw_mc]
                elif raw_mc:
                    mc_items = [str(raw_mc)]
                else:
                    mc_items = []
                fh = err_msg.get("fix_hint", (
                    "Ensure Verification Commands section has fenced ```bash blocks "
                    "with $ prefixed commands."
                ))
                rule = err_msg.get("rule", "VCP001")
                # AC7: avoid double namespace — if rule already has a known prefix,
                # do not prepend "VCP_" again (e.g. "VC000_BODY_RETRIEVAL_FAILED" stays as-is)
                if rule and rule != "VCP001":
                    if rule.startswith("VCP_") or rule.startswith("VC") and "_" in rule:
                        rule_id = rule
                    else:
                        rule_id = f"VCP_{rule}"
                else:
                    rule_id = "VCP001"
                # Blocker 3: consume "kind" field to determine category and readiness_status
                kind = str(err_msg.get("kind", "extraction_error"))
                if kind == "retrieval_error":
                    category = "body_retrieval_failed"
                    readiness_status = "human_judgment"
                elif kind in ("extraction_error", "unsupported_vc_format"):
                    category = kind
                    readiness_status = "needs_fix"
                else:
                    category = kind or "preflight_error"
                    readiness_status = "human_judgment"
            else:
                msg = str(err_msg)
                mc_items = []
                fh = (
                    "Ensure Verification Commands section has fenced ```bash blocks "
                    "with $ prefixed commands."
                )
                rule_id = "VCP001"
                category = "no_commands_extracted"
                readiness_status = "needs_fix"
            errors.append(
                {
                    "rule_id": rule_id,
                    "severity": "error",
                    "source_check": "baseline_vc_preflight",
                    "category": category,
                    "section": "Verification Commands",
                    "line_start": 0,
                    "line_end": 0,
                    "minimal_context": [msg] + mc_items,
                    "fix_hint": fh,
                    "autofixable": False,
                }
            )
            aggregate = _raise_status(aggregate, readiness_status)
        return errors, aggregate

    for r in preflight_result.get("results", []):
        classification = r.get("classification", "")
        category = r.get("category", "")
        decision = r.get("decision", "go")
        scope_class = r.get("scope_class", "")

        # Skipped items: routing metadata, not errors
        if classification == "skipped":
            continue
        # expected_pass: no error
        if classification == "expected_pass":
            continue
        # expected_fail with go decision: normal baseline fail
        if classification == "expected_fail" and decision == "go":
            continue

        # Determine readiness impact
        readiness_status: Optional[str] = None

        # human_judgment decision: always human_judgment (MUST NOT collapse)
        if decision == "human_judgment":
            readiness_status = "human_judgment"
        elif decision == "blocked":
            mapped = _PREFLIGHT_CATEGORY_TO_READINESS.get(category)
            if mapped is not None:
                readiness_status = mapped
            elif scope_class == "regression_gate":
                readiness_status = "human_judgment"
            else:
                readiness_status = "human_judgment"

        # unexpected_pass classification overrides: normally needs_fix
        # Issue #889: if preflight payload reports baseline_expect=pass for this result,
        # then unexpected_pass was already re-mapped to expected_pass in baseline_vc_preflight.py
        # and would not reach here. But guard defensively:
        # - category == "baseline_expect_pass" → already handled above (mapped to go)
        # - category == "baseline_regression_failed" → already mapped to human_judgment
        # - annotation absent → backward compat: unexpected_pass → needs_fix
        if classification == "unexpected_pass":
            annotations = r.get("annotations", {})
            baseline_expect_val = annotations.get("baseline_expect") if isinstance(annotations, dict) else None
            if baseline_expect_val == "pass":
                # Should not normally occur (preflight re-maps to expected_pass),
                # but if it does, treat as go (AC12: use annotations from payload)
                readiness_status = "go"
            else:
                readiness_status = "needs_fix"

        if readiness_status in ("needs_fix", "human_judgment"):
            aggregate = _raise_status(aggregate, readiness_status)
            errors.append(
                {
                    "rule_id": f"VCP_{category.upper()[:20]}"
                    if category
                    else "VCP_UNKNOWN",
                    "severity": "error",
                    "source_check": "baseline_vc_preflight",
                    "category": category,
                    "section": "Verification Commands",
                    "line_start": r.get("line", 0),
                    "line_end": r.get("line", 0),
                    "minimal_context": _build_vc_context(r),
                    "fix_hint": r.get("fix_hint") or _default_fix_hint(category),
                    "autofixable": category in (
                        "compound_command_disallowed",
                        "inline_baseline_expect_invalid_placement",
                        "missing_baseline_expect_for_new_allowed_path",
                    ),
                    "source_payload": {
                        "classification": classification,
                        "category": category,
                        "scope_class": scope_class,
                        "decision": decision,
                        "confidence": r.get("confidence", ""),
                        "exit_code": r.get("exit_code"),
                        "command_hash": r.get("command_hash", ""),
                        "duration_ms": r.get("duration_ms"),
                        "strict": r.get("strict"),
                        "repair": r.get("repair"),
                        "annotations": r.get("annotations"),
                        "runner_env_delta": r.get("runner_env_delta", {}),
                    },
                }
            )

    return errors, aggregate


def _raise_status(current: str, candidate: str) -> str:
    """Priority: human_judgment > needs_fix > go."""
    priority = {"go": 0, "needs_fix": 1, "human_judgment": 2}
    if priority.get(candidate, 0) > priority.get(current, 0):
        return candidate
    return current


def _build_vc_context(result_item: dict) -> list[str]:
    cmd = result_item.get("raw_command", "")
    ac = result_item.get("ac", "")
    lines: list[str] = []
    if ac:
        lines.append(f"# {ac}")
    if cmd:
        lines.append(f"$ {cmd}")
    stderr_head = result_item.get("stderr_head", [])
    stdout_head = result_item.get("stdout_head", [])
    if stderr_head:
        lines.extend(stderr_head[:3])
    elif stdout_head:
        lines.extend(stdout_head[:3])
    return lines


def _default_fix_hint(category: str) -> str:
    hints: dict[str, str] = {
        "compound_command_disallowed": (
            "Replace compound shell command with a single command. "
            "See body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL."
        ),
        "unexpected_pass": (
            "VC passed before implementation. Tighten VC so it fails at baseline."
        ),
        "env_missing_dep": (
            "Required tool or command is missing from the environment. Human intervention needed."
        ),
        "regression_gate": (
            "Regression gate command failed. Check environment or fix implementation."
        ),
        "timeout": "Command timed out. May require human investigation.",
        "unknown": "Unable to classify result. Human judgment required.",
        "file_not_found_unrunnable": (
            "Script or file referenced in VC does not exist. Fix path in VC."
        ),
    }
    return hints.get(category, "See baseline_vc_preflight output for details.")


# ---------------------------------------------------------------------------
# AC4: Runtime Verification Applicability immediate field check
# ---------------------------------------------------------------------------


def check_rva_immediate_fields(body: str) -> list[dict]:
    """
    AC4: Check that `decision: immediate` RVA section has all required fields.

    Required for decision: immediate:
      applicable_acs, execution_environment, skip_conditions,
      fallback_policy, artifact_requirements

    Returns list of readiness errors (may be empty).
    """
    errors: list[dict] = []

    rva_match = re.search(
        r"^##\s+Runtime Verification Applicability\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not rva_match:
        return []

    section_content = rva_match.group(1)
    section_start_line = body[: rva_match.start()].count("\n") + 1

    # Only check when decision: immediate
    if not re.search(r"decision:\s*immediate", section_content, re.IGNORECASE):
        return []

    missing_fields: list[str] = []
    for field in _RVA_IMMEDIATE_REQUIRED_FIELDS:
        # Check for field as a direct YAML key (possibly inside a yaml block)
        simple_pattern = re.compile(rf"^\s*{re.escape(field)}\s*:", re.MULTILINE)
        if not simple_pattern.search(section_content):
            missing_fields.append(field)

    for field in missing_fields:
        # Build context from first non-empty lines of section
        context_lines: list[str] = []
        for line in section_content.split("\n"):
            if line.strip():
                context_lines.append(line)
            if len(context_lines) >= 3:
                break

        errors.append(
            {
                "rule_id": "RVA001",
                "severity": "error",
                "source_check": "contract_readiness_check",
                "category": "rva_immediate_field_missing",
                "section": "Runtime Verification Applicability",
                "line_start": section_start_line,
                "line_end": section_start_line + section_content.count("\n"),
                "minimal_context": context_lines,
                "fix_hint": (
                    f"Add '{field}' field to Runtime Verification Applicability section. "
                    "Required fields for decision: immediate are: "
                    + ", ".join(_RVA_IMMEDIATE_REQUIRED_FIELDS)
                ),
                "autofixable": True,
            }
        )

    return errors


# ---------------------------------------------------------------------------
# AC4 (#1346): Required Design References check (implementation issues only)
# ---------------------------------------------------------------------------

# AC10 (#1346): build the RDR heading regex from prose_boundary_policy.py's
# HEADING_POLICY accepted_forms so this static checker recognises the same heading
# variants (including Japanese forms) as the authoring-side prose boundary guard.
_RDR_ACCEPTED_HEADINGS = HEADING_POLICY["Required Design References"]["accepted_forms"]
_RDR_HEADING_ALT = "|".join(re.escape(h) for h in _RDR_ACCEPTED_HEADINGS)
_RDR_SECTION_RE = re.compile(
    rf"^##\s+(?:{_RDR_HEADING_ALT})\s*$(.+?)(?=^##|\Z)",
    re.MULTILINE | re.DOTALL,
)

# AC11 (#1346): design-doc path references must point at an actual, narrowly-scoped
# design-doc location (docs/**/*.md|yml, .claude/skills/**/SKILL.md,
# .claude/skills/**/references/**/*.md). src/ and scripts/ are intentionally excluded:
# those are implementation paths, not design-doc references.
_REQUIRED_DESIGN_REFERENCES_PATH_RE = re.compile(
    r"(?:^|[\s(`\[])("
    r"docs/[\w\-./]+\.(?:md|yml)"
    r"|\.claude/skills/[\w\-./]+/SKILL\.md"
    r"|\.claude/skills/[\w\-./]+/references/[\w\-./]+\.md"
    r")"
)

_PLACEHOLDER_ONLY_VALUES = {"", "n/a", "none", "なし", "-"}


def _extract_issue_kind(body: str) -> Optional[str]:
    """Extract `issue_kind` from the `## Machine-Readable Contract` YAML block.

    Self-contained regex extraction — does NOT forward --kind to
    validate_issue_body.py (AC6: keep responsibility boundaries intact,
    do not change existing kind-agnostic fixture behavior).
    """
    mrc_match = re.search(
        r"^##\s+Machine-Readable Contract\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not mrc_match:
        return None
    kind_match = re.search(r"^\s*issue_kind:\s*(\S+)", mrc_match.group(1), re.MULTILINE)
    if not kind_match:
        return None
    return kind_match.group(1).strip().strip('"').strip("'")


def check_required_design_references(body: str) -> list[dict]:
    """
    AC4: For `issue_kind: implementation` issues, validate the
    `## Required Design References` section (when present) is not
    empty / N/A / none-only, and contains at least one repo-relative
    design-doc path reference (e.g. docs/dev/agent-skill-boundaries.md).

    Mirrors the RVA precedent (check_rva_immediate_fields): when the
    section is entirely absent, this function returns no errors here
    (existence enforcement is a template / review-issue concern, not this
    static checker — AC6: do not regress existing go fixtures that predate
    this section).
    """
    if _extract_issue_kind(body) != "implementation":
        return []

    section_match = _RDR_SECTION_RE.search(body)
    if not section_match:
        return []

    section_content = section_match.group(1)
    section_start_line = body[: section_match.start()].count("\n") + 1

    stripped = section_content.strip()
    non_empty_lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    is_placeholder_only = (not non_empty_lines) or all(
        line.lower().lstrip("- ").strip() in _PLACEHOLDER_ONLY_VALUES for line in non_empty_lines
    )

    # AC11 (#1346): a candidate path is only a valid design-doc reference when it
    # (a) matches the narrowed docs/**|.claude/skills/**/SKILL.md|.claude/skills/**/references/**
    #     shape, and (b) actually exists in the repo (Path.exists()).
    has_path_ref = False
    for match in _REQUIRED_DESIGN_REFERENCES_PATH_RE.finditer(section_content):
        candidate = match.group(1)
        if (_REPO_ROOT / candidate).exists():
            has_path_ref = True
            break

    if is_placeholder_only or not has_path_ref:
        errors = [
            {
                "rule_id": "RDR001",
                "severity": "error",
                "source_check": "contract_readiness_check",
                "category": "required_design_references_missing_or_empty",
                "section": "Required Design References",
                "line_start": section_start_line,
                "line_end": section_start_line + section_content.count("\n"),
                "minimal_context": non_empty_lines[:3],
                "fix_hint": (
                    "Add at least one repo-relative, *existing* design-doc path reference "
                    "(e.g. docs/dev/agent-skill-boundaries.md, .claude/skills/<skill>/SKILL.md, "
                    "or .claude/skills/<skill>/references/<doc>.md) to Required Design "
                    "References. Do not leave it empty / N/A / none only. "
                    "See body-authoring.md#Required Design References Authoring Guidance."
                ),
                # AC11 (#1346): not autofixable — the correct design-doc reference requires
                # human judgment about which SSOT the issue actually depends on.
                "autofixable": False,
            }
        ]
        return errors

    return []


# ---------------------------------------------------------------------------
# Static VC syntax check (compound command detection without execution)
# ---------------------------------------------------------------------------


def check_vc_static_syntax(body: str) -> list[dict]:
    """
    Static-only check of Verification Commands for compound shell operators.

    Does NOT execute any commands. Used in --mode static (the default).
    Returns list of errors.
    """
    errors: list[dict] = []

    vc_match = re.search(
        r"^##\s+Verification Commands\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not vc_match:
        return []

    vc_section = vc_match.group(1)
    section_start_line = body[: vc_match.start()].count("\n") + 2  # +2 for header

    # Sync operator set with body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL
    # Redirect operators (<, >, <<, >>, <<<) are NOT enforced here:
    # they risk false positives with placeholder syntax (e.g., <file>, <pattern>).
    # Only control operators that affect exit-code reliability are hard errors.
    compound_operators = frozenset(["&&", "||", "|", ";", "&"])

    # B4: only ```bash fenced blocks are canonical VC format; unlabeled fences are ignored
    for block_match in re.finditer(r"```bash[ \t]*\n(.*?)```", vc_section, re.DOTALL):
        block_content = block_match.group(1)
        block_start_in_section = vc_section[: block_match.start()].count("\n")
        block_abs_start = section_start_line + block_start_in_section

        for line_offset, line in enumerate(block_content.split("\n")):
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith("#"):
                continue
            # Strip leading $ prefix
            cmd = re.sub(r"^\$\s*", "", stripped)
            if not cmd or cmd.startswith("#"):
                continue

            try:
                lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
                tokens = list(lexer)
                if any(t in compound_operators for t in tokens):
                    abs_line = block_abs_start + line_offset + 2
                    errors.append(
                        {
                            "rule_id": "VCS001",
                            "severity": "error",
                            "source_check": "contract_readiness_check",
                            "category": "compound_command_disallowed",
                            "section": "Verification Commands",
                            "line_start": abs_line,
                            "line_end": abs_line,
                            "minimal_context": [line],
                            "fix_hint": (
                                "Remove compound shell operators (&&, ||, |, ;, &) from VC. "
                                "Use a single command per VC. "
                                "See body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL."
                            ),
                            "autofixable": False,
                        }
                    )
            except ValueError:
                abs_line = block_abs_start + line_offset + 2
                errors.append(
                    {
                        "rule_id": "VCS001",
                        "severity": "error",
                        "source_check": "contract_readiness_check",
                        "category": "compound_command_disallowed",
                        "section": "Verification Commands",
                        "line_start": abs_line,
                        "line_end": abs_line,
                        "minimal_context": [line],
                        "fix_hint": (
                            "Command syntax could not be parsed. "
                            "Simplify to a single command."
                        ),
                        "autofixable": False,
                    }
                )

    return errors


# ---------------------------------------------------------------------------
# Aggregate status computation
# ---------------------------------------------------------------------------


def compute_aggregate_status(
    validate_errors: list[dict],
    preflight_errors: list[dict],
    rva_errors: list[dict],
    static_vc_errors: list[dict],
    preflight_aggregate: str,
) -> str:
    """
    Compute overall readiness status from all sources.
    Priority: human_judgment > needs_fix > go.
    """
    status = "go"

    # validate_issue_body errors: body-author-fixable → needs_fix
    # validator_tool_error / validator_internal_error → human_judgment (not author-fixable)
    if any(e.get("severity") == "error" for e in validate_errors):
        # Check if errors come from a tool/internal failure (not body-author-fixable)
        tool_error_rule_ids = {"VALIDATOR_TIMEOUT", "VALIDATOR_INTERNAL", "VALIDATOR_JSON_ERROR"}
        if any(e.get("rule_id") in tool_error_rule_ids for e in validate_errors):
            status = _raise_status(status, "human_judgment")
        else:
            status = _raise_status(status, "needs_fix")

    # RVA immediate field errors: author can add fields → needs_fix
    if rva_errors:
        status = _raise_status(status, "needs_fix")

    # Static VC errors: compound commands → needs_fix
    if static_vc_errors:
        status = _raise_status(status, "needs_fix")

    # Preflight aggregate (execute mode only)
    status = _raise_status(status, preflight_aggregate)

    return status


# ---------------------------------------------------------------------------
# Main result builder
# ---------------------------------------------------------------------------


def build_result(
    body: str,
    mode: str,
    validate_result: dict,
    preflight_result: Optional[dict],
    preflight_exit_code: Optional[int],
) -> dict:
    """Build ISSUE_CONTRACT_READINESS_RESULT_V1 from all check results."""
    body_sha256 = sha256_of(body)

    validate_status = validate_result.get("status", "fail")
    validate_exit_code = 0 if validate_status == "pass" else 1

    source_checks: list[dict] = [
        {
            "name": "validate_issue_body",
            "schema": "loop_body_lint/v1",
            "status": validate_status,
            "exit_code": validate_exit_code,
        }
    ]

    if preflight_result is not None:
        preflight_status = preflight_result.get("status", "blocked")
        source_checks.append(
            {
                "name": "baseline_vc_preflight",
                "schema": "baseline_vc_preflight/v1",
                "status": preflight_status,
                "exit_code": preflight_exit_code if preflight_exit_code is not None else -1,
            }
        )

    validate_errors = map_validate_errors_to_readiness_errors(validate_result)
    rva_errors = check_rva_immediate_fields(body)
    rdr_errors = check_required_design_references(body)

    preflight_errors: list[dict] = []
    preflight_aggregate = "go"
    if preflight_result is not None:
        preflight_errors, preflight_aggregate = map_preflight_result_to_errors(
            preflight_result
        )

    # Static VC syntax check: in static/preflight-static mode (execute mode uses preflight)
    static_vc_errors: list[dict] = []
    if mode in ("static", "preflight-static"):
        static_vc_errors = check_vc_static_syntax(body)

    all_errors = validate_errors + rva_errors + rdr_errors + static_vc_errors + preflight_errors

    overall_status = compute_aggregate_status(
        validate_errors,
        preflight_errors,
        rva_errors,
        static_vc_errors,
        preflight_aggregate,
    )
    if rdr_errors:
        overall_status = _raise_status(overall_status, "needs_fix")

    fix_hint: Optional[str] = None
    minimal_context: list = []
    if all_errors:
        first_error = all_errors[0]
        fix_hint = first_error.get("fix_hint")
        minimal_context = first_error.get("minimal_context", [])

    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": overall_status,
        "body_sha256": body_sha256,
        "source_checks": source_checks,
        "errors": all_errors,
        "minimal_context": minimal_context,
        "fix_hint": fix_hint,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Contract Readiness Check: returns ISSUE_CONTRACT_READINESS_RESULT_V1 JSON"
    )
    parser.add_argument("--body-file", help="Path to issue body file")
    parser.add_argument(
        "--issue", "--issue-number", dest="issue", type=int, help="GitHub Issue number"
    )
    parser.add_argument(
        "--repo", default="squne121/loop-protocol", help="GitHub repo (owner/name)"
    )
    parser.add_argument(
        "--mode",
        choices=["static", "preflight-static", "execute"],
        default="static",
        help=(
            "static (default): VC syntax/section/schema only; no execution, no network. "
            "preflight-static: alias for static; use in review-issue / issue-reviewer callers. "
            "  Detects compound_command_disallowed statically. "
            "  unexpected_pass detection requires --mode execute (execution-only signal). "
            "execute: also runs baseline_vc_preflight.py to execute VCs."
        ),
    )

    args = parser.parse_args()

    # Acquire body
    body: Optional[str] = None
    error_code: Optional[str] = None

    if args.body_file:
        body, error_code = read_body_file(args.body_file)
    elif args.issue:
        body, error_code = fetch_body_from_github(args.issue, args.repo)
    else:
        print("ERROR: --body-file or --issue required", file=sys.stderr)
        return 3

    if body is None:
        error_result = {
            "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
            "status": "human_judgment",
            "body_sha256": "sha256:",
            "source_checks": [],
            "errors": [
                {
                    "rule_id": "INPUT001",
                    "severity": "error",
                    "source_check": "contract_readiness_check",
                    "category": "input_error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "minimal_context": [error_code or "unknown"],
                    "fix_hint": f"Input error: {error_code}",
                    "autofixable": False,
                }
            ],
            "minimal_context": [],
            "fix_hint": f"Input error: {error_code}",
        }
        print(json.dumps(error_result, indent=2))
        return 3

    # Run validate_issue_body (always)
    validate_result = run_validate_issue_body(body)

    # Run baseline_vc_preflight only in execute mode
    preflight_result: Optional[dict] = None
    preflight_exit_code: Optional[int] = None
    if args.mode == "execute":  # preflight-static is static-only; no execution
        preflight_result, preflight_exit_code = run_baseline_vc_preflight(body)

    result = build_result(body, args.mode, validate_result, preflight_result, preflight_exit_code)

    print(json.dumps(result, indent=2))

    status = result["status"]
    if status == "go":
        return 0
    elif status == "needs_fix":
        return 1
    else:  # human_judgment
        return 2


if __name__ == "__main__":
    sys.exit(main())
