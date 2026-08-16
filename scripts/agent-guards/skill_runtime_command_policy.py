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
SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR_FIXTURE = "exact_skill_runtime_anchor_fixture"

# Issue #1498: sibling exact profile for anchor-comment-scoped preflight runs.
# `preflight.run` itself (argv / placeholders / execution_class / timeout) is
# entirely unmodified by this addition -- this is a new, independent
# eligible_command_ids entry, not a generalization of the existing one.
SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR = "exact_skill_runtime_anchor"
SKILL_RUNTIME_EXECUTION_CLASS_CONTRACT_UPDATE_ANCHOR = "exact_skill_runtime_contract_update_anchor"

# #2086 AC10 (iteration 2, post-#2053/#2068 merge): sibling exact profiles
# for the `decide.run` / `authority_transport.produce` /
# `authority_transport.consume` command classes. `decide.run`'s
# eligible_command_ids entry was added in iteration 1 of this Issue, but
# `authority_transport.produce` / `authority_transport.consume` had no
# matching entry at all, and none of the three had a real *dispatch path*
# through `skill_runtime_exec.py`'s privileged executor (confirmed by
# #2053/PR #2068's own pinned regression test
# `TestAuthorityTransportPrivilegedExecutorRealSubprocessDispatch::
# test_privileged_executor_rejects_command_id_today`, whose docstring
# records this exact gap as an explicit Stop Condition scope delta for a
# follow-up Issue) -- a registry/policy-declaration-only false-green this
# Issue closes. `SKILL_RUNTIME_EXECUTION_CLASS_DECIDE`'s value is updated
# from iteration 1's placeholder `"exact_skill_runtime_decide"` to
# `"exact_router_authority_transport"` to match #2053/PR #2068's own
# now-merged `command_registry.py` declaration for `decide.run` exactly
# (the constant *name* is kept unchanged so existing references, including
# `scripts/agent-guards/tests/test_skill_runtime_policy_anchor.py`, do not
# need to change). `preflight.run` / anchor / fixture / contract_update
# profiles above are entirely unmodified by this addition.
SKILL_RUNTIME_EXECUTION_CLASS_DECIDE = "exact_router_authority_transport"
SKILL_RUNTIME_EXECUTION_CLASS_AUTHORITY_TRANSPORT_PRODUCER = "exact_authority_transport_producer"
SKILL_RUNTIME_EXECUTION_CLASS_AUTHORITY_TRANSPORT_CONSUMER = "exact_authority_transport_consumer"
# Issue #2039 AC8/AC11: repair_action.apply controlled consumer command class.
SKILL_RUNTIME_EXECUTION_CLASS_REPAIR_ACTION_APPLY = "exact_repair_action_apply"
_DECIDE_VERDICT_VALUES = frozenset({"approve", "request_changes", "needs-fix"})


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
        "preflight.run.fixture.with_human_context": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR_FIXTURE,
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
        "preflight.run.with_human_context": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "github_read_only",
        },
        "preflight.run.with_agent_report": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "github_read_only",
        },
        "contract_update.run.with_anchor": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_CONTRACT_UPDATE_ANCHOR,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
                "artifacts/{active_issue}/issue-metadata/",
            ],
            "network_effect": "github_read_only",
        },
        "contract_update.run.with_human_context": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_CONTRACT_UPDATE_ANCHOR,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
                "artifacts/{active_issue}/issue-metadata/",
            ],
            "network_effect": "github_read_only",
        },
        # #2086 AC10 (iteration 2): registers the router role of the
        # SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 chain (command_registry.py
        # "decide.run") so the privileged runtime policy actually recognizes
        # it, and so its real-subprocess dispatch through
        # skill_runtime_exec.py is reachable. decide_next_loop_action.py
        # genuinely writes a SCOPE_DELTA_ROUTER_RECEIPT_V1 under the active
        # issue's artifact root when --issue-number/--invocation-id are
        # supplied, so `allowed_write_roots` matches every other eligible
        # command_id's `.claude/artifacts/issue-refinement-loop/{active_issue}/`
        # root (iteration 1's `[]` predated #2053's merge and was stale).
        "decide.run": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_DECIDE,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "local_only",
        },
        # #2086 AC10 / AC9 (iteration 2): producer role of the #2053/#2068
        # chain (command_registry.py "authority_transport.produce") --
        # generates and immutably persists a
        # SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest from a fixture file,
        # bypassing the gh CLI entirely (network_effect: local_only, mirrored
        # from the registry entry).
        "authority_transport.produce": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_AUTHORITY_TRANSPORT_PRODUCER,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "local_only",
        },
        # #2086 AC10 / AC9 (iteration 2): controlled consumer role
        # (command_registry.py "authority_transport.consume") -- verifies a
        # SCOPE_DELTA_ROUTER_RECEIPT_V1, mutates exactly once, reads back, and
        # emits SCOPE_DELTA_CONSUMPTION_RECEIPT_V1. `network_effect` is
        # `github_mutation` (mirrored from the registry entry): when the
        # optional `--contract-patch-plan-file`/`--anchor-context-file`
        # placeholders are supplied, this command's default execution path
        # genuinely performs a real GitHub issue mutation via
        # `edit_issue_txn.py`'s `gh` subprocess calls.
        "authority_transport.consume": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_AUTHORITY_TRANSPORT_CONSUMER,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "github_mutation",
        },
        # Issue #2039 AC8/AC11: repair_action.apply controlled consumer --
        # bridges a repair_issue_contract.py auto_apply_safe candidate to a
        # controlled edit_issue_txn.py Issue-body mutation. Bound to the
        # issue's own active worktree (not root-no-worktree eligible), same
        # boundary as authority_transport.consume.
        "repair_action.apply": {
            "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_REPAIR_ACTION_APPLY,
            "required_cwd": "canonical_main_root",
            "required_branch": "default_branch",
            "allowed_write_roots": [
                ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            ],
            "network_effect": "github_mutation",
        },
    },
}

ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS = frozenset(
    {
        "preflight.run",
        "preflight.run.fixture",
        "preflight.run.fixture.with_human_context",
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
        "decide.run",
    }
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
    "preflight.run.fixture.with_human_context": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR_FIXTURE,
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
    "preflight.run.with_human_context": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "github_read_only",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        ],
    },
    "preflight.run.with_agent_report": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "github_read_only",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        ],
    },
    "contract_update.run.with_anchor": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_CONTRACT_UPDATE_ANCHOR,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "github_read_only",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            "artifacts/{active_issue}/issue-metadata/",
        ],
    },
    "contract_update.run.with_human_context": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_CONTRACT_UPDATE_ANCHOR,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "github_read_only",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            "artifacts/{active_issue}/issue-metadata/",
        ],
    },
    "decide.run": {
        "execution_class": SKILL_RUNTIME_EXECUTION_CLASS_DECIDE,
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "network_effect": "local_only",
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
    loop_state_file: str = ""
    verdict: str = ""
    max_iterations: str = ""
    # #2086 AC10 (iteration 2): authority_transport.produce/consume fields.
    invocation_id: str = ""
    git_head_sha: str = ""
    evidence_fixture_path: str = ""
    router_receipt_path: str = ""
    contract_patch_plan_file: str = ""
    anchor_context_file: str = ""
    # Issue #2039 AC8/AC11: repair_action.apply field.
    preflight_result_path: str = ""


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


# Issue #2136 adversarial hardening (H3): every value that flows through this
# module's `_is_safe_repo_relative_fixture_path` gate is later re-validated
# by an exact parser that re-tokenizes a *re-serialized* `" ".join(argv)`
# string with `shlex.split()` (see `skill_runtime_exec.py`'s
# `command_text = " ".join(command_tokens)` construction). That join/re-split
# round trip is lossy for any value containing shell-lexer-significant
# characters (whitespace, quotes, backslash): the value actually forwarded to
# the real child subprocess (built from the untouched, un-split Python
# string) can diverge from the shorter/differently-tokenized value the exact
# parser actually validated at its fixed argv position. Rejecting these
# characters here closes that validation/execution divergence for every
# `repo_relative_file`-typed placeholder uniformly (production and
# test-only), without changing any command's argv shape, flags, or accepted
# command_ids.
_SHLEX_ROUND_TRIP_UNSAFE_CHARS = frozenset({" ", "\t", "\"", "'", "\\"})


def _is_safe_repo_relative_fixture_path(fixture: str, root: str) -> bool:
    """True iff `fixture` is a safe repo-relative path for `--fixture`.

    Rejects absolute paths, `..` traversal, NUL/newline injection, a leading
    `-` (flag-injection), whitespace/quote/backslash (shlex round-trip
    divergence, Issue #2136 H3), and any realpath resolution that escapes
    `root` (including via symlink components). Issue #1439 Scope Delta 2 Out
    of Scope: `--fixture` must never accept an absolute path, path
    traversal, or a symlink-mediated repo-external reference.
    """
    if not fixture or fixture.startswith("-"):
        return False
    if os.path.isabs(fixture):
        return False
    if "\x00" in fixture or "\n" in fixture or "\r" in fixture:
        return False
    if any(ch in fixture for ch in _SHLEX_ROUND_TRIP_UNSAFE_CHARS):
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


def _path_contains_symlink_component(root: str, rel_path: str) -> bool:
    """True iff any path component between `root` and `rel_path` (inclusive
    of the leaf) is itself a symlink.

    Confinement checks built solely from `os.path.realpath()` +
    `os.path.commonpath()` are bypassable when a directory *inside* (or
    exactly at) the confinement prefix is a symlink to a location outside
    the intended target: both sides of the comparison resolve through the
    same symlink and still agree with each other (Issue #2136 adversarial
    hardening H1). Walking every literal (non-resolved) path component and
    rejecting on the first symlink closes that class of bypass regardless of
    where the symlink ultimately points -- fixture path, transport path,
    intermediate directory, leaf file, or the artifact-root prefix directory
    itself (including a sibling-issue-root alias).
    """
    current = root
    for part in rel_path.replace("\\", "/").split("/"):
        if not part:
            continue
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def _is_safe_issue_artifact_path(path: str, root: str, issue_number: str) -> bool:
    """Require #2136 sibling inputs to stay under their bound artifact root.

    This intentionally supplements, rather than changes, the generic fixture
    profile's repository-relative confinement.  Both the target issue number
    and every symlink-resolved component are bound before a child can start.
    """
    if not _is_safe_repo_relative_fixture_path(path, root):
        return False
    prefix = f".claude/artifacts/issue-refinement-loop/{issue_number}/"
    if not path.replace("\\", "/").startswith(prefix):
        return False
    # Issue #2136 H1: reject before resolving `artifact_root`/`resolved` via
    # `realpath` -- a symlinked artifact-root prefix (or any intermediate
    # component) would otherwise make both sides of the commonpath
    # comparison below agree with each other while silently pointing at a
    # different issue's artifact tree.
    if _path_contains_symlink_component(root, prefix) or _path_contains_symlink_component(root, path):
        return False
    artifact_root = os.path.realpath(os.path.join(root, prefix))
    try:
        if os.path.commonpath([root, artifact_root]) != root:
            return False
        resolved = os.path.realpath(os.path.join(root, path))
        return os.path.commonpath([artifact_root, resolved]) == artifact_root
    except ValueError:
        return False


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


def parse_exact_skill_runtime_anchor_fixture_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact parser for #2136's test-only fixture + human-context sibling.

    This is intentionally separate from both the fixture and anchor parsers:
    production profiles cannot acquire either field by this test-only route.
    """
    root = os.path.realpath(project_root or resolve_project_root())
    if not command or _METACHAR_RE.search(command) or _LEADING_ENV_RE.match(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    required_flags = ["--command-id", "--issue-number", "--repo", "--fixture", "--anchor-comment-url"]
    required_positions = [4, 6, 8, 10, 12]
    if len(tokens) not in {14, 16} or tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if any(tokens[pos] != flag for flag, pos in zip(required_flags, required_positions)):
        return None
    if len(tokens) == 16 and tokens[14] != "--investigation-evidence-transport-path":
        return None
    all_flags = [*required_flags, "--investigation-evidence-transport-path"]
    if any(token.startswith(f"{flag}=") for token in tokens for flag in all_flags):
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    if os.path.realpath(os.path.join(root, tokens[3])) != os.path.realpath(
        os.path.join(root, SKILL_RUNTIME_EXEC_REL)
    ):
        return None
    command_id, issue_number, repo = tokens[5], tokens[7], tokens[9]
    fixture, anchor_comment_url = tokens[11], tokens[13]
    transport_path = tokens[15] if len(tokens) == 16 else None
    if command_id != "preflight.run.fixture.with_human_context":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0 or repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    if not _is_safe_issue_artifact_path(fixture, root, issue_number):
        return None
    if transport_path is not None and not _is_safe_issue_artifact_path(transport_path, root, issue_number):
        return None
    match = _GH_ISSUE_COMMENT_URL_RE.fullmatch(anchor_comment_url)
    if match is None or f"{match.group('owner')}/{match.group('repo')}" != repo:
        return None
    if match.group("issue") != issue_number:
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        fixture=fixture,
        anchor_comment_url=anchor_comment_url,
    )


def is_exact_skill_runtime_anchor_fixture_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    parsed = parse_exact_skill_runtime_anchor_fixture_command(command, project_root)
    if parsed is None:
        return False
    if os.path.realpath(cwd) != os.path.realpath(project_root):
        return False
    if current_branch(project_root, deadline) != resolve_default_branch(project_root, deadline):
        return False
    if resolve_repo_slug(project_root, deadline) != parsed.repo:
        return False
    return command_allows_root_no_worktree(parsed)


def parse_exact_skill_runtime_decide_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact-match parser for the `decide.run` command class (#2086 AC10).

    Mirrors `parse_exact_skill_runtime_command` token-for-token, with three
    additional trailing flag/value pairs (`--loop-state-file <path>
    --review-result-verdict <verdict> --max-iterations <n>`). This is a
    separate, independent function -- production `preflight.run` / fixture /
    anchor parsers above are entirely unmodified by this addition. Unlike
    `--fixture` (test-only), `--loop-state-file` is exercised by the real
    production `decide.run` invocation, so the same repo-relative /
    traversal / symlink-escape safety check (`_is_safe_repo_relative_fixture_path`)
    is applied here for real writes-adjacent path handling, not just tests.
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
    if len(tokens) != 16:
        return None
    if tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    expected_flags = [
        "--command-id",
        "--issue-number",
        "--repo",
        "--loop-state-file",
        "--review-result-verdict",
        "--max-iterations",
    ]
    expected_positions = [4, 6, 8, 10, 12, 14]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(
        tok.startswith("--command-id=")
        or tok.startswith("--issue-number=")
        or tok.startswith("--repo=")
        or tok.startswith("--loop-state-file=")
        or tok.startswith("--review-result-verdict=")
        or tok.startswith("--max-iterations=")
        for tok in tokens
    ):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    loop_state_file = tokens[11]
    verdict = tokens[13]
    max_iterations = tokens[15]
    if command_id != "decide.run":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    if not _is_safe_repo_relative_fixture_path(loop_state_file, root):
        return None
    if verdict not in _DECIDE_VERDICT_VALUES:
        return None
    if not max_iterations.isdigit() or int(max_iterations) <= 0:
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        loop_state_file=loop_state_file,
        verdict=verdict,
        max_iterations=max_iterations,
    )


def is_exact_skill_runtime_decide_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """Same trusted-repo / default-branch / canonical-root safety boundary as
    `is_exact_skill_runtime_executor_command`, applied to the `decide.run`
    command class (#2086 AC10). `decide.run` is always root-no-worktree
    eligible (it is a read-only router, not bound to a single active
    issue's artifact directory)."""
    parsed = parse_exact_skill_runtime_decide_command(command, project_root)
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
    return command_allows_root_no_worktree(parsed)


_GIT_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_INVOCATION_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def parse_exact_skill_runtime_decide_authority_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact-match parser for `decide.run`'s "Mode B" -- #2053's canonical
    authority-transport verification (`generate_router_receipt()`)
    dispatched ALONGSIDE the ordinary loop-state decision (#2086 P0
    fix_delta Blocker 3).

    Mirrors `parse_exact_skill_runtime_decide_command` (Mode A, 16 tokens)
    token-for-token, with a FIXED further seven-token suffix:
    `--authority-transport-path <repo-relative-path> --authority-expected
    --invocation-id <id> --git-head-sha <sha>` (23 tokens total). All four
    authority sub-fields (transport path / the bare `--authority-expected`
    flag / invocation id / git head sha) are required together in this
    grammar -- there is no shorter/partial variant; a caller that wants only
    Mode A uses `parse_exact_skill_runtime_decide_command` instead. This is
    a single, narrow, exact-match extension -- not a generic "any registry
    field" passthrough (the discipline already applied to Mode A and to
    `authority_transport.produce`/`consume` above, and explicitly required
    by this Issue's Blocker 3).
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
    if len(tokens) != 23:
        return None
    if tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    expected_flags = [
        "--command-id",
        "--issue-number",
        "--repo",
        "--loop-state-file",
        "--review-result-verdict",
        "--max-iterations",
        "--authority-transport-path",
        "--authority-expected",
        "--invocation-id",
        "--git-head-sha",
    ]
    expected_positions = [4, 6, 8, 10, 12, 14, 16, 18, 19, 21]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(
        tok.startswith("--command-id=")
        or tok.startswith("--issue-number=")
        or tok.startswith("--repo=")
        or tok.startswith("--loop-state-file=")
        or tok.startswith("--review-result-verdict=")
        or tok.startswith("--max-iterations=")
        or tok.startswith("--authority-transport-path=")
        or tok.startswith("--authority-expected=")
        or tok.startswith("--invocation-id=")
        or tok.startswith("--git-head-sha=")
        for tok in tokens
    ):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    loop_state_file = tokens[11]
    verdict = tokens[13]
    max_iterations = tokens[15]
    authority_transport_manifest_path = tokens[17]
    invocation_id = tokens[20]
    git_head_sha = tokens[22]
    if command_id != "decide.run":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    if not _is_safe_repo_relative_fixture_path(loop_state_file, root):
        return None
    if verdict not in _DECIDE_VERDICT_VALUES:
        return None
    if not max_iterations.isdigit() or int(max_iterations) <= 0:
        return None
    if not _is_safe_repo_relative_fixture_path(authority_transport_manifest_path, root):
        return None
    if not _INVOCATION_ID_TOKEN_RE.match(invocation_id):
        return None
    if not _GIT_HEAD_SHA_RE.match(git_head_sha):
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        loop_state_file=loop_state_file,
        verdict=verdict,
        max_iterations=max_iterations,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
    )


def is_exact_skill_runtime_decide_authority_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """Same trusted-repo / default-branch / canonical-root safety boundary
    as `is_exact_skill_runtime_decide_executor_command`, applied to
    `decide.run`'s Mode B (#2086 P0 fix_delta Blocker 3). Mode B remains
    root-no-worktree eligible -- it is still the same read-only router,
    merely also verifying an authority transport manifest."""
    parsed = parse_exact_skill_runtime_decide_authority_command(command, project_root)
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
    return command_allows_root_no_worktree(parsed)


def parse_exact_skill_runtime_authority_transport_produce_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact-match parser for the `authority_transport.produce` command class
    (#2086 AC9/AC10, #2053/PR #2068 producer role).

    Mirrors `parse_exact_skill_runtime_command` token-for-token, with four
    additional trailing flag/value pairs (`--invocation-id <id>
    --git-head-sha <sha> --produce-authority-transport <repo-relative-path>`).
    All four placeholders are `required: True` in command_registry.py's
    `authority_transport.produce` entry, so this is a single fixed-length
    (16-token) shape -- there is no optional-segment ambiguity to resolve,
    unlike `decide.run`. This is a separate, independent function --
    production `preflight.run` / fixture / anchor / decide parsers above are
    entirely unmodified by this addition.
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
    if len(tokens) != 16:
        return None
    if tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    expected_flags = [
        "--command-id",
        "--issue-number",
        "--repo",
        "--invocation-id",
        "--git-head-sha",
        "--produce-authority-transport",
    ]
    expected_positions = [4, 6, 8, 10, 12, 14]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(
        tok.startswith("--command-id=")
        or tok.startswith("--issue-number=")
        or tok.startswith("--repo=")
        or tok.startswith("--invocation-id=")
        or tok.startswith("--git-head-sha=")
        or tok.startswith("--produce-authority-transport=")
        for tok in tokens
    ):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    invocation_id = tokens[11]
    git_head_sha = tokens[13]
    evidence_fixture_path = tokens[15]
    if command_id != "authority_transport.produce":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    if not _INVOCATION_ID_TOKEN_RE.match(invocation_id):
        return None
    if not _GIT_HEAD_SHA_RE.match(git_head_sha):
        return None
    if not _is_safe_repo_relative_fixture_path(evidence_fixture_path, root):
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        evidence_fixture_path=evidence_fixture_path,
    )


def is_exact_skill_runtime_authority_transport_produce_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """Same trusted-repo / default-branch / canonical-root / active-issue
    safety boundary as `is_exact_skill_runtime_executor_command`, applied to
    the `authority_transport.produce` command class (#2086 AC9/AC10).
    `authority_transport.produce` is bound to the issue's own active
    worktree -- unlike `decide.run`, it is not root-no-worktree eligible."""
    parsed = parse_exact_skill_runtime_authority_transport_produce_command(command, project_root)
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


def parse_exact_skill_runtime_repair_action_apply_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact-match parser for the `repair_action.apply` command class
    (Issue #2039 AC8/AC11).

    Mirrors `parse_exact_skill_runtime_command` token-for-token, with two
    additional trailing tokens (`--apply-repair-action <repo-relative-path>`).
    The single placeholder is `required: True` in command_registry.py's
    `repair_action.apply` entry, so this is a single fixed-length (12-token)
    shape -- no optional-segment ambiguity, same as
    `authority_transport.produce`. This is a separate, independent function;
    every other parser above is entirely unmodified by this addition.
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
    expected_flags = ["--command-id", "--issue-number", "--repo", "--apply-repair-action"]
    expected_positions = [4, 6, 8, 10]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if any(
        tok.startswith("--command-id=")
        or tok.startswith("--issue-number=")
        or tok.startswith("--repo=")
        or tok.startswith("--apply-repair-action=")
        for tok in tokens
    ):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    preflight_result_path = tokens[11]
    if command_id != "repair_action.apply":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    # PR #2202 review fix (P0-2): bind the artifact path to THIS Issue's own
    # artifact tree (.claude/artifacts/issue-refinement-loop/{issue_number}/)
    # via the Issue-scoped helper, not the generic repo-relative fixture
    # helper -- otherwise a candidate belonging to a different Issue number
    # (or any other repo-relative file) is accepted.
    if not _is_safe_issue_artifact_path(preflight_result_path, root, issue_number):
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        preflight_result_path=preflight_result_path,
    )


def is_exact_skill_runtime_repair_action_apply_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """Same trusted-repo / default-branch / canonical-root / active-issue
    safety boundary as `is_exact_skill_runtime_executor_command`, applied to
    the `repair_action.apply` command class (Issue #2039 AC8/AC11).
    `repair_action.apply` is bound to the issue's own active worktree --
    like `authority_transport.consume`, it is not root-no-worktree
    eligible."""
    parsed = parse_exact_skill_runtime_repair_action_apply_command(command, project_root)
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


def _parse_exact_skill_runtime_authority_transport_consume_tail(
    tokens: list[str], root: str
) -> ExactSkillRuntimeCommand | None:
    """Shared tail-parsing for both `authority_transport.consume` shapes
    (minimal 5-required-placeholder shape, and the with-patch-plan 7-placeholder
    shape). `--contract-patch-plan-file` / `--anchor-context-file` are
    `optional_flag_pair` in command_registry.py's `authority_transport.consume`
    entry and, per `run_refinement_preflight.py`'s own argparse help text, are
    only meaningful when supplied together -- so this parser accepts exactly
    16 tokens (neither optional pair) or exactly 20 tokens (both optional
    pairs present); any other length, or exactly one of the two pairs
    present, is rejected."""
    if tokens[:4] != ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL]:
        return None
    if os.path.islink(os.path.join(root, SKILL_RUNTIME_EXEC_REL)):
        return None
    expected_script = os.path.realpath(os.path.join(root, SKILL_RUNTIME_EXEC_REL))
    if os.path.realpath(os.path.join(root, tokens[3])) != expected_script:
        return None
    expected_flags = [
        "--command-id",
        "--issue-number",
        "--repo",
        "--invocation-id",
        "--git-head-sha",
        "--consume-authority-transport",
    ]
    expected_positions = [4, 6, 8, 10, 12, 14]
    for flag, pos in zip(expected_flags, expected_positions):
        if tokens[pos] != flag:
            return None
    if len(tokens) == 20:
        if tokens[16] != "--contract-patch-plan-file" or tokens[18] != "--anchor-context-file":
            return None
    elif len(tokens) != 16:
        return None
    equals_guard_flags = (
        "--command-id=",
        "--issue-number=",
        "--repo=",
        "--invocation-id=",
        "--git-head-sha=",
        "--consume-authority-transport=",
        "--contract-patch-plan-file=",
        "--anchor-context-file=",
    )
    if any(tok.startswith(prefix) for prefix in equals_guard_flags for tok in tokens):
        return None
    command_id = tokens[5]
    issue_number = tokens[7]
    repo = tokens[9]
    invocation_id = tokens[11]
    git_head_sha = tokens[13]
    router_receipt_path = tokens[15]
    contract_patch_plan_file = tokens[17] if len(tokens) == 20 else ""
    anchor_context_file = tokens[19] if len(tokens) == 20 else ""
    if command_id != "authority_transport.consume":
        return None
    if not issue_number.isdigit() or int(issue_number) <= 0:
        return None
    if repo != TRUSTED_REPO_SLUG:
        return None
    if command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return None
    if not _INVOCATION_ID_TOKEN_RE.match(invocation_id):
        return None
    if not _GIT_HEAD_SHA_RE.match(git_head_sha):
        return None
    if not _is_safe_repo_relative_fixture_path(router_receipt_path, root):
        return None
    if contract_patch_plan_file and not _is_safe_repo_relative_fixture_path(contract_patch_plan_file, root):
        return None
    if anchor_context_file and not _is_safe_repo_relative_fixture_path(anchor_context_file, root):
        return None
    return ExactSkillRuntimeCommand(
        command_id=command_id,
        issue_number=issue_number,
        repo=repo,
        argv=tuple(tokens),
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        router_receipt_path=router_receipt_path,
        contract_patch_plan_file=contract_patch_plan_file,
        anchor_context_file=anchor_context_file,
    )


def parse_exact_skill_runtime_authority_transport_consume_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    """Exact-match parser for the `authority_transport.consume` command class
    (#2086 AC9/AC10, #2053/PR #2068 controlled consumer role). This is a
    separate, independent function -- production `preflight.run` / fixture /
    anchor / decide / produce parsers above are entirely unmodified by this
    addition."""
    root = os.path.realpath(project_root or resolve_project_root())
    if not command or _METACHAR_RE.search(command) or _LEADING_ENV_RE.match(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    return _parse_exact_skill_runtime_authority_transport_consume_tail(tokens, root)


def is_exact_skill_runtime_authority_transport_consume_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    """Same trusted-repo / default-branch / canonical-root / active-issue
    safety boundary as `is_exact_skill_runtime_executor_command`, applied to
    the `authority_transport.consume` command class (#2086 AC9/AC10)."""
    parsed = parse_exact_skill_runtime_authority_transport_consume_command(command, project_root)
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




def _parse_exact_skill_runtime_anchor_command(
    command: str, expected_command_ids: frozenset[str], project_root: str | None = None
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
    if len(tokens) < 12:
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
    if command_id not in expected_command_ids:
        return None

    # The command-id is the canonical, caller-selected origin lane.  Keep it
    # in the exact argv grammar so a human/agent lane cannot be silently
    # dropped between the registry and the executor.  Generic anchor profiles
    # intentionally have no origin-lane flag.
    lane_flag = {
        "preflight.run.with_human_context": "--human-context-comment-url",
        "preflight.run.with_agent_report": "--agent-report-comment-url",
        "contract_update.run.with_human_context": "--human-context-comment-url",
    }.get(command_id)
    base_expected_length = 14 if lane_flag else 12
    # #2086 P0 fix_delta (Blocker 1/2): `preflight.run.with_human_context`
    # ONLY may optionally carry a further, exact two-token
    # `--investigation-evidence-transport-path <repo-relative-path>` suffix
    # (a bound SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest path -- see
    # `run_refinement_preflight._validate_investigation_evidence_transport`).
    # No other command_id in this parser's grammar may carry it -- this is a
    # single, narrow, exact-match extension, not a generic passthrough.
    investigation_evidence_transport_path: "str | None" = None
    allows_investigation_transport = command_id == "preflight.run.with_human_context"
    if len(tokens) == base_expected_length:
        pass
    elif (
        allows_investigation_transport
        and len(tokens) == base_expected_length + 2
        and tokens[base_expected_length] == "--investigation-evidence-transport-path"
    ):
        investigation_evidence_transport_path = tokens[base_expected_length + 1]
        if not _is_safe_repo_relative_fixture_path(investigation_evidence_transport_path, root):
            return None
    else:
        return None
    if lane_flag and (tokens[12] != lane_flag or tokens[13] != anchor_comment_url):
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


def parse_exact_skill_runtime_anchor_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    return _parse_exact_skill_runtime_anchor_command(
        command,
        frozenset(
            {
                "preflight.run.with_anchor",
                "preflight.run.with_human_context",
                "preflight.run.with_agent_report",
            }
        ),
        project_root,
    )


def parse_exact_skill_runtime_contract_update_anchor_command(
    command: str, project_root: str | None = None
) -> ExactSkillRuntimeCommand | None:
    return _parse_exact_skill_runtime_anchor_command(
        command,
        frozenset(
            {
                "contract_update.run.with_anchor",
                "contract_update.run.with_human_context",
            }
        ),
        project_root,
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


def is_exact_skill_runtime_contract_update_anchor_executor_command(
    command: str, cwd: str, project_root: str, deadline: Deadline | None = None
) -> bool:
    parsed = parse_exact_skill_runtime_contract_update_anchor_command(command, project_root)
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
    return command_allows_root_no_worktree(parsed)


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
    "preflight.run.fixture.with_human_context": [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "{issue_number}",
        "--repo", "{repo}",
        "--fixture", "{fixture}",
        "--anchor-comment-url", "{anchor_comment_url}",
        "--human-context-comment-url", "{anchor_comment_url}",
        "--investigation-evidence-transport-path", "{investigation_evidence_transport_path}",
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
    "preflight.run.with_human_context": [
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
        "--human-context-comment-url",
        "{anchor_comment_url}",
        # #2086 P0 fix_delta (Blocker 1/2): optional trailing pair, dropped
        # by render_command()'s `optional_flag_pair` mechanism when absent
        # -- matched here verbatim for parity with
        # `_parse_exact_skill_runtime_anchor_command()`'s own optional
        # two-token suffix handling for this command_id only.
        "--investigation-evidence-transport-path",
        "{investigation_evidence_transport_path}",
    ],
    "preflight.run.with_agent_report": [
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
        "--agent-report-comment-url",
        "{anchor_comment_url}",
    ],
    "contract_update.run.with_anchor": [
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
        "--consume-contract-patch-plan",
    ],
    "contract_update.run.with_human_context": [
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
        "--human-context-comment-url",
        "{anchor_comment_url}",
        "--consume-contract-patch-plan",
    ],
    # #2086 AC10 (iteration 2): full argv template including #2053's
    # authority-transport extension (all six trailing placeholders are
    # optional_flag_pair/bool_flag in command_registry.py, matched here
    # verbatim for parity with `validate_registry_entry()`).
    "decide.run": [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py",
        "--loop-state-file",
        "{loop_state_file}",
        "--review-result-verdict",
        "{verdict}",
        "--max-iterations",
        "{max_iterations}",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
        "--authority-transport-path",
        "{authority_transport_manifest_path}",
        "{authority_expected}",
        "--invocation-id",
        "{invocation_id}",
        "--git-head-sha",
        "{git_head_sha}",
    ],
    # #2086 AC9/AC10 (iteration 2)
    "authority_transport.produce": [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
        "--invocation-id",
        "{invocation_id}",
        "--git-head-sha",
        "{git_head_sha}",
        "--produce-authority-transport",
        "{evidence_fixture_path}",
    ],
    # #2086 AC9/AC10 (iteration 2)
    "authority_transport.consume": [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
        "--invocation-id",
        "{invocation_id}",
        "--git-head-sha",
        "{git_head_sha}",
        "--consume-authority-transport",
        "{router_receipt_path}",
        "--contract-patch-plan-file",
        "{contract_patch_plan_file}",
        "--anchor-context-file",
        "{anchor_context_file}",
    ],
    # Issue #2039 AC8/AC11
    "repair_action.apply": [
        "uv",
        "run",
        "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
        "--apply-repair-action",
        "{preflight_result_path}",
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
    "preflight.run.fixture.with_human_context": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "fixture": {"type": "repo_relative_file", "required": True},
        "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        "investigation_evidence_transport_path": {
            "type": "repo_relative_file", "required": False, "optional_flag_pair": True,
        },
    },
    "preflight.run.with_anchor": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
    },
    "preflight.run.with_human_context": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        # #2086 P0 fix_delta (Blocker 1/2)
        "investigation_evidence_transport_path": {
            "type": "path", "required": False, "optional_flag_pair": True,
        },
    },
    "preflight.run.with_agent_report": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
    },
    "contract_update.run.with_anchor": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
    },
    "contract_update.run.with_human_context": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
    },
    # #2086 AC10 (iteration 2)
    "decide.run": {
        "loop_state_file": {"type": "repo_relative_file", "required": True},
        "verdict": {"type": "verdict", "required": True},
        "max_iterations": {"type": "positive_int", "required": False, "optional_flag_pair": True},
        "issue_number": {"type": "positive_int", "required": False, "optional_flag_pair": True},
        "repo": {"type": "owner_repo", "required": False, "optional_flag_pair": True},
        "authority_transport_manifest_path": {"type": "path", "required": False, "optional_flag_pair": True},
        "authority_expected": {"type": "bool_flag", "flag_literal": "--authority-expected"},
        "invocation_id": {"type": "string", "required": False, "optional_flag_pair": True},
        "git_head_sha": {"type": "string", "required": False, "optional_flag_pair": True},
    },
    # #2086 AC9/AC10 (iteration 2)
    "authority_transport.produce": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "invocation_id": {"type": "string", "required": True},
        "git_head_sha": {"type": "string", "required": True},
        "evidence_fixture_path": {"type": "path", "required": True},
    },
    # #2086 AC9/AC10 (iteration 2)
    "authority_transport.consume": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "invocation_id": {"type": "string", "required": True},
        "git_head_sha": {"type": "string", "required": True},
        "router_receipt_path": {"type": "path", "required": True},
        "contract_patch_plan_file": {"type": "path", "required": False, "optional_flag_pair": True},
        "anchor_context_file": {"type": "path", "required": False, "optional_flag_pair": True},
    },
    # Issue #2039 AC8/AC11
    "repair_action.apply": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
        "preflight_result_path": {"type": "path", "required": True},
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
    if command_id in {"contract_update.run.with_anchor", "contract_update.run.with_human_context"}:
        expected_write_roots.append("artifacts/{active_issue}/issue-metadata/")
    # #2086 AC10 (iteration 2): `decide.run` / `authority_transport.produce` /
    # `authority_transport.consume` all now use the same
    # `.claude/artifacts/issue-refinement-loop/{active_issue}/` write root as
    # every other eligible command_id (matching this module's own
    # eligible_command_ids declarations, which were already merged from
    # #2053/PR #2068 before this Issue's AC10 wiring made this function
    # actually check them) -- no per-command-id exception is needed.
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


