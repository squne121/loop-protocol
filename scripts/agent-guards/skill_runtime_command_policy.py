#!/usr/bin/env python3
"""Shared policy for exact privileged skill runtime commands (Issue #1154)."""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
_AGENT_OPS_DIR = _ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

from worktree_catalog import Deadline, list_worktrees, select_issue_worktree  # noqa: E402

SKILL_RUNTIME_COMMAND_POLICY_SCHEMA = "SKILL_RUNTIME_COMMAND_POLICY_V2"
SKILL_RUNTIME_EXECUTION_CLASS = "exact_skill_runtime"
SKILL_RUNTIME_REASON_CODE = "skill_runtime_executor_command"
TRUSTED_REPO_SLUG = "squne121/loop-protocol"
SKILL_RUNTIME_EXEC_REL = "scripts/agent-guards/skill_runtime_exec.py"
REGISTRY_REL = ".claude/skills/issue-refinement-loop/scripts/command_registry.py"
_DEFAULT_BRANCH_NAMES = ("main", "master", "trunk")
_METACHAR_RE = re.compile(r"[;&|<>`\n\r\0]")
_LEADING_ENV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)+")
_PLACEHOLDER_RE = re.compile(r"^\{[A-Za-z0-9_]+\}$")

SKILL_RUNTIME_COMMAND_POLICY_V2: dict[str, Any] = {
    "schema": SKILL_RUNTIME_COMMAND_POLICY_SCHEMA,
    "eligible_command_ids": {
        "preflight.run": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "github_read_only",
        }
    },
}


@dataclass(frozen=True)
class ExactSkillRuntimeCommand:
    command_id: str
    issue_number: str
    repo: str
    argv: tuple[str, ...]


def resolve_project_root() -> str:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env_root:
        return os.path.realpath(env_root)
    return os.path.realpath(str(_ROOT))


