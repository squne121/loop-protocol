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

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

_MAX_PANE_LINES = 400
_MAX_LINE_CHARS = 2000
_MAX_SESSION_LOG_LINES = 200
_DEFAULT_MAX_TURNS = 30

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
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            shell=False,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
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


def preflight_claude_available() -> tuple[str | None, str | None]:
    """Resolve the ``claude`` executable exactly once and return
    ``(resolved_executable, skip_reason)``.

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


def run_structured_claude(worktree: str, prompt: str, timeout_seconds: float,
                           max_turns: int, claude_bin: str = "claude",
                           claude_agent_name: str | None = None) -> tuple[int | None, str, str, bool]:
    argv = [
        claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-turns", str(max_turns),
        "--verbose",
    ]
    # Issue #1734 fix_delta 3 (AC7): purely additive, opt-in persona binding.
    # When ``claude_agent_name`` is provided, insert ``--agent <name>`` so the
    # underlying ``claude`` process actually launches with that Agent as the
    # active session persona (rather than just declaring a static label via
    # ``--agent-type``, which is never forwarded to the CLI). Omitted by
    # default, so every pre-existing caller's argv is unchanged.
    if claude_agent_name:
        argv += ["--agent", claude_agent_name]
    return _run(argv, cwd=worktree, timeout=timeout_seconds, input_text=prompt)


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
) -> list[str]:
    """Drive an isolated-session herdr agent lifecycle. Mutates ``evidence``
    in place (so cleanup/session identity survive even if this raises) and
    returns the bounded, redacted pane output lines."""
    session_name = new_isolated_session_name(herdr_bin)
    evidence["session_name"] = session_name

    agent_name = f"rts-{runtime}-{run_id}"[:32]
    evidence["agent_name"] = agent_name
    pane_output_lines: list[str] = []
    session_proc: subprocess.Popen | None = None
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="worktree-agent-runtime-smoke runner")
    parser.add_argument("--runtime", choices=["claude", "codex"], required=True)
    parser.add_argument("--mode", choices=["structured", "interactive"], required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--max-turns", type=_positive_int, default=_DEFAULT_MAX_TURNS,
                         help="bounded turn count for Claude Code (structured lane only; positive integer)")
    parser.add_argument("--expect-marker", action="append", default=[])
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
    return parser


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

    if args.mode == "interactive":
        skip_reason = preflight_herdr()
        if skip_reason:
            errors.append(skip_reason)
            exit_code = EXIT_SKIP

    if exit_code == EXIT_OK:
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
            resolved_runtime_bin, skip_reason = preflight_claude_available()
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
    # No independent runtime signal of "which persona was effectively
    # active" was found beyond what was requested: neither Claude Code's
    # stream-json ``system``/``result`` events nor Codex's ``--json`` event
    # types echo back a caller-declared agent/persona name (only Codex's
    # rollout-only ``spawn_agent`` calls carry an ``agent_type`` -- for a
    # *sub*-agent spawned *by* this run, not for this run's own identity --
    # see the ``_CODEX_COLLAB_ITEM_TYPE`` note above). Documented finding:
    # effective_agent_type is therefore set equal to requested_agent_type.
    effective_agent_type = requested_agent_type
    loaded_skills = load_static_declared_skills(worktree, requested_agent_type)
    prompt_sha256 = compute_prompt_sha256(prompt)

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
        if exit_code != EXIT_OK:
            # A capability/herdr preflight above already decided this run is
            # a controlled SKIP -- do not attempt to launch either lane.
            # Evidence (schema_summary as built so far, including
            # resolved_executable and the SKIP reason already appended to
            # ``errors``) is still written unconditionally below (Issue
            # #1960 AC7 P1-1 fix-delta).
            pass
        elif args.mode == "structured":
            if args.runtime == "claude":
                rc, out, err, timed_out = run_structured_claude(
                    worktree, prompt, float(args.timeout_seconds), args.max_turns,
                    claude_bin=resolved_runtime_bin,
                    claude_agent_name=args.claude_agent_name,
                )
                capability_decision, capability_reason = classify_claude_structured_outcome(
                    rc, out, err, timed_out
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

            if capability_decision == "capability_skip":
                # AC2: a known unknown/unrecognized-option parser diagnostic
                # -- SKIP 77, never promoted to FAIL. summary.md (written
                # unconditionally below) records runtime_version and
                # capability_error_classification as evidence.
                errors.append(capability_reason)
                exit_code = EXIT_SKIP
            elif timed_out:
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
            elif event_count > 0 and not has_terminal_event(args.runtime, out):
                errors.append("no terminal/result event observed in structured output")
                exit_code = EXIT_FAIL

            if args.expect_marker:
                combined = out + "\n" + err
                missing = [m for m in args.expect_marker if m not in combined]
                schema_summary["expected_markers_missing"] = missing
                if missing:
                    errors.append(f"expected markers not observed: {missing}")
                    exit_code = EXIT_FAIL

            if args.require_session_log_metadata or args.inspect_session_log_metadata:
                metadata_count = count_session_log_metadata(out.splitlines())
                schema_summary["session_log_metadata_count"] = metadata_count
                if args.require_session_log_metadata and metadata_count == 0:
                    errors.append("session-log metadata required but unavailable")
                    exit_code = EXIT_SKIP if exit_code == EXIT_OK else exit_code

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
