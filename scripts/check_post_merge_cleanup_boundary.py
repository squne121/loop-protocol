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

    return ValidationResult(len(errors) == 0, errors)


def _validate_parent_issue_status(value: dict) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(value.keys()) - _PARENT_ISSUE_STATUS_KEYS)
    if unknown:
        errors.append(f"parent_issue_status: unknown key(s): {unknown}")
    missing = sorted(_PARENT_ISSUE_STATUS_KEYS - set(value.keys()))
    if missing:
        errors.append(f"parent_issue_status: missing key(s): {missing}")
    action = value.get("recommended_action")
    if action is not None and action not in _PARENT_ISSUE_STATUS_ALLOWED_ACTIONS:
        errors.append(f"parent_issue_status.recommended_action has invalid value: {action!r}")
    return errors


def validate_report_yaml(text: str) -> ValidationResult:
    """Parse and validate raw YAML text for POST_MERGE_CLEANUP_REPORT_V1.

    Malformed YAML is reported as a validation failure (not an exception).
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


# Phrases that indicate main-thread orchestration execution (not candidate
# listing). Presence of these inside the *executor* Skill body is a boundary
# violation (AC5). Absence of the deterministic 8-step procedure heading in
# the *orchestrator* Skill body is required by AC4.
_ORCHESTRATION_EXECUTION_MARKERS = (
    "post-merge-cleanup-worker` SubAgent を Agent tool で起動する",
    "issue-author SubAgent に委譲して create-issue skill 経由で起票",
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

    if "no-child policy" not in executor_text and "no_child" not in executor_text.replace(" ", "_").lower():
        errors.append("executor SKILL.md does not declare a no-child policy section")

    return ValidationResult(len(errors) == 0, errors)


def check_followup_routing_ownership(orchestrator_skill_path: Path, executor_skill_path: Path) -> ValidationResult:
    """AC9: follow-up Issue routing (creation + dedupe) is only performed by
    the orchestrator; the executor never calls gh issue create directly."""
    errors: list[str] = []
    orchestrator_text = orchestrator_skill_path.read_text(encoding="utf-8")
    executor_text = executor_skill_path.read_text(encoding="utf-8")

    if "issue-author SubAgent に委譲して create-issue skill 経由で起票" not in orchestrator_text:
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
# ---------------------------------------------------------------------------


def find_runtime_smoke_summary(repo_root: Path = REPO_ROOT) -> Path | None:
    smoke_dir = repo_root / "artifacts" / "runtime-smoke"
    if not smoke_dir.is_dir():
        return None
    candidates = sorted(smoke_dir.glob("*/summary.md"))
    return candidates[0] if candidates else None


def check_runtime_smoke_evidence(repo_root: Path = REPO_ROOT) -> int:
    """Return an exit code (0/1/77) per docs/dev/runtime-verification-policy.md.

    SKIP (77) is returned when no runtime evidence directory exists (the
    execution environment did not run worktree-agent-runtime-smoke) — this
    is never promoted to PASS. When evidence exists, it is inspected for a
    clean, non-timed-out, error-free structured run with no missing markers.
    """
    summary_path = find_runtime_smoke_summary(repo_root)
    if summary_path is None:
        print("SKIP: no worktree-agent-runtime-smoke evidence found under artifacts/runtime-smoke/", file=sys.stderr)
        return EXIT_SKIP

    text = summary_path.read_text(encoding="utf-8")
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- ") and ":" in line:
            key, _, value = line[2:].partition(":")
            fields[key.strip()] = value.strip()

    errors: list[str] = []
    if fields.get("exit_code") not in {"0"}:
        errors.append(f"exit_code={fields.get('exit_code')!r} (expected 0)")
    if fields.get("timed_out") not in {"False", "false"}:
        errors.append(f"timed_out={fields.get('timed_out')!r} (expected False)")
    if fields.get("expected_markers_missing") not in {"[]"}:
        errors.append(f"expected_markers_missing={fields.get('expected_markers_missing')!r} (expected [])")
    if fields.get("errors") not in {"[]"}:
        errors.append(f"errors={fields.get('errors')!r} (expected [])")

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
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()

    if args.assert_current_repository:
        _assert_current_repository(repo_root)

    if args.check == "runtime_smoke_evidence":
        return check_runtime_smoke_evidence(repo_root)

    return EXIT_FAIL  # pragma: no cover - argparse choices exhaustive


if __name__ == "__main__":
    sys.exit(main())
