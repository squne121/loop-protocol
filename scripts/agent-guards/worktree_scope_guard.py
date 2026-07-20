#!/usr/bin/env python3
"""worktree_scope_guard.py — PreToolUse hook that blocks mutation outside the active issue worktree.

Contract: WORKTREE_SCOPE_RESOLUTION_V1 / MUTATING_BASH_CLASSIFIER_V1 (Issue #960).
Extended: LOCAL_MAIN_SCRATCH_ALLOW_V1 (Issue #974).

When an active issue worktree exists, Write/Edit/MultiEdit targeting paths outside
the expected worktree, and mutating Bash commands whose effective target is outside
the expected worktree, are blocked (fail-closed). Read-only Bash and worktree-internal
mutation are allowed.

LOCAL_MAIN_SCRATCH_ALLOW_V1: When cwd is the project root (local main checkout),
the current branch is main/default branch, and cwd is not inside any linked issue
worktree, Write/Edit/MultiEdit to safe_scratch_prefix paths that are:
  - gitignored by repo-local .gitignore (not global / .git/info/exclude only)
  - untracked (not in git index)
  - under SAFE_SCRATCH_PREFIXES (component-boundary match)
  - free of sensitive path patterns
  - free of symlink components
are allowed. Phase 1: Write/Edit/MultiEdit only. Bash write exception is Phase 2.

Exit codes:
  0  — allow (no stdout/stderr)
  2  — block (bounded stderr only: expected worktree + actual cwd)

Design notes:
- project_root is resolved via CLAUDE_PROJECT_DIR, else by walking up from __file__
  (the settings.json parent). `git rev-parse --show-toplevel` is NOT used because
  worktree isolation makes it return the main repo root.
- path containment uses os.path.realpath + os.path.commonpath (NOT startswith), so
  symlink-outside / `..` traversal / absolute-outside targets are blocked.
- Unparseable Bash that may still mutate is blocked (fail-closed) when an active
  issue worktree exists and the effective target cannot be proven inside it.
- file-write mutations (redirection, tee, sed/perl -i, interpreter one-liners) have
  their write-target path extracted and containment-checked; when a write mutation
  is detected but no target can be extracted, it is fail-closed (block) while a
  worktree exists.
- `bash -c|-lc` / `sh -c` / `zsh -c` wrappers are unwrapped and their inner script
  is recursively classified / target-extracted.
"""

import json
import os
import re
import shutil
import subprocess
import sys

_AGENT_GUARDS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "scripts",
    "agent-guards",
)
if _AGENT_GUARDS_DIR not in sys.path:
    sys.path.insert(0, _AGENT_GUARDS_DIR)

from skill_runtime_command_policy import (  # noqa: E402
    command_allows_root_no_worktree,
    current_branch,
    looks_like_skill_runtime_executor_command,
    parse_exact_skill_runtime_anchor_command,
    parse_exact_skill_runtime_command,
    resolve_active_issue,
    resolve_default_branch,
    resolve_repo_slug,
)
from git_mutation_command_policy import (  # noqa: E402
    COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY,
    COMMAND_CLASS_RTK_GIT_MERGE_DEFAULT_BRANCH_FF_ONLY,
    GitMutationPolicyResult,
    classify_rtk_git_mutation,
    execute_verified_ff_merge_transaction,
    execute_verified_default_branch_ff_merge_transaction,
    parse_controlled_git_change_exec_command,
)
from hook_repair_hints import render_hook_command_repair_hint, render_publish_safety_stop_report  # noqa: E402

# Issue #1241 marker: HOOK_COMMAND_REPAIR_HINT_V1 is rendered via hook_repair_hints.py.

# Shared one-shot V3 cleanup validator + worktree catalog (Issue #1137).
# Imported from scripts/agent-ops so V3 validation / per-operation command_hash /
# the three-valued loader / durable IO / git check-ref-format branch validation /
# the shared catalog are not re-implemented here. Import is fail-closed: if it is
# unavailable, V3 gating is skipped and only the legacy V2 path applies.
_AGENT_OPS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "scripts",
    "agent-ops",
)
if _AGENT_OPS_DIR not in sys.path:
    sys.path.insert(0, _AGENT_OPS_DIR)
try:
    import cleanup_contract_v3 as _cc3
    import worktree_catalog as _wcat

    _V3_AVAILABLE = True
    _RC_ROOT_DRIFT_ACTIVE_WT_MISMATCH = _cc3.ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH
except Exception:  # pragma: no cover - defensive fail-closed
    _cc3 = None
    _wcat = None
    _V3_AVAILABLE = False
    _RC_ROOT_DRIFT_ACTIVE_WT_MISMATCH = "root_drift_active_worktree_mismatch"

# Branch names in V3 contracts are validated with `git check-ref-format --branch`
# inside cleanup_contract_v3.is_valid_branch_ref (Issue #1137 Medium).

# Shared controlled skill mutation policy (Issue #1166).
# Imported from scripts/agent-guards so the same is_controlled_skill_mutation_exec_command
# function is consumed by both this guard and local_main_branch_guard (AC4/AC17).
_AGENT_GUARDS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "scripts",
    "agent-guards",
)
if _AGENT_GUARDS_DIR not in sys.path:
    sys.path.insert(0, _AGENT_GUARDS_DIR)
try:
    from controlled_skill_mutation_policy import (
        is_controlled_skill_mutation_exec_command as _is_csm_exec_command,
    )

    _CSM_POLICY_AVAILABLE = True
except Exception:  # pragma: no cover - defensive fail-closed

    def _is_csm_exec_command(cmd: str, project_root: str) -> bool:  # type: ignore[misc]
        return False

    _CSM_POLICY_AVAILABLE = False

_PUBLISH_REPORT_SCRIPT = ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"


def _extract_inner_python_argv(tokenized: list[str]) -> list[str] | None:
    """Extract the inner python/uv argv from a potentially-wrapped command.

    Returns the tokenized list starting with "uv" (for `uv run python3 ...`)
    or "python"/"python3", or None when no python invocation can be extracted.

    Handles:
    - Direct:          ["uv", "run", "python3", ...]
    - Direct:          ["python3", ...]
    - env VAR=VAL:     ["env", "K=V", ..., "uv", "run", "python3", ...]
    - command builtin: ["command", "python3", ...]
    - shell -c/-lc:    ["bash", "-lc", "uv run python3 script.py"]
    """
    import shlex  # noqa: PLC0415

    if not tokenized:
        return None

    t0 = tokenized[0]
    prog = os.path.basename(t0)

    # uv run python3 ...
    if prog == "uv" and len(tokenized) >= 3 and tokenized[1] == "run" and tokenized[2] == "python3":
        return tokenized

    # Direct python/python3
    if prog in {"python", "python3"}:
        return tokenized

    # env VAR=VAL ... <python-cmd>
    if prog == "env":
        rest = list(tokenized[1:])
        while rest and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[0]):
            rest = rest[1:]
        return _extract_inner_python_argv(rest)

    # command python3 ...
    if prog == "command":
        return _extract_inner_python_argv(tokenized[1:])

    # bash/sh/zsh -c/-lc <script>
    if prog in _SHELL_WRAPPERS:
        script = _extract_shell_wrapper_script(tokenized)
        if script is not None:
            try:
                inner_toks = shlex.split(script, posix=True)
            except ValueError:
                inner_toks = script.split()
            return _extract_inner_python_argv(inner_toks)
        return None

    return None


def _is_direct_publish_termination_command(cmd: str, project_root: str) -> bool:
    """Return True iff command directly executes publish_termination_report.py.

    This command must be executed via controlled_skill_mutation_exec.py instead.
    Detects wrapper forms (bash -c, env VAR=VAL, command) in addition to direct
    uv run python3 / python3 invocations.
    """
    try:
        toks = _tokenize(cmd)
    except Exception:
        return False
    if not toks:
        return False

    inner = _extract_inner_python_argv(toks)
    if inner is None:
        return False

    if inner[:3] == ["uv", "run", "python3"]:
        args = inner[3:]
    elif os.path.basename(inner[0]) in {"python", "python3"}:
        args = inner[1:]
    else:
        return False

    if not args or args[0].startswith("-"):
        return False
    if os.path.isabs(args[0]):
        script = os.path.realpath(args[0])
    else:
        script = os.path.realpath(os.path.join(project_root, args[0]))

    return script == os.path.realpath(os.path.join(project_root, _PUBLISH_REPORT_SCRIPT))


# Worktree bootstrap executor policy (Issue #1209).
# Shared policy function — same is_exact_worktree_bootstrap_executor_command as
# consumed by local_main_branch_guard (no split-brain allowlist).
_AGENT_OPS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "scripts",
    "agent-ops",
)
if _AGENT_OPS_DIR not in sys.path:
    sys.path.insert(0, _AGENT_OPS_DIR)
try:
    from worktree_bootstrap_command_policy import (
        is_exact_worktree_bootstrap_executor_command as _wbe_exec_command,
    )

    _WBE_POLICY_AVAILABLE = True
except Exception:  # pragma: no cover - defensive fail-closed

    def _wbe_exec_command(cmd: str, cwd: str, project_root: str, deadline: object | None = None) -> bool:  # type: ignore[misc]
        return False

    _WBE_POLICY_AVAILABLE = False

# Agent-ops tools allowed as an exact command class from the local main root even
# when an issue worktree is active (Issue #1137 Blocker 1). realpath-matched.
_AGENT_OPS_ALLOWED_SCRIPTS = (
    "scripts/agent-ops/cleanup_exec.py",
    "scripts/agent-ops/guard_preflight.py",
    "scripts/agent-ops/materialize_cleanup_contract.py",
    "scripts/agent-ops/git_ref_probe.py",
    "scripts/agent-ops/git_worktree_probe.py",
)

# Per-script exact argv allowlist (Issue #1137 Blocker 1). Only these flags are
# accepted, none may repeat, value-flags must be followed by exactly one value
# token, and ``--flag=value`` forms are rejected for unambiguous parsing. The
# public ``--project-root`` / ``--no-verify`` escape hatches are intentionally
# absent so an agent cannot retarget the executor or skip authorization.
_AGENT_OPS_ARG_SPECS = {
    "scripts/agent-ops/cleanup_exec.py": {
        "value_flags": frozenset({"--pr-number", "--linked-issue-number", "--worktree-path", "--branch-name"}),
        "bool_flags": frozenset({"--json"}),
        "required": frozenset({"--pr-number", "--worktree-path", "--branch-name"}),
    },
    "scripts/agent-ops/guard_preflight.py": {
        "value_flags": frozenset({"--cwd"}),
        "bool_flags": frozenset({"--json"}),
        "required": frozenset(),
    },
    "scripts/agent-ops/materialize_cleanup_contract.py": {
        "value_flags": frozenset(
            {"--pr-number", "--linked-issue-number", "--worktree-path", "--branch-name", "--operation", "--ttl-seconds"}
        ),
        "bool_flags": frozenset({"--json"}),
        "required": frozenset({"--pr-number", "--worktree-path", "--branch-name"}),
    },
    "scripts/agent-ops/git_ref_probe.py": {
        "value_flags": frozenset({"--branch", "--remote"}),
        "bool_flags": frozenset({"--json"}),
        "required": frozenset({"--branch"}),
    },
    "scripts/agent-ops/git_worktree_probe.py": {
        "value_flags": frozenset(),
        "bool_flags": frozenset({"--json"}),
        "required": frozenset(),
    },
}


def _validate_agent_ops_argv(rel_script: str, args: list[str]) -> bool:
    """True iff ``args`` is an exact, non-redundant argv for ``rel_script``.

    Rejects unknown flags (including ``--project-root`` / ``--no-verify``),
    duplicates, ``--flag=value`` forms, positionals (which is how an injected
    trailing ``git worktree remove`` would appear), value-flags missing a value,
    any value that itself looks like a flag, and missing required flags.
    """
    spec = _AGENT_OPS_ARG_SPECS.get(rel_script)
    if spec is None:
        return False
    value_flags = spec["value_flags"]
    bool_flags = spec["bool_flags"]
    seen: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i]
        if not tok.startswith("--") or "=" in tok:
            return False
        if tok in seen:
            return False
        seen.add(tok)
        if tok in bool_flags:
            i += 1
            continue
        if tok in value_flags:
            if i + 1 >= len(args):
                return False
            if args[i + 1].startswith("--"):
                return False
            i += 2
            continue
        return False
    return spec["required"].issubset(seen)


# ── Tool classes ──────────────────────────────────────────────────────────────
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}
BASH_TOOL = "Bash"

# Matched mutation tools: a matched PreToolUse tool for which malformed payload is
# fail-closed. The hook matcher is "Bash|Write|Edit|MultiEdit".
MATCHED_TOOLS = WRITE_TOOLS | {BASH_TOOL}


# =============================================================================
# LOCAL_MAIN_SCRATCH_ALLOW_V1 — safe scratch paths on local main checkout
# =============================================================================

# Safe scratch prefixes (component-boundary match required, not string prefix).
# These must also be present in repo-local .gitignore.
SAFE_SCRATCH_PREFIXES = (
    "artifacts",
    "playwright-report",
    "test-results",
    "coverage",
    ".cache",
)

# Sensitive path patterns — any target matching these is ALWAYS blocked
# regardless of prefix / ignore status.
_SENSITIVE_PATTERNS = re.compile(
    r"""
    (^|/) (
        \.env[^/]*         |   # .env, .env.local, .env.production, etc.
        [^/]*\.pem         |   # *.pem
        [^/]*\.key         |   # *.key
        [^/]*\.p12         |   # *.p12
        [^/]*\.pfx         |   # *.pfx
        [^/]*token[^/]*    |   # *token*
        [^/]*secret[^/]*   |   # *secret*
        [^/]*credential[^/]* | # *credential*
        \.npmrc            |   # .npmrc
        \.pypirc           |   # .pypirc
        \.netrc            |   # .netrc
        \.ssh(/|$)         |   # .ssh/**
        \.config/gh(/|$)       # .config/gh/**
    ) ($|/)
    """,
    re.VERBOSE,
)

