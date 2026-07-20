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
from urllib.parse import urlparse
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
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

SKILL_RUNTIME_EXECUTION_CLASS_FIXTURE = "exact_skill_runtime_fixture"

# Issue #1498: sibling exact profile for anchor-comment-scoped preflight runs.
# `preflight.run` itself (argv / placeholders / execution_class / timeout) is
# entirely unmodified by this addition -- this is a new, independent
# eligible_command_ids entry, not a generalization of the existing one.
SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR = "exact_skill_runtime_anchor"

# Canonical GitHub issue comment URL shape used both for the registry
# placeholder type (`.claude/skills/issue-refinement-loop/scripts/
# command_registry.py`) and for this module's own context-binding check.
# Character classes deliberately exclude "%" so any percent-encoded disguise
# attempt (Issue #1498 Matrix #14) is rejected by construction -- no
# separate decode-and-recheck step is required because the class itself
# cannot match a "%" byte anywhere in owner/repo/issue/comment.
_GH_ISSUE_COMMENT_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"issues/(?P<issue>[1-9][0-9]*)"
    r"#issuecomment-(?P<comment>[1-9][0-9]*)$"
)

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
        },
        # Issue #1439 Scope Delta 2: test-only command-id that drives the
        # real executor -> real preflight -> real planner subprocess chain
        # offline (via --fixture). Production `preflight.run` argv/timeout
        # /placeholders are entirely unaffected -- this is a sibling entry,
        # not a generalization of `preflight.run`. Same trusted repo slug /
        # default branch / canonical root safety boundary applies.
        "preflight.run.fixture": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_FIXTURE,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "local_only",
        },
        # Issue #1498: sibling exact profile — anchor-comment-scoped preflight
        # run through the same privileged executor. `preflight.run` above is
        # entirely unmodified.
        "preflight.run.with_anchor": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "github_read_only",
        },
    },
}

ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS = frozenset(
    {"preflight.run", "preflight.run.fixture", "preflight.run.with_anchor"}
)
_ROOT_NO_WORKTREE_POLICY_INVARIANTS: dict[str, dict[str, Any]] = {
    "preflight.run": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "github_read_only",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        ],
    },
    "preflight.run.fixture": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_FIXTURE,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "local_only",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        ],
    },
    "preflight.run.with_anchor": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "github_read_only",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        ],
    },
}


@dataclass(frozen=True)
class ExactSkillRuntimeCommand:
    command_id: str
    issue_number: str
    repo: str
    argv: tuple[str, ...]
    fixture: str = ""
    anchor_comment_url: str = ""


def command_allows_root_no_worktree(parsed: ExactSkillRuntimeCommand) -> bool:
    """Return True only for the explicitly allowlisted root preflight profile."""
    if parsed.command_id not in ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS:
        return False
    policy = SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"].get(parsed.command_id)
    expected = _ROOT_NO_WORKTREE_POLICY_INVARIANTS.get(parsed.command_id)
    if not isinstance(policy, dict) or not isinstance(expected, dict):
        return False
    for key, expected_value in expected.items():
        if policy.get(key) != expected_value:
            return False
    return True


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
    if remote.startswith("https://"):
        parsed = urlparse(remote)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            return None
        if parsed.params or parsed.query or parsed.fragment or parsed.username or parsed.password:
            return None
        path = parsed.path.removesuffix(".git")
        parts = [part for part in path.split("/") if part]
        if len(parts) != 2:
            return None
        slug = "/".join(parts)
        return slug if _OWNER_REPO_RE.match(slug) else None
    match = re.fullmatch(r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", remote)
    if match is None:
        return None
    slug = match.group(1)
    return slug if _OWNER_REPO_RE.match(slug) else None


def resolve_active_issue(
    project_root: str, cwd: str, deadline: Deadline | None = None
) -> tuple[str | None, dict | None]:
    del cwd
    issue = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    if not issue.isdigit():
        return None, None
    catalog = list_worktrees(project_root, deadline)
    entry = select_issue_worktree(catalog, issue, os.path.realpath(project_root)) if catalog is not None else None
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
    if len(tokens) != 10:
        return None
    if tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    expected_flags = ["--command-id", "--issue-number", "--repo"]
    expected_positions = [4, 6, 8]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(
        tok.startswith("--command-id=") or tok.startswith("--issue-number=") or tok.startswith("--repo=")
        for tok in tokens
    ):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    policy = SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"].get(command_id)
    if policy is None:
        return None
    # This parser's argv contract is exactly 10 tokens with no --fixture /
    # --anchor-comment-url suffix. Only command ids whose policy execution
    # class matches that plain shape may match here -- sibling profiles that
    # require additional trailing tokens (preflight.run.fixture,
    # preflight.run.with_anchor) have their own dedicated parsers and must
    # never be reachable through this 10-token shape with a missing suffix.
    if policy.get("execution_class") != SKILL_RUNTIME_EXECUTION_CLASS:
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
    )


