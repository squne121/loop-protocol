#!/usr/bin/env python3
"""run_worktree_agent_runtime_smoke.py — cross-runtime worktree agent smoke runner (Issue #1887).

Launches Claude Code inside an identity-verified, linked worktree and
observes either (Issue #2161: the native Codex CLI ``codex`` runtime lane
was retired; only the ``claude`` lane remains):

- ``structured`` lane: a non-interactive process (``claude -p``) whose exit
  code and native structured stdout are the evidence, always run as a
  direct subprocess (never via herdr), or
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

- ``mode=interactive`` never touches or observes a human/pre-existing Herdr
  session. It generates a brand-new, high-entropy named session without
  listing namespaces, runs the lifecycle only inside that session, and tears
  it down via own-name ``herdr session stop`` -> ``herdr session delete`` plus
  launcher-process termination in every controlled exit path (success,
  failure, timeout, SIGINT, SIGTERM). Cleanup whose own-session commands or
  process termination cannot be confirmed overrides an otherwise-successful
  run to FAIL (fail-closed). ``--require-session-baseline-preservation`` is
  the sole explicit opt-in to observe pre-existing-session preservation.
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
import importlib.util
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

import jsonschema
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

# Issue #2161: the requested-mutation-route preflight mechanism (formerly
# backed by these constants) has been deleted -- it was a broken,
# zero-live-caller mechanism after this PR's migration from the deleted
# native Codex CLI agent TOML directory (which declared
# `runtime_followup_route:`) to `.claude/agents/*.md` (which mostly lacks
# that declaration), which made it always return `declared_route_unavailable`
# for any agent other than `web-researcher`. See PR #2409 OWNER
# adversarial review comment.
_REQUIRED_RUNTIME_OBSERVATION_FIELDS = frozenset(
    {"effective_permission_profile", "loaded_skill", "executor", "mutation", "permission_mode"}
)

# Issue #2854: the only ``--require-observed-runtime-field`` value that has a
# native extractor (``extract_claude_subagentstop_permission_mode``). Every
# other field in ``_REQUIRED_RUNTIME_OBSERVATION_FIELDS`` stays unsupported.
_PERMISSION_MODE_FIELD = "permission_mode"
# Closed set listed by the official Claude Code hooks reference.
_PERMISSION_MODE_VALUES = frozenset(
    {"default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"}
)
_OBSERVATION_REASON_NO_SUBAGENTSTOP_EVENT = "no_subagentstop_hook_event"
_OBSERVATION_REASON_FIELD_ABSENT = "field_absent"
_OBSERVATION_REASON_INVALID_OR_CONFLICTING = "invalid_or_conflicting_value"
_OBSERVATION_REASON_NO_NATIVE_EXTRACTOR = "no_native_extractor"

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

# Public integrity values are preserved only by their explicit evidence-field
# paths during serialization. Never exempt a token merely because its text
# happens to look like a Git SHA or a digest.
_PUBLIC_EVIDENCE_SHA_LENGTHS = {
    ("tested_head",): 40,
    ("prompt_sha256",): 64,
    ("resolved_executable_sha256",): 64,
    ("mutation_boundary", "settings_digest_sha256"): 64,
    ("settings_provenance", "digest_sha256"): 64,
    # Issue #2839: ``--approval-profile`` を使った run に限り evidence に載る
    # ``approval_carrier`` の公開 hash (git の commit / blob hash と overlay の sha256)。
    ("approval_carrier", "repo_head"): 40,
    ("approval_carrier", "overlay_sha256"): 64,
    ("approval_carrier", "fixture_git_blob_hash"): 40,
    # Issue #2840: ``--named-subagent-resume`` evidence (public-safe ids/hashes only).
    ("named_subagent_resume", "tested_head"): 40,
    ("named_subagent_resume", "launcher", "sha256"): 64,
    ("named_subagent_resume", "fixtures", "prompt_sha256"): 64,
    ("named_subagent_resume", "fixtures", "compat_note_sha256"): 64,
}

# Issue #2421: ``resolved_executable`` must never persist a raw absolute
# path, regardless of prefix (HOME, /tmp, /opt, /mnt, ...). This is a narrow
# field-semantics special case -- not an extension of ``_SECRET_LIKE_RE`` --
# because the value is an executable path by definition whenever it is a
# string (resolution succeeded); ``None`` (unresolved / SKIP) is unaffected
# since it never reaches the string branch below.
_ALWAYS_REDACT_FIELD_PATHS = {
    ("resolved_executable",): "<redacted>",
}

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
    """Redact every secret-like token in arbitrary text."""
    text = _ANSI_ESCAPE_RE.sub("", text)
    return _SECRET_LIKE_RE.sub("<redacted>", text)


def _is_public_evidence_sha(field_path: tuple[str, ...], value: str) -> bool:
    """Return whether a validated, explicitly-designated evidence field is safe."""
    expected_length = _PUBLIC_EVIDENCE_SHA_LENGTHS.get(field_path)
    return expected_length is not None and bool(
        re.fullmatch(rf"[0-9a-fA-F]{{{expected_length}}}", value)
    )


def _redact_evidence_value(value: object, *, field_path: tuple[str, ...] = ()) -> object:
    """Recursively redact persisted evidence, preserving only named SHA fields."""
    if isinstance(value, str):
        if field_path in _ALWAYS_REDACT_FIELD_PATHS:
            return _ALWAYS_REDACT_FIELD_PATHS[field_path]
        return value if _is_public_evidence_sha(field_path, value) else _redact(value)
    if isinstance(value, list):
        return [_redact_evidence_value(item, field_path=field_path) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_evidence_value(item, field_path=field_path) for item in value)
    if isinstance(value, dict):
        return {
            _redact(key) if isinstance(key, str) else key: _redact_evidence_value(
                item, field_path=field_path + (key,) if isinstance(key, str) else field_path
            )
            for key, item in value.items()
        }
    return value


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
# This is harness-owned invocation-local input.  Keep the peer policy in the
# same overlay as the existing observability hooks so every native lane has one
# exact, auditable policy payload and no global Claude settings are changed.
_CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON = json.dumps({
    "crossSessionInbound": "refuse",
    "permissions": {"deny": ["SendMessage", "ListAgents"]},
    "hooks": {
        "SubagentStart": [{"hooks": [{"type": "command", "command": "cat"}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": "cat"}]}],
    },
})

# Issue #2498 AC4: an ADDITIVE sibling of
# ``_CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON`` above -- a SEPARATE
# constant, never a mutation of the pre-existing one, so every pre-existing
# caller (including the interactive herdr lane construction below and the
# native structured lane's default argv) keeps observing byte-identical
# ``--settings`` JSON (the pinned hook set ``{"SubagentStart",
# "SubagentStop"}`` this repository's own regression test asserts on). This
# sibling additionally registers the native ``UserPromptExpansion`` hook
# (confirmed against a live Claude Code 2.1.261 invocation: a ``command:
# "cat"`` hook echoes back the hook's own stdin payload, which carries
# ``command_name``/``command_args``/``command_source`` fields for a direct
# slash-command/Skill invocation) so ``--expect-skill-command`` has a native
# evidence channel to read from. Used ONLY when the caller passes
# ``--expect-skill-command`` (see ``run_structured_claude``'s
# ``include_user_prompt_expansion_hook`` parameter below) -- every
# pre-existing caller that omits the new flag still gets the unmodified
# constant above.
_CLAUDE_SPAWN_HOOK_OBSERVABILITY_WITH_USER_PROMPT_EXPANSION_SETTINGS_JSON = json.dumps({
    "crossSessionInbound": "refuse",
    "permissions": {"deny": ["SendMessage", "ListAgents"]},
    "hooks": {
        "SubagentStart": [{"hooks": [{"type": "command", "command": "cat"}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": "cat"}]}],
        "UserPromptExpansion": [{"hooks": [{"type": "command", "command": "cat"}]}],
    },
})

# Issue #2840: opt-in ``--named-subagent-resume`` scenario overlay.  A SEPARATE
# constant (never a mutation of the two constants above), so every default
# smoke keeps observing byte-identical ``--settings`` JSON.  Compared with the
# default overlay this scenario drops ONLY the harness's blanket ``SendMessage``
# deny (a name-addressed resume needs ``SendMessage``).  ``ListAgents`` deny,
# ``crossSessionInbound: refuse`` and the normal permission decision flow are
# kept; no permission mode is set here (never a bypass mode).  The hook set is
# GENERIC observation only (``cat`` echoes the hook's own stdin payload so the
# stream-json channel carries it): it never carries a Task Context verdict.
# ``NAMED_SUBAGENT_RESUME_OBSERVATION_HOOKS`` is the single definition shared by
# this overlay and the launcher-owned fixed value
# (``CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-name-resume``); the AC1 tests assert
# both sides carry exactly this set.
NAMED_SUBAGENT_RESUME_OBSERVATION_HOOKS = (
    ("SubagentStart", None),
    ("SubagentStop", None),
    ("PostToolUse", "Agent"),
    ("PreToolUse", "SendMessage"),
)


def _named_subagent_resume_hooks_payload() -> dict:
    hooks: dict = {}
    for event, matcher in NAMED_SUBAGENT_RESUME_OBSERVATION_HOOKS:
        group: dict = {"hooks": [{"type": "command", "command": "cat"}]}
        if matcher is not None:
            group = {"matcher": matcher, **group}
        hooks[event] = [group]
    return hooks


_NAMED_SUBAGENT_RESUME_SETTINGS_JSON = json.dumps({
    "crossSessionInbound": "refuse",
    "permissions": {"deny": ["ListAgents"]},
    "hooks": _named_subagent_resume_hooks_payload(),
})

# Launcher-owned fixed ``CLAUDE_GPT_RUNTIME_SMOKE_HOOKS`` value for this scenario
# (``scripts/claude-gpt/launch.sh``).  The runner only ever sets this one fixed
# string; no caller-supplied JSON / ``--settings`` crosses the launcher boundary.
NAMED_SUBAGENT_RESUME_LAUNCHER_SMOKE_HOOKS = "subagent-name-resume"


# ---------------------------------------------------------------------------
# Issue #2663: generic hook-chain evidence capability -- bounded, CLOSED
# allowlist (never caller-supplied) of exactly one PreToolUse event/tool
# target (AC2's ``all_matching_hooks_observed``) and one Stop-event side
# effect target (AC3's ``sibling_side_effect_inventory_complete``). Neither
# the event, the tool, the side-effect handler, nor the artifact path is
# accepted as a CLI input -- ``--require-hook-chain-evidence`` is a bare
# opt-in switch (Out of Scope: "caller-supplied arbitrary command/path/
# marker/config").
# ---------------------------------------------------------------------------

_HOOK_CHAIN_EVIDENCE_EVENT = "PreToolUse"
_HOOK_CHAIN_EVIDENCE_TOOL = "Bash"
_HOOK_CHAIN_SIDE_EFFECT_EVENT = "Stop"
# Basename-only match against the current project settings' Stop-event
# command hook(s) (Issue #2663 "Current Validated Scope" /
# ``.claude/hooks/session_manifest_coordinator.sh``, Out of Scope for this
# Issue to edit). Never a hardcoded path -- only used to confirm the target
# handler is genuinely CONFIGURED in the tested_head's own
# ``.claude/settings.json`` before asserting anything about its side effect.
_HOOK_CHAIN_SIDE_EFFECT_TARGET_BASENAME = "session_manifest_coordinator.sh"
# ``generate_session_manifest_from_hook.mjs``'s own default
# ``SESSION_MANIFEST_ARTIFACTS_DIR`` (Out of Scope for this Issue to edit).
_HOOK_CHAIN_SIDE_EFFECT_ARTIFACT_RELPATH = ("artifacts", "session-manifest-runtime", "manifests")
# Bounded directory listing -- never an unbounded scan (AC3 "bounded
# location/resource").
_HOOK_CHAIN_SIDE_EFFECT_MAX_SNAPSHOT_ENTRIES = 2000
# ``generate_session_manifest_from_hook.mjs``'s own confirmed-live filename
# contract: ``private-agent-session-manifest-{eventNameLower}-{timestamp}-
# {stableKeySegment}.json`` (see its own source, "Artifact naming" comment).
# This runner's own settings overlay ALSO additively registers a
# ``PostToolUse`` debounced manifest writer that is NOT the AC3 target
# handler (``session_manifest_coordinator.sh``, a ``Stop``-event hook) --
# confirmed live: a single trial session produced BOTH a
# ``...-posttooluse-...json`` and a ``...-stop-...json`` new manifest file
# for the SAME session. Only the ``-stop-`` tagged filename is the target
# handler's OWN side effect; the debounced PostToolUse writer is a
# different, non-target hook and must never be counted toward this
# assertion (positively OR negatively).
_HOOK_CHAIN_SIDE_EFFECT_FILENAME_TOKEN = f"-{_HOOK_CHAIN_SIDE_EFFECT_EVENT.lower()}-"
# A single structured (-p, single session, no subagent) invocation produces
# at most one genuinely NEW, Stop-tagged manifest file; more than that is
# an overflow anomaly, never silently accepted as extra evidence of success.
_HOOK_CHAIN_SIDE_EFFECT_MAX_EXPECTED_NEW_FILES = 1

# The raw hook stdin payload keys this module inspects to positively
# self-identify the runner's OWN additive ``cat`` observer hook responses
# (never the underlying project command hooks, none of which echo their own
# stdin verbatim -- confirmed by reading every hook script named in the
# expected PreToolUse/Stop cohorts). Only presence of these keys is used;
# their VALUES (tool_input, transcript_path, last_assistant_message, ...)
# are never read or persisted, except ``stop_hook_active`` (a bare boolean,
# not prose/content).
_HOOK_CHAIN_SELF_ECHO_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "PreToolUse": ("tool_name", "tool_input", "tool_use_id"),
    "Stop": ("stop_hook_active",),
}


def _read_project_settings(worktree: str) -> dict | None:
    """Read-only parse of the tested_head worktree's OWN
    ``.claude/settings.json`` (Issue #2663 AC2/AC3's expected-cohort /
    target-handler source of truth). Reads the checked-out working-tree
    file directly -- ``verify_worktree_identity`` already guarantees
    ``worktree`` is the real, identity-verified checkout, and the final
    acceptance-evidence HEAD-binding requirement (AC5) is enforced
    elsewhere (``tested_head`` / postcondition fingerprint), not here.
    Returns ``None`` (never a fabricated/empty cohort) on any read or parse
    failure."""
    settings_path = Path(worktree) / ".claude" / "settings.json"
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_command_hook_records_for_event(
    settings: dict, event: str, tool_name: str | None
) -> list[dict]:
    """Detailed form of ``_load_command_hooks_for_event`` (Issue #2865): one
    ``{"command": str, "has_if": bool, "if": <raw value or None>}`` record
    per ``type: "command"`` hook registered under ``hooks[event]``,
    restricted to groups whose ``matcher`` token-set covers ``tool_name``
    (or every group, when ``tool_name`` is ``None`` -- used for matcher-less
    events like ``Stop``). ``has_if`` is ``True`` whenever the hook object
    carries an ``if`` key at all (regardless of its value type), so a
    malformed ``if`` is never silently read as "no condition".

    Issue #2663 Out of Scope: this deliberately mirrors (but does not
    subprocess-execute, and is not imported from)
    ``.claude/hooks/tests/hookchain_harness.py``'s
    ``load_pretool_hook_commands`` read-only settings-parsing logic --
    reimplemented locally, read-only, so this Allowed-Paths-scoped module
    never depends on a file outside its own Allowed Paths, and never
    reuses that harness's SEQUENTIAL SUBPROCESS EXECUTION technique as a
    runtime evidence producer (Out of Scope)."""
    groups = (settings.get("hooks") or {}).get(event) or []
    records: list[dict] = []
    if not isinstance(groups, list):
        return records
    for group in groups:
        if not isinstance(group, dict):
            continue
        if tool_name is not None:
            matcher = group.get("matcher", "")
            matcher_tools = {m.strip() for m in str(matcher).split("|") if m.strip()}
            if matcher_tools and tool_name not in matcher_tools:
                continue
        for hook in group.get("hooks", []) or []:
            if not isinstance(hook, dict) or hook.get("type") != "command":
                continue
            command = hook.get("command")
            if isinstance(command, str):
                records.append({
                    "command": command,
                    "has_if": "if" in hook,
                    "if": hook.get("if"),
                })
    return records


def _load_command_hooks_for_event(
    settings: dict, event: str, tool_name: str | None
) -> list[str]:
    """The ``command`` template string of every ``type: "command"`` hook
    registered under ``hooks[event]`` (see
    ``_load_command_hook_records_for_event`` for the matcher semantics).
    Kept as a ``list[str]`` wrapper for consumers that only need the command
    text (e.g. the Stop-event target-handler configured check)."""
    return [
        record["command"]
        for record in _load_command_hook_records_for_event(settings, event, tool_name)
    ]


# Issue #2865: handler-level ``if`` evaluation for the hook-chain evidence
# expected count. Deliberately NOT a Bash parser / permission-rule
# interpreter: only the exact current-settings form ``Bash(<word> *)`` and a
# conservative "provably a single plain command" check are decided; anything
# else is ``unknown`` and never counted towards a pass.
_HOOK_IF_MATCH = "match"
_HOOK_IF_NONMATCH = "nonmatch"
_HOOK_IF_UNKNOWN = "unknown"
_HOOK_IF_BASH_RULE_RE = re.compile(r"Bash\(([A-Za-z0-9_][A-Za-z0-9_.+-]*) \*\)")
_HOOK_IF_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_./:@%+,=-]*")
_HOOK_IF_BARE_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
_HOOK_IF_SHELL_CONTROL_CHARS = frozenset("&|;<>()`$\\\n\r{}")
_HOOK_IF_QUOTE_CHARS = frozenset("'\"")
# Bash words are separated only by space / tab (newline separates commands).
# Any other Unicode / control whitespace (NBSP, \r, \x0b, \x0c, \x1c-\x1f,
# U+2028, ...) is part of a Bash word, so a command containing one cannot be
# tokenised the way Python's bare ``str.split()`` would; it is ``unknown``.
_HOOK_IF_BASH_SEPARATORS = " \t\n"
_HOOK_IF_SPLIT_RE = re.compile(r"[ \t\n]+")
# Words that run / modify / introduce another command: a command starting
# with one of these is never provably "just this word" (``timeout 5 herdr
# x`` fires a ``herdr *`` rule through the wrapped command), so it is
# ``unknown`` rather than ``nonmatch``.
_HOOK_IF_WRAPPER_WORDS = frozenset({
    "timeout", "time", "nice", "nohup", "stdbuf", "xargs", "env", "command",
    "exec", "sudo", "sh", "bash", "zsh", "eval",
    "doas", "su", "setsid", "ionice", "chrt", "taskset", "flock", "watch",
    "strace", "ltrace", "unbuffer", "parallel", "busybox", "builtin", "source",
    "if", "then", "else", "elif", "fi", "while", "until", "for", "do", "done",
    "case", "esac", "select", "function", "coproc",
})


def _evaluate_hook_if_condition(if_value: object, tool_name: object, command: object) -> str:
    """3-valued evaluation of one hook's handler-level ``if`` condition for
    one ``Bash`` tool call (Issue #2865): ``"match"`` (the hook fires),
    ``"nonmatch"`` (provably does not fire) or ``"unknown"`` (cannot be
    decided here). Pure function of its inputs; the command text is only
    inspected in memory and never returned or logged."""
    if tool_name != "Bash" or not isinstance(if_value, str):
        return _HOOK_IF_UNKNOWN
    rule = _HOOK_IF_BASH_RULE_RE.fullmatch(if_value)
    if rule is None:
        return _HOOK_IF_UNKNOWN
    if not isinstance(command, str) or not command.strip():
        return _HOOK_IF_UNKNOWN
    if any(ch.isspace() and ch not in _HOOK_IF_BASH_SEPARATORS for ch in command):
        return _HOOK_IF_UNKNOWN
    word = rule.group(1)
    tokens = [tok for tok in _HOOK_IF_SPLIT_RE.split(command) if tok]
    index = 0
    while index < len(tokens) and _HOOK_IF_ASSIGNMENT_RE.fullmatch(tokens[index]):
        index += 1
    if index >= len(tokens):
        return _HOOK_IF_UNKNOWN
    first = tokens[index]
    if first == word:
        # ``Bash(<word> *)`` also matches the bare word (no arguments); a
        # compound command whose first subcommand is the word fires too.
        return _HOOK_IF_MATCH
    if (
        any(ch in _HOOK_IF_SHELL_CONTROL_CHARS or ch in _HOOK_IF_QUOTE_CHARS for ch in command)
        or _HOOK_IF_BARE_WORD_RE.fullmatch(first) is None
        or first in _HOOK_IF_WRAPPER_WORDS
    ):
        return _HOOK_IF_UNKNOWN
    return _HOOK_IF_NONMATCH


def _expected_hook_count_for_command(
    hook_records: list[dict], tool_name: str, command: object
) -> int | None:
    """The number of non-observer hooks expected to fire for one tool call,
    or ``None`` when any conditional hook's ``if`` is ``unknown`` (the
    expected count is then NOT a confirmed value; an undecidable hook is
    never added to the count). Hooks without an ``if`` always count."""
    expected = 0
    for record in hook_records:
        if not record["has_if"]:
            expected += 1
            continue
        verdict = _evaluate_hook_if_condition(record["if"], tool_name, command)
        if verdict == _HOOK_IF_MATCH:
            expected += 1
        elif verdict == _HOOK_IF_UNKNOWN:
            return None
    return expected


def _task_context_env_pairs(
    task_context_scope: str | None, task_context_state_root: str | None
) -> list[tuple[str, str]]:
    """Issue #2568 In Scope: purely additive Task Context environment/
    carrier PASSTHROUGH -- this runner never interprets these values (no
    Task Context semantic verdict lives here, see Issue #2568 Out of
    Scope / the ``task_context_runtime_smoke_verifier`` module under
    ``scripts/task-context/`` for that). It only forwards
    ``LOOP_TASK_CONTEXT_SCOPE``/``LOOP_TASK_CONTEXT_STATE_ROOT`` verbatim to
    the launched child runtime process/session when a caller supplies them,
    so a caller can drive an isolated, run-scoped Task Context state root
    through a REAL fresh Native/Claude-GPT runtime. Omitted (empty list)
    when neither is given, so every pre-existing caller's argv/env is
    unchanged.

    Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 1 (atomic carrier
    integrity): the two carrier values are a single atomic pair. Exactly
    one supplied (the other missing/empty) is a caller configuration error
    -- never silently forwarded as a half-carrier, which would let a
    downstream process either derive scope with no isolated root (falling
    through to the canonical DB) or receive a bare state-root override
    with no scope opt-in. Raised here, strictly before every call site's
    own child-process launch (both ``run_structured_claude``'s
    ``subprocess.run`` and ``run_interactive_herdr_isolated``'s ``herdr
    workspace create``), so no process is ever spawned with a
    half-carrier."""
    if bool(task_context_scope) != bool(task_context_state_root):
        raise ValueError(
            "task_context_scope and task_context_state_root must be given "
            "together (both or neither) -- got "
            f"task_context_scope={task_context_scope!r}, "
            f"task_context_state_root={task_context_state_root!r}"
        )
    pairs: list[tuple[str, str]] = []
    if task_context_scope:
        pairs.append(("LOOP_TASK_CONTEXT_SCOPE", task_context_scope))
    if task_context_state_root:
        pairs.append(("LOOP_TASK_CONTEXT_STATE_ROOT", task_context_state_root))
    return pairs


_APPROVAL_CONTRACT_MODULE_NAME = "runtime_vc_approval_contract_for_runner"


def _load_approval_contract():
    """Issue #2839: 兄弟 module ``runtime_vc_approval_contract.py`` を file path で読み込む。

    approval carrier の registry / overlay / precondition の唯一の定義はその module にあり、
    runner はここで読み込んだ定数を使うだけである (二重定義しない)。共有 pytest session で
    同名 module と衝突しないよう、一意な module 名で ``sys.modules`` に登録する。
    """
    module = sys.modules.get(_APPROVAL_CONTRACT_MODULE_NAME)
    if module is not None:
        return module
    path = Path(__file__).resolve().parent / "runtime_vc_approval_contract.py"
    spec = importlib.util.spec_from_file_location(_APPROVAL_CONTRACT_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_APPROVAL_CONTRACT_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def select_native_observation_settings_json(
    *, include_user_prompt_expansion_hook: bool = False,
    include_hook_chain_evidence_hooks: bool = False,
    named_subagent_resume: bool = False,
) -> str:
    """Issue #2839 (PR #2844 OWNER fix_delta): native adapter が子 ``claude -p`` へ渡す固定
    observation overlay (``--settings`` の JSON) を選択して返す唯一の関数。

    ``run_structured_claude()`` と、approval carrier の overlay を組み立てる ``main()`` の
    両方がこの関数を使うため、carrier は runner が実際に選択した overlay (hook-chain 用の
    PreToolUse / Stop を含む) の同じ内容へ ``autoMode`` を足す形で合成される。
    caller 由来の JSON / 文字列は受け取らない。

    Issue #2840: ``named_subagent_resume=True`` は固定 scenario overlay
    (``_NAMED_SUBAGENT_RESUME_SETTINGS_JSON``。blanket ``SendMessage`` deny だけを外す) を
    返す。他の opt-in 観測 overlay とは併用しない単独 scenario である。"""
    if named_subagent_resume:
        if include_user_prompt_expansion_hook or include_hook_chain_evidence_hooks:
            raise ValueError(
                "named_subagent_resume overlay cannot be combined with other observation overlays"
            )
        return _NAMED_SUBAGENT_RESUME_SETTINGS_JSON
    # Issue #2498 AC4: purely additive -- the extended settings JSON
    # (adding a "UserPromptExpansion" hook registration) is used ONLY
    # when the caller opted into ``--expect-skill-command``. Every
    # pre-existing native-adapter caller (``include_user_prompt_
    # expansion_hook`` defaults to ``False``) keeps getting the exact,
    # byte-identical ``_CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON``
    # this repository's own regression test pins to ``{"SubagentStart",
    # "SubagentStop"}``.
    settings_json = (
        _CLAUDE_SPAWN_HOOK_OBSERVABILITY_WITH_USER_PROMPT_EXPANSION_SETTINGS_JSON
        if include_user_prompt_expansion_hook
        else _CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON
    )
    # Issue #2663 AC1/AC2/AC3: purely additive, opt-in observation-only
    # hook registration used ONLY when the caller passes
    # ``--require-hook-chain-evidence``. This does NOT parse from a
    # settings JSON re-derived at call time -- it mutates the SAME fixed
    # constant dict (via json.loads/json.dumps) so every pre-existing
    # caller (``include_hook_chain_evidence_hooks`` defaults to
    # ``False``) keeps getting the exact, byte-identical settings_json
    # selected above. The two ADDED groups are bounded and closed (no
    # caller-supplied command/path/marker/config is ever accepted):
    # - ``PreToolUse`` (matcher "Bash"): an additive ``cat`` observer
    #   hook that echoes its own stdin verbatim, giving
    #   ``evaluate_all_matching_hooks_observed`` a self-identifying
    #   signature (Issue #2663 AC2) to positively exclude the observer
    #   itself from the current-project-settings PreToolUse/Bash cohort
    #   it is comparing against.
    # - ``Stop`` (no matcher): the same additive ``cat`` observer
    #   pattern, giving ``evaluate_sibling_side_effect_inventory``
    #   (Issue #2663 AC3) the real ``stop_hook_active`` boolean the
    #   runtime's own Stop hook payload carries, used ONLY to recognize
    #   the documented valid-no-change condition -- never to read or
    #   persist the surrounding raw hook payload (e.g.
    #   ``last_assistant_message``, ``transcript_path``).
    if include_hook_chain_evidence_hooks:
        settings_obj = json.loads(settings_json)
        hooks_obj = settings_obj.setdefault("hooks", {})
        hooks_obj["PreToolUse"] = [
            {
                "matcher": _HOOK_CHAIN_EVIDENCE_TOOL,
                "hooks": [{"type": "command", "command": "cat"}],
            }
        ]
        hooks_obj["Stop"] = [{"hooks": [{"type": "command", "command": "cat"}]}]
        settings_json = json.dumps(settings_obj)
    return settings_json


def run_structured_claude(worktree: str, prompt: str, timeout_seconds: float,
                           max_turns: int, claude_bin: str = "claude",
                           claude_agent_name: str | None = None,
                           hermetic_agents_file: str | None = None,
                           hermetic_settings_file: str | None = None,
                           claude_adapter: str = "native",
                           include_user_prompt_expansion_hook: bool = False,
                           include_hook_chain_evidence_hooks: bool = False,
                           task_context_scope: str | None = None,
                           task_context_state_root: str | None = None,
                           approval_settings_json: str | None = None,
                           approval_child_env: dict[str, str] | None = None,
                           named_subagent_resume: bool = False,
                           append_system_prompt_file: str | None = None,
                           ) -> tuple[int | None, str, str, bool]:
    """Issue #2839: ``approval_settings_json`` / ``approval_child_env`` は
    ``--approval-profile`` 使用時にだけ main() が渡す、registry 由来の固定 overlay と、
    検証済みの値で明示的に組み立てた子 env である。どちらも既定 ``None`` で、未指定の
    呼び出しの argv と env は変更前と byte-identical に保たれる。

    Issue #2174 AC1 fix_delta (OWNER REQUEST_CHANGES
    https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173):
    ``claude_adapter`` is the ONLY input that decides launcher-specific argv
    shape / env-var injection -- never ``bool(claude_bin)`` alone (the prior
    ``claude_bin_is_override`` parameter conflated "a --claude-bin path was
    given" with "apply claude-gpt launcher protocol", which silently forced
    claude-gpt argv/env handling onto ANY --claude-bin override, including a
    plain absolute-path native claude binary or a transparent wrapper).
    ``"native"`` (default): --claude-bin (if any) is a pure binary-path
    override, argv is byte-identical to the PATH-resolved case (AC6).
    ``"claude-gpt"``: --claude-bin must point at
    scripts/claude-gpt/launch.sh; that launcher's own CLI contract is
    honored (see below)."""
    argv = [claude_bin]
    if claude_adapter == "claude-gpt":
        # Issue #2176 (live AC3 finding): ``scripts/claude-gpt/launch.sh``
        # only accepts its own launcher options (``--claude-bin``,
        # ``--check-only``, ``--dry-run``) before a literal ``--``
        # separator; any other ``-*`` token there is rejected as
        # ``unknown_launcher_option`` (confirmed against the launcher
        # committed at Issue #2158 / PR #2162's worktree HEAD). Everything
        # after ``--`` is forwarded to the underlying claude binary
        # unparsed. The 'native' adapter never receives this separator, so
        # its argv shape is unchanged.
        argv.append("--")
    # Issue #2840: a name-addressed ``SendMessage`` resume re-reads the child's
    # persisted transcript, so the opt-in named SubAgent resume scenario (and
    # ONLY that scenario) must not pass ``--no-session-persistence``
    # (confirmed live: with it, ``SendMessage`` fails with "No transcript found
    # for agent ID").  Every other caller keeps the exact default argv.
    argv += [
        "-p",
        "--output-format", "stream-json",
        "--include-hook-events",
        *([] if named_subagent_resume else ["--no-session-persistence"]),
        "--max-turns", str(max_turns),
        "--verbose",
    ]
    if append_system_prompt_file and not named_subagent_resume:
        raise ValueError("append_system_prompt_file is only supported by the named SubAgent resume scenario")
    # Issue #2840: invocation-local compatibility note.  Passed through the CLI's
    # own per-invocation capability (never a permanent system-prompt injection).
    if append_system_prompt_file:
        argv += ["--append-system-prompt-file", append_system_prompt_file]
    # Issue #2176: a launcher wrapper pinned via ``--claude-bin`` (e.g.
    # ``scripts/claude-gpt/launch.sh``) rejects any ``--settings`` CLI flag
    # outright as a policy-weakening extra flag
    # (``CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS``), so unconditionally appending
    # the fixed SubagentStart/SubagentStop observability
    # ``--settings <JSON>`` flag here (as done for the native ``claude``
    # binary below) would make every structured-lane launcher invocation a
    # deterministic BLOCKED. Instead, when the caller explicitly opted into
    # ``--claude-adapter claude-gpt``, request the same fixed hook pair
    # through a narrow, value-fixed environment variable
    # (``CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop``) that the
    # launcher itself interprets and materializes into its own
    # launcher-managed settings file -- no caller-supplied JSON ever
    # crosses the launcher's forbidden-flags boundary. For the ``native``
    # adapter (default, regardless of whether --claude-bin was given), this
    # branch is not taken and argv keeps the pre-existing fixed
    # ``--settings <JSON>`` flag unchanged (AC6 backward compatibility).
    launch_env = None
    if claude_adapter == "claude-gpt":
        if approval_settings_json is not None or approval_child_env is not None:
            # Issue #2839: launcher は ``--settings`` を policy-weakening flag として拒否する
            # ため、carrier は native adapter 専用である (呼び出し側の precondition の二重防御)。
            raise ValueError("approval carrier requires the native adapter")
        launch_env = os.environ.copy()
        launch_env["CLAUDE_GPT_RUNTIME_SMOKE_HOOKS"] = (
            NAMED_SUBAGENT_RESUME_LAUNCHER_SMOKE_HOOKS
            if named_subagent_resume
            else "subagent-start-stop"
        )
    else:
        settings_json = select_native_observation_settings_json(
            include_user_prompt_expansion_hook=include_user_prompt_expansion_hook,
            include_hook_chain_evidence_hooks=include_hook_chain_evidence_hooks,
            named_subagent_resume=named_subagent_resume,
        )
        if approval_settings_json is not None:
            # Issue #2839: carrier の overlay は、上で選択した観測 overlay と同じ内容に
            # ``autoMode`` だけを足したものでなければならない。それ以外 (観測 hooks の欠落や
            # 任意 key の混入) は fail-closed で拒否し、``--settings`` は常に 1 個だけ渡す。
            approval_obj = json.loads(approval_settings_json)
            if (
                not isinstance(approval_obj, dict)
                or approval_obj.pop("autoMode", None) is None
                or approval_obj != json.loads(settings_json)
            ):
                raise ValueError(
                    "approval overlay must equal the selected observation overlay plus autoMode"
                )
            settings_json = approval_settings_json
        if approval_child_env is not None:
            launch_env = dict(approval_child_env)
        argv += ["--settings", settings_json]
        if include_hook_chain_evidence_hooks:
            # Issue #2663 AC2 live-trial fix, corrected by PR #2668
            # fix_delta (P1-1, anchor review
            # https://github.com/squne121/loop-protocol/pull/2668#issuecomment-5737957277):
            # the expected cohort is explicitly defined as "current PROJECT
            # settings (.claude/settings.json)". Claude Code's own
            # ``--setting-sources`` flag (per the official CLI reference,
            # https://code.claude.com/docs/en/cli-reference: "Comma-
            # separated list of setting sources to load (user, project,
            # local)") controls ONLY which SETTINGS FILES are read to
            # assemble a session's static hook configuration -- it does
            # NOT, and cannot, exclude hooks registered by managed policy,
            # plugins, or Skills, which register independently of
            # ``--setting-sources`` (an earlier revision of this comment
            # incorrectly claimed a broader "user/local/managed" exclusion;
            # corrected here). Fixing this to ``"project"`` (never caller-
            # configurable) is confirmed live to exclude the user/local
            # settings.json FILE sources specifically -- e.g. it prevents a
            # host's own ``~/.claude/settings.json`` PreToolUse/Bash hook
            # from leaking into the observed cohort. Any managed/plugin/
            # Skill-registered hook sharing the same event+tool remains
            # observationally indistinguishable from a genuine unknown/
            # duplicate entry on this channel regardless of this flag, and
            # is handled the same way everywhere else in this module:
            # ``unattributable_extra_hook_execution`` (unverified, never
            # silently promoted to pass or fail) -- see the "Confirmed
            # runtime-capability boundary" comment above
            # ``_hook_chain_self_echo_fields`` for the full handler-
            # identity limitation this flag does NOT resolve.
            # ``--settings <JSON>`` (this runner's own additive observer
            # overlay, appended above) is a SEPARATE mechanism from the
            # ``--setting-sources`` file-source allowlist and is confirmed
            # live to still apply even when only "project" is loaded.
            argv += ["--setting-sources", "project"]
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
    # Issue #2568 In Scope: additive Task Context env/carrier passthrough,
    # applied last so it never disturbs any argv construction above. Only
    # takes effect when the caller opts in.
    task_context_pairs = _task_context_env_pairs(task_context_scope, task_context_state_root)
    if task_context_pairs:
        if launch_env is None:
            launch_env = os.environ.copy()
        for key, value in task_context_pairs:
            launch_env[key] = value
    return _run(argv, cwd=worktree, timeout=timeout_seconds, input_text=prompt, env=launch_env)


_CLAUDE_GPT_LAUNCH_RESULT_RE = re.compile(
    r'\{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1"[^\n]*\}'
)


def extract_claude_gpt_launcher_receipt(stderr: str) -> dict | None:
    """Best-effort extraction of ``scripts/claude-gpt/launch.sh``'s own
    ``CLAUDE_GPT_LAUNCH_RESULT_V1`` JSON receipt line from ``stderr`` (Issue
    #2174 AC8). This runner does not implement or duplicate the launcher's
    forbidden-flag policy -- it only surfaces the launcher's own,
    already-structured refusal/success receipt as evidence, so a matrix
    combination that structurally fails (e.g. claude-gpt adapter + hermetic
    --settings forwarding) is independently observable rather than silently
    swallowed as an opaque non-zero exit. Returns ``None`` when no such
    receipt line is present (e.g. native adapter, or a run that never
    reached the point of emitting one)."""
    match = _CLAUDE_GPT_LAUNCH_RESULT_RE.search(stderr or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Issue #2219 AC1/AC7: claude-gpt launcher proxy stderr side-channel parsing
# and INDEPENDENT proxy cleanup re-confirmation.
#
# ``scripts/claude-gpt/launch.sh`` (Out of Scope for this Issue -- confirmed
# by prior investigation to already emit everything needed) writes fixed
# ``KEY=value`` lines to stderr around proxy startup
# (``CLAUDE_GPT_PROXY_PORT``/``_LOG``/``_PID``, ``launch.sh:420-424``) and
# cleanup (``CLAUDE_GPT_PROXY_CLEANUP_OK``/``CLAUDE_GPT_CLAUDE_EXIT_CODE``,
# ``launch.sh:548-551``). This runner only PARSES those already-emitted
# lines; it never re-implements or duplicates the launcher's own proxy
# lifecycle management.
# ---------------------------------------------------------------------------


def extract_claude_gpt_proxy_sidechannel(stderr: str) -> dict:
    """Parse the claude-gpt launcher's stderr ``KEY=value`` proxy
    side-channel lines (Issue #2219 AC1/AC7).

    Returns ``{"proxy_port": int|None, "proxy_log": str|None, "proxy_pid":
    int|None, "proxy_cleanup_ok_self_reported": bool|None,
    "claude_exit_code_self_reported": int|None}``. Every field fails
    closed to ``None`` when its line is absent or malformed -- never
    guessed."""
    result: dict = {
        "proxy_port": None,
        "proxy_log": None,
        "proxy_pid": None,
        "proxy_cleanup_ok_self_reported": None,
        "claude_exit_code_self_reported": None,
    }
    for line in (stderr or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "CLAUDE_GPT_PROXY_PORT" and value.isdigit():
            result["proxy_port"] = int(value)
        elif key == "CLAUDE_GPT_PROXY_LOG" and value:
            result["proxy_log"] = value
        elif key == "CLAUDE_GPT_PROXY_PID" and value.isdigit():
            result["proxy_pid"] = int(value)
        elif key == "CLAUDE_GPT_PROXY_CLEANUP_OK":
            if value == "true":
                result["proxy_cleanup_ok_self_reported"] = True
            elif value == "false":
                result["proxy_cleanup_ok_self_reported"] = False
        elif key == "CLAUDE_GPT_CLAUDE_EXIT_CODE" and value.lstrip("-").isdigit():
            result["claude_exit_code_self_reported"] = int(value)
    return result


def _claude_gpt_proxy_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _claude_gpt_proxy_port_listening(port: int) -> bool:
    ss = shutil.which("ss")
    if ss is None:
        return False
    rc, out, _err, timed_out = _run([ss, "-ltn"], timeout=10.0)
    if timed_out or rc != 0:
        return False
    needle = f":{port} "
    return any(needle in line or line.rstrip().endswith(f":{port}") for line in out.splitlines())


def verify_claude_gpt_proxy_cleanup_independent(
    proxy_pid: int | None, proxy_port: int | None, *,
    max_attempts: int = 3, sleep_seconds: float = 0.5,
) -> dict:
    """Bounded poll-with-retry, INDEPENDENT re-confirmation that the
    claude-gpt launcher's proxy process/port are actually gone (Issue
    #2219 AC7) -- never trusting
    ``extract_claude_gpt_proxy_sidechannel``'s
    ``proxy_cleanup_ok_self_reported`` value. Checks ``kill(pid, 0)``
    (process liveness) and ``ss -ltn`` (listen-socket presence) directly
    from THIS process, independent of the launcher's own already-printed
    verdict.

    When both ``proxy_pid`` and ``proxy_port`` are ``None`` (nothing to
    check -- e.g. a non-claude-gpt-adapter run), returns ``checked:
    False``, ``cleanup_confirmed: None``: absence of evidence is never
    asserted as a pass.

    Returns ``{"checked": bool, "pid_alive": bool|None, "port_listening":
    bool|None, "cleanup_confirmed": bool|None, "attempts": int}``.
    ``cleanup_confirmed`` is ``True`` only once BOTH applicable checks
    report clean on the SAME attempt within ``max_attempts`` bounded
    retries."""
    if proxy_pid is None and proxy_port is None:
        return {
            "checked": False, "pid_alive": None, "port_listening": None,
            "cleanup_confirmed": None, "attempts": 0,
        }
    attempts = 0
    pid_alive: bool | None = None
    port_listening: bool | None = None
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        pid_alive = _claude_gpt_proxy_pid_alive(proxy_pid) if proxy_pid is not None else None
        port_listening = _claude_gpt_proxy_port_listening(proxy_port) if proxy_port is not None else None
        if not pid_alive and not port_listening:
            return {
                "checked": True, "pid_alive": pid_alive, "port_listening": port_listening,
                "cleanup_confirmed": True, "attempts": attempts,
            }
        if attempt < max_attempts:
            time.sleep(sleep_seconds)
    return {
        "checked": True, "pid_alive": pid_alive, "port_listening": port_listening,
        "cleanup_confirmed": False, "attempts": attempts,
    }


def extract_claude_resolved_executable_sha256(resolved_executable: str | None) -> str | None:
    """sha256 of the resolved launcher/executable FILE CONTENT (Issue #2219
    AC1), binding evidence to the exact binary bytes actually invoked --
    not merely its path (a path can be repointed by a symlink swap between
    preflight and execution without a path-only claim changing). Returns
    ``None`` when ``resolved_executable`` is ``None`` or the file cannot be
    read (fails closed -- never a guessed/partial digest)."""
    if not resolved_executable:
        return None
    try:
        with open(resolved_executable, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def verify_evidence_not_stale(
    evidence_tested_head: str | None,
    evidence_repo_fingerprint: dict | None,
    fresh_worktree_head: str | None,
    fresh_repo_fingerprint: dict | None,
) -> dict:
    """Explicit rejection guard for stale-head evidence reuse (Issue #2219
    AC10): a previously-written evidence JSON's ``tested_head`` /
    ``repo_fingerprint`` must match a FRESH worktree HEAD/fingerprint taken
    at verification time, or it must be rejected as PASS-ineligible --
    never silently re-accepted just because it once passed.

    Returns ``{"stale": bool, "reason": str|None}``. ``stale=True``
    whenever either the head SHA or the fingerprint dict differs (or the
    fresh head/fingerprint itself could not be captured -- an unverifiable
    freshness claim is treated as stale, fail-closed)."""
    if not fresh_worktree_head:
        return {"stale": True, "reason": "fresh_worktree_head_unavailable"}
    if not evidence_tested_head:
        return {"stale": True, "reason": "evidence_tested_head_missing"}
    if evidence_tested_head != fresh_worktree_head:
        return {"stale": True, "reason": "tested_head_mismatch"}
    if evidence_repo_fingerprint != fresh_repo_fingerprint:
        return {"stale": True, "reason": "repo_fingerprint_mismatch"}
    return {"stale": False, "reason": None}


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


def extract_claude_permission_denials(stdout: str | None) -> list:
    """Issue #1881 PR #2385 fix_delta (Extension 3): the underlying
    ``claude`` CLI's own final ``type: "result"`` stream-json event already
    carries a top-level ``permission_denials`` array -- populated by Claude
    Code itself whenever a ``PreToolUse`` hook denies a tool call before it
    runs -- that this runner has never surfaced in its own evidence output.
    Purely additive: this surfaces Claude Code's own existing structured
    field verbatim (already redaction-safe -- ``tool_name``/``tool_use_id``/
    ``tool_input``, the same shape Claude Code itself returns; never a new
    mechanism). Returns ``[]`` (never ``None``, matching this module's other
    list-typed evidence fields, e.g. ``spawn_events``) when the final result
    event carries no such array, or is absent altogether -- never
    fabricated."""
    if not stdout:
        return []
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "result":
            continue
        denials = payload.get("permission_denials")
        if isinstance(denials, list):
            return denials
    return []


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

# Issue #2161 (native Codex CLI retirement): the ``_CODEX_COLLAB_ITEM_TYPE``
# constant and its native Codex CLI sub-agent dispatch detection notes were
# removed along with the ``codex`` runtime lane.

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


def count_direct_web_tool_events(runtime: str, stdout: str) -> int:
    """Issue #1886 P0-1 fix_delta (PR #2005 adversarial review): AC8
    requires ``direct_fallback_invocation_count`` to reflect an ACTUAL
    native-event-derived observation, never a permanently hard-coded 0.
    Claude Code's native ``stream-json`` events unambiguously name direct
    web tools (``WebSearch`` / ``WebFetch``) as ``tool_use`` blocks, exactly
    like the existing ``Agent``/``Bash`` classification above -- counted
    precisely.

    Issue #2161 (native Codex CLI retirement): the Codex-only best-effort
    token-scan branch (``_CODEX_DIRECT_WEB_TOKEN_RE``) was removed along
    with the ``codex`` runtime; ``runtime`` is always ``"claude"`` now."""
    count = 0
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
#
# Issue #2161 (native Codex CLI retirement): the parallel Codex CLI
# ``spawn_agent`` rollout-log extraction note was removed along with the
# ``codex`` runtime lane.
#
# The Claude extractor is best-effort and fails closed to ``None`` on any error
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

# Issue #1881 PR #2385 fix_delta (Extension 1): provenance label for a
# ``SessionStart`` hook stdout/output text that carries a plain-text
# ``agent_type=<value>`` marker (e.g. ``.claude/hooks/pr_reviewer_guard.py``'s
# opt-in ``observe-identity`` probe channel, which emits
# ``reviewer-identity-observed agent_type=<value>``) rather than an embedded
# JSON object. Kept distinct from ``AGENT_TYPE_SOURCE_HOOK_PAYLOAD`` so a
# consumer can tell the two recognition paths apart if it ever needs to.
AGENT_TYPE_SOURCE_PLAIN_MARKER = "plain_marker"

# Matches a plain-text ``agent_type=<value>`` marker embedded anywhere in a
# SessionStart hook's stdout/output text (Issue #1881 Extension 1). Only
# tried as a FALLBACK after ``_parse_embedded_json_object`` finds no JSON
# object on that same text -- the pre-existing JSON-object recognition path
# is untouched and stays byte-identical for any caller relying on it.
_PLAIN_AGENT_TYPE_MARKER_RE = re.compile(r"agent_type=([a-zA-Z0-9_-]+)")

# ``child_spawn_launch_mode`` values (Issue #2021 AC7).
SPAWN_LAUNCH_MODE_ASYNC = "async_launched"
SPAWN_LAUNCH_MODE_COMPLETED = "completed"
SPAWN_LAUNCH_MODE_UNKNOWN = None

# Issue #2219 fix_delta iteration 2: ``_find_claude_interactive_transcript``
# scans this many leading lines of a candidate transcript for a ``cwd``
# field before giving up on that line-window (a real persisted transcript's
# first ``cwd``-bearing record is typically the 3rd-4th line, not the
# first -- see that function's own docstring for the live evidence).
_TRANSCRIPT_CWD_SCAN_LINES = 50

# Issue #2219 fix_delta iteration 2 (live verification finding): an ASYNC
# spawn's own ``tool_use_result`` never transitions to ``status: "completed"``
# in place -- live inspection of a real claude-gpt interactive session
# transcript found its completion notification arrives later, as a SEPARATE
# ``queue-operation``/``queued_command`` record whose ``content``/``prompt``
# string embeds a ``<task-notification>`` block with this exact
# ``<task-id>...</task-id>`` / ``<status>completed</status>`` shape. This is
# Claude Code's own async Task-dispatch notification protocol (observed
# identically for the structured lane's single-child async path too, not an
# adapter-specific quirk), so it is read generically -- not gated on
# ``claude_adapter``.
_CLAUDE_TASK_NOTIFICATION_RE = re.compile(
    r"<task-id>([0-9a-zA-Z_-]+)</task-id>.*?<status>([a-z_]+)</status>",
    re.DOTALL,
)


def extract_claude_task_notification_completions(text: str) -> set[str]:
    """Agent ids whose async ``<task-notification>`` block (see
    ``_CLAUDE_TASK_NOTIFICATION_RE`` docstring above) reports
    ``<status>completed</status>`` anywhere in ``text``. This is a SEPARATE
    completion channel from ``tool_use_result.status == "completed"``
    (the synchronous-completion shape) -- an async-launched spawn's
    ``tool_use_result`` instead reports ``status: "async_launched"``
    (``SPAWN_LAUNCH_MODE_ASYNC``) and never updates in place; the actual
    completion signal for that child arrives later as one of these
    notification blocks. Scans the RAW text directly (not per-event JSON
    fields) since the notification payload is itself embedded as escaped
    text inside a JSON string value, not a top-level structured field.
    Best-effort / fail-closed: returns an empty set, never a guess, on any
    text that does not contain a matching block."""
    completed: set[str] = set()
    for match in _CLAUDE_TASK_NOTIFICATION_RE.finditer(text):
        agent_id, status = match.group(1), match.group(2)
        if status == SPAWN_LAUNCH_MODE_COMPLETED:
            completed.add(agent_id)
    return completed


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


_STREAM_DECODED_STRING_MAX_DEPTH = 4


def _collect_decoded_json_strings(value: object, parts: list[str], depth: int = 0) -> None:
    """Issue #2923: collect every JSON string VALUE reachable from ``value``
    (already JSON-decoded, i.e. ``\\"`` is a plain ``"``).  A string value that
    itself parses to a JSON object (a hook event echoes its own payload as a
    JSON-encoded string, e.g. ``SubagentStop`` with ``last_assistant_message``)
    is additionally descended into, bounded by ``_STREAM_DECODED_STRING_MAX_DEPTH``."""
    if isinstance(value, str):
        parts.append(value)
        if depth < _STREAM_DECODED_STRING_MAX_DEPTH and value.lstrip().startswith("{"):
            try:
                nested = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return
            if isinstance(nested, dict):
                _collect_decoded_json_strings(nested, parts, depth + 1)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_decoded_json_strings(item, parts, depth)
    elif isinstance(value, list):
        for item in value:
            _collect_decoded_json_strings(item, parts, depth)


def extract_claude_stream_decoded_text(stdout: str) -> str:
    """Issue #2923: the JSON-UNESCAPED text of every string value in every
    stream-json event of ``stdout`` (newline-joined).

    In raw stream-json a ``"`` inside model text appears as ``\\"``, so a
    ``--expect-marker`` literal containing a double quote never matches the raw
    stdout even when the child produced it.  ``_marker_provenance_verified``
    already matches against the child's unescaped text; this gives the
    ``--expect-marker-source subagent`` missing-check the same representation.
    It is used ONLY to widen that missing-check -- provenance still requires the
    marker in the correlated child's own text, never merely anywhere in the stream."""
    parts: list[str] = []
    for payload in _iter_claude_stream_events(stdout):
        _collect_decoded_json_strings(payload, parts)
    return "\n".join(parts)


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


# ---------------------------------------------------------------------------
# Issue #2498: ``skill-invocation-runtime-smoke`` profile assertion
# evaluators (``procedure_steps_executed_in_declared_order`` /
# ``output_contract_schema_fields_present``), plus the native
# ``UserPromptExpansion`` evidence channel used by ``--expect-skill-command``
# (AC1/AC2/AC4). This runner does NOT interpret arbitrary SKILL.md Markdown
# Procedure text -- the caller supplies the ordered marker list / schema path
# explicitly; these functions only match that caller-supplied contract
# against already-captured native evidence.
# ---------------------------------------------------------------------------


def evaluate_ordered_evidence_match(native_evidence_text: str, expected_ordered_markers: list) -> dict:
    """``procedure_steps_executed_in_declared_order`` assertion evaluator
    (Issue #2498 AC1): a caller-supplied ORDERED list of expected evidence
    markers is matched, in order, against ``native_evidence_text`` (the
    structured lane's own captured stdout -- the native stream-json event
    text, already in emission order; never stderr, whose write ordering
    relative to stdout is not reliably interleaved).

    This is a literal, sequential subsequence match -- never a generic
    Markdown Procedure interpreter: each marker in
    ``expected_ordered_markers`` must occur as a literal substring at or
    after the position immediately following the previous marker's match
    (``str.find(marker, cursor)``). This naturally tolerates a repeated
    marker appearing more than once (each subsequent occurrence is searched
    for strictly after the previous one), and fails closed (missing) for
    both an absent marker and a marker that is present only BEFORE its
    predecessor in the given order (out-of-order evidence).

    Returns ``{"verified": bool, "expected_order": list[str],
    "observed_positions": {marker: int}, "missing_markers": list[str]}``.
    ``verified`` is ``True`` iff every marker was found in order (an empty
    ``expected_ordered_markers`` trivially verifies)."""
    observed_positions: dict = {}
    missing_markers: list = []
    cursor = 0
    for marker in expected_ordered_markers:
        idx = native_evidence_text.find(marker, cursor)
        if idx < 0:
            missing_markers.append(marker)
            continue
        observed_positions[marker] = idx
        cursor = idx + len(marker)
    return {
        "verified": not missing_markers,
        "expected_order": list(expected_ordered_markers),
        "observed_positions": observed_positions,
        "missing_markers": missing_markers,
    }


def extract_claude_final_result_text(stdout: str) -> str | None:
    """The final ``type: "result"`` stream-json event's own ``result``
    field -- Claude Code's own final text response (confirmed against a
    live Claude Code 2.1.261 invocation: this field carries the exact text
    of the model's last assistant message). ``None`` (never fabricated)
    when no ``result`` event was observed or it carries no string
    ``result`` field."""
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "result":
            continue
        text = payload.get("result")
        if isinstance(text, str):
            return text
    return None


def extract_claude_main_output_text(stdout: str) -> str:
    """The single, authoritative "main" (non-subagent, non-hook-echo)
    output text channel (PR #2500 fix_delta for Issue #2498, OWNER
    REQUEST_CHANGES
    https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805,
    findings P1-1/P1-2): every ``type: "assistant"`` stream-json event's
    own ``message.content[].text`` block, IN EMISSION ORDER, joined with a
    newline.

    Two things are deliberately EXCLUDED, both load-bearing:

    * Any ``type: "system"`` hook lifecycle event (e.g.
      ``UserPromptExpansion``'s own stdin echo of the caller's
      prompt/command_args/command text). Including those would let a
      marker or ordered-marker the CALLER merely wrote into the prompt
      satisfy a ``--expect-marker-source main`` / ``--expect-ordered-
      marker`` assertion the model itself never produced (P1-1).
    * The terminal ``type: "result"`` event's own ``result`` field.
      ``extract_claude_final_result_text``'s docstring already documents
      (confirmed against a live Claude Code 2.1.261 invocation) that this
      field REPLAYS the exact text of the last assistant message verbatim
      -- i.e. it is not independent evidence, it is the SAME content as
      the last entry already included from the loop above. Including it
      too would double-count that one final answer as two separate
      occurrences, which is exactly what let a reverse-order single
      answer masquerade as forward-order evidence in
      ``evaluate_ordered_evidence_match`` (P1-2): a cursor-based
      sequential scan could find an early marker in the assistant event's
      own text and a later marker only in the result event's replay of
      that SAME text, incorrectly reporting forward order for content
      that was actually reversed within one single answer.

    Used as the canonical evidence text for BOTH the ``--expect-marker-
    source main`` marker search (P1-1) and the ``--expect-ordered-marker``
    match (P1-2), so the two concerns share one definition of "the main
    output" rather than drifting independently."""
    texts: list[str] = []
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "assistant":
            continue
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return "\n".join(texts)


_FENCED_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def _extract_final_output_json_object(text: str) -> dict | None:
    """Deterministic final-output JSON extraction (PR #2500 fix_delta for
    Issue #2498, finding P2-2) -- a DISTINCT extraction path from
    ``_parse_embedded_json_object`` (which remains reserved for the
    prefix-tolerant hook-payload use case; it is unchanged).

    Accepts, in this FIXED priority order only (never multiple candidates
    scored/picked by whichever validates against the schema -- that
    ambiguous approach is explicitly out of scope):

    1. the entire trimmed ``text`` is itself a bare JSON object, or
    2. ``text`` contains EXACTLY ONE ```json fenced Markdown code block;
       that block's own content is parsed as JSON.

    Returns ``None`` (extraction failure, never a guess) when neither
    applies -- including when the text is empty, not an object, or
    contains zero or more-than-one fenced ```json block."""
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    fenced_blocks = _FENCED_JSON_BLOCK_RE.findall(text)
    if len(fenced_blocks) == 1:
        try:
            parsed = json.loads(fenced_blocks[0].strip())
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def evaluate_output_contract_schema_fields_present(stdout: str, schema_path: str) -> dict:
    """``output_contract_schema_fields_present`` assertion evaluator (Issue
    #2498 AC2): full ``jsonschema.validate()`` validation of a domain output
    payload against a SINGLE, caller-supplied, pre-existing canonical JSON
    Schema file (e.g. ``.claude/skills/review-issue/schemas/
    review_issue_result_v1.json``) -- never a new required-key-existence-only
    checker, and never a generic multi-domain schema registry (the schema
    path is caller-supplied every time, not looked up from any table this
    runner owns).

    The domain output payload itself is recovered from the structured
    lane's own final assistant response text (``extract_claude_final_result_
    text``) via a DETERMINISTIC final-output JSON extraction
    (``_extract_final_output_json_object`` -- PR #2500 fix_delta P2-2:
    bare JSON object or a single fenced ```json block only, never the
    prefix-tolerant ``_parse_embedded_json_object`` used for hook
    payloads) -- never guessed, never a self-report.

    Returns ``{"verified": bool, "schema_path": str,
    "output_payload_found": bool, "error": str | None}``."""
    result_text = extract_claude_final_result_text(stdout)
    if result_text is None:
        return {
            "verified": False,
            "schema_path": schema_path,
            "output_payload_found": False,
            "error": "no final result text observed in native evidence",
        }
    payload = _extract_final_output_json_object(result_text)
    if payload is None:
        return {
            "verified": False,
            "schema_path": schema_path,
            "output_payload_found": False,
            "error": "final result text did not contain a parseable JSON object",
        }
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "verified": False,
            "schema_path": schema_path,
            "output_payload_found": True,
            "error": f"schema load failed: {exc}",
        }
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except (jsonschema.exceptions.ValidationError, jsonschema.exceptions.SchemaError) as exc:
        # PR #2500 fix_delta P2-3 (defense in depth): the primary guard is
        # the pre-launch --output-schema-path meta-schema check in main()
        # (before any runtime subprocess is spawned) -- this narrow catch
        # only prevents an unhandled SchemaError here too, it never
        # replaces that early gate.
        return {
            "verified": False,
            "schema_path": schema_path,
            "output_payload_found": True,
            "error": _redact(str(exc))[:2000],
        }
    return {
        "verified": True,
        "schema_path": schema_path,
        "output_payload_found": True,
        "error": None,
    }


_CLAUDE_USER_PROMPT_EXPANSION_HOOK_EVENT = "UserPromptExpansion"


def extract_claude_user_prompt_expansion_command_names(stdout: str) -> list:
    """Every ``command_name`` observed on a native ``UserPromptExpansion``
    hook lifecycle event, IN ORDER (Issue #2498 AC4). Confirmed against a
    live Claude Code 2.1.261 invocation: when a ``UserPromptExpansion`` hook
    is registered (see ``_CLAUDE_SPAWN_HOOK_OBSERVABILITY_WITH_USER_PROMPT_
    EXPANSION_SETTINGS_JSON``), its ``command:"cat"`` handler echoes back the
    hook's own stdin payload -- carrying ``command_name`` / ``command_args``
    / ``command_source`` -- on both the ``stdout``/``output`` fields of the
    resulting ``hook_response`` event, exactly like the pre-existing
    ``SubagentStart``/``SubagentStop`` hook parsing above.

    Per Issue #2498's own research (official docs do not enumerate
    ``command_source``'s value domain, e.g. whether it uniquely identifies a
    "main session direct invocation" vs. some other provenance), only
    ``command_name`` is read here -- ``command_source`` is deliberately never
    consulted as a pass/fail signal.

    A hook event whose ``stdout``/``output`` channels both parse but
    DISAGREE on ``command_name`` is treated the same as the pre-existing
    hook lifecycle parsing above: untrustworthy, and skipped (never guessed
    by preferring one channel)."""
    command_names: list = []
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "system":
            continue
        if payload.get("hook_event") != _CLAUDE_USER_PROMPT_EXPANSION_HOOK_EVENT:
            continue
        channel_parsed: dict = {}
        for key in ("stdout", "output"):
            text = payload.get(key)
            if not isinstance(text, str) or not text.strip():
                continue
            parsed = _parse_embedded_json_object(text)
            if parsed is not None:
                channel_parsed[key] = parsed
        if "stdout" in channel_parsed and "output" in channel_parsed:
            name_a = channel_parsed["stdout"].get("command_name")
            name_b = channel_parsed["output"].get("command_name")
            if (
                isinstance(name_a, str)
                and name_a
                and isinstance(name_b, str)
                and name_b
                and name_a != name_b
            ):
                continue  # contradictory channels -- never guess
        for parsed in channel_parsed.values():
            command_name = parsed.get("command_name")
            if isinstance(command_name, str) and command_name:
                command_names.append(command_name)
                break
    return command_names


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

    Each entry: ``{"hook_event": "SubagentStart"|"SubagentStop", "stream_index":
    int, "agent_id": str|None, "agent_type": str|None, "agent_transcript_path":
    str|None, "last_assistant_message": str|None, "session_id": str|None,
    "prompt_id": str|None, "stop_hook_active": bool|None, "contradictory": bool}``.

    ``stream_index`` (Issue #2183 PR #2220 P0-3 fix-delta): the 0-based
    ordinal of this line among ALL parsed stream-json lines in ``stdout``
    (not merely among hook lifecycle lines) -- used by
    ``subagent_causal_evidence_verdict()`` to require a Start to
    structurally PRECEDE its correlated Stop, instead of a bare set
    membership test that could not distinguish a Stop that (in a corrupted
    or adversarial stream) appears before its own Start.

    ``session_id`` / ``prompt_id`` (Issue #2183 PR #2220 P0-3 fix-delta):
    recovered, in priority order, from the embedded hook stdin payload's own
    ``session_id``/``prompt_id`` fields (the official per-invocation hook
    payload channel), falling back to the OUTER native stream-json event's
    own top-level ``session_id``/``sessionId`` field when the embedded
    payload does not carry one. ``None`` when neither source has a value --
    never guessed.

    ``agent_transcript_path`` (Issue #2183 AC1/AC2 causal evidence): the
    official hook stdin payload documented above also carries an
    ``agent_transcript_path`` field on ``SubagentStop`` -- recovered here
    the same best-effort way as ``agent_id``/``agent_type`` (never guessed,
    ``None`` when absent). ``last_assistant_message`` (Issue #2183 AC11):
    when the ``SubagentStop`` hook payload additionally carries a
    ``last_assistant_message`` field (the child's own final response text,
    as opposed to the PARENT model's own final response), it is recovered
    the same way -- used by ``subagent_causal_evidence_verdict()`` as an
    alternate, still child-scoped, provenance source for expected-marker
    matching when no transcript file is readable. Best-effort / fail closed
    to ``None`` fields on any parse mismatch -- never a guess.

    ``contradictory`` (Issue #2183 PR #2220 P1-2 fix-delta): a hook event
    that echoes its own hook stdin payload on BOTH the ``stdout`` and
    ``output`` fields is trusted only when the two channels AGREE on
    ``agent_id``/``agent_type``/``agent_transcript_path`` (when both channels
    parsed a value for a given field). A hook event whose two channels
    disagree on any of those fields is marked ``contradictory: True`` and
    every derived field on the entry (``agent_id``, ``agent_type``,
    ``agent_transcript_path``, ``last_assistant_message``, ``session_id``,
    ``prompt_id``, ``stop_hook_active``) is left ``None`` -- an event this
    harness cannot honestly interpret must never silently prefer one channel
    over the other and must never participate in correlation."""
    events: list[dict] = []
    for stream_index, payload in enumerate(_iter_claude_stream_events(stdout)):
        if payload.get("type") != "system":
            continue
        hook_event = payload.get("hook_event")
        if hook_event not in _CLAUDE_HOOK_LIFECYCLE_EVENTS:
            continue
        entry: dict = {
            "hook_event": hook_event,
            "stream_index": stream_index,
            "agent_id": None,
            "agent_type": None,
            "agent_transcript_path": None,
            "last_assistant_message": None,
            "session_id": None,
            "prompt_id": None,
            "stop_hook_active": None,
            "contradictory": False,
        }
        outer_session_id = payload.get("session_id")
        if not isinstance(outer_session_id, str) or not outer_session_id:
            outer_session_id = payload.get("sessionId")

        hook_name_agent_type: str | None = None
        hook_name = payload.get("hook_name")
        if isinstance(hook_name, str) and hook_name.startswith(f"{hook_event}:"):
            suffix = hook_name.split(":", 1)[1].strip()
            if suffix:
                hook_name_agent_type = suffix

        channel_parsed: dict[str, dict] = {}
        for key in ("stdout", "output"):
            raw_text = payload.get(key)
            if not isinstance(raw_text, str) or not raw_text.strip():
                continue
            parsed = _parse_embedded_json_object(raw_text)
            if parsed is not None:
                channel_parsed[key] = parsed

        if "stdout" in channel_parsed and "output" in channel_parsed:
            channel_a, channel_b = channel_parsed["stdout"], channel_parsed["output"]
            for field in ("agent_id", "agent_type", "agent_transcript_path"):
                value_a, value_b = channel_a.get(field), channel_b.get(field)
                if (
                    isinstance(value_a, str)
                    and value_a
                    and isinstance(value_b, str)
                    and value_b
                    and value_a != value_b
                ):
                    entry["contradictory"] = True
                    break

        if not entry["contradictory"]:
            for parsed in channel_parsed.values():
                agent_id = parsed.get("agent_id")
                if isinstance(agent_id, str) and agent_id and entry["agent_id"] is None:
                    entry["agent_id"] = agent_id
                agent_type = parsed.get("agent_type")
                if isinstance(agent_type, str) and agent_type and entry["agent_type"] is None:
                    entry["agent_type"] = agent_type
                agent_transcript_path = parsed.get("agent_transcript_path")
                if (
                    isinstance(agent_transcript_path, str)
                    and agent_transcript_path
                    and entry["agent_transcript_path"] is None
                ):
                    entry["agent_transcript_path"] = agent_transcript_path
                last_assistant_message = parsed.get("last_assistant_message")
                if (
                    isinstance(last_assistant_message, str)
                    and last_assistant_message
                    and entry["last_assistant_message"] is None
                ):
                    entry["last_assistant_message"] = last_assistant_message
                embedded_session_id = parsed.get("session_id")
                if (
                    isinstance(embedded_session_id, str)
                    and embedded_session_id
                    and entry["session_id"] is None
                ):
                    entry["session_id"] = embedded_session_id
                prompt_id = parsed.get("prompt_id")
                if isinstance(prompt_id, str) and prompt_id and entry["prompt_id"] is None:
                    entry["prompt_id"] = prompt_id
                stop_hook_active = parsed.get("stop_hook_active")
                if isinstance(stop_hook_active, bool) and entry["stop_hook_active"] is None:
                    entry["stop_hook_active"] = stop_hook_active
            if entry["agent_type"] is None and hook_name_agent_type is not None:
                entry["agent_type"] = hook_name_agent_type
            if entry["session_id"] is None and isinstance(outer_session_id, str) and outer_session_id:
                entry["session_id"] = outer_session_id

        events.append(entry)
    return events


def _decode_native_hook_echo(raw_text: str) -> dict | None:
    """Strict whole-string JSON object decode for a native hook stdin echo.

    The runner's ``cat`` hook echoes the stdin payload verbatim, so a genuine
    echo is a pure JSON object string. Unlike the prefix-tolerant
    ``_parse_embedded_json_object`` (kept for other consumers), anything with
    leading / trailing prose (a hook handler's ordinary log line such as
    ``Worker said: {...}``) is NOT decoded here."""
    try:
        parsed = json.loads(raw_text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_claude_subagentstop_permission_mode(stdout: str) -> dict:
    """Issue #2854: independently extract ``permission_mode`` from the native
    stream-json ``SubagentStop`` hook event ONLY.

    Adopted observation: a stream line with ``type == "system"``,
    ``subtype == "hook_response"`` and ``hook_event == "SubagentStop"`` whose
    ``stdout`` / ``output`` channel is a pure JSON object (the native hook
    stdin echo, same acquisition path as ``extract_claude_hook_lifecycle_events``)
    that, WITHIN that single decoded object, has ``hook_event_name ==
    "SubagentStop"``, a non-empty string ``agent_id`` and a ``permission_mode``
    string from the closed official set.

    Eligibility is per channel object: fields are never composed across
    ``stdout`` / ``output`` (one channel's missing field is not filled from the
    other), and prose-prefixed / non-JSON / wrong-``hook_event_name`` output is
    ignored (not a candidate, not a conflict). Disagreement between valid
    candidates (``stdout`` vs ``output`` value or ``agent_id``, or across
    events) and an out-of-enum / non-string ``permission_mode`` on an otherwise
    native-shaped candidate are ``invalid_or_conflicting_value`` -- fail closed.

    Returns ``{"observed": bool, "value": str | None, "reason": str | None}``.
    ``reason`` is ``None`` when observed, otherwise one of
    ``no_subagentstop_hook_event`` / ``field_absent`` /
    ``invalid_or_conflicting_value``.

    ``permission_mode`` only proves the mode the runtime reported on that
    event; it is NOT an ``effective_permission_profile`` observation."""
    stop_event_count = 0
    invalid = False
    adopted_values: list[str] = []
    for payload in _iter_claude_stream_events(stdout):
        if (
            payload.get("type") != "system"
            or payload.get("subtype") != "hook_response"
            or payload.get("hook_event") != "SubagentStop"
        ):
            continue
        stop_event_count += 1
        event_values: set[str] = set()
        event_agent_ids: set[str] = set()
        for key in ("stdout", "output"):
            raw_text = payload.get(key)
            if not isinstance(raw_text, str) or not raw_text.strip():
                continue
            parsed = _decode_native_hook_echo(raw_text)
            if parsed is None or parsed.get("hook_event_name") != "SubagentStop":
                continue
            agent_id = parsed.get("agent_id")
            if not isinstance(agent_id, str) or not agent_id:
                continue
            if _PERMISSION_MODE_FIELD not in parsed:
                continue
            value = parsed[_PERMISSION_MODE_FIELD]
            if not isinstance(value, str) or value not in _PERMISSION_MODE_VALUES:
                invalid = True
                continue
            event_values.add(value)
            event_agent_ids.add(agent_id)
        if len(event_values) > 1 or len(event_agent_ids) > 1:
            invalid = True
            continue
        adopted_values.extend(event_values)

    if invalid or len(set(adopted_values)) > 1:
        return {"observed": False, "value": None, "reason": _OBSERVATION_REASON_INVALID_OR_CONFLICTING}
    if adopted_values:
        return {"observed": True, "value": adopted_values[0], "reason": None}
    if stop_event_count == 0:
        return {"observed": False, "value": None, "reason": _OBSERVATION_REASON_NO_SUBAGENTSTOP_EVENT}
    return {"observed": False, "value": None, "reason": _OBSERVATION_REASON_FIELD_ABSENT}


# ---------------------------------------------------------------------------
# Issue #2663: generic hook-chain evidence capability.
#
# ``all_matching_hooks_observed`` and ``sibling_side_effect_inventory_
# complete`` are two INDEPENDENT assertions:
#
# 1. ``all_matching_hooks_observed`` -- for the bounded
#    ``_HOOK_CHAIN_EVIDENCE_EVENT``/``_HOOK_CHAIN_EVIDENCE_TOOL`` target
#    (PreToolUse/Bash), does every command hook currently registered in this
#    tested_head's OWN ``.claude/settings.json`` for that event+tool
#    actually produce completion evidence (a matched hook_started +
#    hook_response pair, by ``hook_id``) for each real Bash tool call the
#    session makes -- excluding this runner's own additive ``cat`` observer
#    hook (self-identified structurally, never by counting alone)?
#
# 2. ``sibling_side_effect_inventory_complete`` -- independent of any hook's
#    own exit code, does the bounded
#    ``_HOOK_CHAIN_SIDE_EFFECT_ARTIFACT_RELPATH`` directory show the actual
#    POST-CONDITION (a new file) the Stop-event
#    ``_HOOK_CHAIN_SIDE_EFFECT_TARGET_BASENAME`` handler is documented to
#    produce, read only AFTER the whole ``claude -p`` subprocess (and thus
#    every synchronous Stop hook) has exited?
#
# Both were validated against REAL, live ``claude`` CLI output (2.1.277,
# ``--include-hook-events --output-format stream-json``) during this
# Issue's own implementation trial -- not guessed from documentation, which
# does not specify this shape (see the Issue's own "Current Validated
# Scope" caveat). Key confirmed facts this module relies on:
#
# - Each hook execution emits a ``{"type":"system","subtype":"hook_
#   started",...}`` record followed later by a ``{"type":"system",
#   "subtype":"hook_response",...}`` record, correlated by a shared,
#   per-invocation-random ``hook_id`` -- NOT by any stable per-config-entry
#   identifier. ``hook_name`` is ``"<event>"`` (e.g. ``"Stop"``) or
#   ``"<event>:<resolved-tool>"`` (e.g. ``"PreToolUse:Bash"``) -- the same
#   label for every sibling hook registered for that event+tool, so it
#   cannot itself distinguish individual sibling command hooks. This is
#   exactly the "runtimeが実際に提供する同等の識別子" AC2 explicitly allows
#   as a fallback for the (unavailable) event+matcher+command-string
#   identity -- the real command string is never exposed on this channel.
# - ALL sibling hooks registered for an event+tool run to completion
#   regardless of whether one of them returns a deny (exit code 2) --
#   confirmed live: a denying hook does not short-circuit its siblings.
# - A hook that stays silent on success (this repository's own
#   secret_boundary_guard.sh / guard-japanese-prose.sh /
#   ci_test_performance_advisory.sh / root_temporary_residue_advisory.sh,
#   confirmed by reading each script) produces an EMPTY ``stdout``/
#   ``output``, never a JSON echo -- only this runner's own additive
#   ``cat`` observer hook echoes its verbatim stdin JSON payload, which
#   this module uses as a positive, structural self-identification
#   signature (never a count-based guess).
# - PreToolUse hook_started/hook_response records for one Bash tool call
#   appear in the stream between that tool_use's own line and the NEXT
#   Bash tool_use's line -- but NOT necessarily immediately adjacent or
#   mutually contiguous. Confirmed live (2.1.277): a ``rate_limit_event``
#   line can land physically BETWEEN one call's own hook_started and
#   hook_response records, and a ``PostToolUse`` hook's own hook_started
#   can appear before the matching ``user``/tool_result line. Evidence
#   windowing below therefore scopes by a bounded stream-index SPAN
#   (bounded by consecutive Bash tool_use lines), never by strict
#   cluster-adjacency.
# - Issue #2663 AC5 live-trial fix_delta (post-merge repeated-trial
#   instability report): a hook's ``hook_response`` can itself be flushed
#   to the JSON stream AFTER the NEXT Bash tool_use line entirely -- not
#   merely interleaved with rate_limit_event noise INSIDE its own window,
#   but genuinely crossing the window boundary. Confirmed live in a
#   multi-Bash-call trial where ALL 5 ``hook_response`` records for the
#   FIRST Bash call's PreToolUse/Bash cohort (4 project sibling hooks +
#   this runner's own additive observer) were only flushed to the stream
#   AFTER the SECOND Bash tool_use line -- leaving the first call's own
#   window-span with zero ``hook_response`` records at all (spuriously
#   reading as ``observer_self_echo_not_uniquely_identified`` /
#   ``observed_count: 0``) while the second call's window-span absorbed
#   those 5 records as spurious ``unmatched_hook_response`` noise on top
#   of its own genuine 5. ``hook_started`` itself was NOT observed to
#   drift across a window boundary in that same trial (it is emitted at
#   the moment the hook gate begins, structurally tied to its own tool
#   call) -- only a completed hook's RESPONSE flush timing is unreliable
#   under load. Pairing below is therefore GLOBAL (keyed by ``hook_id``
#   across the WHOLE PreToolUse/Bash record stream, never window-scoped),
#   and each resulting pair (or true orphan/dangling record) is attributed
#   to a window by its own ``hook_started``'s stream_index -- never by
#   wherever the paired ``hook_response`` physically landed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Confirmed runtime-capability boundary (PR #2668 fix_delta P1-1; anchor
# review https://github.com/squne121/loop-protocol/pull/2668#issuecomment-5737957277).
# This is a documented, evidence-backed LIMIT of what this module's evidence
# channel can prove -- not an oversight left for a future fix_delta.
#
# ``hook_id`` proves ONLY that a given ``hook_started`` record and a given
# ``hook_response`` record are the SAME single hook invocation (a random,
# per-invocation correlation id -- never a stable per-config-entry
# identifier). ``hook_name`` proves ONLY that a record belongs to a given
# event(+tool) GROUP -- it is the IDENTICAL label for every sibling command
# hook registered under that same event+matcher (e.g. every PreToolUse/Bash
# sibling hook shares the literal ``hook_name`` "PreToolUse:Bash"). Neither
# field -- nor any other field on the documented ``hook_started``/
# ``hook_response`` system events, nor on a hook's own stdin payload --
# identifies WHICH specific configured command hook (by event+matcher+
# command-string identity, as Issue #2663 AC2 originally envisioned)
# produced a given execution. Sources (official docs, not inferred):
# https://code.claude.com/docs/en/hooks-guide and
# https://code.claude.com/docs/en/agent-sdk/hooks -- "When an event fires,
# all matching hooks run in parallel ... write each hook to act
# independently rather than relying on another hook having run first."
# Execution order across sibling hooks is explicitly non-deterministic, so
# an ordering/positional heuristic cannot substitute for a real identity
# channel either (and none is implemented here for that reason).
#
# Consequence (confirmed by local reproduction against synthetic stream-json
# input during this fix_delta, not merely asserted): if the expected cohort
# is handlers {A, B, C, D} and the ACTUAL executions are {A, X, C, D} (X a
# same-count substitution for B, X also producing its own well-formed
# hook_started/hook_response pair with a normal hook_id), ``evaluate_all_
# matching_hooks_observed`` cannot distinguish that stream from a genuine
# {A, B, C, D} execution -- both read as ``pass``. It verifies "N sibling
# PreToolUse/Bash hook executions completed, hook_id-paired, none orphaned,
# none missing, none unattributable-extra", never "the N handlers named in
# tested_head's settings.json, SPECIFICALLY, each individually ran". This is
# the maximal identity granularity Claude Code's documented hook stream
# provides today; closing this gap would require an undocumented/future
# runtime channel (e.g. a stable per-config-entry hook identifier), not a
# code change in this module. Do not read this assertion's ``pass`` as
# per-command handler identity proof, and do not add an ordering/positional
# "identity" heuristic to fake one -- the fact above (non-deterministic
# parallel execution) makes any such heuristic actively misleading.
# ---------------------------------------------------------------------------


def _hook_chain_self_echo_fields(
    hook_event: str | None, stdout_text: object
) -> tuple[bool, bool | None, str | None]:
    """Structural (never count-based) self-identification of this runner's
    OWN additive ``cat`` observer hook response (Issue #2663 AC2/AC3): the
    observer is the only registered hook for
    ``_HOOK_CHAIN_EVIDENCE_EVENT``/``_HOOK_CHAIN_SIDE_EFFECT_EVENT`` that
    echoes its own raw hook stdin payload verbatim back on ``stdout``. Only
    PRESENCE of the allowlisted keys is checked -- their values (prompt
    text, transcript paths, tool input) are never read here or returned to
    any caller, with two narrow exceptions, both bare scalar fields already
    REQUIRED to be present (never prose/content, never a new raw-content
    channel): the ``Stop`` event's own ``stop_hook_active`` boolean (AC3's
    valid-no-change signal) and, since PR #2668 fix_delta (P1-2), the
    ``PreToolUse`` event's own ``tool_use_id`` string -- used ONLY to
    disambiguate which of MULTIPLE same-``stream_index`` Bash ``tool_use``
    blocks (officially supported: an assistant message may declare more
    than one tool_use, each with its own distinct ``tool_use_id`` --
    https://code.claude.com/docs/en/agent-sdk/hooks) this particular
    self-echoed PreToolUse invocation belongs to. This is NOT a per-command
    HANDLER identity channel (see the "Confirmed runtime-capability
    boundary" comment above) -- it identifies only which REAL tool call
    (not which sibling command hook) this runner's own observer was
    triggered for.

    Returns ``(is_self_echo, stop_hook_active, tool_use_id)``.
    ``stop_hook_active`` is always ``None`` unless ``hook_event == "Stop"``
    and self-echo is confirmed. ``tool_use_id`` is always ``None`` unless
    ``hook_event == "PreToolUse"`` and self-echo is confirmed."""
    if hook_event not in _HOOK_CHAIN_SELF_ECHO_REQUIRED_KEYS:
        return False, None, None
    if not isinstance(stdout_text, str) or not stdout_text.strip():
        return False, None, None
    parsed = _parse_embedded_json_object(stdout_text)
    if not isinstance(parsed, dict):
        return False, None, None
    if parsed.get("hook_event_name") != hook_event:
        return False, None, None
    if not all(key in parsed for key in _HOOK_CHAIN_SELF_ECHO_REQUIRED_KEYS[hook_event]):
        return False, None, None
    stop_hook_active = None
    tool_use_id = None
    if hook_event == _HOOK_CHAIN_SIDE_EFFECT_EVENT:
        value = parsed.get("stop_hook_active")
        stop_hook_active = value if isinstance(value, bool) else None
    if hook_event == _HOOK_CHAIN_EVIDENCE_EVENT:
        value = parsed.get("tool_use_id")
        tool_use_id = value if isinstance(value, str) and value else None
    return True, stop_hook_active, tool_use_id


_SESSION_MANIFEST_COORDINATOR_RESULT_MARKER = "SESSION_MANIFEST_COORDINATOR_RESULT_V1="


def _extract_session_manifest_coordinator_result(stderr_text: object) -> dict | None:
    """Issue #2663 PR #2668 fix_delta (P1-3(b), anchor review
    https://github.com/squne121/loop-protocol/pull/2668#issuecomment-5737957277
    counter-example B): bounded, line-scoped parse of
    ``.claude/hooks/session_manifest_coordinator.sh``'s own
    ``SESSION_MANIFEST_COORDINATOR_RESULT_V1={...}`` JSON marker (read-only
    reference; Out of Scope for this Issue to edit) -- confirmed, by
    reading that script's every exit path, to be written VERBATIM to its
    own STDERR (never stdout) on EVERY exit (the early
    ``stop_hook_active`` guard, a guard failure, and full success all end
    with this exact marker line).

    Confirmed live (bounded local trial against installed Claude Code
    2.1.277, run during this fix_delta) that the structured
    ``hook_response`` system event DOES expose a hook's own stderr text,
    verbatim, on a dedicated ``stderr`` field -- distinct from
    ``stdout``/``output`` (the latter being the combined stream) -- a
    channel this module never read before. Only the parsed marker OBJECT
    is returned (never the surrounding raw stderr text, never persisted
    beyond this call) -- ``None`` on any absence/parse failure, never
    fabricated. Only an exact object parse of the marker's own line is
    accepted -- never a regex-scraped value."""
    if not isinstance(stderr_text, str) or not stderr_text:
        return None
    marker_index = stderr_text.find(_SESSION_MANIFEST_COORDINATOR_RESULT_MARKER)
    if marker_index < 0:
        return None
    json_start = marker_index + len(_SESSION_MANIFEST_COORDINATOR_RESULT_MARKER)
    line_end = stderr_text.find("\n", json_start)
    candidate = stderr_text[json_start:] if line_end < 0 else stderr_text[json_start:line_end]
    candidate = candidate.strip()
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_claude_hook_event_records(
    stdout: str, hook_event: str, hook_name: str | None = None
) -> list[dict]:
    """Every ``hook_started``/``hook_response`` system record in ``stdout``
    for one specific ``hook_event`` (optionally further restricted to an
    exact ``hook_name``), each retaining its own stream_index. Each record:
    ``{"stream_index", "subtype", "hook_id", "hook_name", "hook_event",
    "exit_code", "outcome", "is_self_echo", "stop_hook_active",
    "tool_use_id", "coordinator_result"}``. No raw hook stdin/stdout/stderr
    body is retained on the returned records -- every derived field is
    computed once, up front, and the underlying text is discarded.
    ``tool_use_id`` (PR #2668 fix_delta P1-2) is populated only for a
    self-echoed ``PreToolUse`` response (see
    ``_hook_chain_self_echo_fields``). ``coordinator_result`` (PR #2668
    fix_delta P1-3(b)) is populated only for a ``Stop`` ``hook_response``
    whose own ``stderr`` field carries
    ``.claude/hooks/session_manifest_coordinator.sh``'s own
    ``SESSION_MANIFEST_COORDINATOR_RESULT_V1={...}`` completion marker (see
    ``_extract_session_manifest_coordinator_result``) -- confirmed, by a
    bounded local live trial against installed Claude Code 2.1.277 during
    this fix_delta, that a ``hook_response`` system event's own ``stderr``
    field DOES carry a hook's stderr text verbatim (distinct from
    ``stdout``/``output``, which is the combined stream) -- previously
    unread anywhere in this module.

    Issue #2663 live-trial fix: an earlier revision of this module grouped
    hook records via strict "consecutive stream-index" clustering and
    matched a Bash ``tool_use`` to whichever cluster began EXACTLY at
    ``tool_use.stream_index + 1``. Confirmed live against real Claude Code
    2.1.277 output, that assumption is false in practice -- a
    ``rate_limit_event`` line landed physically BETWEEN this repository's
    own PreToolUse ``hook_started`` and ``hook_response`` records for a
    single real Bash tool call, splitting what should have been one
    contiguous evidence window into two and causing a live PASS-eligible
    run to read as spuriously ``unverified``. This function therefore
    performs NO clustering/contiguity assumption at all -- callers scope
    query records to a bounded stream_index SPAN instead (see
    ``evaluate_all_matching_hooks_observed`` below), which tolerates any
    interleaved non-hook noise (rate_limit_event, a different event's own
    hook records, thinking blocks, ...)."""
    records: list[dict] = []
    for stream_index, payload in enumerate(_iter_claude_stream_events(stdout)):
        if payload.get("type") != "system":
            continue
        subtype = payload.get("subtype")
        if subtype not in ("hook_started", "hook_response"):
            continue
        if payload.get("hook_event") != hook_event:
            continue
        payload_hook_name = payload.get("hook_name")
        if hook_name is not None and payload_hook_name != hook_name:
            continue
        is_self_echo = False
        stop_hook_active = None
        tool_use_id = None
        coordinator_result = None
        if subtype == "hook_response":
            is_self_echo, stop_hook_active, tool_use_id = _hook_chain_self_echo_fields(
                hook_event, payload.get("stdout")
            )
            if hook_event == _HOOK_CHAIN_SIDE_EFFECT_EVENT:
                coordinator_result = _extract_session_manifest_coordinator_result(
                    payload.get("stderr")
                )
        records.append({
            "stream_index": stream_index,
            "subtype": subtype,
            "hook_id": payload.get("hook_id"),
            "hook_name": payload_hook_name,
            "hook_event": hook_event,
            "exit_code": payload.get("exit_code"),
            "outcome": payload.get("outcome"),
            "is_self_echo": is_self_echo,
            "stop_hook_active": stop_hook_active,
            "tool_use_id": tool_use_id,
            "coordinator_result": coordinator_result,
        })
    return records


def _claude_bash_tool_use_events(stdout: str) -> list[dict]:
    """Every ``Bash`` ``tool_use`` block's ``{"stream_index", "tool_use_id",
    "command"}`` (Issue #2663 AC5 positive/deny scenario windowing).

    Issue #2865: ``command`` is ``input.command`` bound to its own
    ``tool_use_id`` (``None`` when absent or not a string) and exists only
    in memory so handler-level ``if`` conditions can be evaluated per
    window -- it is never copied into any summary / evidence / error
    text."""
    results: list[dict] = []
    for stream_index, payload in enumerate(_iter_claude_stream_events(stdout)):
        if payload.get("type") != "assistant":
            continue
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Bash"
            ):
                tool_input = block.get("input")
                command = tool_input.get("command") if isinstance(tool_input, dict) else None
                results.append({
                    "stream_index": stream_index,
                    "tool_use_id": block.get("id"),
                    "command": command if isinstance(command, str) else None,
                })
    return results


def _pair_pretool_hook_records_globally(records: list[dict]) -> dict:
    """Global (never window-scoped) ``hook_started``/``hook_response``
    pairing by ``hook_id`` across the WHOLE PreToolUse/Bash record stream
    (Issue #2663 AC5 live-trial fix_delta -- see the module-level comment
    block above ``_pair_pretool_hook_records_globally``'s call site for the
    confirmed-live counter-example this exists to tolerate).

    Only ``subtype == "hook_started"`` and ``subtype == "hook_response"``
    records with a non-empty ``hook_id`` participate. A duplicate
    ``hook_started`` re-announcement for the same ``hook_id`` (AC6(b)) is
    NOT double-counted -- the first (earliest stream_index) announcement is
    the pairing anchor. A ``hook_response`` whose ``hook_id`` has no
    corresponding ``hook_started`` anywhere in ``records`` -- or a SECOND
    ``hook_response`` for an already-paired ``hook_id`` -- is an orphan,
    never silently counted as evidence (AC6(a)).

    Returns ``{"paired": [{"started": rec, "response": rec}, ...],
    "orphan_responses": [rec, ...]}``; a ``hook_started`` that never
    receives ANY matching ``hook_response`` simply produces no pair (its
    own window naturally under-counts ``observed_count``, matching prior
    "missing_handler_evidence" behavior).

    Note (PR #2668 fix_delta P1-1): this pairing proves only "this
    hook_started and this hook_response are the same invocation" -- it is
    NOT, and cannot be, a per-command HANDLER identity channel. See the
    "Confirmed runtime-capability boundary" comment above
    ``_hook_chain_self_echo_fields``."""
    started_by_id: dict[str, dict] = {}
    for r in records:
        if (
            r["subtype"] == "hook_started"
            and r["hook_id"]
            and r["hook_event"] == _HOOK_CHAIN_EVIDENCE_EVENT
        ):
            started_by_id.setdefault(r["hook_id"], r)

    paired: list[dict] = []
    orphan_responses: list[dict] = []
    claimed_hook_ids: set[str] = set()
    for r in records:
        if r["subtype"] != "hook_response" or r["hook_event"] != _HOOK_CHAIN_EVIDENCE_EVENT:
            continue
        hook_id = r["hook_id"]
        if hook_id and hook_id in started_by_id and hook_id not in claimed_hook_ids:
            paired.append({"started": started_by_id[hook_id], "response": r})
            claimed_hook_ids.add(hook_id)
        else:
            orphan_responses.append(r)
    return {"paired": paired, "orphan_responses": orphan_responses}


def _evaluate_hook_chain_window(
    paired: list[dict], unmatched_response_count: int, expected_count: int | None
) -> dict:
    """Evaluate one PreToolUse/Bash scenario window (Issue #2663 AC2/AC6).

    ``paired`` is every GLOBALLY hook_id-matched ``{"started", "response"}``
    pair whose ``started`` record's stream_index falls within this window's
    bounded span (see ``evaluate_all_matching_hooks_observed`` and
    ``_pair_pretool_hook_records_globally``) -- attribution never depends on
    where the paired ``response`` physically landed. ``unmatched_response_
    count`` is the count of orphan ``hook_response`` records (no
    corresponding ``hook_started`` anywhere in the whole stream) whose OWN
    stream_index falls within this window -- a ``hook_response`` with no
    matching ``hook_started`` is dropped as unmatched/anomalous, never
    silently counted (Issue #2663 AC6(a)/(h): a masked "same total count,
    one missing + one unattributed" substitution must not read as PASS).

    Note (PR #2668 fix_delta P1-1): a ``pass`` verdict here proves "N
    sibling PreToolUse/Bash hook executions completed, hook_id-paired,
    none orphaned/missing/unattributable-extra" -- never "the specific N
    handlers named in settings.json, individually, each ran" (no channel
    exists to prove the latter; see the "Confirmed runtime-capability
    boundary" comment above ``_hook_chain_self_echo_fields``).

    Issue #2865: ``expected_count`` is this window's OWN expected count
    (handler-level ``if`` conditions already resolved for this window's
    Bash command), or ``None`` when some conditional hook's ``if`` could
    not be decided -- such a window is ``unverified`` (reason
    ``if_condition_unverifiable``) and never ``pass``, regardless of the
    observed count."""
    self_echo = [p for p in paired if p["response"]["is_self_echo"]]
    non_observer = [p for p in paired if not p["response"]["is_self_echo"]]

    if len(self_echo) != 1:
        return {
            "status": "unverified",
            "reason": "observer_self_echo_not_uniquely_identified",
            "observer_response_count": len(self_echo),
            "observed_count": len(non_observer),
            "expected_count": expected_count,
            "unmatched_response_count": unmatched_response_count,
            "denied": any(p["response"]["exit_code"] == 2 for p in non_observer),
        }

    observed_count = len(non_observer)
    denied = any(p["response"]["exit_code"] == 2 for p in non_observer)

    if unmatched_response_count > 0:
        return {
            "status": "fail",
            "reason": "unmatched_hook_response",
            "observed_count": observed_count,
            "expected_count": expected_count,
            "unmatched_response_count": unmatched_response_count,
            "denied": denied,
        }
    if expected_count is None:
        return {
            "status": "unverified",
            "reason": "if_condition_unverifiable",
            "observed_count": observed_count,
            "expected_count": None,
            "unmatched_response_count": 0,
            "denied": denied,
        }
    if observed_count < expected_count:
        return {
            "status": "fail",
            "reason": "missing_handler_evidence",
            "observed_count": observed_count,
            "expected_count": expected_count,
            "unmatched_response_count": 0,
            "denied": denied,
        }
    if observed_count > expected_count:
        # Issue #2663 In Scope: "project settings外から合成されるhook
        # source...の存在をもって「runtime全体の全hookを証明した」とは
        # 主張しない（識別不能な場合はunverifiedとする）" -- a user/local/
        # managed/plugin/skill-level extra hook (e.g. this environment's
        # own ``~/.claude/settings.json`` PreToolUse/Bash hook, confirmed
        # live) is observationally indistinguishable from a genuine
        # "duplicate/unknown" entry on this channel; never silently
        # promoted to either pass or fail.
        return {
            "status": "unverified",
            "reason": "unattributable_extra_hook_execution",
            "observed_count": observed_count,
            "expected_count": expected_count,
            "unmatched_response_count": 0,
            "denied": denied,
        }
    return {
        "status": "pass",
        "reason": None,
        "observed_count": observed_count,
        "expected_count": expected_count,
        "unmatched_response_count": 0,
        "denied": denied,
    }


def _partition_shared_stream_index_span(
    lower: int,
    upper: int | None,
    declared_tool_use_ids: list[str],
    self_echo_pairs: list[dict],
) -> list[tuple[int, int | None]] | None:
    """Issue #2663 PR #2668 fix_delta (P1-2, anchor review
    https://github.com/squne121/loop-protocol/pull/2668#issuecomment-5737957277):
    sub-boundary partition for a SHARED stream_index span -- multiple
    ``Bash`` ``tool_use`` blocks declared inside the SAME assistant
    message (officially supported by the Agent SDK: distinct tool_use
    blocks in one assistant message each carry their own ``tool_use_id``,
    see https://code.claude.com/docs/en/agent-sdk/hooks and
    https://code.claude.com/docs/en/hooks-guide). The bare assistant
    message stream_index can no longer distinguish these calls' own
    windows (they all share the same ``lower`` bound) -- instead, each
    declared ``tool_use_id`` is anchored to the GLOBALLY hook_id-paired
    self-echo PreToolUse response whose OWN embedded ``tool_use_id``
    (see ``_hook_chain_self_echo_fields``) matches it, using that pair's
    ``started`` record's stream_index (never the response's own
    stream_index, which can be flushed late -- see the module-level
    comment block's AC5 finding above ``_hook_chain_self_echo_fields``) as
    the anchor.

    Anchors are sorted ascending and adjacent anchors are split at their
    integer midpoint -- tolerant of this module's own confirmed
    non-deterministic PARALLEL sibling hook_started ordering WITHIN one
    call's own cohort (https://code.claude.com/docs/en/hooks-guide: "all
    matching hooks run in parallel"), because the gap between one call's
    own cohort and the NEXT call's cohort structurally spans that entire
    call's synchronous hook resolution plus tool execution plus result
    emission -- comfortably larger than the sibling-launch jitter within a
    single cohort.

    Returns ``None`` (never a guess) when the self-echo evidence within
    this span does not cleanly, unambiguously biject onto
    ``declared_tool_use_ids`` -- a missing anchor for a declared id, two
    anchors claiming the same id, or an anchor whose value matches no
    declared id -- forcing the caller to fail closed to
    ``self_echo_tool_use_id_ambiguous`` rather than guess an
    attribution."""
    anchors: dict[str, int] = {}
    for pair in self_echo_pairs:
        started_index = pair["started"]["stream_index"]
        if not (started_index > lower and (upper is None or started_index < upper)):
            continue
        tool_use_id = pair["response"].get("tool_use_id")
        if not isinstance(tool_use_id, str) or tool_use_id not in declared_tool_use_ids:
            return None
        if tool_use_id in anchors:
            return None
        anchors[tool_use_id] = started_index
    if len(anchors) != len(declared_tool_use_ids):
        return None

    ordered_ids = sorted(declared_tool_use_ids, key=lambda tool_use_id: anchors[tool_use_id])
    anchor_values = [anchors[tool_use_id] for tool_use_id in ordered_ids]
    bounds_by_id: dict[str, tuple[int, int | None]] = {}
    for i, tool_use_id in enumerate(ordered_ids):
        window_lower = lower if i == 0 else (anchor_values[i - 1] + anchor_values[i]) // 2
        window_upper = (
            upper if i == len(ordered_ids) - 1
            else (anchor_values[i] + anchor_values[i + 1]) // 2
        )
        bounds_by_id[tool_use_id] = (window_lower, window_upper)
    return [bounds_by_id[tool_use_id] for tool_use_id in declared_tool_use_ids]


def evaluate_all_matching_hooks_observed(stdout: str, worktree: str) -> dict:
    """Issue #2663 AC2: ``all_matching_hooks_observed`` verdict.

    Returns ``{"status": "pass"|"fail"|"unverified", "passed": bool,
    "expected_count": int|None, "positive_window_count": int,
    "deny_window_count": int, "windows": [...], "reason": str|None}``.
    ``passed`` is ``True`` only when ``status == "pass"``.

    Note (PR #2668 fix_delta P1-1): a ``pass`` here is NOT per-command
    handler identity proof -- see the "Confirmed runtime-capability
    boundary" comment above ``_hook_chain_self_echo_fields``."""
    settings = _read_project_settings(worktree)
    if settings is None:
        return {
            "status": "unverified",
            "passed": False,
            "reason": "project_settings_unreadable",
            "expected_count": None,
            "positive_window_count": 0,
            "deny_window_count": 0,
            "windows": [],
        }
    expected_hook_records = _load_command_hook_records_for_event(
        settings, _HOOK_CHAIN_EVIDENCE_EVENT, _HOOK_CHAIN_EVIDENCE_TOOL
    )

    bash_tool_uses = _claude_bash_tool_use_events(stdout)
    # Issue #2865: expected count is per Bash tool_use (handler-level ``if``
    # conditions are evaluated against THAT call's own command); ``None``
    # means "not decidable" and is never turned into a number.
    per_tool_use_expected: list[int | None] = [
        _expected_hook_count_for_command(
            expected_hook_records, _HOOK_CHAIN_EVIDENCE_TOOL, tool_use["command"]
        )
        for tool_use in bash_tool_uses
    ]

    if not bash_tool_uses:
        return {
            "status": "fail",
            "passed": False,
            "reason": "no_bash_tool_use_observed",
            "expected_count": None,
            "positive_window_count": 0,
            "deny_window_count": 0,
            "windows": [],
        }

    # Issue #2663 live-trial fix: SPAN-based windowing, never strict
    # cluster-adjacency (see extract_claude_hook_event_records's docstring
    # for the confirmed-live counter-example). All PreToolUse/Bash records
    # strictly between one Bash tool_use and the NEXT Bash tool_use (or end
    # of stream, for the last one) belong to that tool_use's own window --
    # tolerant of any interleaved rate_limit_event / other-tool hook
    # records / thinking blocks.
    all_pretool_bash_records = extract_claude_hook_event_records(
        stdout, _HOOK_CHAIN_EVIDENCE_EVENT,
        f"{_HOOK_CHAIN_EVIDENCE_EVENT}:{_HOOK_CHAIN_EVIDENCE_TOOL}",
    )
    # Issue #2663 AC5 live-trial fix_delta: pair hook_started/hook_response
    # GLOBALLY by hook_id first (never window-scoped -- see
    # _pair_pretool_hook_records_globally's docstring), then attribute each
    # resulting pair to a window using its OWN hook_started's stream_index.
    # A response that physically lands after the NEXT Bash tool_use's line
    # (confirmed live) is therefore still correctly attributed to the call
    # that actually triggered it, never to whichever window it happened to
    # land in. Computed here (before window construction) because PR #2668
    # fix_delta (P1-2) SHARED-stream_index window construction (below) also
    # needs the self-echo pairs to derive sub-boundaries.
    pairing = _pair_pretool_hook_records_globally(all_pretool_bash_records)
    self_echo_pairs = [p for p in pairing["paired"] if p["response"]["is_self_echo"]]

    # Issue #2663 PR #2668 fix_delta (P1-2): group Bash tool_use events by
    # their own (possibly SHARED) stream_index -- consecutive tool_use
    # entries sharing one stream_index came from the SAME assistant
    # message (multiple tool_use blocks in one message; officially
    # supported -- see _partition_shared_stream_index_span's docstring).
    # ``bash_tool_uses`` is already in stream order, so co-located entries
    # are always adjacent -- grouping never reorders anything.
    groups: list[list[dict]] = []
    for tool_use in bash_tool_uses:
        if groups and groups[-1][0]["stream_index"] == tool_use["stream_index"]:
            groups[-1].append(tool_use)
        else:
            groups.append([tool_use])
    group_upper_bounds = [group[0]["stream_index"] for group in groups[1:]] + [None]

    # window_bounds[i] / window_forced[i] stay parallel to the ORIGINAL
    # ``bash_tool_uses`` order (grouping only merges ADJACENT
    # identical-stream_index entries, so flattening groups back out
    # reproduces that same order).
    window_bounds: list[tuple[int, int | None] | None] = []
    window_forced: list[dict | None] = []
    for group, group_upper in zip(groups, group_upper_bounds):
        group_lower = group[0]["stream_index"]
        if len(group) == 1:
            window_bounds.append((group_lower, group_upper))
            window_forced.append(None)
            continue
        declared_ids = [tool_use["tool_use_id"] for tool_use in group]
        partition = None
        if all(isinstance(tool_use_id, str) and tool_use_id for tool_use_id in declared_ids):
            partition = _partition_shared_stream_index_span(
                group_lower, group_upper, declared_ids, self_echo_pairs
            )
        if partition is None:
            # Issue #2663 PR #2668 fix_delta (P1-2 negative case): the
            # self-echo evidence for this shared span could not cleanly,
            # unambiguously biject onto the declared tool_use_ids -- never
            # guess an attribution; every window sharing this span fails
            # closed to unverified.
            for _ in group:
                window_bounds.append(None)
                window_forced.append({
                    "status": "unverified",
                    "reason": "self_echo_tool_use_id_ambiguous",
                    "observed_count": 0,
                    "expected_count": per_tool_use_expected[len(window_bounds) - 1],
                    "unmatched_response_count": 0,
                    "denied": False,
                })
        else:
            for bounds in partition:
                window_bounds.append(bounds)
                window_forced.append(None)

    def _window_index_for(stream_index: int) -> int | None:
        for i, bounds in enumerate(window_bounds):
            if bounds is None:
                continue
            lower, upper = bounds
            if stream_index > lower and (upper is None or stream_index < upper):
                return i
        return None

    per_window_paired: list[list[dict]] = [[] for _ in window_bounds]
    per_window_unmatched_response_count: list[int] = [0 for _ in window_bounds]
    for pair in pairing["paired"]:
        idx = _window_index_for(pair["started"]["stream_index"])
        if idx is not None:
            per_window_paired[idx].append(pair)
    for orphan_response in pairing["orphan_responses"]:
        idx = _window_index_for(orphan_response["stream_index"])
        if idx is not None:
            per_window_unmatched_response_count[idx] += 1

    window_results: list[dict] = []
    for i in range(len(window_bounds)):
        if window_forced[i] is not None:
            window_results.append(window_forced[i])
        else:
            window_results.append(_evaluate_hook_chain_window(
                per_window_paired[i], per_window_unmatched_response_count[i],
                per_tool_use_expected[i],
            ))

    if any(w["status"] == "fail" for w in window_results):
        overall_status = "fail"
    elif any(w["status"] == "unverified" for w in window_results):
        overall_status = "unverified"
    else:
        positive_window_count = sum(1 for w in window_results if not w["denied"])
        deny_window_count = sum(1 for w in window_results if w["denied"])
        # Issue #2663 AC5/AC6(f): an empty expected/observed scenario set
        # (no positive call, no deny call) must never read as PASS.
        overall_status = "pass" if positive_window_count > 0 and deny_window_count > 0 else "fail"

    positive_window_count = sum(1 for w in window_results if not w["denied"])
    deny_window_count = sum(1 for w in window_results if w["denied"])
    # Issue #2865: ``windows[i].expected_count`` is canonical; the top-level
    # value is the common value only when every window agrees, else ``None``.
    window_expected_values = {w["expected_count"] for w in window_results}
    top_level_expected_count = (
        next(iter(window_expected_values)) if len(window_expected_values) == 1 else None
    )
    return {
        "status": overall_status,
        "passed": overall_status == "pass",
        "reason": None if overall_status == "pass" else "see windows[]",
        "expected_count": top_level_expected_count,
        "positive_window_count": positive_window_count,
        "deny_window_count": deny_window_count,
        "windows": window_results,
    }


def _snapshot_session_manifest_files(worktree: str) -> tuple[list[str] | None, bool]:
    """Bounded directory listing (filenames only -- never content) of the
    Issue #2663 AC3 side-effect target directory.

    Issue #2663 PR #2668 fix_delta (P2-1, anchor review
    https://github.com/squne121/loop-protocol/pull/2668#issuecomment-5737957277):
    an earlier revision listed and alphabetically sorted EVERY file in the
    directory, THEN capped to the first
    ``_HOOK_CHAIN_SIDE_EFFECT_MAX_SNAPSHOT_ENTRIES`` -- Stop-tag filtering
    happened only afterward, in the caller. Because this directory is
    ALSO shared by an unrelated, high-volume writer (the settings.json
    PostToolUse debounce hook's own ``-posttooluse-`` tagged files, which
    sort alphabetically BEFORE ``-stop-`` tagged ones), once that many
    pre-existing ``-posttooluse-`` files accumulated, a genuinely NEW
    ``-stop-`` tagged manifest was silently truncated away, and this
    run's own valid new evidence read as a false ``missing_side_effect``.

    Fixed here: filtering for the Stop-tag token
    (``_HOOK_CHAIN_SIDE_EFFECT_FILENAME_TOKEN``) happens DURING directory
    iteration, BEFORE any sort/cap -- the cap now applies ONLY to the
    already-small, Stop-tag-relevant subset, never to the whole unfiltered
    directory listing (an unrelated writer's own file volume can never
    influence this snapshot at all, positively or negatively).

    Returns ``(entries, truncated)``. ``entries`` is the sorted list of
    Stop-tagged filenames only; ``None`` (never a fabricated/empty list)
    only when the directory exists but cannot be listed (permission
    error) -- an absent directory is a legitimate, listable "no manifests
    yet" state. ``truncated`` is ``True`` only when the STOP-TAGGED subset
    itself exceeds the defensive cap -- surfaced explicitly (never a
    silent drop) so the caller (``evaluate_sibling_side_effect_inventory``)
    treats that specific case as ``unverified`` rather than silently
    losing evidence."""
    manifests_dir = Path(worktree).joinpath(*_HOOK_CHAIN_SIDE_EFFECT_ARTIFACT_RELPATH)
    if not manifests_dir.exists():
        return [], False
    try:
        stop_tagged = sorted(
            p.name for p in manifests_dir.iterdir()
            if p.is_file() and _HOOK_CHAIN_SIDE_EFFECT_FILENAME_TOKEN in p.name
        )
    except OSError:
        return None, False
    truncated = len(stop_tagged) > _HOOK_CHAIN_SIDE_EFFECT_MAX_SNAPSHOT_ENTRIES
    return stop_tagged[:_HOOK_CHAIN_SIDE_EFFECT_MAX_SNAPSHOT_ENTRIES], truncated


def extract_claude_hook_chain_stop_hook_active(stdout: str) -> bool | None:
    """The real, runtime-returned ``stop_hook_active`` boolean from this
    runner's own additive Stop observer hook (Issue #2663 AC3 valid
    no-change condition). ``None`` when the observer's own response cannot
    be uniquely, structurally identified (never guessed from count)."""
    stop_records = extract_claude_hook_event_records(
        stdout, _HOOK_CHAIN_SIDE_EFFECT_EVENT, _HOOK_CHAIN_SIDE_EFFECT_EVENT
    )
    self_echo_responses = [
        record for record in stop_records
        if record["subtype"] == "hook_response" and record["is_self_echo"]
    ]
    if len(self_echo_responses) != 1:
        return None
    return self_echo_responses[0]["stop_hook_active"]


def _extract_target_coordinator_completion(stdout: str) -> dict | None:
    """Issue #2663 PR #2668 fix_delta (P1-3(b), anchor review counter-
    example B): the TARGET ``session_manifest_coordinator.sh`` handler's
    OWN (non-observer) completion marker for the ``Stop`` event --
    distinguished from this runner's own additive observer response the
    same way the rest of this module already does (structural ``is_self_
    echo`` presence check), never by position/count. Returns the parsed
    ``SESSION_MANIFEST_COORDINATOR_RESULT_V1`` marker object only when
    EXACTLY ONE non-self-echo Stop ``hook_response`` record carries one
    (see ``_extract_session_manifest_coordinator_result``) -- ``None``
    (never guessed) when zero or more than one candidate is found; a hook
    that happens to run alongside the coordinator but never emits this
    exact marker is never mistaken for it (this is a CONTENT-based
    signature, the same class of positive self-identification already
    used for the observer's own self-echo -- not an ordering/position
    guess, and not a claim about handler identity in general; see the
    "Confirmed runtime-capability boundary" comment above
    ``_hook_chain_self_echo_fields``)."""
    stop_records = extract_claude_hook_event_records(
        stdout, _HOOK_CHAIN_SIDE_EFFECT_EVENT, _HOOK_CHAIN_SIDE_EFFECT_EVENT
    )
    candidates = [
        record for record in stop_records
        if record["subtype"] == "hook_response"
        and not record["is_self_echo"]
        and record.get("coordinator_result") is not None
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]["coordinator_result"]


def _session_manifest_actor_session_id(worktree: str, filename: str) -> str | None:
    """Issue #2663 PR #2668 fix_delta (P1-3(a)): best-effort, bounded read
    of one Issue #2663 AC3 target-directory manifest file's own embedded
    ``actor.session_id`` field -- read-only, bounded to this run's own
    Allowed-Paths-scoped artifact directory (never attacker-controlled,
    never a network fetch), used ONLY to bind a candidate new file to the
    CURRENT run's own session before counting it as this run's evidence.
    The field is populated (see ``scripts/generate-session-manifest.mjs``
    and ``.claude/hooks/generate_session_manifest_from_hook.mjs``, both
    read-only references, Out of Scope for this Issue to edit) from the
    SAME hook stdin ``session_id`` this module's own stream already
    exposes for the current run. ``None`` (never fabricated) when the file
    cannot be read/parsed or the field is absent -- callers must never
    treat that as a match."""
    manifest_path = Path(worktree).joinpath(*_HOOK_CHAIN_SIDE_EFFECT_ARTIFACT_RELPATH, filename)
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    actor = data.get("actor")
    if not isinstance(actor, dict):
        return None
    session_id = actor.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def evaluate_sibling_side_effect_inventory(
    worktree: str,
    stdout: str,
    manifests_before: list[str] | None,
    manifests_after: list[str] | None,
    snapshot_truncated: bool = False,
) -> dict:
    """Issue #2663 AC3: ``sibling_side_effect_inventory_complete`` verdict.

    Independent of hook exit codes -- reads the actual post-condition (new
    files under the bounded manifests directory) AFTER the whole structured
    ``claude`` subprocess (and thus every synchronous Stop hook, per the
    Claude Code Stop hook contract) has exited.

    Issue #2663 PR #2668 fix_delta (P1-3(a)/(b), P2-1, anchor review
    https://github.com/squne121/loop-protocol/pull/2668#issuecomment-5737957277):

    - ``snapshot_truncated`` (P2-1): ``True`` when either the before/after
      Stop-tagged manifest-directory snapshot was itself truncated by
      ``_snapshot_session_manifest_files``'s own defensive cap -- this
      assertion refuses to guess completeness in that case:
      ``unverified``, never a silently-incomplete "no new file" read.
    - New-file "run binding" (P1-3(a), counter-example A): a candidate new
      Stop-tagged file is counted ONLY when its own embedded
      ``actor.session_id`` (see ``_session_manifest_actor_session_id``)
      matches the CURRENT run's own session id (reusing the existing
      ``extract_claude_stream_session_ids`` extraction, never a new raw
      channel). A same-directory file belonging to a concurrent, stale, or
      otherwise unrelated session is never counted as (or against) this
      run's own evidence.
    - Valid no-change corroboration (P1-3(b), counter-example B): the "no
      new file" path now requires BOTH the observer's own ``stop_hook_
      active: true`` self-echo AND the TARGET
      ``session_manifest_coordinator.sh`` handler's OWN (non-observer)
      completion marker with ``"steps": ["stop_guard"]`` (see
      ``_extract_target_coordinator_completion``) -- the observer's own
      input alone no longer suffices (previously the sole signal)."""
    settings = _read_project_settings(worktree)
    if settings is None:
        return {
            "status": "unverified", "passed": False,
            "reason": "project_settings_unreadable", "new_file_count": None,
        }
    stop_commands = _load_command_hooks_for_event(settings, _HOOK_CHAIN_SIDE_EFFECT_EVENT, None)
    target_configured = any(
        _HOOK_CHAIN_SIDE_EFFECT_TARGET_BASENAME in command for command in stop_commands
    )
    if not target_configured:
        return {
            "status": "unverified", "passed": False,
            "reason": "target_handler_not_configured", "new_file_count": None,
        }
    if manifests_before is None or manifests_after is None:
        return {
            "status": "unverified", "passed": False,
            "reason": "postcondition_unreadable", "new_file_count": None,
        }
    if snapshot_truncated:
        return {
            "status": "unverified", "passed": False,
            "reason": "session_manifest_snapshot_truncated", "new_file_count": None,
        }

    # Issue #2663 live-trial fix: scope "new files" to ONLY the target
    # handler's own filename tag (see _HOOK_CHAIN_SIDE_EFFECT_FILENAME_
    # TOKEN's docstring) -- a co-registered, non-target manifest writer
    # (the settings.json PostToolUse debounce hook) sharing the same
    # bounded directory must never inflate or satisfy this assertion.
    candidate_new_files = sorted(
        name for name in (set(manifests_after) - set(manifests_before))
        if _HOOK_CHAIN_SIDE_EFFECT_FILENAME_TOKEN in name
    )
    # Issue #2663 PR #2668 fix_delta (P1-3(a)): bind each candidate to the
    # CURRENT run's own session before counting it (see this function's
    # own docstring above). A file whose own session id cannot be
    # determined -- or that does not match -- is excluded, never guessed.
    current_session_ids = set(extract_claude_stream_session_ids(stdout))
    new_files = [
        name for name in candidate_new_files
        if current_session_ids
        and _session_manifest_actor_session_id(worktree, name) in current_session_ids
    ]

    if len(new_files) > _HOOK_CHAIN_SIDE_EFFECT_MAX_EXPECTED_NEW_FILES:
        return {
            "status": "fail", "passed": False,
            "reason": "overflow", "new_file_count": len(new_files),
        }
    if len(new_files) > 0:
        return {
            "status": "pass", "passed": True,
            "reason": "new_manifest_observed", "new_file_count": len(new_files),
        }

    # Issue #2663 PR #2668 fix_delta (P1-3(b)): the observer's own
    # stop_hook_active self-echo is a necessary but no longer SUFFICIENT
    # no-change signal -- it proves only that the EARLY-EXIT CONDITION
    # reached this runner's own additive observer hook, never that the
    # TARGET coordinator itself actually completed its own no-op path.
    # Both must independently agree.
    stop_hook_active = extract_claude_hook_chain_stop_hook_active(stdout)
    coordinator_result = _extract_target_coordinator_completion(stdout)
    coordinator_no_op_confirmed = (
        isinstance(coordinator_result, dict)
        and coordinator_result.get("status") == "ok"
        and coordinator_result.get("steps") == ["stop_guard"]
    )
    if stop_hook_active is True and coordinator_no_op_confirmed:
        return {
            "status": "pass", "passed": True,
            "reason": "valid_no_change_stop_hook_active", "new_file_count": 0,
        }
    return {
        "status": "fail", "passed": False,
        "reason": "missing_side_effect", "new_file_count": 0,
    }


def evaluate_hook_chain_evidence(
    stdout: str,
    worktree: str,
    manifests_before: list[str] | None,
    manifests_after: list[str] | None,
    snapshot_truncated: bool = False,
) -> dict:
    """Issue #2663 AC4: aggregate hook-chain-evidence verdict. ``passed`` is
    ``True`` only when BOTH ``all_matching_hooks_observed`` AND
    ``sibling_side_effect_inventory_complete`` independently report
    ``status: pass``."""
    all_matching_hooks_observed = evaluate_all_matching_hooks_observed(stdout, worktree)
    sibling_side_effect_inventory_complete = evaluate_sibling_side_effect_inventory(
        worktree, stdout, manifests_before, manifests_after, snapshot_truncated=snapshot_truncated
    )
    passed = bool(all_matching_hooks_observed["passed"] and sibling_side_effect_inventory_complete["passed"])
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "all_matching_hooks_observed": all_matching_hooks_observed,
        "sibling_side_effect_inventory_complete": sibling_side_effect_inventory_complete,
    }


# ---------------------------------------------------------------------------
# Hook-ID-correlated SubAgent causal evidence (Issue #2183, follow-up to
# Issue #2174 OWNER REQUEST_CHANGES
# https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173,
# PR #2214 OWNER review
# https://github.com/squne121/loop-protocol/pull/2214#issuecomment-5307009937,
# and PR #2220 OWNER REQUEST_CHANGES
# https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514):
# a marker string observed in captured stdout/pane text proves only that
# SOME text containing that literal token appeared somewhere in the
# transcript -- it is trivially satisfiable by a synthetic fixture that
# never spawned a real child at all. This section adds a verdict function that instead
# requires a structural, hook-ID-correlated signal: the SAME ``agent_id``
# observed on BOTH a ``SubagentStart`` and a ``SubagentStop`` hook lifecycle
# event, with the ``SubagentStop`` event additionally carrying a
# ``agent_transcript_path`` (proof a durable child transcript actually
# exists, not merely that two hook events with a matching id happened to
# appear in the stream), the SAME ``session_id``/``prompt_id``/``agent_type``
# when both events carry one, the Start structurally PRECEDING the Stop in
# the captured stream, a terminal and SUCCESSFUL ``Agent`` tool_use/
# tool_result correlation, and a uniquely-identified candidate chain (never
# an ambiguous choice among multiple equally-qualifying candidates).
# ---------------------------------------------------------------------------

CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED = "hook_id_correlated"
CAUSAL_EVIDENCE_SOURCE_MARKER_ONLY_INSUFFICIENT = "marker_only_insufficient"
CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE = "no_evidence"

# P1-1 hardening: an unbounded transcript read is itself a denial-of-service
# / memory-exhaustion surface for a hook payload path this harness does not
# control the contents of. 25 MiB is generously larger than any real Claude
# Code child transcript this harness has ever observed.
_TRANSCRIPT_MAX_BYTES = 25 * 1024 * 1024


def _extract_assistant_message_texts_from_transcript(content: str) -> list[str]:
    """Issue #2183 PR #2220 P0-2/P1-1 fix-delta: parse ``content`` as a
    Claude Code transcript JSONL file and return ONLY the text of records
    whose ``type`` is ``"assistant"`` -- never a user prompt, tool input, or
    any other record kind. A ``"text"`` content block's own ``text`` field
    is used when the record's ``message.content`` is a list of content
    blocks (the real transcript shape); a bare string
    ``message.content`` is used verbatim (also observed, and used by this
    module's own hermetic fixtures).

    When NO line in ``content`` parses as a JSON object at all (i.e. this is
    not a JSONL transcript, just some opaque text -- an older/simpler test
    fixture shape this function must remain compatible with), the entire
    ``content`` string is returned as a single-element list, preserving the
    substring-search behavior this function's callers relied on before this
    fix-delta. This fallback never applies to a file that DID parse as
    JSONL, even if none of its records were assistant-authored (that must
    still fail closed to no assistant text, not silently degrade to a
    whole-file substring search that could match a user prompt or tool
    input)."""
    texts: list[str] = []
    any_line_parsed = False
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        any_line_parsed = True
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        content_field = message.get("content") if isinstance(message, dict) else None
        if isinstance(content_field, str) and content_field:
            texts.append(content_field)
        elif isinstance(content_field, list):
            for block in content_field:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
    if not any_line_parsed:
        return [content]
    return texts


def _read_claude_agent_transcript_content(
    agent_transcript_path: str | None,
    *,
    allowed_roots: list[str] | None = None,
    run_start_time: float | None = None,
    run_end_time: float | None = None,
    max_bytes: int = _TRANSCRIPT_MAX_BYTES,
) -> dict:
    """Issue #2183 AC11 / PR #2220 P1-1 fix-delta: best-effort read of the
    durable child transcript file a ``SubagentStop`` hook payload claims to
    have written, hardened against a path string that was merely ECHOED in
    a hook payload rather than one that honestly names a durable,
    trustworthy child artifact.

    Returns ``{"content": str|None, "sha256": str|None, "rejected_reason":
    str|None}``. ``content``/``sha256`` are non-``None`` only when
    ``agent_transcript_path`` is a non-empty string naming a file that is
    ALL of: not a symlink (rejected before any further check -- a hook
    payload path is untrusted input and a symlink could point anywhere on
    the filesystem); a REGULAR file; within ``allowed_roots`` when that list
    is given (containment check -- e.g. the worktree or a known session
    directory; skipped entirely, not merely permissive, when
    ``allowed_roots`` is ``None``, preserving this function's prior
    unrestricted behavior for callers that do not supply a root); no larger
    than ``max_bytes``; consistent with ``[run_start_time, run_end_time]``
    (its mtime falls within that window) when both bounds are given; and
    non-empty/non-whitespace-only once read. ``rejected_reason`` names the
    first check that failed (``"missing"``, ``"is_symlink"``,
    ``"not_a_file"``, ``"outside_allowed_roots"``, ``"oversized"``,
    ``"stale_mtime"``, ``"unreadable"``, ``"empty"``) or ``None`` on
    success. A path string that was merely echoed in a hook payload but
    never materialized on disk (or otherwise fails one of these checks)
    must never be trusted as durable evidence -- this function is the
    single place that distinguishes "a path string was reported" from "a
    transcript actually, honestly exists"."""
    result: dict = {"content": None, "sha256": None, "rejected_reason": None}
    if not agent_transcript_path:
        result["rejected_reason"] = "missing"
        return result
    try:
        if os.path.islink(agent_transcript_path):
            result["rejected_reason"] = "is_symlink"
            return result
        if not os.path.exists(agent_transcript_path):
            result["rejected_reason"] = "missing"
            return result
        if not os.path.isfile(agent_transcript_path):
            result["rejected_reason"] = "not_a_file"
            return result
        if allowed_roots:
            real_path = os.path.realpath(agent_transcript_path)
            contained = False
            for root in allowed_roots:
                real_root = os.path.realpath(root)
                if real_path == real_root or real_path.startswith(real_root + os.sep):
                    contained = True
                    break
            if not contained:
                result["rejected_reason"] = "outside_allowed_roots"
                return result
        file_size = os.path.getsize(agent_transcript_path)
        if file_size > max_bytes:
            result["rejected_reason"] = "oversized"
            return result
        if run_start_time is not None and run_end_time is not None:
            mtime = os.path.getmtime(agent_transcript_path)
            if mtime < run_start_time or mtime > run_end_time:
                result["rejected_reason"] = "stale_mtime"
                return result
        with open(agent_transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        result["rejected_reason"] = "unreadable"
        return result
    if not content.strip():
        result["rejected_reason"] = "empty"
        return result
    result["content"] = content
    result["sha256"] = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return result


# Issue #2848 fix-delta: ``tool_use_result.status`` values that mean an Agent
# invocation has been launched but has not yet reached a terminal state. A
# report-less envelope with one of these statuses is a genuinely intermediate
# notification and is NOT a terminal failure; any other non-``completed``
# status (``failed``, ``completed_with_errors``, unknown ...) is.
_CLAUDE_AGENT_INTERMEDIATE_STATUSES = frozenset(
    {"async_launched", "running", "in_progress", "pending"}
)


def _claude_agent_invocation_evidence(
    stdout: str,
    agent_id: str | None,
    *,
    session_id: str | None = None,
    prompt_id: str | None = None,
) -> dict:
    """Issue #2848 fix-delta: per-``Agent``-invocation (``tool_use_id``)
    result consistency, evaluated independently of whether any envelope
    carries a ``handbackReport``. Shared by
    ``_claude_agent_tool_invocation_correlated`` and
    ``_claude_agent_handback_report_text`` so neither can be satisfied by
    "the first convenient result" while another result of the SAME
    invocation contradicts it.

    An invocation is *expected* when at least one ``tool_result`` envelope
    (whose ``tool_use_id`` ties to an earlier ``Agent`` ``tool_use`` in the
    same session/prompt scope) carries ``tool_use_result.agentId ==
    agent_id``. Every envelope of an expected invocation is then examined
    (envelopes of other ``tool_use_id`` values are unrelated and ignored):

    - a different ``agentId`` on the same invocation is a contradiction;
    - ``is_error: True`` is a contradiction, with or without a report;
    - a ``status`` that is neither ``completed`` nor a known intermediate
      status is a terminal failure, hence a contradiction, with or without
      a report;
    - an envelope carrying a ``handbackReport`` key must be ``completed``,
      not errored, and have a non-empty string ``text``, otherwise it is a
      contradiction (an intermediate status with a report is inconsistent);
    - a ``completed`` non-errored envelope without a report, and a
      report-less intermediate envelope, are neutral.

    Returns ``{"correlated": bool, "contradiction": bool,
    "success_seen": bool, "report_texts": list[str]}``. ``success_seen``
    means some ``agentId``-matching envelope was ``completed`` and not
    errored. Identical successful duplicates are not a contradiction."""
    evidence: dict = {
        "correlated": False,
        "contradiction": False,
        "success_seen": False,
        "report_texts": [],
    }
    if not agent_id:
        return evidence
    pending_agent_tool_use_ids: set[str] = set()
    envelopes: list[tuple[str, dict, dict]] = []
    for payload in _iter_claude_stream_events(stdout):
        payload_session_id = payload.get("session_id")
        if not isinstance(payload_session_id, str) or not payload_session_id:
            payload_session_id = payload.get("sessionId")
        payload_prompt_id = payload.get("prompt_id")
        if session_id and isinstance(payload_session_id, str) and payload_session_id:
            if payload_session_id != session_id:
                continue
        if prompt_id and isinstance(payload_prompt_id, str) and payload_prompt_id:
            if payload_prompt_id != prompt_id:
                continue

        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if payload.get("type") == "assistant":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != _CLAUDE_SPAWN_TOOL_NAME:
                    continue
                tool_use_id = block.get("id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    pending_agent_tool_use_ids.add(tool_use_id)
        elif payload.get("type") == "user":
            tool_use_result = payload.get("tool_use_result")
            if not isinstance(tool_use_result, dict):
                tool_use_result = {}
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                if not isinstance(tool_use_id, str) or tool_use_id not in pending_agent_tool_use_ids:
                    continue
                envelopes.append((tool_use_id, tool_use_result, block))

    expected_ids = {
        tool_use_id
        for tool_use_id, tool_use_result, _block in envelopes
        if tool_use_result.get("agentId") == agent_id
    }
    if not expected_ids:
        return evidence
    evidence["correlated"] = True
    for tool_use_id, tool_use_result, block in envelopes:
        if tool_use_id not in expected_ids:
            continue
        envelope_agent_id = tool_use_result.get("agentId")
        if envelope_agent_id is not None and envelope_agent_id != agent_id:
            evidence["contradiction"] = True
            continue
        status = tool_use_result.get("status")
        is_error = block.get("is_error") is True
        completed = status == "completed" and not is_error
        if is_error or (
            isinstance(status, str)
            and status != "completed"
            and status not in _CLAUDE_AGENT_INTERMEDIATE_STATUSES
        ):
            evidence["contradiction"] = True
            continue
        if "handbackReport" in tool_use_result:
            report = tool_use_result.get("handbackReport")
            text = report.get("text") if isinstance(report, dict) else None
            if not completed or not isinstance(text, str) or not text.strip():
                evidence["contradiction"] = True
                continue
            evidence["report_texts"].append(text)
        if completed and envelope_agent_id == agent_id:
            evidence["success_seen"] = True
    return evidence


def _claude_agent_tool_invocation_correlated(
    stdout: str,
    agent_id: str | None,
    *,
    session_id: str | None = None,
    prompt_id: str | None = None,
) -> dict:
    """Issue #2183 AC3 / PR #2220 P0-3 fix-delta: whether the runtime's own
    tool-invocation-ID correlation channel ties the observed ``agent_id``
    back to a specific ``Agent`` tool call this run itself made, rather than
    any bare hook-channel identity floating unattached in the stream --
    additionally requiring that tool call to have occurred within the SAME
    ``session_id``/``prompt_id`` as the correlated Start/Stop pair when
    those values are known, and reporting separately whether the matched
    invocation reached a TERMINAL, SUCCESSFUL state.

    Mirrors the existing ``tool_use_id`` correlation pattern already used by
    ``extract_claude_canonical_read_receipt`` for the ``Read`` tool: an
    ``Agent`` ``tool_use`` block's own ``id`` is matched against a
    ``tool_result`` block's ``tool_use_id`` in the SAME already-captured
    stream, and the matched ``tool_result``'s ``tool_use_result.agentId``
    must equal ``agent_id`` exactly.

    Returns ``{"tool_invocation_id_correlated": bool,
    "terminal_tool_result_success": bool}``. The first field is ``True`` as
    soon as an agentId-matching tool_result is found for ``agent_id`` (in
    the same session/prompt scope, when those were supplied). The second
    field requires (Issue #2848 fix-delta: evaluated over EVERY result of
    that ``tool_use_id`` via ``_claude_agent_invocation_evidence``, not just
    the first) that some matching envelope be ``status == "completed"``
    and not ``is_error: True`` AND that no result of the same invocation
    contradict it (terminal failure, ``is_error``, conflicting ``agentId``).
    Fails closed to ``{False, False}`` on any missing/unmatched id or
    session/prompt mismatch -- never a guess."""
    evidence = _claude_agent_invocation_evidence(
        stdout, agent_id, session_id=session_id, prompt_id=prompt_id
    )
    return {
        "tool_invocation_id_correlated": evidence["correlated"],
        "terminal_tool_result_success": (
            evidence["correlated"] and evidence["success_seen"] and not evidence["contradiction"]
        ),
    }


def _claude_agent_handback_report_text(
    stdout: str,
    agent_id: str | None,
    *,
    session_id: str | None = None,
    prompt_id: str | None = None,
) -> str | None:
    """Issue #2848: the child's own final report text as delivered by the
    Agent tool's ``tool_use_result.handbackReport.text``, used as an
    ADDITIONAL marker-provenance input only when the correlated
    ``SubagentStop`` payload carries no ``last_assistant_message`` (observed
    missing on the measured Claude Code 2.1.285 execution path; the
    ``handbackReport`` was still present there).

    Shares ``_claude_agent_invocation_evidence`` with
    ``_claude_agent_tool_invocation_correlated``. Returns the text only when
    the invocation has no contradiction (terminal failure / ``is_error`` /
    conflicting ``agentId`` on ANY result of the same ``tool_use_id``,
    with or without a ``handbackReport``) and every valid report agrees on
    the same non-empty text. Fails closed to ``None`` otherwise."""
    evidence = _claude_agent_invocation_evidence(
        stdout, agent_id, session_id=session_id, prompt_id=prompt_id
    )
    texts = evidence["report_texts"]
    if not evidence["correlated"] or evidence["contradiction"]:
        return None
    if not texts or len(set(texts)) != 1:
        return None
    return texts[0]


def _marker_provenance_verified(
    expected_markers: list[str] | None,
    last_assistant_message: str | None,
    transcript_content: str | None,
    handback_report_text: str | None = None,
) -> tuple[bool, bool]:
    """Issue #2183 AC11/AC12 / PR #2220 P0-2 fix-delta (further refined by
    the P1-1-vs-``last_assistant_message``-primacy fix-delta below): whether
    EVERY expected marker (not merely at least one, as the pre-fix-delta
    ``any()`` check allowed) is recovered from a CHILD-scoped source -- the
    correlated ``SubagentStop`` event's own ``last_assistant_message``
    field (the primary provenance source: the child's actual final
    response, per the official hook payload contract), and/or the
    assistant-authored records of the child's own transcript file (parsed
    via ``_extract_assistant_message_texts_from_transcript``, which excludes
    user-prompt and tool-input records -- the exact provenance gap the
    P0-2 fix-delta closed: a marker that appears ONLY in the child's own
    prompt or tool input, never in anything the child itself said, must not
    count).

    Returns ``(verified, transcript_fallback_used)``.

    P1-1-vs-primacy fix-delta (live-verified against a real ``--runtime
    claude --mode structured`` run, run_id ``2e93d701786c``, tested_head
    ``28c16484``): when ``last_assistant_message`` ALONE already satisfies
    every expected marker, ``transcript_fallback_used`` is ``False`` -- the
    caller must NOT then also require ``agent_transcript_verified`` (P1-1's
    file-existence/content check) to be ``True``. Structured-lane
    ``--no-session-persistence`` runs have been observed to write only a
    small ``agent-<id>.meta.json`` stub, never the full per-agent
    ``.jsonl`` transcript that P1-1 verifies -- a structural, runtime-mode
    artifact of that lane, not evidence the SubAgent didn't genuinely run.
    ``last_assistant_message`` is the OFFICIAL hook payload's own
    provenance field for "what did this child actually say", independent
    of whether its transcript file was persisted.

    In every other case (``last_assistant_message`` absent, or present but
    not sufficient on its own to cover every expected marker)
    ``transcript_fallback_used`` is ``True`` and the child's own transcript
    file content is folded into the combined text the markers are checked
    against -- exactly the original (pre-this-fix-delta) behavior. This
    keeps ``agent_transcript_verified`` a hard gate for every case that
    still relies, even partially, on the transcript file for marker
    provenance (AC11's file-existence/symlink/size/content checks, and
    AC12's negative control, are unaffected).

    ``verified`` is ``True`` when ``expected_markers`` is empty/``None``
    (no marker provenance claim is being made, so there is nothing to
    fail) -- and in that case ``transcript_fallback_used`` stays ``True``
    (no ``last_assistant_message``-alone fast path applies when there was
    no marker claim to satisfy from it in the first place), preserving the
    pre-existing requirement that a correlated Stop with no expected
    markers still needs a genuinely verified transcript file (AC11).

    Issue #2848: ``handback_report_text`` is the correlated Agent tool
    ``handbackReport.text`` (see ``_claude_agent_handback_report_text``),
    consulted ONLY when ``last_assistant_message`` is absent/empty (it was
    missing on the measured Claude Code 2.1.285 execution path).
    Like ``last_assistant_message`` it is the child's own final report, so
    when it ALONE covers every expected marker the result is
    ``(True, False)`` (no transcript fallback). Otherwise the pre-existing
    logic below is unchanged. When ``last_assistant_message`` is present
    this parameter is ignored entirely."""
    if expected_markers and last_assistant_message:
        if all(marker in last_assistant_message for marker in expected_markers):
            return True, False
    if expected_markers and not last_assistant_message and handback_report_text:
        if all(marker in handback_report_text for marker in expected_markers):
            return True, False
    child_texts: list[str] = []
    if last_assistant_message:
        child_texts.append(last_assistant_message)
    if transcript_content:
        child_texts.extend(_extract_assistant_message_texts_from_transcript(transcript_content))
    if not expected_markers:
        return True, True
    if not child_texts:
        return False, True
    combined = "\n".join(child_texts)
    return all(marker in combined for marker in expected_markers), True


def subagent_causal_evidence_verdict(
    stdout: str,
    expected_markers: list[str] | None = None,
    *,
    transcript_allowed_roots: list[str] | None = None,
    run_start_time: float | None = None,
    run_end_time: float | None = None,
) -> dict:
    """Issue #2183 AC1/AC2/AC3 (PR #2220 P0-2/P0-3 fix-delta strengthened):
    structural, hook-ID-correlated causal evidence that a SubAgent genuinely
    ran, replacing a bare marker-string observation as the PASS-determining
    signal.

    Returns a dict with:

    - ``agent_id``: the correlated ``SubagentStart``/``SubagentStop``
      ``agent_id`` (``None`` if no fully-identified Start/Stop chain
      exists -- see below).
    - ``subagent_start_observed`` / ``subagent_stop_observed``: bool,
      whether at least one hook event of that kind was observed at all
      (independent of correlation).
    - ``agent_transcript_path`` / ``agent_transcript_verified``: as before
      (AC11 -- a transcript path is trusted only once
      ``_read_claude_agent_transcript_content`` verifies it).
    - ``tool_invocation_id_correlated`` (AC3): as before, now scoped to the
      correlated chain's own ``session_id``/``prompt_id``.
    - ``marker_provenance_verified`` (AC11/AC12, P0-2 strengthened): ``True``
      only when EVERY expected marker is recovered from the correlated
      child's own scoped text (see ``_marker_provenance_verified``).
    - ``causal_evidence_source`` (AC2, enum): ``hook_id_correlated`` only
      when a SINGLE, UNAMBIGUOUS Start/Stop chain satisfies ALL of (P0-3
      strengthened -- any single omission, or more than one equally
      qualifying candidate chain, fails closed):

      1. same ``agent_id`` on a Start and a Stop event, with the Start's
         own ``stream_index`` structurally preceding the Stop's;
      2. same ``session_id`` when both events carry one;
      3. same ``prompt_id`` when both events carry one;
      4. same ``agent_type`` when both events carry one;
      5. a recovered, VERIFIED ``agent_transcript_path``;
      6. ``tool_invocation_id_correlated`` AND
         ``terminal_tool_result_success`` (a terminal, successful ``Agent``
         tool_use/tool_result pair, scoped to the same session/prompt);
      7. (only when ``expected_markers`` was supplied)
         ``marker_provenance_verified``.

      An ``agent_id`` with more than one ``SubagentStart`` or more than one
      ``SubagentStop`` event is EXCLUDED from candidacy entirely (P1-2:
      ambiguous multi-Start/multi-Stop chains never correlate). When more
      than one DIFFERENT candidate chain independently satisfies every
      condition above, none is promoted (P0-3's uniqueness requirement).
      ``marker_only_insufficient`` when no hook lifecycle evidence exists
      at all but a caller-supplied ``expected_markers`` string was found in
      ``stdout``; ``no_evidence`` otherwise.

    Fails closed on every field -- a value that cannot be honestly derived
    from the already-captured stream (or, for ``agent_transcript_verified``,
    the filesystem) is left ``None``/``False``/``no_evidence``, never
    guessed or promoted from a weaker signal."""
    events = extract_claude_hook_lifecycle_events(stdout)
    usable_events = [event for event in events if not event["contradictory"]]

    subagent_start_observed = any(event["hook_event"] == "SubagentStart" for event in events)
    subagent_stop_observed = any(event["hook_event"] == "SubagentStop" for event in events)

    starts_by_agent: dict[str, list[dict]] = {}
    stops_by_agent: dict[str, list[dict]] = {}
    for event in usable_events:
        if not event["agent_id"]:
            continue
        bucket = starts_by_agent if event["hook_event"] == "SubagentStart" else stops_by_agent
        bucket.setdefault(event["agent_id"], []).append(event)

    # P1-2: an agent_id with more than one Start or more than one Stop is
    # ambiguous on its own terms -- excluded from candidacy before any
    # further check, never resolved by picking "the first one".
    candidate_pairs: list[tuple[str, dict, dict]] = []
    for agent_id, starts in starts_by_agent.items():
        stops = stops_by_agent.get(agent_id, [])
        if len(starts) != 1 or len(stops) != 1:
            continue
        start, stop = starts[0], stops[0]
        if stop["stream_index"] <= start["stream_index"]:
            continue
        if start["session_id"] and stop["session_id"] and start["session_id"] != stop["session_id"]:
            continue
        if start["prompt_id"] and stop["prompt_id"] and start["prompt_id"] != stop["prompt_id"]:
            continue
        if start["agent_type"] and stop["agent_type"] and start["agent_type"] != stop["agent_type"]:
            continue
        candidate_pairs.append((agent_id, start, stop))

    evaluated_candidates: list[dict] = []
    for agent_id, start, stop in candidate_pairs:
        agent_transcript_path = stop["agent_transcript_path"]
        transcript_read = _read_claude_agent_transcript_content(
            agent_transcript_path,
            allowed_roots=transcript_allowed_roots,
            run_start_time=run_start_time,
            run_end_time=run_end_time,
        )
        transcript_content = transcript_read["content"]
        agent_transcript_verified = transcript_content is not None

        tool_correlation = _claude_agent_tool_invocation_correlated(
            stdout, agent_id, session_id=stop["session_id"], prompt_id=stop["prompt_id"]
        )
        tool_invocation_id_correlated = tool_correlation["tool_invocation_id_correlated"]
        terminal_tool_result_success = tool_correlation["terminal_tool_result_success"]

        # Issue #2848: only when the correlated Stop carries no
        # ``last_assistant_message`` (missing on the measured Claude Code
        # 2.1.285 execution path), consult the
        # correlated Agent tool ``handbackReport.text`` as an additional
        # provenance input. When ``last_assistant_message`` is present
        # nothing below differs from the pre-#2848 behaviour.
        handback_report_text: str | None = None
        if expected_markers and not stop["last_assistant_message"]:
            handback_report_text = _claude_agent_handback_report_text(
                stdout, agent_id, session_id=stop["session_id"], prompt_id=stop["prompt_id"]
            )

        marker_provenance_verified, marker_provenance_transcript_fallback_used = (
            _marker_provenance_verified(
                expected_markers,
                stop["last_assistant_message"],
                transcript_content,
                handback_report_text,
            )
        )
        marker_provenance_handback_report_used = bool(
            handback_report_text
            and marker_provenance_verified
            and not marker_provenance_transcript_fallback_used
        )

        # P1-1-vs-primacy fix-delta (Issue #2183 PR #2220 OWNER P0-2
        # follow-up): ``agent_transcript_verified`` (P1-1's file-existence/
        # symlink/size/content check) is a hard gate ONLY when marker
        # provenance actually relied on the transcript file -- i.e. when
        # ``last_assistant_message`` was absent or, alone, did not already
        # cover every expected marker. When ``last_assistant_message``
        # alone fully satisfied marker provenance, an unverifiable/missing
        # transcript file (e.g. the structured ``--no-session-persistence``
        # lane's ``agent-<id>.meta.json`` stub, which never writes the full
        # ``.jsonl`` transcript) must not, by itself, block promotion --
        # ``agent_transcript_verified`` is recorded as audit metadata only
        # in that case, never consulted for ``qualifies``.
        qualifies = (
            agent_transcript_path is not None
            and tool_invocation_id_correlated
            and terminal_tool_result_success
            and marker_provenance_verified
            and (not marker_provenance_transcript_fallback_used or agent_transcript_verified)
        )
        evaluated_candidates.append(
            {
                "agent_id": agent_id,
                "agent_transcript_path": agent_transcript_path,
                "agent_transcript_verified": agent_transcript_verified,
                "agent_transcript_sha256": transcript_read["sha256"],
                "tool_invocation_id_correlated": tool_invocation_id_correlated,
                "terminal_tool_result_success": terminal_tool_result_success,
                "marker_provenance_verified": marker_provenance_verified,
                "marker_provenance_transcript_fallback_used": (
                    marker_provenance_transcript_fallback_used
                ),
                "last_assistant_message": stop["last_assistant_message"],
                "marker_provenance_handback_report_used": marker_provenance_handback_report_used,
                "qualifies": qualifies,
            }
        )

    fully_qualifying = [candidate for candidate in evaluated_candidates if candidate["qualifies"]]

    chosen: dict | None = None
    causal_evidence_source: str | None = None
    if len(fully_qualifying) == 1:
        chosen = fully_qualifying[0]
        causal_evidence_source = CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    elif len(evaluated_candidates) == 1:
        # Exactly one candidate PAIR correlated structurally (agent_id,
        # ordering, session/prompt/type) but failed a downstream check --
        # surface its diagnostics as the (non-promoted) result rather than
        # silently reporting "no evidence at all".
        chosen = evaluated_candidates[0]

    if chosen is not None:
        agent_id = chosen["agent_id"]
        agent_transcript_path = chosen["agent_transcript_path"]
        agent_transcript_verified = chosen["agent_transcript_verified"]
        tool_invocation_id_correlated = chosen["tool_invocation_id_correlated"]
        marker_provenance_verified = chosen["marker_provenance_verified"]
        marker_provenance_transcript_fallback_used = chosen[
            "marker_provenance_transcript_fallback_used"
        ]
        marker_provenance_handback_report_used = chosen["marker_provenance_handback_report_used"]
    else:
        marker_provenance_handback_report_used = False
        agent_id = None
        agent_transcript_path = None
        agent_transcript_verified = False
        tool_invocation_id_correlated = False
        marker_provenance_verified = not expected_markers
        marker_provenance_transcript_fallback_used = True

    if causal_evidence_source is None:
        if events:
            # Some hook lifecycle evidence was observed (e.g. a lone
            # SubagentStart with no matching SubagentStop, an ambiguous
            # multi-Start/multi-Stop agent_id, a session/prompt/type
            # mismatch, a correlated Stop missing its transcript path or
            # unverifiable transcript, a Stop not tied to a terminal
            # successful Agent tool invocation, a Stop whose expected
            # marker provenance can't be confirmed, or more than one
            # equally-qualifying candidate chain) -- never promoted to
            # correlated, and never re-classified as marker_only_insufficient
            # (hook evidence, even incomplete or ambiguous, is a
            # stronger/different signal than "no hook evidence at all, just
            # a marker").
            causal_evidence_source = CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE
        elif expected_markers and any(marker in stdout for marker in expected_markers):
            causal_evidence_source = CAUSAL_EVIDENCE_SOURCE_MARKER_ONLY_INSUFFICIENT
        else:
            causal_evidence_source = CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE

    verdict = {
        "agent_id": agent_id,
        "subagent_start_observed": subagent_start_observed,
        "subagent_stop_observed": subagent_stop_observed,
        "agent_transcript_path": agent_transcript_path,
        # P1-1-vs-primacy fix-delta (audit metadata only -- see
        # ``_marker_provenance_verified`` and the ``qualifies`` computation
        # above for how this field's meaning as a hard gate is now scoped):
        # ``agent_transcript_verified`` remains the honest P1-1 file
        # existence/symlink/size/content verdict for this candidate's
        # transcript path, but it only participates in
        # ``causal_evidence_source`` promotion when
        # ``marker_provenance_transcript_fallback_used`` is True.
        "agent_transcript_verified": agent_transcript_verified,
        "tool_invocation_id_correlated": tool_invocation_id_correlated,
        "marker_provenance_verified": marker_provenance_verified,
        "marker_provenance_transcript_fallback_used": (
            marker_provenance_transcript_fallback_used
        ),
        "causal_evidence_source": causal_evidence_source,
        "causal_evidence_ambiguous_candidate_count": (
            len(fully_qualifying) if len(fully_qualifying) > 1 else None
        ),
    }
    # Issue #2848: additive audit field, emitted ONLY when marker provenance
    # was actually satisfied from the Agent tool ``handbackReport.text``
    # (``last_assistant_message`` was missing on the measured Claude Code
    # 2.1.285 execution path).
    # Absent otherwise, so every pre-#2848 verdict shape is unchanged.
    if marker_provenance_handback_report_used:
        verdict["marker_provenance_handback_report_used"] = True
    return verdict


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


# ---------------------------------------------------------------------------
# Issue #2219 AC2/AC3/AC6: multi-SubAgent lifecycle (set-operation based,
# extending the single-child classify_claude_child_spawn_agent_id /
# classify_claude_child_completion pair above to the multi-agent case),
# same-main-session-across-turns verification, and forbidden-marker
# scanning.
# ---------------------------------------------------------------------------


def classify_claude_multi_child_lifecycle(stdout: str, min_required: int) -> dict:
    """Set/multiset-operation-based proof that at least ``min_required``
    DISTINCT SubAgents were spawned AND all reached a terminal completion,
    via agent_id EXACT pairing (Issue #2219 AC3) -- extending the existing
    single-child ``classify_claude_child_spawn_agent_id`` /
    ``classify_claude_child_completion`` pair (which only ever binds ONE
    spawn id) to the multi-agent case, reusing the same two evidence
    channels (hook lifecycle events + the ``tool_use_result`` synchronous
    completion shape) without re-deriving their parsing.

    Detects, and fails closed on, every one of the AC4 negative scenarios:
    start-only (``orphan_starts``), stop-only / agent_id mismatch /
    unknown child (``unknown_children``), and duplicate completion
    (``duplicate_completions``) -- any one of these overrides
    ``verified`` to ``False`` even when the required PAIRED count is met.

    Returns ``{"verified": bool, "spawned_agent_ids": list[str],
    "completed_agent_ids": list[str], "paired_agent_ids": list[str],
    "orphan_starts": list[str], "unknown_children": list[str],
    "duplicate_completions": list[str]}``.

    Live verification (Issue #2219 AC12) surfaced that this repo's own
    project ``.claude/settings.json`` wires a ``SubagentStop`` hook, so a
    single real completion is independently corroborated by BOTH the
    ``SubagentStop`` hook event channel and the ``tool_use_result``
    synchronous "completed" shape channel. Treating that cross-channel
    corroboration as a duplicate (as a single flat multiset would) makes
    ``verified`` structurally unreachable in this repo's live environment.
    Duplicate detection is therefore scoped to WITHIN each independent
    evidence channel -- a genuine double-fire of the SAME channel for the
    SAME agent_id still fails closed via ``_pair_agent_lifecycle``'s
    multiset check, exactly as the AC4 poison test expects."""
    spawn_ids: list[str] = []
    hook_stop_ids: list[str] = []
    for event in extract_claude_hook_lifecycle_events(stdout):
        agent_id = event.get("agent_id")
        if not agent_id:
            continue
        if event["hook_event"] == "SubagentStart":
            spawn_ids.append(agent_id)
        elif event["hook_event"] == "SubagentStop":
            hook_stop_ids.append(agent_id)
    tool_result_stop_ids: list[str] = []
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "user":
            continue
        tool_use_result = payload.get("tool_use_result")
        if not isinstance(tool_use_result, dict):
            continue
        agent_id = tool_use_result.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        if agent_id not in spawn_ids:
            spawn_ids.append(agent_id)
        if tool_use_result.get("status") == SPAWN_LAUNCH_MODE_COMPLETED:
            tool_result_stop_ids.append(agent_id)
    # Issue #2219 fix_delta iteration 2: an async-launched spawn's own
    # tool_use_result never transitions to "completed" in place (see
    # extract_claude_task_notification_completions docstring) -- only
    # bind a task-notification completion to a spawn_id ALREADY observed
    # above, so this can never itself manufacture a spawn event.
    notification_stop_ids: list[str] = [
        agent_id for agent_id in extract_claude_task_notification_completions(stdout) if agent_id in spawn_ids
    ]

    stop_channels = (hook_stop_ids, tool_result_stop_ids, notification_stop_ids)
    duplicate_within_channel: set[str] = set()
    for channel in stop_channels:
        seen: set[str] = set()
        for agent_id in channel:
            if agent_id in seen:
                duplicate_within_channel.add(agent_id)
            seen.add(agent_id)
    stop_ids: list[str] = []
    merged_seen: set[str] = set()
    for channel in stop_channels:
        for agent_id in channel:
            if agent_id not in merged_seen:
                stop_ids.append(agent_id)
                merged_seen.add(agent_id)
    # Re-inject genuine within-channel duplicates so the shared multiset
    # check in ``_pair_agent_lifecycle`` still fails closed on them.
    stop_ids.extend(sorted(duplicate_within_channel))

    return _pair_agent_lifecycle(spawn_ids, stop_ids, min_required)


def _pair_agent_lifecycle(spawn_ids: list[str], stop_ids: list[str], min_required: int) -> dict:
    """Shared agent_id set/multiset-operation pairing core (Issue #2219 AC3,
    factored out in the AC13-AC17 hook-event-evidence-channel fix_delta so
    ``classify_claude_hook_sink_multi_child_lifecycle`` -- which sources its
    ``spawn_ids``/``stop_ids`` from the interactive lane's durable hook sink
    JSONL rather than structured-lane stdout -- reuses the EXACT SAME
    fail-closed pairing algorithm as ``classify_claude_multi_child_lifecycle``
    instead of re-deriving a separate, looser classifier (OWNER anchor
    decision, Issue #2219 body: "既存の classify_claude_multi_child_lifecycle()
    のロジックを...再利用し、structured lane 用と別のより緩い classifier を
    新設しない")."""
    spawned = set(spawn_ids)
    completed = set(stop_ids)
    duplicate_completions = sorted({aid for aid in stop_ids if stop_ids.count(aid) > 1})
    unknown_children = sorted(completed - spawned)
    orphan_starts = sorted(spawned - completed)
    paired = spawned & completed
    verified = (
        len(paired) >= max(min_required, 1)
        and not orphan_starts
        and not unknown_children
        and not duplicate_completions
    )
    return {
        "verified": verified,
        "spawned_agent_ids": sorted(spawned),
        "completed_agent_ids": sorted(completed),
        "paired_agent_ids": sorted(paired),
        "orphan_starts": orphan_starts,
        "unknown_children": unknown_children,
        "duplicate_completions": duplicate_completions,
    }


def extract_claude_stream_session_ids(stdout: str) -> list[str]:
    """Every DISTINCT ``session_id``/``sessionId`` value observed across
    the stream-json output, IN ORDER of first appearance (Issue #2219
    AC2) -- unlike ``extract_claude_parent_session_id`` (which returns
    only the first value seen and stops), this sees a session id that
    silently changed mid-stream instead of masking it."""
    seen: list[str] = []
    for payload in _iter_claude_stream_events(stdout):
        for key in ("session_id", "sessionId"):
            value = payload.get(key)
            if isinstance(value, str) and value and value not in seen:
                seen.append(value)
    return seen


def count_claude_stream_turns(stdout: str) -> int:
    """Number of assistant-message turns observed in a single
    structured-lane stream-json invocation (Issue #2219 AC2). Each
    ``type: "assistant"`` event is one agentic-loop turn -- counted
    independently of the runtime's own self-declared ``--max-turns``
    bound, never assumed equal to it."""
    return sum(
        1 for payload in _iter_claude_stream_events(stdout)
        if payload.get("type") == "assistant"
    )


def verify_same_main_session_across_turns(stdout: str, min_turns: int) -> dict:
    """Fail-closed proof that the SAME main session_id persisted across at
    least ``min_turns`` agentic-loop turns (Issue #2219 AC2).

    Design choice (documented per the Issue's own two-option framing, and
    revised by fix_delta iteration 1 -- pr-reviewer REQUEST_CHANGES,
    https://github.com/squne121/loop-protocol/pull/2222): this function is
    Option A's own primitive -- a single ``claude -p --output-format
    stream-json --max-turns >= min_turns`` invocation's own internal
    agentic loop, verified via the native ``session_id`` field that is
    already present on every stream-json event -- and remains the
    structured lane's wiring. Iteration 1's OWNER decision procedure
    (Issue #2219 body) required attempting Option B FIRST (re-implementing
    an ``--additional-prompt``-like flag for the interactive herdr lane,
    as PR #2176 prototyped in commit 06d8baa9 and reverted in commit
    5a44ebf0 -- a SCOPE decision for Issue #2174, not a technical
    rejection) before falling back to Option A; the prior iteration
    skipped that attempt and was rejected. Option B is now ALSO
    implemented (``run_interactive_herdr_isolated``'s ``additional_prompts``
    parameter, driven by the new ``--additional-prompt`` CLI flag) and
    reuses THIS SAME function as a shared building block: the interactive
    lane's own persisted session transcript (see
    ``_find_claude_interactive_transcript`` -- the interactive lane never
    passes ``--no-session-persistence``, so Claude Code persists it) is
    fed into this function exactly like structured-lane stdout is, so
    "same session identity across >= 2 turns" is verified identically
    across both lanes. Both A and B remain available; the structured
    lane's ``--require-min-turns``/``--max-turns`` combination still
    exercises Option A's own agentic-loop path unchanged.

    Returns ``{"verified": bool, "turn_count": int, "session_ids_observed":
    list[str], "lane": "option_a_single_invocation_agentic_loop"}``.
    ``verified`` is ``True`` only when EXACTLY ONE distinct session_id was
    observed AND ``turn_count >= min_turns``; zero or multiple distinct
    ids, or too few turns, both fail closed to ``False`` -- never
    guessed."""
    session_ids = extract_claude_stream_session_ids(stdout)
    turn_count = count_claude_stream_turns(stdout)
    verified = len(session_ids) == 1 and turn_count >= max(min_turns, 1)
    return {
        "verified": verified,
        "turn_count": turn_count,
        "session_ids_observed": session_ids,
        "lane": "option_a_single_invocation_agentic_loop",
    }


# Issue #2219 AC6: fixed, literal allowlist of forbidden failure markers.
# Deliberately literal substring matching only -- no fuzzy/regex matching --
# so this never over-matches unrelated text that merely resembles one of
# these fixed strings.
_FORBIDDEN_FAILURE_MARKERS = (
    "403 WebSocket upgrade",
    "WebSocket upgrade was rejected",
    "Please run /login",
    "early termination",
    "context limit",
    "auto-compaction failure",
)


def verify_no_forbidden_marker(
    stdout: str, stderr: str, herdr_pane_excerpt: str | None = None
) -> dict:
    """Fail-closed scan for the fixed ``_FORBIDDEN_FAILURE_MARKERS``
    allowlist (Issue #2219 AC6) across every text channel this harness
    captures (structured lane stdout/stderr, or the interactive lane's own
    redacted pane excerpt).

    Returns ``{"verified": bool, "matched_markers": list[str]}``.
    ``verified`` is ``True`` (PASS) only when the allowlist matched ZERO
    markers across all supplied channels."""
    combined = "\n".join(
        text for text in (stdout, stderr, herdr_pane_excerpt) if isinstance(text, str)
    )
    matched = [marker for marker in _FORBIDDEN_FAILURE_MARKERS if marker in combined]
    return {"verified": not matched, "matched_markers": matched}


# ---------------------------------------------------------------------------
# Issue #2219 AC2/AC3/AC13-AC17 (OWNER anchor decision, 2026-08-16): the
# interactive-lane hook-event evidence channel. Live investigation proved
# herdr-PTY-driven claude-gpt sessions never write a flat ``<session-id>.jsonl``
# main transcript (see ``_find_claude_interactive_transcript`` docstring and
# the Issue body Notes for Reviewer for the full incident trail), so this
# channel replaces transcript-existence as the interactive lane's PASS
# authority. Every function below consumes already-parsed hook sink RECORDS
# (never the raw sink file text, never raw prompt/response content) -- the
# sink itself is written by a launcher-owned (``launch.sh``, "claude-gpt") or
# harness-owned (native adapter) fixed hook command, never from a
# caller-influenced string.
# ---------------------------------------------------------------------------

# The ONLY keys a well-formed hook sink record may carry (AC13: no raw
# prompt/response/credential/token content ever appears in a record).
_HOOK_SINK_ALLOWED_RECORD_KEYS = frozenset(
    {"run_nonce", "event", "session_id", "agent_id", "ts", "prompt_digest"}
)
_HOOK_SINK_LIFECYCLE_EVENTS = frozenset(
    {"UserPromptSubmit", "Stop", "StopFailure", "SubagentStart", "SubagentStop"}
)
# Bounded read: a single sink file is never trusted to be arbitrarily large
# (defense in depth against a corrupted/runaway sink being read wholesale).
_MAX_HOOK_SINK_LINES = 20000

# Native-adapter hook sink writer (Issue #2219 In Scope: native adapter is
# wired entirely from THIS harness, never scripts/claude-gpt/**). Same
# record schema and same single-bounded-``printf``-equivalent-write
# atomicity guarantee (AC15: one ``open(..., "a")`` + one ``write()`` call
# per invocation, each record well under PIPE_BUF) as the claude-gpt
# adapter's inline hook command in ``scripts/claude-gpt/launch.sh``'s
# ``hook-sink-multi-turn`` gate -- kept in sync deliberately (same record
# shape, same field names) so both adapters' sinks are parsed by the exact
# same ``parse_claude_gpt_hook_sink_records``.
_HOOK_SINK_WRITER_SOURCE = '''\
import hashlib
import json
import os
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id") or payload.get("subagent_id")
    nonce = os.environ.get("CLAUDE_GPT_HOOK_SINK_NONCE", "")
    sink_path = os.environ.get("CLAUDE_GPT_HOOK_SINK_PATH")
    prompt = payload.get("prompt") if event == "UserPromptSubmit" else None
    digest = None
    if isinstance(prompt, str):
        digest = hashlib.sha256((nonce + prompt).encode("utf-8")).hexdigest()
    record = {
        "run_nonce": nonce,
        "event": event,
        "session_id": session_id,
        "agent_id": agent_id,
        "ts": __import__("time").time(),
        "prompt_digest": digest,
    }
    line = json.dumps(record, separators=(",", ":"))
    if sink_path:
        # Single bounded write per record (AC15): one open + one write
        # call, well under PIPE_BUF, so concurrent SubagentStart events
        # never interleave/corrupt lines.
        with open(sink_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\\n")


if __name__ == "__main__":
    main()
'''


def parse_claude_gpt_hook_sink_records(sink_path: str | Path) -> tuple[list[dict], int]:
    """Bounded, fail-closed parse of the append-only hook-event sink JSONL
    file (Issue #2219 AC13/AC15). Returns ``(records, malformed_line_count)``
    -- ``records`` contains ONLY well-formed lines (valid JSON object, a
    recognized ``event`` value, and no key outside
    ``_HOOK_SINK_ALLOWED_RECORD_KEYS``); every other line (parse failure,
    non-dict payload, unrecognized event, or an extra/unexpected key --
    which would indicate either sink corruption from an interleaved
    concurrent write, AC15, or a smuggled raw-content field, AC13) is
    counted in ``malformed_line_count`` and silently dropped from
    ``records`` rather than guessed at or repaired. Missing file (sink never
    configured, or the lane never ran) returns ``([], 0)``, never raises."""
    try:
        path = Path(sink_path)
        if not path.is_file():
            return [], 0
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 0
    records: list[dict] = []
    malformed = 0
    for line in raw_lines[:_MAX_HOOK_SINK_LINES]:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        if not set(payload.keys()) <= _HOOK_SINK_ALLOWED_RECORD_KEYS:
            malformed += 1
            continue
        if payload.get("event") not in _HOOK_SINK_LIFECYCLE_EVENTS:
            malformed += 1
            continue
        records.append(payload)
    return records, malformed


def classify_claude_hook_sink_multi_child_lifecycle(records: list[dict], min_required: int) -> dict:
    """Multi-SubAgent lifecycle proof sourced from hook sink records (Issue
    #2219 AC3/AC17), via the SAME ``_pair_agent_lifecycle`` set-operation
    core ``classify_claude_multi_child_lifecycle`` uses for the structured
    lane -- never a separately re-derived, looser classifier. A process
    killed before its ``SubagentStop`` fires (Issue #2219 AC17, e.g.
    SIGKILL) leaves its ``agent_id`` in ``orphan_starts``, which fails
    ``verified`` closed with no grace-window/timeout-based auto-PASS."""
    spawn_ids = [r["agent_id"] for r in records if r.get("event") == "SubagentStart" and r.get("agent_id")]
    stop_ids = [r["agent_id"] for r in records if r.get("event") == "SubagentStop" and r.get("agent_id")]
    return _pair_agent_lifecycle(spawn_ids, stop_ids, min_required)


def verify_claude_gpt_hook_sink_multi_turn(records: list[dict], min_turns: int) -> dict:
    """Multi-turn same-session proof sourced from hook sink records (Issue
    #2219 AC2), independent of any persisted transcript file. ``verified``
    requires: (1) exactly one distinct ``session_id`` observed across
    ``UserPromptSubmit`` records, (2) at least ``min_turns`` such records for
    that session_id, (3) a ``Stop`` record for that SAME session_id for each
    ``UserPromptSubmit`` (paired count, not merely an aggregate count -- a
    stalled/never-completed turn is not silently counted as done), and (4)
    zero ``StopFailure`` records for that session_id. Zero or multiple
    distinct session_ids both fail closed.

    Returns ``{"verified": bool, "session_id": str|None, "turn_count": int,
    "stop_count": int, "stop_failure_count": int}``."""
    prompt_sids = [r["session_id"] for r in records if r.get("event") == "UserPromptSubmit" and r.get("session_id")]
    stop_sids = [r["session_id"] for r in records if r.get("event") == "Stop" and r.get("session_id")]
    stop_failure_sids = [
        r["session_id"] for r in records if r.get("event") == "StopFailure" and r.get("session_id")
    ]
    distinct_prompt_sids = sorted(set(prompt_sids))
    session_id = distinct_prompt_sids[0] if len(distinct_prompt_sids) == 1 else None
    turn_count = prompt_sids.count(session_id) if session_id else 0
    stop_count = stop_sids.count(session_id) if session_id else 0
    stop_failure_count = stop_failure_sids.count(session_id) if session_id else len(stop_failure_sids)
    verified = (
        session_id is not None
        and turn_count >= max(min_turns, 1)
        and stop_count >= turn_count
        and stop_failure_count == 0
    )
    return {
        "verified": verified,
        "session_id": session_id,
        "turn_count": turn_count,
        "stop_count": stop_count,
        "stop_failure_count": stop_failure_count,
    }


def verify_claude_gpt_hook_sink_not_stale(records: list[dict], expected_nonce: str | None) -> dict:
    """Sink-specific staleness guard (Issue #2219 AC16), deliberately
    SEPARATE from ``verify_evidence_not_stale`` (which is repo-state/
    ``tested_head`` based, not sink-based). Verifies the 3-way ``run_nonce``
    match the Issue body requires: the nonce baked into ``settings.json`` at
    launch, the nonce THIS harness invocation expects (``expected_nonce``),
    and the ``run_nonce`` stamped on EVERY record actually observed in the
    sink -- a single mismatching or missing-nonce record fails the whole
    check closed (never "mostly fresh"). An empty ``records`` list (sink
    never populated) or a missing/empty ``expected_nonce`` also fails
    closed."""
    if not expected_nonce:
        return {"verified": False, "reason": "expected_nonce_missing", "mismatched_count": 0}
    if not records:
        return {"verified": False, "reason": "no_records", "mismatched_count": 0}
    mismatched = [r for r in records if r.get("run_nonce") != expected_nonce]
    return {
        "verified": not mismatched,
        "reason": None if not mismatched else "run_nonce_mismatch",
        "mismatched_count": len(mismatched),
    }


def verify_hook_sink_not_stale(records: list[dict], expected_nonce: str | None) -> dict:
    """Issue #2219 AC16 VC literal-name alias for
    ``verify_claude_gpt_hook_sink_not_stale`` -- kept as a distinct symbol
    (not a bare assignment) so the AC16 Verification Command
    (``rg -n "def verify_hook_sink_not_stale"``) matches a real function
    definition."""
    return verify_claude_gpt_hook_sink_not_stale(records, expected_nonce)


def verify_claude_gpt_hook_sink_no_raw_content(records: list[dict]) -> dict:
    """Structural, fail-closed proof that no sink record smuggles raw
    prompt/response text or a credential/token (Issue #2219 AC13). Because
    ``parse_claude_gpt_hook_sink_records`` already drops any record carrying
    a key outside the fixed allowlist, this only needs to additionally
    check the ONE field that legitimately carries prompt-derived content --
    ``prompt_digest`` -- is always either absent/``None`` or a 64-hex-
    character SHA-256 digest, never raw text."""
    violations: list[str] = []
    for record in records:
        digest = record.get("prompt_digest")
        if digest is None:
            continue
        if not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
            violations.append(str(record.get("event")))
    return {"verified": not violations, "violating_events": violations}


def claude_gpt_proxy_state_dir_python() -> Path:
    """Python-side mirror of ``scripts/claude-gpt/lib.sh``'s
    ``claude_gpt_proxy_state_dir()`` -- ``$CLAUDE_GPT_HOME/state`` (default
    ``~/.claude-gpt/state`` when ``CLAUDE_GPT_HOME`` is unset). Mirrors the
    launcher's own default expression exactly (same pattern as
    ``_resolve_claude_projects_root``) rather than re-deriving a different
    one, so an operator-set ``CLAUDE_GPT_HOME`` is honored identically on
    both sides. This IS the "launcher-owned constant" the Issue body
    requires the sink path be built from (AC14) -- never any
    caller-supplied value (worktree path, CLI argument, etc.)."""
    claude_gpt_home = os.environ.get("CLAUDE_GPT_HOME") or str(Path.home() / ".claude-gpt")
    return Path(claude_gpt_home) / "state"


def claude_gpt_hook_sink_path(nonce: str) -> Path:
    """Deterministic sink path for the ``claude-gpt`` adapter (Issue #2219
    AC14), built ONLY from ``claude_gpt_proxy_state_dir_python()`` (a
    launcher-owned constant) and the run nonce -- never from ``worktree``,
    CLI args, or any other caller-supplied value. Must byte-for-byte match
    the path ``scripts/claude-gpt/launch.sh`` computes for the same nonce
    (see the ``hook-sink-multi-turn`` gate there)."""
    return claude_gpt_proxy_state_dir_python() / f"hook-sink-{nonce}.jsonl"


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


# ---------------------------------------------------------------------------
# Issue #2219 fix_delta iteration 1 (Option B reintroduction): the interactive
# herdr lane has no native ``--output-format stream-json`` stdout stream (see
# ``run_interactive_herdr_isolated``'s own AC5 note -- structured-only flags
# are never forwarded to the TUI launch). It DOES, however, never pass
# ``--no-session-persistence`` (that flag is structured-lane only), so Claude
# Code persists this run's own session transcript to a
# ``<projects-root>/<cwd-slug>/<session_id>.jsonl`` file (see
# ``extract_claude_child_session_id``'s fallback path) -- containing the SAME
# stream-json event shapes (``session_id``/``sessionId``, ``type:
# "assistant"``, ``tool_use_result.agentId``/``agentType``) that the
# structured lane's Option A functions (``extract_claude_stream_session_ids``,
# ``count_claude_stream_turns``, ``verify_same_main_session_across_turns``,
# ``classify_claude_multi_child_lifecycle``) already parse. Locating and
# reading this persisted transcript is therefore the wiring point that lets
# the interactive lane reuse those exact functions as shared building blocks
# instead of re-deriving equivalent logic for a pane-text transcript (which
# cannot reliably distinguish a genuine multi-agent hook event from ordinary
# TUI prose -- see the existing documented ``spawn_events: None`` gap for
# this lane).
#
# Issue #2219 fix_delta iteration 2 (live verification finding against the
# real claude-gpt adapter, https://github.com/squne121/loop-protocol/pull/2222#issuecomment-5307351011):
# ``<projects-root>`` above is NOT always ``~/.claude/projects``. The
# ``claude-gpt`` adapter isolates its whole Claude Code config root to
# ``$CLAUDE_GPT_HOME/claude`` (default ``~/.claude-gpt/claude`` -- see
# ``scripts/claude-gpt/lib.sh``'s ``claude_gpt_claude_config_dir``, exported
# as ``CLAUDE_CONFIG_DIR`` by ``launch.sh`` before the isolated session ever
# starts), so its session transcript is persisted under
# ``$CLAUDE_GPT_HOME/claude/projects`` instead -- the old hardcoded
# ``~/.claude/projects`` scan could never find it, producing a false
# ``interactive_transcript_found: False``. Live filesystem inspection of a
# real isolated claude-gpt session
# (``~/.claude-gpt/claude/projects/<cwd-slug>/<session-id>.jsonl``) confirms
# the SAME flat, single-file, native stream-json-shaped transcript format the
# native adapter writes -- there is no adapter-specific transcript shape to
# special-case here; only the ROOT directory differs, and it differs for the
# exact same reason (and using the exact same resolution logic) the launcher
# itself already isolates it. ``_resolve_claude_projects_root`` below mirrors
# ``lib.sh``'s own default expression exactly, rather than re-deriving a
# different one, so an operator-set ``CLAUDE_GPT_HOME`` is honored
# identically on both sides.
# ---------------------------------------------------------------------------


def _resolve_claude_projects_root(claude_adapter: str) -> Path:
    """The Claude Code ``projects`` directory root this run's own session
    transcript was actually persisted under, based on which adapter
    launched it (Issue #2219 fix_delta iteration 2). ``native`` (the
    default) uses ``~/.claude/projects`` directly. ``claude-gpt`` isolates
    its Claude Code config root to ``$CLAUDE_GPT_HOME/claude`` (default
    ``~/.claude-gpt/claude`` when ``CLAUDE_GPT_HOME`` is unset -- mirrors
    ``scripts/claude-gpt/lib.sh``'s ``claude_gpt_claude_config_dir``
    default expression exactly), so its transcript lives under
    ``$CLAUDE_GPT_HOME/claude/projects`` instead."""
    if claude_adapter == "claude-gpt":
        claude_gpt_home = os.environ.get("CLAUDE_GPT_HOME") or str(Path.home() / ".claude-gpt")
        return Path(claude_gpt_home) / "claude" / "projects"
    return Path.home() / ".claude" / "projects"


def _find_claude_interactive_transcript(
    worktree: str, since_epoch: float, claude_adapter: str = "native"
) -> Path | None:
    """Best-effort, content-linked (never filename-guessed) locate of the
    persisted Claude Code session transcript this interactive-lane run
    itself wrote, by scanning every ``*/*.jsonl`` file under the adapter's
    own resolved projects root (see ``_resolve_claude_projects_root``) for
    one whose (a) mtime is at or after ``since_epoch`` (a small 2s grace
    window absorbs clock/flush skew) and (b) contains a ``cwd`` field
    (checked across the first ``_TRANSCRIPT_CWD_SCAN_LINES`` lines, not just
    the first -- Issue #2219 fix_delta iteration 2 live finding: a real
    Claude Code transcript's first line(s) are session-bookkeeping records
    with no ``cwd`` field at all; ``cwd`` only appears once the first actual
    message record is written, typically the 3rd-4th line, so a first-line-
    only check silently failed to ever find a real transcript, native or
    claude-gpt alike) equal to ``worktree`` exactly -- so a concurrent or
    leftover session for a DIFFERENT worktree can never be mistaken for this
    run's own transcript. When multiple candidates match (e.g. a stale file
    from an earlier run against the same worktree that happens to satisfy
    the mtime window), the most recently modified one is returned. Returns
    ``None`` (never a guess) if no matching file is found or the scan itself
    fails (missing projects directory, permission error, etc.)."""
    try:
        projects_dir = _resolve_claude_projects_root(claude_adapter)
        if not projects_dir.is_dir():
            return None
        best: tuple[float, Path] | None = None
        for candidate in projects_dir.glob("*/*.jsonl"):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime < since_epoch - 2.0:
                continue
            try:
                matched_cwd = False
                with candidate.open(encoding="utf-8", errors="replace") as handle:
                    for _ in range(_TRANSCRIPT_CWD_SCAN_LINES):
                        line = handle.readline()
                        if not line:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if not isinstance(record, dict):
                            continue
                        if "cwd" not in record:
                            continue
                        matched_cwd = record.get("cwd") == worktree
                        break
            except OSError:
                continue
            if not matched_cwd:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, candidate)
        return best[1] if best else None
    except OSError:
        return None


def _find_claude_interactive_subagent_only_session_dirs(
    worktree: str, since_epoch: float, claude_adapter: str = "native"
) -> list[str]:
    """Issue #2219 fix_delta iteration 2 LIVE re-verification finding
    (https://github.com/squne121/loop-protocol/pull/2222, live run against
    the real claude-gpt adapter after the iteration-2 fixes above): every
    session directory this scan can find in the mtime window that contains
    ``subagents/*.meta.json`` spawn evidence (genuine SubAgent activity
    confirmed on disk) but NO sibling flat ``<session-id>.jsonl`` transcript
    file at all -- unlike the flat-transcript shape
    ``_find_claude_interactive_transcript`` looks for above (confirmed
    present for OLDER/manually-driven sessions inspected during this same
    investigation), this harness's own herdr-PTY-automation-driven
    claude-gpt interactive sessions in this environment were live-observed
    to NEVER produce a flat transcript at all -- reproducibly, across four
    separate real runs against the same worktree, spanning hours -- only
    the per-SubAgent ``subagents/*.meta.json`` spawn metadata (Issue #2219
    fix_delta iteration 2's own live verification requirement) is ever
    written for THIS invocation shape. That metadata alone cannot supply an
    honest ``cwd`` match (no such field) or a completion signal (no such
    field either -- see references/claude-code.md), so it is NOT promoted
    to a PASS-capable evidence source here; this function exists ONLY to
    surface a non-fabricated diagnostic breadcrumb (an advisory list of
    matching session directory paths) for a human/follow-up investigation,
    never to satisfy ``--require-min-subagents``/``--require-min-turns``.
    Returns an empty list (never a guess) on any lookup failure."""
    try:
        projects_dir = _resolve_claude_projects_root(claude_adapter)
        if not projects_dir.is_dir():
            return []
        found: list[str] = []
        for candidate in projects_dir.glob("*/*"):
            if not candidate.is_dir():
                continue
            subagents_dir = candidate / "subagents"
            if not subagents_dir.is_dir():
                continue
            if not any(subagents_dir.glob("*.meta.json")):
                continue
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime < since_epoch - 2.0:
                continue
            sibling_transcript = candidate.parent / f"{candidate.name}.jsonl"
            if sibling_transcript.exists():
                continue
            found.append(str(candidate))
        return sorted(found)
    except OSError:
        return []


# Issue #2161 (native Codex CLI retirement): extract_codex_parent_session_id(),
# _codex_agent_id_from_spawn_agent_calls(), _find_codex_child_session_meta(),
# extract_codex_child_session_id(), extract_codex_child_agent_role(), and
# classify_codex_events() were removed along with the native Codex CLI
# ``codex`` runtime lane (rollout-log scanning under the native Codex
# CLI's home-directory session state).


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
    # Issue #1881 PR #2385 fix_delta (Extension 2): the sole tracked
    # reference `pr-reviewer` is expected to Read (allowed-paths-gate
    # canonical reference). `extract_claude_canonical_read_receipt` below
    # is genuinely persona-agnostic (it only ever consumes
    # `expected_rel_path` as a plain argument), so this is a pure allowlist
    # addition -- no other code path changes.
    "pr-reviewer": ".claude/skills/pr-review-judge/references/allowed-paths-gate.md",
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
                # Issue #1881 PR #2385 fix_delta (Extension 1): fall back to
                # a plain-text ``agent_type=<value>`` marker (see
                # ``.claude/hooks/pr_reviewer_guard.py``'s ``observe-identity``
                # opt-in probe channel) ONLY when the JSON-object recognition
                # path above found nothing on this same text. This is a new,
                # additional recognition path; it never runs when a JSON
                # object was already found, so every existing JSON-payload
                # caller's behavior stays byte-identical.
                if result["agent_type"] is None:
                    match = _PLAIN_AGENT_TYPE_MARKER_RE.search(text)
                    if match:
                        result["agent_type"] = match.group(1)
                        result["source"] = AGENT_TYPE_SOURCE_PLAIN_MARKER
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


def _herdr_sessions(herdr_bin: str, env: dict[str, str] | None = None) -> list[dict] | None:
    """List every herdr session the local supervisor currently knows about.

    Issue #2176 (P0-3 fix-delta): accepts an explicit ``env`` so explicit
    baseline-preservation opt-in callers use stripped environment identity.
    """
    rc, out, _err, _timed_out = _run(
        [herdr_bin, "session", "list", "--json"], timeout=15.0, env=env
    )
    if rc != 0:
        return None
    try:
        payload = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        # Issue #2174 AC7 fail-closed requirement (PR #2176 OWNER
        # REQUEST_CHANGES Finding 3): a malformed/unexpected payload shape
        # is an UNOBTAINABLE session list, never an empty one. Silently
        # returning [] here previously let a genuinely-unreadable session
        # list masquerade as "there are no other sessions to protect".
        return None
    return [entry for entry in sessions if isinstance(entry, dict)]


def _herdr_session_names(herdr_bin: str, env: dict[str, str] | None = None) -> set[str] | None:
    sessions = _herdr_sessions(herdr_bin, env=env)
    if sessions is None:
        return None
    return {str(entry["name"]) for entry in sessions if entry.get("name")}


# Only stable, non-secret identity fields are kept in a baseline snapshot --
# never a transcript, pane content, or credential-bearing field. herdr
# session ids/names are not considered secrets (see caller instructions),
# but nothing beyond bare session-registry identity is captured here.
_SESSION_BASELINE_FIELDS = ("name", "running", "default", "socket_path", "session_dir")


def snapshot_herdr_sessions(herdr_bin: str, env: dict[str, str] | None) -> list[dict] | None:
    """A normalized, name-sorted snapshot of every herdr session the local
    supervisor currently knows about (Issue #2176 P0-3: existing human
    session baseline preservation).

    Returns ``None`` if the session list itself could not be retrieved.
    ``None`` MUST be treated by callers as "could not evaluate" -- never as
    "no sessions" -- so a baseline comparison that cannot actually observe
    the supervisor never silently reports success (fail-closed).
    """
    sessions = _herdr_sessions(herdr_bin, env=env)
    if sessions is None:
        return None
    normalized = [
        {field: entry.get(field) for field in _SESSION_BASELINE_FIELDS}
        for entry in sessions
    ]
    return sorted(normalized, key=lambda item: str(item.get("name")))


def diff_herdr_session_baseline(
    before: list[dict] | None,
    after: list[dict] | None,
    *,
    new_session_names: set[str],
) -> list[str]:
    """Compare two ``snapshot_herdr_sessions`` results, ignoring only the
    isolated session(s) this specific run itself created and is
    responsible for cleaning up (``new_session_names``).

    Any OTHER addition, removal, or field change to a pre-existing session
    -- including the caller's own attached human session -- is reported as
    a baseline-preservation violation. ``None`` on either side means the
    listing itself failed and cannot be evaluated; that is reported as a
    violation too (fail-closed), never silently treated as "no change".
    """
    if before is None or after is None:
        return ["could not evaluate herdr session baseline (session list failed)"]
    before_by_name = {
        str(item["name"]): item for item in before if str(item.get("name")) not in new_session_names
    }
    after_by_name = {
        str(item["name"]): item for item in after if str(item.get("name")) not in new_session_names
    }
    diffs: list[str] = []
    for name in sorted(set(before_by_name) | set(after_by_name)):
        if before_by_name.get(name) != after_by_name.get(name):
            diffs.append(
                f"session baseline changed: {name} "
                f"({before_by_name.get(name)} -> {after_by_name.get(name)})"
            )
    return diffs


# ---------------------------------------------------------------------------
# Human Herdr session FULL snapshot preservation (Issue #2174 AC7)
#
# ``snapshot_herdr_sessions``/``diff_herdr_session_baseline`` above (Issue
# #2176 P0-3) already cover session-LEVEL identity (name/default/running/
# socket_path/session_dir), but AC7 additionally requires workspace ID,
# agent ID, and the currently focused/active workspace-tab-pane selection
# to be verified unchanged. ``herdr api snapshot`` (confirmed against
# installed herdr v0.8.0) is the real endpoint that carries this: each
# ``result.snapshot.agents[]`` entry has ``workspace_id``/``tab_id``/
# ``pane_id``, and the top-level snapshot carries ``focused_workspace_id``/
# ``focused_tab_id``/``focused_pane_id`` (the human's active selection).
# Any required field being unobtainable fails the WHOLE snapshot closed
# (``None``) -- never a partial/best-effort snapshot (AC7's explicit
# fail-closed requirement).
# ---------------------------------------------------------------------------

_WORKSPACE_SNAPSHOT_AGENT_FIELDS = ("agent", "terminal_id", "workspace_id", "tab_id", "pane_id")
_WORKSPACE_SNAPSHOT_FOCUS_FIELDS = ("focused_workspace_id", "focused_tab_id", "focused_pane_id")


def _normalize_agent_session(record: dict) -> tuple | None:
    """Normalize herdr's native ``agent_session`` object (``kind``/``source``/
    ``value``) into a hashable, comparable tuple, or ``None`` when absent
    (e.g. a non-agent pane). ``value`` is the native per-agent session
    identity herdr itself hands out (confirmed against live ``herdr api
    snapshot`` v0.8.0 output) -- the strongest actually-available identity
    signal, used in place of the "session ID" AC7 originally asked for
    (see the module-level upstream-reality note below)."""
    session = record.get("agent_session")
    if not isinstance(session, dict):
        return None
    return (session.get("kind"), session.get("source"), session.get("value"))


def capture_herdr_workspace_snapshot(
    herdr_bin: str, env: dict[str, str] | None, *, session_name: str | None = None,
) -> dict | None:
    """Fail-closed capture of the FULL herdr session snapshot identity for
    ONE named session (Issue #2174 AC7; PR #2176 OWNER REQUEST_CHANGES
    Finding 3: https://github.com/squne121/loop-protocol/pull/2176#issuecomment-5302819792).

    Unlike the prior implementation (which only projected each agent's
    ``(workspace_id, tab_id, pane_id)`` location tuple), this captures each
    agent's own identity (kind, ``terminal_id``, native ``agent_session``),
    every pane record (including non-agent panes), every tab record, every
    workspace record (including empty workspaces with no agent), and every
    layout record's structural shape. Returns ``None`` if ANY required
    field on ANY record is unobtainable (fail-closed; never a partial
    snapshot).

    Upstream-reality note (independently confirmed against the installed
    live ``herdr session list --json`` / ``herdr api snapshot`` v0.8.0):
    herdr's public ``SessionInfo`` (``session list``) has no separate
    ``session_id`` field distinct from ``name`` -- only
    name/default/running/socket_path/session_dir are exposed. This function
    therefore does not claim a ``session_id`` field; the strongest actually
    available per-agent identity is ``agent`` (kind) + ``terminal_id`` +
    the native ``agent_session`` triple (kind/source/value), which IS
    captured here as this run's operational definition of "agent ID".

    Volatile-but-identity-irrelevant fields (``revision``, ``state_change_seq``,
    ``scroll`` offsets, exact pixel ``rect`` dimensions from ordinary
    terminal resize) are deliberately excluded from the compared
    projection: herdr increments/changes them on ordinary agent activity
    that has nothing to do with identity or layout, so including them would
    fail this check on every run regardless of any real mutation. What IS
    compared for layout is its structural shape (pane_id membership,
    zoomed flag, split count), which DOES change when a pane is actually
    added, removed, split, or unzoomed.

    ``session_name`` (optional) targets a specific named herdr session via
    ``herdr --session <name> api snapshot`` (Finding 4: without this, only
    the ambient/default session was ever snapshotted, so a human operator
    attached to e.g. ``HERDR_SESSION=development`` was never protected)."""
    argv = [herdr_bin]
    if session_name:
        argv += ["--session", session_name]
    argv += ["api", "snapshot"]
    rc, out, _err, timed_out = _run(argv, timeout=15.0, env=env)
    if timed_out or rc != 0:
        return None
    try:
        payload = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    snapshot = result.get("snapshot") if isinstance(result, dict) else None
    if not isinstance(snapshot, dict):
        return None
    if any(field not in snapshot for field in _WORKSPACE_SNAPSHOT_FOCUS_FIELDS):
        return None

    agents_raw = snapshot.get("agents")
    if not isinstance(agents_raw, list):
        return None
    agent_records: list[tuple] = []
    for agent in agents_raw:
        if not isinstance(agent, dict) or any(
            field not in agent for field in _WORKSPACE_SNAPSHOT_AGENT_FIELDS
        ):
            return None
        agent_records.append((
            str(agent["pane_id"]),
            tuple(str(agent[field]) for field in _WORKSPACE_SNAPSHOT_AGENT_FIELDS),
            _normalize_agent_session(agent),
        ))
    agent_records.sort(key=lambda r: r[0])

    panes_raw = snapshot.get("panes", [])
    if not isinstance(panes_raw, list):
        return None
    pane_records: list[tuple] = []
    for pane in panes_raw:
        if not isinstance(pane, dict) or "pane_id" not in pane:
            return None
        pane_records.append((
            str(pane["pane_id"]),
            str(pane.get("workspace_id")),
            str(pane.get("tab_id")),
            str(pane["agent"]) if pane.get("agent") is not None else None,
            str(pane["terminal_id"]) if pane.get("terminal_id") is not None else None,
            _normalize_agent_session(pane),
        ))
    pane_records.sort(key=lambda r: r[0])

    tabs_raw = snapshot.get("tabs", [])
    if not isinstance(tabs_raw, list):
        return None
    tab_records: list[tuple] = []
    for tab in tabs_raw:
        if not isinstance(tab, dict) or "tab_id" not in tab:
            return None
        tab_records.append((str(tab["tab_id"]), str(tab.get("workspace_id")), tab.get("pane_count")))
    tab_records.sort(key=lambda r: r[0])

    workspaces_raw = snapshot.get("workspaces", [])
    if not isinstance(workspaces_raw, list):
        return None
    workspace_records: list[tuple] = []
    for ws in workspaces_raw:
        if not isinstance(ws, dict) or "workspace_id" not in ws:
            return None
        workspace_records.append(
            (str(ws["workspace_id"]), ws.get("pane_count"), ws.get("tab_count"), ws.get("label"))
        )
    workspace_records.sort(key=lambda r: r[0])

    layouts_raw = snapshot.get("layouts", [])
    if not isinstance(layouts_raw, list):
        return None
    layout_records: list[tuple] = []
    for layout in layouts_raw:
        if not isinstance(layout, dict) or "tab_id" not in layout:
            return None
        panes_in_layout = layout.get("panes")
        if not isinstance(panes_in_layout, list):
            return None
        pane_ids_in_layout = tuple(sorted(
            str(p.get("pane_id")) for p in panes_in_layout if isinstance(p, dict)
        ))
        splits = layout.get("splits")
        layout_records.append((
            str(layout["tab_id"]), str(layout.get("workspace_id")),
            bool(layout.get("zoomed")), pane_ids_in_layout,
            len(splits) if isinstance(splits, list) else None,
        ))
    layout_records.sort(key=lambda r: r[0])

    return {
        "focused_workspace_id": snapshot["focused_workspace_id"],
        "focused_tab_id": snapshot["focused_tab_id"],
        "focused_pane_id": snapshot["focused_pane_id"],
        "agent_records": agent_records,
        "pane_records": pane_records,
        "tab_records": tab_records,
        "workspace_records": workspace_records,
        "layout_records": layout_records,
    }


def diff_herdr_workspace_snapshot(before: dict | None, after: dict | None) -> list[str]:
    """Compare two ``capture_herdr_workspace_snapshot`` results for ONE
    session (Issue #2174 AC7). Fails closed (a single diagnostic entry) if
    either snapshot is ``None`` -- unobtainable evidence is treated as a
    preservation FAILURE, never silently skipped. This is a strict
    equality check across every captured record group (agent identity,
    panes including non-agent ones, tabs, workspaces including empty ones,
    and layout structure) -- not merely agent location."""
    if before is None or after is None:
        return ["could not evaluate herdr workspace/agent/focus snapshot (api snapshot unavailable)"]
    diffs: list[str] = []
    for field in _WORKSPACE_SNAPSHOT_FOCUS_FIELDS:
        if before[field] != after[field]:
            diffs.append(f"{field} changed: {before[field]!r} -> {after[field]!r}")
    for key, label in (
        ("agent_records", "agent identity records"),
        ("pane_records", "pane records (including non-agent panes)"),
        ("tab_records", "tab records"),
        ("workspace_records", "workspace records"),
        ("layout_records", "layout records"),
    ):
        if before[key] != after[key]:
            diffs.append(f"{label} changed: {before[key]} -> {after[key]}")
    return diffs


def capture_all_herdr_workspace_snapshots(
    herdr_bin: str, env: dict[str, str] | None,
) -> dict[str, dict] | None:
    """Capture every existing session only for explicit preservation opt-in.

    The default interactive lane never calls this helper: enumerating or
    snapshotting the ambient/default and named namespaces would observe human
    sessions.  When ``--require-session-baseline-preservation`` explicitly
    requests it, every named session is captured and unavailable data returns
    ``None`` so the opt-in remains fail-closed.
    """
    sessions = _herdr_sessions(herdr_bin, env=env)
    if sessions is None:
        return None
    default_snapshot = capture_herdr_workspace_snapshot(herdr_bin, env)
    if default_snapshot is None:
        return None
    result: dict[str, dict] = {"default": default_snapshot}
    for entry in sessions:
        name = entry.get("name")
        if not name:
            return None
        if str(name) == "default" or bool(entry.get("default")):
            continue
        snapshot = capture_herdr_workspace_snapshot(herdr_bin, env, session_name=str(name))
        if snapshot is None:
            return None
        result[str(name)] = snapshot
    return result


def diff_all_herdr_workspace_snapshots(
    before: dict[str, dict] | None, after: dict[str, dict] | None,
) -> list[str]:
    """Compare two ``capture_all_herdr_workspace_snapshots`` results,
    per-session, keyed by the stable session ``name`` (Issue #2174 AC7
    Finding 4). A session present before and absent after (or vice versa)
    is reported as a violation, exactly like any other identity diff."""
    if before is None or after is None:
        return [
            "could not evaluate herdr workspace/agent/focus snapshot for one or "
            "more sessions (api snapshot unavailable)"
        ]
    diffs: list[str] = []
    for name in sorted(set(before) | set(after)):
        if name not in before:
            diffs.append(f"session {name!r} newly present after the isolated lane run")
            continue
        if name not in after:
            diffs.append(f"session {name!r} missing after the isolated lane run")
            continue
        for d in diff_herdr_workspace_snapshot(before[name], after[name]):
            diffs.append(f"session {name!r}: {d}")
    return diffs


def _herdr_session_argv_prefix(herdr_bin: str, env: dict[str, str]) -> list[str]:
    """Issue #2568 AC1/AC10: every Herdr CLI operation that targets THIS
    lane's isolated named session must explicitly pass ``--session <name>``,
    never rely on the ambient ``HERDR_SESSION`` env var alone (#2571 real-
    machine spike finding: env-var-only routing is not reliably honored by
    every herdr subcommand). ``env`` is this lane's own explicit,
    already-pinned ``isolated_env`` (never the ambient process env), and
    ``HERDR_SESSION`` is set on it exactly once, right after the isolated
    session is created -- this helper only reads that already-isolated
    value back out, it never derives the session name from anything else.
    Every call site below still ALSO passes ``env=isolated_env`` (both the
    explicit flag AND the isolated env var stay pinned; belt-and-suspenders,
    not a replacement for the existing env isolation)."""
    session_name = env.get("HERDR_SESSION")
    if not session_name:
        # Should be unreachable within this lane (HERDR_SESSION is always
        # set before any of these call sites run) -- fail closed rather than
        # silently falling back to an unspecified/default/ambient session.
        raise HerdrLaneError(
            "internal error: herdr command constructed with no isolated "
            "HERDR_SESSION set on env -- refusing to target an unspecified "
            "session"
        )
    return [herdr_bin, "--session", session_name]


def new_isolated_session_name(herdr_bin: str, env: dict[str, str] | None = None) -> str:
    """Generate a fresh high-entropy name without reading any Herdr namespace.

    The unused arguments retain the existing helper's call shape for callers
    and focused tests.  A UUID4-derived name makes accidental collision
    impracticable without enumerating, listing, or observing a pre-existing
    or human Herdr session in the default interactive lane.
    """
    del herdr_bin, env
    return f"rts-{uuid.uuid4().hex}"[:32]


def create_isolated_session(
    herdr_bin: str, session_name: str, env: dict[str, str], *, timeout_seconds: float = 20.0
) -> subprocess.Popen:
    """Spawn a new detached, named Herdr session without observing others.

    Readiness is established by the following own-session ``workspace create``
    operation.  The default lane deliberately does not poll ``session list``:
    listing would observe pre-existing or human namespaces.  A process that
    exits before the own-session workspace lifecycle can start remains a
    bounded SKIP rather than falling back to an ambient session.
    """
    del timeout_seconds
    try:
        proc = subprocess.Popen(
            [herdr_bin, "--session", session_name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, start_new_session=True,
        )
    except OSError as exc:
        raise HerdrLaneError(f"could not spawn isolated herdr session: {exc}", skip=True) from exc

    # Let an immediate nested-session refusal surface without inspecting the
    # Herdr control plane.  Later own-session commands provide the remaining
    # readiness signal.
    time.sleep(0.1)
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
    return proc


def _session_socket_path(herdr_bin: str, session_name: str, env: dict[str, str] | None = None) -> str | None:
    sessions = _herdr_sessions(herdr_bin, env=env)
    if not sessions:
        return None
    for entry in sessions:
        if str(entry.get("name")) == session_name and entry.get("socket_path"):
            return str(entry["socket_path"])
    return None


def _send_prompt_turn(
    herdr_bin: str,
    agent_name: str,
    prompt_text: str,
    timeout_seconds: float,
    isolated_env: dict[str, str],
    evidence: dict,
) -> None:
    """Send a single prompt turn to an already-started herdr agent and block
    until it settles, including the existing bracketed-paste stall-recovery
    path (see module docstring / ``references/herdr.md``). Raises
    ``HerdrLaneError`` on failure."""
    prompt_deadline = time.monotonic() + timeout_seconds
    rc, out, err, timed_out = _run(
        _herdr_session_argv_prefix(herdr_bin, isolated_env) +
        ["agent", "prompt", agent_name, prompt_text, "--wait",
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
            if evidence.get("prompt_stall_recovered") is not True:
                evidence["prompt_stall_recovered"] = False
            baseline_rc, baseline_out, _e, _t = _run(
                _herdr_session_argv_prefix(herdr_bin, isolated_env) + ["agent", "get", agent_name],
                timeout=15.0, env=isolated_env,
            )
            baseline_seq = (
                _extract_agent_field(baseline_out, "state_change_seq")
                if baseline_rc == 0 else None
            )

            remaining = max(1.0, prompt_deadline - time.monotonic())
            send_rc, send_out, send_err, send_timed_out = _run(
                _herdr_session_argv_prefix(herdr_bin, isolated_env) + ["agent", "send-keys", agent_name, "enter"],
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
                    _herdr_session_argv_prefix(herdr_bin, isolated_env) + ["agent", "get", agent_name],
                    timeout=10.0, env=isolated_env,
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
                _herdr_session_argv_prefix(herdr_bin, isolated_env) +
                ["agent", "wait", agent_name, "--timeout", str(int(remaining * 1000))],
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
    claude_adapter: str = "native",
    additional_prompts: list[str] | None = None,
    hook_sink_enabled: bool = False,
    task_context_scope: str | None = None,
    task_context_state_root: str | None = None,
) -> list[str]:
    """Drive an isolated-session herdr agent lifecycle. Mutates ``evidence``
    in place (so cleanup/session identity survive even if this raises) and
    returns the bounded, redacted pane output lines.

    Issue #2219 (fix_delta iteration 1, Option B reintroduction):
    ``additional_prompts`` is an optional, ordered list of extra prompt
    turns sent to the SAME already-started agent/session AFTER the initial
    ``prompt`` turn settles, reusing the exact same per-turn send/wait/
    stall-recovery behavior (``_send_prompt_turn``) as the initial turn --
    re-implementing, in spirit, the ``--additional-prompt`` mechanism PR
    #2176 prototyped in commit 06d8baa9 and reverted in commit 5a44ebf0 for
    being out of scope for Issue #2174 (a scope decision, not a technical
    rejection). ``evidence["turns_completed"]`` counts how many prompt
    turns (initial + additional) actually settled. The herdr
    ``session_name``/``agent_name`` used for every turn are the SAME local
    values captured once above and threaded through this whole function --
    structurally the same Herdr session/agent for the entire multi-turn
    journey, never re-created per turn. Omitted by default (``None``),
    leaving every pre-existing caller's single-turn behavior unchanged
    (AC6-equivalent for this lane).

    Issue #2176 (post-merge live-environment finding, 2026-08-16): when
    ``claude_adapter == "claude-gpt"``, the PATH shim built below (needed so
    Herdr's own ``agent start --kind claude`` resolves the forwarder) is
    ALSO visible to ``scripts/claude-gpt/launch.sh`` itself once it execs.
    ``launch.sh`` internally calls ``claude_gpt_resolve_claude_bin()``
    (``scripts/claude-gpt/lib.sh``), which falls back to ``command -v
    claude`` whenever ``CLAUDE_GPT_CLAUDE_BIN`` is unset -- and with the shim
    directory prepended to PATH, that lookup resolves back to the shim
    itself, so ``launch.sh`` execs itself again (self-recursion) with its
    own canonical flags (including ``--strict-mcp-config``) appended, which
    its own top-level parser then rejects as an externally-supplied flag,
    exiting 2 before Claude Code ever starts (Herdr then times out waiting
    for agent startup). To break this loop, the REAL native ``claude``
    binary's absolute path is resolved here via ``shutil.which("claude")``
    against the ORIGINAL (pre-shim) PATH, and threaded into
    ``CLAUDE_GPT_CLAUDE_BIN`` for both the ``herdr workspace create --env``
    call and the ``herdr pane run ... export`` re-pin step below -- this
    tells ``launch.sh`` exactly which real binary to use, bypassing its own
    self-referential PATH-based lookup entirely. Omitted for
    ``claude_adapter == "native"`` (default), so every pre-existing
    caller's isolated-session env is unchanged (AC6).

    Issue #2174 (AC1): when ``runtime == "claude"`` and ``claude_bin_override``
    is a non-empty absolute path, ``herdr agent start --kind claude`` is made
    to resolve that exact binary instead of whatever ``claude`` happens to be
    on the ambient ``PATH``. ``herdr`` itself has no flag to accept an
    explicit binary path for ``--kind`` (it always re-resolves the runtime
    name via its own PATH lookup -- see ``references/claude-code.md``), so
    this is done via a session-local temporary directory containing a single
    ``claude`` FORWARDER SCRIPT (never a symlink -- a real shell script
    generated from a hard-coded ``exec '<absolute-launcher>' "$@"`` template,
    because a shell script invoked through a symlink resolves ``$0`` to the
    symlink's own path, not the real script's directory, which breaks any
    launcher that sources a sibling file such as ``lib.sh`` via
    ``dirname -- "$0"``; see PR #2176 OWNER REQUEST_CHANGES Finding 2:
    https://github.com/squne121/loop-protocol/pull/2176#issuecomment-5302819792).
    The resulting shim directory is explicitly passed to Herdr's own
    root-shell/PTY process via ``herdr workspace create --env PATH=...``
    (Finding 1 of the same review: updating only this Python client's own
    ``isolated_env["PATH"]`` never reaches the already-running Herdr server
    process that actually resolves ``claude`` inside the PTY, so the shim
    was previously unreachable and ``agent start --kind claude`` could
    silently run the ambient/native ``claude`` on the server's own PATH
    instead). The forwarder additionally writes a run-scoped nonce to a
    0600 receipt file immediately before ``exec``-ing the real launcher;
    this function reads that receipt back after the run and raises
    ``HerdrLaneError`` (never silently accepts a PASS) if it is missing or
    does not match, so a decoy ``claude`` anywhere else on the ambient PATH
    can never produce a false-positive launcher-selection PASS. Omitted by
    default (``claude_bin_override=None``), leaving every pre-existing
    caller's isolated-session ``PATH`` unchanged (AC6)."""
    # A single explicit, stripped environment is computed once and threaded
    # through every own-session Herdr command.  The default lane never lists,
    # snapshots, or otherwise observes pre-existing/human namespaces.
    isolated_env = _isolated_env()
    session_name = new_isolated_session_name(herdr_bin, env=isolated_env)
    evidence["session_name"] = session_name

    agent_name = f"rts-{runtime}-{run_id}"[:32]
    evidence["agent_name"] = agent_name
    pane_output_lines: list[str] = []
    session_proc: subprocess.Popen | None = None
    claude_bin_shim_dir: str | None = None
    hook_sink_shim_dir: str | None = None
    try:
        # Create and use only the fresh named session.  ``workspace create``
        # is the own-session readiness operation; no session-list poll or
        # socket lookup is permitted in the default lane.
        session_proc = create_isolated_session(herdr_bin, session_name, isolated_env, timeout_seconds=20.0)
        evidence["cleanup"]["session_started"] = True
        isolated_env["HERDR_SESSION"] = session_name

        claude_bin_receipt_path: str | None = None
        claude_bin_launcher_nonce: str | None = None
        workspace_create_argv = _herdr_session_argv_prefix(herdr_bin, isolated_env) + [
            "workspace", "create", "--cwd", worktree, "--no-focus",
        ]
        if runtime == "claude" and claude_bin_override:
            resolved_claude_bin_override = os.path.realpath(claude_bin_override)
            if not os.path.isfile(resolved_claude_bin_override) or not os.access(
                resolved_claude_bin_override, os.X_OK
            ):
                raise HerdrLaneError(
                    "--claude-bin path is not an executable file: "
                    f"{claude_bin_override}"
                )
            # PR #2176 OWNER REQUEST_CHANGES Findings 1+2
            # (https://github.com/squne121/loop-protocol/pull/2176#issuecomment-5302819792):
            # a real forwarder script (never a symlink -- see the docstring
            # above) that (a) writes a run-scoped nonce to a 0600 receipt
            # file, then (b) ``exec``s the actual launcher, preserving its
            # exit status / signal semantics. ``mkdtemp`` already creates the
            # directory 0700; both the directory and the forwarder are
            # explicitly (re)set to 0700 below so this never depends on the
            # platform umask.
            claude_bin_shim_dir = tempfile.mkdtemp(prefix="worktree-agent-runtime-smoke-claude-bin-")
            os.chmod(claude_bin_shim_dir, 0o700)
            claude_bin_launcher_nonce = uuid.uuid4().hex
            claude_bin_receipt_path = str(Path(claude_bin_shim_dir) / "receipt")
            forwarder_path = Path(claude_bin_shim_dir) / "claude"
            _escaped_target = resolved_claude_bin_override.replace("'", "'\\''")
            _escaped_receipt = claude_bin_receipt_path.replace("'", "'\\''")
            forwarder_path.write_text(
                "#!/bin/sh\n"
                "umask 0077\n"
                f"printf '%s' '{claude_bin_launcher_nonce}' > '{_escaped_receipt}'\n"
                f"exec '{_escaped_target}' \"$@\"\n",
                encoding="utf-8",
            )
            forwarder_path.chmod(0o700)
            evidence["claude_bin_shim_kind"] = "forwarder"
            existing_path = isolated_env.get("PATH", os.defpath)
            claude_gpt_real_claude_bin: str | None = None
            if claude_adapter == "claude-gpt":
                # Resolved against the PRE-shim PATH (``existing_path``, not
                # ``isolated_env["PATH"]`` after the prepend below) so this
                # never finds the forwarder itself. See the function
                # docstring (Issue #2176 CLAUDE_GPT_CLAUDE_BIN self-
                # recursion fix, 2026-08-16).
                claude_gpt_real_claude_bin = shutil.which("claude", path=existing_path)
                if claude_gpt_real_claude_bin:
                    isolated_env["CLAUDE_GPT_CLAUDE_BIN"] = claude_gpt_real_claude_bin
                    evidence["claude_gpt_claude_bin_resolved"] = claude_gpt_real_claude_bin
                else:
                    evidence["claude_gpt_claude_bin_resolved"] = None
            isolated_env["PATH"] = claude_bin_shim_dir + os.pathsep + existing_path
            # Finding 1: the updated PATH must be handed to Herdr's own
            # server/root-shell process explicitly -- updating only this
            # Python client's isolated_env["PATH"] never reaches it.
            workspace_create_argv += ["--env", "PATH=" + isolated_env["PATH"]]
            if claude_gpt_real_claude_bin:
                workspace_create_argv += [
                    "--env", "CLAUDE_GPT_CLAUDE_BIN=" + claude_gpt_real_claude_bin,
                ]

        # Claude-GPT accepts the runtime-smoke peer policy only through its
        # existing fixed launcher-owned channel.  Thread the default fixed
        # value through both workspace creation and the already-running pane
        # shell, because rc files can otherwise erase an inherited value.  A
        # hook-sink run upgrades this same fixed channel below; no caller value
        # is accepted or forwarded.
        launcher_env_pairs: list[tuple[str, str]] = []
        if runtime == "claude" and claude_adapter == "claude-gpt":
            launcher_env_pairs = [
                ("CLAUDE_GPT_RUNTIME_SMOKE_HOOKS", "subagent-start-stop"),
            ]

        # Issue #2219 AC2/AC3/AC13-AC17 (OWNER anchor decision, hook-event
        # evidence channel): wire the interactive lane's durable hook sink
        # the SAME way ``CLAUDE_GPT_CLAUDE_BIN``/``PATH`` are already
        # threaded above -- via ``herdr workspace create --env`` (Finding 1:
        # updating only this process's own env never reaches the already-
        # running Herdr server/pane process) AND the pane re-pin
        # ``export`` string below (an interactive login shell's own rc
        # files can clobber an inherited env var, same Finding 2 rationale).
        # Independent of ``claude_bin_override`` -- this applies whenever
        # the caller opted in via ``--require-min-subagents``/
        # ``--require-min-turns`` (``hook_sink_enabled``), regardless of
        # whether a ``--claude-bin`` override was also requested.
        hook_sink_env_pairs: list[tuple[str, str]] = []
        if runtime == "claude" and hook_sink_enabled:
            hook_sink_nonce = uuid.uuid4().hex
            evidence["hook_sink_nonce"] = hook_sink_nonce
            if claude_adapter == "claude-gpt":
                # Path built ONLY from the launcher-owned constant
                # (``claude_gpt_proxy_state_dir_python()`` mirrors
                # ``scripts/claude-gpt/lib.sh``'s
                # ``claude_gpt_proxy_state_dir()`` exactly) plus the nonce
                # generated above -- never from ``worktree`` or any other
                # caller-supplied value (AC14). ``scripts/claude-gpt/
                # launch.sh``'s own ``hook-sink-multi-turn`` gate computes
                # the identical path for the same nonce.
                hook_sink_path = claude_gpt_hook_sink_path(hook_sink_nonce)
                hook_sink_path.parent.mkdir(parents=True, exist_ok=True)
                evidence["hook_sink_path"] = str(hook_sink_path)
                # Replace only the default fixed launcher channel with the
                # other already-recognized fixed channel.  This remains a
                # harness-selected value, never public caller input.
                launcher_env_pairs[0] = (
                    "CLAUDE_GPT_RUNTIME_SMOKE_HOOKS", "hook-sink-multi-turn"
                )
                hook_sink_env_pairs = [
                    ("CLAUDE_GPT_HOOK_SINK_NONCE", hook_sink_nonce),
                    # launch.sh independently computes this SAME path from
                    # its own launcher-owned constant + this nonce; also
                    # exporting it here is redundant-but-harmless
                    # observability, never an input launch.sh trusts for
                    # path construction (AC14).
                    ("CLAUDE_GPT_HOOK_SINK_PATH", str(hook_sink_path)),
                ]
            else:
                # native adapter (Issue #2219 In Scope: "native adapter は
                # scripts/claude-gpt/** を一切変更せず harness 側のみで完結
                # させる"): a harness-owned CLAUDE_CONFIG_DIR pointing at a
                # generated settings.json with the SAME fixed hook set
                # (UserPromptSubmit/Stop/StopFailure/SubagentStart/
                # SubagentStop), all self-contained in a fresh temp dir
                # this function itself controls -- no scripts/claude-gpt/**
                # file is read or written for this branch.
                hook_sink_shim_dir = tempfile.mkdtemp(prefix="worktree-agent-runtime-smoke-hook-sink-")
                os.chmod(hook_sink_shim_dir, 0o700)
                hook_sink_path = Path(hook_sink_shim_dir) / f"hook-sink-{hook_sink_nonce}.jsonl"
                hook_sink_writer_path = Path(hook_sink_shim_dir) / "hook_sink_writer.py"
                hook_sink_writer_path.write_text(_HOOK_SINK_WRITER_SOURCE, encoding="utf-8")
                hook_sink_writer_path.chmod(0o700)
                claude_config_dir = Path(hook_sink_shim_dir) / "claude-config"
                claude_config_dir.mkdir(parents=True, exist_ok=True)
                settings_path = claude_config_dir / "settings.json"
                hook_command = f'python3 "{hook_sink_writer_path}"'
                settings_payload = {
                    "hooks": {
                        event_name: [{"hooks": [{"type": "command", "command": hook_command}]}]
                        for event_name in sorted(_HOOK_SINK_LIFECYCLE_EVENTS)
                    },
                }
                settings_path.write_text(json.dumps(settings_payload, indent=2), encoding="utf-8")
                evidence["hook_sink_path"] = str(hook_sink_path)
                hook_sink_env_pairs = [
                    ("CLAUDE_CONFIG_DIR", str(claude_config_dir)),
                    ("CLAUDE_GPT_HOOK_SINK_NONCE", hook_sink_nonce),
                    ("CLAUDE_GPT_HOOK_SINK_PATH", str(hook_sink_path)),
                ]

        # Issue #2568 In Scope: additive Task Context env/carrier
        # passthrough, threaded through the SAME herdr `workspace create
        # --env` mechanism as the launcher/hook-sink pairs above (Finding 1:
        # updating only this Python client's own env never reaches the
        # already-running Herdr server/pane process). Only applied when the
        # caller opts in; omitted by default.
        task_context_env_pairs = _task_context_env_pairs(task_context_scope, task_context_state_root)

        for key, value in [*launcher_env_pairs, *hook_sink_env_pairs, *task_context_env_pairs]:
            isolated_env[key] = value
            workspace_create_argv += ["--env", f"{key}={value}"]

        rc, out, err, timed_out = _run(
            workspace_create_argv,
            timeout=20.0, env=isolated_env,
        )
        if timed_out or rc != 0:
            raise HerdrLaneError(f"herdr workspace create failed: {_redact(err or out)}")
        pane_id = _extract_pane_id_from_workspace(out)
        if not pane_id:
            raise HerdrLaneError("could not parse pane_id from herdr workspace create output")
        evidence["pane_id"] = pane_id

        if claude_bin_shim_dir is not None:
            # Live-environment finding (2026-08-16 AC4 re-verification):
            # ``workspace create --env PATH=...`` DOES set the newly
            # spawned pane shell's initial process PATH (confirmed via
            # ``herdr pane run <pane> 'echo $PATH'``), but an interactive
            # login shell re-sources its own rc files (.bashrc/.zshrc/
            # .profile) immediately afterward, and those commonly
            # PREPEND the user's own standard directories (e.g.
            # ``~/.local/bin``) in front of whatever was inherited --
            # pushing the shim directory behind a real ambient ``claude``
            # and silently defeating the override (exactly the false-PASS
            # scenario Finding 1 warned about). ``herdr pane run`` executes
            # a command in the ALREADY-running (post-rc-sourced) shell, so
            # explicitly re-exporting PATH there -- immediately before
            # ``agent start`` -- guarantees the shim wins regardless of
            # rc-script ordering.
            _escaped_shim_dir = claude_bin_shim_dir.replace("'", "'\\''")
            _pin_cmd = f"export PATH='{_escaped_shim_dir}':\"$PATH\""
            if claude_adapter == "claude-gpt" and isolated_env.get("CLAUDE_GPT_CLAUDE_BIN"):
                # Same self-recursion fix as the ``--env`` above (Issue
                # #2176, 2026-08-16), re-applied to the already-running,
                # post-rc-sourced pane shell for the same reason PATH is
                # re-pinned here: an interactive login shell's own rc files
                # could otherwise clobber an inherited env var too.
                _escaped_claude_gpt_bin = isolated_env["CLAUDE_GPT_CLAUDE_BIN"].replace("'", "'\\''")
                _pin_cmd += f" && export CLAUDE_GPT_CLAUDE_BIN='{_escaped_claude_gpt_bin}'"
            _pin_rc, _pin_out, _pin_err, _pin_timed_out = _run(
                _herdr_session_argv_prefix(herdr_bin, isolated_env) + ["pane", "run", pane_id, _pin_cmd],
                timeout=15.0, env=isolated_env,
            )
            if _pin_timed_out or _pin_rc != 0:
                raise HerdrLaneError(
                    f"could not pin --claude-bin shim PATH in the isolated pane's "
                    f"already-running shell: {_redact(_pin_err or _pin_out)}"
                )

        runtime_env_pairs = [*launcher_env_pairs, *hook_sink_env_pairs, *task_context_env_pairs]
        if runtime_env_pairs:
            # An interactive login shell can clobber inherited launcher
            # policy, hook-sink, or Task Context carrier env vars, so
            # explicitly re-export the fixed values in the already-running
            # pane shell before ``agent start``.
            _pin_runtime_env_cmd = " && ".join(
                f"export {key}='{value.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
                for key, value in runtime_env_pairs
            )
            _pin_runtime_env_rc, _pin_runtime_env_out, _pin_runtime_env_err, _pin_runtime_env_timed_out = _run(
                _herdr_session_argv_prefix(herdr_bin, isolated_env) + ["pane", "run", pane_id, _pin_runtime_env_cmd],
                timeout=15.0, env=isolated_env,
            )
            if _pin_runtime_env_timed_out or _pin_runtime_env_rc != 0:
                raise HerdrLaneError(
                    "could not pin launcher runtime env vars in the isolated pane's "
                    f"already-running shell: {_redact(_pin_runtime_env_err or _pin_runtime_env_out)}"
                )

        # Issue #1960 AC5: the interactive lane never forwards
        # structured-only flags (``--output-format`` / ``--include-hook-events``
        # / ``--no-session-persistence`` / ``--max-turns``) to the TUI
        # launch. Bounded execution for this lane comes from herdr's own
        # wait timeout, process termination, and own-name stop/delete command
        # success -- not from a structured-lane print-mode flag
        # that has not been separately confirmed to be honored by an
        # interactive Claude Code launch.
        # ``herdr agent start`` explicitly supports ``-- [AGENT_ARG]...``.
        # Native Claude receives the same fixed, invocation-local policy used
        # by the structured subprocess. Claude-GPT keeps its launcher-owned
        # policy channel instead: forwarding --settings to that launcher is a
        # rejected policy-bypass input. Neither branch alters the isolated
        # Herdr lifecycle or routes structured execution through Herdr.
        agent_extra_args: list[str] = []
        if claude_adapter == "native":
            agent_extra_args = [
                "--", "--settings", _CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON,
            ]

        # A freshly created workspace's shell may not be an "available shell"
        # yet (still initializing). Retry ``agent start`` with a bounded,
        # short backoff instead of failing on the first race.
        start_rc = None
        start_out = start_err = ""
        start_timed_out = False
        for attempt in range(5):
            start_rc, start_out, start_err, start_timed_out = _run(
                _herdr_session_argv_prefix(herdr_bin, isolated_env) +
                ["agent", "start", agent_name, "--kind", runtime,
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

        _send_prompt_turn(herdr_bin, agent_name, prompt, timeout_seconds, isolated_env, evidence)
        evidence["turns_completed"] = 1
        for extra_prompt in (additional_prompts or []):
            _send_prompt_turn(herdr_bin, agent_name, extra_prompt, timeout_seconds, isolated_env, evidence)
            evidence["turns_completed"] += 1

        rc, out, err, timed_out = _run(
            _herdr_session_argv_prefix(herdr_bin, isolated_env) + ["agent", "get", agent_name],
            timeout=20.0, env=isolated_env,
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
            _herdr_session_argv_prefix(herdr_bin, isolated_env) + ["agent", "explain", agent_name, "--json"],
            timeout=20.0, env=isolated_env,
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
            _herdr_session_argv_prefix(herdr_bin, isolated_env) +
            ["agent", "read", agent_name, "--source", "recent-unwrapped",
             "--lines", str(_MAX_PANE_LINES)],
            timeout=20.0, env=isolated_env,
        )
        if rc == 0:
            pane_output_lines = _bounded_redacted_lines(out, _MAX_PANE_LINES)

        # Finding 1 causal-proof requirement: a PASS must never be possible
        # from an ambient/native "claude" that happened to be on Herdr's own
        # PATH instead of the specified launcher. The receipt is the only
        # signal in this lifecycle that is actually written BY the
        # specified launcher's own forwarder (a decoy binary elsewhere on
        # PATH cannot produce it), so its absence or mismatch is a hard
        # FAIL, never a silent pass-through.
        if claude_bin_override and claude_bin_receipt_path is not None:
            try:
                _receipt_observed = Path(claude_bin_receipt_path).read_text(encoding="utf-8").strip()
            except OSError:
                _receipt_observed = None
            evidence["claude_bin_launcher_receipt_verified"] = (
                _receipt_observed is not None and _receipt_observed == claude_bin_launcher_nonce
            )
            if not evidence["claude_bin_launcher_receipt_verified"]:
                raise HerdrLaneError(
                    "--claude-bin launcher receipt not observed or mismatched; the "
                    "specified launcher may not have actually executed (PATH shim "
                    "did not reach the herdr workspace process)"
                )

        if hook_sink_env_pairs:
            # Parsed HERE (before the temp dir, native adapter only, is
            # removed in the ``finally`` below) so the sink's own records
            # survive in ``evidence`` regardless of adapter. Never the raw
            # sink file text -- only already-validated records (Issue
            # #2219 AC13: no raw prompt/response content ever leaves this
            # function).
            _hook_sink_records, _hook_sink_malformed = parse_claude_gpt_hook_sink_records(
                evidence.get("hook_sink_path", "")
            )
            evidence["hook_sink_records"] = _hook_sink_records
            evidence["hook_sink_malformed_line_count"] = _hook_sink_malformed

        return pane_output_lines
    finally:
        if hook_sink_shim_dir is not None:
            shutil.rmtree(hook_sink_shim_dir, ignore_errors=True)
        cleanup = evidence["cleanup"]
        cleanup["attempted"] = True
        # Cleanup is scoped exclusively to the generated name.  The default
        # lane cannot confirm deletion by listing the global control plane,
        # because that would observe human/pre-existing namespaces.  Instead
        # both own-session commands must succeed and the launcher process must
        # be terminated; either failure remains fail-closed.
        stop_rc, _o, _e, _t = _run(
            [herdr_bin, "session", "stop", session_name, "--json"], timeout=20.0, env=isolated_env,
        )
        cleanup["stop_rc"] = stop_rc
        delete_rc, _o2, _e2, _t2 = _run(
            [herdr_bin, "session", "delete", session_name, "--json"], timeout=20.0, env=isolated_env,
        )
        cleanup["delete_rc"] = delete_rc
        session_process_terminated = session_proc is None
        if session_proc is not None and session_proc.poll() is None:
            session_proc.terminate()
            try:
                session_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                session_proc.kill()
                try:
                    session_proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
        if session_proc is not None:
            session_process_terminated = session_proc.poll() is not None
        cleanup["confirmed_removed"] = bool(
            stop_rc == 0 and delete_rc == 0 and session_process_terminated
        )
        if claude_bin_shim_dir is not None:
            shutil.rmtree(claude_bin_shim_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Evidence writing — allowlist-only summary.md (Issue #1921 P1 fix-delta:
# no raw transcript, no native event dump, no agent-explain blob).
# ---------------------------------------------------------------------------


def write_evidence(output_dir: Path, *, schema_summary: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    redacted_schema_summary = _redact_evidence_value(schema_summary)
    summary_lines = ["# Runtime Smoke Summary", ""]
    for key in sorted(redacted_schema_summary.keys()):
        summary_lines.append(f"- {key}: {redacted_schema_summary[key]}")
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
# Issue #2840: opt-in named SubAgent spawn -> complete -> name resume -> complete
# scenario (``--named-subagent-resume``).
#
# Ownership boundary: everything below is GENERIC observation and correlation of
# the already-captured native stream-json channel (hook name, decision kind,
# name <-> agent ID correspondence, lifecycle ordering).  This runner never
# emits a Task Context semantic verdict -- that stays owned by
# ``scripts/task-context/task_context_runtime_smoke_verifier.py::
# orchestrate_runtime_smoke()`` (the SKILL documents how to call it).
# ---------------------------------------------------------------------------

NAMED_SUBAGENT_RESUME_EVIDENCE_SCHEMA = "NAMED_SUBAGENT_RESUME_EVIDENCE_V1"
NAMED_SUBAGENT_RESUME_FIXTURE_DIR_RELPATH = ".claude/skills/worktree-agent-runtime-smoke/fixtures"
NAMED_SUBAGENT_RESUME_PROMPT_FIXTURE_RELPATH = (
    NAMED_SUBAGENT_RESUME_FIXTURE_DIR_RELPATH + "/named-subagent-resume.prompt.md"
)
NAMED_SUBAGENT_RESUME_COMPAT_FIXTURE_RELPATH = (
    NAMED_SUBAGENT_RESUME_FIXTURE_DIR_RELPATH + "/named-subagent-resume.compat.md"
)
NAMED_SUBAGENT_RESUME_LAUNCHER_RELPATH = "scripts/claude-gpt/launch.sh"
NAMED_SUBAGENT_RESUME_AGENT_NAME = "named-resume-worker"
NAMED_SUBAGENT_RESUME_FIRST_MARKER = "NAMED_RESUME_FIRST_DONE"
NAMED_SUBAGENT_RESUME_SECOND_MARKER = "NAMED_RESUME_SECOND_DONE"

NAMED_RESUME_VERDICT_PASS = "pass"
NAMED_RESUME_VERDICT_FAIL = "fail"
NAMED_RESUME_VERDICT_SKIP = "skip"

NAMED_RESUME_FAILURE_LAYERS = (
    "client_schema",
    "launcher_config",
    "proxy_translation",
    "backend_model_emission",
    "hook_lifecycle",
    "unclassified",
)

_NAMED_RESUME_SPAWN_TOOL_NAMES = frozenset({"Agent", "Task"})
_NAMED_RESUME_NON_ALLOW_DECISIONS = frozenset({"ask", "deny", "block", "defer"})
_NAMED_RESUME_CLIENT_SCHEMA_ERROR_RE = re.compile(
    r"InputValidationError|unexpected parameter|is not a valid parameter|"
    r"Invalid input|schema validation|unknown (?:parameter|field)",
    re.IGNORECASE,
)
# Proxy translation evidence: only strings that describe an actual translation fault
# (a malformed-request rejection: ``API Error: 400`` / ``422``, ``invalid_request_error``,
# a tool-schema / strict tool|function message).  A generic ``API Error: 4xx/5xx`` (401
# auth, 429 rate limit, 529 overload, 5xx upstream) is NOT evidence and is never matched
# here.  The bare product name (``claude-code-proxy``) is deliberately NOT a pattern: it
# appears in the launcher's normal ``launcher=... proxy=<version>`` startup line, and a
# normal log line must never change which layer a failure is attributed to.
_NAMED_RESUME_PROXY_ERROR_RE = re.compile(
    r"invalid_request_error|API Error: *(?:400|422)\b|"
    r"tool[^\n]{0,40}schema|strict[^\n]{0,40}(?:tool|function)",
    re.IGNORECASE,
)
# A ``result`` error event carrying one of these API statuses is a request the upstream
# rejected as malformed (the shape of a translation fault).  Other statuses (auth, rate
# limit, overload) say nothing about translation.
_NAMED_RESUME_TRANSLATION_API_STATUSES = frozenset({400, 422})
_NAMED_RESUME_LAUNCHER_STARTUP_LINE_PREFIX = "launcher="
# Hook execution outcomes that are a normal run of the hook.
_NAMED_RESUME_HOOK_OK_OUTCOMES = frozenset({None, "success"})
_HOME_PATH_RE = re.compile(r"/(?:home|root|Users)/[^\s\"']+")


def _nr_public_text(value: str) -> str:
    """Strip HOME-style absolute paths from a string that may reach public evidence."""
    return _HOME_PATH_RE.sub("<redacted-path>", value)


def _nr_public_path(path: str | None, *, repo_root: str | None = None) -> str | None:
    """Public-safe rendering of a filesystem path: repo-relative when inside the
    checkout, ``~/...`` when under HOME, otherwise the path unchanged unless it
    still looks like a HOME path (then only the basename is kept)."""
    if not path:
        return None
    candidate = os.path.abspath(path)
    if repo_root:
        root = os.path.abspath(repo_root)
        if candidate == root or candidate.startswith(root + os.sep):
            return os.path.relpath(candidate, root)
    home = os.path.expanduser("~")
    if home and home != "~" and (candidate == home or candidate.startswith(home + os.sep)):
        return "~" + candidate[len(home):]
    return os.path.basename(candidate) if _HOME_PATH_RE.search(candidate) else candidate


def _nr_file_sha256(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _nr_text_blocks(message: object) -> list[str]:
    """Plain text a (child) assistant message *reports*: ``text`` blocks and the
    ``message`` of a ``SubagentHandback`` tool_use (the handback form).  The input
    strings of any other ``tool_use`` (e.g. ``Grep(pattern=<marker>)``) are what the
    model asked a tool to do, never a result, and are not returned."""
    texts: list[str] = []
    if not isinstance(message, dict):
        return texts
    content = message.get("content")
    if isinstance(content, str):
        texts.append(content)
        return texts
    if not isinstance(content, list):
        return texts
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif (
            block.get("type") == "tool_use" and block.get("name") == "SubagentHandback"
            and isinstance(block.get("input"), dict) and isinstance(block["input"].get("message"), str)
        ):
            texts.append(block["input"]["message"])
    return texts


def _nr_hook_payload(event: dict) -> dict | None:
    """Embedded hook stdin payload from a ``hook_response`` stream event (``cat``
    echoes it on ``stdout``/``output``).  Only an exact object parse is accepted."""
    for key in ("stdout", "output"):
        raw = event.get(key)
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_embedded_json_object(raw)
            if parsed is not None and "hook_event_name" in parsed:
                return parsed
    return None


def _nr_hook_decision(event: dict) -> str:
    """Generic decision kind of a ``PreToolUse`` hook response: the value of
    ``hookSpecificOutput.permissionDecision`` (or a legacy top-level ``decision``)
    when the hook stdout carries a decision object, ``"none"`` otherwise.  A hook
    that exits non-zero without a decision is reported as ``"error"``."""
    for key in ("stdout", "output"):
        raw = event.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        parsed = _parse_embedded_json_object(raw)
        if parsed is None:
            continue
        specific = parsed.get("hookSpecificOutput")
        if isinstance(specific, dict):
            decision = specific.get("permissionDecision")
            if isinstance(decision, str) and decision:
                return decision
        decision = parsed.get("decision")
        if isinstance(decision, str) and decision:
            return decision
    exit_code = event.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return "error"
    return "none"


def extract_named_subagent_resume_observations(stdout: str) -> dict:
    """Generic, ordered observations from a captured stream-json stdout.

    Every list entry carries ``index`` (0-based ordinal among ALL parsed stream
    events) so the chain evaluator can require real ordering.  Raw prompt /
    message text is returned only as transient strings for marker matching and is
    never persisted by the evidence builder."""
    obs: dict = {
        "init": {
            "session_id": None, "model": None, "claude_code_version": None,
            "permission_mode": None, "tool_names": None,
        },
        "agent_calls": [],
        "name_records": [],
        "agent_starts": [],
        "agent_stops": [],
        "sendmessage_calls": [],
        "sendmessage_pretool": [],
        "sendmessage_decisions": [],
        "sendmessage_results": [],
        "task_started": [],
        "task_notifications": [],
        "child_texts": [],
        "parent_texts": [],
        "tool_errors": [],
        "result_events": [],
        "hook_event_counts": {},
        "team_signals": {
            "team_tool_use_events": 0, "team_name_in_agent_input": False,
            "teammate_task_types": [], "teammate_hook_events": 0,
        },
    }
    tool_use_names: dict[str, str] = {}
    for index, event in enumerate(_iter_claude_stream_events(stdout)):
        etype = event.get("type")
        subtype = event.get("subtype")
        if etype == "system" and subtype == "init":
            if obs["init"]["session_id"] is None:
                session_value = event.get("session_id")
                obs["init"]["session_id"] = session_value if isinstance(session_value, str) else None
                obs["init"]["model"] = event.get("model") if isinstance(event.get("model"), str) else None
                version = event.get("claude_code_version")
                obs["init"]["claude_code_version"] = version if isinstance(version, str) else None
                mode = event.get("permissionMode")
                obs["init"]["permission_mode"] = mode if isinstance(mode, str) else None
                tools = event.get("tools")
                if isinstance(tools, list):
                    obs["init"]["tool_names"] = [t for t in tools if isinstance(t, str)]
            continue
        if etype == "system" and subtype == "task_started":
            task_type = event.get("task_type")
            obs["task_started"].append({
                "index": index,
                "task_id": event.get("task_id"),
                "tool_use_id": event.get("tool_use_id"),
                "task_type": task_type,
            })
            if isinstance(task_type, str) and "teammate" in task_type.lower():
                obs["team_signals"]["teammate_task_types"].append(task_type)
            continue
        if etype == "system" and subtype == "task_notification":
            obs["task_notifications"].append({
                "index": index,
                "task_id": event.get("task_id"),
                "tool_use_id": event.get("tool_use_id"),
                "status": event.get("status"),
            })
            continue
        if etype == "system" and subtype == "hook_response":
            hook_event = event.get("hook_event")
            hook_name = event.get("hook_name") if isinstance(event.get("hook_name"), str) else ""
            if isinstance(hook_event, str):
                obs["hook_event_counts"][hook_event] = obs["hook_event_counts"].get(hook_event, 0) + 1
            if isinstance(hook_event, str) and hook_event.startswith("Teammate"):
                obs["team_signals"]["teammate_hook_events"] += 1
            payload = _nr_hook_payload(event)
            if hook_event in _CLAUDE_HOOK_LIFECYCLE_EVENTS and payload is not None:
                record = {
                    "index": index,
                    "agent_id": payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None,
                    "session_id": payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
                    "agent_type": payload.get("agent_type") if isinstance(payload.get("agent_type"), str) else None,
                }
                key = "agent_starts" if hook_event == "SubagentStart" else "agent_stops"
                obs[key].append(record)
            elif (
                hook_event == "PostToolUse" and payload is not None
                and payload.get("tool_name") in _NAMED_RESUME_SPAWN_TOOL_NAMES
            ):
                tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
                tool_response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
                name = tool_input.get("name")
                agent_id = tool_response.get("agentId")
                obs["name_records"].append({
                    "index": index,
                    "session_id": payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
                    "name": name if isinstance(name, str) and name else None,
                    "agent_id": agent_id if isinstance(agent_id, str) and agent_id else None,
                    "tool_use_id": payload.get("tool_use_id"),
                    "status": tool_response.get("status"),
                    "response_texts": _nr_response_texts(tool_response),
                })
            elif hook_event == "PreToolUse" and hook_name.endswith(":SendMessage"):
                obs["sendmessage_decisions"].append({
                    "index": index,
                    "hook_name": hook_name,
                    "exit_code": event.get("exit_code") if isinstance(event.get("exit_code"), int) else None,
                    "outcome": event.get("outcome") if isinstance(event.get("outcome"), str) else None,
                    "decision": _nr_hook_decision(event),
                    "payload_tool_use_id": (
                        payload.get("tool_use_id") if payload is not None
                        and isinstance(payload.get("tool_use_id"), str) else None
                    ),
                })
                if payload is not None and payload.get("tool_name") == "SendMessage":
                    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
                    to = tool_input.get("to")
                    obs["sendmessage_pretool"].append({
                        "index": index,
                        "session_id": payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
                        "tool_use_id": payload.get("tool_use_id"),
                        "to": to if isinstance(to, str) else None,
                    })
            continue
        if etype == "result":
            obs["result_events"].append({
                "index": index,
                "is_error": bool(event.get("is_error")),
                "api_error_status": event.get("api_error_status"),
                "text": event.get("result") if isinstance(event.get("result"), str) else "",
            })
            continue
        if etype == "assistant":
            message = event.get("message")
            parent_tool_use_id = event.get("parent_tool_use_id")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    name = block.get("name")
                    tool_use_id = block.get("id")
                    if isinstance(tool_use_id, str) and isinstance(name, str):
                        tool_use_names[tool_use_id] = name
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if name in ("TeamCreate", "TeamDelete"):
                        obs["team_signals"]["team_tool_use_events"] += 1
                    if parent_tool_use_id is not None:
                        continue
                    if name in _NAMED_RESUME_SPAWN_TOOL_NAMES:
                        agent_name = tool_input.get("name")
                        if "team_name" in tool_input:
                            obs["team_signals"]["team_name_in_agent_input"] = True
                        obs["agent_calls"].append({
                            "index": index,
                            "tool_use_id": tool_use_id,
                            "name": agent_name if isinstance(agent_name, str) and agent_name else None,
                            "subagent_type": tool_input.get("subagent_type")
                            if isinstance(tool_input.get("subagent_type"), str) else None,
                        })
                    elif name == "SendMessage":
                        to = tool_input.get("to")
                        obs["sendmessage_calls"].append({
                            "index": index,
                            "tool_use_id": tool_use_id,
                            "to": to if isinstance(to, str) else None,
                        })
            texts = _nr_text_blocks(message)
            bucket = "parent_texts" if parent_tool_use_id is None else "child_texts"
            for text in texts:
                record = {"index": index, "text": text}
                if parent_tool_use_id is not None:
                    # provenance: which Agent/Task call's child produced this text
                    record["parent_tool_use_id"] = parent_tool_use_id if isinstance(parent_tool_use_id, str) else None
                obs[bucket].append(record)
            continue
        if etype == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            tool_use_result = event.get("tool_use_result")
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                tool_use_id = block.get("tool_use_id")
                tool_name = tool_use_names.get(tool_use_id) if isinstance(tool_use_id, str) else None
                if block.get("is_error") and tool_name in (_NAMED_RESUME_SPAWN_TOOL_NAMES | {"SendMessage"}):
                    obs["tool_errors"].append({
                        "index": index, "tool_name": tool_name,
                        "text": _nr_tool_result_text(block),
                    })
                if tool_name == "SendMessage":
                    result_obj = tool_use_result if isinstance(tool_use_result, dict) else _nr_parse_json_text(
                        _nr_tool_result_text(block)
                    )
                    result_obj = result_obj if isinstance(result_obj, dict) else {}
                    pin = result_obj.get("pin") if isinstance(result_obj.get("pin"), dict) else {}
                    resumed = result_obj.get("resumedAgentId")
                    pin_name = pin.get("name")
                    obs["sendmessage_results"].append({
                        "index": index,
                        "tool_use_id": tool_use_id,
                        "success": result_obj.get("success") is True,
                        "resumed_agent_id": resumed if isinstance(resumed, str) and resumed else None,
                        "pin_name": pin_name if isinstance(pin_name, str) and pin_name else None,
                        "error_text": _nr_public_text(str(result_obj.get("message") or ""))[:200]
                        if result_obj.get("success") is not True else None,
                    })
            continue
    return obs


def _nr_tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _nr_parse_json_text(text: str) -> object:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _nr_response_texts(tool_response: dict) -> list[str]:
    texts: list[str] = []
    handback = tool_response.get("handbackReport")
    if isinstance(handback, dict) and isinstance(handback.get("text"), str):
        texts.append(handback["text"])
    content = tool_response.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    elif isinstance(content, str):
        texts.append(content)
    return texts


def evaluate_named_subagent_resume_chain(
    obs: dict, *, first_marker: str = NAMED_SUBAGENT_RESUME_FIRST_MARKER,
    resume_marker: str = NAMED_SUBAGENT_RESUME_SECOND_MARKER,
) -> dict:
    """Generic causal-chain correlation (no Task Context verdict, no resolver).

    A name is only addressable when the SAME caller session recorded a
    ``PostToolUse:Agent`` ``tool_input.name`` <-> ``tool_response.agentId``
    pair for it.  A name that only another session recorded, an unrecorded name,
    an ``agent_type``-only match, or a same-session name collision is NEVER
    resolved here: the chain breaks and the verdict is never ``pass``.

    Returns ``{"verdict", "chain_break", "addressing", "agent_name", "agent_id",
    "steps", ...}``; ``steps`` is the ordered boolean causal chain."""
    steps = {
        "agent_call_with_name": False,
        "name_agent_id_recorded": False,
        "first_completion": False,
        "sendmessage_to_name_issued": False,
        "sendmessage_accepted": False,
        "same_agent_id_resumed": False,
        "no_new_agent_invocation": False,
        "resume_completion": False,
        "parent_retrieved_resume_result": False,
    }
    result: dict = {
        "verdict": NAMED_RESUME_VERDICT_FAIL,
        "chain_break": None,
        "addressing": None,
        "agent_name": None,
        "agent_id": None,
        "agent_id_lane": {"agent_id": None, "accepted": False, "resumed_same_agent_id": False},
        "first_completion_marker_from_child": False,
        "resume_completion_marker_from_child": False,
        "steps": steps,
        "sendmessage_decisions": [],
        "new_agent_ids_after_spawn": [],
    }

    def _break(reason: str) -> dict:
        result["chain_break"] = reason
        result["verdict"] = NAMED_RESUME_VERDICT_FAIL
        return result

    caller = obs["init"]["session_id"]
    if not caller:
        return _break("caller_session_unknown")

    hook_signal_present = bool(obs["name_records"] or obs["agent_starts"] or obs["agent_stops"])
    if obs["agent_calls"] and not hook_signal_present:
        # The Agent tool was really called but the generic hook channel produced no
        # SubagentStart/SubagentStop/PostToolUse:Agent record at all: the causal
        # evidence is unobservable (e.g. #2846 ``no_evidence``).  That is a SKIP,
        # never a PASS and never a guess.
        result["verdict"] = NAMED_RESUME_VERDICT_SKIP
        result["chain_break"] = "subagent_causal_observation_unavailable"
        return result

    if not obs["agent_calls"]:
        return _break("no_agent_call")
    steps["agent_call_with_name"] = any(call["name"] for call in obs["agent_calls"])

    # Identity: a ``PostToolUse:Agent`` record only counts when it answers a REAL Agent
    # invocation (same tool_use_id, same ``name``, issued earlier in the stream).  A
    # record that answers no invocation, or answers one with another name, is never used.
    calls_by_tuid = {call["tool_use_id"]: call for call in obs["agent_calls"] if call["tool_use_id"]}

    def _linked_call(record: dict) -> dict | None:
        call = calls_by_tuid.get(record["tool_use_id"]) if isinstance(record["tool_use_id"], str) else None
        if call is None or call["index"] >= record["index"]:
            return None
        return call

    # Caller-session records only: a record another session produced is never used.
    own_records = [
        record for record in obs["name_records"]
        if record["session_id"] == caller and record["name"] and record["agent_id"]
        and (call := _linked_call(record)) is not None and call["name"] == record["name"]
    ]
    own_agent_ids = {
        record["agent_id"] for record in obs["name_records"]
        if record["session_id"] == caller and record["agent_id"]
    }
    own_agent_ids |= {s["agent_id"] for s in obs["agent_starts"] if s["session_id"] == caller and s["agent_id"]}

    send_calls = [call for call in obs["sendmessage_calls"] if call["to"]]
    if not send_calls:
        steps["name_agent_id_recorded"] = bool(own_records)
        return _break("sendmessage_not_issued" if steps["agent_call_with_name"] else "agent_call_without_name")
    send = send_calls[0]
    target = send["to"]

    # --- addressing: name (caller-session recorded) vs. agent ID vs. unrecorded ---
    name_ids = {record["agent_id"] for record in own_records if record["name"] == target}
    same_name_calls = [call for call in obs["agent_calls"] if call["name"] == target]
    if len(name_ids) > 1 or len(same_name_calls) > 1:
        # Same-session name collision: never silently resolved to one agent.
        result["addressing"] = "name"
        result["agent_name"] = target
        return _break("name_collision_same_session")
    if len(name_ids) == 1:
        addressing = "name"
        agent_id = next(iter(name_ids))
        result["agent_name"] = target
    elif target in own_agent_ids:
        addressing = "agent_id"
        agent_id = target
        recorded_for_id = [r["name"] for r in own_records if r["agent_id"] == agent_id and r["name"]]
        result["agent_name"] = recorded_for_id[0] if recorded_for_id else None
    else:
        agent_types = {call["subagent_type"] for call in obs["agent_calls"] if call["subagent_type"]}
        agent_types |= {s["agent_type"] for s in obs["agent_starts"] if s["agent_type"]}
        result["agent_name"] = target
        if target in agent_types:
            return _break("sendmessage_target_is_agent_type_only")
        if not steps["agent_call_with_name"]:
            return _break("agent_call_without_name")
        if not same_name_calls:
            # No Agent invocation actually carried this name.
            return _break("sendmessage_target_not_recorded_name")
        # The name was really passed to an Agent call but no PostToolUse:Agent record
        # answers THAT invocation (tool_use_id / name): the name <-> agent ID pair is
        # not established by the hook channel.
        return _break("agent_call_post_tool_use_unlinked")
    result["addressing"] = addressing
    result["agent_id"] = agent_id
    steps["agent_call_with_name"] = addressing == "name"
    steps["name_agent_id_recorded"] = bool(own_records) and addressing == "name"
    if addressing == "agent_id":
        steps["name_agent_id_recorded"] = False
        steps["agent_call_with_name"] = any(call["name"] for call in obs["agent_calls"])

    # --- spawn identity of A: the Agent call whose PostToolUse:Agent returned agentId A ---
    spawn_record = next(
        (
            r for r in obs["name_records"]
            if r["session_id"] == caller and r["agent_id"] == agent_id and _linked_call(r) is not None
            and (addressing != "name" or r["name"] == target)
        ),
        None,
    )
    spawn_call = _linked_call(spawn_record) if spawn_record else None
    owner_tuids: set[str] = set()
    if spawn_call is not None and isinstance(spawn_call["tool_use_id"], str):
        owner_tuids.add(spawn_call["tool_use_id"])
    if isinstance(send["tool_use_id"], str):
        owner_tuids.add(send["tool_use_id"])
    owner_tuids |= {
        t["tool_use_id"] for t in obs["task_started"]
        if t["task_id"] == agent_id and isinstance(t["tool_use_id"], str)
    }
    spawn_index = spawn_call["index"] if spawn_call is not None else None

    # --- first completion of A: its own SubagentStart -> child result -> SubagentStop,
    #     all after the Agent call that created A and before the SendMessage call ---
    first_starts = [
        s for s in obs["agent_starts"]
        if s["agent_id"] == agent_id and spawn_index is not None and spawn_index < s["index"] < send["index"]
    ]
    first_stops = [
        s for s in obs["agent_stops"]
        if first_starts and s["agent_id"] == agent_id and first_starts[0]["index"] < s["index"] < send["index"]
    ]
    first_marker_from_child = bool(first_starts) and (
        any(
            first_marker in text["text"] for text in obs["child_texts"]
            if text.get("parent_tool_use_id") in owner_tuids
            and first_starts[0]["index"] < text["index"] < send["index"]
        )
        or bool(spawn_record and any(first_marker in text for text in spawn_record["response_texts"]))
    )
    result["first_completion_marker_from_child"] = first_marker_from_child
    steps["first_completion"] = bool(first_starts) and bool(first_stops) and first_marker_from_child

    # --- SendMessage issued / hook observation / accepted ---
    steps["sendmessage_to_name_issued"] = addressing == "name"
    send_results = [r for r in obs["sendmessage_results"] if r["tool_use_id"] == send["tool_use_id"]]
    accepted_result = send_results[0] if send_results else None
    # The PreToolUse hooks that ran for THIS SendMessage sit between the call and its result.
    window_end = accepted_result["index"] if accepted_result is not None else float("inf")
    window_decisions = [d for d in obs["sendmessage_decisions"] if send["index"] < d["index"] < window_end]
    result["sendmessage_decisions"] = [
        {"hook_name": d["hook_name"], "decision": d["decision"], "exit_code": d["exit_code"], "outcome": d["outcome"]}
        for d in window_decisions[:8]
    ]
    # Observation of the hook itself, separate from whether the SendMessage went through:
    # observed (a hook response echoing this tool_use_id, all responses ran normally),
    # unobserved (no hook response for this call), failed (a response did not run normally).
    correlated = [
        p for p in obs["sendmessage_pretool"]
        if p["tool_use_id"] == send["tool_use_id"] and send["index"] < p["index"] < window_end
    ]
    failed_hooks = [
        d for d in window_decisions
        if d["decision"] == "error"
        or (d["exit_code"] is not None and d["exit_code"] != 0)
        or d["outcome"] not in _NAMED_RESUME_HOOK_OK_OUTCOMES
    ]
    hook_status = "failed" if failed_hooks else ("observed" if correlated else "unobserved")
    result["hook_observation"] = {
        "status": hook_status, "response_count": len(window_decisions), "failed_count": len(failed_hooks),
    }
    steps["sendmessage_accepted"] = bool(
        accepted_result and accepted_result["success"]
        and not any(d["decision"] in _NAMED_RESUME_NON_ALLOW_DECISIONS for d in window_decisions)
    )  # whether the call went through; whether this smoke may PASS on it is ``hook_status``
    result["agent_id_lane"]["agent_id"] = agent_id if addressing == "agent_id" else None
    if any(d["decision"] in _NAMED_RESUME_NON_ALLOW_DECISIONS for d in window_decisions):
        return _break("pretooluse_sendmessage_non_allow_decision")
    if not steps["first_completion"]:
        return _break("first_completion_not_observed")
    if hook_status == "failed":
        return _break("pretooluse_sendmessage_hook_failed")
    if hook_status == "unobserved":
        return _break("pretooluse_sendmessage_hook_unobserved")
    if accepted_result is None or not accepted_result["success"]:
        return _break("sendmessage_not_accepted")
    resumed_id = accepted_result["resumed_agent_id"]
    if resumed_id != agent_id:
        return _break("resumed_agent_id_mismatch")

    # --- same agent ID resumed, no new Agent invocation ---
    resume_starts = [s for s in obs["agent_starts"] if s["agent_id"] == agent_id and s["index"] > send["index"]]
    all_start_ids = {s["agent_id"] for s in obs["agent_starts"] if s["agent_id"]}
    new_ids = sorted(all_start_ids - {agent_id})
    result["new_agent_ids_after_spawn"] = new_ids
    new_agent_calls_after = [c for c in obs["agent_calls"] if c["index"] > send["index"]]
    steps["same_agent_id_resumed"] = bool(resume_starts)
    steps["no_new_agent_invocation"] = not new_ids and not new_agent_calls_after
    if addressing == "agent_id":
        result["agent_id_lane"]["accepted"] = True
        result["agent_id_lane"]["resumed_same_agent_id"] = steps["same_agent_id_resumed"]
    if not steps["no_new_agent_invocation"]:
        return _break("new_agent_invocation_observed")
    if not steps["same_agent_id_resumed"]:
        return _break("resume_subagent_start_missing")

    # --- resume completion (A's own resume lifecycle) and parent retrieval ---
    resume_start_index = resume_starts[0]["index"]
    resume_stops = [s for s in obs["agent_stops"] if s["agent_id"] == agent_id and s["index"] > resume_start_index]
    resume_marker_indexes = [
        text["index"] for text in obs["child_texts"]
        if resume_marker in text["text"] and text.get("parent_tool_use_id") in owner_tuids
        and text["index"] > resume_start_index
    ]
    resume_marker_from_child = bool(resume_marker_indexes)
    result["resume_completion_marker_from_child"] = resume_marker_from_child
    steps["resume_completion"] = bool(resume_stops) and resume_marker_from_child
    if not resume_stops:
        return _break("resume_subagent_stop_missing")
    if not resume_marker_from_child:
        return _break("resume_marker_not_from_child")

    completion_index = max(resume_stops[0]["index"], min(resume_marker_indexes))
    notifications = [
        n for n in obs["task_notifications"]
        if n["task_id"] == agent_id and n["status"] == "completed" and n["index"] > resume_start_index
    ]
    if notifications:
        completion_index = max(completion_index, notifications[0]["index"])
    steps["parent_retrieved_resume_result"] = any(
        resume_marker in text["text"] for text in obs["parent_texts"] if text["index"] > completion_index
    )
    if not steps["parent_retrieved_resume_result"]:
        return _break("parent_did_not_retrieve_resume_result")

    if addressing != "name":
        # Native success of the agent-ID lane alone is never a name-resume PASS.
        return _break("agent_id_lane_only_not_name_resume")
    if hook_status != "observed" or not all(steps.values()):
        # Defence in depth only: every step above already broke the chain on its own
        # evidence; this never turns a missing step into a pass.
        return _break("causal_step_missing:" + ",".join(k for k, v in steps.items() if not v))
    result["verdict"] = NAMED_RESUME_VERDICT_PASS
    result["chain_break"] = None
    return result


def _nr_stderr_error_text(stderr_text: str) -> str:
    """stderr minus the launcher's own startup diagnostic line (``launcher=... proxy=<version>``).

    That line is a normal start-up record, not an error event, so it is never evidence
    for a failure layer."""
    return "\n".join(
        line for line in (stderr_text or "").splitlines()
        if not line.lstrip().startswith(_NAMED_RESUME_LAUNCHER_STARTUP_LINE_PREFIX)
    )


def classify_named_subagent_resume_failure_layer(
    obs: dict, chain: dict, *, adapter: str,
    launcher_receipt: dict | None = None,
    process_exit_code: int | None = None,
    stderr_text: str = "",
) -> str | None:
    """Failure-layer classification (AC7).  ``None`` for a PASS; otherwise one of
    :data:`NAMED_RESUME_FAILURE_LAYERS`.  ``unclassified`` is the explicit
    fallback and is never promotable to PASS."""
    if chain.get("verdict") == NAMED_RESUME_VERDICT_PASS:
        return None
    reason = chain.get("chain_break")
    receipt_status = (launcher_receipt or {}).get("status") if isinstance(launcher_receipt, dict) else None
    started = obs["init"]["session_id"] is not None
    if adapter == "claude-gpt" and receipt_status in ("blocked", "failed") and not started:
        return "launcher_config"
    if not started:
        return "launcher_config" if adapter == "claude-gpt" and process_exit_code not in (None, 0) else "unclassified"
    tool_names = obs["init"]["tool_names"]
    if (
        isinstance(tool_names, list) and tool_names and reason == "no_agent_call"
        and not (_NAMED_RESUME_SPAWN_TOOL_NAMES & set(tool_names))
    ):
        return "client_schema"
    if isinstance(tool_names, list) and tool_names and "SendMessage" not in tool_names and reason in (
        "sendmessage_not_issued", "no_agent_call", "agent_call_without_name",
    ):
        # Tool list is known and does not expose SendMessage: client-side schema.
        return "client_schema"
    if any(_NAMED_RESUME_CLIENT_SCHEMA_ERROR_RE.search(err["text"] or "") for err in obs["tool_errors"]):
        return "client_schema"
    if adapter == "claude-gpt" and (
        any(
            r["is_error"] and (
                r["api_error_status"] in _NAMED_RESUME_TRANSLATION_API_STATUSES
                or _NAMED_RESUME_PROXY_ERROR_RE.search(r["text"] or "")
            )
            for r in obs["result_events"]
        )
        or _NAMED_RESUME_PROXY_ERROR_RE.search(_nr_stderr_error_text(stderr_text))
    ):
        return "proxy_translation"
    if reason in (
        "pretooluse_sendmessage_non_allow_decision",
        "pretooluse_sendmessage_hook_failed",
        "pretooluse_sendmessage_hook_unobserved",
        "agent_call_post_tool_use_unlinked",
        "resume_subagent_start_missing",
        "resume_subagent_stop_missing",
        "first_completion_not_observed",
    ):
        return "hook_lifecycle"
    if reason in (
        "no_agent_call",
        "agent_call_without_name",
        "sendmessage_not_issued",
        "sendmessage_target_is_agent_type_only",
        "resume_marker_not_from_child",
    ):
        return "backend_model_emission"
    return "unclassified"


def determine_named_resume_agent_kind(obs: dict, chain: dict) -> dict:
    """Ordinary SubAgent vs. teammate, decided ONLY from hook events and team
    signals in the stream -- never from a panel rendering or the name alone."""
    signals = obs["team_signals"]
    basis: list[str] = []
    teammate = False
    if signals["team_name_in_agent_input"]:
        teammate = True
        basis.append("agent_input_has_team_name")
    if signals["team_tool_use_events"]:
        teammate = True
        basis.append("team_tool_use_observed")
    if signals["teammate_task_types"]:
        teammate = True
        basis.append("teammate_task_type_observed")
    if signals["teammate_hook_events"]:
        teammate = True
        basis.append("teammate_hook_event_observed")
    agent_id = chain.get("agent_id")
    lifecycle_ids = {s["agent_id"] for s in obs["agent_starts"] if s["agent_id"]} | {
        s["agent_id"] for s in obs["agent_stops"] if s["agent_id"]
    }
    if teammate:
        return {"kind": "teammate", "basis": basis}
    if agent_id and agent_id in lifecycle_ids:
        basis.append("subagent_start_stop_hook_events_observed")
        basis.append("no_team_signal_observed")
        return {"kind": "ordinary_subagent", "basis": basis}
    return {"kind": "unknown", "basis": basis or ["no_subagent_lifecycle_for_agent"]}


def determine_agent_teams_effective_state(obs: dict, env: dict | None = None) -> dict:
    """Agent Teams effective state from independent signals: the runner's own
    ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`` environment value (forwarded to the
    child) and whether the child's tool list exposes the team tools."""
    env = os.environ if env is None else env
    raw = env.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
    env_enabled = isinstance(raw, str) and raw.strip().lower() in ("1", "true", "yes", "on")
    tool_names = obs["init"]["tool_names"]
    tools_known = isinstance(tool_names, list)
    team_tools_exposed = tools_known and any(t in ("TeamCreate", "TeamDelete") for t in tool_names)
    if env_enabled or team_tools_exposed:
        state = "enabled"
    elif tools_known:
        state = "disabled"
    else:
        state = "unknown"
    return {
        "effective_state": state,
        "env_flag_set": env_enabled,
        "team_tools_exposed": team_tools_exposed if tools_known else None,
        "structured_print_lane": True,
    }


# Closed allowlist of result-affecting paths (Issue #2840 AC8).  A directory
# entry ends with ``/``; every other entry is an exact file path.
NAMED_RESUME_FRESHNESS_BOTH_ADAPTER_PATHS = (
    "scripts/agent-ops/run_worktree_agent_runtime_smoke.py",
    ".claude/skills/worktree-agent-runtime-smoke/fixtures/",
    ".claude/settings.json",
    ".claude/hooks/task_context/",
    "scripts/task-context/",
)
NAMED_RESUME_FRESHNESS_CLAUDE_GPT_ONLY_PATHS = ("scripts/claude-gpt/",)


def evaluate_evidence_freshness(recorded: dict, current: dict) -> dict:
    """Pure recorded-vs-current evidence reuse decision (Issue #2840 AC8).

    ``recorded`` is the flat freshness record of ONE saved evidence file
    (``freshness_record_from_evidence``); ``current`` carries the values observed
    now.  Required ``current`` keys: ``head``, ``changed_paths`` (the
    ``git diff --name-only <recorded.tested_head>..<current.head>`` list),
    ``changed_paths_base`` (the base the diff was computed from; must equal
    ``recorded.tested_head`` whenever the heads differ), ``claude_code_version``,
    ``model_route``, ``fixture_sha256``, ``compat_note_sha256`` and, for a
    ``claude-gpt`` record, ``proxy_version`` and ``launcher_sha256``.  A missing
    key is fail-closed (not reusable); an unrelated path outside the closed
    allowlist is never a recapture reason on its own."""
    reasons: list[str] = []
    adapter = recorded.get("adapter") if isinstance(recorded, dict) else None
    if adapter not in ("native", "claude-gpt"):
        return {
            "reusable": False, "affected_adapters": [],
            "reasons": ["recorded_adapter_invalid"], "result_affecting_changed_paths": [],
        }
    if not isinstance(current, dict):
        current = {}
    required_recorded = ["tested_head", "claude_code_version", "model_route", "fixture_sha256", "compat_note_sha256"]
    required_current = [
        "head", "changed_paths", "claude_code_version", "model_route", "fixture_sha256", "compat_note_sha256",
    ]
    if adapter == "claude-gpt":
        required_recorded += ["proxy_version", "launcher_sha256"]
        required_current += ["proxy_version", "launcher_sha256"]
    for key in required_recorded:
        if not recorded.get(key):
            reasons.append(f"recorded_key_missing:{key}")
    for key in required_current:
        value = current.get(key)
        if key == "changed_paths":
            if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
                reasons.append("current_key_missing:changed_paths")
        elif not value:
            reasons.append(f"current_key_missing:{key}")
    if recorded.get("verdict") != NAMED_RESUME_VERDICT_PASS:
        reasons.append("recorded_verdict_not_pass")
    # Producer/consumer contract: only the evidence of a run whose FINAL exit code was 0
    # (``finalize_named_resume_evidence``) is a reusable success.  A missing exit code is
    # a pre-finalization record and is never reusable.
    exit_recorded = recorded.get("runner_exit_code")
    if isinstance(exit_recorded, bool) or not isinstance(exit_recorded, int) or exit_recorded != EXIT_OK:
        reasons.append("recorded_runner_exit_code_not_zero")
    heads_differ = bool(recorded.get("tested_head")) and recorded.get("tested_head") != current.get("head")
    if heads_differ and current.get("changed_paths_base") != recorded.get("tested_head"):
        reasons.append("changed_paths_base_mismatch")

    both_prefixes = NAMED_RESUME_FRESHNESS_BOTH_ADAPTER_PATHS
    gpt_prefixes = NAMED_RESUME_FRESHNESS_CLAUDE_GPT_ONLY_PATHS

    def _matches(path: str, entries: tuple[str, ...]) -> bool:
        normalized = path[2:] if path.startswith("./") else path
        return any(
            normalized.startswith(entry) if entry.endswith("/") else normalized == entry
            for entry in entries
        )

    changed = current.get("changed_paths") if isinstance(current.get("changed_paths"), list) else []
    affecting: list[str] = []
    affected_adapters: set[str] = set()
    for path in changed:
        if not isinstance(path, str):
            continue
        if _matches(path, both_prefixes):
            affecting.append(path)
            affected_adapters.update({"native", "claude-gpt"})
            reasons.append(f"result_affecting_path_changed:{path}")
        elif _matches(path, gpt_prefixes):
            affecting.append(path)
            affected_adapters.add("claude-gpt")
            if adapter == "claude-gpt":
                reasons.append(f"result_affecting_path_changed:{path}")
    for key in ("claude_code_version", "model_route", "fixture_sha256", "compat_note_sha256"):
        if recorded.get(key) and current.get(key) and recorded.get(key) != current.get(key):
            reasons.append(f"{key}_changed")
    if adapter == "claude-gpt":
        for key in ("proxy_version", "launcher_sha256"):
            if recorded.get(key) and current.get(key) and recorded.get(key) != current.get(key):
                reasons.append(f"{key}_changed")
    return {
        "reusable": not reasons,
        "affected_adapters": sorted(affected_adapters),
        "reasons": reasons,
        "result_affecting_changed_paths": affecting,
    }




def freshness_record_from_evidence(evidence: dict) -> dict:
    """Flatten a saved named-resume evidence JSON into the ``recorded`` mapping
    consumed by :func:`evaluate_evidence_freshness`."""
    launcher = evidence.get("launcher") if isinstance(evidence.get("launcher"), dict) else {}
    proxy = evidence.get("proxy") if isinstance(evidence.get("proxy"), dict) else {}
    route = evidence.get("model_route") if isinstance(evidence.get("model_route"), dict) else {}
    fixtures = evidence.get("fixtures") if isinstance(evidence.get("fixtures"), dict) else {}
    return {
        "adapter": evidence.get("adapter"),
        "verdict": evidence.get("verdict"),
        "runner_exit_code": evidence.get("runner_exit_code"),
        "tested_head": evidence.get("tested_head"),
        "claude_code_version": evidence.get("claude_code_version"),
        "model_route": route.get("observed_main_model"),
        "proxy_version": proxy.get("version"),
        "launcher_sha256": launcher.get("sha256"),
        "fixture_sha256": fixtures.get("prompt_sha256"),
        "compat_note_sha256": fixtures.get("compat_note_sha256"),
    }


def compute_changed_paths(repo_dir: str, base: str, head: str) -> list[str] | None:
    """``git diff --name-only <base>..<head>`` (``None`` when git fails)."""
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, timed_out = _run([git, "-C", repo_dir, "diff", "--name-only", f"{base}..{head}"], timeout=30.0)
    if rc != 0 or timed_out:
        return None
    return [line for line in out.splitlines() if line.strip()]


def _nr_launcher_receipt_public(receipt: dict | None) -> dict | None:
    if not isinstance(receipt, dict):
        return None
    return {
        key: (_nr_public_text(str(receipt[key])) if isinstance(receipt[key], str) else receipt[key])
        for key in ("schema", "status", "reason", "flag")
        if key in receipt
    }


def _nr_stderr_proxy_version(stderr_text: str) -> str | None:
    """Proxy version string from the launcher's own startup diagnostic line
    (``launcher=... proxy=<version line>``)."""
    for line in (stderr_text or "").splitlines():
        match = re.search(r"\bproxy=(.+)$", line.strip())
        if line.strip().startswith("launcher=") and match:
            return _nr_public_text(match.group(1).strip())[:200] or None
    return None


def _nr_launcher_model_policy_main(launcher_path: str | None) -> str | None:
    if not launcher_path:
        return None
    lib = Path(launcher_path).resolve().parent / "lib.sh"
    try:
        text = lib.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^CLAUDE_GPT_MODEL_MAIN="([^"\n]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def build_named_subagent_resume_evidence(
    *, stdout: str, stderr: str, process_exit_code: int | None, timed_out: bool,
    adapter: str, tested_head: str | None, runtime_version: str | None,
    resolved_runtime_bin: str | None, worktree: str,
    prompt_fixture_path: str | None, compat_fixture_path: str | None,
    compat_note_applied: bool, invocation_flags: list[str],
    launcher_receipt: dict | None = None, env: dict | None = None,
    proxy_bin: str | None = None,
) -> dict:
    """Public-safe evidence for one named SubAgent resume run (AC2/AC3/AC4/AC8/AC9).

    Contains ids, hashes, versions, booleans and event counts only: never a raw
    prompt, message body, transcript, credential or HOME absolute path."""
    obs = extract_named_subagent_resume_observations(stdout)
    chain = evaluate_named_subagent_resume_chain(obs)
    layer = classify_named_subagent_resume_failure_layer(
        obs, chain, adapter=adapter, launcher_receipt=launcher_receipt,
        process_exit_code=process_exit_code, stderr_text=stderr,
    )
    verdict = chain["verdict"]
    if verdict == NAMED_RESUME_VERDICT_FAIL and layer is None:
        layer = "unclassified"
    if timed_out and verdict == NAMED_RESUME_VERDICT_PASS:
        verdict, layer = NAMED_RESUME_VERDICT_FAIL, "unclassified"
    is_gpt = adapter == "claude-gpt"
    launcher_abs = resolved_runtime_bin if is_gpt else None
    launcher_sha256 = _nr_file_sha256(launcher_abs) if is_gpt else None
    proxy_path = proxy_bin
    if is_gpt and proxy_path is None:
        env_proxy = (env or os.environ).get("CLAUDE_GPT_PROXY_BIN")
        proxy_path = env_proxy or shutil.which("claude-code-proxy")
    proxy_version = None
    if is_gpt:
        proxy_version = _nr_stderr_proxy_version(stderr)
        if proxy_version is None and proxy_path:
            rc, out, err, _t = _run([proxy_path, "--version"], timeout=10.0, input_text="")
            version_text = (out or err).strip()
            if rc == 0 and version_text:
                proxy_version = _nr_public_text(version_text.splitlines()[0])[:200]
    agent_kind = determine_named_resume_agent_kind(obs, chain)
    teams = determine_agent_teams_effective_state(obs, env)
    claude_code_version = obs["init"]["claude_code_version"]
    if not claude_code_version and runtime_version:
        claude_code_version = _nr_public_text(runtime_version)
    caller = obs["init"]["session_id"]
    evidence: dict = {
        "schema": NAMED_SUBAGENT_RESUME_EVIDENCE_SCHEMA,
        # ``verdict`` / ``failure_layer`` are the RUN-level result.  Here they are provisional
        # (stream-only); ``finalize_named_resume_evidence`` fixes them once from the final
        # exit code, after every later assertion ran.  ``causal_chain_verdict`` is the
        # causal chain's own result and stays separate from the run-level verdict.
        "verdict": verdict,
        "failure_layer": layer,
        "causal_chain_verdict": chain["verdict"],
        "chain_break": chain["chain_break"],
        "tested_head": tested_head,
        "claude_code_version": claude_code_version,
        "adapter": adapter,
        "runtime": "claude",
        "mode": "structured",
        "permission_mode_observed": obs["init"]["permission_mode"],
        "launcher": {
            "path": (
                _nr_public_path(launcher_abs, repo_root=worktree) if is_gpt
                else "native-claude-binary"
            ),
            "sha256": launcher_sha256,
        },
        "proxy": {
            "path": _nr_public_path(proxy_path, repo_root=worktree) if is_gpt else None,
            "version": proxy_version,
        },
        "model_route": {
            "observed_main_model": obs["init"]["model"],
            "launcher_policy_main_model": _nr_launcher_model_policy_main(launcher_abs) if is_gpt else None,
        },
        "agent_name": chain["agent_name"],
        "agent_name_matches_fixture": chain["agent_name"] == NAMED_SUBAGENT_RESUME_AGENT_NAME,
        "agent_id": chain["agent_id"],
        "addressing": chain["addressing"],
        "caller_session_id": caller,
        "agent_teams": teams,
        "agent_kind": agent_kind,
        "first_completion": {
            "observed": chain["steps"]["first_completion"],
            "marker_from_child": chain["first_completion_marker_from_child"],
        },
        "same_agent_id_after_resume": bool(
            chain["steps"]["same_agent_id_resumed"] and chain["steps"]["no_new_agent_invocation"]
        ),
        "causal_chain": dict(chain["steps"]),
        "agent_id_lane": chain["agent_id_lane"],
        "sendmessage_hook_decisions": chain["sendmessage_decisions"],
        "sendmessage_hook_observation": chain.get("hook_observation"),
        "false_ask_observed": any(
            d["decision"] in _NAMED_RESUME_NON_ALLOW_DECISIONS for d in chain["sendmessage_decisions"]
        ),
        "hook_event_counts": {
            key: obs["hook_event_counts"].get(key, 0)
            for key in ("SubagentStart", "SubagentStop", "PostToolUse", "PreToolUse")
        },
        "new_agent_ids_after_spawn": chain["new_agent_ids_after_spawn"],
        "process_exit_code": process_exit_code,
        "timed_out": timed_out,
        "invocation_flags_readback": invocation_flags,
        "compat_note": {
            "applied_via_append_system_prompt_file": compat_note_applied,
            "fixture_path": NAMED_SUBAGENT_RESUME_COMPAT_FIXTURE_RELPATH if compat_note_applied else None,
        },
        "fixtures": {
            "prompt_path": NAMED_SUBAGENT_RESUME_PROMPT_FIXTURE_RELPATH,
            "prompt_sha256": _nr_file_sha256(prompt_fixture_path),
            "compat_note_path": NAMED_SUBAGENT_RESUME_COMPAT_FIXTURE_RELPATH,
            "compat_note_sha256": _nr_file_sha256(compat_fixture_path),
        },
        "launcher_receipt": _nr_launcher_receipt_public(launcher_receipt),
    }
    return _nr_assert_public_safe(evidence)


def finalize_named_resume_evidence(evidence: dict, exit_code: int) -> dict:
    """Fix the run-level ``verdict`` / ``failure_layer`` / ``runner_exit_code`` ONCE, from the
    runner's FINAL exit code (after every assertion -- ``--expect-marker``, ordered markers,
    output schema, required runtime observations -- has had its say).

    ``causal_chain_verdict`` (the chain's own result) is kept untouched and separate.  The
    exit code is authoritative: a run that exits non-zero is never ``pass`` and a run that
    exits 0 is ``pass`` only when the stream-level verdict is ``pass``."""
    final = dict(evidence)
    provisional = final.get("verdict")
    final["causal_chain_verdict"] = final.get("causal_chain_verdict", provisional)
    final["runner_exit_code"] = exit_code
    if exit_code == EXIT_OK:
        if provisional != NAMED_RESUME_VERDICT_PASS:
            final["verdict"] = NAMED_RESUME_VERDICT_FAIL
            final["failure_layer"] = final.get("failure_layer") or "unclassified"
            final["chain_break"] = final.get("chain_break") or "runner_exit_ok_without_chain_pass"
        else:
            final["failure_layer"] = None
    elif exit_code == EXIT_SKIP:
        final["verdict"] = NAMED_RESUME_VERDICT_SKIP
        if provisional != NAMED_RESUME_VERDICT_SKIP:
            final["failure_layer"] = None
            final["chain_break"] = "runner_skipped_before_chain_evaluation"
    else:
        final["verdict"] = NAMED_RESUME_VERDICT_FAIL
        if provisional == NAMED_RESUME_VERDICT_PASS:
            final["failure_layer"] = "unclassified"
            final["chain_break"] = "runner_outcome_not_ok_despite_chain"
        elif not final.get("failure_layer"):
            final["failure_layer"] = "unclassified"
    return final


def _nr_assert_public_safe(evidence: dict) -> dict:
    """Last-line scrub: any string that still carries a HOME-style absolute path is
    replaced.  (The evidence is built from ids/hashes/booleans, so this only guards
    against an unexpected path leaking through a diagnostic field.)"""
    def _scrub(value):
        if isinstance(value, str):
            return _nr_public_text(value)
        if isinstance(value, list):
            return [_scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: _scrub(item) for key, item in value.items()}
        return value
    return _scrub(evidence)


def named_resume_invocation_flag_readback(adapter: str, compat_note_applied: bool) -> list[str]:
    """Flag-token readback of the fixed argv ``run_structured_claude`` builds for
    the named SubAgent resume scenario (flag names only -- never a value/path).
    A test pins this against the argv actually handed to the subprocess layer."""
    flags = ["-p", "--output-format", "--include-hook-events", "--max-turns", "--verbose"]
    if adapter == "native":
        flags.append("--settings")
    if compat_note_applied:
        flags.append("--append-system-prompt-file")
    return flags


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


def _absolute_existing_file(value: str) -> str:
    """argparse ``type`` for ``--append-system-prompt-file`` (Issue #2840): an
    absolute path to an existing regular file."""
    if not os.path.isabs(value):
        raise argparse.ArgumentTypeError(f"path must be absolute, got: {value!r}")
    if not os.path.isfile(value):
        raise argparse.ArgumentTypeError(f"path is not an existing file: {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="worktree-agent-runtime-smoke runner")
    # Issue #2161 (native Codex CLI retirement): "codex" was removed from
    # the choices; only the "claude" (native) lane remains.
    parser.add_argument("--runtime", choices=["claude"], required=True)
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
    parser.add_argument(
        "--claude-adapter",
        choices=["native", "claude-gpt"],
        default="native",
        help=(
            "Issue #2174 AC1 (fix_delta, OWNER REQUEST_CHANGES "
            "https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173): "
            "explicit, INDEPENDENT launcher-adapter selection -- never "
            "derived from bool(--claude-bin) alone. '--claude-bin' by "
            "itself is a pure binary-path override with no argv/env side "
            "effects (same argv shape as PATH resolution, e.g. for an "
            "absolute-path native claude binary or a transparent wrapper). "
            "'--claude-adapter claude-gpt' (requires --claude-bin) is what "
            "opts into scripts/claude-gpt/launch.sh's own CLI contract: a "
            "literal `--` separator before claude's own fixed argv, and "
            "CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop instead of "
            "a caller-supplied --settings JSON flag (which that launcher "
            "rejects as a policy-weakening flag). Default 'native' keeps "
            "every pre-existing --claude-bin caller's argv unchanged."
        ),
    )
    parser.add_argument(
        "--evidence-json",
        default=None,
        help=(
            "Issue #2231: optional path to write the full schema_summary "
            "(WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1) as machine-generated "
            "JSON, in addition to the existing summary.md written under "
            "--output-dir. Enables callers (e.g. regression tests) to "
            "assert on live evidence fields such as "
            "causal_evidence_source and exit_code without re-parsing "
            "summary.md's str()-rendered lines. The parent directory must "
            "already exist; the file itself is created/overwritten. "
            "Omitted by default (None), so every pre-existing caller's "
            "behavior is unchanged."
        ),
    )
    parser.add_argument("--expect-marker", action="append", default=[])
    parser.add_argument(
        "--expect-ordered-marker",
        action="append",
        default=[],
        help=(
            "Issue #2498 AC1 (skill-invocation-runtime-smoke profile, "
            "procedure_steps_executed_in_declared_order assertion): a "
            "caller-supplied ORDERED expected-evidence-marker list "
            "(repeatable, given in the expected order), matched against the "
            "structured lane's own captured native stdout via "
            "evaluate_ordered_evidence_match() (a literal, sequential "
            "subsequence match -- this runner never interprets an arbitrary "
            "SKILL.md Markdown Procedure). Independent of --expect-marker: "
            "using this flag alone never triggers the SubAgent "
            "causal-evidence default gate. Applies to --mode structured "
            "only. Omitted by default, so every pre-existing caller's "
            "behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--output-schema-path",
        default=None,
        help=(
            "Issue #2498 AC2 (skill-invocation-runtime-smoke profile, "
            "output_contract_schema_fields_present assertion): path to a "
            "single, pre-existing, caller-supplied canonical JSON Schema "
            "file (e.g. .claude/skills/review-issue/schemas/"
            "review_issue_result_v1.json) that the structured lane's final "
            "native result text is validated against via full "
            "jsonschema.validate() (evaluate_output_contract_schema_fields_"
            "present()) -- never a new required-key-existence-only checker, "
            "never a generic multi-domain schema registry. Applies to "
            "--mode structured only. Omitted by default, so every "
            "pre-existing caller's behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--expect-marker-source",
        choices=["main", "subagent"],
        default="subagent",
        help=(
            "Issue #2498 AC3: additive main/subagent evidence-provenance "
            "input for --expect-marker's existing SubAgent causal-evidence "
            "default gate (structured lane, Issue #2183 fix-delta). "
            "'subagent' (default): byte-identical to every pre-existing "
            "caller's implicit behavior -- causal_evidence_source == "
            "hook_id_correlated is still required by default whenever "
            "--expect-marker is given. 'main': opts OUT of that default "
            "gate for a run asserting a DIRECT (non-delegated) Skill "
            "invocation instead -- requires --expect-skill-command "
            "(mandatory pairing; 'main' alone is a usage error, never a "
            "silent unconditional causal-evidence opt-out). "
            "--require-subagent-causal-evidence (an explicit, independent "
            "ask) is unaffected by this flag's value either way."
        ),
    )
    parser.add_argument(
        "--expect-skill-command",
        default=None,
        help=(
            "Issue #2498 AC4: paired with --expect-marker-source main. "
            "Verifies a direct Skill/slash-command invocation occurred via "
            "the native UserPromptExpansion hook event's own "
            "'command_name' field (extract_claude_user_prompt_expansion_"
            "command_names()) -- never the undocumented 'command_source' "
            "value domain. Requires --mode structured and --claude-adapter "
            "native. Omitted by default, so every pre-existing caller's "
            "argv and behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--require-subagent-causal-evidence",
        action="store_true",
        help=(
            "Issue #2183 (PR #2220 review fix-delta): require "
            "subagent_causal_evidence_verdict()'s causal_evidence_source to be "
            "hook_id_correlated (a same-agent_id SubagentStart/SubagentStop "
            "pair with a recovered agent_transcript_path) for exit_code to "
            "remain PASS -- a marker string observed in stdout/pane text "
            "alone is never sufficient. The causal-evidence verdict itself "
            "is always recorded in schema_summary['subagent_causal_evidence'] "
            "regardless of this flag. In the STRUCTURED lane, this "
            "requirement is already applied BY DEFAULT (no flag needed) "
            "whenever --expect-marker is also given, since the structured "
            "lane always has the hook stream-json channel available; this "
            "flag is only needed there to require the gate on structured "
            "runs that pass no --expect-marker at all. In the INTERACTIVE "
            "lane, and for structured runs with no --expect-marker, this "
            "flag remains genuinely opt-in: the herdr pane text does not "
            "structurally carry hook payload today, so pre-existing "
            "interactive-lane callers' exit_code is unchanged by default."
        ),
    )
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
    parser.add_argument(
        "--require-hook-chain-evidence",
        action="store_true",
        help=(
            "Issue #2663: opt-in (default off). Requires --runtime claude "
            "--mode structured. When set, additively registers a bounded, "
            "closed pair of observation-only 'cat' hooks (PreToolUse/Bash, "
            "Stop -- never a caller-supplied command/path/marker/config) "
            "and evaluates two independent assertions against the ALREADY "
            "captured native stream-json evidence: "
            "all_matching_hooks_observed (every command hook currently "
            "registered in this tested_head's own .claude/settings.json "
            "for PreToolUse/Bash, excluding this runner's own observer, "
            "produced matched hook_started+hook_response completion "
            "evidence for each real Bash tool call) and "
            "sibling_side_effect_inventory_complete (a genuine new-file "
            "post-condition under artifacts/session-manifest-runtime/"
            "manifests/, read only after the whole subprocess exits, "
            "independent of any hook's own exit code). Both are recorded "
            "in schema_summary['hook_chain_evidence'] with a pass|fail|"
            "unverified status; exit_code FAILs when either is not "
            "status: pass. Omitted by default, so every pre-existing "
            "caller's argv/behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--task-context-scope",
        default=None,
        help=(
            "Issue #2568 In Scope: purely additive environment/carrier "
            "passthrough. When given, forwarded verbatim to the launched "
            "child runtime process/session as LOOP_TASK_CONTEXT_SCOPE "
            "(structured lane: subprocess env; interactive lane: herdr "
            "'workspace create --env' plus a pane re-export, same "
            "mechanism as the existing launcher/hook-sink env pairs). This "
            "runner never interprets or validates the value -- Task "
            "Context-specific semantics (e.g. gating "
            "'task-contextctl smoke seed' on scope=='runtime_smoke') live "
            "in scripts/task-context/task_contextctl.py and "
            "task_context_runtime_smoke_verifier.py, never here. Omitted "
            "by default (None), so every pre-existing caller's argv/env is "
            "unchanged."
        ),
    )
    parser.add_argument(
        "--task-context-state-root",
        default=None,
        help=(
            "Issue #2568 In Scope: the LOOP_TASK_CONTEXT_STATE_ROOT "
            "counterpart to --task-context-scope above -- same additive, "
            "opt-in, uninterpreted passthrough mechanism. Callers pass a "
            "run-scoped isolated absolute path (e.g. from "
            "task_context_runtime_smoke_verifier.build_isolated_state_root)."
        ),
    )
    parser.add_argument(
        "--require-session-baseline-preservation",
        action="store_true",
        help=(
            "interactive mode only: explicitly opt in to before/after "
            "preservation observation of pre-existing Herdr sessions. The "
            "runner snapshots session identity and full workspace/agent/focus "
            "state before and after its own isolated lane, excluding only its "
            "temporary session; any unavailable snapshot or observed change "
            "fails closed. Omitted by default: the normal lane never lists, "
            "snapshots, or observes pre-existing/human namespaces."
        ),
    )
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
    parser.add_argument(
        "--approval-profile",
        choices=list(_load_approval_contract().approval_profile_ids()),
        default=None,
        help=(
            "Issue #2839: opt-in approval carrier. closed enum の profile id だけを受け付け "
            "(任意の JSON / 文字列 / path は受け付けない)、registry 由来の固定 overlay "
            "(autoMode.allow に \"$defaults\" と固定 rule 1 件) を、当該 invocation の "
            "--settings にだけ載せる。native adapter かつ structured mode 専用で、"
            "--expect-skill-command / --require-hook-chain-evidence / "
            "--hermetic-agent-definition とは併用できない。precondition (fixture の実体と "
            "CLAUDE_GPT_HOME / CLAUDE_GPT_REPAIR_INSTALLER_URL の束縛) が不成立なら子 "
            "session を起動せず fail-closed で終了する。未指定なら従来と byte-identical。"
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
    parser.add_argument(
        "--require-min-subagents",
        type=int,
        default=0,
        help=(
            "Issue #2219 AC3/AC11: opt-in (default 0 = not required). When "
            "> 0, requires at least this many DISTINCT SubAgents to be "
            "spawned AND completed (agent_id exact pairing, "
            "classify_claude_multi_child_lifecycle) or the run FAILs. "
            "Applies to --runtime claude only. Omitted (0) by default, so "
            "every pre-existing caller's behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--require-min-turns",
        type=int,
        default=0,
        help=(
            "Issue #2219 AC2/AC11: opt-in (default 0 = not required). When "
            "> 0, requires the SAME main session_id to persist across at "
            "least this many agentic-loop turns within a single structured "
            "-lane invocation (verify_same_main_session_across_turns) or the "
            "run FAILs. Requires --max-turns >= this value (validated below "
            "as a parser.error, never silently clamped). Applies to "
            "--runtime claude only. Omitted (0) by default, so every "
            "pre-existing caller's behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--scan-forbidden-markers",
        action="store_true",
        help=(
            "Issue #2219 AC6: opt-in (default off). When set, scans "
            "captured output (structured lane: stdout/stderr; interactive "
            "lane: the persisted session transcript plus the bounded pane "
            "excerpt) for a fixed, literal forbidden failure marker "
            "allowlist (verify_no_forbidden_marker) -- e.g. '403 WebSocket "
            "upgrade', 'Please run /login' -- and FAILs if any is observed. "
            "Applies to --runtime claude only. Omitted (off) by default, so "
            "every pre-existing caller's behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--additional-prompt",
        action="append",
        default=[],
        help=(
            "Issue #2219 fix_delta iteration 1 (Option B reintroduction, in "
            "the spirit of PR #2176 commit 06d8baa9's --additional-prompt, "
            "reverted in commit 5a44ebf0 for being out of scope for Issue "
            "#2174): interactive mode only. An extra prompt turn sent to "
            "the SAME already-started agent/session after the initial "
            "--prompt-file turn settles. May be repeated (sent in the "
            "given order) to drive a multi-turn operator journey inside "
            "one isolated interactive session. Combine with "
            "--require-min-turns / --require-min-subagents / "
            "--scan-forbidden-markers to verify same-session-identity, "
            "multi-SubAgent lifecycle, and forbidden-marker absence across "
            "the resulting persisted session transcript. Requires --mode "
            "interactive and --runtime claude. Omitted by default, so "
            "every pre-existing caller's single-turn behavior is "
            "unchanged."
        ),
    )
    parser.add_argument(
        "--named-subagent-resume",
        action="store_true",
        help=(
            "Issue #2840: opt-in named SubAgent scenario (structured lane only). "
            "Drives and observes ordinary named SubAgent spawn -> completion -> "
            "SendMessage(to=<name>) resume -> completion -> parent retrieval. "
            "Drops ONLY the harness's blanket SendMessage deny (ListAgents deny, "
            "crossSessionInbound: refuse, the Task Context hook, the normal "
            "permission decision flow and isolation are kept) and registers a "
            "generic observation hook set (SubagentStart / SubagentStop / "
            "PostToolUse:Agent / PreToolUse:SendMessage). No Task Context "
            "semantic verdict is produced here. With --claude-adapter "
            "claude-gpt and no --claude-bin, the checkout's own "
            "scripts/claude-gpt/launch.sh is resolved as an absolute path. "
            "Omitted by default, so the default smoke policy/output is unchanged."
        ),
    )
    parser.add_argument(
        "--append-system-prompt-file",
        type=_absolute_existing_file,
        default=None,
        help=(
            "Issue #2840: absolute path to an invocation-local compatibility "
            "note forwarded as the CLI's own --append-system-prompt-file "
            "(requires --named-subagent-resume; never a permanent system-prompt "
            "injection)."
        ),
    )
    parser.add_argument(
        "--named-resume-evidence-json",
        default=None,
        help=(
            "Issue #2840: path of the public-safe named SubAgent resume evidence "
            "JSON (requires --named-subagent-resume; the parent directory must "
            "already exist). Unlike --evidence-json it carries only ids, hashes, "
            "versions, booleans and event counts."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.claude_adapter == "claude-gpt" and not args.claude_bin and not args.named_subagent_resume:
        parser.error("--claude-adapter claude-gpt requires --claude-bin")
    # Issue #2840: scenario-only flags and their incompatible combinations.
    if args.append_system_prompt_file and not args.named_subagent_resume:
        parser.error("--append-system-prompt-file requires --named-subagent-resume")
    if args.named_resume_evidence_json and not args.named_subagent_resume:
        parser.error("--named-resume-evidence-json requires --named-subagent-resume")
    if args.named_subagent_resume:
        if args.runtime != "claude":
            parser.error("--named-subagent-resume requires --runtime claude")
        if args.mode != "structured":
            parser.error("--named-subagent-resume requires --mode structured")
        for flag_name, enabled in (
            ("--approval-profile", bool(args.approval_profile)),
            ("--expect-skill-command", bool(args.expect_skill_command)),
            ("--require-hook-chain-evidence", bool(args.require_hook_chain_evidence)),
            ("--hermetic-agent-definition", bool(args.hermetic_agent_definition)),
            ("--claude-agent-name", bool(args.claude_agent_name)),
        ):
            if enabled:
                parser.error(f"--named-subagent-resume cannot be combined with {flag_name}")

    # PR #2176 OWNER REQUEST_CHANGES Finding 6
    # (https://github.com/squne121/loop-protocol/pull/2176#issuecomment-5302819792):
    # claude-specific inputs combined with --runtime codex were previously
    # silently accepted and ignored while Codex actually ran, which could
    # let a caller believe they had validated claude-gpt when they had
    # actually run Codex. Likewise --hermetic-agent-definition outside
    # structured mode was previously accepted and silently ignored.
    if args.runtime != "claude":
        if args.claude_bin:
            parser.error("--claude-bin requires --runtime claude")
        if args.claude_adapter != "native":
            parser.error("--claude-adapter requires --runtime claude")
        if args.claude_agent_name:
            parser.error("--claude-agent-name requires --runtime claude")
        if args.hermetic_agent_definition:
            parser.error("--hermetic-agent-definition requires --runtime claude")
    if args.mode != "structured" and args.hermetic_agent_definition:
        parser.error("--hermetic-agent-definition requires --mode structured")
    if args.require_min_subagents < 0:
        parser.error("--require-min-subagents must be >= 0")
    if args.require_min_turns < 0:
        parser.error("--require-min-turns must be >= 0")
    if args.mode == "structured" and args.require_min_turns > 0 and args.max_turns < args.require_min_turns:
        parser.error("--require-min-turns requires --max-turns >= --require-min-turns")
    if args.require_min_subagents > 0 and args.runtime != "claude":
        parser.error("--require-min-subagents requires --runtime claude")
    if args.require_min_turns > 0 and args.runtime != "claude":
        parser.error("--require-min-turns requires --runtime claude")
    if args.scan_forbidden_markers and args.runtime != "claude":
        parser.error("--scan-forbidden-markers requires --runtime claude")
    if args.additional_prompt and args.mode != "interactive":
        parser.error("--additional-prompt requires --mode interactive")
    if bool(args.task_context_scope) != bool(args.task_context_state_root):
        # Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 1: the two
        # carrier flags are a single atomic pair -- reject a half-carrier
        # here, before any worktree/child-process setup below, rather than
        # letting it reach _task_context_env_pairs()'s own later raise.
        parser.error(
            "--task-context-scope and --task-context-state-root must be "
            "given together (both or neither)"
        )
    if args.additional_prompt and args.runtime != "claude":
        parser.error("--additional-prompt requires --runtime claude")
    # Issue #2839: opt-in approval carrier。子 session を起動する前に precondition を
    # 決定論的に検証し、不成立なら fail-closed で終了する (PASS / SKIP にはしない)。
    approval_mod = None
    approval_verified = None
    approval_overlay_json = None
    approval_child_env = None
    if args.approval_profile:
        approval_mod = _load_approval_contract()
        if args.runtime != "claude":
            parser.error("--approval-profile requires --runtime claude")
        approval_verified = approval_mod.verify_approval_carrier_preconditions(
            args.approval_profile,
            worktree=os.path.abspath(args.worktree),
            env=os.environ,
            claude_adapter=args.claude_adapter,
            mode=args.mode,
            incompatible_flags={
                "expect_skill_command": bool(args.expect_skill_command),
                "hermetic_agent_definition": bool(args.hermetic_agent_definition),
            },
        )
        if not approval_verified["ok"]:
            parser.error(
                f"--approval-profile precondition failed: {approval_verified['reason_code']}"
            )
        # Issue #2839 (PR #2844 OWNER fix_delta): runner が実際に選択する観測 overlay
        # (--require-hook-chain-evidence 時は PreToolUse / Stop を含む) に carrier の
        # ``autoMode`` を足す。overlay を作り直さず、``--settings`` は 1 個のまま。
        approval_overlay_json = approval_mod.build_approval_overlay_json(
            args.approval_profile,
            select_native_observation_settings_json(
                include_user_prompt_expansion_hook=bool(args.expect_skill_command),
                include_hook_chain_evidence_hooks=bool(args.require_hook_chain_evidence),
            ),
        )
        approval_child_env = approval_mod.build_approval_child_env(os.environ, approval_verified)
    # Issue #2219 fix_delta iteration 1 (Option B): the interactive lane's
    # own turn count is 1 (the initial --prompt-file turn) plus however many
    # --additional-prompt entries were supplied -- there is no --max-turns
    # equivalent for this lane (never forwarded to the TUI launch, see AC5).
    if (
        args.mode == "interactive"
        and args.require_min_turns > 0
        and (1 + len(args.additional_prompt)) < args.require_min_turns
    ):
        parser.error(
            "--require-min-turns requires enough --additional-prompt entries "
            "(1 initial turn + len(--additional-prompt) must be >= --require-min-turns)"
        )

    # Issue #2183 PR #2220 OWNER REQUEST_CHANGES P0-1
    # (https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514),
    # narrowed by the follow-up fix-delta
    # (https://github.com/squne121/loop-protocol/pull/2220 P0-1 re-review):
    # ``subagent_causal_evidence_verdict()`` is only ever computed
    # (non-``None``) for ``--runtime claude`` -- every other runtime leaves
    # ``causal_evidence`` unconditionally ``None``, which
    # ``--require-subagent-causal-evidence`` would then always fail on,
    # regardless of whether the underlying run actually succeeded. Reject
    # that combination at parse time (fail fast, before any process is
    # spawned) rather than let it silently FAIL every such run downstream.
    #
    # This must NOT also reject ``--mode interactive``: the interactive
    # lane computes the SAME ``causal_evidence`` verdict for
    # ``--runtime claude`` (see ``run_interactive_herdr_isolated`` call
    # site below) and honors ``--require-subagent-causal-evidence`` as a
    # genuine opt-in gate there too (Issue #2183 AC10) -- it simply tends
    # to observe ``no_evidence``/``marker_only_insufficient`` in practice,
    # because the herdr pane render does not echo the
    # ``--include-hook-events`` stream-json hook payloads the structured
    # lane can parse. That is an expected gate *outcome*, not a reason to
    # reject the flag/mode combination outright.
    if args.require_subagent_causal_evidence and args.runtime != "claude":
        parser.error(
            "--require-subagent-causal-evidence requires --runtime claude"
        )

    # Issue #2663: the hook-chain-evidence observer hooks and its two
    # assertions are only meaningful for a direct-subprocess structured
    # claude invocation (the native stream-json --include-hook-events
    # channel this whole capability reads).
    if args.require_hook_chain_evidence and (args.runtime != "claude" or args.mode != "structured"):
        parser.error("--require-hook-chain-evidence requires --runtime claude --mode structured")
    # The observer-hook / --setting-sources injection above (see
    # run_structured_claude) is only wired into the native adapter's
    # branch -- the claude-gpt launcher owns its own separate settings
    # mechanism (CLAUDE_GPT_RUNTIME_SMOKE_HOOKS) and forbids any
    # additional --settings/--setting-sources flag outright.
    if args.require_hook_chain_evidence and args.claude_adapter != "native":
        parser.error("--require-hook-chain-evidence requires --claude-adapter native")

    # Issue #2498 AC4 (Step 2.5 semantic design review, severity: high):
    # '--expect-marker-source main' MUST NOT be usable on its own -- that
    # would degrade into an unconditional opt-out of the SubAgent
    # causal-evidence gate with no substitute assertion at all. Rejected at
    # parse time (fail fast, before any process is spawned), exactly like
    # the pre-existing flag/runtime combination checks above.
    if args.expect_marker_source == "main" and not args.expect_skill_command:
        parser.error(
            "--expect-marker-source main requires --expect-skill-command "
            "(mandatory pairing; 'main' alone would be an unconditional "
            "causal-evidence opt-out)"
        )
    if args.expect_skill_command and args.mode != "structured":
        parser.error("--expect-skill-command requires --mode structured")
    if args.expect_skill_command and args.runtime != "claude":
        parser.error("--expect-skill-command requires --runtime claude")
    if args.expect_skill_command and args.claude_adapter != "native":
        parser.error("--expect-skill-command requires --claude-adapter native")
    if args.expect_ordered_marker and args.mode != "structured":
        parser.error("--expect-ordered-marker requires --mode structured")
    if args.output_schema_path and args.mode != "structured":
        parser.error("--output-schema-path requires --mode structured")

    # PR #2500 fix_delta P2-3 (OWNER REQUEST_CHANGES
    # https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805):
    # a structurally invalid --output-schema-path (one jsonschema.validate()
    # itself would reject via its own meta-schema check, raising
    # jsonschema.exceptions.SchemaError -- distinct from ValidationError,
    # which is a rejection of the INSTANCE, not the schema) must fail here,
    # at argument-validation time, BEFORE the (possibly expensive) runtime
    # subprocess is ever launched -- never as an unhandled exception deep
    # inside evaluate_output_contract_schema_fields_present() after runtime
    # cost has already been paid and no evidence artifact survives to
    # explain the failure. jsonschema.validators.validator_for() picks the
    # exact same validator class jsonschema.validate() would use later for
    # the real instance validation, so this check-schema call is guaranteed
    # consistent with that later validate() call.
    if args.output_schema_path:
        try:
            schema_text = Path(args.output_schema_path).read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"--output-schema-path could not be read: {exc}")
        try:
            schema_obj = json.loads(schema_text)
        except (json.JSONDecodeError, ValueError) as exc:
            parser.error(f"--output-schema-path is not valid JSON: {exc}")
        try:
            validator_cls = jsonschema.validators.validator_for(schema_obj)
            validator_cls.check_schema(schema_obj)
        except jsonschema.exceptions.SchemaError as exc:
            parser.error(f"--output-schema-path is not a valid JSON Schema: {exc}")

    run_id = uuid.uuid4().hex[:12]
    errors: list[str] = []

    repo_root = args.repo_root or _default_repo_root()

    try:
        worktree = verify_worktree_identity(args.worktree, repo_root)
    except IdentityError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return EXIT_FAIL

    if args.named_subagent_resume and args.claude_adapter == "claude-gpt" and not args.claude_bin:
        # Issue #2840: the repository-owned launcher of the checkout under test
        # (the tested HEAD's own scripts/claude-gpt/launch.sh), resolved as an
        # absolute path so no VC depends on a machine-specific path or shell
        # substitution. An explicit --claude-bin is never overridden.
        launcher_candidate = os.path.join(worktree, NAMED_SUBAGENT_RESUME_LAUNCHER_RELPATH)
        if not os.path.isfile(launcher_candidate):
            print("[FAIL] repository-owned Claude-GPT launcher not found in the worktree", file=sys.stderr)
            return EXIT_FAIL
        args.claude_bin = launcher_candidate

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
        # (inside ``preflight_claude_available``) and thread that same
        # absolute path through version capture and structured-lane
        # execution below, instead of independently re-resolving "claude"
        # by name in each place. Structured-lane flag capability itself is
        # still decided from the actual fixed-argv invocation result
        # (classify_claude_structured_outcome), never from ``claude --help``
        # text (AC1/AC5). Issue #2161: the native Codex CLI
        # ``preflight_codex_flags`` branch was removed along with the
        # ``codex`` runtime lane.
        resolved_runtime_bin, skip_reason = preflight_claude_available(args.claude_bin)
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
    # Issue #2219 AC1: sha256 of the resolved executable's FILE CONTENT,
    # binding evidence to the exact binary bytes actually invoked.
    resolved_executable_sha256 = (
        extract_claude_resolved_executable_sha256(resolved_runtime_bin)
        if resolved_runtime_bin
        else None
    )
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
        # Issue #2568 In Scope: raw-observation-only record of whether the
        # caller opted into the additive Task Context env/carrier
        # passthrough (--task-context-scope / --task-context-state-root).
        # No Task Context semantic verdict is derived or embedded here --
        # this is purely "was the carrier configured for this run", never
        # "did Task Context behave correctly" (that lives in
        # scripts/task-context/task_context_runtime_smoke_verifier.py).
        "task_context_carrier_configured": bool(
            args.task_context_scope or args.task_context_state_root
        ),
        "tested_head": tested_head,
        "runtime_version": runtime_version,
        "resolved_executable": resolved_runtime_bin,
        "resolved_executable_sha256": resolved_executable_sha256,
        "requested_agent_type": requested_agent_type,
        "effective_agent_type": effective_agent_type,
        "loaded_skills": loaded_skills,
        "loaded_skills_source": "static_frontmatter" if loaded_skills is not None else None,
        "prompt_sha256": prompt_sha256,
        "agent_definition": agent_definition,
        # main_agent_identity / skill_evidence / mutation_boundary /
        # permission_denials are placeholders here (no stdout captured
        # yet); overwritten below with real evidence once the structured
        # claude invocation completes.
        "main_agent_identity": build_main_agent_identity(args.claude_agent_name, None),
        "skill_evidence": build_skill_evidence(args.claude_agent_name, worktree, None),
        "mutation_boundary": build_mutation_boundary(hermetic_active, hermetic_settings_digest, None, None),
        # Issue #1881 PR #2385 fix_delta (Extension 3): purely additive --
        # never gated behind --hermetic-agent-definition, never touches
        # mutation_boundary.
        "permission_denials": extract_claude_permission_denials(None),
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
        # Issue #2437: these names deliberately distinguish the two settings
        # facts from runtime observations. None means the lane did not expose
        # a trustworthy observation channel; it is never promoted from a
        # model self-report or an absence of unrelated output.
        "peer_policy_configured": args.runtime == "claude",
        "cross_session_inbound_configured_refuse": args.runtime == "claude",
        "outbound_peer_tools_absent": None,
        "agent_spawn_completion_observed": None,
        "herdr_namespace_isolated": None,
        "preexisting_herdr_preserved": None,
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
            "override (a `claude`-named forwarder script -- never a "
            "symlink, see PR #2176 OWNER REQUEST_CHANGES Finding 2 -- "
            "that execs the --claude-bin absolute path) was prepended "
            "to the isolated herdr session's PATH and passed explicitly "
            "via `herdr workspace create --env PATH=...` and a "
            "post-creation `herdr pane run` PATH re-export (Finding 1), "
            "so herdr's own PATH lookup resolves to this explicit "
            "binary; a run-scoped nonce/receipt (see "
            "claude_bin_launcher_receipt_verified) independently confirms "
            "the forwarder itself executed (Issue #2174)."
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
                # Issue #2663 AC3: the "before" snapshot of the bounded
                # side-effect target directory MUST be captured before the
                # subprocess starts (never fabricated/backfilled), and is
                # independent of --require-clean-postcondition's own
                # before_fp (a different, whole-repo-fingerprint gate).
                if args.require_hook_chain_evidence:
                    hook_chain_manifests_before, hook_chain_snapshot_before_truncated = (
                        _snapshot_session_manifest_files(worktree)
                    )
                else:
                    hook_chain_manifests_before, hook_chain_snapshot_before_truncated = None, False
                rc, out, err, timed_out = run_structured_claude(
                    worktree, prompt, float(args.timeout_seconds), args.max_turns,
                    claude_bin=resolved_runtime_bin,
                    claude_agent_name=effective_claude_agent_name,
                    hermetic_agents_file=hermetic_agents_file if hermetic_active else None,
                    hermetic_settings_file=hermetic_settings_file if hermetic_active else None,
                    claude_adapter=args.claude_adapter,
                    include_user_prompt_expansion_hook=bool(args.expect_skill_command),
                    include_hook_chain_evidence_hooks=bool(args.require_hook_chain_evidence),
                    task_context_scope=args.task_context_scope,
                    task_context_state_root=args.task_context_state_root,
                    approval_settings_json=approval_overlay_json,
                    approval_child_env=approval_child_env,
                    named_subagent_resume=bool(args.named_subagent_resume),
                    append_system_prompt_file=args.append_system_prompt_file,
                )
                capability_decision, capability_reason = classify_claude_structured_outcome(
                    rc, out, err, timed_out
                )
                # Issue #2174 AC8: this runner's own view of the launcher's
                # own structured refusal receipt (e.g. a forbidden
                # --settings flag forwarded via a hermetic combination),
                # never a synthesized/guessed classification.
                schema_summary["claude_adapter"] = args.claude_adapter
                if approval_verified is not None:
                    # Issue #2839: run 後に fixture の内容 hash を再計算し、変わっていたら PASS
                    # にしない (子 session が承認済み fixture を書き換えた場合の検出)。
                    fixture_hash_after_run = approval_mod.fixture_content_blob_hash(
                        str(worktree), approval_verified["fixture_relpath"]
                    )
                    schema_summary["approval_carrier"] = approval_mod.build_approval_carrier_evidence(
                        approval_verified,
                        overlay_json=approval_overlay_json,
                        fixture_hash_after_run=fixture_hash_after_run,
                    )
                    if not schema_summary["approval_carrier"]["fixture_unchanged"]:
                        errors.append(
                            "approval carrier: fixture installer content changed during the run"
                        )
                        exit_code = EXIT_FAIL
                schema_summary["claude_gpt_launcher_receipt"] = (
                    extract_claude_gpt_launcher_receipt(err)
                    if args.claude_adapter == "claude-gpt"
                    else None
                )
                # Issue #2219 AC1/AC7: parse the launcher's own proxy
                # stderr side-channel and INDEPENDENTLY re-confirm cleanup
                # -- never trusting the launcher's own
                # CLAUDE_GPT_PROXY_CLEANUP_OK self-report.
                if args.claude_adapter == "claude-gpt":
                    proxy_sidechannel = extract_claude_gpt_proxy_sidechannel(err)
                    schema_summary["claude_gpt_proxy_sidechannel"] = proxy_sidechannel
                    proxy_cleanup_independent = verify_claude_gpt_proxy_cleanup_independent(
                        proxy_sidechannel["proxy_pid"], proxy_sidechannel["proxy_port"]
                    )
                    schema_summary["claude_gpt_proxy_cleanup_independent"] = proxy_cleanup_independent
                    if (
                        proxy_cleanup_independent["checked"]
                        and not proxy_cleanup_independent["cleanup_confirmed"]
                    ):
                        errors.append(
                            "claude-gpt proxy cleanup not independently confirmed "
                            f"(self-reported={proxy_sidechannel['proxy_cleanup_ok_self_reported']}): "
                            f"{proxy_cleanup_independent}"
                        )
                        exit_code = EXIT_FAIL
                else:
                    schema_summary["claude_gpt_proxy_sidechannel"] = None
                    schema_summary["claude_gpt_proxy_cleanup_independent"] = None
                # Issue #2046: real evidence now that stdout is captured.
                schema_summary["main_agent_identity"] = build_main_agent_identity(args.claude_agent_name, out)
                schema_summary["skill_evidence"] = build_skill_evidence(args.claude_agent_name, worktree, out)
                schema_summary["mutation_boundary"] = build_mutation_boundary(
                    hermetic_active, hermetic_settings_digest, claude_invocation_argv_for_evidence, out
                )
                # Issue #1881 PR #2385 fix_delta (Extension 3): purely
                # additive field, independent of mutation_boundary/hermetic
                # gating above.
                schema_summary["permission_denials"] = extract_claude_permission_denials(out)
                denied_peer_tools = {
                    str(denial.get("tool_name"))
                    for denial in schema_summary["permission_denials"]
                    if isinstance(denial, dict)
                }
                if {"SendMessage", "ListAgents"}.issubset(denied_peer_tools):
                    schema_summary["outbound_peer_tools_absent"] = True
            # Issue #2161 (native Codex CLI retirement): the
            # run_structured_codex()-based else branch was removed along
            # with the ``codex`` runtime lane.

            event_count = parse_native_event_count(out)
            schema_summary["process_exit_code"] = rc
            schema_summary["timed_out"] = timed_out
            schema_summary["native_event_count"] = event_count
            terminal_event_observed = has_terminal_event(args.runtime, out) if event_count > 0 else None
            schema_summary["terminal_event_observed"] = terminal_event_observed
            schema_summary["capability_decision"] = capability_decision
            schema_summary["capability_error_classification"] = capability_reason

            # Issue #2161: the classify_codex_events() else branch was
            # removed along with the ``codex`` runtime lane.
            spawn_events, self_restart_count, orchestration_count = classify_claude_events(out)
            schema_summary["spawn_events"] = spawn_events
            schema_summary["child_spawn_event_count"] = len(spawn_events)
            schema_summary["self_restart_event_count"] = self_restart_count
            schema_summary["orchestration_action_count"] = orchestration_count
            schema_summary["direct_web_tool_event_count"] = count_direct_web_tool_events(
                args.runtime, out
            )

            # Issue #2219 AC2/AC3/AC6: opt-in multi-SubAgent lifecycle /
            # same-main-session-across-turns / forbidden-marker checks.
            # Each is gated on its own opt-in flag so every pre-existing
            # caller's evidence/exit-code behavior is unchanged.
            if args.runtime == "claude":
                if args.require_min_subagents > 0:
                    multi_child_lifecycle = classify_claude_multi_child_lifecycle(
                        out, args.require_min_subagents
                    )
                    schema_summary["multi_child_lifecycle"] = multi_child_lifecycle
                    if not multi_child_lifecycle["verified"]:
                        errors.append(
                            "multi-SubAgent lifecycle not verified: "
                            f"{multi_child_lifecycle}"
                        )
                        exit_code = EXIT_FAIL
                if args.require_min_turns > 0:
                    same_session_turns = verify_same_main_session_across_turns(
                        out, args.require_min_turns
                    )
                    schema_summary["same_session_across_turns"] = same_session_turns
                    if not same_session_turns["verified"]:
                        errors.append(
                            "same main session across >= "
                            f"{args.require_min_turns} turns not verified: {same_session_turns}"
                        )
                        exit_code = EXIT_FAIL
                if args.scan_forbidden_markers:
                    forbidden_marker_scan = verify_no_forbidden_marker(out, err)
                    schema_summary["forbidden_marker_scan"] = forbidden_marker_scan
                    if not forbidden_marker_scan["verified"]:
                        errors.append(
                            "forbidden failure marker(s) observed: "
                            f"{forbidden_marker_scan['matched_markers']}"
                        )
                        exit_code = EXIT_FAIL
                if args.require_hook_chain_evidence:
                    # Issue #2663 AC3: the "after" snapshot is read only now
                    # -- after the whole subprocess (and thus every
                    # synchronous Stop hook) has already exited above.
                    hook_chain_manifests_after, hook_chain_snapshot_after_truncated = (
                        _snapshot_session_manifest_files(worktree)
                    )
                    hook_chain_evidence = evaluate_hook_chain_evidence(
                        out, worktree, hook_chain_manifests_before, hook_chain_manifests_after,
                        snapshot_truncated=(
                            hook_chain_snapshot_before_truncated
                            or hook_chain_snapshot_after_truncated
                        ),
                    )
                    schema_summary["hook_chain_evidence"] = hook_chain_evidence
                    if not hook_chain_evidence["passed"]:
                        errors.append(
                            "hook-chain evidence not verified: "
                            f"all_matching_hooks_observed.status="
                            f"{hook_chain_evidence['all_matching_hooks_observed']['status']!r}, "
                            f"sibling_side_effect_inventory_complete.status="
                            f"{hook_chain_evidence['sibling_side_effect_inventory_complete']['status']!r}"
                        )
                        exit_code = EXIT_FAIL

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
            # Issue #2161: the native Codex CLI identity-evidence branch
            # (rollout-log `session_meta.agent_role`) was removed along with
            # the ``codex`` runtime lane.
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
            # Issue #2161: the native Codex CLI else branch (rollout-log
            # based parent/child session id and agent-role extraction) was
            # removed along with the ``codex`` runtime lane.
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
            # Issue #2161: the native Codex CLI else branch (rollout-log
            # spawn/completion approximation) was removed along with the
            # ``codex`` runtime lane.
            schema_summary["child_spawn_observed"] = child_spawn_observed
            schema_summary["child_spawn_source"] = child_spawn_source
            schema_summary["child_launch_mode"] = child_spawn_launch_mode
            schema_summary["child_completion_observed"] = child_completion_observed
            schema_summary["child_completion_source"] = child_completion_source
            schema_summary["child_terminal_status"] = child_terminal_status
            schema_summary["child_agent_id"] = child_agent_id
            schema_summary["spawn_elapsed_sec"] = spawn_elapsed_sec
            schema_summary["completion_elapsed_sec"] = completion_elapsed_sec
            schema_summary["agent_spawn_completion_observed"] = bool(
                child_spawn_observed and child_completion_observed
            )

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

            if args.named_subagent_resume:
                # Issue #2840: generic causal-chain correlation + failure-layer
                # classification over the already-captured stream.  PASS needs the
                # whole chain; an unobservable chain is SKIP (77); everything else
                # is FAIL.  ``unclassified`` is a FAIL layer, never a PASS.
                named_resume_evidence = build_named_subagent_resume_evidence(
                    stdout=out, stderr=err, process_exit_code=rc, timed_out=timed_out,
                    adapter=args.claude_adapter, tested_head=tested_head,
                    runtime_version=runtime_version, resolved_runtime_bin=resolved_runtime_bin,
                    worktree=worktree, prompt_fixture_path=args.prompt_file,
                    compat_fixture_path=(
                        args.append_system_prompt_file
                        or os.path.join(worktree, NAMED_SUBAGENT_RESUME_COMPAT_FIXTURE_RELPATH)
                    ),
                    compat_note_applied=bool(args.append_system_prompt_file),
                    invocation_flags=named_resume_invocation_flag_readback(
                        args.claude_adapter, bool(args.append_system_prompt_file)
                    ),
                    launcher_receipt=schema_summary.get("claude_gpt_launcher_receipt"),
                )
                # The evidence's run-level verdict is NOT adjusted here: later assertions
                # may still change ``exit_code``.  It is finalised once, from the final
                # exit code, where ``schema_summary["exit_code"]`` is set.
                schema_summary["named_subagent_resume"] = named_resume_evidence
                if exit_code == EXIT_OK:
                    if named_resume_evidence["verdict"] == NAMED_RESUME_VERDICT_SKIP:
                        errors.append(
                            "named SubAgent resume causal evidence unobservable "
                            f"({named_resume_evidence['chain_break']}); never promoted to PASS"
                        )
                        exit_code = EXIT_SKIP
                    elif named_resume_evidence["verdict"] != NAMED_RESUME_VERDICT_PASS:
                        errors.append(
                            "named SubAgent resume chain not verified: "
                            f"chain_break={named_resume_evidence['chain_break']!r} "
                            f"failure_layer={named_resume_evidence['failure_layer']!r}"
                        )
                        exit_code = EXIT_FAIL

            # A marker is a success-only assertion.  If the bounded runtime
            # capability is unavailable (including timeout SKIP), no model
            # output is available to check and a missing marker must not
            # overwrite the authoritative exit-77 classification.
            if args.expect_marker and exit_code == EXIT_OK:
                # PR #2500 fix_delta P1-1 (OWNER REQUEST_CHANGES
                # https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805):
                # '--expect-marker-source main' evidence must come ONLY
                # from extract_claude_main_output_text() -- the model's own
                # assistant message text -- never the raw combined
                # stdout/stderr blob. That raw blob also contains the
                # UserPromptExpansion hook's own echoed stdin payload
                # (command_name/command_args/prompt, verbatim from the
                # CALLER's own prompt text), so a marker the caller merely
                # WROTE into the prompt could otherwise satisfy this check
                # even if the model's own output never produced it. The
                # pre-existing 'subagent' (default/omitted) source keeps
                # searching the full combined stdout+stderr blob, byte-
                # identical to every pre-#2498 caller -- this restriction
                # applies ONLY to the 'main' source path.
                if args.expect_marker_source == "main":
                    marker_search_text = extract_claude_main_output_text(out)
                else:
                    # Issue #2923: raw stdout/stderr (byte-identical to the
                    # pre-#2923 search text) PLUS the JSON-unescaped stream
                    # string values, so a marker containing a double quote is
                    # observed in the same representation
                    # ``_marker_provenance_verified`` uses.  Unquoted markers
                    # are unaffected (a superset only ever adds matches that
                    # the raw text could not express).
                    marker_search_text = (
                        out + "\n" + err + "\n" + extract_claude_stream_decoded_text(out)
                    )
                missing = [m for m in args.expect_marker if m not in marker_search_text]
                schema_summary["expected_markers_missing"] = missing
                if missing:
                    errors.append(f"expected markers not observed: {missing}")
                    exit_code = EXIT_FAIL

            # Issue #2498 AC1: --expect-ordered-marker is independent of
            # --expect-marker -- it never participates in the SubAgent
            # causal-evidence default gate below, and is recorded/evaluated
            # unconditionally (subject only to the same exit_code == EXIT_OK
            # success-only-assertion guard as --expect-marker above).
            #
            # PR #2500 fix_delta P1-2: matched against
            # extract_claude_main_output_text(out) -- the assistant-only
            # channel -- rather than the raw combined stdout blob. The raw
            # blob also contains the terminal 'result' event's own replay
            # of that SAME final assistant text (see
            # extract_claude_main_output_text's docstring); scanning the
            # raw blob let a single reversed-order answer masquerade as
            # forward-order evidence by finding an early marker in the
            # assistant event's own text and a later marker only in the
            # result event's duplicate replay of that identical text. Using
            # the assistant-only channel here means each final answer's
            # text is present exactly once, so it can never be
            # double-counted as two independent pieces of ordered evidence.
            if args.expect_ordered_marker:
                ordered_evidence_match = evaluate_ordered_evidence_match(
                    extract_claude_main_output_text(out), args.expect_ordered_marker
                )
                schema_summary["ordered_evidence_match"] = ordered_evidence_match
                if exit_code == EXIT_OK and not ordered_evidence_match["verified"]:
                    errors.append(
                        f"ordered evidence match failed: {ordered_evidence_match}"
                    )
                    exit_code = EXIT_FAIL

            # Issue #2498 AC2: --output-schema-path is independent of both
            # --expect-marker and --expect-ordered-marker.
            if args.output_schema_path:
                output_contract_schema_validation = evaluate_output_contract_schema_fields_present(
                    out, args.output_schema_path
                )
                schema_summary["output_contract_schema_validation"] = output_contract_schema_validation
                if exit_code == EXIT_OK and not output_contract_schema_validation["verified"]:
                    errors.append(
                        "output contract schema validation failed: "
                        f"{output_contract_schema_validation.get('error')}"
                    )
                    exit_code = EXIT_FAIL

            # Issue #2498 AC3/AC4: recorded unconditionally, same spirit as
            # subagent_causal_evidence below -- a future reader can always
            # see which provenance mode this run asserted.
            schema_summary["expect_marker_source"] = args.expect_marker_source
            if args.expect_skill_command:
                observed_skill_commands = extract_claude_user_prompt_expansion_command_names(out)
                schema_summary["user_prompt_expansion_command_names"] = observed_skill_commands
                skill_command_observed = args.expect_skill_command in observed_skill_commands
                schema_summary["expect_skill_command_observed"] = skill_command_observed
                if exit_code == EXIT_OK and not skill_command_observed:
                    errors.append(
                        "expected direct skill invocation not observed via "
                        f"UserPromptExpansion.command_name: {args.expect_skill_command!r} "
                        f"(observed={observed_skill_commands!r})"
                    )
                    exit_code = EXIT_FAIL

            # Issue #2183: structural, hook-ID-correlated causal evidence,
            # recorded unconditionally (never gated on any flag) so a
            # future reader can always see WHY a marker-only PASS was or
            # was not trusted. Claude's structured lane has the
            # SubagentStart/SubagentStop stream-json channel this function
            # parses (`out` above). Issue #2161: the native Codex CLI else
            # branch was removed along with the ``codex`` runtime lane.
            causal_evidence = subagent_causal_evidence_verdict(out, args.expect_marker)
            schema_summary["subagent_causal_evidence"] = causal_evidence
            # PR #2220 review fix-delta: the structured lane already has the
            # SubagentStart/SubagentStop stream-json channel available on
            # every invocation, so a marker-only PASS in this lane is
            # exactly the "narrow scope without amendment" gap Issue #2183
            # set out to close -- it must not remain opt-in-only. Whenever
            # the caller supplies ``--expect-marker`` (i.e. the run is
            # actually being used to assert a SubAgent produced some
            # observable output), the structured lane now REQUIRES
            # causal_evidence_source == hook_id_correlated by default, with
            # no flag needed. ``--require-subagent-causal-evidence`` still
            # exists to opt in to the same requirement for structured runs
            # that don't use ``--expect-marker`` at all.
            #
            # PR #2220 OWNER REQUEST_CHANGES P0-1
            # (https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514):
            # the prior expression (``args.require_subagent_causal_evidence
            # or bool(args.expect_marker)``) was NOT scoped to
            # ``args.runtime == "claude"`` even though ``causal_evidence``
            # (computed above) is unconditionally ``None`` for every other
            # runtime -- a Codex ``--mode structured --expect-marker``
            # caller whose fake/real process exited 0 and DID print the
            # expected marker text was still forced to FAIL purely because
            # this harness has no hook-lifecycle channel for Codex, not
            # because anything about the run was actually wrong. The
            # requirement is now explicitly scoped to the one runtime that
            # structurally has a causal-evidence channel at all (any mode);
            # ``main()`` rejects (at argparse time, via ``parser.error``)
            # any attempt to combine ``--require-subagent-causal-evidence``
            # with a runtime other than ``claude``, so by the time this
            # line runs ``args.require_subagent_causal_evidence`` being
            # ``True`` already implies ``args.runtime == "claude"``.
            #
            # Issue #2498 AC3: ``--expect-marker-source`` is an ADDITIVE
            # provenance input scoped ONLY to the implicit "--expect-marker
            # was given" branch of this gate -- an explicit
            # ``--require-subagent-causal-evidence`` ask is an independent,
            # unconditional requirement and is never opted out of by
            # ``--expect-marker-source main``. The default value
            # (``"subagent"``) makes ``args.expect_marker_source != "main"``
            # always ``True``, so this expression is byte-identical to the
            # pre-#2498 expression for every caller that omits the new flag.
            causal_evidence_required = args.require_subagent_causal_evidence or (
                args.runtime == "claude"
                and bool(args.expect_marker)
                and args.expect_marker_source != "main"
            )
            if (
                causal_evidence_required
                and exit_code == EXIT_OK
                and (
                    causal_evidence is None
                    or causal_evidence["causal_evidence_source"] != CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
                )
            ):
                observed_source = causal_evidence["causal_evidence_source"] if causal_evidence else None
                trigger = (
                    "--require-subagent-causal-evidence"
                    if args.require_subagent_causal_evidence
                    else "--expect-marker default gate (Issue #2183 fix-delta)"
                )
                errors.append(
                    f"subagent causal evidence insufficient ({trigger}): "
                    f"causal_evidence_source={observed_source!r}"
                )
                exit_code = EXIT_FAIL

            required_observations = sorted(set(args.require_observed_runtime_field))
            if required_observations:
                # Issue #2854: only ``permission_mode`` has a native extractor
                # (SubagentStop ``system/hook_response`` payload). The other
                # fields have no independently extractable event in this
                # runner; record that precise capability gap. Declarations or
                # local re-reads must never fill it in, and ``permission_mode``
                # is never an alias for ``effective_permission_profile``.
                observed_runtime_fields: dict[str, dict] = {}
                unavailable_reasons: dict[str, str] = {}
                for field in required_observations:
                    if field != _PERMISSION_MODE_FIELD:
                        unavailable_reasons[field] = _OBSERVATION_REASON_NO_NATIVE_EXTRACTOR
                        continue
                    if args.runtime != "claude":
                        unavailable_reasons[field] = _OBSERVATION_REASON_NO_SUBAGENTSTOP_EVENT
                        continue
                    extracted = extract_claude_subagentstop_permission_mode(out)
                    if extracted["observed"]:
                        observed_runtime_fields[field] = {
                            "value": extracted["value"],
                            "source_event": "system/hook_response",
                            "source_hook_event": "SubagentStop",
                            "source_field": _PERMISSION_MODE_FIELD,
                        }
                    else:
                        unavailable_reasons[field] = extracted["reason"]
                unavailable = sorted(unavailable_reasons)
                schema_summary["required_runtime_observations"] = required_observations
                schema_summary["unavailable_required_runtime_observations"] = unavailable
                schema_summary["observed_runtime_fields"] = observed_runtime_fields
                schema_summary["unavailable_required_runtime_observation_reasons"] = unavailable_reasons
                if unavailable and exit_code == EXIT_OK:
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
                "turns_completed": 0,
                "cleanup": {
                    "attempted": False,
                    "session_started": False,
                    "stop_rc": None,
                    "delete_rc": None,
                    "confirmed_removed": False,
                },
            }
            pane_output_lines: list[str] = []

            # Pre-existing-session observation is an explicit opt-in only.
            # The default interactive lane must not enumerate, list, or
            # snapshot any human/pre-existing Herdr namespace.
            session_baseline_before: list[dict] | None = None
            workspace_snapshot_before: dict[str, dict] | None = None
            if args.require_session_baseline_preservation:
                session_baseline_before = snapshot_herdr_sessions("herdr", _isolated_env())
                schema_summary["session_baseline_before_captured"] = session_baseline_before is not None
                if session_baseline_before is None:
                    errors.append(
                        "could not capture herdr session baseline before interactive lane "
                        "(session list failed)"
                    )
                    exit_code = EXIT_FAIL

                workspace_snapshot_before = capture_all_herdr_workspace_snapshots("herdr", _isolated_env())
                schema_summary["herdr_workspace_snapshot_before_captured"] = workspace_snapshot_before is not None
                if workspace_snapshot_before is None:
                    errors.append(
                        "could not capture herdr workspace/agent/focus snapshot before interactive "
                        "lane (api snapshot failed)"
                    )
                    exit_code = EXIT_FAIL

            # Issue #2219 fix_delta iteration 1 (Option B): captured BEFORE
            # the isolated session is created, so the persisted-transcript
            # lookup below (_find_claude_interactive_transcript) has an
            # honest lower bound on mtime and can never match a stale
            # transcript file left over from an earlier, unrelated run
            # against the same worktree.
            interactive_run_started_epoch = time.time()
            try:
                _hook_sink_enabled = args.runtime == "claude" and (
                    args.require_min_subagents > 0 or args.require_min_turns > 0
                )
                pane_output_lines = run_interactive_herdr_isolated(
                    args.runtime, worktree, prompt, float(args.timeout_seconds), run_id, evidence,
                    claude_bin_override=args.claude_bin if args.runtime == "claude" else None,
                    claude_adapter=args.claude_adapter,
                    additional_prompts=args.additional_prompt or None,
                    hook_sink_enabled=_hook_sink_enabled,
                    task_context_scope=args.task_context_scope,
                    task_context_state_root=args.task_context_state_root,
                )

                if evidence.get("final_state") == "blocked":
                    errors.append("agent reached blocked state; evidence captured, not auto-approved")
                    exit_code = EXIT_FAIL

                combined_pane_text = "\n".join(pane_output_lines)

                if args.expect_marker:
                    missing = [m for m in args.expect_marker if m not in combined_pane_text]
                    schema_summary["expected_markers_missing"] = missing
                    if missing:
                        errors.append(f"expected markers not observed in pane output: {missing}")
                        exit_code = EXIT_FAIL

                # Issue #2219 (OWNER anchor decision, 2026-08-16): the
                # interactive lane's PASS authority for multi-turn/multi-
                # SubAgent lifecycle is the hook-event evidence channel
                # (hook sink records already parsed into
                # ``evidence["hook_sink_records"]`` inside
                # ``run_interactive_herdr_isolated``), NOT transcript
                # existence -- live investigation proved herdr-PTY-driven
                # claude-gpt sessions never write a flat
                # ``<session-id>.jsonl`` main transcript (see
                # ``_find_claude_interactive_transcript`` docstring for the
                # full incident trail). The transcript lookup below is kept
                # for ADVISORY/diagnostic breadcrumbs only (never promoted
                # to PASS authority) alongside the fragmentary
                # ``subagents/*.meta.json`` scan.
                if args.runtime == "claude" and (
                    args.require_min_subagents > 0
                    or args.require_min_turns > 0
                    or args.scan_forbidden_markers
                ):
                    transcript_path = _find_claude_interactive_transcript(
                        worktree, interactive_run_started_epoch, args.claude_adapter
                    )
                    schema_summary["interactive_transcript_found"] = transcript_path is not None
                    if transcript_path is None:
                        # Advisory-only breadcrumb, never a PASS-capable
                        # evidence source -- see
                        # _find_claude_interactive_subagent_only_session_dirs
                        # docstring.
                        schema_summary["interactive_transcript_subagent_only_session_dirs"] = (
                            _find_claude_interactive_subagent_only_session_dirs(
                                worktree, interactive_run_started_epoch, args.claude_adapter
                            )
                        )
                    transcript_text = ""
                    if transcript_path is not None:
                        try:
                            transcript_text = transcript_path.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            transcript_text = ""
                            schema_summary["interactive_transcript_found"] = False

                    hook_sink_records = evidence.get("hook_sink_records") or []
                    hook_sink_malformed = evidence.get("hook_sink_malformed_line_count", 0)
                    hook_sink_expected_nonce = evidence.get("hook_sink_nonce")
                    hook_sink_staleness = verify_claude_gpt_hook_sink_not_stale(
                        hook_sink_records, hook_sink_expected_nonce
                    )
                    schema_summary["hook_sink_records_count"] = len(hook_sink_records)
                    schema_summary["hook_sink_malformed_line_count"] = hook_sink_malformed
                    schema_summary["hook_sink_staleness"] = hook_sink_staleness
                    schema_summary["hook_sink_no_raw_content"] = verify_claude_gpt_hook_sink_no_raw_content(
                        hook_sink_records
                    )
                    if _hook_sink_enabled and not hook_sink_staleness["verified"]:
                        errors.append(
                            "hook-event evidence sink stale or unavailable (interactive lane): "
                            f"{hook_sink_staleness}"
                        )
                        exit_code = EXIT_FAIL

                    if args.require_min_subagents > 0:
                        multi_child_lifecycle = classify_claude_hook_sink_multi_child_lifecycle(
                            hook_sink_records, args.require_min_subagents
                        )
                        schema_summary["multi_child_lifecycle"] = multi_child_lifecycle
                        schema_summary["multi_child_lifecycle_source"] = "hook_event_sink"
                        if not multi_child_lifecycle["verified"]:
                            errors.append(
                                "multi-SubAgent lifecycle not verified (interactive lane, "
                                f"hook_event_sink): {multi_child_lifecycle}"
                            )
                            exit_code = EXIT_FAIL

                    if args.require_min_turns > 0:
                        same_session_turns = verify_claude_gpt_hook_sink_multi_turn(
                            hook_sink_records, args.require_min_turns
                        )
                        schema_summary["same_session_across_turns"] = same_session_turns
                        schema_summary["same_session_across_turns_source"] = "hook_event_sink"
                        if not same_session_turns["verified"]:
                            errors.append(
                                "same main session across >= "
                                f"{args.require_min_turns} turns not verified "
                                f"(interactive lane, hook_event_sink): {same_session_turns}"
                            )
                            exit_code = EXIT_FAIL

                    if args.scan_forbidden_markers:
                        forbidden_marker_scan = verify_no_forbidden_marker(
                            transcript_text, "", combined_pane_text
                        )
                        schema_summary["forbidden_marker_scan"] = forbidden_marker_scan
                        if not forbidden_marker_scan["verified"]:
                            errors.append(
                                "forbidden failure marker(s) observed (interactive lane): "
                                f"{forbidden_marker_scan['matched_markers']}"
                            )
                            exit_code = EXIT_FAIL

                # Issue #2183: same structural causal-evidence verdict as
                # the structured lane, applied here to the captured pane
                # text. Recorded unconditionally; only gates exit_code when
                # --require-subagent-causal-evidence is set (unlike the
                # structured lane, this stays genuinely opt-in here even
                # when --expect-marker is given -- see PR #2220 review
                # fix-delta). Honest limitation (documented, not silently
                # worked around): the interactive lane's herdr pane is a
                # terminal render of Claude Code's own interactive UI,
                # which does not echo the `--include-hook-events`
                # stream-json hook payloads this function parses -- so
                # causal_evidence_source is expected to be
                # no_evidence/marker_only_insufficient here today, and
                # defaulting this gate to on would make every
                # --expect-marker interactive-lane caller FAIL
                # unconditionally, not just detect a real regression.
                # Wiring an interactive-lane hook-output channel equivalent
                # to the structured lane's is future work, not this Issue's
                # scope (no new hook settings/matcher configuration here).
                # Issue #2161: the native Codex CLI else branch was removed
                # along with the ``codex`` runtime lane.
                causal_evidence = subagent_causal_evidence_verdict(combined_pane_text, args.expect_marker)
                schema_summary["subagent_causal_evidence"] = causal_evidence
                if (
                    args.require_subagent_causal_evidence
                    and exit_code == EXIT_OK
                    and (
                        causal_evidence is None
                        or causal_evidence["causal_evidence_source"] != CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
                    )
                ):
                    observed_source = causal_evidence["causal_evidence_source"] if causal_evidence else None
                    errors.append(
                        "subagent causal evidence insufficient (--require-subagent-causal-evidence): "
                        f"causal_evidence_source={observed_source!r}"
                    )
                    exit_code = EXIT_FAIL

                if args.require_session_log_metadata or args.inspect_session_log_metadata:
                    schema_summary["session_log_metadata_count"] = 0
                    if args.require_session_log_metadata:
                        errors.append("session-log metadata required but unavailable in interactive lane")
                        exit_code = EXIT_SKIP if exit_code == EXIT_OK else exit_code
            except HerdrLaneError as exc:
                errors.append(exc.message)
                exit_code = EXIT_SKIP if exc.skip else EXIT_FAIL
                if exc.skip:
                    # A nested-session refusal is an explicit environment
                    # constraint, not evidence that an existing namespace was
                    # mutated. Keep its bounded classification through the
                    # own-session cleanup path below.
                    schema_summary["runtime_skip_reason_code"] = "herdr_isolated_session_unavailable"
            finally:
                if args.require_session_baseline_preservation:
                    # The explicit opt-in preserves the existing fail-closed
                    # before/after baseline and full workspace observation.
                    if session_baseline_before is not None:
                        session_baseline_after = snapshot_herdr_sessions("herdr", _isolated_env())
                        schema_summary["session_baseline_after_captured"] = session_baseline_after is not None
                        if session_baseline_after is None:
                            errors.append(
                                "could not capture herdr session baseline after interactive lane "
                                "(session list failed)"
                            )
                            exit_code = EXIT_FAIL
                        else:
                            created_name = evidence.get("session_name")
                            baseline_diffs = diff_herdr_session_baseline(
                                session_baseline_before, session_baseline_after,
                                new_session_names={created_name} if created_name else set(),
                            )
                            schema_summary["session_baseline_diffs"] = baseline_diffs
                            if baseline_diffs:
                                errors.append(
                                    f"herdr session baseline preservation failed: {baseline_diffs}"
                                )
                                exit_code = EXIT_FAIL

                    workspace_snapshot_after = capture_all_herdr_workspace_snapshots("herdr", _isolated_env())
                    schema_summary["herdr_workspace_snapshot_after_captured"] = workspace_snapshot_after is not None
                    workspace_snapshot_diffs = diff_all_herdr_workspace_snapshots(
                        workspace_snapshot_before, workspace_snapshot_after
                    )
                    schema_summary["herdr_workspace_snapshot_diffs"] = workspace_snapshot_diffs
                    schema_summary["herdr_workspace_snapshot_preserved"] = (
                        None if exit_code == EXIT_SKIP else not workspace_snapshot_diffs
                    )
                    if workspace_snapshot_diffs and exit_code != EXIT_SKIP:
                        errors.append(
                            "herdr workspace/agent/focus snapshot not preserved across isolated "
                            f"interactive lane run: {workspace_snapshot_diffs}"
                        )
                        exit_code = EXIT_FAIL

            schema_summary["session_name"] = evidence.get("session_name")
            schema_summary["pane_id"] = evidence.get("pane_id")
            schema_summary["agent_name"] = evidence.get("agent_name")
            schema_summary["final_state"] = evidence.get("final_state")
            schema_summary["detected_agent"] = evidence.get("detected_agent")
            schema_summary["detected_agent_confidence"] = evidence.get("detected_agent_confidence")
            schema_summary["prompt_stall_recovered"] = evidence.get("prompt_stall_recovered")
            schema_summary["turns_completed"] = evidence.get("turns_completed")

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
            schema_summary["herdr_namespace_isolated"] = (
                None if exit_code == EXIT_SKIP else bool(
                    evidence.get("session_name") and cleanup.get("confirmed_removed")
                )
            )
            # This existing observed evidence key/type is available only for
            # the explicit baseline-preservation opt-in; default runs leave it
            # honestly unavailable rather than inferring preservation.
            schema_summary["preexisting_herdr_preserved"] = (
                schema_summary.get("herdr_workspace_snapshot_preserved")
                if args.require_session_baseline_preservation
                else None
            )
            # A failed own-session cleanup remains a hard failure once a
            # session process was successfully started.  A nested-session
            # refusal can occur before that point; its named session never
            # existed, so an unconfirmable no-op cleanup must preserve the
            # already-classified explicit SKIP rather than convert it to FAIL.
            if (
                cleanup.get("session_started")
                and cleanup.get("attempted")
                and not cleanup.get("confirmed_removed")
            ):
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
    if isinstance(schema_summary.get("named_subagent_resume"), dict):
        # Issue #2840 (PR #2879 review): run-level verdict / failure_layer / runner_exit_code
        # are fixed once, here, from the final exit code.
        schema_summary["named_subagent_resume"] = finalize_named_resume_evidence(
            schema_summary["named_subagent_resume"], exit_code
        )

    write_evidence(output_dir, schema_summary=schema_summary)

    for error in errors:
        print(f"[FAIL] {error}" if exit_code == EXIT_FAIL else f"SKIP: {error}", file=sys.stderr)

    if exit_code == EXIT_OK:
        print(f"OK: runtime smoke evidence written to {output_dir}")

    # Issue #2231: optional machine-generated JSON dump of the full
    # schema_summary, written last (after every field -- including
    # errors/exit_code -- is finalized above) so callers never observe a
    # partially-populated evidence file. Best-effort: a write failure
    # here (e.g. --evidence-json parent directory missing) is reported
    # to stderr but never changes exit_code -- the JSON evidence option
    # is additive to the existing summary.md contract, not a new gate.
    if args.evidence_json:
        try:
            Path(args.evidence_json).write_text(
                json.dumps(_redact_evidence_value(schema_summary), indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[WARN] could not write --evidence-json to {args.evidence_json}: {exc}", file=sys.stderr)

    # Issue #2840: dedicated public-safe evidence (ids / hashes / versions /
    # booleans only).  Already finalised from the final exit code above.
    if args.named_resume_evidence_json:
        named_resume_payload = schema_summary.get("named_subagent_resume")
        if isinstance(named_resume_payload, dict):
            try:
                Path(args.named_resume_evidence_json).write_text(
                    json.dumps(_nr_assert_public_safe(named_resume_payload), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                print(f"[WARN] could not write --named-resume-evidence-json: {exc}", file=sys.stderr)
        else:
            print(
                "[WARN] --named-resume-evidence-json given but no named SubAgent resume evidence "
                "was produced (the runtime did not reach the structured run)",
                file=sys.stderr,
            )

    return exit_code



# --- Issue #2186 AC7: Spark sanitized failure classification -----------------
#
# Fixed, closed classification set for entitlement/catalog/parameter/quota/
# tool/context-continuation failures observed for the explicit-only
# spark-codex SubAgent. No caller of this function may promote any of these
# classifications to PASS or to an automatic fallback (ordinary model, Lite
# lane, or cached/catalog-only result) -- ``fallback_eligible`` and
# ``promotable_to_pass`` are unconditionally ``False`` for every branch.
SPARK_FAILURE_CLASSIFICATIONS = (
    "unsupported_entitlement",
    "unavailable_catalog",
    "request_parameter_incompatibility",
    "quota",
    "tool_incompatibility",
    "context_continuation_error",
    "other_safe_failure",
)


def classify_spark_failure(sanitized_reason: str) -> dict:
    """Map a sanitized (credential/token/raw-auth/raw-request-response/raw-
    transcript-free) failure reason string to one of the fixed
    ``SPARK_FAILURE_CLASSIFICATIONS`` categories. Always returns exit_code 77
    (SKIP/blocked) and never PASS; never marks the result fallback-eligible.
    """
    reason = (sanitized_reason or "").lower()
    if "entitlement" in reason or "not entitled" in reason or "forbidden" in reason:
        classification = "unsupported_entitlement"
    elif "catalog" in reason or "model not found" in reason or "unknown model" in reason:
        classification = "unavailable_catalog"
    elif "quota" in reason or "rate limit" in reason or "429" in reason:
        classification = "quota"
    elif "tool" in reason and ("unsupported" in reason or "incompatib" in reason):
        classification = "tool_incompatibility"
    elif "context" in reason or "continuation" in reason or "compaction" in reason:
        classification = "context_continuation_error"
    elif "parameter" in reason or "invalid_request" in reason or "unsupported parameter" in reason:
        classification = "request_parameter_incompatibility"
    else:
        classification = "other_safe_failure"
    return {
        "schema": "SPARK_FAILURE_CLASSIFICATION_RESULT_V1",
        "classification": classification,
        "exit_code": 77,
        "status": "skipped_blocked",
        "fallback_eligible": False,
        "promotable_to_pass": False,
        "redaction_confirmed": True,
    }


def extract_spark_gate_writer_source(launch_sh_text: str) -> str | None:
    """Retired (Issue #2651): the ``SPARK_GATE_WRITER_PY_BEGIN``/``_END``
    marker region this function used to extract from ``scripts/claude-gpt/
    launch.sh`` (Issue #2186's explicit-only Spark authorization gate
    source) has been removed from ``launch.sh`` entirely, along with the
    gate itself. This function is kept, name-compatible, only because its
    consumers (``test_run_worktree_agent_runtime_smoke_spark_explicit_gate.
    py`` and ``test_background_execution_foreground_invariant.py``'s
    fixture) now assert the negative/retired outcome -- it always returns
    ``None`` unconditionally, never re-parsing ``launch_sh_text`` for
    markers that can no longer exist."""
    del launch_sh_text  # retired: no marker region exists to extract any more
    return None


def extract_spark_prompt_retirement_hook_source(launch_sh_text: str) -> str | None:
    """Issue #2651 OWNER review fix_delta
    (https://github.com/squne121/loop-protocol/pull/2662#issuecomment-5736035898
    P1 blocker 2): extract the ``SPARK_PROMPT_RETIREMENT_PY_BEGIN``/``_END``
    marker region ``scripts/claude-gpt/launch.sh`` embeds as its
    always-registered ``UserPromptSubmit`` hook. This hook is the small,
    stateless replacement for the retired Spark authorization gate's own
    ``UserPromptSubmit`` entry: it rejects an ACTIVE legacy Spark execution
    request (an ``@agent-spark-codex`` mention, or a valid
    ``DELEGATION_REQUEST_V1`` directive naming ``spark-codex``/
    ``gpt-5.3-codex-spark``) before the model ever processes the prompt,
    without re-adding any pending-authorization state, model evidence, or
    ledger. Returns ``None`` if the marker region is not found (e.g. a
    ``launch_sh_text`` that predates this hook)."""
    begin_marker = "# SPARK_PROMPT_RETIREMENT_PY_BEGIN"
    end_marker = "# SPARK_PROMPT_RETIREMENT_PY_END"
    begin_index = launch_sh_text.find(begin_marker)
    if begin_index == -1:
        return None
    end_index = launch_sh_text.find(end_marker, begin_index)
    if end_index == -1:
        return None
    return launch_sh_text[begin_index : end_index + len(end_marker)]


if __name__ == "__main__":
    raise SystemExit(main())
