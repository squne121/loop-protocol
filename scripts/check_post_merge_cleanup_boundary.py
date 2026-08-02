#!/usr/bin/env python3
"""check_post_merge_cleanup_boundary.py — post-merge-cleanup orchestrator/executor
boundary validator and POST_MERGE_CLEANUP_REPORT_V1 report validator (Issue #1733).

This module provides:

- ``validate_report_v1`` / ``validate_report_yaml``: a closed-key structural
  validator for ``POST_MERGE_CLEANUP_REPORT_V1`` (required keys, types, unknown
  key rejection, malformed YAML detection). This is a real structural
  validator, not an identifier grep.
- boundary check functions consumed by ``tests/test_post_merge_cleanup_boundary.py``
  to verify the Claude/Codex worker frontmatter, the orchestrator/executor
  instruction separation, the no-child policy, and the follow-up routing
  ownership split described in Issue #1733.
- a CLI entrypoint (``--check runtime_smoke_evidence``) that inspects
  ``worktree-agent-runtime-smoke`` evidence written under
  ``artifacts/runtime-smoke/`` for AC12 and exits 0/1/77 per
  ``docs/dev/runtime-verification-policy.md`` (SKIP is never promoted to PASS).

Exit codes (CLI):
  0  success (or SKIP path intentionally not requested)
  1  check failed
  77 SKIP (runtime evidence unavailable — environment blocker, not a failure)
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA_REPORT_V1 = "POST_MERGE_CLEANUP_REPORT_V1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

# ---------------------------------------------------------------------------
# POST_MERGE_CLEANUP_REPORT_V1 structural validator (AC6)
# ---------------------------------------------------------------------------

# key -> expected python type(s). ``dict`` fields (parent_issue_status) are
# validated further via _validate_parent_issue_status.
_REPORT_REQUIRED_KEYS: dict[str, tuple[type, ...]] = {
    "status": (str,),
    "generated_at": (str,),
    "generated_by": (str,),
    "human_review_required": (bool,),
    "cleaned_branches": (list,),
    "cleaned_worktrees": (list,),
    "unresolved_cleanup_items": (list,),
    "parent_issue_status": (dict,),
    "superseded_prs": (list,),
    "follow_up_issue_requests": (list,),
    "stash_restored": (bool, str),  # true | false | "n/a"
    "stash_entry_ref": (str, type(None)),
    "warnings": (list,),
    "errors": (list,),
}

_REPORT_ALLOWED_STATUS = {"ok", "partial", "failed"}
_REPORT_ALLOWED_STASH_RESTORED = {"n/a"}
_PARENT_ISSUE_STATUS_KEYS = {
    "parent_issue_number",
    "all_children_closed",
    "recommended_action",
}
_PARENT_ISSUE_STATUS_ALLOWED_ACTIONS = {"close", "keep_open", "n/a"}

# Nested list-item schemas (P1: report validator gaps — Issue #1733 PR #1947
# fix_delta). ``dict`` for closed required-key/type validation of a single
# list element; a ``None`` value in the source dict means "type-only, no
# further nested closed-key validation" (used for scalar-typed sub-fields).
_FOLLOW_UP_ISSUE_REQUEST_KEYS: dict[str, tuple[type, ...]] = {
    "title": (str,),
    "issue_kind": (str,),
    "severity": (str,),
    "source": (dict,),
    "dedupe_key": (str,),
    "desired_destination": (str,),
    "validated_scope_delta": (str,),
    "origin_skill": (str,),
    "labels": (list,),
}
_FOLLOW_UP_ISSUE_REQUEST_SOURCE_KEYS: dict[str, tuple[type, ...]] = {
    "kind": (str,),
    "url": (str,),
    "note_id": (str,),
}
_SUPERSEDED_PR_KEYS: dict[str, tuple[type, ...]] = {
    "number": (int,),
    "title": (str,),
    "url": (str,),
}


class ValidationResult:
    def __init__(self, valid: bool, errors: list[str] | None = None):
        self.valid = valid
        self.errors = errors or []

    def __bool__(self) -> bool:
        return self.valid

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"ValidationResult(valid={self.valid}, errors={self.errors})"


def validate_report_v1(data: Any) -> ValidationResult:
    """Closed-key structural validator for POST_MERGE_CLEANUP_REPORT_V1.

    Validates required keys, types, unknown-key rejection, and a small set
    of enum constraints. Does not attempt semantic validation of routing
    decisions (that is the orchestrator's responsibility).
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ValidationResult(False, [f"top-level payload must be a mapping, got {type(data).__name__}"])

    unknown_keys = sorted(set(data.keys()) - set(_REPORT_REQUIRED_KEYS.keys()))
    if unknown_keys:
        errors.append(f"unknown top-level key(s): {unknown_keys}")

    missing_keys = sorted(set(_REPORT_REQUIRED_KEYS.keys()) - set(data.keys()))
    if missing_keys:
        errors.append(f"missing required key(s): {missing_keys}")

    for key, expected_types in _REPORT_REQUIRED_KEYS.items():
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, expected_types):
            type_names = " | ".join(t.__name__ for t in expected_types)
            errors.append(f"key '{key}' expected type ({type_names}), got {type(value).__name__}")

    if "status" in data and isinstance(data["status"], str) and data["status"] not in _REPORT_ALLOWED_STATUS:
        errors.append(f"key 'status' has invalid value: {data['status']!r}")

    if "stash_restored" in data and isinstance(data["stash_restored"], str):
        if data["stash_restored"] not in _REPORT_ALLOWED_STASH_RESTORED:
            errors.append(f"key 'stash_restored' string value must be 'n/a', got {data['stash_restored']!r}")

    if "parent_issue_status" in data and isinstance(data["parent_issue_status"], dict):
        errors.extend(_validate_parent_issue_status(data["parent_issue_status"]))

    if "follow_up_issue_requests" in data and isinstance(data["follow_up_issue_requests"], list):
        errors.extend(
            _validate_list_items(
                data["follow_up_issue_requests"],
                "follow_up_issue_requests",
                _FOLLOW_UP_ISSUE_REQUEST_KEYS,
            )
        )
        for index, item in enumerate(data["follow_up_issue_requests"]):
            if isinstance(item, dict) and isinstance(item.get("source"), dict):
                errors.extend(
                    _validate_closed_dict(
                        item["source"],
                        f"follow_up_issue_requests[{index}].source",
                        _FOLLOW_UP_ISSUE_REQUEST_SOURCE_KEYS,
                    )
                )

    if "superseded_prs" in data and isinstance(data["superseded_prs"], list):
        errors.extend(_validate_list_items(data["superseded_prs"], "superseded_prs", _SUPERSEDED_PR_KEYS))

    return ValidationResult(len(errors) == 0, errors)