def is_exact_skill_runtime_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
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
    if command_allows_root_no_worktree(parsed):
        return True
    active_issue, entry = resolve_active_issue(project_root, cwd, deadline)
    if active_issue != parsed.issue_number or entry is None:
        return False
    return True


def _is_safe_repo_relative_fixture_path(fixture: str, root: str) -> bool:
    """True iff `fixture` is a safe repo-relative path for `--fixture`.

    Rejects absolute paths, `..` traversal, NUL/newline injection, a leading
    `-` (flag-injection), and any realpath resolution that escapes `root`
    (including via symlink components). Issue #1439 Scope Delta 2 Out of
    Scope: `--fixture` must never accept an absolute path, path traversal,
    or a symlink-mediated repo-external reference.
    """
    if not fixture or fixture.startswith("-"):
        return False
    if os.path.isabs(fixture):
        return False
    if "\x00" in fixture or "\n" in fixture or "\r" in fixture:
        return False
    parts = fixture.replace("\\", "/").split("/")
    if ".." in parts or "" in parts[1:]:
        return False
    resolved = os.path.realpath(os.path.join(root, fixture))
    try:
        common = os.path.commonpath([root, resolved])
    except ValueError:
        return False
    return common == root


def parse_exact_skill_runtime_fixture_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact-match parser for the test-only `preflight.run.fixture` command
    class (Issue #1439 Scope Delta 2).

    Mirrors `parse_exact_skill_runtime_command` token-for-token, with two
    additional trailing tokens (`--fixture <repo-relative-path>`) and
    fixture-path traversal/absolute-path/symlink-escape rejection. This is a
    separate function -- production `preflight.run`'s 10-token exact match is
    entirely unmodified and unaffected.
    """
    root = os.path.realpath(project_root or resolve_project_root())
    if not command or _METACHAR_RE.search(command) or _LEADING_ENV_RE.match(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    if len(tokens) != 12:
        return None
    if tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    expected_flags = ["--command-id", "--issue-number", "--repo", "--fixture"]
    expected_positions = [4, 6, 8, 10]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(
        tok.startswith("--command-id=")
        or tok.startswith("--issue-number=")
        or tok.startswith("--repo=")
        or tok.startswith("--fixture=")
        for tok in tokens
    ):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    fixture = tokens[11]
    if command_id != "preflight.run.fixture":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    if not _is_safe_repo_relative_fixture_path(fixture, root):
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        fixture=fixture,
    )


def is_exact_skill_runtime_fixture_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """Same trusted-repo / default-branch / canonical-root / active-issue
    safety boundary as `is_exact_skill_runtime_executor_command`, applied to
    the test-only `preflight.run.fixture` command class (Issue #1439 Scope
    Delta 2)."""
    parsed = parse_exact_skill_runtime_fixture_command(command, project_root)
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
    if command_allows_root_no_worktree(parsed):
        return True
    active_issue, entry = resolve_active_issue(project_root, cwd, deadline)
    if active_issue != parsed.issue_number or entry is None:
        return False
    return True


def parse_exact_skill_runtime_anchor_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact-match parser for the `preflight.run.with_anchor` command class
    (Issue #1498).

    Mirrors `parse_exact_skill_runtime_command` token-for-token, with two
    additional trailing tokens (`--anchor-comment-url <canonical-url>`) and
    strict canonical-shape + context-binding rejection. Production
    `preflight.run`'s 10-token exact match is entirely unmodified and
    unaffected -- this is a separate, independent parser.
    """
    root = os.path.realpath(project_root or resolve_project_root())
    if not command or _METACHAR_RE.search(command) or _LEADING_ENV_RE.match(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    if len(tokens) != 12:
        return None
    if tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    expected_flags = ["--command-id", "--issue-number", "--repo", "--anchor-comment-url"]
    expected_positions = [4, 6, 8, 10]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(
        tok.startswith("--command-id=")
        or tok.startswith("--issue-number=")
        or tok.startswith("--repo=")
        or tok.startswith("--anchor-comment-url=")
        for tok in tokens
    ):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    anchor_comment_url = tokens[11]
    if command_id != "preflight.run.with_anchor":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    match = _GH_ISSUE_COMMENT_URL_RE.fullmatch(anchor_comment_url)
    if match is None:
        return None
    # Context-binding: URL owner/repo and issue number must match the CLI
    # --repo / --issue-number arguments exactly (Issue #1498 Matrix #22).
    url_repo_slug = f"{match.group('owner')}/{match.group('repo')}"
    if url_repo_slug != repo:
        return None
    if match.group("issue") != issue_number:
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        anchor_comment_url=anchor_comment_url,
    )


def is_exact_skill_runtime_anchor_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """Same trusted-repo / default-branch / canonical-root / active-issue
    safety boundary as `is_exact_skill_runtime_executor_command`, applied to
    the `preflight.run.with_anchor` command class (Issue #1498)."""
    parsed = parse_exact_skill_runtime_anchor_command(command, project_root)
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
    if command_allows_root_no_worktree(parsed):
        return True
    active_issue, entry = resolve_active_issue(project_root, cwd, deadline)
    if active_issue != parsed.issue_number or entry is None:
        return False
    return True


def looks_like_skill_runtime_executor_command(command: str) -> bool:
    return "skill_runtime_exec.py" in (command or "")


# ─── scope_rollup.run exact executor (Issue #1547) ────────────────────────────────
# Independent exact-match command class bound directly to
# run_scope_rollup_preflight.py (NOT skill_runtime_exec.py, which is out of
# this Issue's Allowed Paths and hard-coded to the issue-refinement-loop
# preflight script). scope_rollup.run always executes at canonical root
# BEFORE any issue worktree is created (impl-review-loop preparation runs it
# pre-worktree), so it is unconditionally root-no-worktree eligible -- there
# is no active-issue-worktree check for this command class.
SCOPE_ROLLUP_RUN_COMMAND_ID = "scope_rollup.run"
SCOPE_ROLLUP_RUN_EXECUTION_CLASS = "exact_scope_rollup_run"
SCOPE_ROLLUP_RUN_REASON_CODE = "scope_rollup_run_executor_command"
SCOPE_ROLLUP_RUN_SCRIPT_REL = "scripts/agent-guards/run_scope_rollup_preflight.py"

SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"][SCOPE_ROLLUP_RUN_COMMAND_ID] = {
    "execution_class": SCOPE_ROLLUP_RUN_EXECUTION_CLASS,
    "required_cwd": "canonical_main_root",
    "required_branch": "default_branch",
    "allowed_write_roots": [],
    "network_effect": "github_read_only",
}

ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS = ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS | frozenset(
    {SCOPE_ROLLUP_RUN_COMMAND_ID}
)
_ROOT_NO_WORKTREE_POLICY_INVARIANTS[SCOPE_ROLLUP_RUN_COMMAND_ID] = {
    "execution_class": SCOPE_ROLLUP_RUN_EXECUTION_CLASS,
    "required_cwd": "canonical_main_root",
    "required_branch": "default_branch",
    "network_effect": "github_read_only",
    "allowed_write_roots": [],
}


