#!/usr/bin/env python3
"""Fail-closed Codex execpolicy matrix runner for CI (Issue #1150).

This script is the SSOT for the required execpolicy gate. It verifies the pinned
Codex CLI version/probe, evaluates the execpolicy JSON contract strictly, and
cross-checks the runtime PreToolUse hook decision against the expected lane contract.

The script writes a JSON artifact on every run (including failures) so the CI job
can upload version / argv / expected / actual / raw JSON / return code evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "codex_execpolicy_matrix_v1"
ALLOWED_DECISIONS = {"allow", "prompt", "forbidden"}
ALLOWED_TOP_LEVEL_KEYS = {"decision", "matchedRules"}

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_MAIN_BRANCH_GUARD_SH = REPO_ROOT / ".codex" / "hooks" / "local_main_branch_guard.sh"
WORKTREE_SCOPE_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"
RULES_DEFAULT = REPO_ROOT / ".codex" / "rules" / "default.rules"
WORKTREE_REL = Path(".claude/worktrees/issue-1-x")
BRANCH_NAME = "issue-1-x"
ISSUE_NUMBER = "1"


class MatrixError(RuntimeError):
    """Raised for any contract drift or verification failure."""


@dataclass(frozen=True)
class FixtureRepo:
    """Temporary git repo + linked issue worktree used for hook-chain replay."""

    root: Path
    worktree: Path
    branch: str
    issue_number: str


def _load_module(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MatrixError(f"failed to import module at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args: str, cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def build_fixture_repo(base_dir: Path, issue_number: str = ISSUE_NUMBER, slug: str = "x") -> FixtureRepo:
    """Create a temporary repo with a real linked worktree on main."""
    root = base_dir / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)
    branch = f"issue-{issue_number}-{slug}"
    worktree = root / ".claude" / "worktrees" / branch
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", branch, str(worktree), "main", cwd=root)
    return FixtureRepo(root=root, worktree=worktree, branch=branch, issue_number=issue_number)


def materialize_valid_contract(fixture: FixtureRepo, operation: str) -> dict[str, Any]:
    """Write a one-shot V3 cleanup contract for the fixture repo."""
    materialize_module = _load_module(
        "materialize_cleanup_contract_under_test",
        REPO_ROOT / "scripts" / "agent-ops" / "materialize_cleanup_contract.py",
    )
    result = materialize_module.materialize(
        pr_number=999,
        linked_issue_number=int(fixture.issue_number),
        worktree_path=str(fixture.worktree),
        branch_name=fixture.branch,
        operation=operation,
        ttl_seconds=300,
        project_root=str(fixture.root),
        verify=False,
    )
    if result.get("status") != "ok":
        raise MatrixError(f"failed to materialize cleanup contract: {result}")
    return result


def render_command(argv: list[str]) -> str:
    return shlex.join(argv)


def execpolicy_case_definitions(fixture: FixtureRepo) -> list[dict[str, Any]]:
    """Static lane contract for Issue #1150.

    The contract includes the "exact cleanup + valid contract" allow corridor and
    deny cases for force / extra argv / missing target mutations.
    """
    worktree = str(fixture.worktree)
    return [
        {
            "label": "read_only_branch_list",
            "argv": ["git", "branch", "--list"],
            "expected_execpolicy": ["allow"],
            "expected_hook": "allow",
            "hook_cwd": "worktree",
        },
        {
            "label": "read_only_worktree_list",
            "argv": ["git", "worktree", "list"],
            "expected_execpolicy": ["allow"],
            "expected_hook": "allow",
            "hook_cwd": "worktree",
        },
        {
            "label": "exact_worktree_remove",
            "argv": ["git", "worktree", "remove", worktree],
            "expected_execpolicy": ["prompt"],
            "expected_hook": "allow",
            "operation": "worktree_remove",
            "materialize_contract": True,
        },
        {
            "label": "exact_branch_delete",
            "argv": ["git", "branch", "-d", fixture.branch],
            "expected_execpolicy": ["prompt"],
            "expected_hook": "allow",
            "operation": "branch_delete",
            "materialize_contract": True,
        },
        {
            "label": "worktree_remove_force_before_target",
            "argv": ["git", "worktree", "remove", "--force", worktree],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "worktree_remove_force_after_target",
            "argv": ["git", "worktree", "remove", worktree, "--force"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "worktree_remove_extra_target",
            "argv": ["git", "worktree", "remove", worktree, "extra"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "worktree_remove_missing_target",
            "argv": ["git", "worktree", "remove"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "branch_delete_multiple_targets",
            "argv": ["git", "branch", "-d", fixture.branch, "other-branch"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "branch_delete_force_combined_flag",
            "argv": ["git", "branch", "-d", "-f", fixture.branch],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "branch_delete_force_shortcut",
            "argv": ["git", "branch", "-D", fixture.branch],
            "expected_execpolicy": ["forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "worktree_add",
            "argv": ["git", "worktree", "add", "../tmp", "main"],
            "expected_execpolicy": ["forbidden"],
            "expected_hook": "deny",
        },
        {
            "label": "worktree_prune",
            "argv": ["git", "worktree", "prune"],
            "expected_execpolicy": ["forbidden"],
            "expected_hook": "deny",
        },
    ]


def run_command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def validate_codex_install(codex_binary: Path, expected_version: str) -> dict[str, Any]:
    """Verify the pinned Codex binary exists, reports the exact version, and probes help."""
    version_run = run_command([str(codex_binary), "--version"])
    help_run = run_command([str(codex_binary), "execpolicy", "check", "--help"])
    result = {
        "binary": str(codex_binary),
        "binary_realpath": os.path.realpath(codex_binary),
        "expected_version": expected_version,
        "version_return_code": version_run.returncode,
        "version_stdout": version_run.stdout.strip(),
        "version_stderr": version_run.stderr.strip(),
        "help_return_code": help_run.returncode,
        "help_stdout": help_run.stdout.strip(),
        "help_stderr": help_run.stderr.strip(),
        "package_route": "npm:@openai/codex",
    }
    if version_run.returncode != 0:
        raise MatrixError(f"codex --version failed: {result}")
    if result["version_stdout"] != expected_version:
        raise MatrixError(f"codex version mismatch: expected {expected_version!r}, got {result['version_stdout']!r}")
    if help_run.returncode != 0:
        raise MatrixError(f"codex execpolicy probe failed: {result}")
    return result


def parse_execpolicy_response(*, label: str, argv: list[str], completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Strict parser for the documented execpolicy JSON contract.

    stdout must contain the raw JSON contract; stderr JSON aliases are rejected.
    """
    raw_stdout = completed.stdout.strip()
    raw_stderr = completed.stderr.strip()
    payload = {
        "argv": argv,
        "command": render_command(argv),
        "return_code": completed.returncode,
        "raw_stdout": raw_stdout,
        "raw_stderr": raw_stderr,
    }
    if completed.returncode != 0:
        raise MatrixError(f"{label}: codex execpolicy returned non-zero: {payload}")
    if not raw_stdout:
        raise MatrixError(f"{label}: codex execpolicy produced empty stdout: {payload}")
    try:
        data = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise MatrixError(f"{label}: codex execpolicy JSON parse error: {exc}: {payload}") from exc
    if not isinstance(data, dict):
        raise MatrixError(f"{label}: codex execpolicy output must be a JSON object: {payload}")
    extra_keys = sorted(set(data) - ALLOWED_TOP_LEVEL_KEYS)
    if extra_keys:
        raise MatrixError(f"{label}: codex execpolicy schema drift (extra keys {extra_keys}): {payload}")
    decision = data.get("decision")
    if decision not in ALLOWED_DECISIONS:
        raise MatrixError(f"{label}: invalid decision {decision!r}: {payload}")
    matched_rules = data.get("matchedRules")
    if not isinstance(matched_rules, list):
        raise MatrixError(f"{label}: matchedRules must be a list: {payload}")
    return {
        **payload,
        "decision": decision,
        "matchedRules": matched_rules,
    }