def _is_strict_int(value: Any) -> bool:
    """``bool`` is a subclass of ``int`` in Python; a literal ``true``/``false``
    must not silently pass an ``int``-typed field (e.g. ``parent_issue_number``)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _validate_closed_dict(value: dict, label: str, schema: dict[str, tuple[type, ...]]) -> list[str]:
    """Closed-key structural validation for a nested dict (required keys,
    unknown-key rejection, per-key type checking). Shared by
    ``parent_issue_status`` and nested list-item sub-objects."""
    errors: list[str] = []
    unknown = sorted(set(value.keys()) - set(schema.keys()))
    if unknown:
        errors.append(f"{label}: unknown key(s): {unknown}")
    missing = sorted(set(schema.keys()) - set(value.keys()))
    if missing:
        errors.append(f"{label}: missing key(s): {missing}")
    for key, expected_types in schema.items():
        if key not in value:
            continue
        item_value = value[key]
        if not isinstance(item_value, expected_types):
            type_names = " | ".join(t.__name__ for t in expected_types)
            errors.append(f"{label}.{key} expected type ({type_names}), got {type(item_value).__name__}")
    return errors


def _validate_list_items(items: list, label: str, schema: dict[str, tuple[type, ...]]) -> list[str]:
    errors: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{label}[{index}]: expected a mapping, got {type(item).__name__}")
            continue
        errors.extend(_validate_closed_dict(item, f"{label}[{index}]", schema))
    return errors


def _validate_parent_issue_status(value: dict) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(value.keys()) - _PARENT_ISSUE_STATUS_KEYS)
    if unknown:
        errors.append(f"parent_issue_status: unknown key(s): {unknown}")
    missing = sorted(_PARENT_ISSUE_STATUS_KEYS - set(value.keys()))
    if missing:
        errors.append(f"parent_issue_status: missing key(s): {missing}")
    if "parent_issue_number" in value and not _is_strict_int(value["parent_issue_number"]):
        errors.append(
            "parent_issue_status.parent_issue_number expected type (int), "
            f"got {type(value['parent_issue_number']).__name__}"
        )
    elif "parent_issue_number" in value and value["parent_issue_number"] <= 0:
        errors.append(
            f"parent_issue_status.parent_issue_number must be a positive integer, got {value['parent_issue_number']!r}"
        )
    if "all_children_closed" in value and not _is_strict_bool(value["all_children_closed"]):
        errors.append(
            "parent_issue_status.all_children_closed expected type (bool), "
            f"got {type(value['all_children_closed']).__name__}"
        )
    action = value.get("recommended_action")
    if action is not None and action not in _PARENT_ISSUE_STATUS_ALLOWED_ACTIONS:
        errors.append(f"parent_issue_status.recommended_action has invalid value: {action!r}")
    return errors


def validate_report_yaml(text: str) -> ValidationResult:
    """Parse and validate raw YAML text for POST_MERGE_CLEANUP_REPORT_V1.

    Malformed YAML is reported as a validation failure (not an exception).
    When the payload uses the wrapper form (``{POST_MERGE_CLEANUP_REPORT_V1:
    {...}}``), any sibling key alongside the wrapper key at the top level is
    rejected — a wrapper-only unwrap must not silently ignore
    attacker-controlled sibling keys (P1, Issue #1733 PR #1947 fix_delta).
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        return ValidationResult(False, [f"pyyaml unavailable: {exc}"])

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ValidationResult(False, [f"malformed YAML: {exc}"])

    if isinstance(data, dict) and SCHEMA_REPORT_V1 in data:
        sibling_keys = sorted(set(data.keys()) - {SCHEMA_REPORT_V1})
        if sibling_keys:
            return ValidationResult(
                False, [f"unknown sibling key(s) alongside wrapper key '{SCHEMA_REPORT_V1}': {sibling_keys}"]
            )
        data = data[SCHEMA_REPORT_V1]

    return validate_report_v1(data)


# ---------------------------------------------------------------------------
# Boundary checks (AC2 / AC3 / AC4 / AC5 / AC7 / AC9 / AC11)
# ---------------------------------------------------------------------------


def extract_frontmatter(text: str) -> dict[str, Any]:
    """Extract the YAML frontmatter block of a Claude agent Markdown file."""
    if not text.startswith("---\n"):
        return {}
    _, _, remainder = text.partition("---\n")
    frontmatter, _, _ = remainder.partition("\n---\n")
    import yaml

    parsed = yaml.safe_load(frontmatter)
    return parsed if isinstance(parsed, dict) else {}


def check_worker_skills_frontmatter(worker_md_path: Path) -> ValidationResult:
    """AC2: post-merge-cleanup-worker.md frontmatter `skills` is exactly
    one entry: post-merge-cleanup-executor."""
    text = worker_md_path.read_text(encoding="utf-8")
    fm = extract_frontmatter(text)
    skills = fm.get("skills")
    if not isinstance(skills, list):
        return ValidationResult(False, [f"frontmatter 'skills' missing or not a list: {skills!r}"])
    if skills != ["post-merge-cleanup-executor"]:
        detail = f"frontmatter 'skills' must be exactly ['post-merge-cleanup-executor'], got {skills!r}"
        return ValidationResult(False, [detail])
    disallowed = fm.get("disallowedTools")
    if not isinstance(disallowed, list) or "Agent" not in disallowed:
        return ValidationResult(False, [f"frontmatter 'disallowedTools' must include 'Agent', got {disallowed!r}"])
    return ValidationResult(True)


def check_codex_skill_surface(toml_path: Path) -> ValidationResult:
    """AC3: post-merge-cleanup-worker.toml repo_local_skill_surface points at
    the executor thin wrapper."""
    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)
    instructions = data.get("developer_instructions", "")
    match = re.search(r"repo_local_skill_surface:\s*(\S+)", instructions)
    if not match:
        return ValidationResult(False, ["repo_local_skill_surface not found in developer_instructions"])
    surface = match.group(1).strip()
    expected = ".agents/skills/post-merge-cleanup-executor/SKILL.md"
    if surface != expected:
        return ValidationResult(False, [f"repo_local_skill_surface expected {expected!r}, got {surface!r}"])
    return ValidationResult(True)