# ---------------------------------------------------------------------------
# Issue #2196 (Child 1 of #2190's worktree-migration split): sanitized Git
# subprocess execution environment policy.
#
# Threat model (explicitly documented per this Issue's In Scope item):
# this policy defends against *misrouting* -- an inherited GIT_* variable,
# an ambient `url.<base>.insteadOf` rewrite, or a repo-local
# `.git/hooks/post-checkout` script silently firing -- from either an
# accidentally-inherited caller environment or an ordinary local Git
# config the dedicated subprocess did not expect. It does NOT defend
# against a same-UID active attacker who can already set arbitrary
# environment variables or write to this process's own filesystem: such an
# attacker has a strictly stronger position than anything checked below.
# ---------------------------------------------------------------------------

# GIT_* environment variables that must never leak into a dedicated Git
# subprocess's environment (Issue #2196 AC1). An inherited value for any of
# these silently redirects git's repository/object/config/executable
# discovery away from the caller-specified cwd (e.g. an ambient `GIT_DIR`
# pointing at an unrelated repository, or a `GIT_CONFIG` that only
# redirects a `git config` *probe* while the real command still reads
# repository-local config -- Issue #2196 PR #2201 owner review P1-1), which
# is exactly the misrouting class this Issue's threat model targets.
# `GIT_EXEC_PATH` / `GIT_CEILING_DIRECTORIES` are unset for the same
# reason as the discovery variables: `GIT_EXEC_PATH` can substitute a
# poisoned Git helper (e.g. a fake `git-remote-https`) ahead of the real
# one (P1-2), and `GIT_SSL_NO_VERIFY` is unset so TLS verification can
# never be silently disabled by an inherited value.
GIT_SUBPROCESS_UNSET_ENV_KEYS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXEC_PATH",
        "GIT_CEILING_DIRECTORIES",
        "GIT_SSL_NO_VERIFY",
    }
)

