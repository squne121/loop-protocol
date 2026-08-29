#!/usr/bin/env python3
"""run_native_session_continuation_canary.py — Issue #2153.

Native Claude Code session continuation canary for the ``claude-native`` and
``claude-gpt`` Claude Code dual-runtime lanes (Issue #2154).

## Terminology

``claude_code_session_id``
    The ``session_id`` field observed in Claude Code's own
    ``--output-format json``/``stream-json`` structured output. This is the
    authoritative continuation identity accepted by ``--resume``. This
    canary observes and compares ONLY this identity.

``provider_continuation_state``
    Out of Scope. ``claude-gpt`` proxy internal state (e.g.
    ``previous_response_id``) is never inspected or asserted on here.

## Runtime Exit Code Ownership (Issue #2153 OWNER review P1-2)

This runner (not the focused pytest process) owns the exit code contract:

    0  = PASS  (all ID/terminal/argv assertions succeeded, no fallback)
    1  = FAIL  (an assertion failed, OR an explicit runtime/provider
                fallback indication was observed --
                ``runtime_fallback: true`` / ``provider_fallback: true``.
                Fallback-derived "success" is never promoted to PASS.)
    77 = SKIP  (the selected runtime/auth/proxy/continuation capability is
                unavailable -- never promoted to PASS)

## Scope

The three-launch (initial / same-continuation / fresh) session lifecycle
state machine in this file is intentionally LOCAL to this canary and is not
added to the generic ``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``
harness (Issue #2153 OWNER review P2). Reuse of that harness's safety
primitives is deliberately limited to: executable/launcher resolution,
``--claude-adapter native|claude-gpt`` selection, timeout/process
termination, stdout/stderr capture, worktree identity, exclusive evidence
directory, and runtime-unavailable classification -- see the
``_load_runtime_smoke_reuse()`` import block below for the exact reused
symbols.

This canary does not exercise ``reviewer_transport.py`` or any production
transport/retry/adapter logic (#2352's scope, not this Issue's).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Reuse of scripts/agent-ops/run_worktree_agent_runtime_smoke.py primitives
# (Issue #2153 AC6). Dynamic file-path import, matching the established
# repo-wide pattern (see e.g.
# .claude/skills/impl-review-loop/scripts/verify_scope_rollup_result.py).
# ---------------------------------------------------------------------------


def _load_runtime_smoke_reuse():
    repo_root = Path(__file__).resolve().parents[4]
    script_path = repo_root / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "issue_2153_native_canary_runtime_smoke_reuse", script_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SMOKE = _load_runtime_smoke_reuse()

# Reused generic primitives -- executable/launcher resolution:
preflight_claude_available = _SMOKE.preflight_claude_available
extract_claude_resolved_executable_sha256 = _SMOKE.extract_claude_resolved_executable_sha256
# Reused generic primitives -- --claude-adapter native|claude-gpt selection
# observability (parses the launcher's own already-structured receipt; does
# not duplicate the launcher's forbidden-flag policy):
extract_claude_gpt_launcher_receipt = _SMOKE.extract_claude_gpt_launcher_receipt
# Reused generic primitives -- timeout/process termination, stdout/stderr
# capture:
_run = _SMOKE._run
_install_signal_handlers = _SMOKE._install_signal_handlers
_TerminateRequested = _SMOKE._TerminateRequested
# Reused generic primitives -- bounded/redacted capture:
_redact = _SMOKE._redact
_bounded_redacted_lines = _SMOKE._bounded_redacted_lines
# Reused generic primitives -- linked-worktree identity, exclusive evidence
# directory:
verify_worktree_identity = _SMOKE.verify_worktree_identity
IdentityError = _SMOKE.IdentityError
prepare_output_dir = _SMOKE.prepare_output_dir
_default_repo_root = _SMOKE._default_repo_root
# Reused generic primitives -- process-group cleanup verdict (repository
# postcondition fingerprint):
repo_fingerprint = _SMOKE.repo_fingerprint
diff_fingerprints = _SMOKE.diff_fingerprints
# Reused generic primitives -- runtime-unavailable classification:
classify_claude_structured_outcome = _SMOKE.classify_claude_structured_outcome
has_terminal_event = _SMOKE.has_terminal_event
extract_claude_parent_session_id = _SMOKE.extract_claude_parent_session_id


# ---------------------------------------------------------------------------
# Local (canary-owned) argv contract -- the "initial/resume/fresh" session
# lifecycle state machine and its argv shape are NOT part of the reused
# generic module (Issue #2153 In Scope / OWNER review P2).
# ---------------------------------------------------------------------------

FORBIDDEN_INITIAL_FRESH_FLAGS = (
    "--resume",
    "--continue",
    "--session-id",
    "--fork-session",
    "--no-session-persistence",
)

FORBIDDEN_RESUME_EXTRA_FLAGS = (
    "--continue",
    "--session-id",
    "--fork-session",
    "--no-session-persistence",
)

_DEFAULT_MAX_TURNS = 3


def build_launch_argv(
    claude_bin: str,
    claude_adapter: str,
    phase: str,
    *,
    max_turns: int = _DEFAULT_MAX_TURNS,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build the argv for one of the three canary launch phases.

    ``phase``: ``"initial"`` | ``"resume"`` | ``"fresh"``. ``"initial"`` and
    ``"fresh"`` never include a continuation directive (Issue #2153 AC8);
    ``"resume"`` includes ``--resume <resume_session_id>`` exactly once.
    Session persistence is left at its Claude Code default (i.e.
    ``--no-session-persistence`` is never passed) so a later launch can
    genuinely ``--resume`` the session this canary itself created.
    """
    if phase not in ("initial", "resume", "fresh"):
        raise ValueError(f"unknown phase: {phase}")
    if phase == "resume" and not resume_session_id:
        raise ValueError("resume phase requires resume_session_id")

    argv = [claude_bin]
    if claude_adapter == "claude-gpt":
        # scripts/claude-gpt/launch.sh only accepts its own launcher options
        # before a literal ``--`` separator; everything after is forwarded
        # unparsed to the underlying claude binary (see
        # references/claude-code.md and launch.sh's own --claude-bin/
        # --check-only/--dry-run parsing loop).
        argv.append("--")
    argv += ["-p", "--output-format", "stream-json", "--verbose"]
    if phase == "resume":
        argv += ["--resume", str(resume_session_id)]
    argv += ["--max-turns", str(max_turns)]
    return argv