# ---------------------------------------------------------------------------
# Blocker 2 (Issue #1733 PR #1947 fix_delta): worker canonical-procedure
# instruction hygiene — the Codex worker's ``developer_instructions`` and the
# Claude worker's frontmatter ``description`` must both name
# ``post-merge-cleanup-executor`` as the sole canonical procedure, not the
# orchestrator Skill (``post-merge-cleanup``).
# ---------------------------------------------------------------------------

_STALE_CODEX_CANONICAL_MARKER = "skill を正本とする"
_CODEX_CANONICAL_REQUIRED_MARKER = "唯一の canonical procedure とする"
_CODEX_ORCHESTRATOR_FORBID_MARKER = "orchestrator Skill（`post-merge-cleanup`）の読込み・実行は禁止する"

_STALE_CLAUDE_DESC_PROCEDURE_MARKER = "post-merge-cleanup` skill の Procedure を実行し"
_CLAUDE_DESC_EXECUTOR_REQUIRED_MARKER = "post-merge-cleanup-executor` skill の Procedure を実行し"


def check_codex_no_stale_canonical(toml_path: Path) -> ValidationResult:
    """Blocker 2: Codex worker developer_instructions must name
    post-merge-cleanup-executor as the sole canonical procedure and must not
    retain the stale ``post-merge-cleanup`` skill を正本とする instruction."""
    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)
    instructions = data.get("developer_instructions", "")
    errors: list[str] = []
    if _STALE_CODEX_CANONICAL_MARKER in instructions:
        errors.append(f"developer_instructions retains stale canonical marker: {_STALE_CODEX_CANONICAL_MARKER!r}")
    if _CODEX_CANONICAL_REQUIRED_MARKER not in instructions:
        errors.append(
            f"developer_instructions missing explicit canonical marker: {_CODEX_CANONICAL_REQUIRED_MARKER!r}"
        )
    if _CODEX_ORCHESTRATOR_FORBID_MARKER not in instructions:
        errors.append(
            "developer_instructions missing explicit orchestrator-load prohibition: "
            f"{_CODEX_ORCHESTRATOR_FORBID_MARKER!r}"
        )
    return ValidationResult(len(errors) == 0, errors)