def resolve_default_branch(project_root: str, deadline: Deadline | None = None) -> str:
    env_branch = os.environ.get("LOOP_DEFAULT_BRANCH", "").strip()
    if env_branch:
        return env_branch
    git = shutil.which("git") or "git"
    timeout = deadline.subprocess_timeout(5.0) if deadline is not None else 5.0
    try:
        out = subprocess.run(
            [git, "-C", project_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out and out.returncode == 0 and out.stdout.strip():
        ref = out.stdout.strip()
        return ref.split("/", 1)[1] if "/" in ref else ref
    for candidate in _DEFAULT_BRANCH_NAMES:
        try:
            out = subprocess.run(
                [git, "-C", project_root, "rev-parse", "--verify", candidate],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0:
            return candidate
    return "main"


def current_branch(project_root: str, deadline: Deadline | None = None) -> str | None:
    git = shutil.which("git") or "git"
    timeout = deadline.subprocess_timeout(5.0) if deadline is not None else 5.0
    try:
        out = subprocess.run(
            [git, "-C", project_root, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    branch = out.stdout.strip()
    return branch or None


def resolve_repo_slug(project_root: str, deadline: Deadline | None = None) -> str | None:
    git = shutil.which("git") or "git"
    timeout = deadline.subprocess_timeout(5.0) if deadline is not None else 5.0
    try:
        out = subprocess.run(
            [git, "-C", project_root, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    remote = out.stdout.strip()
    if not remote:
        return None
    remote = remote.removesuffix(".git")
    if remote.startswith("git@github.com:"):
        return remote[len("git@github.com:") :]
    if "github.com/" in remote:
        return remote.split("github.com/", 1)[1]
    return None


def resolve_active_issue(project_root: str, cwd: str, deadline: Deadline | None = None) -> tuple[str | None, dict | None]:
    issue = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    if not issue.isdigit():
        return None, None
    catalog = list_worktrees(project_root, deadline)
    entry = select_issue_worktree(catalog, issue, os.path.realpath(project_root)) if catalog is not None else None
    if entry is not None:
        return issue, entry
    worktrees_dir = Path(project_root) / ".claude" / "worktrees"
    matches = sorted(worktrees_dir.glob(f"issue-{issue}-*"))
    if len(matches) == 1 and matches[0].is_dir():
        return issue, {"worktree_realpath": os.path.realpath(str(matches[0]))}
    return issue, entry


def parse_exact_skill_runtime_command(command: str, project_root: str | None = None) -> ExactSkillRuntimeCommand | None:
    root = os.path.realpath(project_root or resolve_project_root())
    if not command or _METACHAR_RE.search(command) or _LEADING_ENV_RE.match(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    script_index = None
    if tokens[:3] == ["uv", "run", "python3"]:
        script_index = 3
    elif len(tokens) >= 2 and os.path.basename(tokens[0]) == "python3":
        script_index = 1
    else:
        return None
    if len(tokens) != script_index + 7:
        return None
    script_token = tokens[script_index]
    if script_token.startswith("-"):
        return None
    script_path = os.path.realpath(os.path.join(root, script_token)) if not os.path.isabs(script_token) else os.path.realpath(script_token)
    if script_path != os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_flags = ["--command-id", "--issue-number", "--repo"]
    expected_positions = [script_index + 1, script_index + 3, script_index + 5]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(tok.startswith("--command-id=") or tok.startswith("--issue-number=") or tok.startswith("--repo=") for tok in tokens):
        return None
    command_id = tokens[script_index + 2]
    issue_number = tokens[script_index + 4]
    repo = tokens[script_index + 6]
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
    )


def is_exact_skill_runtime_executor_command(command: str, cwd: str, project_root: str, deadline: Deadline | None = None) -> bool:
    parsed = parse_exact_skill_runtime_command(command, project_root)
    if parsed is None:
        return False
    if os.path.realpath(cwd) != os.path.realpath(project_root):
        return False
    branch = current_branch(project_root, deadline)
    default_branch = resolve_default_branch(project_root, deadline)
    if not branch or branch != default_branch:
        return False
    repo_slug = resolve_repo_slug(project_root, deadline)
    if repo_slug != parsed.repo:
        return False
    active_issue, entry = resolve_active_issue(project_root, cwd, deadline)
    if active_issue != parsed.issue_number or entry is None:
        return False
    return True


def looks_like_skill_runtime_executor_command(command: str) -> bool:
    return "skill_runtime_exec.py" in (command or "")


def load_registry_entry(command_id: str, project_root: str | None = None) -> dict[str, Any]:
    root = os.path.realpath(project_root or resolve_project_root())
    registry_path = os.path.realpath(os.path.join(root, REGISTRY_REL))
    rel_registry = os.path.join(root, REGISTRY_REL)
    if os.path.islink(rel_registry):
        raise ValueError("registry_symlink_not_allowed")
    spec = importlib.util.spec_from_file_location("issue_refinement_command_registry", registry_path)
    if spec is None or spec.loader is None or not spec.origin:
        raise ValueError("registry_spec_invalid")
    if os.path.realpath(spec.origin) != registry_path:
        raise ValueError("registry_origin_mismatch")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = getattr(module, "REGISTRY", None)
    if not isinstance(registry, dict):
        raise ValueError("registry_missing")
    entry = registry.get(command_id)
    if not isinstance(entry, dict):
        raise ValueError("registry_entry_missing")
    return dict(entry)


def validate_registry_entry(command_id: str, entry: dict[str, Any], active_issue: str) -> None:
    policy = SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"].get(command_id)
    if policy is None:
        raise ValueError("command_id_not_eligible")
    if entry.get("execution_class") != policy["execution_class"]:
        raise ValueError("execution_class_mismatch")
    if entry.get("cwd_policy") != "repo_root":
        raise ValueError("cwd_policy_mismatch")
    if entry.get("required_cwd") != policy["required_cwd"]:
        raise ValueError("required_cwd_mismatch")
    if entry.get("required_branch") != policy["required_branch"]:
        raise ValueError("required_branch_mismatch")
    if entry.get("network_effect") != policy["network_effect"]:
        raise ValueError("network_effect_mismatch")
    expected_write_roots = [f".claude/artifacts/issue-refinement-loop/{active_issue}/"]
    if entry.get("allowed_write_roots") != expected_write_roots:
        raise ValueError("allowed_write_roots_mismatch")
    argv = entry.get("argv")
    if argv != [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
    ]:
        raise ValueError("argv_template_mismatch")
    placeholders = entry.get("placeholders")
    if placeholders != {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
    }:
        raise ValueError("placeholder_mismatch")
    for token in argv:
        if isinstance(token, str) and _PLACEHOLDER_RE.match(token):
            raise ValueError("unresolved_whole_token_placeholder")
