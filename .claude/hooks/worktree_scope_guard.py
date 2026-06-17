#!/usr/bin/env python3
"""worktree_scope_guard.py — PreToolUse hook that blocks mutation outside the active issue worktree.

Contract: WORKTREE_SCOPE_RESOLUTION_V1 / MUTATING_BASH_CLASSIFIER_V1 (Issue #960).

When an active issue worktree exists, Write/Edit/MultiEdit targeting paths outside
the expected worktree, and mutating Bash commands whose effective target is outside
the expected worktree, are blocked (fail-closed). Read-only Bash and worktree-internal
mutation are allowed.

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
"""

import json
import os
import re
import shutil
import subprocess
import sys

# ── Tool classes ──────────────────────────────────────────────────────────────
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}
BASH_TOOL = "Bash"

# Matched mutation tools: a matched PreToolUse tool for which malformed payload is
# fail-closed. The hook matcher is "Bash|Write|Edit|MultiEdit".
MATCHED_TOOLS = WRITE_TOOLS | {BASH_TOOL}


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
         (<root>/.claude/hooks/worktree_scope_guard.py) so the `.claude` parent
         is the project root. Anchored on __file__, never on
         `git rev-parse --show-toplevel`.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return os.path.realpath(env_root)
    # __file__ = <root>/.claude/hooks/worktree_scope_guard.py
    here = os.path.realpath(__file__)
    hooks_dir = os.path.dirname(here)
    claude_dir = os.path.dirname(hooks_dir)
    root = os.path.dirname(claude_dir)
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