# Regex for main/default branch names
_MAIN_BRANCH_RE = re.compile(r"^(main|master|trunk)$")


def _is_sensitive_path(relpath: str) -> bool:
    """True iff relpath matches a sensitive path pattern."""
    # Normalize to forward slashes for matching
    normalized = relpath.replace(os.sep, "/")
    return bool(_SENSITIVE_PATTERNS.search("/" + normalized))


def _is_under_safe_prefix(relpath: str) -> bool:
    """True iff relpath is under one of the SAFE_SCRATCH_PREFIXES (component boundary).

    Uses os.path.commonpath to ensure component-boundary matching.
    'artifacts_evil/foo' is NOT under 'artifacts'.
    'artifacts/../README.md' is NOT under 'artifacts'.
    """
    if not relpath:
        return False
    # Normalize to remove any .. components first via os.path.normpath
    normalized = os.path.normpath(relpath)
    # Reject if normpath escapes the root (e.g., ../../something)
    if normalized.startswith(".."):
        return False
    parts = normalized.split(os.sep)
    if not parts or not parts[0]:
        return False
    top_component = parts[0]
    return top_component in SAFE_SCRATCH_PREFIXES


def _repo_default_branch(project_root: str, deadline: "object | None" = None) -> str | None:
    """Return the repository default branch name from remote HEAD, or None.

    Uses 'git symbolic-ref refs/remotes/origin/HEAD' to get the true default
    branch, not the current branch. Falls back to None if unavailable.
    """
    git = shutil.which("git")
    if not git:
        return None
    timeout = _deadline_timeout(deadline, 5.0)
    if timeout is None:
        return None
    try:
        out = subprocess.run(
            [git, "-C", project_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if out.returncode == 0:
            ref = out.stdout.strip()
            # ref is e.g. "origin/main" -> extract just "main"
            if "/" in ref:
                branch = ref.split("/", 1)[1]
            else:
                branch = ref
            if branch:
                return branch
    except Exception:
        pass
    return None


def _is_local_main_context(cwd_realpath: str, project_root: str) -> bool:
    """True iff all three local-main context conditions are met (AC1):

    1. cwd_realpath == project_root_realpath
    2. current branch is main/default branch
    3. cwd is NOT inside any linked issue worktree
    """
    project_root_real = os.path.realpath(project_root)
    cwd_real = os.path.realpath(cwd_realpath)

    # Condition 1: cwd == project_root
    if cwd_real != project_root_real:
        return False

    # Condition 2: current branch is main/default branch
    # allowed_branches = {"main", "master", "trunk"} union repo default branch
    branch = _current_branch(project_root)
    if not branch:
        return False
    allowed_branches = set(_MAIN_BRANCH_RE.pattern.strip("^()$").split("|"))
    default = _repo_default_branch(project_root)
    if default:
        allowed_branches.add(default)
    if branch not in allowed_branches:
        return False

    # Condition 3: cwd is not inside any linked issue worktree
    # The project_root itself is not inside a worktree (worktrees are subpaths),
    # but we explicitly verify via the worktree catalog.
    catalog = list_worktrees(project_root)
    if catalog is None:
        # git unavailable — fail-closed
        return False

    for wt in catalog:
        wt_path = wt.get("worktree")
        if not wt_path:
            continue
        wt_real = os.path.realpath(wt_path)
        # Skip the main worktree itself
        if wt_real == project_root_real:
            continue
        # If cwd is inside any non-main worktree, it's not local main context
        try:
            common = os.path.commonpath([wt_real, cwd_real])
        except ValueError:
            continue
        if common == wt_real:
            return False

    return True


def _has_symlink_component(target_path: str, safe_root_real: str) -> bool:
    """True iff any existing path component in target_path (under safe_root) is a symlink.

    Also checks: safe_root itself must exist and not be a symlink.
    Checks every component from safe_root down to target_path (existing ones).
    Non-existent intermediate directories are OK (as long as none of the
    existing parent directories are symlinks).

    Returns True (block) if:
    - safe_root does not exist
    - safe_root is a symlink
    - any existing component under safe_root (up to target) is a symlink
    - target itself exists and is a symlink
    """
    # safe_root must exist and be a real directory (not a symlink)
    if not os.path.exists(safe_root_real):
        return True  # block: safe_root doesn't exist
    if os.path.islink(safe_root_real):
        return True  # block: safe_root is a symlink

    # Walk from safe_root down to target_path, checking existing components
    # target_path is already absolute
    # Compute relative path from safe_root to target
    try:
        rel = os.path.relpath(target_path, safe_root_real)
    except ValueError:
        return True  # different drives — block

    if rel.startswith(".."):
        return True  # target is outside safe_root — block

    parts = rel.split(os.sep)
    current = safe_root_real
    for part in parts:
        current = os.path.join(current, part)
        if os.path.exists(current) or os.path.islink(current):
            if os.path.islink(current):
                return True  # symlink component found — block

    return False


def _is_git_tracked(relpath: str, project_root: str) -> bool:
    """True iff relpath is tracked (in git index). AC7."""
    git = shutil.which("git")
    if not git:
        return True  # fail-closed: can't check → assume tracked
    try:
        out = subprocess.run(
            [git, "-C", project_root, "ls-files", "--error-unmatch", "--", relpath],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.returncode == 0
    except Exception:
        return True  # fail-closed


def _is_repo_local_gitignore(relpath: str, project_root: str) -> bool:
    """True iff relpath is gitignored by repo-local .gitignore (not global/exclude only). AC8.

    Uses `git check-ignore -v -- <rel>` and parses the source line.
    Returns False (block) when:
    - returncode != 0 (not ignored at all)
    - source is ~/.gitignore_global, ~/.config/git/ignore, .git/info/exclude, etc.
    Returns True (allow) only when source is a repo-local .gitignore file.
    """
    git = shutil.which("git")
    if not git:
        return False  # fail-closed

    try:
        out = subprocess.run(
            [git, "-C", project_root, "check-ignore", "-v", "--", relpath],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False  # fail-closed

    if out.returncode != 0:
        # Not ignored at all
        return False

    # Output format: <source>:<linenum>:<pattern>\t<pathname>
    stdout = out.stdout.strip()
    if not stdout:
        return False

    # Extract the source file (before the first colon)
    source_part = stdout.split(":")[0] if ":" in stdout else ""
    if not source_part:
        return False

    # Resolve source path. git check-ignore -v may return a relative path
    # (e.g. ".gitignore"), which must be resolved relative to project_root,
    # not relative to the current working directory of this Python process.
    project_root_real = os.path.realpath(project_root)
    if os.path.isabs(source_part):
        source_real = os.path.realpath(source_part)
    else:
        source_real = os.path.realpath(os.path.join(project_root_real, source_part))

    # Accept only sources inside project_root (repo-local .gitignore files)
    # Reject: global excludes, ~/.gitignore_global, ~/.config/git/ignore,
    # .git/info/exclude (which is inside .git/ directory)
    try:
        common = os.path.commonpath([project_root_real, source_real])
    except ValueError:
        return False

    if common != project_root_real:
        # Source is outside project root — global ignore or home-dir gitignore
        return False

    # Reject .git/info/exclude (inside .git directory)
    git_dir = os.path.join(project_root_real, ".git")
    try:
        git_common = os.path.commonpath([git_dir, source_real])
    except ValueError:
        pass
    else:
        if git_common == os.path.realpath(git_dir):
            return False

    return True


def local_main_scratch_allow_v1(target: str, cwd: str, project_root: str, tool_name: str) -> bool:
    """LOCAL_MAIN_SCRATCH_ALLOW_V1: Allow Write/Edit/MultiEdit to safe scratch paths.

    Returns True iff the target should be allowed under the local main scratch exception.
    Returns False (fall through to normal guard logic) if any condition is not met.

    Conditions (all must pass):
    - tool_name is Write, Edit, or MultiEdit (Phase 1 only — AC10)
    - cwd is local main context (AC1)
    - target is absolute or resolved relative to cwd
    - target_realpath is inside project_root
    - target_realpath is NOT inside .git/** or .claude/worktrees/**
    - normalized relpath is under SAFE_SCRATCH_PREFIXES (component boundary — AC4)
    - NOT a sensitive path (AC6)
    - NOT git-tracked / in index (AC7)
    - gitignored by repo-local .gitignore (AC8)
    - no symlink component in path (AC5)
    - NOT a git/gh/package-manager mutation context (AC9)
    """
    # AC10: Phase 1 — Write/Edit/MultiEdit only
    if tool_name not in WRITE_TOOLS:
        return False

    # AC9: git/gh/package-manager mutation — scratch exception does not apply
    # (Write/Edit/MultiEdit to git internals is blocked by other checks)
    # No special check needed here as we check against .git/** below.

    # Resolve target to absolute path
    if not target:
        return False
    if not os.path.isabs(target):
        target = os.path.join(cwd, target)
    target_real = os.path.realpath(target)
    project_root_real = os.path.realpath(project_root)

    # AC1: local main context check
    cwd_real = os.path.realpath(cwd)
    if not _is_local_main_context(cwd_real, project_root):
        return False

    # target must be inside project_root
    try:
        common = os.path.commonpath([project_root_real, target_real])
    except ValueError:
        return False
    if common != project_root_real:
        return False

    # target must NOT be inside .git/**
    git_dir = os.path.join(project_root_real, ".git")
    try:
        git_common = os.path.commonpath([git_dir, target_real])
    except ValueError:
        pass
    else:
        if git_common == os.path.realpath(git_dir):
            return False

    # target must NOT be inside .claude/worktrees/**
    worktrees_dir = os.path.join(project_root_real, ".claude", "worktrees")
    try:
        wt_common = os.path.commonpath([os.path.realpath(worktrees_dir), target_real])
    except ValueError:
        pass
    else:
        if wt_common == os.path.realpath(worktrees_dir):
            return False

    # Compute relpath from project_root
    try:
        relpath = os.path.relpath(target_real, project_root_real)
    except ValueError:
        return False

    # AC4: component-boundary prefix check
    if not _is_under_safe_prefix(relpath):
        return False

    # AC6: sensitive path denylist
    if _is_sensitive_path(relpath):
        return False

    # AC7: must NOT be git-tracked
    if _is_git_tracked(relpath, project_root):
        return False

    # AC8: must be gitignored by repo-local .gitignore
    if not _is_repo_local_gitignore(relpath, project_root):
        return False

    # AC5: symlink policy
    # Determine the safe_root (top-level prefix dir)
    top_component = os.path.normpath(relpath).split(os.sep)[0]
    safe_root = os.path.join(project_root_real, top_component)
    safe_root_real = os.path.realpath(safe_root)

    if _has_symlink_component(target_real, safe_root_real):
        return False

    return True


# =============================================================================
# Block emission (bounded stderr — no command / path / worktree list / env leak)
# =============================================================================


def _block(expected_worktree: str, actual_cwd: str) -> None:
    """Emit a bounded block message (<= 20 lines) and exit 2.

    Only expected worktree and actual cwd are shown. No tool command, tool input
    path, worktree list, or env values are emitted.
    """
    lines = [
        "[worktree_scope_guard] blocked: mutation outside active issue worktree",
        f"expected_worktree: {expected_worktree or '<unresolved>'}",
        f"actual_cwd: {actual_cwd or '<unknown>'}",
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.exit(2)


def _block_with_reason(
    expected_worktree: str,
    actual_cwd: str,
    reason_code: str,
    command_class: str,
    policy_result: object | None = None,
) -> None:
    lines = [
        "[worktree_scope_guard] blocked: mutation outside active issue worktree",
        f"expected_worktree: {expected_worktree or '<unresolved>'}",
        f"actual_cwd: {actual_cwd or '<unknown>'}",
        *render_hook_command_repair_hint(
            blocked_command_class=command_class,
            reason_code=reason_code,
            expected_remote_head=getattr(policy_result, "expected_remote_head", None),
            current_remote_head=getattr(policy_result, "current_remote_head", None),
            local_head=getattr(policy_result, "local_head", None),
            verified_head=getattr(policy_result, "verified_head", None),
            declared_publish_head=getattr(policy_result, "declared_publish_head", None),
            allowed_paths_gate_status=getattr(policy_result, "allowed_paths_gate_status", None),
            target_branch=getattr(policy_result, "target_branch", None),
            pr_number=getattr(policy_result, "pr_number", None),
            remote_readback_source=getattr(policy_result, "remote_readback_source", None),
            decision_inputs_complete=getattr(policy_result, "decision_inputs_complete", None),
            required_decisions=getattr(policy_result, "required_decisions", ()),
        ),
    ]
    if getattr(policy_result, "target_branch", None) and command_class == "rtk_git_push":
        lines.extend(
            render_publish_safety_stop_report(
                issue_number=os.environ.get("LOOP_ISSUE_NUMBER", "<unknown>"),
                blocked_command_class=command_class,
                reason_code=reason_code,
                target_branch=getattr(policy_result, "target_branch"),
                expected_remote_head=getattr(policy_result, "expected_remote_head", None),
                current_remote_head=getattr(policy_result, "current_remote_head", None),
                local_head=getattr(policy_result, "local_head", None),
                verified_head=getattr(policy_result, "verified_head", None),
                declared_publish_head=getattr(policy_result, "declared_publish_head", None),
                allowed_paths_gate_status=getattr(policy_result, "allowed_paths_gate_status", None),
                pr_number=getattr(policy_result, "pr_number", None),
                remote_readback_source=getattr(policy_result, "remote_readback_source", None),
                decision_inputs_complete=getattr(policy_result, "decision_inputs_complete", None),
                required_decisions=getattr(policy_result, "required_decisions", ()),
            )
        )
    sys.stderr.write("\n".join(lines) + "\n")
    sys.exit(2)


def _allow() -> None:
    """Allow the tool call (exit 0, no output)."""
    sys.exit(0)


# =============================================================================
# project_root resolution (WORKTREE_SCOPE_RESOLUTION_V1.project_root_source_precedence)
# =============================================================================


def resolve_project_root() -> str:
    """Resolve project root.

    Precedence:
      1. CLAUDE_PROJECT_DIR
      2. settings_json_parent_resolution — walk up from this file
         (<root>/scripts/agent-guards/worktree_scope_guard.py) so the parent
         of `scripts/agent-guards` is the project root. Anchored on
         __file__, never on `git rev-parse --show-toplevel`.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return os.path.realpath(env_root)
    # __file__ = <root>/scripts/agent-guards/worktree_scope_guard.py
    here = os.path.realpath(__file__)
    agent_guards_dir = os.path.dirname(here)
    scripts_dir = os.path.dirname(agent_guards_dir)
    root = os.path.dirname(scripts_dir)
    return os.path.realpath(root)


# =============================================================================
# current issue resolution (WORKTREE_SCOPE_RESOLUTION_V1.current_issue_source_precedence)
# =============================================================================

_ISSUE_RE = re.compile(r"^(?:worktree-)?issue-(\d+)-")
_ISSUE_BASENAME_RE = re.compile(r"^issue-(\d+)-")
_ISSUE_BRANCH_RE = re.compile(r"^(?:worktree-)?issue-(\d+)-")


def resolve_current_issue(cwd: str, project_root: str) -> str | None:
    """Resolve the active issue number as a string.

    Precedence:
      1. env LOOP_ISSUE_NUMBER
      2. cwd basename matching /^issue-(\\d+)-/
      3. current branch matching /^issue-(\\d+)-/
    """
    env_issue = os.environ.get("LOOP_ISSUE_NUMBER")
    if env_issue and env_issue.strip().isdigit():
        return env_issue.strip()

    if cwd:
        base = os.path.basename(os.path.normpath(cwd))
        m = _ISSUE_BASENAME_RE.match(base)
        if m:
            return m.group(1)

    branch = _current_branch(cwd or project_root)
    if branch:
        m = _ISSUE_BRANCH_RE.match(branch)
        if m:
            return m.group(1)

    return None


def _deadline_timeout(deadline: "object | None", maximum: float) -> float | None:
    """Per-call timeout clamped to a shared Deadline; None on exhaustion (Blocker 8)."""
    if deadline is None:
        return maximum
    try:
        return deadline.subprocess_timeout(maximum)
    except Exception:
        return None  # budget exhausted → caller treats as unavailable (fail-closed)


def _current_branch(path: str, deadline: "object | None" = None) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    timeout = _deadline_timeout(deadline, 5.0)
    if timeout is None:
        return None
    try:
        out = subprocess.run(
            [git, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# =============================================================================
# worktree catalog (WORKTREE_SCOPE_RESOLUTION_V1.worktree_catalog + parser)
# =============================================================================


def parse_worktree_porcelain_z(data: str) -> list[dict]:
    """Parse `git worktree list --porcelain -z` output (NUL-separated records).

    The -z form separates *attribute lines* by NUL. A worktree record starts with
    a `worktree <path>` line and continues until the next `worktree ` line (or end).
    Returns a list of dicts with at least 'worktree' and optionally 'branch'.
    """
    worktrees: list[dict] = []
    current: dict | None = None
    # -z separates each attribute by a single NUL byte.
    for field in data.split("\0"):
        if field == "":
            continue
        # Each field is like "worktree /path", "HEAD <sha>", "branch refs/heads/x",
        # "bare", "detached", "locked", "prunable".
        if field.startswith("worktree "):
            if current is not None:
                worktrees.append(current)
            current = {"worktree": field[len("worktree ") :]}
        elif current is not None:
            if field.startswith("branch "):
                current["branch"] = field[len("branch ") :]
            elif " " in field:
                key, _, value = field.partition(" ")
                current[key] = value
            else:
                current[field] = True
    if current is not None:
        worktrees.append(current)
    return worktrees


def list_worktrees(project_root: str) -> list[dict] | None:
    """Return parsed worktree catalog, or None if git is unavailable / fails."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run(
            [git, "-C", project_root, "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return parse_worktree_porcelain_z(out.stdout)


# =============================================================================
# expected worktree selection (WORKTREE_SCOPE_RESOLUTION_V1.expected_worktree_selection)
# =============================================================================


class WorktreeResolution:
    """Result of expected-worktree resolution."""

    def __init__(self, expected: str | None, match_count: int, git_available: bool):
        self.expected = expected  # realpath of expected worktree, or None
        self.match_count = match_count  # number of catalog matches
        self.git_available = git_available


def resolve_expected_worktree(
    issue: str | None, project_root: str, deadline: "object | None" = None
) -> WorktreeResolution:
    """Select the expected worktree for the active issue.

    Issue #1137 Blocker 7: delegates selection to the shared ``worktree_catalog``
    SSOT (``select_issue_worktrees``) so the runtime guard and ``guard_preflight``
    never disagree. The canonical rule still requires BOTH the branch short-name
    AND the path basename to match ``(worktree-)?issue-<issue>-*``. When the shared
    module is unavailable a local parser fallback preserves the same rule.
    """
    if not issue:
        return WorktreeResolution(None, 0, shutil.which("git") is not None)

    if _V3_AVAILABLE and _wcat is not None:
        try:
            catalog = _wcat.list_worktrees(project_root, deadline)
        except _wcat.GuardDeadlineExceeded:
            return WorktreeResolution(None, 0, True)  # fail-closed (0 matches)
        if catalog is None:
            return WorktreeResolution(None, 0, False)
        matches = _wcat.select_issue_worktrees(catalog, issue, os.path.realpath(project_root))
        if len(matches) == 1:
            return WorktreeResolution(matches[0].get("worktree_realpath"), 1, True)
        return WorktreeResolution(None, len(matches), True)

    catalog = list_worktrees(project_root)
    if catalog is None:
        # git unavailable / failed
        return WorktreeResolution(None, 0, False)

    branch_re = re.compile(r"^refs/heads/(?:worktree-)?issue-%s-" % re.escape(issue))
    base_re = re.compile(r"^issue-%s-" % re.escape(issue))

    matches_local: list[str] = []
    for wt in catalog:
        path = wt.get("worktree")
        if not path:
            continue
        base = os.path.basename(os.path.normpath(path))
        branch = wt.get("branch", "")
        branch_ok = bool(branch) and bool(branch_re.match(branch))
        base_ok = bool(base_re.match(base))
        if branch_ok and base_ok:
            matches_local.append(os.path.realpath(path))

    if len(matches_local) == 1:
        return WorktreeResolution(matches_local[0], 1, True)
    return WorktreeResolution(None, len(matches_local), True)


# =============================================================================
# path containment (AC11)
# =============================================================================


def is_inside(expected_realpath: str, target_path: str, cwd: str) -> bool:
    """True iff target_path resolves inside expected_realpath.

    Uses realpath + commonpath (NOT startswith). Relative target paths are
    resolved against cwd. Handles `..` traversal, symlink-outside, absolute-outside.
    """
    if not target_path:
        return False
    if not os.path.isabs(target_path):
        base = cwd if cwd else os.getcwd()
        target_path = os.path.join(base, target_path)
    actual = os.path.realpath(target_path)
    expected = os.path.realpath(expected_realpath)
    try:
        common = os.path.commonpath([expected, actual])
    except ValueError:
        # Different drives / mixed abs-rel — treat as outside.
        return False
    return common == expected


# =============================================================================
# Bash mutation classifier (MUTATING_BASH_CLASSIFIER_V1)
# =============================================================================

_GIT_MUTATING_SUBCMDS = {
    "add",
    "commit",
    "push",
    "checkout",
    "switch",
    "restore",
    "reset",
    "rebase",
    "merge",
    "cherry-pick",
    "revert",
    "am",
    "apply",
    "rm",
    "mv",
    "tag",
}
# Git add options that widen scope beyond explicit pathspec-only operations.
_GIT_ADD_STRICT_PROHIBITED_OPTS = {
    "-A",
    "--all",
    "-a",
    "-u",
    "--update",
    "--patch",
    "-p",
    "-i",
    "--interactive",
    "--intent-to-add",
    "-N",
    "--dry-run",
}
# git stash mutates unless list/show; git worktree mutates unless list.
_GH_PR_MUTATING = {
    "create",
    "edit",
    "merge",
    "review",
    "comment",
    "close",
    "reopen",
    "ready",
    "draft",
    "lock",
    "unlock",
}
_GH_ISSUE_MUTATING = {
    "create",
    "edit",
    "comment",
    "close",
    "reopen",
    "delete",
    "lock",
    "unlock",
}
_PKG_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_PKG_MUTATING = {
    "add",
    "install",
    "remove",
    "update",
    "publish",
    "version",
    "link",
    "unlink",
    "i",
    "rm",
    "un",
    "uninstall",
}

# read-only allowlist (worktree 解決不能でも allow)
_GIT_READONLY = {"status", "diff", "log", "show", "rev-parse"}

# General-purpose read-only programs that do NOT mutate the filesystem by their
# own action. An external absolute-path *positional* argument to one of these is a
# read source, not a write destination, so it must NOT trigger an over-block. Note:
# redirection / `tee` write-target checks still apply (they are checked BEFORE this
# allowlist via _is_file_write_mutation), so `cat x > ROOT/y` / `... | tee ROOT/y`
# are still blocked via the write-target path.
# `sed`/`perl` are intentionally EXCLUDED here: they are read-only only when no
# `-i` in-place flag is present, which is handled separately (an `-i` form is
# already classified as a file-write mutation upstream).
_READONLY_PROGRAMS = {
    "cat",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ls",
    "head",
    "tail",
    "wc",
    "find",
    "awk",
    "cut",
    "sort",
    "uniq",
    "nl",
    "tr",
    "column",
    "diff",
    "cmp",
    "comm",
    "file",
    "stat",
    "realpath",
    "readlink",
    "dirname",
    "basename",
    "pwd",
    "echo",
    "printf",
    "test",
    "true",
    "false",
    "which",
    "type",
    "command-v",
    "date",
    "env",
    "printenv",
    "id",
    "whoami",
    "uname",
    "hostname",
    "less",
    "more",
    "tac",
    "od",
    "xxd",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "jq",
    "yq",
    "xargs",
}
# sed/perl are read-only only when NOT in-place (`-i`). An in-place form is
# already classified as a file-write mutation upstream, so here (post-write-check)
# any remaining sed/perl is read-only.
_CONDITIONAL_READONLY_PROGRAMS = {"sed", "perl"}

# Option flags that indicate a write destination for an otherwise-unknown program.
# When one of these carries an external absolute path, the unknown program is
# treated as a file-writer (Major formatter-write risk) and blocked. A bare
# external *positional* arg (no write option) is treated as a read source → allow.
_WRITE_OPTION_FLAGS = {
    "-o",
    "--output",
    "--out",
    "-w",
    "--write",
    "--in-place",
    "--fix",
    "--output-file",
    "--outfile",
}

# shell wrappers whose `-c` / `-lc` script argument carries an inner command.
_SHELL_WRAPPERS = {"bash", "sh", "zsh"}


def _tokenize(command: str) -> list[str]:
    """Tokenize a shell command best-effort. On failure return a coarse split."""
    import shlex

    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


def _has_redirection_or_inplace(command: str) -> bool:
    """Detect shell file-write patterns: >, >>, tee, sed -i, perl -i."""
    # redirection (avoid matching >&2 fd-dup and >( process subst loosely; any
    # plain > / >> to a path is treated as a write — fail-closed bias).
    if re.search(r"(^|\s)\d*>>?(?!&)", command):
        return True
    if re.search(r"(^|[|;&]|&&)\s*tee\b", command):
        return True
    if re.search(r"\bsed\b[^|;&]*\s-[a-zA-Z]*i\b", command):
        return True
    if re.search(r"\bsed\b[^|;&]*\s-i\b", command):
        return True
    if re.search(r"\bperl\b[^|;&]*\s-[a-zA-Z]*i\b", command):
        return True
    return False


def _is_file_write_oneliner(command: str) -> bool:
    """Detect python/node/ruby file-write one-liners."""
    if re.search(r"\bpython3?\b[^|;&]*-c\b[^|;&]*\bopen\s*\([^)]*['\"][wax]", command):
        return True
    if re.search(r"\bnode\b[^|;&]*-e\b[^|;&]*(writeFile|createWriteStream|appendFile)", command):
        return True
    if re.search(r"\bruby\b[^|;&]*-e\b[^|;&]*(File\.(open|write)|\.write)", command):
        return True
    # generic: open(..., 'w') in any interpreter -c/-e
    if re.search(r"-[ce]\b[^|;&]*open\s*\([^)]*['\"][wax]\+?['\"]", command):
        return True
    return False


def _is_file_write_mutation(command: str) -> bool:
    """True iff the command performs a file-write (redirection / tee / -i / one-liner)."""
    return _has_redirection_or_inplace(command) or _is_file_write_oneliner(command)


def classify_bash(command: str) -> str:
    """Classify a Bash command.

    Returns one of:
      'read_only'  — known read-only allowlist; allow even if worktree unresolved.
      'mutating'   — known mutation; block if effective target is outside worktree.
      'unknown'    — cannot prove read-only; fail-closed (block) when worktree exists.
    """
    if not command or not command.strip():
        return "unknown"

    tokens = _tokenize(command)
    if not tokens:
        return "unknown"

    # Filesystem write patterns first (these mutate regardless of program).
    if _is_file_write_mutation(command):
        return "mutating"

    # Strip leading wrappers to find the effective program for classification.
    prog_tokens = _strip_wrappers_for_classification(tokens)
    if not prog_tokens:
        return "unknown"

    # `bash -c|-lc <script>` / `sh -c` / `zsh -c` wrapper → classify inner script.
    inner = _extract_shell_wrapper_script(prog_tokens)
    if inner is not None:
        return classify_bash(inner)

    prog = os.path.basename(prog_tokens[0])
    args = prog_tokens[1:]

    if prog == "git":
        return _classify_git(args)
    if prog == "gh":
        return _classify_gh(args)
    if prog in _PKG_MANAGERS:
        return _classify_pkg(args)

    # Pure read-only pipeline / single read-only program: every segment's
    # effective program is in the read-only allowlist (no file-write mutation —
    # already excluded above). `cat a | grep b`, `ls ROOT`, etc.
    if _is_readonly_pipeline(command):
        return "read_only"

    # Unknown program: cannot prove read-only.
    return "unknown"


def _segment_program(segment: str) -> str | None:
    """Return the effective program basename of a single pipeline segment, after
    stripping `command` / `env VAR=...` wrappers. Returns None when empty."""
    toks = _tokenize(segment)
    if not toks:
        return None
    toks = _strip_wrappers_for_classification(toks)
    if not toks:
        return None
    return os.path.basename(toks[0])


def _is_readonly_program(prog: str, segment: str) -> bool:
    """True iff `prog` is a known read-only program for this segment.

    `sed`/`perl` are read-only only when no in-place (`-i`) flag is present; an
    in-place form is classified as a file-write mutation upstream, so by the time
    we reach here a sed/perl segment is read-only.
    """
    if prog in _READONLY_PROGRAMS:
        return True
    if prog in _CONDITIONAL_READONLY_PROGRAMS:
        return not re.search(r"\s-[a-zA-Z]*i\b", segment)
    return False


def _is_readonly_pipeline(command: str) -> bool:
    """True iff every pipeline segment of `command` is a read-only program.

    Splits on `|` (pipe) only — `;`, `&&`, `||` chain heterogeneous commands and
    are conservatively NOT treated as a single read-only pipeline here (each such
    case falls through to 'unknown' for fail-closed bias). Redirection write
    targets are handled before this function is reached.
    """
    # Reject shell-control chaining so a hidden mutation in a later clause cannot
    # be masked by a read-only first clause.
    if re.search(r"(;|&&|\|\|)", command):
        return False
    segments = command.split("|")
    saw_one = False
    for seg in segments:
        seg = seg.strip()
        if not seg:
            return False
        prog = _segment_program(seg)
        if not prog:
            return False
        if not _is_readonly_program(prog, seg):
            return False
        saw_one = True
    return saw_one


def _strip_wrappers_for_classification(tokens: list[str]) -> list[str]:
    """Strip leading `command` and `env VAR=...` to expose the underlying program.

    Note: target-dir / write-target extraction (including `cd ... &&` and
    `bash/sh -c` wrappers) is handled by effective_target_dirs() and
    write_target_paths(); shell-wrapper unwrap for classification is handled by
    _extract_shell_wrapper_script().
    """
    t = list(tokens)
    # Strip `command`
    if t and t[0] == "command":
        t = t[1:]
    # Strip `env VAR=val ...`
    if t and t[0] == "env":
        t = t[1:]
        while t and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t[0]):
            t = t[1:]
    return t


def _extract_shell_wrapper_script(prog_tokens: list[str]) -> str | None:
    """If prog_tokens is `bash/sh/zsh [-l]* -c <script> ...` return <script>, else None.

    Accepts combined flags like `-lc` and separate `-l -c`. The script argument is
    the token immediately following the flag bundle that contains `c`.
    """
    if not prog_tokens:
        return None
    prog = os.path.basename(prog_tokens[0])
    if prog not in _SHELL_WRAPPERS:
        return None
    i = 1
    saw_c = False
    while i < len(prog_tokens):
        tok = prog_tokens[i]
        if tok.startswith("-") and tok != "--":
            # bundled flags, e.g. -lc / -c / -l
            if "c" in tok[1:]:
                saw_c = True
                i += 1
                break
            i += 1
            continue
        if tok == "--":
            i += 1
            break
        break
    if not saw_c:
        return None
    if i < len(prog_tokens):
        return prog_tokens[i]
    return None


def _classify_git(args: list[str]) -> str:
    # Find first non-flag, non `-C <path>` token as the subcommand.
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C":
            i += 2
            continue
        if a.startswith("-C"):
            i += 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    if i >= len(args):
        return "unknown"
    sub = args[i]
    rest = args[i + 1 :]
    if sub in _GIT_READONLY:
        return "read_only"
    if sub == "worktree":
        nxt = rest[0] if rest else ""
        if nxt == "list":
            return "read_only"
        if nxt == "remove":
            return "cleanup"
        return "mutating"
    if sub == "branch":
        # -d (safe delete) is cleanup class (AC4c); -D / --force / -f are mutating.
        if rest and rest[0] == "-d" and len(rest) == 2:
            return "cleanup"
        return "mutating"
    if sub == "stash":
        nxt = rest[0] if rest else ""
        return "read_only" if nxt in ("list", "show") else "mutating"
    if sub in _GIT_MUTATING_SUBCMDS:
        return "mutating"
    # Unknown git subcommand — fail-closed.
    return "unknown"


def _extract_inner_git_argv(tokenized: list[str]) -> list[str] | None:
    """Extract the inner git argv from a potentially-wrapped command.

    Returns the tokenized list whose first element is "git" (possibly followed
    by global options like -C <path>), or None when no git invocation can be
    extracted.

    Handles transparent wrappers:
    - Direct:          ["git", ...]  (including ["git", "-C", "/path", ...])
    - env VAR=VAL:     ["env", "K=V", ..., "git", ...]
    - command builtin: ["command", "git", ...]
    - rtk:             ["rtk", "git", ...]
    - shell -c/-lc:    ["bash", "-lc", "git add ."] → script re-tokenized, recurse
    """
    import shlex  # noqa: PLC0415

    if not tokenized:
        return None

    t0 = tokenized[0]
    prog = os.path.basename(t0)

    # Direct git invocation (including git -C <path> ...)
    if prog == "git":
        return tokenized

    # env VAR=VAL ... git ...
    if prog == "env":
        rest = list(tokenized[1:])
        while rest and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[0]):
            rest = rest[1:]
        return _extract_inner_git_argv(rest)

    # command git ...
    if prog == "command":
        return _extract_inner_git_argv(tokenized[1:])

    # rtk git ...
    if prog == "rtk":
        return _extract_inner_git_argv(tokenized[1:])

    # bash/sh/zsh -c/-lc <script>
    if prog in _SHELL_WRAPPERS:
        script = _extract_shell_wrapper_script(tokenized)
        if script is not None:
            try:
                inner_toks = shlex.split(script, posix=True)
            except ValueError:
                inner_toks = script.split()
            return _extract_inner_git_argv(inner_toks)
        return None

    return None


def _is_git_add_pathspec_violation(args: list[str], cwd: str | None = None) -> bool:
    """Issue #1215: reject non-explicit git add forms.

    Args:
        args: git arguments after "git" (may include global options like -C <path>).
        cwd:  working directory for directory-pathspec detection.

    Returns True when:
    - no explicit pathspec is provided;
    - prohibited add option is used (`-A`, `-u`, etc.);
    - pathspec appears to be broad (`.`, `..`, wildcard patterns, `:` magic);
    - `--pathspec-from-file` is used;
    - pathspec resolves to an existing directory under cwd.
    """
    if not args:
        return False

    # Skip global git options (e.g. -C <path>) to find the "add" subcommand.
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C":
            i += 2  # skip -C and its <path> argument
            continue
        if len(a) > 2 and a.startswith("-C"):
            i += 1  # -C<path> combined form
            continue
        if a == "--":
            break  # end of global options; not "add" subcommand
        if a.startswith("-"):
            i += 1  # other global option; skip one token conservatively
            continue
        break

    if i >= len(args) or args[i] != "add":
        return False

    pathspecs: list[str] = []
    allow_opts = True
    j = i + 1
    while j < len(args):
        arg = args[j]
        if arg == "--":
            allow_opts = False
            j += 1
            continue

        if allow_opts and arg.startswith("-"):
            # Hard-fail on explicitly broad add options.
            if arg in _GIT_ADD_STRICT_PROHIBITED_OPTS:
                return True
            # `--pathspec-from-file` can hold broad / dynamic patterns, so deny.
            if arg == "--pathspec-from-file" or arg.startswith("--pathspec-from-file="):
                return True
            if arg in {"-f", "--force"}:
                # force add is allowed only when followed by a pathspec.
                j += 1
                continue
            # Unknown/other options do not satisfy strict exact-path policy.
            return True
        else:
            pathspecs.append(arg)

        j += 1

    if not pathspecs:
        return True

    for pathspec in pathspecs:
        if pathspec in {".", ".."}:
            return True
        if any(ch in pathspec for ch in ("*", "?", "[")):
            return True
        if pathspec.startswith(":"):
            return True
        # Directory-wide pathspec: block if pathspec resolves to an existing directory.
        if os.path.isdir(os.path.join(cwd or ".", pathspec)):
            return True
    return False


def _classify_gh(args: list[str]) -> str:
    if not args:
        return "unknown"
    sub = args[0]
    rest = args[1:]
    if sub == "pr":
        action = rest[0] if rest else ""
        if action == "view":
            return "read_only"
        if action in _GH_PR_MUTATING:
            return "mutating"
        return "unknown"
    if sub == "issue":
        action = rest[0] if rest else ""
        if action == "view":
            return "read_only"
        if action in _GH_ISSUE_MUTATING:
            return "mutating"
        return "unknown"
    if sub == "api":
        return _classify_gh_api(rest)
    # other gh subcommands — fail-closed (cannot prove read-only).
    return "unknown"


def _classify_gh_api(args: list[str]) -> str:
    """gh api is GET by default but becomes a write with method/field/input flags."""
    method = None
    has_field = False
    explicit_get = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--method"):
            if i + 1 < len(args):
                method = args[i + 1].upper()
            i += 2
            continue
        m = re.match(r"^--method=(.+)$", a)
        if m:
            method = m.group(1).upper()
            i += 1
            continue
        m = re.match(r"^-X(.+)$", a)
        if m:
            method = m.group(1).upper()
            i += 1
            continue
        if a in ("-f", "-F", "--field", "--raw-field", "--input"):
            has_field = True
            i += 1
            continue
        if (
            a.startswith("-f")
            or a.startswith("-F")
            or a.startswith("--field=")
            or a.startswith("--raw-field=")
            or a.startswith("--input=")
        ):
            has_field = True
            i += 1
            continue
        i += 1

    if method == "GET":
        explicit_get = True

    write_methods = {"PATCH", "POST", "PUT", "DELETE"}
    if method in write_methods:
        return "mutating"
    if has_field and not explicit_get:
        return "mutating"
    # default GET / explicit GET with no field flags → read-only
    return "read_only"


def _classify_pkg(args: list[str]) -> str:
    if not args:
        return "unknown"
    sub = args[0]
    if sub in _PKG_MUTATING:
        return "mutating"
    # readonly-ish: list, ls, run, test, view, why, outdated, audit, etc.
    return "read_only"


# =============================================================================
# wrapper / explicit-target extraction (AC9)
# =============================================================================


def _abs_against(cwd: str, p: str) -> str:
    if not os.path.isabs(p):
        base = cwd if cwd else os.getcwd()
        p = os.path.join(base, p)
    return os.path.realpath(p)


def _inner_scripts(command: str) -> list[str]:
    """Return inner scripts from `bash/sh/zsh -c|-lc <script>` wrappers (recursive)."""
    tokens = _tokenize(command)
    prog_tokens = _strip_wrappers_for_classification(tokens)
    inner = _extract_shell_wrapper_script(prog_tokens)
    if inner is None:
        return []
    out = [inner]
    out.extend(_inner_scripts(inner))
    return out


def effective_target_dirs(command: str, cwd: str) -> list[str]:
    """Extract candidate effective working directories from wrappers/explicit targets.

    Detects (including inside `bash/sh/zsh -c` wrapper scripts):
      - `git -C <path>`
      - leading `cd <path> &&`
      - `command git -C <path>`
      - `env VAR=... git -C <path>`
    Returns absolute candidate dirs (resolved against cwd when relative). The
    presence of any candidate outside the expected worktree triggers a block.
    """
    candidates: list[str] = []

    def _scan(cmd: str) -> None:
        # leading `cd <path> &&` / `cd <path> ;` (also when it appears after `&&`/`;`)
        for m in re.finditer(r"(?:^|&&|;|\|\|)\s*cd\s+(['\"]?)([^&;|'\"]+)\1\s*(?=&&|;|\|\||$)", cmd):
            candidates.append(_abs_against(cwd, m.group(2).strip()))
        # `git -C <path>` (and `command git -C`, `env ... git -C`)
        for gm in re.finditer(r"\bgit\s+(?:-c\s+\S+\s+)*-C\s+(['\"]?)([^\s'\"]+)\1", cmd):
            candidates.append(_abs_against(cwd, gm.group(2)))
        for gm in re.finditer(r"\bgit\s+-C(['\"]?)([^\s'\"]+)\1", cmd):
            candidates.append(_abs_against(cwd, gm.group(2)))

    _scan(command)
    for inner in _inner_scripts(command):
        _scan(inner)

    return candidates


# =============================================================================
# write-target extraction (B2 / AC8) — file-write mutation destination paths
# =============================================================================


def write_target_paths(command: str, cwd: str) -> list[str]:
    """Extract destination paths of file-write mutations from a command.

    Covers (including inside `bash/sh/zsh -c` wrapper scripts):
      - redirection `> path`, `>> path`
      - `tee [opts] path...`
      - `sed -i ... <file>`, `perl -i ... <file>`
      - interpreter one-liners: open(PATH,'w'|'a'|'x'), writeFileSync(PATH,...),
        appendFileSync(PATH,...), createWriteStream(PATH), File.write(PATH,...),
        File.open(PATH,'w'|'a')
    Returns absolute realpaths resolved against cwd.
    """
    targets: list[str] = []

    def _add(p: str) -> None:
        p = p.strip().strip("'\"")
        if not p:
            return
        # Dynamic destinations (command substitution / globs / var expansion) are
        # not statically resolvable → do NOT add (leaves targets empty so the
        # caller fail-closes on a detected write mutation).
        if re.search(r"[$`*?]|\$\(|\bsubprocess\b", p) or p.startswith("$("):
            return
        targets.append(_abs_against(cwd, p))

    def _scan(cmd: str) -> None:
        # redirection: > path / >> path (skip fd-dup >&)
        for m in re.finditer(r"\d*>>?\s*(?!&)(['\"]?)([^\s'\";|&<>]+)\1", cmd):
            _add(m.group(2))
        # tee [opts] file...
        for tm in re.finditer(r"\btee\b((?:\s+-[^\s]+)*)\s+([^|;&]+)", cmd):
            rest = tm.group(2)
            # stop at next shell operator
            rest = re.split(r"[|;&]", rest)[0]
            for tok in rest.split():
                if tok.startswith("-"):
                    continue
                _add(tok)
        # sed -i / perl -i: the target file(s) — take trailing non-flag tokens.
        for sm in re.finditer(r"\b(?:sed|perl)\b([^|;&]*)", cmd):
            seg = sm.group(1)
            if not re.search(r"\s-[a-zA-Z]*i\b", seg):
                continue
            _extract_inplace_files(seg, _add)
        # interpreter one-liners (open / writeFileSync / appendFileSync /
        # createWriteStream / File.write / File.open)
        for pm in re.finditer(r"open\s*\(\s*(['\"])([^'\"]+)\1\s*,\s*(['\"])[wax]\+?\3", cmd):
            _add(pm.group(2))
        for pm in re.finditer(r"(?:writeFileSync|appendFileSync|createWriteStream)\s*\(\s*(['\"])([^'\"]+)\1", cmd):
            _add(pm.group(2))
        for pm in re.finditer(r"File\.(?:write|open)\s*\(\s*(['\"])([^'\"]+)\1", cmd):
            _add(pm.group(2))
        # python pathlib: Path('x').write_text / write_bytes
        for pm in re.finditer(r"Path\s*\(\s*(['\"])([^'\"]+)\1\s*\)\s*\.write_", cmd):
            _add(pm.group(2))

    _scan(command)
    for inner in _inner_scripts(command):
        _scan(inner)

    return targets


def _extract_inplace_files(seg: str, add) -> None:
    """Extract file arguments of a `sed -i ... files` / `perl -i ... files` segment.

    Heuristic: trailing tokens that are not flags and not the sed/perl script
    expression. We treat any non-flag token that is not the first quoted
    s///-style script as a candidate file path. To stay fail-closed-friendly we
    add every non-flag, non-option-value token except an obvious sed program.
    """
    toks = seg.split()
    # drop a leading combined `-i*` / `-e` etc handled by skipping flags below
    candidates = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("-"):
            # -e <expr> consumes the next token as the program
            if t in ("-e", "-f"):
                i += 2
                continue
            i += 1
            continue
        candidates.append(t)
        i += 1
    # The first non-flag token for sed without -e is the script (e.g. 's/a/b/').
    # Treat tokens that look like a sed/perl program (contain / and start with a
    # command letter, or are quoted s///) as the script, the rest as files.
    for c in candidates:
        if re.match(r"^s/.+/.+/[gpix]*$", c) or re.match(r"^['\"]?s/", c):
            continue
        if re.match(r"^[0-9,]+[a-z]", c):  # line-addr sed program
            continue
        add(c)


# =============================================================================
# absolute-path argument extraction (Major / AC15) — for unknown / mutating cmds
# =============================================================================


def absolute_path_args(command: str, cwd: str) -> list[str]:
    """Extract ALL absolute-path-looking argument tokens (and inner-script tokens).

    Returns realpaths of every `/abs` token and `--opt=/abs` value. Relative
    tokens are ignored (cwd-containment already covers relative writes from inside
    the worktree). NOTE: this returns positional reads too; callers that only care
    about write destinations should use `write_option_abs_path_args()`.
    """
    found: list[str] = []

    def _scan(cmd: str) -> None:
        toks = _tokenize(cmd)
        for tok in toks:
            # `--output=/abs/path` style
            if "=" in tok and not tok.startswith("/"):
                _, _, val = tok.partition("=")
                if val.startswith("/"):
                    found.append(os.path.realpath(val))
                continue
            if tok.startswith("/") and len(tok) > 1:
                found.append(os.path.realpath(tok))

    _scan(command)
    for inner in _inner_scripts(command):
        _scan(inner)
    return found


def write_option_abs_path_args(command: str, cwd: str) -> list[str]:
    """Extract external-write-destination absolute paths from write-indicating options.

    Only absolute paths that are the *value of a known write option*
    (`-o`/`--output`/`--out`/`-w`/`--write`/`--in-place`/`--fix`/...) are returned.
    A bare positional absolute path (a read source for an unknown program) is NOT
    returned — that is the over-block this narrows. Covers both
    `--output /abs` (separate value) and `--output=/abs` (joined) forms, plus
    inner `bash -c` wrapper scripts.
    """
    found: list[str] = []

    def _scan(cmd: str) -> None:
        toks = _tokenize(cmd)
        i = 0
        while i < len(toks):
            tok = toks[i]
            # joined: --output=/abs  or  -o/abs
            if "=" in tok and tok.startswith("-"):
                flag, _, val = tok.partition("=")
                if flag in _WRITE_OPTION_FLAGS and val.startswith("/"):
                    found.append(os.path.realpath(val))
                i += 1
                continue
            # joined short form: -o/abs
            if tok.startswith("-o") and len(tok) > 2 and tok[2] == "/":
                found.append(os.path.realpath(tok[2:]))
                i += 1
                continue
            # separate value: --output /abs
            if tok in _WRITE_OPTION_FLAGS:
                if i + 1 < len(toks):
                    nxt = toks[i + 1]
                    if nxt.startswith("/") and len(nxt) > 1:
                        found.append(os.path.realpath(nxt))
                i += 2
                continue
            i += 1

    _scan(command)
    for inner in _inner_scripts(command):
        _scan(inner)
    return found


# =============================================================================
# main decision
# =============================================================================


def decide(payload: dict) -> None:
    """Make the allow/block decision and exit."""
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.environ.get("PWD") or os.getcwd()

    # Malformed payload for a matched mutation tool → fail-closed.
    if not tool_name:
        _block("<unresolved>", cwd)
    if tool_name not in MATCHED_TOOLS:
        # Not a tool we guard (defensive; matcher should already scope this).
        _allow()

    # Blocker 8: ONE shared monotonic deadline created at the entry of the decision
    # and threaded through every guard subprocess (resolver, default-branch, branch
    # validation, catalog, worktree status) so the sum of inner timeouts can never
    # exceed the outer hook timeout. Budget < outer hook timeout (10s).
    deadline = _wcat.Deadline(8.0) if _V3_AVAILABLE and _wcat is not None else None

    project_root = resolve_project_root()
    issue = resolve_current_issue(cwd, project_root)
    resolution = resolve_expected_worktree(issue, project_root, deadline)

    if tool_name in WRITE_TOOLS:
        _decide_write(tool_input, cwd, issue, resolution, tool_name, project_root)
    elif tool_name == BASH_TOOL:
        _decide_bash(tool_input, cwd, issue, resolution, deadline)

    _allow()


def _decide_write(
    tool_input: dict,
    cwd: str,
    issue: str | None,
    resolution: "WorktreeResolution",
    tool_name: str = "Write",
    project_root: str | None = None,
) -> None:
    target = tool_input.get("file_path") or tool_input.get("path") or ""

    if project_root is None:
        project_root = resolve_project_root()

    # No active issue resolvable → write tools are not scoped to a worktree; allow.
    if not issue:
        _allow()

    # An issue is resolved but git is unavailable → fail-closed for mutation.
    if not resolution.git_available:
        _block("<git-unavailable>", cwd)

    # B1: active issue resolved but NO matching worktree → fail-closed block
    # (symmetric with _decide_bash zero_matches_for_mutation:block).
    if resolution.match_count == 0:
        _block("<no-matching-worktree>", cwd)

    # multiple matches (match_count > 1) → fail-closed block.
    if resolution.expected is None:
        _block("<ambiguous>", cwd)

    if is_inside(resolution.expected, target, cwd):
        _allow()

    # Target is outside the expected worktree.
    # LOCAL_MAIN_SCRATCH_ALLOW_V1: allow writes to safe scratch paths from local main.
    if local_main_scratch_allow_v1(target, cwd, project_root, tool_name):
        _allow()

    _block(_rel(resolution.expected, project_root=resolve_project_root()), cwd)


def _decide_rtk_git_merge(
    bounded_rtk_git: "GitMutationPolicyResult",
    cwd: str,
    issue: str | None,
    resolution: "WorktreeResolution",
    project_root: str,
) -> None:
    """Issue #1609 fix_delta (P0 Blocker): authorize -- active Issue resolved,
    exactly one matching worktree, cwd bound to that worktree, and the
    worktree is LINKED (not the root/primary checkout) -- BEFORE calling
    `execute_verified_ff_merge_transaction`. `classify_rtk_git_mutation`'s
    merge lane is now a PURE shape classifier with no side effects, so this
    ordering closes the P0 window where the previous design executed the
    real `git merge --ff-only` as a side effect of classification, before any
    of these authorization checks ever ran. This function always exits (via
    `_block_with_reason`) -- it never returns and never lets the caller's raw
    `rtk git merge` shell command run afterward (mirrors the
    initial_branch_create push lane's classify-executes-then-always-deny
    pattern, Issue #1449)."""
    if bounded_rtk_git.status == "deny":
        _block_with_reason(
            _rel(resolution.expected, project_root=project_root) if resolution.expected else "<unresolved>",
            cwd,
            bounded_rtk_git.reason_code,
            bounded_rtk_git.command_class,
            bounded_rtk_git,
        )
    if not issue:
        _block_with_reason("<issue-context-required>", cwd, "issue_context_required", bounded_rtk_git.command_class)
    if not resolution.git_available:
        _block_with_reason("<git-unavailable>", cwd, "no_matching_worktree", bounded_rtk_git.command_class)
    if resolution.match_count == 0:
        _block_with_reason("<no-matching-worktree>", cwd, "no_matching_worktree", bounded_rtk_git.command_class)
    if resolution.expected is None:
        _block_with_reason("<ambiguous>", cwd, "ambiguous_worktree", bounded_rtk_git.command_class)

    expected_real = os.path.realpath(resolution.expected)
    if os.path.realpath(project_root) == expected_real:
        _block_with_reason(
            _rel(resolution.expected, project_root=project_root),
            cwd,
            "expected_worktree_is_root_checkout",
            bounded_rtk_git.command_class,
        )
    if os.path.realpath(cwd) != expected_real:
        _block_with_reason(
            _rel(resolution.expected, project_root=project_root),
            cwd,
            "cwd_not_expected_worktree",
            bounded_rtk_git.command_class,
        )

    transaction = execute_verified_ff_merge_transaction(
        cwd,
        bounded_rtk_git.target_sha or "",
        expected_worktree_realpath=resolution.expected,
        active_issue_number=issue,
        remote="origin",
        timeout=30,
    )
    result = GitMutationPolicyResult(
        status="deny",
        command_class=bounded_rtk_git.command_class,
        reason_code=transaction.reason_code,
        target_branch=transaction.active_branch,
        local_head=transaction.verified_local_head,
        current_remote_head=transaction.live_remote_head,
    )
    _block_with_reason(
        _rel(resolution.expected, project_root=project_root),
        cwd,
        transaction.reason_code,
        bounded_rtk_git.command_class,
        result,
    )


def _decide_rtk_git_merge_default_branch(
    bounded_rtk_git: "GitMutationPolicyResult",
    cwd: str,
    issue: str | None,
    resolution: "WorktreeResolution",
    project_root: str,
) -> None:
    """Issue #1603: sibling authorization gate to `_decide_rtk_git_merge`
    (Issue #1589 / #1609 fix_delta), for the default-branch fast-forward
    sync lane. Authorizes -- active Issue resolved, exactly one matching
    worktree, cwd bound to that worktree, and the worktree is LINKED (not
    the root/primary checkout) -- BEFORE calling
    `execute_verified_default_branch_ff_merge_transaction`. This function
    always exits (via `_block_with_reason`) -- it never returns and never
    lets the caller's raw `rtk git merge` shell command run afterward."""
    if bounded_rtk_git.status == "deny":
        _block_with_reason(
            _rel(resolution.expected, project_root=project_root) if resolution.expected else "<unresolved>",
            cwd,
            bounded_rtk_git.reason_code,
            bounded_rtk_git.command_class,
            bounded_rtk_git,
        )
    if not issue:
        _block_with_reason("<issue-context-required>", cwd, "issue_context_required", bounded_rtk_git.command_class)
    if not resolution.git_available:
        _block_with_reason("<git-unavailable>", cwd, "no_matching_worktree", bounded_rtk_git.command_class)
    if resolution.match_count == 0:
        _block_with_reason("<no-matching-worktree>", cwd, "no_matching_worktree", bounded_rtk_git.command_class)
    if resolution.expected is None:
        _block_with_reason("<ambiguous>", cwd, "ambiguous_worktree", bounded_rtk_git.command_class)

    expected_real = os.path.realpath(resolution.expected)
    if os.path.realpath(project_root) == expected_real:
        _block_with_reason(
            _rel(resolution.expected, project_root=project_root),
            cwd,
            "expected_worktree_is_root_checkout",
            bounded_rtk_git.command_class,
        )
    if os.path.realpath(cwd) != expected_real:
        _block_with_reason(
            _rel(resolution.expected, project_root=project_root),
            cwd,
            "cwd_not_expected_worktree",
            bounded_rtk_git.command_class,
        )

    transaction = execute_verified_default_branch_ff_merge_transaction(
        cwd,
        bounded_rtk_git.target_branch or "",
        expected_worktree_realpath=resolution.expected,
        active_issue_number=issue,
        remote="origin",
        timeout=30,
    )
    result = GitMutationPolicyResult(
        status="deny",
        command_class=bounded_rtk_git.command_class,
        reason_code=transaction.reason_code,
        target_branch=transaction.active_branch,
        local_head=transaction.verified_local_head,
        current_remote_head=transaction.live_default_branch_oid,
    )
    _block_with_reason(
        _rel(resolution.expected, project_root=project_root),
        cwd,
        transaction.reason_code,
        bounded_rtk_git.command_class,
        result,
    )


def _decide_bash(
    tool_input: dict, cwd: str, issue: str | None, resolution: "WorktreeResolution", deadline: "object | None" = None
) -> None:
    command = tool_input.get("command") or ""
    _pr = resolve_project_root()

    # Issue #1137 Blocker 1: exact agent-ops tool invocation from main root is
    # allowed even with an active issue worktree (checked before classification).
    if _is_agent_ops_tool_command(command, cwd, _pr, deadline):
        _allow()

    if _is_skill_runtime_executor_command(command, cwd, _pr):
        _allow()
    if _is_skill_runtime_anchor_executor_command(command, cwd, _pr):
        _allow()
    if looks_like_skill_runtime_executor_command(command):
        _block(_rel(resolution.expected, project_root=_pr) if resolution.expected else "<skill-runtime-denied>", cwd)

    # Issue #1209: worktree bootstrap executor from main root is allowed even with
    # an active issue worktree. Shared policy function (no split-brain allowlist).
    if _WBE_POLICY_AVAILABLE and _wbe_exec_command(command, cwd, _pr):
        _allow()

    # Issue #1166: controlled skill mutation executor from main root is allowed.
    # Shared policy function (AC4/AC17): same is_controlled_skill_mutation_exec_command
    # as consumed by local_main_branch_guard — no split-brain allowlist.
    # Issue #1284 AC5: this same exact-command-class allow covers the 3 issue
    # metadata mutation command ids (issue_body.update / issue_comment.publish /
    # contract_snapshot.publish) since is_controlled_skill_mutation_exec_command
    # only validates executor script identity + argv shape, not the --command-id
    # value — no separate allowlist entry is required per command id. Raw
    # `gh issue edit` / `gh issue comment` remain classified as `mutating` by
    # _classify_gh below and are still blocked in this state.
    if _CSM_POLICY_AVAILABLE and _is_csm_exec_command(command, _pr):
        _allow()
    if issue and _is_direct_publish_termination_command(command, _pr):
        _block(_rel(resolution.expected, project_root=_pr) if resolution.expected else "<publish-denied>", cwd)

    # Issue #1611 AC14 (CI repair): the controlled git change executor's
    # exact invocation is the sole authorized staging/commit path (see
    # `git_mutation_command_policy.classify_agent_lane_add_commit`, which
    # denies raw `git add`/`git commit`/`rtk git add`/`rtk git commit`
    # unconditionally). Its `--cwd` flag, not the PreToolUse `cwd`, is the
    # operative target -- the caller normally runs it from the root
    # checkout with `--cwd <linked-issue-worktree>` (mirroring the `uv run
    # --directory <worktree> git ...` pattern used elsewhere in this repo)
    # rather than `cd`-ing into the worktree first. Allow only when the
    # `--cwd` value resolves inside the active issue's resolved worktree;
    # any other target (main root, a different issue's worktree, an
    # unresolved/ambiguous worktree) is denied with `worktree_binding_mismatch`.
    parsed_cgce = parse_controlled_git_change_exec_command(command, _pr)
    if parsed_cgce is not None:
        if resolution.expected and is_inside(resolution.expected, parsed_cgce.cwd, cwd):
            _allow()
        _block_worktree_binding_mismatch(cwd)

    bounded_rtk_git = classify_rtk_git_mutation(
        command=command,
        cwd=cwd,
        require_active_branch_push=True,
    )
    if bounded_rtk_git is not None:
        if bounded_rtk_git.command_class == COMMAND_CLASS_RTK_GIT_MERGE_DEFAULT_BRANCH_FF_ONLY:
            # Issue #1603: default-branch sync lane authorization -- distinct
            # command class from the active-branch remote-head lane below,
            # routed through its own trusted transaction. Always exits.
            _decide_rtk_git_merge_default_branch(bounded_rtk_git, cwd, issue, resolution, _pr)
        if bounded_rtk_git.command_class == COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY:
            # Issue #1609 fix_delta (P0 Blocker): authorize BEFORE executing
            # the merge transaction -- see _decide_rtk_git_merge. This call
            # always exits (never returns, never falls through to the
            # add/commit/push handling below).
            _decide_rtk_git_merge(bounded_rtk_git, cwd, issue, resolution, _pr)
        if bounded_rtk_git.status == "deny":
            _block_with_reason(
                _rel(resolution.expected, project_root=_pr) if resolution.expected else "<unresolved>",
                cwd,
                bounded_rtk_git.reason_code,
                bounded_rtk_git.command_class,
                bounded_rtk_git,
            )
        if not issue:
            _block_with_reason("<issue-context-required>", cwd, "issue_context_required", bounded_rtk_git.command_class)
        if not resolution.git_available:
            _block_with_reason("<git-unavailable>", cwd, "no_matching_worktree", bounded_rtk_git.command_class)
        if resolution.match_count == 0:
            _block_with_reason("<no-matching-worktree>", cwd, "no_matching_worktree", bounded_rtk_git.command_class)
        if resolution.expected is None:
            _block_with_reason("<ambiguous>", cwd, "ambiguous_worktree", bounded_rtk_git.command_class)
        if not _dir_inside(resolution.expected, cwd):
            _block_with_reason(
                _rel(resolution.expected, project_root=_pr),
                cwd,
                "target_dir_outside_worktree",
                bounded_rtk_git.command_class,
            )
        _allow()

    klass = classify_bash(command)

    # read-only allowlist: allow even if worktree unresolved / git unavailable.
    if klass == "read_only":
        _allow()

    # Cleanup class: git worktree remove / git branch -d — V3 one-shot arbitration.
    if klass == "cleanup":
        # Blocker 9: the V3 module is the sole authorization source for bare-git
        # cleanup. If it failed to import, cleanup is DENIED — never silently
        # downgraded to legacy V2.
        if not _V3_AVAILABLE:
            _block_cleanup("cleanup_v3_module_unavailable")
            return  # unreachable
        # Recovery deadlock (policy B): never cleanup from a drifted root with an
        # active issue worktree — emit the shared reason via bounded cleanup block.
        if _root_drift_active_worktree_mismatch(_pr, deadline):
            _block_cleanup(_RC_ROOT_DRIFT_ACTIVE_WT_MISMATCH)
            return  # unreachable
        # Blocker 2: claim-first one-shot enforcement (atomic; concurrency-safe).
        _enforce_cleanup(command, cwd, _pr, deadline)
        return  # unreachable

    # Deny force branch deletion even inside the active worktree (AC4c: -D / --force / -f)
    # git branch -D/--force is mutating (not cleanup), but must be denied regardless of cwd.
    if klass == "mutating" and _is_force_branch_delete(command):
        _block_cleanup("branch_force_delete_denied")
        return  # unreachable

    # Issue #1215: enforce exact-path pathspec for git add while an issue worktree is active.
    rel_expected = (
        _rel(resolution.expected, project_root=resolve_project_root()) if resolution.expected else "<unresolved>"
    )

    if issue and klass == "mutating":
        tokenized = _tokenize(command)
        inner_git = _extract_inner_git_argv(tokenized)
        if inner_git and len(inner_git) >= 2 and inner_git[0] == "git":
            if _is_git_add_pathspec_violation(inner_git[1:], cwd=cwd):
                _block(rel_expected, cwd)
                return  # unreachable

    # From here, command is 'mutating' or 'unknown' (possible mutation).
    if issue and not resolution.git_available:
        # git binary unavailable for a possible mutation → fail-closed.
        _block("<git-unavailable>", cwd)

    if not issue or resolution.match_count == 0:
        # No active issue worktree to scope against → allow non-read-only too
        # (no worktree to escape). zero_matches_for_mutation:block applies only
        # when an issue IS resolved but no matching worktree exists.
        if issue and resolution.match_count == 0:
            # Active issue resolved but no matching worktree → fail-closed block.
            _block("<no-matching-worktree>", cwd)
        _allow()

    if resolution.expected is None:
        # multiple matches → ambiguous → fail-closed.
        _block("<ambiguous>", cwd)

    expected = resolution.expected

    # explicit target / wrapper dirs outside expected → block (B3: includes
    # `cd`/`git -C` found inside bash -c wrapper scripts).
    for d in effective_target_dirs(command, cwd):
        if not _dir_inside(expected, d):
            _block(rel_expected, cwd)

    # B2: file-write mutation destination paths outside expected → block.
    if _is_file_write_mutation_recursive(command):
        wtargets = write_target_paths(command, cwd)
        if not wtargets:
            # write mutation detected but destination unextractable → fail-closed.
            _block(rel_expected, cwd)
        for t in wtargets:
            if not _path_inside(expected, t):
                _block(rel_expected, cwd)

    # Major: a non-read-only command whose write destination is an external
    # absolute path must be blocked. Read-only allowlist programs already returned
    # above (so reads like `cat ROOT/f.txt`, `grep x ROOT/f`, `ls ROOT` are NOT
    # affected). Any remaining 'unknown' program is unparsed: a bare external
    # absolute path could be a write destination (cp/mv/dd/install/<formatter>),
    # so we fail-closed on ANY external absolute path arg (positional OR option).
    # For 'mutating' commands the precise write target is already handled by the
    # B2 redirection/tee/-i write-target check and the cd/git -C check above
    # (e.g. `cat ROOT/f > inside` writes inside → allowed); we additionally block
    # external write-option destinations.
    if klass == "unknown":
        for p in absolute_path_args(command, cwd):
            if not _path_inside(expected, p):
                _block(rel_expected, cwd)
    if klass == "mutating":
        for p in write_option_abs_path_args(command, cwd):
            if not _path_inside(expected, p):
                _block(rel_expected, cwd)

    # cwd outside expected → block (mutating or unknown-possible-mutation).
    if not _dir_inside(expected, cwd):
        _block(rel_expected, cwd)

    # cwd inside expected and no outside explicit target / write-target.
    if klass == "mutating":
        _allow()
    if klass == "unknown":
        # Unknown command but cwd inside worktree and no outside target detected.
        # Mutation possibility is contained to the worktree → allow.
        _allow()

    _allow()


def _is_file_write_mutation_recursive(command: str) -> bool:
    """True iff command (or an inner `bash -c` script) is a file-write mutation."""
    if _is_file_write_mutation(command):
        return True
    for inner in _inner_scripts(command):
        if _is_file_write_mutation(inner):
            return True
    return False


def _path_inside(expected_realpath: str, candidate_path: str) -> bool:
    """commonpath containment for an already-absolute realpath candidate."""
    if not candidate_path:
        return False
    actual = os.path.realpath(candidate_path)
    expected = os.path.realpath(expected_realpath)
    try:
        common = os.path.commonpath([expected, actual])
    except ValueError:
        return False
    return common == expected


def _dir_inside(expected_realpath: str, candidate_dir: str) -> bool:
    if not candidate_dir:
        return False
    actual = os.path.realpath(candidate_dir)
    expected = os.path.realpath(expected_realpath)
    try:
        common = os.path.commonpath([expected, actual])
    except ValueError:
        return False
    return common == expected


def _rel(path: str, project_root: str) -> str:
    """Return project-relative path for bounded message; fall back to basename."""
    try:
        return os.path.relpath(path, project_root)
    except ValueError:
        return os.path.basename(os.path.normpath(path))


# =============================================================================
# POST_MERGE_CLEANUP_REQUEST_V2 — cleanup contract (Issue #1050)
# =============================================================================
# Contract supply (AC4e):
#   1. CLAUDE_WORKTREE_CLEANUP_CONTRACT env var (JSON string)
#   2. .claude/artifacts/cleanup_contract.json (file fallback)
#
# Schema:
#   schema: "POST_MERGE_CLEANUP_REQUEST_V2"
#   worktree_path: str  — absolute path to the worktree to remove
#   branch_name:   str  — branch to delete with git branch -d
#   require_clean: bool — if true, worktree must have empty porcelain=v1 status


def _validate_cleanup_contract(contract: dict) -> bool:
    """True iff contract is a valid POST_MERGE_CLEANUP_REQUEST_V2.

    require_clean must be exactly True (not just bool-typed or truthy) — AC4b/AC10.
    worktree_path must be an absolute path string.
    branch_name must be non-empty and free of shell control characters.
    """
    if not isinstance(contract, dict):
        return False
    if contract.get("schema") != "POST_MERGE_CLEANUP_REQUEST_V2":
        return False
    wt_path = contract.get("worktree_path")
    if not isinstance(wt_path, str) or not os.path.isabs(wt_path):
        return False
    branch = contract.get("branch_name")
    if not isinstance(branch, str) or not branch:
        return False
    # branch_name must not contain shell control / whitespace chars
    if any(c in branch for c in " \t\n\r;|&<>$`(){}!"):
        return False
    # require_clean must be exactly True — not just truthy (AC4b/AC10)
    if contract.get("require_clean") is not True:
        return False
    return True


def load_cleanup_contract(project_root: str) -> dict | None:
    """Load POST_MERGE_CLEANUP_REQUEST_V2 from env var or artifact file (AC4e).

    Supply precedence:
      1. CLAUDE_WORKTREE_CLEANUP_CONTRACT env var (JSON string)
      2. <project_root>/.claude/artifacts/cleanup_contract.json
    Returns None on missing, JSON error, or schema mismatch.
    Env var present but invalid → fail-closed (None).
    """
    env_json = os.environ.get("CLAUDE_WORKTREE_CLEANUP_CONTRACT")
    if env_json:
        try:
            contract = json.loads(env_json)
            if _validate_cleanup_contract(contract):
                return contract
        except (json.JSONDecodeError, Exception):
            pass
        return None  # env var present but invalid — fail-closed

    artifact_path = os.path.join(project_root, ".claude", "artifacts", "cleanup_contract.json")
    try:
        with open(artifact_path, encoding="utf-8") as f:
            contract = json.load(f)
        if _validate_cleanup_contract(contract):
            return contract
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


# =============================================================================
# Issue #1137: exact agent-ops tool allow + one-shot V3 cleanup arbitration
# =============================================================================
# V3 cleanup reason codes (literals kept here for grep/VC visibility):
#   cleanup_contract_present_but_invalid / cleanup_contract_expired /
#   cleanup_contract_consumed / cleanup_command_hash_mismatch /
#   cleanup_operation_mismatch / root_drift_active_worktree_mismatch /
#   guard_deadline_exceeded


def _is_agent_ops_tool_command(command: str, cwd: str, project_root: str, deadline: "object | None" = None) -> bool:
    """True iff command is an exact agent-ops tool invocation allowed from main root.

    Issue #1137 Blocker 1: cleanup_exec / guard_preflight / materialize must run
    from cwd=canonical main root (default branch) even when an issue worktree is
    active. Accepted forms: `uv run python3 <script> ...` or `python3 <script> ...`.
    Rejected: shell chains, inline env, `python -c`, wrappers, non-allowed scripts,
    or cwd that is not the main root.
    """
    # Blocker 1: reject ANY shell metacharacter or command separator, including
    # newline / CR / NUL (a bare regex that only listed ';&|<>$`' let a newline-
    # separated second command — e.g. a bare `git worktree remove` — ride along).
    if not command or re.search(r"[;&|<>$`\n\r\0\\(){}*?\[\]!~]", command):
        return False
    toks = _tokenize(command)
    if not toks:
        return False
    if toks[:3] == ["uv", "run", "python3"]:
        rest = toks[3:]
    elif toks and os.path.basename(toks[0]) in ("python3", "python"):
        rest = toks[1:]
    else:
        return False
    if not rest or rest[0].startswith("-"):
        return False  # reject python -c and bare flags
    script = rest[0]
    script_abs = script if os.path.isabs(script) else os.path.join(cwd, script)
    script_real = os.path.realpath(script_abs)
    # Map the resolved realpath back to its repo-relative allowlist key so the
    # per-script argv spec can be looked up (Blocker 1: validate trailing argv).
    rel_key = None
    for s in _AGENT_OPS_ALLOWED_SCRIPTS:
        if script_real == os.path.realpath(os.path.join(project_root, s)):
            rel_key = s
            break
    if rel_key is None:
        return False
    # Blocker 1: the trailing argv must be an exact, non-redundant argv for the
    # script — no extra/unknown/duplicate flags, no --project-root / --no-verify.
    if not _validate_agent_ops_argv(rel_key, rest[1:]):
        return False
    if os.path.realpath(cwd) != os.path.realpath(project_root):
        return False
    # cwd=main root must be on the default branch (Design Decision 2).
    branch = _current_branch(project_root, deadline)
    default = _repo_default_branch(project_root, deadline)
    if not branch or (default and branch != default):
        return False
    return True


def _is_skill_runtime_executor_command(
    command: str, cwd: str, project_root: str, deadline: "object | None" = None
) -> bool:
    """True iff command is the exact privileged skill runtime executor class."""
    parsed = parse_exact_skill_runtime_command(command, project_root)
    if parsed is None:
        return False
    if os.path.realpath(cwd) != os.path.realpath(project_root):
        return False
    default_branch = resolve_default_branch(project_root, None)
    if current_branch(project_root, None) != default_branch:
        return False
    if resolve_repo_slug(project_root, None) != parsed.repo:
        return False
    if command_allows_root_no_worktree(parsed):
        return True
    active_issue, entry = resolve_active_issue(project_root, cwd, deadline)
    if active_issue != parsed.issue_number or entry is None:
        return False
    return True


def _is_skill_runtime_anchor_executor_command(
    command: str, cwd: str, project_root: str, deadline: "object | None" = None
) -> bool:
    """True iff command is the exact `preflight.run.with_anchor` privileged
    skill runtime executor class (Issue #1498)."""
    parsed = parse_exact_skill_runtime_anchor_command(command, project_root)
    if parsed is None:
        return False
    if os.path.realpath(cwd) != os.path.realpath(project_root):
        return False
    default_branch = resolve_default_branch(project_root, None)
    if current_branch(project_root, None) != default_branch:
        return False
    if resolve_repo_slug(project_root, None) != parsed.repo:
        return False
    if command_allows_root_no_worktree(parsed):
        return True
    active_issue, entry = resolve_active_issue(project_root, cwd, deadline)
    if active_issue != parsed.issue_number or entry is None:
        return False
    return True


def _root_drift_active_worktree_mismatch(project_root: str, deadline: "object | None" = None) -> bool:
    """True iff the root checkout has drifted to an issue-like branch (Issue #1137 AC13)."""
    branch = _current_branch(project_root, deadline)
    if branch is None:
        return False
    if _ISSUE_BRANCH_RE.match(branch):
        return True
    default = _repo_default_branch(project_root, deadline)
    if default and branch != default:
        env_issue = os.environ.get("LOOP_ISSUE_NUMBER")
        if env_issue and env_issue.strip().isdigit():
            return True
    return False


def _decide_cleanup_v3(
    command: str, cwd: str, project_root: str, contract: dict, deadline: "object | None" = None
) -> tuple[str, str]:
    """V3 one-shot cleanup decision (expiry already excluded by caller). Returns (decision, reason)."""
    # Blocker 8: reuse the shared monotonic deadline; only create a fresh one if a
    # pure caller (build_decision / tests) did not supply one.
    if deadline is None:
        deadline = _wcat.Deadline(8.0)
    toks = _tokenize(command)
    if len(toks) < 3 or toks[0] != "git":
        return "deny", "not_a_git_cleanup_command"

    if toks[1] == "worktree" and toks[2] == "remove":
        op = _cc3.OP_WORKTREE_REMOVE
        if len(toks) != 4:
            return "deny", "worktree_remove_wrong_argc"
        actual_argv = ["git", "worktree", "remove", toks[3]]
    elif toks[1] == "branch" and toks[2] == "-d":
        op = _cc3.OP_BRANCH_DELETE
        if len(toks) != 4:
            return "deny", "branch_delete_wrong_argc"
        actual_argv = ["git", "branch", "-d", toks[3]]
    else:
        return "deny", "not_a_cleanup_command"

    if contract.get("operation") != op:
        return "deny", _cc3.CLEANUP_OPERATION_MISMATCH

    # per-operation hash recomputed from the ACTUAL argv (never reconstructed).
    recomputed = _cc3.canonical_command_hash(actual_argv, op, os.path.realpath(project_root), contract.get("nonce", ""))
    if recomputed != contract.get("command_hash"):
        return "deny", _cc3.CLEANUP_COMMAND_HASH_MISMATCH

    try:
        if op == _cc3.OP_WORKTREE_REMOVE:
            path_arg = toks[3]
            actual_path = os.path.realpath(path_arg if os.path.isabs(path_arg) else os.path.join(cwd, path_arg))
            expected_path = os.path.realpath(contract["worktree_path"])
            if actual_path != expected_path:
                return "deny", _cc3.WORKTREE_PATH_MISMATCH
            worktrees_dir = os.path.realpath(os.path.join(project_root, ".claude", "worktrees"))
            if not expected_path.startswith(worktrees_dir + os.sep):
                return "deny", "worktree_path_outside_worktrees_dir"
            catalog = _wcat.list_worktrees(project_root, deadline)
            if catalog is None:
                return "deny", "worktree_list_failed"
            entry = _wcat.find_by_realpath(catalog, expected_path)
            if entry is None:
                return "deny", _cc3.WORKTREE_NOT_IN_CATALOG
            if _wcat.branch_short_name(entry.get("branch_ref")) != contract.get("branch_name"):
                return "deny", "worktree_branch_mismatch"
            st = subprocess.run(
                ["git", "-C", expected_path, "status", "--porcelain=v1", "-z"],
                capture_output=True,
                text=True,
                timeout=deadline.subprocess_timeout(10.0),
            )
            if st.returncode != 0 or st.stdout:
                return "deny", _cc3.WORKTREE_DIRTY
            return "allow", "cleanup_worktree_remove_ok"
        else:
            if toks[3] != contract.get("branch_name"):
                return "deny", "branch_name_mismatch"
            return "allow", "cleanup_branch_delete_ok"
    except _wcat.GuardDeadlineExceeded:
        return "deny", _cc3.GUARD_DEADLINE_EXCEEDED
    except (OSError, subprocess.TimeoutExpired):
        return "deny", _cc3.WORKTREE_DIRTY


def cleanup_decision_dispatch(
    command: str, cwd: str, project_root: str, deadline: "object | None" = None
) -> tuple[str, str, str | None]:
    """PURE dispatch of the cleanup decision (no consume). Returns (decision, reason, kind).

    ``kind`` in {v3, v2, None}. A present-but-invalid or expired V3 contract is
    denied and never downgrades to V2 (Blocker 2). Legacy V2 applies only when the
    V3 contract is genuinely ABSENT *and* no consume tombstone forbids it (Blocker 3).
    If the V3 module failed to import, bare-git cleanup is denied — never silently
    V2 (Blocker 9). The runtime path (``_enforce_cleanup``) performs the claim-first
    consume; this function stays side-effect-free for ``build_decision`` / tests.
    """
    # Blocker 9: V3 module unavailable → deny (no V2 fallback).
    if not _V3_AVAILABLE:
        return "deny", "cleanup_v3_module_unavailable", "v3_unavailable"
    state, contract, reason = _cc3.load_contract_state(project_root)
    if state == _cc3.STATE_PRESENT_BUT_INVALID:
        return "deny", _normalize_cleanup_reason(reason), "v3"
    if state == _cc3.STATE_VALID_V3:
        if _cc3.is_expired(contract):
            return "deny", _cc3.CLEANUP_CONTRACT_EXPIRED, "v3"
        decision, dreason = _decide_cleanup_v3(command, cwd, project_root, contract, deadline)
        return decision, dreason, "v3"
    # STATE_ABSENT → legacy V2 fallback, unless a consume tombstone forbids it.
    if _cc3.v2_fallback_forbidden(project_root):
        return "deny", _cc3.CLEANUP_V2_DOWNGRADE_DENIED, "v3"
    contract_v2 = load_cleanup_contract(project_root)
    decision, dreason = _decide_cleanup_bash(command, cwd, contract_v2)
    return decision, dreason, ("v2" if contract_v2 is not None else None)


def _normalize_cleanup_reason(reason: str | None) -> str:
    """Map an internal V3 validation reason to a shared cleanup reason code."""
    if reason and _cc3 is not None and reason in _cc3.SHARED_CLEANUP_REASON_CODES:
        return reason
    if _cc3 is not None:
        return _cc3.CLEANUP_CONTRACT_PRESENT_BUT_INVALID
    return "cleanup_contract_present_but_invalid"


def _enforce_cleanup(command: str, cwd: str, project_root: str, deadline: "object | None" = None) -> None:
    """Runtime claim-first one-shot cleanup enforcement (Blocker 2). Never returns.

    Order (race-safe): (1) deny a present contract on an IO-incapable platform
    (Blocker 9); (2) atomically CLAIM the contract — only the single rename winner
    proceeds, everyone else is denied ``cleanup_contract_consumed`` (Blocker 2);
    (3) validate the CLAIMED copy and burn it (tombstone + discard) regardless of
    outcome so it cannot replay; (4) only an ABSENT contract may fall back to legacy
    V2, and only when no consume tombstone forbids it (Blocker 3).
    """
    # Blocker 9: a present contract the platform can't evaluate safely → deny.
    contract_target = os.path.join(project_root, _cc3.SAFE_SCRATCH_CONTRACT_PATH)
    if os.path.lexists(contract_target) and not _cc3.IO_CAPABLE:
        _block_cleanup(_cc3.CLEANUP_IO_UNSUPPORTED_PLATFORM)
        return  # unreachable

    claimed = _cc3.claim_contract(project_root)
    if claimed is not None:
        ok, contract, vreason = _cc3.read_claimed_contract(project_root, claimed)
        # One-shot: burn the claim (durable tombstone + discard) on EVERY outcome.
        _cc3.write_consume_tombstone(project_root, contract)
        _cc3.discard_claimed(project_root, claimed)
        if not ok:
            _block_cleanup(_normalize_cleanup_reason(vreason))
            return  # unreachable
        decision, dreason = _decide_cleanup_v3(command, cwd, project_root, contract, deadline)
        if decision == "allow":
            _allow()
        _block_cleanup(dreason)
        return  # unreachable

    # Nothing to claim → contract genuinely ABSENT.
    # Blocker 3: a consume tombstone forbids any legacy V2 downgrade.
    if _cc3.v2_fallback_forbidden(project_root):
        _block_cleanup(_cc3.CLEANUP_V2_DOWNGRADE_DENIED)
        return  # unreachable
    contract_v2 = load_cleanup_contract(project_root)
    decision, reason = _decide_cleanup_bash(command, cwd, contract_v2)
    if decision == "allow":
        _allow()
    _block_cleanup(reason)
    return  # unreachable


def _parse_worktree_list_branch(output: str, target_path: str) -> str | None:
    """Parse `git worktree list --porcelain` output; return branch name for target_path.

    Returns branch name (without refs/heads/ prefix) if found, else None.
    """
    current_wt: str | None = None
    current_branch: str | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_wt = os.path.realpath(line[9:].strip())
            current_branch = None
        elif line.startswith("branch "):
            current_branch = line[7:].strip()
            if current_branch.startswith("refs/heads/"):
                current_branch = current_branch[len("refs/heads/") :]
        elif line == "":
            if current_wt == target_path:
                return current_branch
    if current_wt == target_path:
        return current_branch
    return None


def _is_force_branch_delete(command: str) -> bool:
    """True iff command is a git branch force-delete or multi-target variant (AC4c).

    Denied forms: -D, --force, -f, --delete --force, -d with >1 target.
    Only git branch -d <exactly-one-branch> is NOT force-delete.
    """
    toks = _tokenize(command)
    if not toks or toks[0] != "git" or len(toks) < 3:
        return False
    # Skip -C <path> flags to find the subcommand
    i = 1
    while i < len(toks):
        if toks[i] == "-C" and i + 1 < len(toks):
            i += 2
        elif toks[i].startswith("-C") and len(toks[i]) > 2:
            i += 1
        elif toks[i].startswith("-"):
            i += 1
        else:
            break
    if i >= len(toks) or toks[i] != "branch":
        return False
    opts = toks[i + 1 :]
    if not opts:
        return False
    _FORCE_OPTS = {"-D", "--force", "-f"}
    if opts[0] in _FORCE_OPTS:
        return True
    if opts[0] == "--delete" and any(o in opts[1:] for o in _FORCE_OPTS):
        return True
    # -d with more than one target (e.g. git branch -d foo bar)
    if opts[0] == "-d" and len(opts) > 2:
        return True
    return False


def _decide_cleanup_bash(command: str, cwd: str, contract: dict | None) -> tuple[str, str]:
    """Decide allow/deny for a cleanup-class command (AC4).

    Returns (decision, reason).
    Allow only for exact argv forms with a valid matching contract.
    Deny conditions:
      AC4a: no contract
      AC4b: path mismatch/extra args/require_clean not true/dirty worktree
      AC4c: -D/--force/-f/multiple targets
      AC4d: path traversal/sibling/outside .claude/worktrees/
    """
    if contract is None:
        return "deny", "no_cleanup_contract"  # AC4a

    toks = _tokenize(command)
    if len(toks) < 3 or toks[0] != "git":
        return "deny", "not_a_git_cleanup_command"

    if toks[1] == "worktree" and toks[2] == "remove":
        # Exact argv only: git worktree remove <path>  (AC4b: no wildcard/chain/extra)
        if len(toks) != 4:
            return "deny", "worktree_remove_wrong_argc"

        # require_clean must be exactly True in contract — already validated by
        # _validate_cleanup_contract, but double-check here for defence-in-depth (AC4b/AC10)
        if contract.get("require_clean") is not True:
            return "deny", "require_clean_not_true"

        path_arg = toks[3]
        actual_path = (
            os.path.realpath(path_arg) if os.path.isabs(path_arg) else os.path.realpath(os.path.join(cwd, path_arg))
        )
        expected_path = os.path.realpath(contract["worktree_path"])

        # AC4d: exact-path match (covers path traversal / sibling prefix)
        if actual_path != expected_path:
            return "deny", "worktree_path_mismatch"

        # Contract worktree_path must be strictly under <project_root>/.claude/worktrees/
        # This prevents stale contracts from targeting arbitrary filesystem paths.
        _pr = resolve_project_root()
        worktrees_dir = os.path.realpath(os.path.join(_pr, ".claude", "worktrees"))
        if not expected_path.startswith(worktrees_dir + os.sep):
            return "deny", "worktree_path_outside_worktrees_dir"

        # Verify worktree exists in `git worktree list` and branch matches contract
        try:
            wl = subprocess.run(
                ["git", "-C", _pr, "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "deny", "worktree_list_exception"
        if wl.returncode != 0:
            return "deny", "worktree_list_failed"
        found_branch = _parse_worktree_list_branch(wl.stdout, expected_path)
        if found_branch is None:
            return "deny", "worktree_not_in_list"
        if found_branch != contract.get("branch_name", ""):
            return "deny", "worktree_branch_mismatch"

        # Clean check: git -C <path> status --porcelain=v1 -z must be empty (AC4b/AC10)
        try:
            st = subprocess.run(
                ["git", "-C", expected_path, "status", "--porcelain=v1", "-z"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "deny", "status_command_exception"
        if st.returncode != 0:
            return "deny", "status_command_failed"
        if st.stdout:
            return "deny", "worktree_dirty"

        return "allow", "cleanup_worktree_remove_ok"

    if toks[1] == "branch" and toks[2] == "-d":
        # Exact argv only: git branch -d <branch>  (AC4c: -D/--force/--delete denied)
        if len(toks) != 4:
            return "deny", "branch_delete_wrong_argc"
        if toks[3] != contract.get("branch_name", ""):
            return "deny", "branch_name_mismatch"
        return "allow", "cleanup_branch_delete_ok"

    return "deny", "not_a_cleanup_command"


def _block_worktree_binding_mismatch(actual_cwd: str) -> None:
    """Emit a bounded block message for the controlled_git_change_exec.py
    executor's `--cwd` flag not resolving inside the active issue worktree
    (Issue #1611 AC14). Uses the same `reason: <code>` bounded-line format
    `_block_cleanup` below uses, so tooling that greps a stderr line
    starting with ``reason: `` (e.g. `scripts/ci/codex_execpolicy_matrix.py`)
    can extract it uniformly across both denial classes.
    """
    lines = [
        "[worktree_scope_guard] blocked: mutation outside active issue worktree",
        "reason: worktree_binding_mismatch",
        f"actual_cwd: {actual_cwd or '<unknown>'}",
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.exit(2)


def _block_cleanup(reason: str) -> None:
    """Emit bounded block message for cleanup-class denials (AC6).

    Outputs: ≤10 lines, no raw command/path/branch/env values.
    """
    lines = [
        "[worktree_scope_guard] blocked: cleanup operation denied",
        f"reason: {reason}",
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.exit(2)


# =============================================================================
# WORKTREE_SCOPE_DECISION_V2 — public importable API (Issue #1050, AC9)
# =============================================================================


def _v2(command_class: str, cwd_class: str, decision: str, reason: str) -> dict:
    """Construct a WORKTREE_SCOPE_DECISION_V2 dict."""
    return {
        "schema": "WORKTREE_SCOPE_DECISION_V2",
        "command_class": command_class,
        "cwd_class": cwd_class,
        "decision": decision,
        "reason": reason,
    }


def build_decision(payload: dict) -> dict:
    """Public importable function: make guard decision, return WORKTREE_SCOPE_DECISION_V2.

    WORKTREE_SCOPE_DECISION_V2 is NOT written to stdout by the runtime hook (AC9).
    Unit tests call this directly instead of using subprocess (AC9).

    Returns dict: schema, command_class, cwd_class, decision, reason.
    """
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.environ.get("PWD") or os.getcwd()

    project_root = resolve_project_root()
    issue = resolve_current_issue(cwd, project_root)
    resolution = resolve_expected_worktree(issue, project_root)

    cwd_class: str
    if resolution.expected:
        cwd_class = "inside_worktree" if _dir_inside(resolution.expected, cwd) else "outside_worktree"
    else:
        cwd_class = "unknown"

    if not tool_name or tool_name not in MATCHED_TOOLS:
        return _v2("unknown", cwd_class, "allow", "unmatched_tool")

    if tool_name in WRITE_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("path") or ""
        if not issue:
            return _v2("mutation", cwd_class, "deny", "issue_context_required")
        if not resolution.git_available:
            return _v2("mutation", cwd_class, "deny", "git_unavailable")
        if resolution.match_count == 0:
            return _v2("mutation", cwd_class, "deny", "no_matching_worktree")
        if resolution.expected is None:
            return _v2("mutation", cwd_class, "deny", "ambiguous_worktree")
        if is_inside(resolution.expected, target, cwd):
            return _v2("mutation", "inside_worktree", "allow", "write_inside_worktree")
        if local_main_scratch_allow_v1(target, cwd, project_root, tool_name):
            return _v2("mutation", "outside_worktree", "allow", "local_main_scratch")
        return _v2("mutation", cwd_class, "deny", "write_outside_worktree")

    # Bash tool
    command = tool_input.get("command") or ""

    # Issue #1137 Blocker 1: exact agent-ops tool invocation from main root allow.
    if _is_agent_ops_tool_command(command, cwd, project_root):
        return _v2("agent_ops_tool", cwd_class, "allow", "agent_ops_tool_allowed")

    if _is_skill_runtime_executor_command(command, cwd, project_root):
        return _v2("skill_runtime_executor", cwd_class, "allow", "skill_runtime_executor_allowed")
    if _is_skill_runtime_anchor_executor_command(command, cwd, project_root):
        return _v2("skill_runtime_executor", cwd_class, "allow", "skill_runtime_executor_anchor_allowed")
    if looks_like_skill_runtime_executor_command(command):
        return _v2("skill_runtime_executor", cwd_class, "deny", "skill_runtime_executor_denied")

    bounded_rtk_git = classify_rtk_git_mutation(
        command=command,
        cwd=cwd,
        require_active_branch_push=True,
    )
    if bounded_rtk_git is not None:
        if bounded_rtk_git.status == "deny":
            return _v2(bounded_rtk_git.command_class, cwd_class, "deny", bounded_rtk_git.reason_code)
        if not issue:
            return _v2(bounded_rtk_git.command_class, cwd_class, "deny", "issue_context_required")
        if not resolution.git_available:
            return _v2(bounded_rtk_git.command_class, cwd_class, "deny", "git_unavailable")
        if resolution.match_count == 0:
            return _v2(bounded_rtk_git.command_class, cwd_class, "deny", "no_matching_worktree")
        if resolution.expected is None:
            return _v2(bounded_rtk_git.command_class, cwd_class, "deny", "ambiguous_worktree")
        if not _dir_inside(resolution.expected, cwd):
            return _v2(bounded_rtk_git.command_class, "outside_worktree", "deny", "target_dir_outside_worktree")
        return _v2(bounded_rtk_git.command_class, "inside_worktree", "allow", "mutation_inside_worktree")

    klass = classify_bash(command)

    if klass == "read_only":
        return _v2("read_only", cwd_class, "allow", "read_only_command")

    if klass == "cleanup":
        # build_decision is pure (no consume); _decide_bash runtime consumes V3.
        if _V3_AVAILABLE and _root_drift_active_worktree_mismatch(project_root):
            return _v2("cleanup", cwd_class, "deny", _RC_ROOT_DRIFT_ACTIVE_WT_MISMATCH)
        decision, reason, _kind = cleanup_decision_dispatch(command, cwd, project_root)
        return _v2("cleanup", cwd_class, decision, reason)

    # Deny force branch deletion even inside the active worktree (AC4c)
    if klass == "mutating" and _is_force_branch_delete(command):
        return _v2("mutating", cwd_class, "deny", "branch_force_delete_denied")

    if issue and klass == "mutating":
        tokens = _tokenize(command)
        inner_git = _extract_inner_git_argv(tokens)
        if inner_git and len(inner_git) >= 2 and inner_git[0] == "git":
            if _is_git_add_pathspec_violation(inner_git[1:], cwd=cwd):
                return _v2("mutating", cwd_class, "deny", "git_add_requires_explicit_pathspec")

    # mutating or unknown
    if not issue:
        return _v2(klass, cwd_class, "allow", "no_issue_no_scope")
    if not resolution.git_available:
        return _v2(klass, cwd_class, "deny", "git_unavailable")
    if resolution.match_count == 0:
        if issue:
            return _v2(klass, cwd_class, "deny", "no_matching_worktree")
        return _v2(klass, cwd_class, "allow", "no_issue_no_scope")
    if resolution.expected is None:
        return _v2(klass, cwd_class, "deny", "ambiguous_worktree")

    expected = resolution.expected

    for d in effective_target_dirs(command, cwd):
        if not _dir_inside(expected, d):
            return _v2(klass, "outside_worktree", "deny", "target_dir_outside_worktree")

    if _is_file_write_mutation_recursive(command):
        wtargets = write_target_paths(command, cwd)
        if not wtargets:
            return _v2(klass, cwd_class, "deny", "write_mutation_unextractable")
        for t in wtargets:
            if not _path_inside(expected, t):
                return _v2(klass, cwd_class, "deny", "write_target_outside_worktree")

    if klass == "unknown":
        for p in absolute_path_args(command, cwd):
            if not _path_inside(expected, p):
                return _v2(klass, cwd_class, "deny", "unknown_cmd_external_abs_path")
    if klass == "mutating":
        for p in write_option_abs_path_args(command, cwd):
            if not _path_inside(expected, p):
                return _v2(klass, cwd_class, "deny", "mutating_write_option_external")

    if not _dir_inside(expected, cwd):
        return _v2(klass, "outside_worktree", "deny", "cwd_outside_worktree")

    return _v2(klass, "inside_worktree", "allow", "mutation_inside_worktree")


# =============================================================================
# entrypoint
# =============================================================================


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Malformed stdin. We cannot know the tool. Since the hook matcher only
        # fires for Bash|Write|Edit|MultiEdit (matched mutation tools), a malformed
        # payload for a matched tool is fail-closed.
        cwd = os.environ.get("PWD") or os.getcwd()
        _block("<unresolved>", cwd)
        return
    if not isinstance(payload, dict):
        cwd = os.environ.get("PWD") or os.getcwd()
        _block("<unresolved>", cwd)
        return
    decide(payload)


if __name__ == "__main__":
    main()
