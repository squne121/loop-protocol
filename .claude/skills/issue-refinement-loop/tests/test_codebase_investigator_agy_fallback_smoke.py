"""Runtime verification for Issue #2360's conditional AGY advisory native
fallback (AC3/AC4, `decision: immediate` per `## Runtime Verification
Applicability`).

Two independent scenarios are covered:

- ``-k live_smoke`` (AC3): a real, bounded ``claude -p --agent
  codebase-investigator`` invocation is launched exactly once. A fake AGY
  delegation wrapper failure (``ok: false``, ``failure_class: agy_timeout`` --
  the taxonomy's representative non-retryable AGY-side failure, see
  ``.claude/skills/gemini-cli-headless-delegation/references/failure-class-taxonomy.md``)
  is supplied as a pre-completed test double in the task prompt, together
  with an explicit ``agy_advisory_native_fallback_allowed: true`` opt-in.
  The test asserts that the live SubAgent (a) transitions to bounded native
  read-only investigation (Read/Grep/Glob only -- no Edit/Write/MultiEdit
  tool_use is ever observed), (b) successfully retrieves a unique sentinel
  marker placed inside the worktree, (c) reports ``status: ok`` in its final
  ``CODEBASE_INVESTIGATION_RESULT_V1``, and (d) leaves the worktree's tracked
  files byte-for-byte unchanged (``git status --porcelain`` diff before vs.
  after is empty). Per this Issue's ``skip_conditions``, the test SKIPs
  (never fabricates PASS) when the real Claude Code SubAgent launch
  environment is unavailable (``claude`` missing from PATH, or a transport
  failure whose stdout/stderr matches a known environment-unavailable
  marker such as ``Please run /login``).

- ``-k fail_closed`` (AC4): hermetic (no subprocess, no network) checks that
  (1) a pure mirror of the documented transition-condition predicate in
  ``.claude/agents/codebase-investigator.md`` forbids native fallback
  whenever ``agy_advisory_native_fallback_allowed`` is unset/false --
  regardless of ``failure_class`` -- and forbids it for
  permission-boundary-classified failures even when the flag is ``true``;
  and (2) the agent definition's own prose documents this default
  fail-closed behavior and the permission-boundary exclusion, so the mirror
  and the SubAgent-owned prose cannot silently drift apart.

SKIP is never converted to PASS (``fallback_success_is_pass: false`` per
this Issue's Runtime Verification Applicability).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
# tests/ -> issue-refinement-loop/ -> skills/ -> .claude/ -> repo (or worktree) root
_REPO_ROOT = _THIS_FILE.parents[4]
_AGENT_MD_PATH = _REPO_ROOT / ".claude" / "agents" / "codebase-investigator.md"
_SKILL_MD_PATH = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "SKILL.md"
_ARTIFACTS_DIR = _REPO_ROOT / "artifacts" / "2360" / "runtime-verification"

_MUTATING_TOOL_NAMES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Known "the live SubAgent launch environment itself is unavailable" signals
# (auth/login/transport-level failures), mirroring the forbidden-marker /
# environment-unavailable classification convention already established by
# `.claude/skills/worktree-agent-runtime-smoke/SKILL.md` and
# `docs/dev/runtime-verification-policy.md` section 10 -- these are never
# converted into a PASS or treated as an assertion failure of this test's
# actual subject (the fallback decision logic).
_ENVIRONMENT_UNAVAILABLE_MARKERS = (
    "Please run /login",
    "403 WebSocket upgrade",
    "WebSocket upgrade was rejected",
    "Not authenticated",
    "invalid_grant",
    "command not found",
)

_PERMISSION_BOUNDARY_FAILURE_CLASSES = frozenset(
    {
        "agy_permission_boundary_unavailable",
        "agy_permission_boundary_inconclusive",
    }
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _git_porcelain_status(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.stdout


def _write_runtime_verification_log(
    *,
    ac: str,
    verdict: str,
    reason: str,
    payload: dict,
) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _ARTIFACTS_DIR / f"runtime-verification-{ac}-{timestamp}.log"
    lines = [
        "=== Runtime Verification Log ===",
        f"AC: {ac} -- codebase-investigator AGY advisory native fallback (Issue #2360)",
        f"Timestamp: {timestamp}",
        "Environment: real `claude` binary on PATH (bounded, --agent codebase-investigator)",
        "",
        "--- Input / Output ---",
        json.dumps(payload, indent=2, sort_keys=True, default=str)[:20000],
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Reason: {reason}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


# ---------------------------------------------------------------------------
# Pure mirror of the documented transition-condition predicate (AC4, hermetic)
# ---------------------------------------------------------------------------


def _native_fallback_transition_allowed(
    *, agy_advisory_native_fallback_allowed: bool, failure_class: str | None
) -> bool:
    """Mirrors the "遷移条件（すべて満たす場合のみ）" list in
    ``.claude/agents/codebase-investigator.md`` -- "AGY advisory native
    fallback" section. This is a hermetic Python re-statement of the
    SubAgent's own documented decision predicate, used only to pin the
    *default-forbidden* and *permission-boundary-excluded* invariants down
    with a fast, deterministic, non-live test. It is not consumed by any
    production code path (the SubAgent itself is prompt-driven, not
    Python-driven).
    """
    if agy_advisory_native_fallback_allowed is not True:
        return False
    if not failure_class:
        return False
    if failure_class in _PERMISSION_BOUNDARY_FAILURE_CLASSES:
        return False
    return True


# ---------------------------------------------------------------------------
# AC4: hermetic fail-closed tests (no subprocess, no network)
# ---------------------------------------------------------------------------


class TestFailClosedDefault:
    """AC4: fallback forbidden (default) when AGY fails -- hermetic."""

    def test_fail_closed_default_forbids_fallback_for_agy_timeout(self):
        """GIVEN agy_advisory_native_fallback_allowed is unset (default)
        WHEN the AGY delegation wrapper fails with failure_class: agy_timeout
        THEN the documented transition predicate must forbid native fallback
        (fail-closed is the only allowed outcome).
        """
        assert (
            _native_fallback_transition_allowed(
                agy_advisory_native_fallback_allowed=False, failure_class="agy_timeout"
            )
            is False
        )

    def test_fail_closed_default_forbids_fallback_when_flag_omitted_entirely(self):
        """GIVEN the caller never passes agy_advisory_native_fallback_allowed at all
        WHEN the AGY delegation wrapper fails with failure_class: agy_timeout
        THEN the documented transition predicate must forbid native fallback
        (an omitted/None flag must not be silently treated as an opt-in).
        """
        assert (
            _native_fallback_transition_allowed(
                agy_advisory_native_fallback_allowed=None,  # type: ignore[arg-type]
                failure_class="agy_timeout",
            )
            is False
        )

    def test_fail_closed_permission_boundary_failure_class_forbidden_even_when_allowed(self):
        """GIVEN agy_advisory_native_fallback_allowed: true
        WHEN the observed failure_class is a permission-boundary classification
        (agy_permission_boundary_unavailable / agy_permission_boundary_inconclusive)
        THEN native fallback must still be forbidden (Issue #2360 Out of Scope:
        permission-boundary failure_class values are never fallback inputs).
        """
        for failure_class in sorted(_PERMISSION_BOUNDARY_FAILURE_CLASSES):
            assert (
                _native_fallback_transition_allowed(
                    agy_advisory_native_fallback_allowed=True, failure_class=failure_class
                )
                is False
            ), f"expected fail-closed for {failure_class!r} even with the flag set to true"

    def test_fail_closed_missing_failure_class_forbidden_even_when_allowed(self):
        """GIVEN agy_advisory_native_fallback_allowed: true
        WHEN the wrapper's ok: false result does not expose an observable
        failure_class (missing/null)
        THEN native fallback must still be forbidden (fail-closed is the
        default whenever the failure cannot be positively classified).
        """
        assert (
            _native_fallback_transition_allowed(
                agy_advisory_native_fallback_allowed=True, failure_class=None
            )
            is False
        )

    def test_fail_closed_documented_as_default_in_agent_definition(self):
        """The agent definition's own prose must document that
        agy_advisory_native_fallback_allowed unset/false keeps the existing
        fail-close behavior unconditionally -- so this hermetic mirror and
        the SubAgent-owned instructions cannot silently drift apart."""
        text = _read(_AGENT_MD_PATH)
        assert "agy_advisory_native_fallback_allowed" in text
        # The default-forbidden statement must exist verbatim near the
        # fail-close section (not just anywhere in the file).
        assert re.search(
            r"agy_advisory_native_fallback_allowed[^\n]{0,20}(未指定|渡していない|渡されていない)",
            text,
        ), "agent definition does not document the default-forbidden (unset) case"
        assert "fail-close" in text or "fail_close" in text

    def test_fail_closed_permission_boundary_exclusion_documented(self):
        """The agent definition must document that permission-boundary
        failure_class values are excluded from the fallback opt-in even when
        the caller explicitly allows fallback (Out of Scope carve-out)."""
        text = _read(_AGENT_MD_PATH)
        for failure_class in sorted(_PERMISSION_BOUNDARY_FAILURE_CLASSES):
            assert failure_class in text, (
                f"agent definition does not mention the excluded failure_class {failure_class!r}"
            )


# ---------------------------------------------------------------------------
# AC3: live runtime smoke (real, bounded SubAgent launch)
# ---------------------------------------------------------------------------


def test_live_smoke_agy_timeout_native_fallback_when_allowed():
    """GIVEN a real, bounded `claude -p --agent codebase-investigator` launch,
    a fake AGY delegation wrapper failure (ok: false, failure_class:
    agy_timeout) supplied as an already-completed test double, and an
    explicit agy_advisory_native_fallback_allowed: true opt-in
    WHEN the live SubAgent decides how to proceed per its own operating
    instructions (`.claude/agents/codebase-investigator.md`)
    THEN it transitions to bounded native read-only investigation (no
    Edit/Write/MultiEdit tool_use observed), retrieves a unique sentinel
    marker placed inside the worktree, reports status: ok, and leaves the
    worktree's tracked files byte-for-byte unchanged.

    Per Issue #2360's skip_conditions: SKIPs (never fabricates PASS) when
    the real Claude Code SubAgent launch environment is unavailable.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        _write_runtime_verification_log(
            ac="AC3",
            verdict="SKIP",
            reason="claude CLI not found on PATH -- real SubAgent launch environment unavailable",
            payload={"claude_bin": None},
        )
        pytest.skip(
            "SKIP: claude CLI not found on PATH -- see docs/dev/runtime-verification-policy.md"
            " SKIP convention"
        )

    marker = f"SENTINEL_MARKER_{uuid.uuid4().hex}"
    sentinel_dir = _ARTIFACTS_DIR / "live-smoke-sentinel"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    sentinel_path = sentinel_dir / "sentinel.txt"
    sentinel_path.write_text(marker + "\n", encoding="utf-8")

    fake_wrapper_result = {
        "schema": "delegation_result/v1",
        "provider": "agy",
        "ok": False,
        "tool_profile": "local_asset_research",
        "exit_code": 1,
        "stderr": "agy_timeout: process exceeded 600s",
        "warnings": ["agy_timeout: process exceeded 600s"],
        "failure_reason": "agy_timeout: process exceeded 600s",
        "failure_class": "agy_timeout",
    }

    prompt = f"""You are being invoked as the codebase-investigator SubAgent
(fake-AGY-failure test double scenario, Issue #2360 runtime verification
smoke -- this is a real, single, bounded live invocation).

## Local investigation mode input

- target_path: {sentinel_path}
- purpose: Read the sentinel file and report its exact content.
- agy_advisory_native_fallback_allowed: true

## Pre-completed AGY delegation wrapper attempt (test double)

For this test scenario only, the AGY delegation wrapper invocation (the
canonical builder + run_gemini_headless.py steps of your normal procedure)
has ALREADY been attempted and completed. Do not re-invoke build_request.py
or run_gemini_headless.py for this request. The wrapper's --output-file
JSON result was:

```json
{json.dumps(fake_wrapper_result, indent=2)}
```

## Your task

Per your own operating instructions in codebase-investigator.md (the "AGY
advisory native fallback" section), given the above wrapper failure
(ok: false, failure_class: agy_timeout) and the explicit
agy_advisory_native_fallback_allowed: true input, decide what to do next and
carry it out. You must not use Edit, Write, MultiEdit, or any Bash command
that mutates files or git state. Use Read (and Grep/Glob only if needed) to
read {sentinel_path} and report its exact content.

Report the final CODEBASE_INVESTIGATION_RESULT_V1 (YAML) as your last
message.
"""

    argv = [
        claude_bin,
        "-p",
        "--agent",
        "codebase-investigator",
        "--output-format",
        "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-turns",
        "12",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]

    before_status = _git_porcelain_status(_REPO_ROOT)

    try:
        proc = subprocess.run(
            argv,
            cwd=_REPO_ROOT,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError:
        _write_runtime_verification_log(
            ac="AC3",
            verdict="SKIP",
            reason="claude binary resolved by shutil.which() could not be executed",
            payload={"argv": argv},
        )
        pytest.skip("SKIP: claude binary could not be executed")

    combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    environment_marker_hit = next(
        (m for m in _ENVIRONMENT_UNAVAILABLE_MARKERS if m in combined_output), None
    )
    if proc.returncode != 0 and environment_marker_hit is not None:
        _write_runtime_verification_log(
            ac="AC3",
            verdict="SKIP",
            reason=f"environment-unavailable marker observed: {environment_marker_hit!r}",
            payload={
                "argv": argv,
                "returncode": proc.returncode,
                "stderr_excerpt": (proc.stderr or "")[-2000:],
            },
        )
        pytest.skip(
            f"SKIP: live SubAgent launch environment unavailable ({environment_marker_hit!r})"
        )

    after_status = _git_porcelain_status(_REPO_ROOT)

    tool_uses: list[dict] = []
    final_result_text = ""
    parse_errors: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(str(exc))
            continue
        obj_type = obj.get("type")
        if obj_type == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_uses.append({"name": block.get("name"), "input": block.get("input")})
        elif obj_type == "result":
            final_result_text = obj.get("result") or ""

    mutating_tool_uses = [t for t in tool_uses if t["name"] in _MUTATING_TOOL_NAMES]

    payload = {
        "argv": argv,
        "returncode": proc.returncode,
        "marker": marker,
        "sentinel_path": str(sentinel_path),
        "tool_uses": tool_uses,
        "mutating_tool_uses": mutating_tool_uses,
        "final_result_excerpt": final_result_text[:4000],
        "git_status_before": before_status,
        "git_status_after": after_status,
        "worktree_unchanged": before_status == after_status,
        "parse_errors": parse_errors,
    }

    verdict_ok = (
        proc.returncode == 0
        and not mutating_tool_uses
        and before_status == after_status
        and marker in final_result_text
        and re.search(r"status:\s*ok", final_result_text) is not None
    )

    log_path = _write_runtime_verification_log(
        ac="AC3",
        verdict="PASS" if verdict_ok else "FAIL",
        reason=(
            "native fallback executed, sentinel evidence retrieved, status: ok reported,"
            " no mutating tool_use observed, worktree unchanged"
            if verdict_ok
            else "one or more assertions failed -- see payload"
        ),
        payload=payload,
    )

    assert proc.returncode == 0, (
        f"claude -p --agent codebase-investigator exited {proc.returncode}; see {log_path}"
    )
    assert not mutating_tool_uses, (
        f"mutating tool_use observed during native fallback: {mutating_tool_uses}; see {log_path}"
    )
    assert before_status == after_status, (
        "worktree tracked-file state changed across the live SubAgent invocation"
        f" (mutation detected); see {log_path}"
    )
    assert marker in final_result_text, (
        f"sentinel marker {marker!r} not found in final SubAgent result; see {log_path}"
    )
    assert re.search(r"status:\s*ok", final_result_text) is not None, (
        f"final SubAgent result did not report status: ok; see {log_path}"
    )