# The structured, name-only query pattern used to detect `url.<base>.insteadOf`
# and `url.<base>.pushInsteadOf` rewrite rules (Issue #2196 AC4 / P1-4).
# This is passed to `git config --get-regexp --name-only -z <pattern>` (or
# the modern `git config get --all --show-names --null --regexp <pattern>`
# on Git versions that support it) -- never to a hand-written split of the
# human-readable `git config --list` line format, which cannot
# distinguish a `=` inside a legal subsection name (e.g.
# `url.https://evil.example/?x=y.insteadof=https://good.example/`) from
# the key/value separator. Git normalizes the section and key parts of a
# config name to lowercase for matching purposes, so this pattern matches
# both `insteadOf`/`pushInsteadOf`-cased fixtures and their normalized
# lowercase form; only the subsection segment (`.*`) is genuinely
# case-sensitive, which is exactly the segment this pattern treats as an
# opaque wildcard.
INSTEADOF_CONFIG_NAME_REGEXP = r"^url\..*\.(insteadof|pushinsteadof)$"


class GitSubprocessRewriteRejected(RuntimeError):
    """Raised when the effective Git configuration a dedicated Git
    subprocess would see declares a `url.<base>.insteadOf` or
    `url.<base>.pushInsteadOf` rewrite rule (Issue #2196 AC4). Either
    directive silently rewrites the resolved remote URL for any command
    that references the rewritten base, which can redirect a fetch/push to
    an unintended remote without the caller's argv ever mentioning it --
    this is rejected fail-closed before the real command is ever
    executed."""


