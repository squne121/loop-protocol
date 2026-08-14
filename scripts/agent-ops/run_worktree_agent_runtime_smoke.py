#!/usr/bin/env python3
"""run_worktree_agent_runtime_smoke.py — cross-runtime worktree agent smoke runner (Issue #1887).

Launches Claude Code or Codex CLI inside an identity-verified, linked worktree
and observes either:

- ``structured`` lane: a non-interactive process (``claude -p`` /
  ``codex exec``) whose exit code and native structured stdout are the
  evidence, always run as a direct subprocess (never via herdr), or
- ``interactive`` lane: a herdr agent lifecycle inside a freshly created,
  isolated named herdr session (never the caller's own attached session)
  whose bounded, allowlist-only summary is the evidence.

This runner does not own semantic verdicts (hook-reason classification,
mutation-deny correctness, Skill preload domain judgement, context-budget
scoring, review verdicts, merge readiness). It only reports whether the
runtime started, ran in the requested worktree, reached a settled/terminal
state within the timeout, produced the requested evidence, and left the
worktree in the expected postcondition.

Isolation (PR #1921 human OWNER fix-delta iteration 5):

- ``mode=interactive`` never touches the human operator's own attached
  Herdr session. It always creates a brand-new, high-entropy named session
  (``herdr session list --json`` collision check, then lazily created via
  ``HERDR_SESSION=<name>``), runs the agent lifecycle inside it, and tears
  the whole session down (``herdr session stop`` -> ``herdr session
  delete`` -> ``herdr session list --json`` removal confirmation) in every
  controlled exit path (success, failure, timeout, SIGINT, SIGTERM).
  Cleanup that cannot be confirmed removed overrides an otherwise-successful
  run to FAIL (fail-closed).
- Inherited ``HERDR_SESSION`` / ``HERDR_SOCKET_PATH`` / ``HERDR_PANE_ID`` /
  ``HERDR_TAB_ID`` / ``HERDR_WORKSPACE_ID`` are stripped before targeting the
  isolated session, so a caller's own runtime namespace never leaks in.

Exit codes:
  0  success
  1  runtime failure / timeout / identity mismatch / unexpected postcondition
     / cleanup not confirmed removed
  77 SKIP (unavailable runtime/auth/capability/herdr — never promoted to PASS)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import yaml

SCHEMA = "WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1"

# Default requested_agent_type when the caller does not declare one (Issue
# #1733 Scope Delta, 2026-08-02 owner-approved harness extension). Existing
# callers of this script predate the ``--agent-type`` flag (the harness's own
# test suite invokes it without this flag in >100 places), so the flag is
# deliberately optional with a clearly-labeled placeholder default rather than
# a hard-required argument that would break them. Issue #1733 AC12's own
# invocation always passes a real ``--agent-type`` value.
_UNSPECIFIED_AGENT_TYPE = "unspecified"

# A mutation route is deliberately checked by this runner before it starts a
# runtime.  This is a deterministic control-plane receipt, not an assertion
# about what a model might choose to say in its final response.  The optional
# flag keeps the generic smoke runner backward compatible for callers which do
# not exercise a role-specific mutation route.
_RUNTIME_FOLLOWUP_ROUTE_RE = re.compile(
    r"(?m)^\s*-\s*runtime_followup_route:\s*([^\s]+)\s*$"
)

_TRANSACTION_ENTRYPOINTS = {
    "create-issue": ".claude/skills/create-issue/scripts/create_issue_txn.py",
    "edit-issue": ".claude/skills/edit-issue/scripts/edit_issue_txn.py",
}
_REQUIRED_RUNTIME_OBSERVATION_FIELDS = frozenset(
    {"effective_permission_profile", "loaded_skill", "executor", "mutation"}
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

_MAX_PANE_LINES = 400
_MAX_LINE_CHARS = 2000
_MAX_SESSION_LOG_LINES = 200
_DEFAULT_MAX_TURNS = 30
_CODEX_CHILD_META_SCAN_SECONDS = 1.0

# Absolute path / long-base64-token redaction (mirrors git_worktree_probe.py).
_SECRET_LIKE_RE = re.compile(
    r"(/(?:home|root|Users)/[^\s\"']+)|"
    r"([A-Za-z0-9+/]{40,}=*)"
)

# CLI color/formatting escape sequences (observed in real ``herdr`` stderr
# output) are cosmetic noise, not secrets, but they degrade the readability
# of persisted evidence and are stripped for cleanliness.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Deliberately narrow: only presence-signalling keys, never a value that
# could carry prose (reasoning, prompt text, tool output). ``cwd`` and
# ``session_id`` were dropped (PR #1921 P1 fix-delta) — they are not needed
# for a presence signal and add unnecessary exposure surface.
_ALLOWLIST_SESSION_LOG_KEYS = {
    "type",
    "event",
    "role",
    "subagent",
    "label",
    "timestamp",
    "ts",
}

_ISOLATION_ENV_KEYS_TO_STRIP = (
    "HERDR_SESSION",
    "HERDR_SOCKET_PATH",
    "HERDR_PANE_ID",
    "HERDR_TAB_ID",
    "HERDR_WORKSPACE_ID",
)


def _redact(text: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", text)
    return _SECRET_LIKE_RE.sub("<redacted>", text)


def _bounded_redacted_lines(raw: str, max_lines: int) -> list[str]:
    lines = raw.splitlines()[:max_lines]
    out = []
    for line in lines:
        line = line[:_MAX_LINE_CHARS]
        out.append(_redact(line))
    return out


class _TerminateRequested(BaseException):
    """Raised from a SIGTERM handler so ``finally`` cleanup still runs."""


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        raise _TerminateRequested(f"received signal {signum}")

    signal.signal(signal.SIGTERM, _handler)


def _run(argv: list[str], *, cwd: str | None = None, timeout: float,
          input_text: str | None = None, env: dict[str, str] | None = None) -> tuple[int | None, str, str, bool]:
    """Run argv with shell=False. Returns (returncode, stdout, stderr, timed_out)."""
    proc: subprocess.Popen[str] | None = None
    try:
        # A runtime may leave descendants holding the captured pipe FDs after
        # its direct CLI process times out.  ``subprocess.run`` then waits for
        # EOF during cleanup and can overrun the caller's verifier budget.
        # A dedicated process group makes this runner's timeout authoritative:
        # kill every descendant first, then drain the now-closed pipes.
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            # Do not call ``communicate()`` after a timeout. A descendant
            # that escaped the process group can retain the pipe FDs, making
            # communicate wait indefinitely for EOF even though the direct
            # runtime process was killed. The partial bytes already supplied
            # by TimeoutExpired are sufficient for a SKIP receipt.
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
        else:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return None, stdout, stderr, True
    except OSError as exc:
        return None, "", str(exc), False


# ---------------------------------------------------------------------------
# Worktree / repository identity
# ---------------------------------------------------------------------------


class IdentityError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _git_common_dir(path: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, _timed_out = _run([git, "-C", path, "rev-parse", "--git-common-dir"], timeout=10.0)
    if rc != 0:
        return None
    return os.path.realpath(os.path.join(path, out.strip()))


def _git_toplevel(path: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, _timed_out = _run([git, "-C", path, "rev-parse", "--show-toplevel"], timeout=10.0)
    if rc != 0:
        return None
    return os.path.realpath(out.strip())


def _default_repo_root() -> str:
    """Resolve the canonical repository root without assuming this file's own
    checkout location is the canonical repository root.

    This script is checked out both in the canonical repository and inside
    linked worktrees under ``.claude/worktrees/<slug>/`` (Issue #1887 AC3-AC7
    invoke it from within such a worktree). A naive ``__file__``-relative
    resolution therefore resolves ``repo_root`` to the worktree itself when
    invoked from inside a worktree, causing ``verify_worktree_identity`` to
    reject a correctly supplied ``--worktree`` as a "root checkout" (fix-delta
    iteration 1).

    Linked worktrees share the same ``git rev-parse --git-common-dir`` target
    as the canonical checkout (the shared ``.git`` directory lives at the
    canonical repository root, never inside a worktree). Use that to derive
    the canonical root regardless of which checkout this file happens to live
    in.
    """
    script_dir = str(Path(__file__).resolve().parent)
    common_dir = _git_common_dir(script_dir)
    if common_dir is not None:
        candidate = os.path.dirname(common_dir.rstrip(os.sep))
        if candidate:
            return candidate
    # Fallback: legacy __file__-relative resolution (e.g. git unavailable).
    return str(Path(__file__).resolve().parent.parent.parent)


def verify_worktree_identity(worktree_arg: str, repo_root: str) -> str:
    """Return the resolved, verified worktree realpath, or raise IdentityError."""
    if not worktree_arg:
        raise IdentityError("worktree path is required")
    worktree_real = os.path.realpath(worktree_arg)
    if not os.path.isdir(worktree_real):
        raise IdentityError(f"worktree path does not exist: {_redact(worktree_real)}")

    repo_root_real = os.path.realpath(repo_root)
    repo_common_dir = _git_common_dir(repo_root_real)
    if repo_common_dir is None:
        raise IdentityError("could not resolve canonical repository git-common-dir")

    toplevel = _git_toplevel(worktree_real)
    if toplevel is None:
        raise IdentityError("worktree is not inside a git checkout")
    if toplevel != worktree_real:
        raise IdentityError(
            "cwd mismatch: --worktree does not match its own git toplevel"
        )

    if worktree_real == repo_root_real:
        raise IdentityError(
            "root checkout rejected: --worktree must be a linked worktree, "
            "not the canonical repository root"
        )

    worktree_common_dir = _git_common_dir(worktree_real)
    if worktree_common_dir != repo_common_dir:
        raise IdentityError(
            "different repository rejected: worktree git-common-dir does not match "
            "canonical repository"
        )

    claude_worktrees_prefix = os.path.realpath(os.path.join(repo_root_real, ".claude", "worktrees"))
    if not (worktree_real == claude_worktrees_prefix or worktree_real.startswith(claude_worktrees_prefix + os.sep)):
        raise IdentityError("worktree must be located under .claude/worktrees/ of the canonical repository")

    return worktree_real


# ---------------------------------------------------------------------------
# Output directory (exclusive create — Issue #1921 P0-4)
# ---------------------------------------------------------------------------


def prepare_output_dir(output_dir: Path) -> str | None:
    """Return an error message if ``output_dir`` cannot be exclusively used."""
    if output_dir.is_symlink():
        return f"output directory must not be a symlink: {_redact(str(output_dir))}"
    if output_dir.exists():
        return f"output directory already exists (exclusive create required): {_redact(str(output_dir))}"
    return None


# ---------------------------------------------------------------------------
# Postcondition (Issue #1921 P0-5 — full repository fingerprint, not a
# porcelain-line-set diff)
# ---------------------------------------------------------------------------


def _git_rev_parse(path: str, rev: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, _timed_out = _run([git, "-C", path, "rev-parse", rev], timeout=10.0)
    if rc != 0:
        return None
    return out.strip()


def _git_symbolic_branch(path: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, _timed_out = _run([git, "-C", path, "symbolic-ref", "--short", "-q", "HEAD"], timeout=10.0)
    if rc != 0:
        return None
    return out.strip() or None


def _git_status_porcelain_all(path: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, _timed_out = _run(
        [git, "-C", path, "status", "--porcelain", "--untracked-files=all"], timeout=15.0
    )
    if rc != 0:
        return None
    return out


def _content_fingerprint(path: str, rel: str, status: str) -> str | None:
    """Content-level fingerprint for a single changed path.

    Untracked paths are hashed directly (raw bytes). Tracked paths are
    fingerprinted via ``git diff HEAD -- <path>`` so that a status code that
    stays the same across before/after (e.g. an already-dirty file receiving
    further edits) is still detected as a change.
    """
    if status.strip() == "??" or status[:1] == "?":
        target = Path(path) / rel
        try:
            data = target.read_bytes()
        except OSError:
            return None
        return hashlib.sha256(data).hexdigest()
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, _timed_out = _run([git, "-C", path, "diff", "HEAD", "--", rel], timeout=15.0)
    if rc is None:
        return None
    return hashlib.sha256(out.encode("utf-8", "replace")).hexdigest()


def _within_output_dir(target: str, output_dir_rel: str | None) -> bool:
    if not output_dir_rel:
        return False
    normalized = output_dir_rel.rstrip("/")
    return target == normalized or target.startswith(normalized + "/")


def _parse_porcelain_entries(porcelain: str, output_dir_rel: str | None) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        rest = line[3:]
        target = rest.split(" -> ")[-1].strip().strip('"')
        if _within_output_dir(target, output_dir_rel):
            continue
        entries[target] = status
    return entries


def repo_fingerprint(path: str, output_dir_rel: str | None) -> dict | None:
    head = _git_rev_parse(path, "HEAD")
    porcelain = _git_status_porcelain_all(path)
    if head is None or porcelain is None:
        return None
    branch = _git_symbolic_branch(path) or f"DETACHED:{head}"
    entries = _parse_porcelain_entries(porcelain, output_dir_rel)
    content = {
        rel: {"status": status, "hash": _content_fingerprint(path, rel, status)}
        for rel, status in entries.items()
    }
    return {"head": head, "branch": branch, "entries": content}


def diff_fingerprints(before: dict | None, after: dict | None) -> list[str]:
    if before is None or after is None:
        return ["could not evaluate postcondition (git probe failed)"]
    diffs: list[str] = []
    if before["head"] != after["head"]:
        diffs.append(f"HEAD moved: {before['head']} -> {after['head']}")
    if before["branch"] != after["branch"]:
        diffs.append(f"branch changed: {before['branch']} -> {after['branch']}")
    before_entries = before["entries"]
    after_entries = after["entries"]
    for key in sorted(set(before_entries) | set(after_entries)):
        if before_entries.get(key) != after_entries.get(key):
            diffs.append(f"path changed: {key} ({before_entries.get(key)} -> {after_entries.get(key)})")
    return diffs


# ---------------------------------------------------------------------------
# Capability preflight
# ---------------------------------------------------------------------------


def preflight_claude_available(
    claude_bin_override: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the ``claude`` executable exactly once and return
    ``(resolved_executable, skip_reason)``.

    Issue #2174 (AC1): when ``claude_bin_override`` is a non-empty absolute
    path (the ``--claude-bin`` CLI flag), it is used directly as the
    resolved executable -- ``shutil.which("claude")`` PATH resolution is
    bypassed entirely. This lets a caller pin a specific launcher (e.g. a
    ``claude-gpt`` bootstrap wrapper) instead of whatever ``claude`` happens
    to resolve to on ``PATH``. When ``claude_bin_override`` is ``None`` (the
    default), behavior is byte-for-byte unchanged from before this flag
    existed (AC6).

    Issue #1960: capability (which flags a given Claude Code version
    accepts) is no longer decided from ``claude --help`` text. The CLI
    reference explicitly documents that ``--help`` output is
    human-oriented and non-exhaustive, and help omission of a flag does
    not mean the flag is unsupported (``--max-turns`` was observed missing
    from ``--help`` in Claude Code 2.1.220 while still being a documented,
    accepted print-mode flag). Capability is now decided from the actual
    fixed-argv invocation result (see
    ``classify_claude_structured_outcome``), which applies to the
    structured lane only. The interactive lane does not depend on this
    check's flag list at all -- it only needs the binary to exist so
    ``herdr agent start --kind claude`` has something to launch.

    Issue #1960 Design Decision 5 (P1-2 fix-delta): the executable is
    resolved to a single absolute path here, once, via ``shutil.which()``.
    Callers must thread this same resolved path through both version
    capture (``capture_runtime_version``) and structured-lane execution
    (``run_structured_claude``) instead of independently re-resolving
    ``"claude"`` by name in each place, which risks a different binary
    being used for version-capture vs. execution if PATH/shims/symlinks
    change mid-run.
    """
    if claude_bin_override:
        resolved_override = os.path.realpath(claude_bin_override)
        if not os.path.isfile(resolved_override) or not os.access(resolved_override, os.X_OK):
            return None, f"--claude-bin path is not an executable file: {claude_bin_override}"
        return resolved_override, None
    exe = shutil.which("claude")
    if exe is None:
        return None, "required command not found: claude"
    return os.path.realpath(exe), None


def preflight_codex_flags() -> tuple[str | None, str | None]:
    """Resolve the ``codex`` executable exactly once and return
    ``(resolved_executable, skip_reason)`` (Issue #1960 Design Decision 5,
    P1-2 fix-delta -- see ``preflight_claude_available`` docstring)."""
    exe = shutil.which("codex")
    if exe is None:
        return None, "required command not found: codex"
    resolved = os.path.realpath(exe)
    rc, out, err, timed_out = _run([resolved, "exec", "--help"], timeout=20.0)
    if timed_out or rc != 0:
        return resolved, "unable to introspect codex exec --help capability"
    text = out + err
    required = ["--json", "--ephemeral", "-C"]
    missing = [flag for flag in required if flag not in text]
    if missing:
        return resolved, f"codex CLI missing required structured-lane flags: {missing}"
    return resolved, None


def preflight_herdr() -> str | None:
    if os.environ.get("HERDR_ENV") != "1":
        return "HERDR_ENV=1 not set"
    exe = shutil.which("herdr")
    if exe is None:
        return "required command not found: herdr"
    rc, _out, _err, timed_out = _run([exe, "status", "server"], timeout=10.0)
    if timed_out or rc != 0:
        return "herdr server is not running (herdr status server failed)"
    return None


# ---------------------------------------------------------------------------
# Prompt handling
# ---------------------------------------------------------------------------


def read_prompt(prompt_file: str) -> str:
    return Path(prompt_file).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structured lane (always a direct subprocess — never herdr)
# ---------------------------------------------------------------------------