@dataclass(frozen=True)
class ScopeRollupRunCommand:
    command_id: str
    issue_number: str
    repo: str
    invocation_id: str
    requested_at: str
    argv: tuple[str, ...]


# Issue #1547 fix_delta (P0-1): the executor now requires the caller-generated
# invocation_id / requested_at so the producer (run_scope_rollup_preflight.py)
# and the consumer (parse_scope_rollup_run_result.py) agree on a single
# invocation_id / requested_at value instead of each minting their own. The
# exact-match classifier below validates the *shape* of these two tokens
# (safe charset only -- no literal value pinning is possible since both are
# generated per-invocation).
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_REQUESTED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def parse_scope_rollup_run_command(
    command: str, project_root: str | None = None
) -> ScopeRollupRunCommand | None:
    """Exact-match parser for `uv run python3
    scripts/agent-guards/run_scope_rollup_preflight.py --issue-number <N>
    --repo <owner/repo> --invocation-id <id> --requested-at <ISO8601>`
    (12 tokens, no wrapper, no `--flag=value` form).
    """
    root = os.path.realpath(project_root or resolve_project_root())
    if not command or _METACHAR_RE.search(command) or _LEADING_ENV_RE.match(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens or len(tokens) != 12:
        return None
    if tokens[:3] != ["uv", "run", "python3"]:
        return None
    if tokens[3] != SCOPE_ROLLUP_RUN_SCRIPT_REL:
        return None
    if os.path.islink(os.path.join(root, SCOPE_ROLLUP_RUN_SCRIPT_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SCOPE_ROLLUP_RUN_SCRIPT_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    if tokens[4] != "--issue-number" or tokens[6] != "--repo":
        return None
    if tokens[8] != "--invocation-id" or tokens[10] != "--requested-at":
        return None
    if any(
        tok.startswith("--issue-number=")
        or tok.startswith("--repo=")
        or tok.startswith("--invocation-id=")
        or tok.startswith("--requested-at=")
        for tok in tokens
    ):
        return None
    issue_number = tokens[5]
    repo = tokens[7]
    invocation_id = tokens[9]
    requested_at = tokens[11]
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if not _INVOCATION_ID_RE.match(invocation_id):
        return None
    if not _REQUESTED_AT_RE.match(requested_at):
        return None
    return ScopeRollupRunCommand(
        command_id=SCOPE_ROLLUP_RUN_COMMAND_ID,
        issue_number=issue_number,
        repo=repo,
        invocation_id=invocation_id,
        requested_at=requested_at,
        argv=tuple(tokens),
    )


def is_scope_rollup_run_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """canonical root + default branch + trusted repo binding only. No
    active-issue-worktree requirement -- scope_rollup.run always runs before
    any issue worktree exists (Issue #1547 P0-1)."""
    parsed = parse_scope_rollup_run_command(command, project_root)
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
    return True


def looks_like_scope_rollup_run_command(command: str) -> bool:
    return "run_scope_rollup_preflight.py" in (command or "")


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


# Per-command-id exact argv/placeholder contracts (Issue #1439 Scope Delta 2:
# generalized from the single hard-coded `preflight.run` check so the new
# test-only `preflight.run.fixture` command-id can be validated the same
# exact-match way, without loosening `preflight.run`'s own check at all --
# its entry below is byte-for-byte identical to the prior hard-coded literal).
_EXPECTED_ARGV_BY_COMMAND: dict[str, list[str]] = {
    "preflight.run": [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
    ],
    "preflight.run.fixture": [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
        "--fixture",
        "{fixture}",
    ],
    "preflight.run.with_anchor": [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
        "--anchor-comment-url",
        "{anchor_comment_url}",
    ],
}

_EXPECTED_PLACEHOLDERS_BY_COMMAND: dict[str, dict[str, Any]] = {
    "preflight.run": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
    },
    "preflight.run.fixture": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "fixture": {"type": "repo_relative_file", "required": True},
    },
    "preflight.run.with_anchor": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
    },
}


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
    expected_write_roots = [".claude/artifacts/issue-refinement-loop/{active_issue}/"]
    if entry.get("allowed_write_roots") != expected_write_roots:
        raise ValueError("allowed_write_roots_mismatch")
    argv = entry.get("argv")
    expected_argv = _EXPECTED_ARGV_BY_COMMAND.get(command_id)
    if expected_argv is None or argv != expected_argv:
        raise ValueError("argv_template_mismatch")
    placeholders = entry.get("placeholders")
    expected_placeholders = _EXPECTED_PLACEHOLDERS_BY_COMMAND.get(command_id)
    if expected_placeholders is None or placeholders != expected_placeholders:
        raise ValueError("placeholder_mismatch")
    declared_placeholders = set(placeholders)
    argv_placeholders = {
        token[1:-1]
        for token in argv
        if isinstance(token, str) and _PLACEHOLDER_RE.match(token)
    }
    if argv_placeholders != declared_placeholders:
        raise ValueError("argv_placeholder_contract_mismatch")
    if "{active_issue}" not in "".join(expected_write_roots):
        raise ValueError("active_issue_placeholder_missing")