def run_execpolicy_case(codex_binary: Path, rules: Path, case: dict[str, Any]) -> dict[str, Any]:
    completed = run_command(
        [str(codex_binary), "execpolicy", "check", "--rules", str(rules), "--", *case["argv"]],
        cwd=REPO_ROOT,
    )
    return parse_execpolicy_response(label=case["label"], argv=case["argv"], completed=completed)


def _pretool_payload(command: str, cwd: Path) -> str:
    return json.dumps(
        {
            "event": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd),
        }
    )


def run_hook_chain(command: str, fixture: FixtureRepo, *, cwd: Path | None = None) -> dict[str, Any]:
    """Replay the Codex Bash hook chain decision used for cleanup routing."""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(fixture.root)
    env["LOOP_ISSUE_NUMBER"] = fixture.issue_number
    effective_cwd = cwd or fixture.root
    payload = _pretool_payload(command, effective_cwd)
    local_main = subprocess.run(
        ["bash", str(LOCAL_MAIN_BRANCH_GUARD_SH)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    if local_main.returncode not in (0, 2):
        raise MatrixError(f"local_main_branch_guard unexpected exit code for {command!r}: {local_main.returncode}")
    worktree_guard = subprocess.run(
        ["bash", str(WORKTREE_SCOPE_GUARD_SH)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    if worktree_guard.returncode not in (0, 2):
        raise MatrixError(f"worktree_scope_guard unexpected exit code for {command!r}: {worktree_guard.returncode}")
    decision = "allow" if local_main.returncode == 0 and worktree_guard.returncode == 0 else "deny"
    return {
        "decision": decision,
        "local_main_branch_guard": {
            "return_code": local_main.returncode,
            "stderr": local_main.stderr.strip(),
        },
        "worktree_scope_guard": {
            "return_code": worktree_guard.returncode,
            "stderr": worktree_guard.stderr.strip(),
        },
    }


def evaluate_cases(codex_binary: Path, rules: Path, fixture: FixtureRepo) -> list[dict[str, Any]]:
    """Run every matrix case and return the artifact-ready results."""
    case_results: list[dict[str, Any]] = []
    for case in execpolicy_case_definitions(fixture):
        if case.get("materialize_contract"):
            materialize_valid_contract(fixture, case["operation"])
        execpolicy = run_execpolicy_case(codex_binary, rules, case)
        hook_cwd = fixture.worktree if case.get("hook_cwd") == "worktree" else fixture.root
        hook = run_hook_chain(render_command(case["argv"]), fixture, cwd=hook_cwd)
        execpolicy_ok = execpolicy["decision"] in case["expected_execpolicy"]
        hook_ok = hook["decision"] == case["expected_hook"]
        if not execpolicy_ok or not hook_ok:
            raise MatrixError(
                f"{case['label']}: expected execpolicy {case['expected_execpolicy']} / hook {case['expected_hook']}, "
                f"got execpolicy {execpolicy['decision']} / hook {hook['decision']}"
            )
        case_results.append(
            {
                "label": case["label"],
                "argv": case["argv"],
                "command": render_command(case["argv"]),
                "hook_cwd": str(hook_cwd),
                "expected": {
                    "execpolicy": case["expected_execpolicy"],
                    "hook": case["expected_hook"],
                },
                "actual": {
                    "execpolicy": execpolicy["decision"],
                    "hook": hook["decision"],
                },
                "return_code": execpolicy["return_code"],
                "raw_json": execpolicy["raw_stdout"],
                "matchedRules": execpolicy["matchedRules"],
                "hook_trace": hook,
            }
        )
    return case_results


def build_artifact(*, codex_binary: Path, expected_version: str, rules: Path) -> dict[str, Any]:
    """Build the full artifact payload. Raises MatrixError on failure."""
    install = validate_codex_install(codex_binary, expected_version)
    with tempfile.TemporaryDirectory(prefix="codex_execpolicy_matrix_") as tmp:
        fixture = build_fixture_repo(Path(tmp))
        cases = evaluate_cases(codex_binary, rules, fixture)
    return {
        "schema": SCHEMA,
        "status": "ok",
        "codex": install,
        "rules_path": str(rules),
        "skip_markers_present": [],
        "cases": cases,
    }


def write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex execpolicy matrix runner (Issue #1150)")
    parser.add_argument("--codex-binary", required=True, help="path to the pinned codex binary")
    parser.add_argument("--expected-version", required=True, help="exact stdout expected from codex --version")
    parser.add_argument("--rules", default=str(RULES_DEFAULT), help="path to the execpolicy rules file")
    parser.add_argument("--artifact", required=True, help="path to write the JSON artifact")
    args = parser.parse_args(argv)

    artifact_path = Path(args.artifact)
    try:
        payload = build_artifact(
            codex_binary=Path(args.codex_binary),
            expected_version=args.expected_version,
            rules=Path(args.rules),
        )
        write_artifact(artifact_path, payload)
        return 0
    except Exception as exc:  # pragma: no cover - exercised via CLI tests
        failure = {
            "schema": SCHEMA,
            "status": "failed",
            "error": str(exc),
            "codex_binary": args.codex_binary,
            "expected_version": args.expected_version,
            "rules_path": args.rules,
            "skip_markers_present": [],
            "cases": [],
        }
        write_artifact(artifact_path, failure)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
