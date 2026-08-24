#!/usr/bin/env python3
"""verify_agent_retrospective_live_smoke.py -- dual-runtime (Claude Code /
Claude-GPT) live smoke verifier for agent-retrospective (Issue #2239, Child 6
of #2192).

Issue #2239 PR #2331 fix_delta P0-1
(https://github.com/squne121/loop-protocol/pull/2331#issuecomment-5396730249):
this verifier now actually launches the root `/agent-retrospective` Skill
(via a headless ``claude -p`` slash invocation) and inspects the REAL
Bash tool_use/tool_result pair for the single `run_retrospective.py`
invocation that the Skill's own SKILL.md Procedure step 1 documents --
instead of hand-assembling ``SourcePlan``/``Evaluation`` and calling
``rr.finalize()`` directly in this process (the prior design, which never
executed the root Skill or the real observer wave at all).

Known, independently-confirmed production architecture gap (out of this
fix_delta's Allowed Paths to change -- `run_retrospective.py` is not
editable here): `run_cli()`/`main()` generate `run_id` as a *fresh*
`uuid.uuid4()` inside the call, with no CLI flag to pre-seed it. Any real
caller of the documented CLI entrypoint (including the root Skill session
launched below) therefore cannot know `ctx.run_id` ahead of time when
composing `--prompts-file` observer prompts -- so this verifier does not
try to guess it (previous versions of the observer-wave-bypassing test
data are not reused here). No `--prompts-file` is passed at all, so
`run_retrospective.py` runs with the documented empty-prompts default
(`prompts.get(observer_id, "")` -> `""`), and the real observer wave
against the real `retrospective-runtime-observer` leaf SubAgent
deterministically resolves to a typed failure at bundle validation
(`ObserverWaveFailed`, see `run_observer_wave()`'s `bundle.run_id !=
ctx.run_id` check) -- never a false "resolved". Issue #2239 AC6/AC7
explicitly accept either outcome ("PUBLISH_REQUEST_V1 or a typed failure
envelope, as long as it is parseable"), so this verifier treats a
well-formed typed failure as `status: "fail"` (not `"skip"`) and asserts
the invariants that are meaningful regardless of which branch production
took: schema-parseable output, `tested_head` match, repository fingerprint
diff clean, zero forbidden-mutation tool events, and (for the ``pass``
branch only) `run_identity.base_sha`/`request_id` binding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))
_DEFAULT_REPO_ROOT = _SCRIPTS_DIR.parents[3]
sys.path.insert(0, str(_DEFAULT_REPO_ROOT / "scripts" / "agent-ops"))

# Issue #2239 AC7 fix_delta (both the original PR and this fix_delta): reuse
# -- do not re-implement -- the claude-gpt launcher receipt / proxy PID-port-
# log side-channel parser / INDEPENDENT PID-listen-socket cleanup
# reconfirmation already implemented for `worktree-agent-runtime-smoke`
# (Issue #2174 AC8, #2219 AC1/AC7), plus `capture_runtime_version` (P1-5).
# No new attestation schema is introduced here; this verifier only calls the
# same production functions the existing runner already uses.
import run_worktree_agent_runtime_smoke as rwars  # noqa: E402

_RESULT_SCHEMA = "AGENT_RETROSPECTIVE_LIVE_SMOKE_RESULT_V1"
_LIVE_TIMEOUT_SEC = 240
_MAX_TURNS = 8
_ARTIFACTS_DIRNAME = "artifacts"
_REPOSITORY_ID = "squne121/loop-protocol"
_TARGET_ISSUE = 2239

_RUNTIME_PROFILES = {
    "claude_code": {"runtime": "claude", "claude_bin": None, "claude_adapter": "native"},
    "claude_gpt": {"runtime": "claude", "claude_bin": "scripts/claude-gpt/launch.sh", "claude_adapter": "claude-gpt"},
}

_FORBIDDEN_MUTATION_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_FORBIDDEN_BASH_SUBSTRINGS = (
    "git commit",
    "git push",
    "gh issue",
    "gh pr",
    "gh api",
)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _fail(reason_code: str, message: str, *, extra: dict[str, Any] | None = None) -> int:
    payload = {
        "schema": _RESULT_SCHEMA,
        "status": "fail",
        "reason_code": reason_code,
        "message": message,
    }
    if extra:
        payload.update(extra)
    _emit(payload)
    return 1


def _skip(reason: str) -> int:
    print(f"SKIP: {reason}", file=sys.stderr)
    _emit({"schema": _RESULT_SCHEMA, "status": "skip", "reason_code": "runtime_or_auth_unavailable", "message": reason})
    return 77


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=30)


def _repo_status_snapshot(repo_root: Path, *, exclude_relpath: str) -> set[str]:
    completed = _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    filtered = set()
    for line in lines:
        path = line[3:].strip()
        if path.startswith(exclude_relpath):
            continue
        filtered.add(line)
    return filtered


def _resolve_claude_code(repo_root: Path) -> tuple[str | None, list[str]]:
    del repo_root
    resolved = shutil.which("claude")
    if resolved is None:
        return None, []
    return resolved, [resolved]


def _resolve_claude_gpt(repo_root: Path) -> tuple[str | None, list[str]]:
    launcher = repo_root / "scripts" / "claude-gpt" / "launch.sh"
    if not launcher.is_file() or not __import__("os").access(launcher, __import__("os").X_OK):
        return None, []
    return str(launcher), [str(launcher), "-C", str(repo_root)]


def _preflight_runtime_ok(argv_prefix: list[str], *, is_claude_gpt: bool) -> bool:
    """Issue #2239 PR #2331 fix_delta P0-4: binary/auth-*availability* only.

    The prior implementation performed a real model round-trip here (a live
    prompt/response) and collapsed timeouts/connection failures into SKIP,
    contradicting the contract this repo already established at #2301: only
    binary/auth *unavailability* is SKIP; anything that starts invoking the
    model and then fails is a genuine FAIL. This preflight now only checks
    that the resolved binary/launcher answers a cheap, non-model
    administrative query (``auth status`` for native Claude Code; for
    claude-gpt there is no equivalent cheap administrative query exposed by
    the launcher, so presence + executability of the launcher file --
    already checked by ``_resolve_claude_gpt`` -- is treated as sufficient
    and no further preflight call is made here). Any failure *after* the
    real invocation begins (in ``main()`` below) is surfaced as FAIL, never
    silently downgraded to SKIP.
    """
    if is_claude_gpt:
        # No cheap non-model administrative probe is available through the
        # launcher; binary/executable-bit resolution (already performed by
        # the caller) is the whole of this profile's SKIP condition.
        return True
    try:
        completed = subprocess.run(
            [*argv_prefix, "auth", "status"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _find_bash_tool_result_by_marker(stdout: str, marker: str) -> tuple[str | None, str | None]:
    """Scan an ``--output-format stream-json`` transcript for the ``Bash``
    tool_use whose ``command`` contains ``marker`` and return
    ``(matched_command, tool_result_text)`` for its paired tool_result.
    ``tool_result_text`` is ``None`` if no paired result was found (e.g. the
    outer session never actually ran the command)."""
    tool_use_id: str | None = None
    matched_command: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if tool_use_id is None and block.get("type") == "tool_use" and block.get("name") == "Bash":
                command = (block.get("input") or {}).get("command", "")
                if isinstance(command, str) and marker in command:
                    tool_use_id = block.get("id")
                    matched_command = command
            is_paired_result = block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use_id
            if tool_use_id is not None and is_paired_result:
                result_content = block.get("content")
                if isinstance(result_content, list):
                    texts = [
                        b.get("text", "") for b in result_content if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    return matched_command, "\n".join(texts)
                if isinstance(result_content, str):
                    return matched_command, result_content
    return matched_command, None


def _count_forbidden_mutation_tool_events(stdout: str, *, allowed_bash_marker: str) -> int:
    """Count tool_use events representing a filesystem-mutation tool
    (Write/Edit/MultiEdit/NotebookEdit) or a Bash command containing a
    known git/gh mutation verb, EXCLUDING the single expected
    ``run_retrospective.py`` invocation (identified by
    ``allowed_bash_marker``, which is never itself a mutating command --
    the Skill is proposal-only)."""
    count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name in _FORBIDDEN_MUTATION_TOOLS:
                count += 1
                continue
            if name == "Bash":
                command = (block.get("input") or {}).get("command", "")
                if not isinstance(command, str) or allowed_bash_marker in command:
                    continue
                if any(needle in command for needle in _FORBIDDEN_BASH_SUBSTRINGS):
                    count += 1
    return count


def _last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-profile", required=True, choices=sorted(_RUNTIME_PROFILES))
    parser.add_argument("--repo-root", required=False, default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else _SCRIPTS_DIR.parents[3]
    profile = _RUNTIME_PROFILES[args.runtime_profile]
    is_claude_gpt = profile["claude_adapter"] == "claude-gpt"

    if is_claude_gpt:
        resolved_executable, argv_prefix = _resolve_claude_gpt(repo_root)
    else:
        resolved_executable, argv_prefix = _resolve_claude_code(repo_root)

    if resolved_executable is None:
        return _skip(f"skip_condition: runtime binary not available for profile={args.runtime_profile}")

    if not _preflight_runtime_ok(argv_prefix, is_claude_gpt=is_claude_gpt):
        return _skip(f"skip_condition: preflight check failed for profile={args.runtime_profile}")

    tested_head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
    if not tested_head:
        return _fail("tested_head_unresolvable", "could not resolve current HEAD via git rev-parse")

    artifacts_dir = _SCRIPTS_DIR / "tests" / _ARTIFACTS_DIRNAME
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    exclude_relpath = (
        str(artifacts_dir.relative_to(repo_root)) if artifacts_dir.is_relative_to(repo_root) else "__never__"
    )

    before_snapshot = _repo_status_snapshot(repo_root, exclude_relpath=exclude_relpath)

    nonce = uuid.uuid4().hex
    marker = f"live-smoke-marker:{nonce}"
    request_id = f"live-smoke-req-{nonce}"
    idempotency_key = f"live-smoke-idem-{nonce}"
    runtime_version = rwars.capture_runtime_version(resolved_executable)

    # The exact single Bash command SKILL.md's Procedure step 1 documents,
    # with a trailing shell comment carrying our correlation marker (a `#
    # comment` is inert to bash but appears verbatim in the Bash tool_use's
    # `command` field, letting this verifier find the right tool_use/
    # tool_result pair even if the outer session runs other Bash calls).
    # `--prompts-file` is deliberately omitted (see module docstring): no
    # caller of this documented CLI can know `ctx.run_id` ahead of time.
    inner_command = (
        "uv run --locked python3 "
        ".claude/skills/agent-retrospective/scripts/run_retrospective.py "
        f"--repository-id {_REPOSITORY_ID} --target-issue {_TARGET_ISSUE} "
        f"--request-id {request_id} --idempotency-key {idempotency_key} "
        f"--state-backend fixture  # {marker}"
    )
    slash_prompt = (
        f"/agent-retrospective live-smoke verification (Issue #2239 PR #2331 fix_delta P0-1, nonce={nonce}). "
        "Run exactly ONE Bash tool call with EXACTLY the following command line, verbatim -- do not add, "
        "remove, or alter any token, and do not run any other command before or after it:\n\n"
        f"{inner_command}\n\n"
        "After that single Bash call completes, take no further action: no other Bash/Read/Write/Edit/git/gh "
        "call, no summary. This session's own final text response is not read by the verifier -- only the "
        "Bash tool_use/tool_result event pair for the command above is inspected."
    )

    outer_argv = list(argv_prefix)
    if is_claude_gpt:
        outer_argv.append("--")
    outer_argv += [
        "-p",
        "--output-format",
        "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-turns",
        str(_MAX_TURNS),
        "--verbose",
    ]

    import os as _os

    launch_env = _os.environ.copy()
    if is_claude_gpt:
        launch_env["CLAUDE_GPT_RUNTIME_SMOKE_HOOKS"] = "subagent-start-stop"

    try:
        completed = subprocess.run(
            outer_argv,
            cwd=str(repo_root),
            env=launch_env,
            input=slash_prompt,
            capture_output=True,
            text=True,
            timeout=_LIVE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return _fail("live_invocation_timeout", "root Skill invocation timed out")
    except OSError as exc:
        return _fail("live_invocation_transport_error", f"root Skill invocation could not be started: {exc}")

    claude_gpt_launcher_receipt: dict[str, Any] | None = None
    claude_gpt_proxy_sidechannel: dict[str, Any] | None = None
    claude_gpt_proxy_cleanup_independent: dict[str, Any] | None = None
    if is_claude_gpt:
        # Issue #2239 AC7 fix_delta: reuse the launcher's own already-parsed
        # CLAUDE_GPT_LAUNCH_RESULT_V1 receipt and proxy PID/port/log
        # side-channel (Issue #2174 AC8, #2219 AC1/AC7), then INDEPENDENTLY
        # re-confirm proxy cleanup (never trusting the launcher's own
        # CLAUDE_GPT_PROXY_CLEANUP_OK self-report).
        launcher_result = rwars.extract_claude_gpt_launcher_receipt(completed.stderr)
        claude_gpt_proxy_sidechannel = rwars.extract_claude_gpt_proxy_sidechannel(completed.stderr)
        claude_gpt_proxy_cleanup_independent = rwars.verify_claude_gpt_proxy_cleanup_independent(
            claude_gpt_proxy_sidechannel["proxy_pid"], claude_gpt_proxy_sidechannel["proxy_port"]
        )
        # Issue #2239 PR #2331 fix_delta P0/P1-2: `checked == False` (cleanup
        # could not be independently reconfirmed at all) must FAIL, not pass
        # through. Only `checked is True and cleanup_confirmed is True` is a
        # success; every other combination (including `checked is False`) is
        # a fail-closed FAIL.
        cleanup_ok = (
            claude_gpt_proxy_cleanup_independent.get("checked") is True
            and claude_gpt_proxy_cleanup_independent.get("cleanup_confirmed") is True
        )
        if not cleanup_ok:
            return _fail(
                "claude_gpt_proxy_cleanup_not_independently_confirmed",
                "claude-gpt proxy cleanup was not independently reconfirmed via PID/listen-socket check "
                f"(self-reported={claude_gpt_proxy_sidechannel['proxy_cleanup_ok_self_reported']}, "
                f"checked={claude_gpt_proxy_cleanup_independent.get('checked')})",
                extra={
                    "claude_gpt_proxy_sidechannel": claude_gpt_proxy_sidechannel,
                    "claude_gpt_proxy_cleanup_independent": claude_gpt_proxy_cleanup_independent,
                },
            )
        claude_gpt_launcher_receipt = {
            "resolved_executable": resolved_executable,
            "resolved_executable_digest": _sha256_file(Path(resolved_executable)),
            "launcher_result": launcher_result,
        }

    if completed.returncode != 0:
        return _fail(
            "live_invocation_nonzero_exit",
            "root Skill invocation returned non-zero exit",
            extra={"exit_code": completed.returncode, "stderr_excerpt": completed.stderr[:800]},
        )

    forbidden_event_count = _count_forbidden_mutation_tool_events(completed.stdout, allowed_bash_marker=marker)

    matched_command, inner_result_text = _find_bash_tool_result_by_marker(completed.stdout, marker)
    if matched_command is None:
        return _fail(
            "expected_bash_call_not_found",
            "the outer session never issued the required run_retrospective.py Bash call",
            extra={"forbidden_mutation_tool_events": forbidden_event_count},
        )
    if inner_result_text is None:
        return _fail(
            "expected_bash_call_no_result",
            "the required Bash call's tool_use had no paired tool_result in the transcript",
            extra={"matched_command": matched_command, "forbidden_mutation_tool_events": forbidden_event_count},
        )

    inner_payload = _last_json_line(inner_result_text)
    if inner_payload is None:
        return _fail(
            "run_retrospective_output_unparseable",
            "run_retrospective.py's Bash tool_result did not contain a parseable JSON line",
            extra={
                "inner_result_excerpt": inner_result_text[:1200],
                "forbidden_mutation_tool_events": forbidden_event_count,
            },
        )

    after_snapshot = _repo_status_snapshot(repo_root, exclude_relpath=exclude_relpath)
    fingerprint_clean = before_snapshot == after_snapshot

    receipt: dict[str, Any] = {
        "schema": _RESULT_SCHEMA,
        "runtime_profile": args.runtime_profile,
        "adapter": profile["claude_adapter"],
        "resolved_executable": resolved_executable,
        "resolved_executable_digest": _sha256_file(Path(resolved_executable)),
        "runtime_version": runtime_version,
        "tested_head": tested_head,
        "repository_fingerprint_diff_clean": fingerprint_clean,
        "fallback_used": False,
        "nonce": nonce,
        "marker": marker,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "prompt_digest": hashlib.sha256(slash_prompt.encode("utf-8")).hexdigest(),
        "inner_command": matched_command,
        "outer_exit_code": completed.returncode,
        "forbidden_mutation_tool_events": forbidden_event_count,
        "inner_result": inner_payload,
    }

    if inner_payload.get("status") == "failed" and "reason_code" in inner_payload:
        receipt["status"] = "fail"
        receipt["reason_code"] = inner_payload["reason_code"]
        receipt["message"] = "real /agent-retrospective invocation produced a typed production failure envelope"
    elif "run_identity" in inner_payload and "request_id" in inner_payload:
        run_identity = inner_payload.get("run_identity") or {}
        expected_base_sha = _git(repo_root, "rev-parse", "main").stdout.strip()
        identity_ok = (
            inner_payload.get("request_id") == request_id
            and run_identity.get("base_sha") == expected_base_sha
            and bool(run_identity.get("run_id"))
            and bool(run_identity.get("source_set_digest"))
        )
        if not identity_ok:
            receipt["status"] = "fail"
            receipt["reason_code"] = "publish_request_identity_mismatch"
            receipt["message"] = (
                "run_retrospective.py returned a PublishRequest but request_id/base_sha did not match this run"
            )
        else:
            receipt["status"] = "pass"
    else:
        receipt["status"] = "fail"
        receipt["reason_code"] = "run_retrospective_output_unrecognized"
        receipt["message"] = "run_retrospective.py's output was neither a typed failure nor a PublishRequest"

    if claude_gpt_launcher_receipt is not None:
        receipt["claude_gpt_launcher_receipt"] = claude_gpt_launcher_receipt
        receipt["claude_gpt_proxy_sidechannel"] = claude_gpt_proxy_sidechannel
        receipt["claude_gpt_proxy_cleanup_independent"] = claude_gpt_proxy_cleanup_independent

    artifact_path = artifacts_dir / f"live_smoke_{args.runtime_profile}_{int(time.time())}_{nonce}.json"
    artifact_path.write_text(json.dumps(receipt, sort_keys=True, indent=2), encoding="utf-8")

    if not fingerprint_clean:
        receipt["status"] = "fail"
        receipt["reason_code"] = "repository_fingerprint_diff_detected"
        receipt["message"] = "repository status changed (outside artifacts/) between pre/post live invocation"
        _emit(receipt)
        return 1
    if forbidden_event_count != 0:
        receipt["status"] = "fail"
        receipt["reason_code"] = "forbidden_mutation_tool_event_detected"
        receipt["message"] = f"{forbidden_event_count} forbidden mutation tool event(s) observed"
        _emit(receipt)
        return 1

    # Both "pass" (a full PublishRequest bound to this run) and "fail" (a
    # well-formed typed production failure envelope, e.g. the documented
    # run_id-prompt-binding gap above) are legitimate, expected completions
    # of this live verification per Issue #2239 AC6/AC7 ("PUBLISH_REQUEST_V1
    # or a typed failure envelope, as long as it is parseable") -- exit 0
    # either way. Exit 1 (via `_fail()` above) is reserved for genuine
    # verifier/production anomalies (timeouts, transport errors, malformed
    # transcripts, fingerprint drift, forbidden mutation events).
    _emit(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