def check_claude_worker_description_no_stale_procedure(worker_md_path: Path) -> ValidationResult:
    """Blocker 2: Claude worker frontmatter description must describe
    executing post-merge-cleanup-executor, not the old orchestrator
    Procedure."""
    text = worker_md_path.read_text(encoding="utf-8")
    fm = extract_frontmatter(text)
    description = fm.get("description", "")
    errors: list[str] = []
    if _STALE_CLAUDE_DESC_PROCEDURE_MARKER in description:
        errors.append(
            f"description retains stale orchestrator-Procedure marker: {_STALE_CLAUDE_DESC_PROCEDURE_MARKER!r}"
        )
    if _CLAUDE_DESC_EXECUTOR_REQUIRED_MARKER not in description:
        errors.append(f"description missing executor-Procedure marker: {_CLAUDE_DESC_EXECUTOR_REQUIRED_MARKER!r}")
    return ValidationResult(len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Blocker 3 (Issue #1733 PR #1947 fix_delta): the orchestrator Skill text
# must not describe parent-issue close as firing on mere presence of
# recommended_action (a required field), and must explicitly gate close on
# recommended_action == "close" AND all_children_closed AND a valid
# parent_issue_number.
# ---------------------------------------------------------------------------

_AMBIGUOUS_PARENT_CLOSE_MARKER = "recommended_action` あり → `gh issue close`"
_EXPLICIT_PARENT_CLOSE_MARKER = 'recommended_action == "close"'


def check_parent_close_condition_explicit(orchestrator_skill_path: Path) -> ValidationResult:
    text = orchestrator_skill_path.read_text(encoding="utf-8")
    errors: list[str] = []
    if _AMBIGUOUS_PARENT_CLOSE_MARKER in text:
        errors.append(
            f"orchestrator still contains ambiguous parent-close phrasing: {_AMBIGUOUS_PARENT_CLOSE_MARKER!r}"
        )
    if _EXPLICIT_PARENT_CLOSE_MARKER not in text:
        errors.append(f"orchestrator missing explicit parent-close condition: {_EXPLICIT_PARENT_CLOSE_MARKER!r}")
    return ValidationResult(len(errors) == 0, errors)


# Phrases that indicate main-thread orchestration execution (not candidate
# listing). Presence of these inside the *executor* Skill body is a boundary
# violation (AC5). Absence of the deterministic 8-step procedure heading in
# the *orchestrator* Skill body is required by AC4.
_ORCHESTRATION_EXECUTION_MARKERS = (
    "post-merge-cleanup-worker` SubAgent を Agent tool で起動する",
    "issue-creator SubAgent に委譲して create-issue skill 経由で起票",
    "gh pr close` / `gh pr comment` を実行",
    "gh issue close` を実行",
)

_PROCEDURE_HEADING = "## Procedure（機械的実行手順・8 ステップ）"
_LEGACY_PROCEDURE_HEADING = "## Procedure（SubAgent 側の実行内容）"

# Match an actual command invocation at the start of a (possibly indented,
# possibly ``$``-prefixed) line — e.g. inside a fenced bash code block —
# rather than a prose mention (e.g. inside a prohibition sentence such as
# "`codex exec` の起動を禁止する"). This avoids self-poisoning the checker
# with its own documented prohibition text.
_EXTERNAL_AGENT_CLI_INVOCATION_RE = re.compile(
    r"(?m)^[ \t]*\$?[ \t]*(codex exec|claude -p)\b"
)
_GH_ISSUE_CREATE_INVOCATION_RE = re.compile(r"(?m)^[ \t]*\$?[ \t]*gh issue create\b")

# P1 (Issue #1733 PR #1947 fix_delta): the line-start-only regex above misses
# `env codex exec`, `command codex exec`, absolute-path invocations
# (`/usr/bin/codex exec`), and invocations wrapped inside `bash -lc '...'`.
# Broaden detection to scan *inside fenced code blocks only* (never prose,
# to avoid self-poisoning on this file's own documented prohibition
# sentences, e.g. "`codex exec` の起動を禁止する") for the binary name
# preceded by a shell-token boundary (start, whitespace, slash, quote,
# paren, pipe, ampersand) and optional `env `/`command `/path prefixes.
# This is explicitly defense-in-depth, secondary to the AC12 runtime-trace
# assertions (Blocker 1) per the reviewer's guidance.
_FENCED_CODE_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_-]*)\n(.*?)```", re.S)
_AGENT_CLI_BINARY_IN_CODE_RE = re.compile(
    r"(?:^|[\s/'\"();|&])(?:env\s+|command\s+)*(?:\S*/)?(codex\s+exec|claude\s+-p)\b"
)


def _extract_fenced_code_block_text(text: str) -> str:
    return "\n".join(m.group(1) for m in _FENCED_CODE_BLOCK_RE.finditer(text))


def check_orchestrator_no_procedure(orchestrator_skill_path: Path) -> ValidationResult:
    """AC4: orchestrator SKILL.md does not contain the worker's 8-step
    deterministic Procedure body."""
    text = orchestrator_skill_path.read_text(encoding="utf-8")
    errors = []
    if _PROCEDURE_HEADING in text or _LEGACY_PROCEDURE_HEADING in text:
        errors.append("orchestrator still contains the worker 8-step Procedure heading")
    if "### 1. 未コミット変更と未追跡ファイルを分類" in text:
        errors.append("orchestrator still contains executor step 1 body")
    if "TEMP_CLEANUP_SAFETY_RULES_V1:" in text:
        errors.append("orchestrator still contains TEMP_CLEANUP_SAFETY_RULES_V1 (executor-only)")
    return ValidationResult(len(errors) == 0, errors)


def check_executor_no_orchestration(executor_skill_path: Path) -> ValidationResult:
    """AC5: executor SKILL.md does not contain worker-launch / follow-up
    creation / parent-close / superseded-PR-close execution instructions."""
    text = executor_skill_path.read_text(encoding="utf-8")
    errors = [marker for marker in _ORCHESTRATION_EXECUTION_MARKERS if marker in text]
    if "spawn_agent:" in text:
        errors.append("executor contains a spawn_agent dispatch block")
    formatted_errors = [f"forbidden orchestration marker present: {m}" for m in errors] if errors else []
    return ValidationResult(len(errors) == 0, formatted_errors)


def check_no_child_policy(worker_md_path: Path, executor_skill_path: Path) -> ValidationResult:
    """AC7: no-child policy regression — Agent tool remains disallowed and no
    bash-based external agent CLI invocation is present in the worker's
    executable procedure surface."""
    errors: list[str] = []
    worker_text = worker_md_path.read_text(encoding="utf-8")
    fm = extract_frontmatter(worker_text)
    disallowed = fm.get("disallowedTools")
    if not isinstance(disallowed, list) or "Agent" not in disallowed:
        errors.append("post-merge-cleanup-worker.md disallowedTools must include 'Agent'")

    executor_text = executor_skill_path.read_text(encoding="utf-8")
    combined = worker_text + "\n" + executor_text
    invocation_match = _EXTERNAL_AGENT_CLI_INVOCATION_RE.search(combined)
    if invocation_match:
        errors.append(f"forbidden external agent CLI invocation pattern present: {invocation_match.group(0)!r}")

    code_block_text = _extract_fenced_code_block_text(combined)
    broadened_match = _AGENT_CLI_BINARY_IN_CODE_RE.search(code_block_text)
    if broadened_match:
        errors.append(
            f"forbidden external agent CLI invocation pattern present in code block: {broadened_match.group(0)!r}"
        )

    if "no-child policy" not in executor_text and "no_child" not in executor_text.replace(" ", "_").lower():
        errors.append("executor SKILL.md does not declare a no-child policy section")

    return ValidationResult(len(errors) == 0, errors)


def check_followup_routing_ownership(orchestrator_skill_path: Path, executor_skill_path: Path) -> ValidationResult:
    """AC9: follow-up Issue routing (creation + dedupe) is only performed by
    the orchestrator; the executor never calls gh issue create directly."""
    errors: list[str] = []
    orchestrator_text = orchestrator_skill_path.read_text(encoding="utf-8")
    executor_text = executor_skill_path.read_text(encoding="utf-8")

    if "issue-creator SubAgent に委譲して create-issue skill 経由で起票" not in orchestrator_text:
        errors.append("orchestrator missing the follow-up materialization ownership instruction")

    if _GH_ISSUE_CREATE_INVOCATION_RE.search(executor_text):
        errors.append("executor SKILL.md must not invoke gh issue create directly")

    return ValidationResult(len(errors) == 0, errors)


def check_boundary_docs_catalog(docs_path: Path) -> ValidationResult:
    """AC11: docs/dev/agent-skill-boundaries.md catalog reflects the new
    post-merge-cleanup-executor Skill and worker preload surface."""
    text = docs_path.read_text(encoding="utf-8")
    errors = []
    if "post-merge-cleanup-executor" not in text:
        errors.append("agent-skill-boundaries.md does not mention post-merge-cleanup-executor")
    preload_marker_a = "`post-merge-cleanup`、`post-merge-cleanup-executor`"
    preload_marker_b = "post-merge-cleanup-executor` を実行本文として参照する"
    if preload_marker_a not in text and preload_marker_b not in text:
        errors.append("agent-skill-boundaries.md does not document worker preload of post-merge-cleanup-executor")
    return ValidationResult(len(errors) == 0, errors)