def _current_branch(path: str) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run(
            [git, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
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
            current = {"worktree": field[len("worktree "):]}
        elif current is not None:
            if field.startswith("branch "):
                current["branch"] = field[len("branch "):]
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


def resolve_expected_worktree(issue: str | None, project_root: str) -> WorktreeResolution:
    """Select the expected worktree for the active issue.

    Match requires BOTH:
      - branch == refs/heads/issue-<issue>-* (worktree- prefix tolerated), AND
      - path basename == issue-<issue>-*
    """
    if not issue:
        return WorktreeResolution(None, 0, shutil.which("git") is not None)

    catalog = list_worktrees(project_root)
    if catalog is None:
        # git unavailable / failed
        return WorktreeResolution(None, 0, False)

    branch_re = re.compile(r"^refs/heads/(?:worktree-)?issue-%s-" % re.escape(issue))
    base_re = re.compile(r"^issue-%s-" % re.escape(issue))

    matches: list[str] = []
    for wt in catalog:
        path = wt.get("worktree")
        if not path:
            continue
        base = os.path.basename(os.path.normpath(path))
        branch = wt.get("branch", "")
        branch_ok = bool(branch) and bool(branch_re.match(branch))
        base_ok = bool(base_re.match(base))
        if branch_ok and base_ok:
            matches.append(os.path.realpath(path))

    if len(matches) == 1:
        return WorktreeResolution(matches[0], 1, True)
    return WorktreeResolution(None, len(matches), True)


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
    "add", "commit", "push", "checkout", "switch", "restore", "reset",
    "rebase", "merge", "cherry-pick", "revert", "am", "apply", "rm", "mv",
    "tag",
}
# git stash mutates unless list/show; git worktree mutates unless list.
_GH_PR_MUTATING = {
    "create", "edit", "merge", "review", "comment", "close", "reopen",
    "ready", "draft", "lock", "unlock",
}
_GH_ISSUE_MUTATING = {
    "create", "edit", "comment", "close", "reopen", "delete", "lock", "unlock",
}
_PKG_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_PKG_MUTATING = {
    "add", "install", "remove", "update", "publish", "version", "link", "unlink",
    "i", "rm", "un", "uninstall",
}

# read-only allowlist (worktree 解決不能でも allow)
_GIT_READONLY = {"status", "diff", "log", "show", "rev-parse"}


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
    if _has_redirection_or_inplace(command):
        return "mutating"
    if _is_file_write_oneliner(command):
        return "mutating"

    # Strip leading wrappers to find the effective program for classification.
    prog_tokens = _strip_wrappers_for_classification(tokens)
    if not prog_tokens:
        return "unknown"

    prog = os.path.basename(prog_tokens[0])
    args = prog_tokens[1:]

    if prog == "git":
        return _classify_git(args)
    if prog == "gh":
        return _classify_gh(args)
    if prog in _PKG_MANAGERS:
        return _classify_pkg(args)

    # Unknown program: cannot prove read-only.
    return "unknown"


def _strip_wrappers_for_classification(tokens: list[str]) -> list[str]:
    """Strip leading `command`, `env VAR=...`, `cd ... &&` and `bash/sh -c` to
    expose the underlying program for classification.

    Note: target-dir extraction is handled separately by effective_target_outside().
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
    rest = args[i + 1:]
    if sub in _GIT_READONLY:
        return "read_only"
    if sub == "worktree":
        nxt = rest[0] if rest else ""
        return "read_only" if nxt == "list" else "mutating"
    if sub == "stash":
        nxt = rest[0] if rest else ""
        return "read_only" if nxt in ("list", "show") else "mutating"
    if sub in _GIT_MUTATING_SUBCMDS:
        return "mutating"
    # Unknown git subcommand — fail-closed.
    return "unknown"


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
        if a.startswith("-f") or a.startswith("-F") or a.startswith("--field=") or a.startswith("--raw-field=") or a.startswith("--input="):
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

def effective_target_dirs(command: str, cwd: str) -> list[str]:
    """Extract candidate effective working directories from wrappers/explicit targets.

    Detects:
      - `git -C <path>`
      - leading `cd <path> &&`
      - `command git -C <path>`
      - `env VAR=... git -C <path>`
    Returns absolute candidate dirs (resolved against cwd when relative). The
    presence of any candidate outside the expected worktree triggers a block.
    """
    candidates: list[str] = []

    def _abs(p: str) -> str:
        if not os.path.isabs(p):
            base = cwd if cwd else os.getcwd()
            p = os.path.join(base, p)
        return os.path.realpath(p)

    # leading `cd <path> &&` / `cd <path> ;`
    m = re.match(r"^\s*cd\s+(['\"]?)([^&;|'\"]+)\1\s*(?:&&|;)", command)
    if m:
        candidates.append(_abs(m.group(2).strip()))

    # `git -C <path>` (and `command git -C`, `env ... git -C`)
    for gm in re.finditer(r"\bgit\s+(?:-c\s+\S+\s+)*-C\s+(['\"]?)([^\s'\"]+)\1", command):
        candidates.append(_abs(gm.group(2)))
    for gm in re.finditer(r"\bgit\s+-C(['\"]?)([^\s'\"]+)\1", command):
        candidates.append(_abs(gm.group(2)))

    return candidates


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

    project_root = resolve_project_root()
    issue = resolve_current_issue(cwd, project_root)
    resolution = resolve_expected_worktree(issue, project_root)

    if tool_name in WRITE_TOOLS:
        _decide_write(tool_input, cwd, issue, resolution)
    elif tool_name == BASH_TOOL:
        _decide_bash(tool_input, cwd, issue, resolution)

    _allow()


def _decide_write(tool_input: dict, cwd: str, issue: str | None,
                  resolution: "WorktreeResolution") -> None:
    target = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or ""
    )

    # No active issue worktree resolvable → write tools are not scoped to a worktree;
    # allow (no active worktree to protect). But if an issue is resolvable yet
    # git is unavailable, that's fail-closed for mutation.
    if issue and not resolution.git_available:
        _block("<git-unavailable>", cwd)

    if not issue or resolution.match_count == 0:
        # No active issue worktree → nothing to scope against. Allow.
        # (Mutation-without-worktree is out of scope per contract: read-only allow,
        #  and write without an active worktree is not a worktree-escape.)
        _allow()

    # 0 件は上で allow 済み。複数件 (match_count > 1) は fail-closed block。
    if resolution.expected is None:
        _block("<ambiguous>", cwd)

    if is_inside(resolution.expected, target, cwd):
        _allow()
    _block(_rel(resolution.expected, project_root=resolve_project_root()), cwd)


def _decide_bash(tool_input: dict, cwd: str, issue: str | None,
                 resolution: "WorktreeResolution") -> None:
    command = tool_input.get("command") or ""
    klass = classify_bash(command)

    # read-only allowlist: allow even if worktree unresolved / git unavailable.
    if klass == "read_only":
        _allow()

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
    rel_expected = _rel(expected, project_root=resolve_project_root())

    # explicit target / wrapper dirs outside expected → block.
    for d in effective_target_dirs(command, cwd):
        if not _dir_inside(expected, d):
            _block(rel_expected, cwd)

    # cwd outside expected → block (mutating or unknown-possible-mutation).
    if not _dir_inside(expected, cwd):
        _block(rel_expected, cwd)

    # cwd inside expected and no outside explicit target.
    if klass == "mutating":
        _allow()
    if klass == "unknown":
        # Unknown command but cwd inside worktree and no outside target detected.
        # Mutation possibility is contained to the worktree → allow.
        _allow()

    _allow()


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