def verify_argv_contract(
    argv: list[str], phase: str, *, resume_session_id: str | None = None
) -> tuple[bool, str | None]:
    """Pure argv-contract check (Issue #2153 AC8). Returns ``(ok, detail)``.

    ``detail`` is ``None`` when ``ok`` is ``True``, otherwise a short
    human-readable violation description.
    """
    if phase in ("initial", "fresh"):
        for forbidden in FORBIDDEN_INITIAL_FRESH_FLAGS:
            if forbidden in argv:
                return False, f"forbidden flag present in {phase} launch argv: {forbidden}"
        return True, None
    if phase == "resume":
        count = argv.count("--resume")
        if count != 1:
            return False, f"--resume must appear exactly once in resume launch argv, found {count}"
        idx = argv.index("--resume")
        if idx + 1 >= len(argv) or argv[idx + 1] != resume_session_id:
            return False, "resume launch argv --resume value does not match the observed session id"
        for forbidden in FORBIDDEN_RESUME_EXTRA_FLAGS:
            if forbidden in argv:
                return False, f"forbidden flag present in resume launch argv: {forbidden}"
        return True, None
    return False, f"unknown phase: {phase}"


def build_launch_env() -> dict[str, str]:
    """Environment for a canary launch subprocess.

    Issue #2153 In Scope: ``CLAUDE_CODE_SKIP_PROMPT_HISTORY`` is explicitly
    unset/ignored regardless of whether the parent process happened to have
    it set -- the canary's own behavior must never depend on it.
    """
    env = os.environ.copy()
    env.pop("CLAUDE_CODE_SKIP_PROMPT_HISTORY", None)
    return env