class GitSubprocessConfigProbeFailed(RuntimeError):
    """Raised when the `insteadOf`/`pushInsteadOf` config probe could not
    be positively evaluated as "no matching key" (Issue #2196 AC9 / P1-5).
    A nonzero exit code, a timeout, or a stdout decode failure can each
    mean a config syntax error, a permission error, a bad `include`
    directive, an I/O error, or some other failure unrelated to whether a
    rewrite rule exists -- none of those outcomes license treating the
    probe as having proven "no rewrite exists". Callers must fail closed
    (stop before running the real command) whenever this is raised,
    exactly as they do for `GitSubprocessRewriteRejected` itself."""


def reject_insteadof_rewrite(matched_key_names: list[str]) -> None:
    """Fail-closed rejection of `url.<base>.insteadOf` /
    `url.<base>.pushInsteadOf` rewrite rules.

    `matched_key_names` is the result of a *structured* query (e.g. `git
    config --get-regexp --name-only -z INSTEADOF_CONFIG_NAME_REGEXP`) for
    the effective configuration a dedicated Git subprocess would see for
    its target cwd -- never a hand split of `git config --list` line
    output (Issue #2196 P1-4). Any non-empty list raises
    `GitSubprocessRewriteRejected` naming the first offending key.
    """
    if matched_key_names:
        raise GitSubprocessRewriteRejected(matched_key_names[0])


