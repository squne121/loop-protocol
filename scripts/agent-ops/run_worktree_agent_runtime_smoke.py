#!/usr/bin/env python3
"""run_worktree_agent_runtime_smoke.py — cross-runtime worktree agent smoke runner (Issue #1887).

Launches Claude Code or Codex CLI inside an identity-verified, linked worktree
and observes either:

- ``structured`` lane: a non-interactive process (``claude -p`` /
  ``codex exec``) whose exit code and native structured stdout are the
  evidence, or
- ``interactive`` lane: a herdr sibling pane + agent lifecycle whose bounded,
  redacted pane output and native agent detection are the evidence.

This runner does not own semantic verdicts (hook-reason classification,
mutation-deny correctness, Skill preload domain judgement, context-budget
scoring, review verdicts, merge readiness). It only reports whether the
runtime started, ran in the requested worktree, reached a settled/terminal
state within the timeout, produced the requested evidence, and left the
worktree in the expected postcondition.

Exit codes:
  0  success
  1  runtime failure / timeout / identity mismatch / unexpected postcondition
  77 SKIP (unavailable runtime/auth/capability/herdr — never promoted to PASS)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

SCHEMA = "WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

_MAX_EVENT_LINES = 400
_MAX_PANE_LINES = 400
_MAX_LINE_CHARS = 2000
_MAX_SESSION_LOG_LINES = 200

# Absolute path / long-base64-token redaction (mirrors git_worktree_probe.py).
_SECRET_LIKE_RE = re.compile(
    r"(/(?:home|root|Users)/[^\s\"']+)|"
    r"([A-Za-z0-9+/]{40,}=*)"
)

_ALLOWLIST_SESSION_LOG_KEYS = {
    "type",
    "event",
    "role",
    "subagent",
    "label",
    "timestamp",
    "ts",
    "cwd",
    "session_id",
    "sessionId",
}


def _redact(text: str) -> str:
    return _SECRET_LIKE_RE.sub("<redacted>", text)


def _bounded_redacted_lines(raw: str, max_lines: int) -> list[str]:
    lines = raw.splitlines()[:max_lines]
    out = []
    for line in lines:
        line = line[:_MAX_LINE_CHARS]
        out.append(_redact(line))
    return out


def _run(argv: list[str], *, cwd: str | None = None, timeout: float,
          input_text: str | None = None) -> tuple[int | None, str, str, bool]:
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
# Postcondition
# ---------------------------------------------------------------------------


def _git_status_porcelain(path: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    rc, out, _err, _timed_out = _run([git, "-C", path, "status", "--porcelain"], timeout=10.0)
    if rc != 0:
        return None
    return out


def _filter_evidence_lines(porcelain: str, output_dir_rel: str | None) -> list[str]:
    lines = [line for line in porcelain.splitlines() if line.strip()]
    if not output_dir_rel:
        return lines
    filtered = []
    for line in lines:
        path_part = line[3:].strip()
        if path_part.startswith(output_dir_rel):
            continue
        filtered.append(line)
    return filtered


# ---------------------------------------------------------------------------
# Capability preflight
# ---------------------------------------------------------------------------


def preflight_runtime(runtime: str) -> str | None:
    exe = shutil.which(runtime)
    if exe is None:
        return f"required command not found: {runtime}"
    return None


def preflight_claude_flags() -> str | None:
    exe = shutil.which("claude")
    if exe is None:
        return "required command not found: claude"
    rc, out, err, timed_out = _run([exe, "--help"], timeout=20.0)
    if timed_out or rc != 0:
        return "unable to introspect claude --help capability"
    text = out + err
    required = ["--output-format", "--include-hook-events", "--no-session-persistence"]
    missing = [flag for flag in required if flag not in text]
    if missing:
        return f"claude CLI missing required structured-lane flags: {missing}"
    return None


def preflight_codex_flags() -> str | None:
    exe = shutil.which("codex")
    if exe is None:
        return "required command not found: codex"
    rc, out, err, timed_out = _run([exe, "exec", "--help"], timeout=20.0)
    if timed_out or rc != 0:
        return "unable to introspect codex exec --help capability"
    text = out + err
    required = ["--json", "--ephemeral", "-C"]
    missing = [flag for flag in required if flag not in text]
    if missing:
        return f"codex CLI missing required structured-lane flags: {missing}"
    return None


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
# Structured lane
# ---------------------------------------------------------------------------


def run_structured_claude(worktree: str, prompt: str, timeout_seconds: float,
                           claude_bin: str = "claude") -> tuple[int | None, str, str, bool]:
    argv = [
        claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--verbose",
    ]
    return _run(argv, cwd=worktree, timeout=timeout_seconds, input_text=prompt)


def run_structured_codex(worktree: str, prompt: str, timeout_seconds: float,
                          codex_bin: str = "codex") -> tuple[int | None, str, str, bool]:
    argv = [
        codex_bin,
        "exec",
        "-C", worktree,
        "--json",
        "--ephemeral",
        prompt,
    ]
    return _run(argv, cwd=worktree, timeout=timeout_seconds)


def parse_native_event_count(runtime: str, stdout: str) -> int:
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


# ---------------------------------------------------------------------------
# Interactive herdr lane
# ---------------------------------------------------------------------------


class HerdrLaneError(Exception):
    def __init__(self, message: str, *, skip: bool = False):
        super().__init__(message)
        self.message = message
        self.skip = skip


def run_interactive_herdr(
    runtime: str,
    worktree: str,
    prompt: str,
    timeout_seconds: float,
    run_id: str,
    *,
    keep_pane: bool,
    herdr_bin: str = "herdr",
) -> dict:
    """Drive a herdr sibling pane + agent lifecycle. Returns evidence dict."""
    agent_name = f"runtime-smoke-{runtime}-{run_id}"[:48]
    pane_id: str | None = None
    evidence: dict = {
        "pane_id": None,
        "agent_name": agent_name,
        "final_state": None,
        "pane_output_lines": [],
        "agent_explain": None,
        "cleaned_up": False,
    }
    try:
        rc, out, err, timed_out = _run(
            [herdr_bin, "pane", "split", "--current", "--direction", "right",
             "--cwd", worktree, "--no-focus"],
            timeout=20.0,
        )
        if timed_out or rc != 0:
            raise HerdrLaneError(f"herdr pane split failed: {_redact(err or out)}")
        try:
            payload = json.loads(out)
            pane_id = str(payload.get("pane_id") or payload.get("id") or "").strip()
        except (json.JSONDecodeError, ValueError):
            pane_id = out.strip().splitlines()[-1].strip() if out.strip() else None
        if not pane_id:
            raise HerdrLaneError("could not parse pane_id from herdr pane split output")
        evidence["pane_id"] = pane_id

        rc, out, err, timed_out = _run(
            [herdr_bin, "agent", "start", agent_name, "--kind", runtime,
             "--pane", pane_id, "--timeout", str(int(min(timeout_seconds, 300.0) * 1000))],
            timeout=timeout_seconds,
        )
        if timed_out or rc != 0:
            raise HerdrLaneError(f"herdr agent start failed: {_redact(err or out)}")

        rc, out, err, timed_out = _run(
            [herdr_bin, "agent", "prompt", agent_name, prompt, "--wait",
             "--timeout", str(int(timeout_seconds * 1000))],
            timeout=timeout_seconds + 20.0,
        )
        if timed_out:
            raise HerdrLaneError("herdr agent prompt timed out")
        if rc != 0:
            raise HerdrLaneError(f"herdr agent prompt failed: {_redact(err or out)}")

        rc, out, err, timed_out = _run(
            [herdr_bin, "agent", "get", agent_name], timeout=20.0
        )
        state = None
        if rc == 0:
            try:
                state = json.loads(out).get("state")
            except (json.JSONDecodeError, ValueError):
                state = out.strip()
        evidence["final_state"] = state
        if state in ("unknown", None):
            raise HerdrLaneError(f"agent lifecycle state is unusable for evidence: {state}")

        rc, out, _err, _timed_out = _run(
            [herdr_bin, "agent", "explain", agent_name, "--json"], timeout=20.0
        )
        if rc == 0:
            try:
                evidence["agent_explain"] = json.loads(out)
            except (json.JSONDecodeError, ValueError):
                evidence["agent_explain"] = {"raw": _redact(out[:2000])}

        rc, out, _err, _timed_out = _run(
            [herdr_bin, "agent", "read", agent_name, "--source", "recent-unwrapped",
             "--lines", str(_MAX_PANE_LINES)],
            timeout=20.0,
        )
        if rc == 0:
            evidence["pane_output_lines"] = _bounded_redacted_lines(out, _MAX_PANE_LINES)

        return evidence
    finally:
        if pane_id and not keep_pane:
            _run([herdr_bin, "pane", "close", pane_id], timeout=15.0)
            evidence["cleaned_up"] = True


# ---------------------------------------------------------------------------
# Evidence writing
# ---------------------------------------------------------------------------


def write_evidence(
    output_dir: Path,
    *,
    schema_summary: dict,
    native_events: list[str] | None,
    pane_output: list[str] | None,
    agent_detection: dict | None,
    session_log_metadata: list[dict] | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_lines = ["# Runtime Smoke Summary", ""]
    for key in sorted(schema_summary.keys()):
        summary_lines.append(f"- {key}: {schema_summary[key]}")
    (output_dir / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    if native_events is not None:
        with (output_dir / "native-events.jsonl").open("w", encoding="utf-8") as fh:
            for line in native_events:
                fh.write(line + "\n")

    if pane_output is not None:
        (output_dir / "pane-output.txt").write_text("\n".join(pane_output) + "\n", encoding="utf-8")

    if agent_detection is not None:
        (output_dir / "agent-detection.json").write_text(
            json.dumps(agent_detection, ensure_ascii=True, indent=2), encoding="utf-8"
        )

    if session_log_metadata is not None:
        lines = [json.dumps(entry, ensure_ascii=True) for entry in session_log_metadata]
        (output_dir / "session-log-metadata.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def extract_session_log_metadata(raw_lines: list[str]) -> list[dict]:
    """Extract allowlist-only metadata from native structured event lines."""
    out: list[dict] = []
    for line in raw_lines[:_MAX_SESSION_LOG_LINES]:
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        entry = {k: payload[k] for k in _ALLOWLIST_SESSION_LOG_KEYS if k in payload}
        if entry:
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="worktree-agent-runtime-smoke runner")
    parser.add_argument("--runtime", choices=["claude", "codex"], required=True)
    parser.add_argument("--mode", choices=["structured", "interactive"], required=True)
    parser.add_argument("--transport", choices=["auto", "direct", "herdr"], default="auto")
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--expect-marker", action="append", default=[])
    parser.add_argument("--require-clean-postcondition", action="store_true")
    parser.add_argument("--inspect-session-log-metadata", action="store_true")
    parser.add_argument("--require-session-log-metadata", action="store_true")
    parser.add_argument("--keep-pane", action="store_true")
    parser.add_argument("--repo-root", default=None, help="override canonical repository root (tests only)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:12]
    errors: list[str] = []

    repo_root = args.repo_root or str(Path(__file__).resolve().parent.parent.parent)

    try:
        worktree = verify_worktree_identity(args.worktree, repo_root)
    except IdentityError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return EXIT_FAIL

    transport = args.transport
    if args.mode == "interactive" and transport == "direct":
        print("[FAIL] --mode interactive requires --transport herdr or auto", file=sys.stderr)
        return EXIT_FAIL
    if args.mode == "interactive":
        transport = "herdr"
    elif transport == "auto":
        transport = "herdr" if os.environ.get("HERDR_ENV") == "1" else "direct"

    if transport == "herdr":
        skip_reason = preflight_herdr()
        if skip_reason and args.mode == "interactive":
            print(f"SKIP: {skip_reason}", file=sys.stderr)
            return EXIT_SKIP
        if skip_reason and args.mode == "structured":
            transport = "direct"

    if args.runtime == "claude":
        skip_reason = preflight_claude_flags()
    else:
        skip_reason = preflight_codex_flags()
    if skip_reason:
        print(f"SKIP: {skip_reason}", file=sys.stderr)
        return EXIT_SKIP

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

    before_status = _git_status_porcelain(worktree) if args.require_clean_postcondition else None

    exit_code = EXIT_OK
    schema_summary: dict = {
        "schema": SCHEMA,
        "run_id": run_id,
        "runtime": args.runtime,
        "mode": args.mode,
        "transport": transport,
        "worktree": os.path.relpath(worktree, repo_root),
        "timeout_seconds": args.timeout_seconds,
    }

    native_events: list[str] | None = None
    pane_output: list[str] | None = None
    agent_detection: dict | None = None
    session_log_metadata: list[dict] | None = None

    if args.mode == "structured":
        if args.runtime == "claude":
            rc, out, err, timed_out = run_structured_claude(worktree, prompt, float(args.timeout_seconds))
        else:
            rc, out, err, timed_out = run_structured_codex(worktree, prompt, float(args.timeout_seconds))

        native_events = _bounded_redacted_lines(out, _MAX_EVENT_LINES)
        event_count = parse_native_event_count(args.runtime, out)
        schema_summary["process_exit_code"] = rc
        schema_summary["timed_out"] = timed_out
        schema_summary["native_event_count"] = event_count

        if timed_out:
            errors.append("structured lane timed out")
            exit_code = EXIT_FAIL
        elif rc is None:
            errors.append(f"structured lane failed to start: {_redact(err[:500])}")
            exit_code = EXIT_FAIL
        elif rc != 0:
            errors.append(f"structured lane exited non-zero: {rc}")
            exit_code = EXIT_FAIL

        if args.expect_marker:
            combined = out + "\n" + err
            missing = [m for m in args.expect_marker if m not in combined]
            schema_summary["expected_markers_missing"] = missing
            if missing:
                errors.append(f"expected markers not observed: {missing}")
                exit_code = EXIT_FAIL

        if args.require_session_log_metadata or args.inspect_session_log_metadata:
            session_log_metadata = extract_session_log_metadata(out.splitlines())
            if args.require_session_log_metadata and not session_log_metadata:
                errors.append("session-log metadata required but unavailable")
                exit_code = EXIT_SKIP if exit_code == EXIT_OK else exit_code

    else:  # interactive
        try:
            evidence = run_interactive_herdr(
                args.runtime, worktree, prompt, float(args.timeout_seconds), run_id,
                keep_pane=args.keep_pane,
            )
            pane_output = evidence["pane_output_lines"]
            agent_detection = evidence.get("agent_explain")
            schema_summary["pane_id"] = evidence.get("pane_id")
            schema_summary["agent_name"] = evidence.get("agent_name")
            schema_summary["final_state"] = evidence.get("final_state")

            if evidence.get("final_state") == "blocked":
                errors.append("agent reached blocked state; evidence captured, not auto-approved")
                exit_code = EXIT_FAIL

            if args.expect_marker:
                combined = "\n".join(pane_output or [])
                missing = [m for m in args.expect_marker if m not in combined]
                schema_summary["expected_markers_missing"] = missing
                if missing:
                    errors.append(f"expected markers not observed in pane output: {missing}")
                    exit_code = EXIT_FAIL

            if args.require_session_log_metadata or args.inspect_session_log_metadata:
                session_log_metadata = []
                if args.require_session_log_metadata and not session_log_metadata:
                    errors.append("session-log metadata required but unavailable in interactive lane")
                    exit_code = EXIT_SKIP if exit_code == EXIT_OK else exit_code
        except HerdrLaneError as exc:
            errors.append(exc.message)
            exit_code = EXIT_SKIP if exc.skip else EXIT_FAIL

    if args.require_clean_postcondition and before_status is not None:
        after_status = _git_status_porcelain(worktree)
        if after_status is None:
            errors.append("could not evaluate postcondition (git status failed)")
            exit_code = EXIT_FAIL
        else:
            before_lines = set(_filter_evidence_lines(before_status, output_dir_rel))
            after_lines = set(_filter_evidence_lines(after_status, output_dir_rel))
            unexpected = after_lines - before_lines
            schema_summary["postcondition_unexpected_changes"] = sorted(unexpected)
            if unexpected:
                errors.append(f"unexpected postcondition changes: {sorted(unexpected)}")
                exit_code = EXIT_FAIL

    schema_summary["errors"] = errors
    schema_summary["exit_code"] = exit_code

    write_evidence(
        output_dir,
        schema_summary=schema_summary,
        native_events=native_events,
        pane_output=pane_output,
        agent_detection=agent_detection,
        session_log_metadata=session_log_metadata,
    )

    for error in errors:
        print(f"[FAIL] {error}" if exit_code == EXIT_FAIL else f"SKIP: {error}", file=sys.stderr)

    if exit_code == EXIT_OK:
        print(f"OK: runtime smoke evidence written to {output_dir}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