# ---------------------------------------------------------------------------
# Local (canary-owned) structured-output parsing for the terminal-success
# event and the semantic continuity marker probe (AC3/AC5). These are
# session-lifecycle-specific, not part of the reused generic primitives.
# ---------------------------------------------------------------------------


def parse_terminal_result_event(stdout: str) -> dict | None:
    """Return the native ``type: "result"`` terminal event payload, or
    ``None`` if absent. Does not assume success -- callers must check
    ``is_error``."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            return payload
    return None


def is_terminal_success(stdout: str) -> tuple[bool, str | None]:
    """Issue #2153 AC5: a successful launch requires a terminal-success
    event AND exit 0. Returns ``(ok, reason)``."""
    if not has_terminal_event("claude", stdout):
        return False, "no terminal (type=result) event observed in stdout"
    event = parse_terminal_result_event(stdout)
    if event is None:
        return False, "terminal event detected but could not be parsed"
    if event.get("is_error") is True:
        return False, f"terminal event reported is_error=true (subtype={event.get('subtype')!r})"
    if event.get("subtype") not in (None, "success"):
        return False, f"terminal event subtype is not success: {event.get('subtype')!r}"
    return True, None


def extract_assistant_texts(stdout: str) -> list[str]:
    """Local extraction of ``type: "assistant"`` message text blocks from
    native stream-json stdout, used only for the semantic continuity marker
    probe (AC3)."""
    texts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content:
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        texts.append(text)
    return texts


def marker_recalled(stdout: str, marker: str) -> bool:
    return any(marker in text for text in extract_assistant_texts(stdout))


# ---------------------------------------------------------------------------
# claude-gpt adapter capability preflight (local -- launcher-specific, not
# present in the generic module's preflight_claude_available()).
# ---------------------------------------------------------------------------

_CLAUDE_GPT_RECEIPT_MARKER_RE = re.compile(r'\{\s*"schema"\s*:\s*"CLAUDE_GPT_LAUNCH_RESULT_V1"')


def _parse_claude_gpt_receipt(text: str) -> dict | None:
    """Robustly parse ``scripts/claude-gpt/launch.sh``'s own
    ``CLAUDE_GPT_LAUNCH_RESULT_V1`` JSON receipt from raw text.

    The reused generic ``extract_claude_gpt_launcher_receipt()`` uses a
    single-line regex (``[^\\n]*``), which does not match this launcher's
    ``--check-only`` success receipt: it embeds a pretty-printed (multi-line)
    nested ``preflight`` object inside the outer JSON object, confirmed
    empirically against a live ``--check-only`` invocation. This local
    parser uses ``json.JSONDecoder.raw_decode`` (bracket-aware, not
    line-bound) starting at the first occurrence of the receipt's marker
    (tolerant of whitespace between tokens), so it parses correctly
    regardless of embedded newlines or spacing.
    """
    match = _CLAUDE_GPT_RECEIPT_MARKER_RE.search(text)
    if match is None:
        return None
    idx = match.start()
    decoder = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(text[idx:])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def preflight_claude_gpt(claude_bin: str, timeout_seconds: float) -> tuple[bool, str | None, dict | None]:
    """Run ``<launch.sh> --check-only`` and classify availability from its
    own ``CLAUDE_GPT_LAUNCH_RESULT_V1`` receipt (the receipt lands on
    stdout in --check-only mode; see ``_parse_claude_gpt_receipt``)."""
    rc, out, err, timed_out = _run([claude_bin, "--check-only"], timeout=timeout_seconds)
    if timed_out:
        return False, "claude-gpt launcher --check-only timed out", None
    receipt = (
        _parse_claude_gpt_receipt(out)
        or _parse_claude_gpt_receipt(err)
        or extract_claude_gpt_launcher_receipt(out)
        or extract_claude_gpt_launcher_receipt(err)
    )
    if receipt is None:
        return False, f"claude-gpt launcher --check-only produced no receipt (exit {rc})", None
    if receipt.get("status") != "ok":
        return False, f"claude-gpt launcher unavailable: {receipt.get('reason') or receipt.get('status')}", receipt
    return True, None, receipt


def _launcher_failure_reason(receipt: dict) -> str | None:
    return receipt.get("reason") or receipt.get("status")


def detect_claude_gpt_launch_failure_receipt(stdout: str, stderr: str) -> dict | None:
    """During a real (non-check-only) launch, a pre-claude launcher failure
    (preflight_failed / proxy_not_ready_or_bind_not_confirmed /
    model_alias_not_resolved / claude_binary_not_found) is printed as a
    ``CLAUDE_GPT_LAUNCH_RESULT_V1`` receipt to stdout, before claude's own
    stream-json ever starts. Detecting it here (rather than treating the
    absence of a terminal claude event as a plain FAIL) lets the canary
    classify this as a runtime/proxy capability-unavailable SKIP (AC7)."""
    receipt = _parse_claude_gpt_receipt(stdout) or _parse_claude_gpt_receipt(stderr)
    if receipt is not None and receipt.get("status") in ("blocked", "failed"):
        return receipt
    return None


# ---------------------------------------------------------------------------
# Fallback detection (Issue #2153 AC7 / Runtime Exit Code Ownership).
# Both signals are concrete, evidence-derived, and explicitly handled by
# this canary -- fallback-derived "success" is never promoted to PASS.
# ---------------------------------------------------------------------------


def detect_provider_fallback(launcher_receipt: dict | None) -> bool:
    """claude-gpt lane only: the launcher's own preflight receipt reports
    ``model_alias_ok: false`` -- the proxy silently substituted a model
    other than the one requested. This is a genuine provider-level
    fallback, not this canary's session-lifecycle assertion."""
    if launcher_receipt is None:
        return False
    return launcher_receipt.get("model_alias_ok") is False