def parse_config_get_regexp_name_only_nul(stdout: bytes) -> list[str]:
    """Parse the NUL-delimited stdout of `git config --get-regexp
    --name-only -z <pattern>` (or equivalent structured, name-only,
    NUL-delimited query) into a list of matched config key names.

    Raises `GitSubprocessConfigProbeFailed` if `stdout` cannot be decoded
    as UTF-8 -- an undecodable probe result is a probe failure, not
    evidence of "no matching keys" (Issue #2196 AC9 / P1-5).
    """
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitSubprocessConfigProbeFailed("probe_stdout_decode_error") from exc
    return [name for name in text.split("\0") if name]


# Git global options that, if smuggled into a dedicated Git subprocess's
# argv ahead of (or anywhere alongside) its subcommand, silently override
# this module's own `-c core.hooksPath=...` / `-c credential.helper=`
# wrapper settings -- re-enabling hooks, redirecting repository discovery,
# or injecting further config overrides invisible to the insteadOf probe
# (Issue #2196 AC8 / P1-3). The closed command builder API never accepts
# these from a caller; this set is also used as a defense-in-depth check
# on the internal argv the builders themselves construct.
GIT_GLOBAL_OPTION_TOKENS = frozenset(
    {
        "-c",
        "--config-env",
        "-C",
        "--git-dir",
        "--work-tree",
        "--exec-path",
        "--namespace",
    }
)


def reject_git_global_options(argv: list[str]) -> None:
    """Fail-closed rejection of Git global options appearing anywhere in a
    dedicated-Git-subprocess argv (Issue #2196 AC8 / P1-3).

    Rejects an exact match against `GIT_GLOBAL_OPTION_TOKENS`, and the
    `--long-option=value` form of any of the `--`-prefixed tokens in that
    set, wherever they occur in `argv` -- not only at position 0 -- since
    Git accepts its global options interleaved before the subcommand.
    """
    for token in argv:
        if token in GIT_GLOBAL_OPTION_TOKENS:
            raise ValueError(f"git_global_option_not_allowed:{token}")
        for prefix in GIT_GLOBAL_OPTION_TOKENS:
            if prefix.startswith("--") and token.startswith(prefix + "="):
                raise ValueError(f"git_global_option_not_allowed:{token}")


def reject_option_like_positional(value: str) -> None:
    """Fail-closed rejection of a positional argument that looks like a
    Git option (starts with `-`). Used by the closed command builders to
    stop a caller-supplied value (e.g. a ref name or object id) from being
    interpreted by Git as a flag instead of data (Issue #2196 P1-3)."""
    if value.startswith("-"):
        raise ValueError(f"positional_argument_looks_like_option:{value}")