# Issue #2015 P1 fix (OWNER review #2044, full-route trial finding #2): a
# genuinely-spawned, genuinely-completed child was observed reporting
# ``failure_class: spawn_not_observed`` (contradicting its own
# ``retrieval_status: succeeded`` / non-empty evidence) on the async-launch
# ``tool_use_result`` envelope shape (``isAsync: true``, no ``agentType``
# field -- see the AC7/#2021 comment block above). ``native_spawn_event_
# observed`` is DESIGNED to fall back to the ``SubagentStart``/
# ``SubagentStop`` hook lifecycle channel (surfaced by
# ``--include-hook-events``) whenever that primary channel lacks
# ``agentType`` -- but this repository's own committed
# ``.claude/settings.json`` (NOT in this Issue's Allowed Paths) registers a
# ``SubagentStop`` hook and NO ``SubagentStart`` hook, and even that
# ``SubagentStop`` hook (``session_manifest_coordinator.sh``) does not echo
# its own stdin payload back to stdout -- so the fallback channel the
# extractors were written to consume could structurally never fire in this
# repository's real configuration, regardless of whether a spawn genuinely
# happened.
#
# Confirmed live (2026-08-09, ad hoc probe outside this repo's tracked
# worktree, both with and without a pre-existing project-level
# ``SubagentStop`` hook of the same event): Claude Code's ``--settings
# <file-or-json>`` flag ADDITIVELY layers extra hooks on top of the
# project's own committed ``.claude/settings.json`` (both a project-level
# hook and this scoped one run for the same event -- neither is replaced),
# and ``cat`` (a POSIX-standard command, no custom script needed) echoes
# the hook's own stdin JSON payload (``agent_id`` / ``agent_type``)
# verbatim to stdout, exactly the shape ``extract_claude_hook_agent_
# identity`` / ``extract_claude_hook_lifecycle_events`` /
# ``classify_claude_child_completion`` already parse. This is a
# process-local, this-invocation-only settings overlay -- it never
# modifies the committed ``.claude/settings.json`` (out of Allowed Paths)
# and never disables any hook already configured there.
_CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON = json.dumps({
    "hooks": {
        "SubagentStart": [{"hooks": [{"type": "command", "command": "cat"}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": "cat"}]}],
    }
})


def run_structured_claude(worktree: str, prompt: str, timeout_seconds: float,
                           max_turns: int, claude_bin: str = "claude",
                           claude_agent_name: str | None = None,
                           hermetic_agents_file: str | None = None,
                           hermetic_settings_file: str | None = None,
                           claude_bin_is_override: bool = False,
                           ) -> tuple[int | None, str, str, bool]:
    argv = [claude_bin]
    if claude_bin_is_override:
        # Issue #2176 (live AC3 finding): ``scripts/claude-gpt/launch.sh``
        # only accepts its own launcher options (``--claude-bin``,
        # ``--check-only``, ``--dry-run``) before a literal ``--``
        # separator; any other ``-*`` token there is rejected as
        # ``unknown_launcher_option`` (confirmed against the launcher
        # committed at Issue #2158 / PR #2162's worktree HEAD). Everything
        # after ``--`` is forwarded to the underlying claude binary
        # unparsed. Native ``claude`` (no override) never receives this
        # separator, so its argv shape is unchanged.
        argv.append("--")
    argv += [
        "-p",
        "--output-format", "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-turns", str(max_turns),
        "--verbose",
    ]
    # Issue #2176: a launcher wrapper pinned via ``--claude-bin`` (e.g.
    # ``scripts/claude-gpt/launch.sh``) rejects any ``--settings`` CLI flag
    # outright as a policy-weakening extra flag
    # (``CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS``), so unconditionally appending
    # the fixed SubagentStart/SubagentStop observability
    # ``--settings <JSON>`` flag here (as done for the native ``claude``
    # binary below) would make every structured-lane launcher invocation a
    # deterministic BLOCKED. Instead, when ``claude_bin`` was supplied via
    # ``--claude-bin`` (``claude_bin_is_override=True``), request the same
    # fixed hook pair through a narrow, value-fixed environment variable
    # (``CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop``) that the
    # launcher itself interprets and materializes into its own
    # launcher-managed settings file -- no caller-supplied JSON ever
    # crosses the launcher's forbidden-flags boundary. When ``claude_bin``
    # is the default (native ``claude`` resolved from ``PATH``,
    # ``claude_bin_is_override=False``), this branch is not taken and argv
    # keeps the pre-existing fixed ``--settings <JSON>`` flag unchanged
    # (backward compatibility, AC6-equivalent for this narrow channel).
    launch_env = None
    if claude_bin_is_override:
        launch_env = os.environ.copy()
        launch_env["CLAUDE_GPT_RUNTIME_SMOKE_HOOKS"] = "subagent-start-stop"
    else:
        argv += ["--settings", _CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON]
    # Issue #1734 fix_delta 3 (AC7): purely additive, opt-in persona binding.
    # When ``claude_agent_name`` is provided, insert ``--agent <name>`` so the
    # underlying ``claude`` process actually launches with that Agent as the
    # active session persona (rather than just declaring a static label via
    # ``--agent-type``, which is never forwarded to the CLI). Omitted by
    # default, so every pre-existing caller's argv is unchanged.
    if claude_agent_name:
        argv += ["--agent", claude_agent_name]
    # Issue #2046 AC2/AC5: purely additive, opt-in hermetic no-mutation lane.
    # ``hermetic_agents_file`` points at a session-local JSON file (built
    # deterministically from the candidate Agent definition's own source
    # sha256, see ``resolve_agent_definition``) supplying the session-local
    # persona named by ``claude_agent_name`` above; ``hermetic_settings_file``
    # points at a session-local settings JSON restricting the tool surface to
    # Read only. Both are omitted for every pre-existing (non-hermetic)
    # caller, so their argv is unchanged.
    if hermetic_agents_file:
        # Claude Code --agents expects an inline JSON object literal (per
        # `claude --help`), not a file path -- unlike --settings, which
        # documents "file-or-json" and accepts either. Passing a bare path
        # here causes the CLI to silently fail to register the custom
        # agent, so --agent <name> then reports "not found" (Issue #2046
        # PR #2047 review finding, confirmed against installed Claude Code
        # 2.1.226 --help output).
        with open(hermetic_agents_file, encoding="utf-8") as f:
            hermetic_agents_json = f.read()
        argv += ["--agents", hermetic_agents_json]
    if hermetic_settings_file:
        argv += ["--settings", hermetic_settings_file]
    return _run(argv, cwd=worktree, timeout=timeout_seconds, input_text=prompt, env=launch_env)


def run_structured_codex(worktree: str, prompt: str, timeout_seconds: float,
                          codex_bin: str = "codex") -> tuple[int | None, str, str, bool]:
    # ``-`` reads the prompt from stdin instead of argv, so the prompt text
    # never appears in the process list (Issue #1921 P1 fix-delta).
    argv = [
        codex_bin,
        "exec",
        "-C", worktree,
        "--json",
        "--ephemeral",
        "-",
    ]
    return _run(argv, cwd=worktree, timeout=timeout_seconds, input_text=prompt)


def parse_native_event_count(stdout: str) -> int:
    count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        count += 1
    return count


# Issue #1960: capability classification is now derived from the actual
# fixed-argv invocation result, not from ``claude --help`` text. Only a
# narrowly-matched, known parser-level "unknown/unrecognized option"
# diagnostic is treated as a capability gap (Design Decision #4 -- "任意の
# non-zero exit を capability 不足として扱わない"). Any other non-zero exit
# (auth failure, network failure, model failure, generic runtime error)
# falls through to the existing FAIL classification unchanged (AC3).
#
# Issue #1960 P1-3 fix-delta (owner REQUEST_CHANGES, PR #1976 review): text
# -based classification is now restricted to ``stderr`` only. ``stdout`` is
# Claude's native ``stream-json`` event stream, which can carry assistant-
# message / tool-output prose containing the literal words "unknown option"
# or "Reached max turns" without those words meaning anything about this
# invocation's own argv handling -- searching ``stdout`` for either pattern
# risked misclassifying a model that merely talks about these phrases (or a
# quoted tool-output artifact) as a capability SKIP or a turn-limit FAIL.

# ``Reached max turns`` (or equivalent phrasing), observed on the runtime's
# own diagnostic channel (stderr), is evidence the flag WAS recognized and
# honored -- it must never be classified as a capability SKIP (AC4). It is a
# bounded-turn runtime failure (FAIL 1).
_CLAUDE_MAX_TURNS_REACHED_RE = re.compile(
    r"reached max turns|max turns reached|max[_ ]turns limit|turn limit reached",
    re.IGNORECASE,
)

# This runner's own fixed-argv flags (see ``run_structured_claude``). A
# parser-error line is only trusted as evidence of *this* runner's flag
# being rejected if it explicitly names one of these -- a diagnostic about
# some unrelated flag must never be misclassified as this runner's flags
# being unsupported.
_CLAUDE_FIXED_ARGV_FLAGS = (
    "--max-turns",
    "--output-format",
    "--include-hook-events",
    "--no-session-persistence",
    # Issue #2046 AC2/AC5: the hermetic no-mutation lane's session-local
    # `--agents` / `--settings` payload flags. Adding them here means an
    # unrecognized-option rejection naming either flag is classified as a
    # capability SKIP (exit 77), never a generic runtime FAIL -- a real
    # Claude Code version that does not support these flags degrades to
    # SKIP, exactly like the pre-existing fixed-argv flags above.
    "--agents",
    "--settings",
)

# Anchored to look like an actual CLI parser error line -- ``error:``
# (case-insensitive) near the start of the line, optionally prefixed by a
# short program/log-level tag, immediately followed on the same line by an
# "unknown/unrecognized option|argument" or "not recognized as a valid
# option|argument" phrase. This is deliberately NOT a loose substring match
# anywhere in arbitrary text (Issue #1960 P1-3 fix-delta).
_CLAUDE_PARSER_ERROR_LINE_RE = re.compile(
    r"^\s*(?:[\w.\-]{0,40}:\s*)?error:.*?(?:unknown|unrecognized)\s+(?:option|argument)|"
    r"^\s*(?:[\w.\-]{0,40}:\s*)?error:.*?not\s+recognized\s+as\s+a\s+valid\s+(?:option|argument)",
    re.IGNORECASE,
)


def _is_json_object_line(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(payload, dict)


def _claude_parser_rejection_reason(stderr: str) -> str | None:
    """Return the matched diagnostic line if ``stderr`` contains a narrow,
    parser-level "unknown/unrecognized option" rejection that explicitly
    names one of this runner's own fixed-argv flags, else ``None`` (Issue
    #1960 P1-3 fix-delta)."""
    for line in stderr.splitlines():
        if not _CLAUDE_PARSER_ERROR_LINE_RE.search(line):
            continue
        for flag in _CLAUDE_FIXED_ARGV_FLAGS:
            if flag in line:
                return line.strip()[:300]
    return None


def classify_claude_structured_outcome(
    rc: int | None, stdout: str, stderr: str, timed_out: bool
) -> tuple[str, str | None]:
    """Classify a completed (or errored) structured Claude invocation.

    Returns ``(decision, reason)``:

    - ``"capability_skip"``: ``stderr`` carries a known, narrowly-matched
      parser-level unknown/unrecognized-option diagnostic naming one of
      this runner's own fixed-argv flags, AND no valid JSON stream-json
      event was observed in ``stdout`` (a genuine parser-level rejection
      happens before the runtime ever emits a stream-json event; observing
      one is evidence the runtime actually started executing, not that
      argv was rejected) (SKIP 77, Design Decision #4 -- narrow
      classification only).
    - ``"turn_limit_reached"``: ``stderr`` reports the ``--max-turns``
      bound was reached (the flag was accepted); this is a runtime
      failure, not a capability gap (FAIL 1, AC4).
    - ``"runtime_outcome"``: none of the above matched; the existing
      exit-code / terminal-event based judgement applies unchanged (AC3).

    ``reason`` is a short, redaction-safe human string recorded as
    ``capability_error_classification`` evidence, or ``None`` for
    ``"runtime_outcome"``.
    """
    if timed_out or rc is None:
        return "runtime_outcome", None
    if _CLAUDE_MAX_TURNS_REACHED_RE.search(stderr):
        return "turn_limit_reached", "max turns limit reached (flag accepted; not a capability gap)"
    if rc != 0:
        observed_valid_json_event = any(
            _is_json_object_line(line) for line in stdout.splitlines()
        )
        if not observed_valid_json_event:
            reason_line = _claude_parser_rejection_reason(stderr)
            if reason_line:
                return (
                    "capability_skip",
                    "claude runtime rejected a fixed-argv flag as unknown/unrecognized "
                    f"option (exit {rc}): {reason_line}",
                )
    return "runtime_outcome", None


def has_terminal_event(runtime: str, stdout: str) -> bool:
    """Whether at least one native event looks like a runtime-reported
    terminal/result event (Issue #1921 P1 fix-delta: a non-empty event
    stream with no terminal event must not be treated as PASS just because
    the process exit code was 0)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if runtime == "claude":
            if payload.get("type") == "result":
                return True
        else:
            event_type = str(payload.get("type") or "")
            if event_type in ("item.completed", "turn.completed", "error") or event_type.endswith(".completed"):
                return True
    return False


# ---------------------------------------------------------------------------
# Structured telemetry fields (Issue #1733 Scope Delta, 2026-08-02
# owner-approved harness extension) — tested_head / runtime_version /
# requested_agent_type / effective_agent_type / loaded_skills / spawn_events /
# child_spawn_event_count / self_restart_event_count /
# orchestration_action_count / prompt_sha256. Derived only from data
# genuinely available during the run (native JSON event stream already
# captured by the structured lane, static agent-definition frontmatter, git,
# and hashlib) -- never fabricated. A value that cannot be honestly derived
# is left as ``None`` (rendered by ``write_evidence`` as the literal string
# ``None``, distinguishable from a real value) rather than guessed.
# ---------------------------------------------------------------------------

# Claude Code's SubAgent-spawning tool is named ``Agent`` (confirmed from this
# same repository's own PreToolUse hook matcher configuration, which targets
# the literal tool name ``Agent`` -- see docs/dev/agent-skill-boundaries.md's
# settings.json excerpt, matcher: "Agent" -- and from
# ``.claude/agents/post-merge-cleanup-worker.md``'s ``disallowedTools:
# [Agent]``). It is not named ``Task``.
_CLAUDE_SPAWN_TOOL_NAME = "Agent"

# Codex CLI's native sub-agent dispatch (`spawn_agent`, `namespace:
# collaboration`) was empirically observed (Issue #1859/#1864 evidence,
# artifacts/codex-permission-profile-smoke/pr-1864/*-exec-events.jsonl,
# codex-cli 0.146.0) to NOT surface the ``spawn_agent`` function_call itself
# as a top-level ``item.completed``/``item.started`` event in ``codex exec
# --json`` stdout -- only the full session *rollout* log (not captured by
# this harness, which only reads stdout) shows
# ``payload.type=="function_call"`` with ``payload.name=="spawn_agent"``. The
# only visible signal in the ``--json`` stdout stream is
# ``item.type=="collab_tool_call"`` (observed with ``tool":"wait"`` in that
# evidence, corresponding to the parent's ``wait_agent`` call after a spawn).
# Because a ``spawn_agent`` call is virtually always followed by a
# corresponding ``wait``/``interrupt``/``send_message`` collaboration item
# that IS visible here, any ``collab_tool_call`` item is treated as spawn
# evidence (best-effort, documented over/under-count risk: a lone
# ``collab_tool_call`` cannot positively distinguish "this session spawned an
# agent" from "some other collaboration-tool activity occurred", but for a
# fresh no-child-policy smoke run any such item at all is itself worth
# surfacing rather than silently discarding).
_CODEX_COLLAB_ITEM_TYPE = "collab_tool_call"

# Bash/shell command patterns that indicate the worker re-invoked its own
# agent runtime (self-restart) -- mirrors
# scripts/check_post_merge_cleanup_boundary.py's
# ``_EXTERNAL_AGENT_CLI_INVOCATION_RE`` / ``_AGENT_CLI_BINARY_IN_CODE_RE``
# static-text detection patterns, applied here to genuine runtime Bash
# tool_use commands instead of Skill-body prose.
_SELF_RESTART_COMMAND_RE = re.compile(
    r"(?:^|[\s/'\"();|&])(?:env\s+|command\s+)*(?:\S*/)?(codex\s+exec|claude\s+-p)\b"
)

# Bash/shell command patterns that indicate main-thread-only orchestration
# routing actions (follow-up Issue creation/closure, parent Issue closure,
# superseded PR closure/comment) -- actions the executor Skill explicitly
# says workers must not perform.
_ORCHESTRATION_ACTION_COMMAND_RE = re.compile(
    r"(?:^|[\s/'\"();|&])gh\s+(?:issue\s+close|issue\s+comment|pr\s+close|pr\s+comment)\b"
)


def capture_runtime_version(bin_path: str) -> str | None:
    """``<bin> --version`` output, captured once at run start. Returns
    ``None`` (never a fabricated string) if the binary does not respond.

    ``input_text=""`` is passed explicitly (rather than left unset) so a
    binary that happens to read stdin before checking its argv (as some test
    fixtures do) is handed an immediate EOF instead of depending on the
    caller process's own ambient stdin state. The first line is also
    sanity-checked against JSON-event-stream leakage (a version string is
    never a JSON object) and redacted/bounded like other captured process
    output, defense-in-depth against a binary that does not behave like a
    well-formed ``--version`` implementation."""
    rc, out, err, timed_out = _run([bin_path, "--version"], timeout=15.0, input_text="")
    if timed_out or rc != 0:
        return None
    text = (out or err).strip()
    if not text:
        return None
    first_line = _redact(text.splitlines()[0].strip())[:_MAX_LINE_CHARS]
    if not first_line or first_line.startswith("{") or '"type"' in first_line:
        return None
    return first_line


def compute_prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def load_static_declared_skills(checkout_root: str, agent_type: str) -> list[str] | None:
    """Real, independently-verifiable ground truth: the ``skills:``
    frontmatter list declared in ``.claude/agents/<agent_type>.md`` for the
    given ``requested_agent_type``. This is a STATIC declaration (what the
    agent is configured to preload), not a runtime-observed fact -- callers
    must not read this as "the CLI actually preloaded X" (no such signal is
    available from the native event stream). Returns ``None`` (not a
    fabricated empty list) when the agent definition file does not exist or
    has no ``skills:`` frontmatter key, e.g. for the ``unspecified``
    placeholder agent type.

    ``checkout_root`` must be the *tested worktree*, not the canonical
    repository root: a worktree may carry an in-flight change to the agent
    definition (e.g. a not-yet-merged ``skills:`` frontmatter addition) that
    the canonical root does not yet have, and the smoke evidence must reflect
    the checkout actually being verified.
    """
    if not agent_type or agent_type == _UNSPECIFIED_AGENT_TYPE:
        return None
    agent_md = Path(checkout_root) / ".claude" / "agents" / f"{agent_type}.md"
    if not agent_md.is_file():
        return None
    text = agent_md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    _, _, remainder = text.partition("---\n")
    frontmatter_text, _, _ = remainder.partition("\n---\n")
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    skills = frontmatter.get("skills")
    if not isinstance(skills, list):
        return None
    return [str(s) for s in skills]


_CLAUDE_DIRECT_WEB_TOOL_NAMES = {"WebSearch", "WebFetch"}
_CODEX_DIRECT_WEB_TOKEN_RE = re.compile(r"\b(web_search|browser|fetch_url|http_get|curl)\b", re.IGNORECASE)


def count_direct_web_tool_events(runtime: str, stdout: str) -> int:
    """Issue #1886 P0-1 fix_delta (PR #2005 adversarial review): AC8
    requires ``direct_fallback_invocation_count`` to reflect an ACTUAL
    native-event-derived observation, never a permanently hard-coded 0.
    Claude Code's native ``stream-json`` events unambiguously name direct
    web tools (``WebSearch`` / ``WebFetch``) as ``tool_use`` blocks, exactly
    like the existing ``Agent``/``Bash`` classification above -- counted
    precisely. Codex CLI's ``--json`` event stream does not expose an
    equally explicit tool-name field for a direct-web equivalent in this
    repository's own observed runtime state (documented gap, same caveat as
    ``_CODEX_COLLAB_ITEM_TYPE`` above): a best-effort, deliberately narrow
    token scan over each event's own text/command fields is used instead,
    which may under-detect but is never fabricated as a fixed 0."""
    count = 0
    if runtime == "claude":
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("type") != "assistant":
                continue
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") in _CLAUDE_DIRECT_WEB_TOOL_NAMES:
                    count += 1
    else:
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            item = payload.get("item") if isinstance(payload.get("item"), dict) else None
            if item is None:
                continue
            candidate_text = " ".join(
                str(item.get(field, ""))
                for field in ("tool", "command", "name")
                if item.get(field) is not None
            )
            if _CODEX_DIRECT_WEB_TOKEN_RE.search(candidate_text):
                count += 1
    return count


def classify_claude_events(stdout: str) -> tuple[list[dict], int, int]:
    """Classify the already-captured native ``stream-json`` event stream for
    Claude Code. Returns ``(spawn_events, self_restart_event_count,
    orchestration_action_count)``. ``spawn_events`` entries are short
    structured labels (tool name + a small allowlisted param, never raw
    prompt/task content) per evidence-hygiene discipline."""
    spawn_events: list[dict] = []
    self_restart_count = 0
    orchestration_count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("type") != "assistant":
            continue
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_name = block.get("name")
            if tool_name == _CLAUDE_SPAWN_TOOL_NAME:
                spawn_events.append({"runtime": "claude", "tool": _CLAUDE_SPAWN_TOOL_NAME})
                continue
            if tool_name != "Bash":
                continue
            tool_input = block.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            command = command if isinstance(command, str) else ""
            if _SELF_RESTART_COMMAND_RE.search(command):
                self_restart_count += 1
            if _ORCHESTRATION_ACTION_COMMAND_RE.search(command):
                orchestration_count += 1
    return spawn_events, self_restart_count, orchestration_count


# ---------------------------------------------------------------------------
# Native spawn-session evidence (Issue #1886 AC7): a genuinely independent,
# runtime-returned child agent identifier, distinct from the caller-declared
# ``requested_agent_type`` self-report the previous ``effective_agent_type``
# assignment relied on. Native sources were empirically located in this
# repository's own local runtime state (not fabricated, not documented API,
# discovered by direct inspection of real invocations):
#
# - Claude Code: the ``Agent``/``Task`` tool_use's ``tool_result`` embeds the
#   runtime-generated child agent id in TWO places -- (1) a structured
#   ``tool_use_result.agentId`` field on the ``type: "user"`` stream-json
#   event that carries the tool_result, and (2) a duplicate human-readable
#   ``agentId: <hex>`` text line inside that same tool_result's text
#   content. Both are directly present in the already-captured ``stdout``
#   stream-json itself -- no persisted transcript file is required (Issue
#   #1886 AC7 fix-delta, iteration 6: the prior implementation only looked
#   in the persisted transcript file at ``~/.claude/projects/*/
#   <parent_session_id>.jsonl``, which is never written for the structured
#   lane because ``run_structured_claude`` always passes
#   ``--no-session-persistence`` -- a self-contradiction that made
#   ``native_spawn_event_observed`` permanently ``False``). The parent
#   session id is the top-level ``session_id`` field already present on
#   every native ``stream-json`` event.
# - Codex CLI: ``spawn_agent`` (namespace ``multi_agent_v1``)
#   ``function_call_output`` payloads embed ``{"agent_id": "<uuid>", ...}``.
#   This is only visible in the on-disk rollout log
#   (``~/.codex/sessions/**/rollout-*.jsonl``), not in ``codex exec --json``
#   stdout (see ``_CODEX_COLLAB_ITEM_TYPE`` note above). The parent session
#   id is the ``thread_id`` from the ``thread.started`` stdout event.
#
# Both extractors are best-effort and fail closed to ``None`` on any error
# (missing file, unexpected shape, permission denied) -- a value that cannot
# be honestly derived is never guessed.
# ---------------------------------------------------------------------------

_CLAUDE_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-fA-F-]+)")


def extract_claude_parent_session_id(stdout: str) -> str | None:
    """Top-level ``session_id`` (or ``sessionId``) from the first native
    stream-json event that carries one."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("session_id", "sessionId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_claude_child_session_id_from_stream(stdout: str) -> str | None:
    """Issue #1886 AC7 fix-delta (iteration 6): the previous file-based
    lookup in ``extract_claude_child_session_id`` globs
    ``~/.claude/projects/*/<parent_session_id>.jsonl`` -- a *persisted*
    session transcript file. That file is never written for the structured
    lane, because ``run_structured_claude`` always passes
    ``--no-session-persistence`` (a deliberate, documented safety
    requirement -- see ``references/claude-code.md`` -- that must not be
    removed just to make this extractor's old lookup path succeed). The
    file-based lookup was therefore structurally unable to ever return a
    value, making ``native_spawn_event_observed`` always ``False``
    regardless of whether a spawn genuinely happened.

    Empirically confirmed (live ``claude -p --output-format stream-json
    --include-hook-events --no-session-persistence`` run, single ``Task``
    tool_use) that the runtime-returned child agent id is ALSO present
    directly in the already-captured stdout stream itself, independent of
    any persisted transcript file:

    - A ``type: "user"`` event carrying the ``Agent``/``Task`` tool_result
      has a top-level ``tool_use_result`` object with an ``agentId`` string
      field -- e.g. ``{"tool_use_result": {"agentId": "a72066e6f732aa768",
      "agentType": "general-purpose", ...}}``. This is the primary,
      structured source used below.
    - The same value is duplicated as human-readable text
      (``agentId: <hex> (use SendMessage with to: '<hex>', ...)``) inside a
      ``text`` content block of that same tool_result -- kept here as a
      fallback for any stream-json shape where ``tool_use_result`` is
      absent but the text block still carries the line, reusing the
      existing ``_CLAUDE_AGENT_ID_RE`` pattern.

    Best-effort / read-only against already-captured data: returns ``None``
    on any parse or shape mismatch, never a guess."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("type") != "user":
            continue
        tool_use_result = payload.get("tool_use_result")
        if isinstance(tool_use_result, dict):
            agent_id = tool_use_result.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                return agent_id
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            text_parts: list[str] = []
            if isinstance(inner, str):
                text_parts.append(inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                        text_parts.append(sub["text"])
            for text in text_parts:
                match = _CLAUDE_AGENT_ID_RE.search(text)
                if match:
                    return match.group(1)
    return None


_CLAUDE_AGENT_TYPE_RE = re.compile(r'"agentType"\s*:\s*"([a-zA-Z0-9_-]+)"')

# Issue #2021 (evidence: Issue #2013 research artifact
# ``artifacts/claude-code-spawn-observability-research/``): the ``Agent`` tool
# returns TWO tool_use_result envelope shapes, non-deterministically.
#
# - synchronous completion: ``{"status": "completed", "agentId": ..,
#   "agentType": .., "content": .., ...}``
# - asynchronous launch:    ``{"isAsync": true, "status": "async_launched",
#   "agentId": .., "description": .., "resolvedModel": .., "outputFile": ..}``
#
# The async-launch shape carries ``agentId`` but NO ``agentType``. Because
# ``native_spawn_event_observed`` requires an observed agent type that matches
# the requested one, a genuinely-spawned, fully-observable child was being
# reported as ``spawn_not_observed`` purely because of the envelope shape --
# 20 of 30 live trials in the #2013 research, with zero timeouts and zero
# ``system/api_retry`` events (i.e. deterministic, never a transient race).
#
# The runtime does supply the missing evidence on a second channel: the
# ``SubagentStart``/``SubagentStop`` hook lifecycle events surfaced by
# ``--include-hook-events``. Across all 30 trials the hook-channel ``agent_id``
# matched ``tool_use_result.agentId`` exactly, and the hook-channel agent type
# always matched the requested agent. Two sub-sources exist in-stream:
#
# - ``hook_name``: the runtime labels a per-agent hook invocation
#   ``"<HookEvent>:<agent_type>"`` (observed on ``SubagentStart``).
# - the official hook stdin payload (``agent_id``/``agent_type``/
#   ``agent_transcript_path``/``stop_reason``), which appears in the event's
#   ``stdout``/``output`` field whenever the configured hook echoes it back.
#
# The tool_use_result channel keeps strict precedence: the hook channel is a
# fallback, never a replacement. Hooks are deliberately NOT made the sole
# ground truth -- upstream https://github.com/anthropics/claude-code/issues/27755
# reports (as a community bug report, "Closed as not planned", not an official
# contract) that these hooks can fail to fire. Absent BOTH channels this still
# fails closed to ``None``; a value that cannot be honestly observed is never
# guessed, and the caller's requested agent type is never substituted.

_CLAUDE_HOOK_LIFECYCLE_EVENTS = ("SubagentStart", "SubagentStop")

# Evidence provenance labels for ``child_agent_type_source`` (Issue #2021 AC6).
AGENT_TYPE_SOURCE_TOOL_RESULT = "tool_use_result"
AGENT_TYPE_SOURCE_HOOK_PAYLOAD = "hook_payload"
AGENT_TYPE_SOURCE_HOOK_NAME = "hook_name"

# ``child_spawn_launch_mode`` values (Issue #2021 AC7).
SPAWN_LAUNCH_MODE_ASYNC = "async_launched"
SPAWN_LAUNCH_MODE_COMPLETED = "completed"
SPAWN_LAUNCH_MODE_UNKNOWN = None


def _iter_claude_stream_events(stdout: str):
    """Yield each stream-json line that parses to a JSON object."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            yield payload


def _parse_embedded_json_object(text: str) -> dict | None:
    """Best-effort parse of a JSON object embedded in hook stdout.

    A hook that echoes its stdin payload may prefix it (Claude Code treats
    non-JSON-leading hook stdout as plain text, so loggers commonly add one).
    Only an exact object parse is accepted -- never a regex-scraped value."""
    start = text.find("{")
    if start < 0:
        return None
    candidate = text[start:].strip()
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_claude_hook_agent_identity(stdout: str) -> dict:
    """Runtime-returned child identity from the hook lifecycle channel.

    Returns ``{"agent_id", "agent_type", "source"}``; every value is ``None``
    when the corresponding evidence is absent (Issue #2021). ``source`` is
    ``hook_payload`` when the official payload was recovered, ``hook_name``
    when only the ``"<HookEvent>:<agent_type>"`` label was available."""
    result: dict = {"agent_id": None, "agent_type": None, "source": None}
    hook_name_agent_type: str | None = None
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "system":
            continue
        hook_event = payload.get("hook_event")
        if hook_event not in _CLAUDE_HOOK_LIFECYCLE_EVENTS:
            continue
        hook_name = payload.get("hook_name")
        if isinstance(hook_name, str) and hook_name.startswith(f"{hook_event}:"):
            suffix = hook_name.split(":", 1)[1].strip()
            if suffix and hook_name_agent_type is None:
                hook_name_agent_type = suffix
        for key in ("stdout", "output"):
            text = payload.get(key)
            if not isinstance(text, str) or not text.strip():
                continue
            parsed = _parse_embedded_json_object(text)
            if parsed is None:
                continue
            agent_id = parsed.get("agent_id")
            agent_type = parsed.get("agent_type")
            if isinstance(agent_id, str) and agent_id and result["agent_id"] is None:
                result["agent_id"] = agent_id
            if isinstance(agent_type, str) and agent_type and result["agent_type"] is None:
                result["agent_type"] = agent_type
                result["source"] = AGENT_TYPE_SOURCE_HOOK_PAYLOAD
    if result["agent_type"] is None and hook_name_agent_type is not None:
        result["agent_type"] = hook_name_agent_type
        result["source"] = AGENT_TYPE_SOURCE_HOOK_NAME
    return result


def classify_claude_spawn_launch_mode(stdout: str) -> str | None:
    """How the ``Agent`` tool reported the child launch (Issue #2021 AC7).

    ``async_launched`` / ``completed`` come from the runtime's own
    ``tool_use_result.status`` (with ``isAsync`` as a corroborating signal);
    ``None`` when no Agent tool_result envelope is present at all."""
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "user":
            continue
        tool_use_result = payload.get("tool_use_result")
        if not isinstance(tool_use_result, dict):
            continue
        status = tool_use_result.get("status")
        if status == SPAWN_LAUNCH_MODE_ASYNC or tool_use_result.get("isAsync") is True:
            return SPAWN_LAUNCH_MODE_ASYNC
        if status == SPAWN_LAUNCH_MODE_COMPLETED:
            return SPAWN_LAUNCH_MODE_COMPLETED
    return SPAWN_LAUNCH_MODE_UNKNOWN


# ---------------------------------------------------------------------------
# Spawn/completion separation (Issue #2015 AC11, OWNER Scope Reframe
# 2026-08-09): the fields above (``native_spawn_event_observed`` /
# ``classify_claude_spawn_launch_mode``) prove only that a child WAS
# launched, never that it reached a terminal state. Both
# ``extract_claude_hook_agent_identity`` and the ``tool_use_result``
# channel conflate ``SubagentStart`` and ``SubagentStop`` (or
# ``async_launched``/``completed``) into a single "identity observed"
# signal -- a lone ``SubagentStart`` with no matching ``SubagentStop`` was
# previously indistinguishable from a genuinely completed child. This is a
# real correctness gap, not merely a naming one: it is the exact scenario
# AC11 requires a hermetic regression test for ("SubagentStart present but
# SubagentStop missing (must not falsely report completion)").
#
# Root cause note (see PR #2044 root-cause report): this harness's
# structured lane (``_run`` -> ``proc.communicate(timeout=...)``) blocks
# until the underlying ``claude -p`` / ``codex exec`` PROCESS itself exits.
# Once that process has exited, there is no live process left to "poll" for
# a future event -- a terminal event that has not appeared anywhere in the
# already-captured stdout by the time the process exits can never appear
# later from this process's own stdout. The correct fix is therefore NOT a
# busy/blocking wait on a dead process; it is (a) separating the two
# distinct signals that already exist in the captured stream so a
# spawn-only observation is never silently promoted to "completed", and (b)
# a bounded filesystem poll for durable artifact materialization performed
# by the caller (``run_agent_provider_route_smoke.py``) AFTER this process
# has exited, tolerating a short flush lag between a child's own terminal
# hook firing and its side-effect (e.g. ``delegation_result.json``)
# becoming visible on disk to a separate reading process.
# ---------------------------------------------------------------------------

CHILD_COMPLETION_SOURCE_HOOK_STOP = "hook_subagent_stop"
CHILD_COMPLETION_SOURCE_TOOL_RESULT = "tool_use_result_status_completed"

CHILD_TERMINAL_STATUS_COMPLETED = "completed"
CHILD_TERMINAL_STATUS_ASYNC_NO_STOP = "async_launched_no_stop_observed"
CHILD_TERMINAL_STATUS_UNKNOWN = None


def extract_claude_hook_lifecycle_events(stdout: str) -> list[dict]:
    """Every ``SubagentStart``/``SubagentStop`` hook event observed in the
    already-captured stdout, IN ORDER, each kept as its own record (never
    merged across event kinds -- the prior ``extract_claude_hook_agent_
    identity`` folded both event kinds into one result, which is exactly
    the conflation this function exists to undo).

    Each entry: ``{"hook_event": "SubagentStart"|"SubagentStop",
    "agent_id": str|None, "agent_type": str|None}``. Best-effort / fail
    closed to ``None`` fields on any parse mismatch -- never a guess."""
    events: list[dict] = []
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "system":
            continue
        hook_event = payload.get("hook_event")
        if hook_event not in _CLAUDE_HOOK_LIFECYCLE_EVENTS:
            continue
        entry: dict = {"hook_event": hook_event, "agent_id": None, "agent_type": None}
        hook_name = payload.get("hook_name")
        if isinstance(hook_name, str) and hook_name.startswith(f"{hook_event}:"):
            suffix = hook_name.split(":", 1)[1].strip()
            if suffix:
                entry["agent_type"] = suffix
        for key in ("stdout", "output"):
            text = payload.get(key)
            if not isinstance(text, str) or not text.strip():
                continue
            parsed = _parse_embedded_json_object(text)
            if parsed is None:
                continue
            agent_id = parsed.get("agent_id")
            agent_type = parsed.get("agent_type")
            if isinstance(agent_id, str) and agent_id:
                entry["agent_id"] = agent_id
            if isinstance(agent_type, str) and agent_type:
                entry["agent_type"] = agent_type
        events.append(entry)
    return events


def classify_claude_child_completion(stdout: str, spawn_agent_id: str | None) -> dict:
    """Whether the child actually reached a terminal state, kept strictly
    separate from spawn evidence (Issue #2015 AC11).

    Two independent completion channels, checked in priority order:

    1. ``tool_use_result.status == "completed"`` on the SAME Agent tool
       result envelope that carried the (matching) ``agentId`` -- the
       synchronous-completion shape.
    2. A ``SubagentStop`` hook lifecycle event whose ``agent_id`` matches
       ``spawn_agent_id`` exactly.

    Returns ``{"observed": bool, "source": str|None, "terminal_status":
    str|None}``. When ``spawn_agent_id`` is ``None`` (spawn itself was
    never observed), completion is never asserted -- a value that cannot be
    honestly bound to the spawned child's own identity is never guessed. A
    ``SubagentStart`` with no matching ``SubagentStop`` (or an
    ``agent_id`` mismatch between the two) fails closed to
    ``observed: False`` -- this is the exact AC11 regression scenario."""
    result = {"observed": False, "source": None, "terminal_status": None}
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "user":
            continue
        tool_use_result = payload.get("tool_use_result")
        if not isinstance(tool_use_result, dict):
            continue
        agent_id = tool_use_result.get("agentId")
        if (
            tool_use_result.get("status") == SPAWN_LAUNCH_MODE_COMPLETED
            and isinstance(agent_id, str)
            and agent_id
            and spawn_agent_id
            and agent_id == spawn_agent_id
        ):
            result["observed"] = True
            result["source"] = CHILD_COMPLETION_SOURCE_TOOL_RESULT
            result["terminal_status"] = CHILD_TERMINAL_STATUS_COMPLETED
            return result
    if not spawn_agent_id:
        return result
    for event in extract_claude_hook_lifecycle_events(stdout):
        if event["hook_event"] != "SubagentStop":
            continue
        if event["agent_id"] == spawn_agent_id:
            result["observed"] = True
            result["source"] = CHILD_COMPLETION_SOURCE_HOOK_STOP
            result["terminal_status"] = CHILD_TERMINAL_STATUS_COMPLETED
            return result
    return result


def classify_claude_child_spawn_agent_id(stdout: str) -> tuple[str | None, str | None]:
    """``(agent_id, source)`` for the spawned child, independent of the
    agent-TYPE identity binding above -- ``native_spawn_event_observed``
    additionally requires a matching agent type, which this function does
    not check, so it can supply a genuine child identity even when type
    identity is unverified (used to bind completion evidence to the
    correct spawn in ``classify_claude_child_completion``)."""
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "user":
            continue
        tool_use_result = payload.get("tool_use_result")
        if isinstance(tool_use_result, dict):
            agent_id = tool_use_result.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                return agent_id, "tool_use_result"
    for event in extract_claude_hook_lifecycle_events(stdout):
        if event["hook_event"] == "SubagentStart" and event["agent_id"]:
            return event["agent_id"], "hook_subagent_start"
    return None, None


def extract_claude_child_agent_type_with_source(stdout: str) -> tuple[str | None, str | None]:
    """``(agent_type, source)`` -- the agent type together with the provenance
    of the channel it was actually observed on. Both are ``None`` when no
    channel supplied evidence (fail-closed)."""
    from_tool_result = _extract_claude_child_agent_type_from_tool_result(stdout)
    if from_tool_result is not None:
        return from_tool_result, AGENT_TYPE_SOURCE_TOOL_RESULT
    hook_identity = extract_claude_hook_agent_identity(stdout)
    if hook_identity["agent_type"] is not None:
        return hook_identity["agent_type"], hook_identity["source"]
    return None, None


def extract_claude_child_agent_type(stdout: str) -> str | None:
    """Runtime-returned child agent type, preferring the ``tool_use_result``
    channel and falling back to the hook lifecycle channel (Issue #2021).
    Returns ``None`` -- never a guess -- when neither channel has evidence."""
    agent_type, _source = extract_claude_child_agent_type_with_source(stdout)
    return agent_type


def _extract_claude_child_agent_type_from_tool_result(stdout: str) -> str | None:
    """Issue #1886 P0-2 fix_delta (PR #2005 adversarial review): the prior
    identity evidence only proved *a* child agent id was returned, never
    that it was the *requested* custom agent -- a generic ``general-purpose``
    child satisfied the same evidence as ``codebase-investigator``. This
    extracts the runtime-returned ``tool_use_result.agentType`` (the same
    stream-json event that carries ``agentId``, see
    ``_extract_claude_child_session_id_from_stream``) so callers can bind
    the spawned child's OBSERVED agent type to the REQUESTED
    ``--agent-type`` instead of trusting a caller self-report. Falls back to
    the human-readable ``"agentType": "<name>"`` text fragment if the
    structured field is absent. Returns ``None`` -- never a guess -- if no
    agentType evidence is present at all (fail-closed: absent evidence must
    never be treated as a match)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("type") != "user":
            continue
        tool_use_result = payload.get("tool_use_result")
        if isinstance(tool_use_result, dict):
            agent_type = tool_use_result.get("agentType")
            if isinstance(agent_type, str) and agent_type:
                return agent_type
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            text_parts: list[str] = []
            if isinstance(inner, str):
                text_parts.append(inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                        text_parts.append(sub["text"])
            for text in text_parts:
                match = _CLAUDE_AGENT_TYPE_RE.search(text)
                if match:
                    return match.group(1)
    return None


def extract_claude_child_session_id(
    parent_session_id: str | None, cwd: str, stdout: str | None = None
) -> str | None:
    """``agentId`` extraction for the Claude Code child sub-agent spawned by
    this run. Primary source (Issue #1886 AC7 fix-delta, iteration 6): the
    already-captured ``stdout`` stream-json itself (see
    ``_extract_claude_child_session_id_from_stream`` for why this is
    required -- the file-based path below can never succeed while
    ``--no-session-persistence`` is active). Fallback source: the Claude
    Code project transcript file for ``parent_session_id``
    (``~/.claude/projects/<cwd-slug>/<session_id>.jsonl``), kept only in
    case a future caller invokes this runner without
    ``--no-session-persistence``. Returns ``None`` on any lookup failure --
    this is read-only, best-effort evidence collection, never a guess.

    Issue #2021: the stdout search is no longer gated on ``parent_session_id``.
    Previously a missing/unparsed parent id returned ``None`` immediately,
    without ever consulting ``stdout`` -- so spawn-time evidence being absent
    silently destroyed completion-time evidence that was sitting right there in
    the already-captured stream (recorded as a known defect in the Issue #2013
    research artifact's ``code-analysis.md``). The parent id is still required
    for the *file-based* fallback below, which globs a transcript path built
    from it; that guard now sits where it is actually needed."""
    if stdout:
        found = _extract_claude_child_session_id_from_stream(stdout)
        if found:
            return found
    if not parent_session_id:
        return None
    try:
        home = Path.home()
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.is_dir():
            return None
        for candidate in projects_dir.glob(f"*/{parent_session_id}.jsonl"):
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _CLAUDE_AGENT_ID_RE.search(text)
            if match:
                return match.group(1)
        return None
    except OSError:
        return None


def extract_codex_parent_session_id(stdout: str) -> str | None:
    """``thread_id`` from the ``thread.started`` native ``--json`` event."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "thread.started":
            value = payload.get("thread_id")
            if isinstance(value, str) and value:
                return value
    return None


def _codex_agent_id_from_spawn_agent_calls(candidate: Path) -> str | None:
    """Parse ``spawn_agent`` ``function_call``/``function_call_output`` pairs
    out of a single Codex rollout log file and return the resulting
    ``agent_id``, if any. Extracted as a helper so both the primary
    (filename-substring) and fallback (content-linked) lookup strategies in
    ``extract_codex_child_session_id`` can reuse the same parsing logic."""
    try:
        pending_call_ids: set[str] = set()
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    pending_call_ids.add(call_id)
                continue
            if payload.get("type") == "function_call_output" and payload.get("call_id") in pending_call_ids:
                output = payload.get("output")
                if isinstance(output, str):
                    try:
                        parsed_output = json.loads(output)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    agent_id = parsed_output.get("agent_id") if isinstance(parsed_output, dict) else None
                    if isinstance(agent_id, str) and agent_id:
                        return agent_id
        return None
    except OSError:
        return None


def _find_codex_child_session_meta(parent_session_id: str) -> dict | None:
    """Locate the on-disk Codex rollout log for a spawned child sub-agent
    thread whose first record (``type: session_meta``) content-links back
    to ``parent_session_id`` via ``payload.parent_thread_id`` (also
    duplicated as ``payload.session_id``), and return that ``session_meta``
    ``payload`` dict.

    Extracted as a shared helper (Issue #1886 P0-2 iteration-N fix_delta,
    live rollout-log investigation) so both
    ``extract_codex_child_session_id`` (child session id) and
    ``extract_codex_child_agent_role`` (identity evidence) read the exact
    same on-disk ``session_meta`` record instead of independently
    re-scanning ``~/.codex/sessions`` and risking disagreement if the
    directory changes between the two reads.

    Returns ``None`` on any lookup failure or when no linked record is
    found -- read-only, best-effort evidence collection, never a guess."""
    try:
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.is_dir():
            return None
        deadline = time.monotonic() + _CODEX_CHILD_META_SCAN_SECONDS
        for candidate in sessions_dir.glob("**/*.jsonl"):
            if time.monotonic() >= deadline:
                return None
            try:
                with candidate.open(encoding="utf-8", errors="replace") as handle:
                    first_line = handle.readline()
            except OSError:
                continue
            if not first_line:
                continue
            try:
                record = json.loads(first_line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            linked_parent_id = payload.get("parent_thread_id") or payload.get("session_id")
            if linked_parent_id != parent_session_id:
                continue
            return payload
        return None
    except OSError:
        return None


def extract_codex_child_session_id(parent_session_id: str | None) -> str | None:
    """``agent_id``/child thread id extraction for the Codex CLI child
    sub-agent spawned by this run.

    Primary source (unchanged): the on-disk Codex rollout log whose
    *filename* contains ``parent_session_id``, parsed for
    ``spawn_agent`` ``function_call``/``function_call_output`` pairs. This
    only ever matches when the parent thread's own rollout log is itself
    persisted to disk under that id.

    Fallback source (Issue #1886 AC7 fix-delta, iteration 7): this runner
    invokes ``codex exec --ephemeral`` (see ``run_structured_codex``), which
    -- analogous to Claude Code's ``--no-session-persistence`` -- suppresses
    persistence of the *parent* thread's own rollout log. No file's
    filename will ever contain ``parent_session_id`` in that case. However,
    a spawned child sub-agent thread's *own* rollout log is still written
    to disk, and its first record (``type: session_meta``) carries a
    ``payload.parent_thread_id`` (also duplicated as ``payload.session_id``)
    equal to the spawning parent's own ``thread_id`` -- this is genuine,
    content-level linkage recorded by the Codex CLI itself, not an
    inference. Once such a file is found (via ``_find_codex_child_session_
    meta``), its own ``payload.id`` (also embedded in the filename) is
    returned as the child thread's session id -- direct evidence of a
    distinct, non-empty child session that differs from
    ``parent_session_id`` (see ``native_spawn_event_observed``).

    Returns ``None`` on any lookup failure -- this is read-only,
    best-effort evidence collection, never a guess."""
    if not parent_session_id:
        return None
    try:
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.is_dir():
            return None
        matches = list(sessions_dir.glob(f"**/*{parent_session_id}*.jsonl"))
        for candidate in matches:
            agent_id = _codex_agent_id_from_spawn_agent_calls(candidate)
            if agent_id:
                return agent_id
    except OSError:
        return None
    meta = _find_codex_child_session_meta(parent_session_id)
    if not meta:
        return None
    own_id = meta.get("id")
    if isinstance(own_id, str) and own_id:
        return own_id
    return None


def extract_codex_child_agent_role(parent_session_id: str | None) -> str | None:
    """Runtime-returned custom-agent identity evidence for the Codex CLI
    child sub-agent spawned by this run (Issue #1886 P0-2 iteration-N
    fix_delta).

    The PR #2005 adversarial review's P0-2 finding was correct that a bare
    ``agent_id`` alone never proves *which* custom agent was spawned -- a
    generic child satisfies the same evidence as a named custom agent. The
    prior fix_delta (commit 8915af25) therefore fail-closed
    ``agent_type_identity_verified`` to always ``False`` for Codex,
    documenting that no stable runtime-returned identity field was found
    in this repository's own local runtime state.

    Direct investigation of real, live-produced Codex rollout logs under
    this runner's own structured lane (multiple ``codebase-investigator``
    and ``web-researcher`` routes, local ``~/.codex/sessions``, Codex CLI
    0.146.0, 2026-08-06/07) shows this field DOES exist: the spawned
    child's own rollout log's first record (``type: session_meta``) is
    written by the Codex CLI itself (not by this runner, not by the
    spawning parent's prompt text) with an ``agent_role`` field --
    duplicated under ``source.subagent.thread_spawn.agent_role`` -- that
    holds exactly the custom agent role/persona name passed to the
    multi-agent ``spawn_agent`` collaboration tool (e.g.
    ``"codebase-investigator"``, ``"web-researcher"``). This is genuine,
    content-level identity evidence independently written to disk by the
    runtime, not a caller self-report re-echoing ``requested_agent_type``.

    Reuses ``_find_codex_child_session_meta`` -- the exact same on-disk
    ``session_meta`` record that ``extract_codex_child_session_id``'s
    content-linked fallback path locates -- so the child session id and
    its identity evidence are always read from the same record.

    Returns ``None`` -- never a guess -- when no linked child
    ``session_meta`` record (or no ``agent_role`` field within it) is
    found; this preserves the fail-closed posture for any Codex CLI
    version/config where this field is genuinely absent."""
    if not parent_session_id:
        return None
    meta = _find_codex_child_session_meta(parent_session_id)
    if not meta:
        return None
    agent_role = meta.get("agent_role")
    if isinstance(agent_role, str) and agent_role:
        return agent_role
    return None


def classify_codex_events(stdout: str) -> tuple[list[dict], int, int]:
    """Classify the already-captured native ``--json`` JSONL event stream for
    Codex CLI. Returns ``(spawn_events, self_restart_event_count,
    orchestration_action_count)``. See ``_CODEX_COLLAB_ITEM_TYPE`` docstring
    above for the empirical basis and documented limitation of the
    ``collab_tool_call`` best-effort signal."""
    spawn_events: list[dict] = []
    self_restart_count = 0
    orchestration_count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else None
        if item is not None and item.get("type") == _CODEX_COLLAB_ITEM_TYPE:
            spawn_events.append(
                {"runtime": "codex", "item_type": _CODEX_COLLAB_ITEM_TYPE, "tool": item.get("tool")}
            )
        # Codex's structured lane invokes the model's own shell/exec tool
        # (not a "Bash" tool_use block) -- best-effort scan any string value
        # under an item.completed/item.started payload for the same
        # self-restart / orchestration command patterns observed for the
        # Claude lane, without persisting the raw payload.
        if item is not None:
            command = item.get("command")
            command = command if isinstance(command, str) else ""
            if _SELF_RESTART_COMMAND_RE.search(command):
                self_restart_count += 1
            if _ORCHESTRATION_ACTION_COMMAND_RE.search(command):
                orchestration_count += 1
    return spawn_events, self_restart_count, orchestration_count


# ---------------------------------------------------------------------------
# Main-session agent identity / definition binding / Skill evidence /
# canonical Read receipt / mutation boundary / settings provenance
# (Issue #2046 -- continues Issue #1978's research gap: #2021/#2025/#2027
# implemented SPAWNED child-agent identity evidence; the MAIN session that
# launched itself had no equivalent evidence channel until now).
# ---------------------------------------------------------------------------

# Every new evidence sub-field's "status" is drawn from exactly this set
# (Issue #2046 AC9): a declared static fact, a directly runtime-observed
# fact, a fact derived from other observed evidence (never itself directly
# observed), or unavailable. Never a fabricated/guessed value.
EVIDENCE_STATUS_DECLARED = "declared"
EVIDENCE_STATUS_OBSERVED = "observed"
EVIDENCE_STATUS_DERIVED = "derived_from_observed"
EVIDENCE_STATUS_UNAVAILABLE = "unavailable"

_CLAUDE_SESSION_START_HOOK_EVENT = "SessionStart"

# The canonical Skill body each in-scope persona is expected to Read (Issue
# #2046 AC4, Outcome). Scoped narrowly to the two personas this Issue's
# Outcome names; any other ``--claude-agent-name`` has no canonical target
# and ``canonical_read`` stays ``unavailable`` (fail-closed, never guessed).
_PERSONA_CANONICAL_SKILL_PATH = {
    "issue-creator": ".claude/skills/create-issue/SKILL.md",
    "issue-editor": ".claude/skills/edit-issue/SKILL.md",
}

# Tool names capable of mutating repository/filesystem state or spawning a
# nested agent (Issue #2046 AC5). Deliberately excludes Read/Glob/Grep/
# WebFetch/WebSearch -- observing ANY of these tool_use blocks during a
# hermetic no-mutation lane run is FAIL, never a warning.
_MUTATION_CAPABLE_CLAUDE_TOOL_NAMES = frozenset(
    {"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash", "Agent"}
)


def extract_claude_session_start_identity(stdout: str) -> dict:
    """Runtime-observed main-session identity from the ``SessionStart`` hook
    lifecycle channel (Issue #2046 AC1). Mirrors
    ``extract_claude_hook_agent_identity``'s two sub-channels (the official
    hook stdin payload, and the ``"<HookEvent>:<agent_type>"`` hook_name
    label) but scoped to ``SessionStart`` -- the MAIN session's own startup
    hook -- rather than ``SubagentStart``/``SubagentStop`` (a spawned
    child's lifecycle). Returns ``{"agent_type", "source"}``; both ``None``
    when no SessionStart evidence is present (fail-closed, never a guess)."""
    # Issue #2046 PR #2047 review finding: unlike SubagentStart (where the
    # ``hook_name`` suffix genuinely encodes the spawned subagent_type),
    # SessionStart's ``hook_name`` suffix is the session *source* -- one of
    # ``startup``/``resume``/``clear``/``compact`` (confirmed against a real
    # ``claude --agent issue-creator ...`` invocation, which emitted
    # ``hook_name: "SessionStart:startup"`` regardless of the requested
    # persona). Treating that suffix as the observed agent_type would be a
    # confidently-wrong ``status: observed`` false positive -- exactly the
    # failure mode AC1 exists to prevent. The only legitimate signal is a
    # SessionStart hook script that echoes ``agent_type`` as embedded JSON on
    # its own stdout/output; no such hook is registered in this repo's
    # ``.claude/settings.json`` today, so ``observed`` stays honestly
    # ``unavailable`` rather than a fabricated match.
    result: dict = {"agent_type": None, "source": None}
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "system":
            continue
        if payload.get("hook_event") != _CLAUDE_SESSION_START_HOOK_EVENT:
            continue
        for key in ("stdout", "output"):
            text = payload.get(key)
            if not isinstance(text, str) or not text.strip():
                continue
            parsed = _parse_embedded_json_object(text)
            if parsed is None:
                continue
            agent_type = parsed.get("agent_type")
            if isinstance(agent_type, str) and agent_type and result["agent_type"] is None:
                result["agent_type"] = agent_type
                result["source"] = AGENT_TYPE_SOURCE_HOOK_PAYLOAD
    return result


def build_main_agent_identity(requested_agent_name: str | None, stdout: str | None) -> dict:
    """Issue #2046 AC1: ``main_agent_identity.requested`` / ``.observed`` /
    ``.matched``, evidence-separated so a model self-report can never fill
    ``observed``. ``requested`` is derived purely from runner argv
    (``--claude-agent-name``, never the CLI's own text output); ``observed``
    is derived purely from the ``SessionStart`` hook channel. A missing hook,
    a missing ``agent_type``, or a mismatch is recorded honestly -- never
    silently promoted to ``matched: true``."""
    requested = {"agent_name": requested_agent_name, "source": "runner_argv"}
    if requested_agent_name is None:
        return {
            "requested": requested,
            "observed": {"agent_type": None, "source": None, "status": EVIDENCE_STATUS_UNAVAILABLE},
            "matched": False,
            "status": EVIDENCE_STATUS_UNAVAILABLE,
        }
    observed_identity = (
        extract_claude_session_start_identity(stdout)
        if stdout is not None
        else {"agent_type": None, "source": None}
    )
    observed_status = (
        EVIDENCE_STATUS_OBSERVED if observed_identity["agent_type"] is not None else EVIDENCE_STATUS_UNAVAILABLE
    )
    matched = (
        observed_status == EVIDENCE_STATUS_OBSERVED
        and observed_identity["agent_type"] == requested_agent_name
    )
    return {
        "requested": requested,
        "observed": {**observed_identity, "status": observed_status},
        "matched": matched,
        "status": observed_status,
    }


def compute_hermetic_agents_payload(
    worktree: str, agent_name: str, source_sha256: str
) -> tuple[dict | None, str | None]:
    """Deterministically build a session-local ``--agents`` payload from the
    candidate Agent definition's static frontmatter (Issue #2046 AC2). The
    generated agent's own name embeds the source file's sha256 prefix, so a
    changed candidate definition never collides with a stale session-local
    name from a previous run against the same persona. Returns ``(payload,
    session_local_agent_name)`` or ``(None, None)`` when the frontmatter
    cannot be parsed."""
    agent_md = Path(worktree) / ".claude" / "agents" / f"{agent_name}.md"
    try:
        text = agent_md.read_text(encoding="utf-8")
    except OSError:
        return None, None
    if not text.startswith("---\n"):
        return None, None
    _, _, remainder = text.partition("---\n")
    frontmatter_text, sep, body = remainder.partition("\n---\n")
    if not sep:
        return None, None
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None, None
    if not isinstance(frontmatter, dict):
        return None, None
    description = frontmatter.get("description")
    session_local_name = f"{agent_name}-hermetic-{source_sha256[:12]}"
    payload = {
        session_local_name: {
            "description": description if isinstance(description, str) else agent_name,
            "prompt": body.strip(),
            # Hermetic no-mutation lane (AC5): tools are deliberately fixed
            # to Read only, regardless of what the candidate definition's
            # own `tools:` frontmatter declares -- this lane exists to
            # bound the mutation surface for evidence collection, not to
            # reproduce production permissions (see AC10/`production_
            # settings_lane`: that remains #1881's scope).
            "tools": ["Read"],
        }
    }
    return payload, session_local_name


def resolve_agent_definition(
    worktree: str, agent_name: str | None, hermetic: bool
) -> tuple[dict, dict | None, str | None]:
    """Issue #2046 AC2. Returns ``(agent_definition_summary,
    hermetic_agents_payload_or_None, hermetic_session_local_agent_name_or_None)``."""
    if not agent_name:
        return (
            {
                "intended_repo_path": None,
                "intended_sha256": None,
                "binding_mode": None,
                "status": EVIDENCE_STATUS_UNAVAILABLE,
            },
            None,
            None,
        )
    repo_rel_path = f".claude/agents/{agent_name}.md"
    agent_md = Path(worktree) / repo_rel_path
    try:
        source_sha256 = hashlib.sha256(agent_md.read_bytes()).hexdigest()
    except OSError:
        source_sha256 = None

    if not hermetic:
        return (
            {
                "intended_repo_path": repo_rel_path if source_sha256 is not None else None,
                "intended_sha256": source_sha256,
                "binding_mode": "project_discovery",
                # Claude Code's project-discovery `--agent <name>` lookup
                # resolves `.claude/agents/<name>.md` internally; this
                # runner has no channel to independently confirm exactly
                # which on-disk version it actually loaded, so the
                # *effective* source stays unavailable even though the
                # *intended* source (this worktree's own file) is recorded
                # above.
                "status": EVIDENCE_STATUS_UNAVAILABLE,
            },
            None,
            None,
        )

    if source_sha256 is None:
        return (
            {
                "intended_repo_path": None,
                "intended_sha256": None,
                "binding_mode": "hermetic",
                "status": EVIDENCE_STATUS_UNAVAILABLE,
            },
            None,
            None,
        )
    payload, session_local_name = compute_hermetic_agents_payload(worktree, agent_name, source_sha256)
    if payload is None:
        return (
            {
                "intended_repo_path": repo_rel_path,
                "intended_sha256": source_sha256,
                "binding_mode": "hermetic",
                "status": EVIDENCE_STATUS_UNAVAILABLE,
            },
            None,
            None,
        )
    payload_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        {
            "intended_repo_path": repo_rel_path,
            "intended_sha256": source_sha256,
            "binding_mode": "hermetic",
            "hermetic_payload_sha256": payload_digest,
            "hermetic_agent_name": session_local_name,
            # This payload is deterministically constructed by this runner
            # itself (not observed from a runtime channel) -- a declared
            # fact, exactly like the static frontmatter declaration below.
            "status": EVIDENCE_STATUS_DECLARED,
        },
        payload,
        session_local_name,
    )


def extract_claude_canonical_read_receipt(
    stdout: str, worktree: str, expected_rel_path: str | None
) -> dict:
    """Issue #2046 AC4: independent, tool_use/tool_result-grounded evidence
    that the persona's canonical Skill body was actually Read via the Read
    tool -- never a marker string, never a self-report. Requires ALL of: a
    normalized repo-relative path that matches ``expected_rel_path``
    exactly, a matching ``tool_use_id`` between the Read ``tool_use`` and
    its ``tool_result``, and a non-error ``tool_result``. A path outside
    the expected target, a failed Read result, or an unmatched
    ``tool_use_id`` all fail closed to ``unavailable`` -- never ``observed``."""
    receipt: dict = {
        "expected_repo_relative_path": expected_rel_path,
        "expected_sha256": None,
        "observed_repo_relative_path": None,
        "tool_name": None,
        "tool_use_id": None,
        "read_result_status": None,
        "status": EVIDENCE_STATUS_UNAVAILABLE,
    }
    if not expected_rel_path:
        return receipt
    expected_path = Path(worktree) / expected_rel_path
    try:
        receipt["expected_sha256"] = hashlib.sha256(expected_path.read_bytes()).hexdigest()
    except OSError:
        return receipt

    pending_read_tool_use_ids: dict[str, str] = {}
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") == "assistant":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Read":
                    continue
                tool_input = block.get("input")
                raw_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                candidate_abs = raw_path if os.path.isabs(raw_path) else os.path.join(worktree, raw_path)
                try:
                    normalized = os.path.relpath(os.path.realpath(candidate_abs), os.path.realpath(worktree))
                except ValueError:
                    continue
                tool_use_id = block.get("id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    pending_read_tool_use_ids[tool_use_id] = normalized
        elif payload.get("type") == "user":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                if not isinstance(tool_use_id, str) or tool_use_id not in pending_read_tool_use_ids:
                    continue
                normalized_path = pending_read_tool_use_ids[tool_use_id]
                if normalized_path != expected_rel_path:
                    continue
                is_error = bool(block.get("is_error"))
                receipt["observed_repo_relative_path"] = normalized_path
                receipt["tool_name"] = "Read"
                receipt["tool_use_id"] = tool_use_id
                receipt["read_result_status"] = "error" if is_error else "success"
                if not is_error:
                    receipt["status"] = EVIDENCE_STATUS_OBSERVED
                    return receipt
    return receipt


def build_skill_evidence(agent_name: str | None, worktree: str, stdout: str | None) -> dict:
    """Issue #2046 AC3: declaration/preload/canonical_read kept as three
    strictly separate sub-objects, each with its own honest ``status`` --
    a declared frontmatter fact must never be presented as an observed
    runtime fact, and vice versa."""
    declared_skills = load_static_declared_skills(worktree, agent_name) if agent_name else None
    declaration = {
        "skills": declared_skills,
        "source": "agent_frontmatter",
        "status": EVIDENCE_STATUS_DECLARED if declared_skills is not None else EVIDENCE_STATUS_UNAVAILABLE,
    }
    # No native stream-json event independently confirms Skill *preload*
    # (as opposed to an explicit Read tool_use) in this repository's own
    # observed runtime state -- Claude Code has no documented preload-
    # confirmation event. This is left honestly `unavailable` rather than
    # disguised as `observed` (AC3: "preload が observed と偽装されていない").
    preload = {"status": EVIDENCE_STATUS_UNAVAILABLE, "source": None}
    expected_rel_path = _PERSONA_CANONICAL_SKILL_PATH.get(agent_name or "")
    canonical_read = extract_claude_canonical_read_receipt(stdout or "", worktree, expected_rel_path)
    return {"declaration": declaration, "preload": preload, "canonical_read": canonical_read}


def count_mutation_capable_tool_events(stdout: str) -> list[dict]:
    """Issue #2046 AC5: enumerate every mutation-capable ``tool_use`` block
    observed in the native stream. Any non-empty result is FAIL for a
    hermetic no-mutation lane run -- never a warning."""
    events: list[dict] = []
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "assistant":
            continue
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name in _MUTATION_CAPABLE_CLAUDE_TOOL_NAMES:
                events.append({"tool": name})
    return events


def build_mutation_boundary(
    hermetic: bool, settings_digest: str | None, effective_argv: list[str] | None, stdout: str | None,
) -> dict:
    """Issue #2046 AC5. Only populated for a hermetic no-mutation lane run
    (``hermetic=True``); a non-hermetic run has no session-local settings
    boundary to report and stays honestly ``unavailable``."""
    if not hermetic:
        return {
            "settings_source": None,
            "settings_digest_sha256": None,
            "effective_argv": None,
            "mutation_capable_tool_events": [],
            "mutation_capable_tool_event_count": None,
            "status": EVIDENCE_STATUS_UNAVAILABLE,
        }
    events = count_mutation_capable_tool_events(stdout or "")
    return {
        "settings_source": "session_local_generated",
        "settings_digest_sha256": settings_digest,
        "effective_argv": [_redact(a) for a in effective_argv] if effective_argv else None,
        "mutation_capable_tool_events": events,
        "mutation_capable_tool_event_count": len(events),
        "status": EVIDENCE_STATUS_OBSERVED if stdout is not None else EVIDENCE_STATUS_UNAVAILABLE,
    }


def build_hermetic_settings_payload() -> dict:
    """Issue #2046 AC5: session-local settings restricting the tool surface
    to Read only, independent of (and never mutating) any project-level
    ``.claude/settings.json``. Deliberately narrow and fixed -- not
    configurable per caller -- because this lane's entire purpose is to
    bound the mutation surface for evidence collection."""
    return {
        "permissions": {
            "allow": ["Read(*)"],
            "deny": ["Edit(*)", "MultiEdit(*)", "Write(*)", "NotebookEdit(*)", "Bash(*)", "Agent(*)"],
        }
    }


def build_settings_provenance(worktree: str, hermetic: bool, settings_digest: str | None) -> dict:
    """Issue #2046 Outcome item (6): settings provenance, separated from
    ``mutation_boundary`` so a caller can inspect "which settings source was
    effective" without conflating it with the mutation-event evidence."""
    if hermetic:
        return {
            "source": "session_local_generated",
            "digest_sha256": settings_digest,
            "status": EVIDENCE_STATUS_DECLARED if settings_digest else EVIDENCE_STATUS_UNAVAILABLE,
        }
    settings_path = Path(worktree) / ".claude" / "settings.json"
    try:
        digest = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    except OSError:
        return {"source": "project_default", "digest_sha256": None, "status": EVIDENCE_STATUS_UNAVAILABLE}
    return {"source": "project_default", "digest_sha256": digest, "status": EVIDENCE_STATUS_DECLARED}


# ---------------------------------------------------------------------------
# Interactive herdr lane — isolated named session (Issue #1921 P0-1..P0-4)
# ---------------------------------------------------------------------------


def _extract_agent_field(raw: str, field: str):
    """Extract a field from ``herdr agent get`` JSON output, tolerating both
    the ``{"result": {"agent": {...}}}`` envelope and flatter shapes."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    agent_obj = result.get("agent") if isinstance(result, dict) else None
    if isinstance(agent_obj, dict) and field in agent_obj:
        return agent_obj[field]
    if field in payload:
        return payload[field]
    return None


def _extract_pane_id_from_workspace(raw: str) -> str | None:
    """Parse the ``pane_id`` out of ``herdr workspace create`` JSON output.

    Confirmed against a real ``herdr`` binary (v0.7.5): the shape is
    ``{"result": {"root_pane": {"pane_id": ...}, "workspace": {...}, ...}}``
    -- ``root_pane`` is a sibling of ``workspace`` under ``result``, not
    nested inside it. Fallback shapes are tolerated defensively for
    forward/backward compatibility, but the confirmed shape is checked
    first.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        stripped = raw.strip()
        return stripped.splitlines()[-1].strip() if stripped else None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for candidate in (
        result,
        result.get("workspace") if isinstance(result.get("workspace"), dict) else {},
        payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {},
        payload,
    ):
        root_pane = candidate.get("root_pane") if isinstance(candidate, dict) else None
        if isinstance(root_pane, dict) and root_pane.get("pane_id"):
            return str(root_pane["pane_id"]).strip() or None
    if payload.get("pane_id"):
        return str(payload["pane_id"]).strip() or None
    return None


class HerdrLaneError(Exception):
    def __init__(self, message: str, *, skip: bool = False):
        super().__init__(message)
        self.message = message
        self.skip = skip


def _isolated_env() -> dict[str, str]:
    """Environment with any inherited caller-session Herdr identity stripped."""
    env = dict(os.environ)
    for key in _ISOLATION_ENV_KEYS_TO_STRIP:
        env.pop(key, None)
    return env


def _herdr_sessions(herdr_bin: str) -> list[dict] | None:
    rc, out, _err, _timed_out = _run([herdr_bin, "session", "list", "--json"], timeout=15.0)
    if rc != 0:
        return None
    try:
        payload = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return []
    return [entry for entry in sessions if isinstance(entry, dict)]


def _herdr_session_names(herdr_bin: str) -> set[str] | None:
    sessions = _herdr_sessions(herdr_bin)
    if sessions is None:
        return None
    return {str(entry["name"]) for entry in sessions if entry.get("name")}


def new_isolated_session_name(herdr_bin: str) -> str:
    """A high-entropy session name not currently present in
    ``herdr session list``. Never reuses the caller's own session."""
    for _attempt in range(5):
        candidate = f"rts-{uuid.uuid4().hex}"[:32]
        existing = _herdr_session_names(herdr_bin)
        if existing is None:
            raise HerdrLaneError("could not enumerate existing herdr sessions for collision check")
        if candidate not in existing:
            return candidate
    raise HerdrLaneError("could not generate a unique isolated herdr session name")


def create_isolated_session(herdr_bin: str, session_name: str, *, timeout_seconds: float = 20.0) -> subprocess.Popen:
    """Spawn a brand-new, detached, named Herdr session and block until its
    appearance in ``herdr session list --json`` is confirmed.

    This never reuses -- and never silently falls back to -- the caller's
    own ambient/attached session. A real ``herdr`` refuses to nest a new
    session launch inside a shell that is already running inside an active
    Herdr pane by default ("nested herdr is disabled by default"); any such
    failure (or any other failure to observe the new session actually
    appear) is a hard SKIP, not a fallback to operating in the ambient
    session (Issue #1921 P0-1 fix-delta).
    """
    isolation_env = _isolated_env()
    try:
        proc = subprocess.Popen(
            [herdr_bin, "--session", session_name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=isolation_env, start_new_session=True,
        )
    except OSError as exc:
        raise HerdrLaneError(f"could not spawn isolated herdr session: {exc}", skip=True) from exc

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            try:
                _out, err = proc.communicate(timeout=2.0)
            except (subprocess.TimeoutExpired, ValueError):
                err = ""
            raise HerdrLaneError(
                "herdr isolated session process exited before becoming ready "
                f"(nested-session restrictions may be in effect): {_redact((err or '').strip()[:300])}",
                skip=True,
            )
        names = _herdr_session_names(herdr_bin)
        if names is not None and session_name in names:
            return proc
        time.sleep(0.3)
    proc.terminate()
    raise HerdrLaneError("herdr isolated session did not appear in session list within timeout", skip=True)


def _session_socket_path(herdr_bin: str, session_name: str) -> str | None:
    sessions = _herdr_sessions(herdr_bin)
    if not sessions:
        return None
    for entry in sessions:
        if str(entry.get("name")) == session_name and entry.get("socket_path"):
            return str(entry["socket_path"])
    return None


def run_interactive_herdr_isolated(
    runtime: str,
    worktree: str,
    prompt: str,
    timeout_seconds: float,
    run_id: str,
    evidence: dict,
    *,
    herdr_bin: str = "herdr",
    claude_bin_override: str | None = None,
) -> list[str]:
    """Drive an isolated-session herdr agent lifecycle. Mutates ``evidence``
    in place (so cleanup/session identity survive even if this raises) and
    returns the bounded, redacted pane output lines.

    Issue #2174 (AC1): when ``runtime == "claude"`` and ``claude_bin_override``
    is a non-empty absolute path, ``herdr agent start --kind claude`` is made
    to resolve that exact binary instead of whatever ``claude`` happens to be
    on the ambient ``PATH``. ``herdr`` itself has no flag to accept an
    explicit binary path for ``--kind`` (it always re-resolves the runtime
    name via its own PATH lookup -- see ``references/claude-code.md``), so
    this is done by prepending a session-local temporary directory
    containing a single ``claude`` symlink (pointing at
    ``claude_bin_override``) to ``PATH`` in the isolated session's own
    environment, before ``herdr workspace create`` / ``herdr agent start``
    run. Omitted by default (``claude_bin_override=None``), leaving every
    pre-existing caller's isolated-session ``PATH`` unchanged (AC6)."""
    session_name = new_isolated_session_name(herdr_bin)
    evidence["session_name"] = session_name

    agent_name = f"rts-{runtime}-{run_id}"[:32]
    evidence["agent_name"] = agent_name
    pane_output_lines: list[str] = []
    session_proc: subprocess.Popen | None = None
    claude_bin_shim_dir: str | None = None
    try:
        # Actually create the isolated session (not merely set an env var and
        # hope) and block until its independent existence is confirmed via
        # ``herdr session list --json`` (Issue #1921 P0-1 fix-delta). Any
        # failure here (including a shell already nested inside an active
        # Herdr pane, which real herdr refuses by default) raises a SKIP --
        # it never falls through to operating against the caller's own
        # session. This call (and everything after it) is inside the same
        # try/finally as the rest of the lifecycle so a signal/exception
        # arriving *during* creation confirmation still triggers cleanup
        # (Issue #1921 P0-2 fix-delta iteration 2: a session/process leak
        # was observed here when creation and the rest of the lifecycle
        # were in separate try scopes).
        session_proc = create_isolated_session(herdr_bin, session_name, timeout_seconds=20.0)

        socket_path = _session_socket_path(herdr_bin, session_name)
        isolated_env = _isolated_env()
        isolated_env["HERDR_SESSION"] = session_name
        if socket_path:
            isolated_env["HERDR_SOCKET_PATH"] = socket_path

        if runtime == "claude" and claude_bin_override:
            resolved_claude_bin_override = os.path.realpath(claude_bin_override)
            if not os.path.isfile(resolved_claude_bin_override) or not os.access(
                resolved_claude_bin_override, os.X_OK
            ):
                raise HerdrLaneError(
                    "--claude-bin path is not an executable file: "
                    f"{claude_bin_override}"
                )
            claude_bin_shim_dir = tempfile.mkdtemp(prefix="worktree-agent-runtime-smoke-claude-bin-")
            os.symlink(resolved_claude_bin_override, str(Path(claude_bin_shim_dir) / "claude"))
            existing_path = isolated_env.get("PATH", os.defpath)
            isolated_env["PATH"] = claude_bin_shim_dir + os.pathsep + existing_path

        rc, out, err, timed_out = _run(
            [herdr_bin, "workspace", "create", "--cwd", worktree, "--no-focus"],
            timeout=20.0, env=isolated_env,
        )
        if timed_out or rc != 0:
            raise HerdrLaneError(f"herdr workspace create failed: {_redact(err or out)}")
        pane_id = _extract_pane_id_from_workspace(out)
        if not pane_id:
            raise HerdrLaneError("could not parse pane_id from herdr workspace create output")
        evidence["pane_id"] = pane_id

        # Issue #1960 AC5: the interactive lane never forwards
        # structured-only flags (``--output-format`` / ``--include-hook-events``
        # / ``--no-session-persistence`` / ``--max-turns``) to the TUI
        # launch. Bounded execution for this lane comes from herdr's own
        # wait timeout, process termination, and isolated-session
        # stop/delete/removal confirmation (see Outcome / Interactive lane
        # in Issue #1960) -- not from a structured-lane print-mode flag
        # that has not been separately confirmed to be honored by an
        # interactive Claude Code launch.
        agent_extra_args: list[str] = []

        # A freshly created workspace's shell may not be an "available shell"
        # yet (still initializing). Retry ``agent start`` with a bounded,
        # short backoff instead of failing on the first race.
        start_rc = None
        start_out = start_err = ""
        start_timed_out = False
        for attempt in range(5):
            start_rc, start_out, start_err, start_timed_out = _run(
                [herdr_bin, "agent", "start", agent_name, "--kind", runtime,
                 "--pane", pane_id, "--timeout", str(int(min(timeout_seconds, 300.0) * 1000)),
                 *agent_extra_args],
                timeout=timeout_seconds, env=isolated_env,
            )
            if start_timed_out or start_rc == 0:
                break
            if "agent_pane_busy" not in (start_err or "") and "agent_pane_busy" not in (start_out or ""):
                break
            time.sleep(1.0 + attempt * 0.5)
        if start_timed_out or start_rc != 0:
            raise HerdrLaneError(f"herdr agent start failed: {_redact(start_err or start_out)}")

        prompt_deadline = time.monotonic() + timeout_seconds
        rc, out, err, timed_out = _run(
            [herdr_bin, "agent", "prompt", agent_name, prompt, "--wait",
             "--timeout", str(int(timeout_seconds * 1000))],
            timeout=timeout_seconds + 20.0, env=isolated_env,
        )
        if timed_out:
            raise HerdrLaneError("herdr agent prompt timed out")
        if rc != 0:
            if "agent_prompt_stalled" in (err or "") or "agent_prompt_stalled" in (out or ""):
                # See references/herdr.md — Claude Code's bracketed-paste
                # handling can leave a multi-line prompt unsubmitted. Recover
                # deterministically, exactly once, by sending an explicit
                # ``enter`` keypress, then poll for a genuine
                # ``state_change_seq`` change before trusting ``agent wait``
                # (which matches immediately if already idle at call time).
                evidence["prompt_stall_recovered"] = False
                baseline_rc, baseline_out, _e, _t = _run(
                    [herdr_bin, "agent", "get", agent_name], timeout=15.0, env=isolated_env,
                )
                baseline_seq = (
                    _extract_agent_field(baseline_out, "state_change_seq")
                    if baseline_rc == 0 else None
                )

                remaining = max(1.0, prompt_deadline - time.monotonic())
                send_rc, send_out, send_err, send_timed_out = _run(
                    [herdr_bin, "agent", "send-keys", agent_name, "enter"],
                    timeout=min(20.0, remaining), env=isolated_env,
                )
                if send_timed_out or send_rc != 0:
                    raise HerdrLaneError(
                        "herdr agent prompt stalled and recovery send-keys failed: "
                        f"{_redact(send_err or send_out or err or out)}"
                    )

                poll_deadline = min(prompt_deadline, time.monotonic() + 15.0)
                observed_change = baseline_seq is None
                while not observed_change and time.monotonic() < poll_deadline:
                    poll_rc, poll_out, _e, _t = _run(
                        [herdr_bin, "agent", "get", agent_name], timeout=10.0, env=isolated_env,
                    )
                    if poll_rc == 0:
                        seq = _extract_agent_field(poll_out, "state_change_seq")
                        if seq is not None and seq != baseline_seq:
                            observed_change = True
                            break
                    time.sleep(0.5)
                if not observed_change:
                    raise HerdrLaneError(
                        "herdr agent prompt stalled and recovery send-keys produced "
                        "no observed state change; prompt remains unsubmitted"
                    )

                remaining = max(1.0, prompt_deadline - time.monotonic())
                wait_rc, wait_out, wait_err, wait_timed_out = _run(
                    [herdr_bin, "agent", "wait", agent_name,
                     "--timeout", str(int(remaining * 1000))],
                    timeout=remaining + 20.0, env=isolated_env,
                )
                if wait_timed_out or wait_rc != 0:
                    raise HerdrLaneError(
                        "herdr agent prompt stalled and recovery wait failed: "
                        f"{_redact(wait_err or wait_out or err or out)}"
                    )
                evidence["prompt_stall_recovered"] = True
            else:
                raise HerdrLaneError(f"herdr agent prompt failed: {_redact(err or out)}")

        rc, out, err, timed_out = _run(
            [herdr_bin, "agent", "get", agent_name], timeout=20.0, env=isolated_env,
        )
        state = None
        if rc == 0:
            try:
                payload = json.loads(out)
                result = payload.get("result") if isinstance(payload, dict) else None
                agent_obj = (result or {}).get("agent") if isinstance(result, dict) else None
                if isinstance(agent_obj, dict):
                    state = agent_obj.get("agent_status")
                else:
                    state = (payload or {}).get("agent_status") or (payload or {}).get("state")
            except (json.JSONDecodeError, ValueError):
                state = out.strip()
        evidence["final_state"] = state
        if state in ("unknown", None):
            raise HerdrLaneError(f"agent lifecycle state is unusable for evidence: {state}")

        rc, out, _err, _timed_out = _run(
            [herdr_bin, "agent", "explain", agent_name, "--json"], timeout=20.0, env=isolated_env,
        )
        if rc == 0:
            try:
                explain_payload = json.loads(out)
                if isinstance(explain_payload, dict):
                    evidence["detected_agent"] = explain_payload.get("agent")
                    evidence["detected_agent_confidence"] = explain_payload.get("confidence")
            except (json.JSONDecodeError, ValueError):
                pass

        rc, out, _err, _timed_out = _run(
            [herdr_bin, "agent", "read", agent_name, "--source", "recent-unwrapped",
             "--lines", str(_MAX_PANE_LINES)],
            timeout=20.0, env=isolated_env,
        )
        if rc == 0:
            pane_output_lines = _bounded_redacted_lines(out, _MAX_PANE_LINES)

        return pane_output_lines
    finally:
        cleanup = evidence["cleanup"]
        cleanup["attempted"] = True
        stop_rc, _o, _e, _t = _run(
            [herdr_bin, "session", "stop", session_name, "--json"], timeout=20.0,
        )
        cleanup["stop_rc"] = stop_rc
        delete_rc, _o2, _e2, _t2 = _run(
            [herdr_bin, "session", "delete", session_name, "--json"], timeout=20.0,
        )
        cleanup["delete_rc"] = delete_rc
        remaining = _herdr_session_names(herdr_bin)
        cleanup["confirmed_removed"] = bool(remaining is not None and session_name not in remaining)
        # Defense in depth: ``session stop``/``session delete`` should have
        # already ended the spawned client process, but terminate it
        # explicitly in case it did not (never leave an orphaned process).
        # ``session_proc`` can still be ``None`` here if session creation
        # itself never completed (e.g. it raised before returning).
        if session_proc is not None and session_proc.poll() is None:
            session_proc.terminate()
            try:
                session_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                session_proc.kill()
        if claude_bin_shim_dir is not None:
            shutil.rmtree(claude_bin_shim_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Evidence writing — allowlist-only summary.md (Issue #1921 P1 fix-delta:
# no raw transcript, no native event dump, no agent-explain blob).
# ---------------------------------------------------------------------------


def write_evidence(output_dir: Path, *, schema_summary: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    summary_lines = ["# Runtime Smoke Summary", ""]
    for key in sorted(schema_summary.keys()):
        summary_lines.append(f"- {key}: {schema_summary[key]}")
    (output_dir / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def count_session_log_metadata(raw_lines: list[str]) -> int:
    """Count lines whose parsed JSON object carries at least one allowlisted
    presence-signal key. Values are never persisted (Issue #1921 P1
    fix-delta): only the count is reported."""
    count = 0
    for line in raw_lines[:_MAX_SESSION_LOG_LINES]:
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if any(key in payload for key in _ALLOWLIST_SESSION_LOG_KEYS):
            count += 1
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    """argparse ``type`` for ``--max-turns`` (Issue #1960 AC6): only accepts
    integers >= 1. ``0`` and negative values are rejected as an argument
    error (argparse ``error()`` -> exit code 2), not silently clamped or
    accepted."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--max-turns must be a positive integer, got: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"--max-turns must be a positive integer, got: {parsed}")
    return parsed


def _absolute_path(value: str) -> str:
    """argparse ``type`` for ``--claude-bin`` (Issue #2174 AC1): only accepts
    absolute paths, rejecting relative paths as an argument error (argparse
    ``error()`` -> exit code 2)."""
    if not os.path.isabs(value):
        raise argparse.ArgumentTypeError(
            f"--claude-bin must be an absolute path, got: {value!r}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="worktree-agent-runtime-smoke runner")
    parser.add_argument("--runtime", choices=["claude", "codex"], required=True)
    parser.add_argument("--mode", choices=["structured", "interactive"], required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--timeout-is-capability-unavailable",
        action="store_true",
        help=(
            "classify a structured-lane timeout as capability-unavailable "
            "(exit 77) instead of a runtime failure. This is opt-in for "
            "callers whose bounded verification window is itself the "
            "runtime-capability boundary; the default timeout behavior is "
            "exit 1."
        ),
    )
    parser.add_argument("--max-turns", type=_positive_int, default=_DEFAULT_MAX_TURNS,
                         help="bounded turn count for Claude Code (structured lane only; positive integer)")
    parser.add_argument(
        "--claude-bin",
        type=_absolute_path,
        default=None,
        help=(
            "Issue #2174 (AC1): optional absolute path to a claude-compatible "
            "executable (e.g. a claude-gpt launcher). Applies only to "
            "--runtime claude. When provided, this fixed absolute path is "
            "used directly as the claude executable for the structured lane "
            "(bypassing shutil.which('claude') PATH resolution) and, for "
            "the interactive herdr lane, via a session-local PATH override "
            "so 'herdr agent start --kind claude' resolves this exact "
            "binary instead of whatever 'claude' is on the ambient PATH. "
            "Omitted by default (None), so every pre-existing caller's "
            "shutil.which('claude') PATH resolution is unchanged."
        ),
    )
    parser.add_argument("--expect-marker", action="append", default=[])
    parser.add_argument(
        "--require-observed-runtime-field",
        action="append",
        choices=sorted(_REQUIRED_RUNTIME_OBSERVATION_FIELDS),
        default=[],
        help=(
            "require a field to be independently observed in native runtime "
            "evidence; unavailable required fields cause exit 77/SKIP, never PASS"
        ),
    )
    parser.add_argument("--require-clean-postcondition", action="store_true")
    parser.add_argument("--inspect-session-log-metadata", action="store_true")
    parser.add_argument("--require-session-log-metadata", action="store_true")
    parser.add_argument("--repo-root", default=None, help="override canonical repository root (tests only)")
    parser.add_argument(
        "--agent-type",
        default=_UNSPECIFIED_AGENT_TYPE,
        help=(
            "declares which worker/agent persona this smoke run represents "
            "(e.g. post-merge-cleanup-worker), used to derive "
            "requested_agent_type / effective_agent_type / loaded_skills "
            "evidence. Optional (defaults to the placeholder "
            f"'{_UNSPECIFIED_AGENT_TYPE}') so pre-existing callers that do not "
            "pass this flag are not broken; AC12-grade invocations must pass "
            "a real value."
        ),
    )
    parser.add_argument(
        "--requested-mutation-route",
        default=None,
        help=(
            "optional role-bound mutation route to preflight before launching "
            "the runtime; a mismatched route is refused before any runtime "
            "subprocess is invoked"
        ),
    )
    parser.add_argument(
        "--require-transaction-entrypoint-preflight",
        action="store_true",
        help=(
            "for a role-bound route request, safely invoke the actual "
            "transaction entrypoint with deliberately incomplete input and "
            "require its pre-executor parser refusal before recording a "
            "route mismatch refusal"
        ),
    )
    parser.add_argument(
        "--claude-agent-name",
        default=None,
        help=(
            "Issue #1734 fix_delta 3 (AC7): backward-compatible, additive, "
            "opt-in flag. When passed (claude runtime + structured mode "
            "only), inserts '--agent <name>' into the underlying 'claude' "
            "subprocess invocation inside run_structured_claude(), actually "
            "launching that Agent as the active session persona (unlike "
            "--agent-type, which is only a static declaration label never "
            "forwarded to the CLI). Defaults to None: omitted entirely, "
            "leaving every pre-existing caller's argv and behavior "
            "unchanged."
        ),
    )
    parser.add_argument(
        "--hermetic-agent-definition",
        action="store_true",
        help=(
            "Issue #2046 AC2/AC5: opt-in hermetic no-mutation lane. Requires "
            "--claude-agent-name (claude runtime + structured mode only). "
            "Instead of the project-discovery `--agent <name>` lookup, "
            "generates a session-local `--agents` JSON payload (deterministic "
            "digest of the candidate Agent definition) with tools fixed to "
            "Read only, plus a session-local `--settings` file denying every "
            "mutation-capable tool, and records both digests plus the "
            "observed mutation_capable_tool_event_count in evidence. Omitted "
            "by default, so every pre-existing caller's argv is unchanged."
        ),
    )
    return parser


def preflight_requested_mutation_route(
    worktree: str, agent_type: str, requested_route: str | None, *,
    require_transaction_entrypoint_preflight: bool = False,
) -> dict[str, object]:
    """Return a deterministic receipt for an optional mutation-route request.

    The canonical route is read from the active agent TOML rather than a test
    fixture or a hard-coded role table.  A refusal is a successful preflight
    outcome: it intentionally prevents the runtime from starting, so no
    prompt-only model refusal can be mistaken for an authorization boundary.
    """
    receipt: dict[str, object] = {
        "requested_mutation_route": requested_route,
        "declared_mutation_route": None,
        "route_preflight_decision": "not_requested",
        "route_preflight_source": None,
        "controlled_route_preflight_status": "not_requested",
        "canonical_transaction_entrypoint": None,
        "requested_transaction_entrypoint": None,
        "pre_executor_refusal_observed": False,
        "executor_invocation_observed": False,
        "mutation_attempted": None,
        "mutation_observed_channels": [],
    }
    if requested_route is None:
        return receipt
    if agent_type == _UNSPECIFIED_AGENT_TYPE:
        receipt["route_preflight_decision"] = "invalid_agent_type"
        receipt["route_preflight_source"] = "runner_agent_route_guard"
        return receipt

    agent_path = Path(worktree) / ".codex" / "agents" / f"{agent_type}.toml"
    try:
        text = agent_path.read_text(encoding="utf-8")
    except OSError:
        receipt["route_preflight_decision"] = "agent_config_unavailable"
        receipt["route_preflight_source"] = "runner_agent_route_guard"
        return receipt

    match = _RUNTIME_FOLLOWUP_ROUTE_RE.search(text)
    if match is None:
        receipt["route_preflight_decision"] = "declared_route_unavailable"
        receipt["route_preflight_source"] = "runner_agent_route_guard"
        return receipt

    declared_route = match.group(1)
    receipt["declared_mutation_route"] = declared_route
    receipt["route_preflight_source"] = "runner_agent_route_guard"
    receipt["route_preflight_decision"] = (
        "allow" if requested_route == declared_route else "refused_before_runtime"
    )
    if not require_transaction_entrypoint_preflight:
        return receipt

    declared_entrypoint = _TRANSACTION_ENTRYPOINTS.get(declared_route)
    requested_entrypoint = _TRANSACTION_ENTRYPOINTS.get(requested_route)
    receipt["declared_transaction_entrypoint"] = declared_entrypoint
    receipt["requested_transaction_entrypoint"] = requested_entrypoint
    if declared_entrypoint is None:
        receipt["controlled_route_preflight_status"] = "canonical_entrypoint_unavailable"
        return receipt
    entrypoint_path = Path(worktree) / declared_entrypoint
    receipt["canonical_transaction_entrypoint"] = declared_entrypoint
    if not entrypoint_path.is_file():
        receipt["controlled_route_preflight_status"] = "canonical_entrypoint_missing"
        return receipt
    # Invoke the actual selected transaction entrypoint with deliberately
    # incomplete input. Both create and edit parsers reject before their
    # executor/mutation transport can begin (exit 2), giving AC6 a concrete
    # non-mutating parser receipt rather than treating --help as a decision.
    rc, _out, _err, timed_out = _run(
        [sys.executable, str(entrypoint_path)], cwd=worktree, timeout=10.0
    )
    if rc != 2 or timed_out:
        receipt["controlled_route_preflight_status"] = "invalid_transaction_input_not_rejected"
        return receipt
    receipt["controlled_route_preflight_status"] = "invalid_transaction_input_rejected_pre_executor"
    receipt["pre_executor_refusal_observed"] = requested_route != declared_route
    return receipt


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    parser = build_parser()
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:12]
    errors: list[str] = []

    repo_root = args.repo_root or _default_repo_root()

    try:
        worktree = verify_worktree_identity(args.worktree, repo_root)
    except IdentityError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return EXIT_FAIL

    try:
        prompt = read_prompt(args.prompt_file)
    except OSError as exc:
        print(f"[FAIL] could not read prompt file: {exc}", file=sys.stderr)
        return EXIT_FAIL

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = Path(worktree) / output_dir
    try:
        output_dir_rel = os.path.relpath(str(output_dir), worktree)
    except ValueError:
        output_dir_rel = None

    # Cheap, environment-independent checks (output directory exclusivity)
    # run before any capability/herdr preflight so they fail fast regardless
    # of whether claude/codex/herdr happen to be installed. This check
    # itself cannot emit summary.md evidence (its very failure is that
    # output_dir is unusable to write into), so it remains an early return.
    dir_error = prepare_output_dir(output_dir)
    if dir_error:
        print(f"[FAIL] {dir_error}", file=sys.stderr)
        return EXIT_FAIL

    route_preflight = preflight_requested_mutation_route(
        worktree,
        args.agent_type,
        args.requested_mutation_route,
        require_transaction_entrypoint_preflight=args.require_transaction_entrypoint_preflight,
    )
    route_refused = (
        args.requested_mutation_route is not None
        and route_preflight["route_preflight_decision"] != "allow"
    )

    # From this point on, worktree/prompt/output_dir are all confirmed
    # usable, so EVERY controlled exit below -- including the
    # capability/herdr preflight SKIPs -- must emit allowlist-only
    # summary.md evidence (Issue #1960 AC7 P1-1 fix-delta: prior to this
    # fix, ``preflight_herdr`` / ``preflight_claude_available`` /
    # ``preflight_codex_flags`` failures each did an early ``return
    # EXIT_SKIP`` before ``schema_summary`` was ever constructed, so those
    # three controlled SKIP 77 paths silently produced no summary.md at
    # all). There is no further early ``return`` below this line; every
    # path falls through to the single ``write_evidence`` call at the
    # bottom of this function.
    exit_code = EXIT_OK
    resolved_runtime_bin: str | None = None
    entrypoint_preflight_failed = (
        args.require_transaction_entrypoint_preflight
        and args.requested_mutation_route is not None
        and route_preflight["controlled_route_preflight_status"]
        != "invalid_transaction_input_rejected_pre_executor"
    )
    if entrypoint_preflight_failed:
        errors.append(
            "controlled route preflight unavailable: "
            f"{route_preflight['controlled_route_preflight_status']}"
        )
        exit_code = EXIT_FAIL

    if args.mode == "interactive" and not route_refused:
        skip_reason = preflight_herdr()
        if skip_reason:
            errors.append(skip_reason)
            exit_code = EXIT_SKIP

    if exit_code == EXIT_OK and not route_refused:
        # Issue #1960 Design Decision 5 (P1-2 fix-delta): resolve the
        # runtime executable exactly ONCE here via ``shutil.which()``
        # (inside ``preflight_claude_available`` / ``preflight_codex_flags``)
        # and thread that same absolute path through version capture and
        # structured-lane execution below, instead of independently
        # re-resolving "claude"/"codex" by name in each place. Structured
        # -lane flag capability itself is still decided from the actual
        # fixed-argv invocation result (classify_claude_structured_outcome),
        # never from ``claude --help`` text (AC1/AC5).
        if args.runtime == "claude":
            resolved_runtime_bin, skip_reason = preflight_claude_available(args.claude_bin)
        else:
            resolved_runtime_bin, skip_reason = preflight_codex_flags()
        if skip_reason:
            errors.append(skip_reason)
            exit_code = EXIT_SKIP

    # Structured telemetry fields that are trivially and deterministically
    # derivable up front (Issue #1733 Scope Delta, 2026-08-02 owner-approved
    # harness extension). ``tested_head``/``runtime_version``/
    # ``prompt_sha256`` are captured once at run start; ``loaded_skills`` is a
    # static frontmatter fact independent of the run itself.
    tested_head = _git_rev_parse(worktree, "HEAD")
    runtime_version = capture_runtime_version(resolved_runtime_bin) if resolved_runtime_bin else None
    requested_agent_type = args.agent_type
    # A requested agent is a declaration, not runtime observation.  The
    # effective identity remains unavailable until the native child evidence
    # below supplies one; it must never be filled from the request itself.
    effective_agent_type = None
    loaded_skills = load_static_declared_skills(worktree, requested_agent_type)
    prompt_sha256 = compute_prompt_sha256(prompt)

    # Issue #2046: main-session agent identity / definition binding / Skill
    # evidence / mutation boundary / settings provenance. Scoped to the
    # claude runtime + the caller-supplied --claude-agent-name persona
    # binding (the same flag Issue #1734 fix_delta 3 introduced) -- a run
    # with no --claude-agent-name has nothing to bind identity to and every
    # new field below stays honestly unavailable/not-requested.
    hermetic_requested = bool(args.hermetic_agent_definition) and args.claude_agent_name is not None
    agent_definition, hermetic_agents_payload, hermetic_agent_name = resolve_agent_definition(
        worktree, args.claude_agent_name, hermetic_requested
    )
    hermetic_active = hermetic_requested and hermetic_agents_payload is not None
    hermetic_settings_payload = build_hermetic_settings_payload() if hermetic_active else None
    hermetic_settings_digest = (
        hashlib.sha256(
            json.dumps(hermetic_settings_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if hermetic_settings_payload is not None
        else None
    )
    hermetic_tmp_dir: str | None = None
    hermetic_agents_file: str | None = None
    hermetic_settings_file: str | None = None
    if hermetic_active:
        # A system-temp directory (never inside the worktree), so writing
        # these session-local files never perturbs the worktree's own
        # postcondition fingerprint and is always cleaned up below.
        hermetic_tmp_dir = tempfile.mkdtemp(prefix="worktree-agent-runtime-smoke-hermetic-")
        hermetic_agents_file = str(Path(hermetic_tmp_dir) / "agents.json")
        hermetic_settings_file = str(Path(hermetic_tmp_dir) / "settings.json")
        Path(hermetic_agents_file).write_text(json.dumps(hermetic_agents_payload), encoding="utf-8")
        Path(hermetic_settings_file).write_text(json.dumps(hermetic_settings_payload), encoding="utf-8")

    schema_summary: dict = {
        "schema": SCHEMA,
        "run_id": run_id,
        "runtime": args.runtime,
        "mode": args.mode,
        "transport": "direct" if args.mode == "structured" else "herdr_isolated_session",
        "worktree": os.path.relpath(worktree, repo_root),
        "timeout_seconds": args.timeout_seconds,
        "tested_head": tested_head,
        "runtime_version": runtime_version,
        "resolved_executable": resolved_runtime_bin,
        "requested_agent_type": requested_agent_type,
        "effective_agent_type": effective_agent_type,
        "loaded_skills": loaded_skills,
        "loaded_skills_source": "static_frontmatter" if loaded_skills is not None else None,
        "prompt_sha256": prompt_sha256,
        "agent_definition": agent_definition,
        # main_agent_identity / skill_evidence / mutation_boundary are
        # placeholders here (no stdout captured yet); overwritten below with
        # real evidence once the structured claude invocation completes.
        "main_agent_identity": build_main_agent_identity(args.claude_agent_name, None),
        "skill_evidence": build_skill_evidence(args.claude_agent_name, worktree, None),
        "mutation_boundary": build_mutation_boundary(hermetic_active, hermetic_settings_digest, None, None),
        "settings_provenance": build_settings_provenance(worktree, hermetic_active, hermetic_settings_digest),
        # Issue #2046 AC10: #1881 (production settings lane, pr-reviewer
        # persona safe Read/mutation-deny boundary) remains separately OPEN.
        # This hermetic no-mutation lane's mutation_boundary/settings_
        # provenance evidence is a session-local receipt only and must never
        # be promoted to a production settings/permission claim until #1881
        # merges.
        "production_settings_lane": (
            "deferred_to_#1881: hermetic mutation_boundary/settings_provenance "
            "evidence in this run is not a production_settings_lane result and "
            "must not be promoted to a production settings/permission claim "
            "until #1881 (pr-reviewer persona safe Read/mutation-deny boundary) "
            "merges"
        ),
        **route_preflight,
    }
    if args.mode == "interactive":
        # Issue #1960 Design Decision 5 (P1-2 fix-delta): the interactive
        # lane launches via ``herdr agent start --kind <runtime>``, which
        # resolves the runtime binary through herdr's own PATH lookup
        # rather than accepting an explicit binary path from this runner.
        # The preflight-resolved absolute path above is therefore not
        # passed through to herdr, and exact-binary identity between this
        # preflight resolution and the process herdr actually launches is
        # not independently confirmed for this lane -- an honest,
        # documented constraint rather than a silently omitted guarantee.
        schema_summary["resolved_executable_binding_note"] = (
            "interactive lane launches via `herdr agent start --kind "
            "claude`; --claude-bin was supplied, so a session-local PATH "
            "override (a `claude` name symlinked to the --claude-bin "
            "absolute path) was prepended to the isolated herdr session's "
            "environment before `herdr agent start`, so herdr's own PATH "
            "lookup resolves to this explicit binary (Issue #2174)."
        ) if (args.runtime == "claude" and args.claude_bin) else (
            "interactive lane launches via `herdr agent start --kind "
            "<runtime>`, which re-resolves the binary via herdr's own PATH "
            "lookup; resolved_executable above (from this runner's own "
            "preflight) is not passed through explicitly, so exact-binary "
            "identity is not independently confirmed for this lane."
        )

    before_fp = (
        repo_fingerprint(worktree, output_dir_rel)
        if args.require_clean_postcondition and exit_code == EXIT_OK
        else None
    )

    try:
        if route_refused:
            # The controlled guard declined the route before launching a
            # runtime.  Record that limited fact only; do not synthesize a
            # terminal event, loaded Skill, executor invocation, or mutation
            # observation from static configuration.
            schema_summary["runtime_invocation"] = "not_started_route_preflight_blocked"
            schema_summary["terminal_event_observed"] = None
            schema_summary["child_agent_type_observed"] = None
            schema_summary["child_agent_type_source"] = None
            schema_summary["agent_type_identity_verified"] = False
            schema_summary["native_spawn_event_observed"] = False
            schema_summary["child_spawn_observed"] = False
            schema_summary["child_spawn_source"] = None
            schema_summary["child_launch_mode"] = None
            schema_summary["child_completion_observed"] = False
            schema_summary["child_completion_source"] = None
            schema_summary["child_terminal_status"] = None
            schema_summary["child_agent_id"] = None
            schema_summary["spawn_elapsed_sec"] = None
            schema_summary["completion_elapsed_sec"] = None
        elif exit_code != EXIT_OK:
            # A capability/herdr preflight above already decided this run is
            # a controlled SKIP -- do not attempt to launch either lane.
            # Evidence (schema_summary as built so far, including
            # resolved_executable and the SKIP reason already appended to
            # ``errors``) is still written unconditionally below (Issue
            # #1960 AC7 P1-1 fix-delta).
            pass
        elif args.mode == "structured":
            if args.runtime == "claude":
                effective_claude_agent_name = (
                    hermetic_agent_name if hermetic_active else args.claude_agent_name
                )
                claude_invocation_argv_for_evidence = [
                    resolved_runtime_bin or "claude", "-p", "--output-format", "stream-json",
                    "--include-hook-events", "--no-session-persistence",
                    "--max-turns", str(args.max_turns), "--verbose",
                ]
                if effective_claude_agent_name:
                    claude_invocation_argv_for_evidence += ["--agent", effective_claude_agent_name]
                if hermetic_active:
                    claude_invocation_argv_for_evidence += ["--agents", hermetic_agents_file or ""]
                    claude_invocation_argv_for_evidence += ["--settings", hermetic_settings_file or ""]
                rc, out, err, timed_out = run_structured_claude(
                    worktree, prompt, float(args.timeout_seconds), args.max_turns,
                    claude_bin=resolved_runtime_bin,
                    claude_agent_name=effective_claude_agent_name,
                    hermetic_agents_file=hermetic_agents_file if hermetic_active else None,
                    hermetic_settings_file=hermetic_settings_file if hermetic_active else None,
                    claude_bin_is_override=bool(args.claude_bin),
                )
                capability_decision, capability_reason = classify_claude_structured_outcome(
                    rc, out, err, timed_out
                )
                # Issue #2046: real evidence now that stdout is captured.
                schema_summary["main_agent_identity"] = build_main_agent_identity(args.claude_agent_name, out)
                schema_summary["skill_evidence"] = build_skill_evidence(args.claude_agent_name, worktree, out)
                schema_summary["mutation_boundary"] = build_mutation_boundary(
                    hermetic_active, hermetic_settings_digest, claude_invocation_argv_for_evidence, out
                )
            else:
                rc, out, err, timed_out = run_structured_codex(
                    worktree, prompt, float(args.timeout_seconds), codex_bin=resolved_runtime_bin
                )
                # Codex CLI capability preflight (help-based) is out of scope
                # for Issue #1960 -- see Out of Scope: "Codex CLI lane の
                # capability preflight 見直しは本 Issue の対象外".
                capability_decision, capability_reason = "runtime_outcome", None

            event_count = parse_native_event_count(out)
            schema_summary["process_exit_code"] = rc
            schema_summary["timed_out"] = timed_out
            schema_summary["native_event_count"] = event_count
            terminal_event_observed = has_terminal_event(args.runtime, out) if event_count > 0 else None
            schema_summary["terminal_event_observed"] = terminal_event_observed
            schema_summary["capability_decision"] = capability_decision
            schema_summary["capability_error_classification"] = capability_reason

            if args.runtime == "claude":
                spawn_events, self_restart_count, orchestration_count = classify_claude_events(out)
            else:
                spawn_events, self_restart_count, orchestration_count = classify_codex_events(out)
            schema_summary["spawn_events"] = spawn_events
            schema_summary["child_spawn_event_count"] = len(spawn_events)
            schema_summary["self_restart_event_count"] = self_restart_count
            schema_summary["orchestration_action_count"] = orchestration_count
            schema_summary["direct_web_tool_event_count"] = count_direct_web_tool_events(
                args.runtime, out
            )

            # Issue #1886 AC7: native, runtime-returned spawn session
            # evidence (see extractors above). ``native_spawn_event_observed``
            # is strictly ``True`` only when both ids are non-empty and
            # different -- caller self-report never promotes this to True.
            # Issue #1886 P0-2 fix_delta (PR #2005 adversarial review): a
            # distinct, non-empty parent/child session id pair alone proved
            # only that SOME child was spawned, never that it was the
            # REQUESTED custom agent -- a generic `general-purpose` child
            # satisfied the exact same evidence as `codebase-investigator`.
            # `native_spawn_event_observed` now additionally requires the
            # runtime to have returned independent agent-identity evidence
            # that matches `requested_agent_type`.
            #
            # - Claude: the same stream-json tool_use_result that carries
            #   `agentId` also carries `agentType` (see
            #   `extract_claude_child_agent_type`); identity is verified iff
            #   that observed value equals `requested_agent_type`.
            # - Codex (Issue #1886 P0-2 iteration-N fix_delta): live
            #   investigation of real, local `~/.codex/sessions` rollout
            #   logs (multiple `codebase-investigator` / `web-researcher`
            #   routes, Codex CLI 0.146.0) found that a spawned child's own
            #   rollout log DOES carry runtime-returned identity evidence
            #   after all -- its `session_meta` record's `agent_role` field
            #   (see `extract_codex_child_agent_role`), written by the Codex
            #   CLI itself, holds the custom agent role/persona name. This
            #   supersedes the prior fail-closed-to-always-False posture
            #   (commit 8915af25): identity is verified iff that observed
            #   value equals `requested_agent_type`, exactly mirroring the
            #   Claude lane. If a future Codex CLI version ever stops
            #   emitting this field, `extract_codex_child_agent_role`
            #   returns `None` and this still fails closed.
            if args.runtime == "claude":
                parent_session_id = extract_claude_parent_session_id(out)
                child_session_id = extract_claude_child_session_id(parent_session_id, worktree, out)
                # Issue #2021: record WHICH runtime channel supplied the agent
                # type, and how the Agent tool reported the launch, so a future
                # reader can tell "no evidence at all" apart from "evidence on
                # the hook channel only" without re-parsing the raw stream.
                child_agent_type_observed, child_agent_type_source = (
                    extract_claude_child_agent_type_with_source(out)
                )
                child_spawn_launch_mode = classify_claude_spawn_launch_mode(out)
            else:
                parent_session_id = extract_codex_parent_session_id(out)
                # A timed-out child cannot supply complete identity evidence.
                # Do not spend the caller's bounded capability window scanning
                # the global rollout inventory after that terminal condition.
                child_session_id = (
                    None if timed_out else extract_codex_child_session_id(parent_session_id)
                )
                child_agent_type_observed = (
                    None if timed_out else extract_codex_child_agent_role(parent_session_id)
                )
                child_agent_type_source = (
                    "codex_session_meta_agent_role" if child_agent_type_observed else None
                )
                child_spawn_launch_mode = SPAWN_LAUNCH_MODE_UNKNOWN
            agent_type_identity_verified = (
                child_agent_type_observed is not None
                and requested_agent_type is not None
                and child_agent_type_observed == requested_agent_type
            )
            schema_summary["parent_session_id"] = parent_session_id
            schema_summary["child_session_id"] = child_session_id
            schema_summary["child_agent_type_observed"] = child_agent_type_observed
            schema_summary["child_agent_type_source"] = child_agent_type_source
            schema_summary["child_spawn_launch_mode"] = child_spawn_launch_mode
            schema_summary["agent_type_identity_verified"] = agent_type_identity_verified
            schema_summary["effective_agent_type"] = child_agent_type_observed
            schema_summary["native_spawn_event_observed"] = bool(
                parent_session_id
                and child_session_id
                and parent_session_id != child_session_id
                and agent_type_identity_verified
            )

            # Issue #2015 AC11 (OWNER Scope Reframe 2026-08-09): spawn
            # observation and completion observation as two SEPARATE,
            # explicitly-recorded signals -- see the module docstring above
            # ``classify_claude_child_completion`` for the root-cause
            # rationale (a dead process cannot be polled for a future
            # event; the fix is to stop conflating the two signals that are
            # already present in the captured stream, plus a bounded
            # filesystem poll performed by the caller after this process
            # exits).
            if args.runtime == "claude":
                child_agent_id, child_spawn_source = classify_claude_child_spawn_agent_id(out)
                child_spawn_observed = child_agent_id is not None
                completion = classify_claude_child_completion(out, child_agent_id)
                child_completion_observed = completion["observed"]
                child_completion_source = completion["source"]
                if completion["observed"]:
                    child_terminal_status = CHILD_TERMINAL_STATUS_COMPLETED
                elif child_spawn_launch_mode == SPAWN_LAUNCH_MODE_ASYNC:
                    child_terminal_status = CHILD_TERMINAL_STATUS_ASYNC_NO_STOP
                else:
                    child_terminal_status = CHILD_TERMINAL_STATUS_UNKNOWN
                # Not derivable from the structured lane's captured stdout:
                # neither the ``tool_use_result`` envelope nor the hook
                # lifecycle events in this repository's own observed event
                # shapes carry a wall-clock timestamp field for these
                # specific event kinds. Left ``None`` (never fabricated)
                # rather than approximated from this call's own outer
                # elapsed time, which would misrepresent per-child timing.
                spawn_elapsed_sec = None
                completion_elapsed_sec = None
            else:
                # Issue #2015 AC11 documented limitation: Codex CLI's
                # ``codex exec --json`` stdout (this harness only reads
                # stdout, never the on-disk rollout log for live timing) has
                # no per-child terminal-event equivalent to Claude's
                # ``SubagentStop`` hook currently identified in this
                # repository's own runtime research. ``child_session_id``
                # (recovered from the child's own rollout log, see
                # ``extract_codex_child_session_id``) is the best available
                # spawn evidence; completion is approximated as "the overall
                # session reached ITS OWN terminal event AND a child rollout
                # was recovered" -- this is a parent-level, not a genuine
                # child-level, terminal signal, and is intentionally NOT
                # promoted to the same confidence as the Claude hook-based
                # signal. A dedicated Codex-specific child terminal marker is
                # a known follow-up research gap (see PR #2044 root-cause
                # report), not silently faked here.
                child_agent_id = child_session_id
                child_spawn_source = (
                    "codex_rollout_log_spawn_agent_call" if child_session_id else None
                )
                child_spawn_observed = child_session_id is not None
                child_completion_observed = bool(child_session_id and terminal_event_observed)
                child_completion_source = (
                    "approximate_parent_terminal_event_plus_rollout_spawn"
                    if child_completion_observed
                    else None
                )
                if child_completion_observed:
                    child_terminal_status = CHILD_TERMINAL_STATUS_COMPLETED
                elif child_spawn_observed:
                    child_terminal_status = CHILD_TERMINAL_STATUS_ASYNC_NO_STOP
                else:
                    child_terminal_status = CHILD_TERMINAL_STATUS_UNKNOWN
                spawn_elapsed_sec = None
                completion_elapsed_sec = None
            schema_summary["child_spawn_observed"] = child_spawn_observed
            schema_summary["child_spawn_source"] = child_spawn_source
            schema_summary["child_launch_mode"] = child_spawn_launch_mode
            schema_summary["child_completion_observed"] = child_completion_observed
            schema_summary["child_completion_source"] = child_completion_source
            schema_summary["child_terminal_status"] = child_terminal_status
            schema_summary["child_agent_id"] = child_agent_id
            schema_summary["spawn_elapsed_sec"] = spawn_elapsed_sec
            schema_summary["completion_elapsed_sec"] = completion_elapsed_sec

            if capability_decision == "capability_skip":
                # AC2: a known unknown/unrecognized-option parser diagnostic
                # -- SKIP 77, never promoted to FAIL. summary.md (written
                # unconditionally below) records runtime_version and
                # capability_error_classification as evidence.
                errors.append(capability_reason)
                exit_code = EXIT_SKIP
            elif timed_out:
                if args.timeout_is_capability_unavailable:
                    errors.append("structured lane exceeded the declared capability window")
                    schema_summary["capability_decision"] = "capability_skip_timeout"
                    schema_summary["capability_error_classification"] = (
                        "declared_capability_window_exceeded"
                    )
                    exit_code = EXIT_SKIP
                else:
                    errors.append("structured lane timed out")
                    exit_code = EXIT_FAIL
            elif rc is None:
                errors.append(f"structured lane failed to start: {_redact(err[:500])}")
                exit_code = EXIT_FAIL
            elif capability_decision == "turn_limit_reached":
                # AC4: the flag was accepted (evidence of capability); this
                # is a bounded-turn runtime failure, not a capability SKIP.
                errors.append(capability_reason)
                exit_code = EXIT_FAIL
            elif rc != 0:
                errors.append(f"structured lane exited non-zero: {rc}: {_redact(err[:500])}")
                exit_code = EXIT_FAIL
            elif terminal_event_observed is False:
                errors.append("no terminal/result event observed in structured output")
                exit_code = EXIT_FAIL

            # A marker is a success-only assertion.  If the bounded runtime
            # capability is unavailable (including timeout SKIP), no model
            # output is available to check and a missing marker must not
            # overwrite the authoritative exit-77 classification.
            if args.expect_marker and exit_code == EXIT_OK:
                combined = out + "\n" + err
                missing = [m for m in args.expect_marker if m not in combined]
                schema_summary["expected_markers_missing"] = missing
                if missing:
                    errors.append(f"expected markers not observed: {missing}")
                    exit_code = EXIT_FAIL

            required_observations = sorted(set(args.require_observed_runtime_field))
            if required_observations:
                # These four fields have no independently extractable Codex
                # event in this runner.  Record that precise capability gap;
                # declarations or local re-reads must never fill it in.
                unavailable = required_observations
                schema_summary["required_runtime_observations"] = required_observations
                schema_summary["unavailable_required_runtime_observations"] = unavailable
                if exit_code == EXIT_OK:
                    errors.append(
                        "required runtime observations unavailable: " + ", ".join(unavailable)
                    )
                    schema_summary["capability_decision"] = "required_runtime_evidence_unavailable"
                    schema_summary["capability_error_classification"] = (
                        "native_event_field_unavailable"
                    )
                    exit_code = EXIT_SKIP

            if args.require_session_log_metadata or args.inspect_session_log_metadata:
                metadata_count = count_session_log_metadata(out.splitlines())
                schema_summary["session_log_metadata_count"] = metadata_count
                if args.require_session_log_metadata and metadata_count == 0:
                    errors.append("session-log metadata required but unavailable")
                    exit_code = EXIT_SKIP if exit_code == EXIT_OK else exit_code

            if args.runtime == "claude" and hermetic_active:
                # Issue #2046 AC5: fail-closed, unconditionally -- a
                # hermetic no-mutation lane observing ANY mutation-capable
                # tool_use event overrides even an otherwise-SKIP/OK
                # classification. A mutation attempt is strictly worse than
                # a capability gap and must never be silently absorbed by
                # one.
                mutation_event_count = schema_summary["mutation_boundary"]["mutation_capable_tool_event_count"]
                if mutation_event_count:
                    errors.append(
                        "hermetic no-mutation lane observed mutation-capable tool "
                        f"event(s): {schema_summary['mutation_boundary']['mutation_capable_tool_events']}"
                    )
                    exit_code = EXIT_FAIL

        else:  # interactive
            evidence = {
                "session_name": None,
                "pane_id": None,
                "agent_name": None,
                "final_state": None,
                "detected_agent": None,
                "detected_agent_confidence": None,
                "prompt_stall_recovered": None,
                "cleanup": {"attempted": False, "stop_rc": None, "delete_rc": None, "confirmed_removed": False},
            }
            pane_output_lines: list[str] = []
            try:
                pane_output_lines = run_interactive_herdr_isolated(
                    args.runtime, worktree, prompt, float(args.timeout_seconds), run_id, evidence,
                    claude_bin_override=args.claude_bin if args.runtime == "claude" else None,
                )

                if evidence.get("final_state") == "blocked":
                    errors.append("agent reached blocked state; evidence captured, not auto-approved")
                    exit_code = EXIT_FAIL

                if args.expect_marker:
                    combined = "\n".join(pane_output_lines)
                    missing = [m for m in args.expect_marker if m not in combined]
                    schema_summary["expected_markers_missing"] = missing
                    if missing:
                        errors.append(f"expected markers not observed in pane output: {missing}")
                        exit_code = EXIT_FAIL

                if args.require_session_log_metadata or args.inspect_session_log_metadata:
                    schema_summary["session_log_metadata_count"] = 0
                    if args.require_session_log_metadata:
                        errors.append("session-log metadata required but unavailable in interactive lane")
                        exit_code = EXIT_SKIP if exit_code == EXIT_OK else exit_code
            except HerdrLaneError as exc:
                errors.append(exc.message)
                exit_code = EXIT_SKIP if exc.skip else EXIT_FAIL

            schema_summary["session_name"] = evidence.get("session_name")
            schema_summary["pane_id"] = evidence.get("pane_id")
            schema_summary["agent_name"] = evidence.get("agent_name")
            schema_summary["final_state"] = evidence.get("final_state")
            schema_summary["detected_agent"] = evidence.get("detected_agent")
            schema_summary["detected_agent_confidence"] = evidence.get("detected_agent_confidence")
            schema_summary["prompt_stall_recovered"] = evidence.get("prompt_stall_recovered")

            # Best-effort text-scan classification over the bounded, redacted
            # pane transcript (no native JSON event stream exists for the
            # interactive lane). self_restart / orchestration commands are
            # plausibly visible as literal shell text in the pane; a nested
            # ``Agent`` tool_use invocation is NOT reliably distinguishable
            # from ordinary TUI prose in a plain-text pane transcript, so
            # child_spawn_event_count / spawn_events are left ``None``
            # (documented gap) rather than guessed for this lane.
            pane_text = "\n".join(pane_output_lines)
            schema_summary["spawn_events"] = None
            schema_summary["child_spawn_event_count"] = None
            schema_summary["self_restart_event_count"] = len(_SELF_RESTART_COMMAND_RE.findall(pane_text))
            schema_summary["orchestration_action_count"] = len(_ORCHESTRATION_ACTION_COMMAND_RE.findall(pane_text))

            cleanup = evidence.get("cleanup") or {}
            schema_summary["cleanup_attempted"] = cleanup.get("attempted", False)
            schema_summary["cleanup_confirmed_removed"] = cleanup.get("confirmed_removed", False)
            if cleanup.get("attempted") and not cleanup.get("confirmed_removed"):
                errors.append("herdr isolated session cleanup could not be confirmed removed")
                exit_code = EXIT_FAIL

        if args.require_clean_postcondition and before_fp is not None:
            after_fp = repo_fingerprint(worktree, output_dir_rel)
            diffs = diff_fingerprints(before_fp, after_fp)
            schema_summary["postcondition_unexpected_changes"] = diffs
            if diffs:
                errors.append(f"unexpected postcondition changes: {diffs}")
                exit_code = EXIT_FAIL
    except _TerminateRequested as exc:
        errors.append(f"runner terminated: {exc}")
        exit_code = EXIT_FAIL
    finally:
        if hermetic_tmp_dir is not None:
            shutil.rmtree(hermetic_tmp_dir, ignore_errors=True)

    schema_summary["errors"] = errors
    schema_summary["exit_code"] = exit_code

    write_evidence(output_dir, schema_summary=schema_summary)

    for error in errors:
        print(f"[FAIL] {error}" if exit_code == EXIT_FAIL else f"SKIP: {error}", file=sys.stderr)

    if exit_code == EXIT_OK:
        print(f"OK: runtime smoke evidence written to {output_dir}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