def check_agent_parity_strict(repo_root: Path = REPO_ROOT) -> ValidationResult:
    """AC10: scripts/check_claude_codex_agent_parity.py --strict does not
    regress, and PARITY_AGENTS is not silently extended with
    post-merge-cleanup-worker (Out of Scope)."""
    parity_script = repo_root / "scripts" / "check_claude_codex_agent_parity.py"
    parity_text = parity_script.read_text(encoding="utf-8")
    if "post-merge-cleanup-worker" in re.search(r"PARITY_AGENTS = \{[^}]*\}", parity_text, re.S).group(0):
        return ValidationResult(False, ["PARITY_AGENTS must not include post-merge-cleanup-worker (Out of Scope)"])

    proc = subprocess.run(
        [sys.executable, str(parity_script), "--strict"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        detail = f"check_claude_codex_agent_parity.py --strict failed: {proc.stdout}\n{proc.stderr}"
        return ValidationResult(False, [detail])
    return ValidationResult(True)


# ---------------------------------------------------------------------------
# AC12: runtime_smoke_evidence (preflight-scope: runtime_only)
#
# Blocker 1 (Issue #1733 PR #1947 fix_delta, iteration 5): the original
# implementation picked the lexicographically-first
# ``artifacts/runtime-smoke/*/summary.md`` via glob and only checked 4
# generic health fields (exit_code, timed_out, expected_markers_missing,
# errors). That was a false-positive-prone design: it could silently accept
# a stale/unrelated success artifact, and it never checked *which*
# worker/agent ran, *which* Skill it loaded, whether any child/spawn or
# self-restart/orchestration-action events occurred, or whether the artifact
# was produced against the current HEAD.
#
# Iteration 6 regression (fixed in iteration 7, then genuinely resolved in
# iteration 8): iteration 5's rewrite required an explicit ``--artifact-path``
# for *any* PASS, which meant the Issue's own literal AC12 Verification
# Command (which never passes ``--artifact-path``) could structurally never
# PASS -- only SKIP. It also hard-failed the check whenever any of the 11
# structured fields were absent, even though the shared harness
# (``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``) did not yet
# emit them at the time -- so a real harness-produced artifact could never
# PASS either, even with ``--artifact-path`` supplied.
#
# Iteration 7 (superseded by iteration 8 below) temporarily downgraded the
# structured fields to best-effort (WARNING on absence, not FAIL) as a
# stop-gap, because the harness itself was outside Issue #1733's Allowed
# Paths at that time and structurally could not emit them.
#
# Iteration 8 (Issue #1733 Scope Delta, 2026-08-02 owner-approved extension
# of Allowed Paths to include the harness): the harness
# (``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``) now genuinely
# derives and emits all 10 structured fields (``tested_head``,
# ``runtime_version``, ``requested_agent_type``, ``effective_agent_type``,
# ``loaded_skills``, ``child_spawn_event_count``, ``spawn_events``,
# ``self_restart_event_count``, ``orchestration_action_count``,
# ``prompt_sha256``) for the structured lane, so the iteration 7 best-effort
# downgrade is reverted: this checker again HARD-REQUIRES and HARD-VALIDATES
# all of them (missing -> FAIL, not a WARNING), matching the original
# iteration 6 intent. When ``--artifact-path`` is omitted, this still falls
# back to a single, fixed, deterministic default location
# (``_DEFAULT_ARTIFACT_PATH`` below) -- never a glob-first-match across
# multiple candidate directories -- and SKIP (77) when that default does not
# exist, preserving fail-closed behavior for environments with no evidence at
# all (the iteration 7 fix for the AC12-VC-can-never-PASS regression is
# retained; only the field-strictness downgrade is reverted).
# ---------------------------------------------------------------------------

# Single, fixed, deterministic default artifact location used when
# ``--artifact-path`` is omitted (i.e. the Issue's literal AC12 VC form).
# This is intentionally a single well-known path -- not a glob across
# multiple ``artifacts/runtime-smoke/*/`` candidates -- so there is never
# "which of several candidates" ambiguity (the exact false-positive source
# Blocker 1 objected to).
_DEFAULT_ARTIFACT_PATH = Path("artifacts/runtime-smoke/codex-structured/summary.md")

_REQUIRED_STRUCTURED_SMOKE_FIELDS = (
    "tested_head",
    "runtime_version",
    "requested_agent_type",
    "effective_agent_type",
    "loaded_skills",
    "child_spawn_event_count",
    "spawn_events",
    "self_restart_event_count",
    "orchestration_action_count",
    "prompt_sha256",
    "postcondition_unexpected_changes",
)

_EXPECTED_LOADED_SKILLS = ["post-merge-cleanup-executor"]


def find_runtime_smoke_summary(repo_root: Path = REPO_ROOT) -> Path | None:
    """Deprecated glob-first-match lookup, retained only as a diagnostic
    helper (e.g. to suggest a candidate path in error output). Never used to
    silently select evidence for the CLI check — Blocker 1 requires an
    explicit ``--artifact-path``."""
    smoke_dir = repo_root / "artifacts" / "runtime-smoke"
    if not smoke_dir.is_dir():
        return None
    candidates = sorted(smoke_dir.glob("*/summary.md"))
    return candidates[0] if candidates else None


def _parse_summary_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- ") and ":" in line:
            key, _, value = line[2:].partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _parse_field_value(raw: str | None) -> Any:
    """Best-effort structural parse of a summary.md field value (e.g. Python
    ``repr()`` output of a list/int/bool/None). Falls back to the raw string
    when it is not a literal (e.g. a bare sha/string)."""
    if raw is None:
        return None
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _current_head(repo_root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def check_runtime_smoke_evidence(repo_root: Path = REPO_ROOT, artifact_path: str | None = None) -> int:
    """Return an exit code (0/1/77) per docs/dev/runtime-verification-policy.md.

    When ``artifact_path`` is omitted (the Issue's literal AC12 VC form),
    the check falls back to the single fixed default location
    (``_DEFAULT_ARTIFACT_PATH``). SKIP (77) is returned when the resolved
    path does not exist — this is never promoted to PASS. When an artifact
    exists, it is hard-validated against both the legacy generic-health
    fields and the structured worker-identity / Skill-load / spawn-event /
    tested-head fields (Blocker 1, Issue #1733 PR #1947 fix_delta; iteration 8
    reverted the iteration 7 best-effort/WARNING-only downgrade now that the
    harness genuinely emits all 10 structured fields — a missing or
    unresolved (``None``) structured field is a hard FAIL, not a warning).
    """
    if not artifact_path:
        summary_path = repo_root / _DEFAULT_ARTIFACT_PATH
        if not summary_path.is_file():
            print(
                f"SKIP: no --artifact-path given and fixed default path does not exist: {summary_path} "
                "(this is a single fixed well-known location, not a glob across multiple candidates)",
                file=sys.stderr,
            )
            return EXIT_SKIP
    else:
        summary_path = Path(artifact_path)
        if not summary_path.is_absolute():
            summary_path = repo_root / summary_path
        if not summary_path.is_file():
            print(f"SKIP: --artifact-path does not exist: {summary_path}", file=sys.stderr)
            return EXIT_SKIP

    text = summary_path.read_text(encoding="utf-8")
    fields = _parse_summary_fields(text)

    errors: list[str] = []

    # Legacy generic-health fields (retained, hard-required).
    if fields.get("exit_code") not in {"0"}:
        errors.append(f"exit_code={fields.get('exit_code')!r} (expected 0)")
    if fields.get("timed_out") not in {"False", "false"}:
        errors.append(f"timed_out={fields.get('timed_out')!r} (expected False)")
    if fields.get("expected_markers_missing") not in {"[]"}:
        errors.append(f"expected_markers_missing={fields.get('expected_markers_missing')!r} (expected [])")
    if fields.get("errors") not in {"[]"}:
        errors.append(f"errors={fields.get('errors')!r} (expected [])")
    if fields.get("postcondition_unexpected_changes") not in {"[]"}:
        errors.append(
            f"postcondition_unexpected_changes={fields.get('postcondition_unexpected_changes')!r} (expected [])"
        )

    # Structured fields (Blocker 1, iteration 8: hard-required again). A
    # field is missing either because the "- key: value" line is absent
    # entirely, or because it is present with the literal value ``None``
    # (the harness's own documented "could not be honestly derived" marker,
    # e.g. loaded_skills when --agent-type was not supplied). Both count as
    # a hard FAIL now — a real AC12-grade harness invocation must supply
    # ``--agent-type`` and run in the linked worktree so every field
    # genuinely resolves.
    missing_structured_fields = [
        key for key in _REQUIRED_STRUCTURED_SMOKE_FIELDS
        if key not in fields or _parse_field_value(fields.get(key)) is None
    ]
    if missing_structured_fields:
        errors.append(f"missing or unresolved structured field(s): {missing_structured_fields}")

    if _parse_field_value(fields.get("tested_head")) is not None:
        expected_head = _current_head(repo_root)
        if expected_head is None:
            errors.append("could not resolve current repository HEAD via git rev-parse HEAD")
        elif fields["tested_head"] != expected_head:
            errors.append(f"tested_head={fields['tested_head']!r} does not match current HEAD {expected_head!r}")

    if fields.get("loaded_skills") is not None:
        loaded_skills = _parse_field_value(fields["loaded_skills"])
        if loaded_skills != _EXPECTED_LOADED_SKILLS:
            errors.append(f"loaded_skills={loaded_skills!r} (expected {_EXPECTED_LOADED_SKILLS!r})")

    if fields.get("child_spawn_event_count") is not None:
        count = _parse_field_value(fields["child_spawn_event_count"])
        if count != 0:
            errors.append(f"child_spawn_event_count={count!r} (expected 0)")

    if fields.get("self_restart_event_count") is not None:
        count = _parse_field_value(fields["self_restart_event_count"])
        if count != 0:
            errors.append(f"self_restart_event_count={count!r} (expected 0)")

    if fields.get("orchestration_action_count") is not None:
        count = _parse_field_value(fields["orchestration_action_count"])
        if count != 0:
            errors.append(f"orchestration_action_count={count!r} (expected 0)")

    if fields.get("requested_agent_type") is not None and fields.get("effective_agent_type") is not None:
        if fields["requested_agent_type"] != fields["effective_agent_type"]:
            errors.append(
                "requested_agent_type "
                f"{fields['requested_agent_type']!r} != effective_agent_type {fields['effective_agent_type']!r} "
                "(possible self-restart / runtime mismatch)"
            )

    if fields.get("prompt_sha256") is not None:
        prompt_sha256 = fields["prompt_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256 or ""):
            errors.append(f"prompt_sha256={prompt_sha256!r} is not a 64-char hex sha256 digest")

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return EXIT_FAIL

    print(f"OK: runtime smoke evidence verified at {summary_path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _assert_current_repository(repo_root: Path) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        print("FAIL: unable to resolve current repository via git rev-parse --show-toplevel", file=sys.stderr)
        sys.exit(EXIT_FAIL)
    resolved = Path(proc.stdout.strip()).resolve()
    expected = repo_root.resolve()
    mismatched = resolved != expected and expected not in resolved.parents and resolved not in expected.parents
    if mismatched:
        # Allow the case where repo_root itself is a linked worktree whose
        # toplevel is the worktree path (common case, matches).
        if resolved != expected:
            print(
                f"FAIL: current repository toplevel {resolved} does not match expected repo_root {repo_root.resolve()}",
                file=sys.stderr,
            )
            sys.exit(EXIT_FAIL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assert-current-repository", action="store_true")
    parser.add_argument("--check", choices=["runtime_smoke_evidence"], required=True)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--artifact-path",
        default=None,
        help=(
            "explicit path to a worktree-agent-runtime-smoke summary.md "
            "(optional; when omitted, falls back to the single fixed default "
            f"path {_DEFAULT_ARTIFACT_PATH} — no glob-first-match auto-discovery)"
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()

    if args.assert_current_repository:
        _assert_current_repository(repo_root)

    if args.check == "runtime_smoke_evidence":
        return check_runtime_smoke_evidence(repo_root, artifact_path=args.artifact_path)

    return EXIT_FAIL  # pragma: no cover - argparse choices exhaustive


if __name__ == "__main__":
    sys.exit(main())