def detect_runtime_fallback(id_equal: bool, marker_ok: bool) -> bool:
    """Both lanes: the same-continuation launch reports the SAME
    ``claude_code_session_id`` as the initial launch (structured-output ID
    equality holds) but the semantic continuity marker probe fails (the
    model does not actually recall the marker established in the initial
    launch). This is exactly the "fallback-looking success" the Issue's
    Stop Conditions warn about -- ID equality alone is not treated as
    sufficient continuation proof (AC3)."""
    return id_equal and not marker_ok


# ---------------------------------------------------------------------------
# Evidence artifact (sanitized; Issue #2153 artifact_requirements).
# ---------------------------------------------------------------------------


def _hash_id(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _scrub_uuid_ids(lines: list[str]) -> list[str]:
    """Defense-in-depth: substitute any UUID-shaped token (a Claude Code
    ``claude_code_session_id`` is a UUID) with a fixed placeholder before a
    line is written to the evidence artifact."""
    return [_UUID_RE.sub("<redacted-id>", line) for line in lines]


def write_evidence(output_dir: Path, evidence: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Main canary sequence.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, help="linked worktree absolute path")
    parser.add_argument("--claude-adapter", required=True, choices=["native", "claude-gpt"])
    parser.add_argument(
        "--claude-bin",
        default=None,
        help="absolute path override. Required for --claude-adapter claude-gpt "
        "(must point at scripts/claude-gpt/launch.sh).",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=180.0, help="bound per launch")
    parser.add_argument("--max-turns", type=int, default=_DEFAULT_MAX_TURNS)
    parser.add_argument("--require-clean-postcondition", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    args = build_parser().parse_args(argv)

    evidence: dict[str, Any] = {
        "schema": "NATIVE_SESSION_CONTINUATION_CANARY_RESULT_V1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": args.claude_adapter,
        "command_class": "claude_structured_print_mode",
        "verdict": "FAIL",
        "exit_code": 1,
        "cleanup": {"postcondition_checked": bool(args.require_clean_postcondition)},
        "errors": [],
    }

    try:
        repo_root = _default_repo_root()
        worktree_real = verify_worktree_identity(args.worktree, repo_root)
    except IdentityError as exc:
        evidence["errors"].append(f"worktree identity error: {exc.message}")
        write_evidence(args.output_dir, evidence)
        return 1

    output_dir_error = prepare_output_dir(args.output_dir)
    if output_dir_error:
        # Cannot use write_evidence() (it assumes exclusive create); report
        # via stderr only.
        print(json.dumps({"schema": evidence["schema"], "verdict": "FAIL", "error": output_dir_error}), file=sys.stderr)
        return 1

    if args.claude_adapter == "claude-gpt" and not args.claude_bin:
        evidence["errors"].append("--claude-bin is required for --claude-adapter claude-gpt")
        write_evidence(args.output_dir, evidence)
        return 1

    resolved_bin, skip_reason = preflight_claude_available(args.claude_bin)
    if resolved_bin is None:
        evidence["verdict"] = "SKIP"
        evidence["exit_code"] = 77
        evidence["skip_reason"] = skip_reason
        write_evidence(args.output_dir, evidence)
        return 77
    evidence["resolved_executable_sha256"] = extract_claude_resolved_executable_sha256(resolved_bin)

    launcher_receipt: dict | None = None
    if args.claude_adapter == "claude-gpt":
        available, skip_reason, launcher_receipt = preflight_claude_gpt(resolved_bin, args.timeout_seconds)
        if not available:
            evidence["verdict"] = "SKIP"
            evidence["exit_code"] = 77
            evidence["skip_reason"] = skip_reason
            evidence["claude_gpt_launcher_receipt"] = launcher_receipt
            write_evidence(args.output_dir, evidence)
            return 77
        evidence["claude_gpt_launcher_receipt"] = launcher_receipt
        if detect_provider_fallback(launcher_receipt):
            evidence["verdict"] = "FAIL"
            evidence["exit_code"] = 1
            evidence["provider_fallback"] = True
            evidence["errors"].append("claude-gpt launcher receipt reports model_alias_ok=false (provider fallback)")
            write_evidence(args.output_dir, evidence)
            return 1

    before_fingerprint = repo_fingerprint(worktree_real, None) if args.require_clean_postcondition else None

    # Issue #2153 live finding: a "secret"/"passphrase"-worded marker probe
    # triggered a safety refusal on the claude-gpt lane (the underlying
    # model declined to repeat it on resume, `"result":"...情報は開示できません"`,
    # even though ``cache_read_input_tokens`` on that same reply proved the
    # prior turn's context was genuinely loaded -- a prompt-wording
    # artifact, not a continuation failure). A neutral "reference token"
    # wording does not trigger this and was confirmed live to round-trip
    # correctly on both lanes.
    marker = f"tok-{secrets.token_hex(8)}"
    env = build_launch_env()
    evidence["env_contract"] = {"claude_code_skip_prompt_history_unset": "CLAUDE_CODE_SKIP_PROMPT_HISTORY" not in env}

    def launch(phase: str, prompt: str, resume_session_id: str | None = None) -> tuple[dict, str, str, bool]:
        launch_argv = build_launch_argv(
            resolved_bin,
            args.claude_adapter,
            phase,
            max_turns=args.max_turns,
            resume_session_id=resume_session_id,
        )
        argv_ok, argv_detail = verify_argv_contract(launch_argv, phase, resume_session_id=resume_session_id)
        rc, out, err, timed_out = _run(
            launch_argv, cwd=worktree_real, timeout=args.timeout_seconds, input_text=prompt, env=env
        )
        # NOTE (Issue #2153 artifact_requirements): stdout is the raw native
        # stream-json event stream and carries the raw claude_code_session_id
        # plus assistant/conversation text on every event -- it is never
        # written to the evidence artifact, even redacted/bounded. Only a
        # non-sensitive line count is kept for diagnostics. stderr is
        # launcher/parser diagnostic text (no session id, no conversation
        # text observed in practice); it is still defense-in-depth
        # UUID-scrubbed on top of the reused bounded/redacted capture before
        # being written.
        record = {
            "phase": phase,
            "argv_contract_ok": argv_ok,
            "argv_contract_detail": argv_detail,
            "exit_code": rc,
            "timed_out": timed_out,
            "stdout_line_count": len(out.splitlines()),
            "stderr_redacted_tail": _scrub_uuid_ids(_bounded_redacted_lines(err, 5)),
        }
        return record, out, err, timed_out

    # ---- Phase 1: initial launch -----------------------------------------
    initial_record, initial_out, initial_err, initial_timed_out = launch(
        "initial",
        f"For this automated canary check, the reference token for this conversation is: {marker} . "
        "Respond with exactly one line: ACK. Do not use any tools.",
    )
    evidence["initial_launch"] = initial_record

    if args.claude_adapter == "claude-gpt":
        failure_receipt = detect_claude_gpt_launch_failure_receipt(initial_out, initial_err)
        if failure_receipt is not None:
            evidence["verdict"] = "SKIP"
            evidence["exit_code"] = 77
            evidence["skip_reason"] = (
                f"claude-gpt launcher unavailable at initial launch: {_launcher_failure_reason(failure_receipt)}"
            )
            evidence["claude_gpt_launcher_receipt"] = failure_receipt
            write_evidence(args.output_dir, evidence)
            return 77

    decision, reason = classify_claude_structured_outcome(
        initial_record["exit_code"], initial_out, initial_err, initial_timed_out
    )
    if decision == "capability_skip":
        evidence["verdict"] = "SKIP"
        evidence["exit_code"] = 77
        evidence["skip_reason"] = reason
        write_evidence(args.output_dir, evidence)
        return 77

    if not initial_record["argv_contract_ok"]:
        evidence["errors"].append(f"initial launch argv contract violation: {initial_record['argv_contract_detail']}")
        write_evidence(args.output_dir, evidence)
        return 1

    ok, reason = is_terminal_success(initial_out)
    if not ok:
        evidence["errors"].append(f"initial launch: {reason}")
        write_evidence(args.output_dir, evidence)
        return 1

    observed_id_1 = extract_claude_parent_session_id(initial_out)
    evidence["initial_launch"]["observed_session_id_hash"] = _hash_id(observed_id_1)
    if not observed_id_1:
        evidence["errors"].append(
            "initial launch: no claude_code_session_id observed in native structured output "
            "(synthetic_parent_generated_id is forbidden -- refusing to substitute one)"
        )
        write_evidence(args.output_dir, evidence)
        return 1

    # ---- Phase 2: same-continuation (resume) launch ------------------------
    resume_record, resume_out, resume_err, resume_timed_out = launch(
        "resume",
        "What reference token did I mention earlier in this conversation? "
        "Respond with exactly that token and nothing else. Do not use any tools.",
        resume_session_id=observed_id_1,
    )
    evidence["same_continuation_launch"] = resume_record

    if args.claude_adapter == "claude-gpt":
        failure_receipt = detect_claude_gpt_launch_failure_receipt(resume_out, resume_err)
        if failure_receipt is not None:
            evidence["verdict"] = "SKIP"
            evidence["exit_code"] = 77
            evidence["skip_reason"] = (
                f"claude-gpt launcher unavailable at resume launch: {_launcher_failure_reason(failure_receipt)}"
            )
            evidence["claude_gpt_launcher_receipt"] = failure_receipt
            write_evidence(args.output_dir, evidence)
            return 77

    decision, reason = classify_claude_structured_outcome(
        resume_record["exit_code"], resume_out, resume_err, resume_timed_out
    )
    if decision == "capability_skip":
        evidence["verdict"] = "SKIP"
        evidence["exit_code"] = 77
        evidence["skip_reason"] = reason
        write_evidence(args.output_dir, evidence)
        return 77

    if not resume_record["argv_contract_ok"]:
        evidence["errors"].append(f"resume launch argv contract violation: {resume_record['argv_contract_detail']}")
        write_evidence(args.output_dir, evidence)
        return 1

    ok, reason = is_terminal_success(resume_out)
    if not ok:
        evidence["errors"].append(f"resume launch: {reason}")
        write_evidence(args.output_dir, evidence)
        return 1

    observed_id_2 = extract_claude_parent_session_id(resume_out)
    evidence["same_continuation_launch"]["observed_session_id_hash"] = _hash_id(observed_id_2)
    id_equal = bool(observed_id_2) and observed_id_2 == observed_id_1
    evidence["same_continuation_launch"]["id_equality"] = id_equal
    if not id_equal:
        evidence["errors"].append("resume launch: observed session id does not equal the initial observed session id")
        write_evidence(args.output_dir, evidence)
        return 1

    marker_ok = marker_recalled(resume_out, marker)
    evidence["same_continuation_launch"]["semantic_continuity_marker_recalled"] = marker_ok

    if detect_runtime_fallback(id_equal, marker_ok):
        evidence["verdict"] = "FAIL"
        evidence["exit_code"] = 1
        evidence["runtime_fallback"] = True
        evidence["errors"].append(
            "runtime_fallback: session id equality held but the semantic continuity marker was not "
            "recalled -- this is a fallback-looking success, not genuine continuation, and is not "
            "treated as PASS evidence"
        )
        write_evidence(args.output_dir, evidence)
        return 1

    # ---- Phase 3: fresh launch ---------------------------------------------
    fresh_record, fresh_out, fresh_err, fresh_timed_out = launch(
        "fresh",
        "Respond with exactly one line: FRESH. Do not use any tools.",
    )
    evidence["fresh_launch"] = fresh_record

    if args.claude_adapter == "claude-gpt":
        failure_receipt = detect_claude_gpt_launch_failure_receipt(fresh_out, fresh_err)
        if failure_receipt is not None:
            evidence["verdict"] = "SKIP"
            evidence["exit_code"] = 77
            evidence["skip_reason"] = (
                f"claude-gpt launcher unavailable at fresh launch: {_launcher_failure_reason(failure_receipt)}"
            )
            evidence["claude_gpt_launcher_receipt"] = failure_receipt
            write_evidence(args.output_dir, evidence)
            return 77

    decision, reason = classify_claude_structured_outcome(
        fresh_record["exit_code"], fresh_out, fresh_err, fresh_timed_out
    )
    if decision == "capability_skip":
        evidence["verdict"] = "SKIP"
        evidence["exit_code"] = 77
        evidence["skip_reason"] = reason
        write_evidence(args.output_dir, evidence)
        return 77

    if not fresh_record["argv_contract_ok"]:
        evidence["errors"].append(f"fresh launch argv contract violation: {fresh_record['argv_contract_detail']}")
        write_evidence(args.output_dir, evidence)
        return 1

    ok, reason = is_terminal_success(fresh_out)
    if not ok:
        evidence["errors"].append(f"fresh launch: {reason}")
        write_evidence(args.output_dir, evidence)
        return 1

    observed_id_3 = extract_claude_parent_session_id(fresh_out)
    evidence["fresh_launch"]["observed_session_id_hash"] = _hash_id(observed_id_3)
    id_unequal = bool(observed_id_3) and observed_id_3 != observed_id_1
    evidence["fresh_launch"]["id_inequality"] = id_unequal
    if not id_unequal:
        evidence["errors"].append(
            "fresh launch: observed session id was missing or equal to the initial observed session id "
            "(fresh launch must produce a distinct id, and never a synthetic substitute)"
        )
        write_evidence(args.output_dir, evidence)
        return 1

    # ---- Cleanup / postcondition verdict -----------------------------------
    if args.require_clean_postcondition:
        after_fingerprint = repo_fingerprint(worktree_real, None)
        diffs = diff_fingerprints(before_fingerprint, after_fingerprint)
        evidence["cleanup"]["postcondition_diffs"] = diffs
        if diffs:
            evidence["errors"].append(f"postcondition violated: {diffs}")
            write_evidence(args.output_dir, evidence)
            return 1

    evidence["verdict"] = "PASS"
    evidence["exit_code"] = 0
    evidence["provider_fallback"] = False
    evidence["runtime_fallback"] = False
    write_evidence(args.output_dir, evidence)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except _TerminateRequested:
        sys.exit(1)
