"""Runtime verification for Issue #2360's conditional AGY advisory native
fallback (AC3/AC4, `decision: immediate` per `## Runtime Verification
Applicability`).

Two independent, deliberately separate proofs are combined here into a
**composed proof** (not a single end-to-end AGY producer test, per the OWNER
PR #2365 review comment https://github.com/squne121/loop-protocol/pull/2365
#issuecomment-5445622166 -- see "false integration proof" P0 finding):

- ``-k live_smoke`` (AC3, "live consumer smoke"): a real, bounded ``claude -p
  --agent codebase-investigator`` invocation is launched exactly once, using
  ``--permission-mode dontAsk`` (matching the production frontmatter's
  ``permissionMode: dontAsk`` -- not ``bypassPermissions``, which bypasses the
  tool-allow boundary entirely and therefore cannot prove anything about the
  production permission mode). A fake AGY delegation wrapper failure
  (``ok: false``, ``failure_class: agy_timeout`` -- the taxonomy's
  representative non-retryable AGY-side failure, see
  ``.claude/skills/gemini-cli-headless-delegation/references/failure-class-taxonomy.md``)
  is supplied as a pre-completed test double in the task prompt, together
  with an explicit ``agy_advisory_native_fallback_allowed: true`` opt-in.
  This test does **not** exercise the real AGY delegation wrapper /
  ``run_gemini_headless.py`` invocation itself (that producer-side behavior is
  independently covered by the existing ``gemini-cli-headless-delegation``
  wrapper test suite, which is the other half of this composed proof). What
  this test verifies is the **consumer/fallback behavior**: given a
  schema-valid fake failure result and the opt-in flag, does the live
  SubAgent (a) transition to bounded native investigation under the
  non-mutating investigation policy (Read/Grep/Glob plus bounded, read-only
  Bash such as ``git rev-parse`` and hash computation -- no Edit/Write/
  MultiEdit tool_use is ever observed), (b) successfully retrieve a unique
  sentinel marker placed inside the worktree, (c) report a final
  ``CODEBASE_INVESTIGATION_RESULT_V1`` that actually YAML-parses and contains
  all of the schema's fields (``schema_version``, ``status``,
  ``investigation_route``, ``evidence_refs``, ``discovery_summary``,
  ``impact_scope``, ``failure_reason``, ``source_evidence_result``), with an
  ``evidence_refs`` entry carrying non-placeholder verification metadata
  (``commit_sha`` / ``excerpt_sha256`` / ``verification_status`` /
  ``verification_method``) and a ``discovery_summary`` that explicitly
  discloses the fallback route (mentions the observed ``failure_class`` and
  that native fallback was used), and (d) leaves the worktree's tracked
  files' ``git status --porcelain`` output unchanged across the invocation
  (a narrower claim than "byte-for-byte unchanged": this only proves tracked
  file state as reported by git is unchanged, not that no untracked/ignored
  side effects occurred anywhere on disk). Per this Issue's
  ``skip_conditions``, the test SKIPs (never fabricates PASS) when the real
  Claude Code SubAgent launch environment is unavailable (``claude`` missing
  from PATH, or a transport failure whose stdout/stderr matches a known
  environment-unavailable marker such as ``Please run /login``).

- ``-k fail_closed`` (AC4): hermetic (no subprocess, no network) checks that
  (1) a pure mirror of the documented transition-condition predicate in
  ``.claude/agents/codebase-investigator.md`` forbids native fallback
  whenever ``agy_advisory_native_fallback_allowed`` is unset/false --
  regardless of ``failure_class`` -- and forbids it for any
  ``failure_class`` value that indicates a contract/policy/boundary
  violation (permission-boundary classifications, plus
  ``agy_invocation_policy_denied`` / ``request_policy_denied`` /
  ``request_schema_invalid`` / ``github_research_command_denied``) even when
  the flag is ``true``; and (2) the agent definition's own prose documents
  this default fail-closed behavior and each of those excluded
  classifications, so the mirror and the SubAgent-owned prose cannot
  silently drift apart. This is a small negative policy (explicitly listed
  exclusions), not an exhaustive allowlist of every taxonomy value.

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
import yaml

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

# Contract/policy validation failures (request-time or invocation-time policy
# denials) -- distinct from operational (provider outage / timeout / quota)
# failures. Native-fallback-and-report-success would hide a caller-side bug
# for these, so they are excluded from the fallback opt-in even when
# `agy_advisory_native_fallback_allowed: true` (OWNER PR #2365 review,
# P1 finding: "failure_class の predicate が広すぎる"). This is a small
# negative list, not an exhaustive allowlist of the whole taxonomy.
_CONTRACT_POLICY_VIOLATION_FAILURE_CLASSES = frozenset(
    {
        "agy_invocation_policy_denied",
        "request_policy_denied",
        "request_schema_invalid",
        "github_research_command_denied",
    }
)

_FALLBACK_DENIED_FAILURE_CLASSES = (
    _PERMISSION_BOUNDARY_FAILURE_CLASSES | _CONTRACT_POLICY_VIOLATION_FAILURE_CLASSES
)

_REQUIRED_RESULT_FIELDS = (
    "schema_version",
    "status",
    "investigation_route",
    "evidence_refs",
    "discovery_summary",
    "impact_scope",
    "failure_reason",
    "source_evidence_result",
)

_REQUIRED_EVIDENCE_VERIFICATION_FIELDS = (
    "commit_sha",
    "excerpt_sha256",
    "verification_status",
    "verification_method",
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


def _extract_investigation_result(text: str) -> dict:
    """Extracts and YAML-parses the CODEBASE_INVESTIGATION_RESULT_V1 block
    from a live SubAgent's final message text.

    Tries fenced ```yaml (or ```yml) code blocks first, then falls back to
    slicing from the first literal ``CODEBASE_INVESTIGATION_RESULT_V1``
    occurrence to end-of-text. Returns ``{}`` if no candidate YAML-parses
    into a dict carrying at least a ``status`` field (never fabricates a
    result out of unparseable text).
    """
    candidates: list[str] = list(re.findall(r"```ya?ml\n(.*?)```", text, re.DOTALL))
    idx = text.find("CODEBASE_INVESTIGATION_RESULT_V1")
    if idx != -1:
        candidates.append(text[idx:])
    for candidate in candidates:
        try:
            parsed = yaml.safe_load(candidate)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        inner = parsed.get("CODEBASE_INVESTIGATION_RESULT_V1")
        if isinstance(inner, dict):
            return inner
        if "status" in parsed:
            return parsed
    return {}


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
    *default-forbidden* and *contract/policy/boundary-violation-excluded*
    invariants down with a fast, deterministic, non-live test. It is not
    consumed by any production code path (the SubAgent itself is
    prompt-driven, not Python-driven).
    """
    if agy_advisory_native_fallback_allowed is not True:
        return False
    if not failure_class:
        return False
    if failure_class in _FALLBACK_DENIED_FAILURE_CLASSES:
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

    def test_fail_closed_contract_policy_violation_failure_class_forbidden_even_when_allowed(
        self,
    ):
        """GIVEN agy_advisory_native_fallback_allowed: true
        WHEN the observed failure_class indicates a caller-side contract/policy
        violation (agy_invocation_policy_denied / request_policy_denied /
        request_schema_invalid / github_research_command_denied)
        THEN native fallback must still be forbidden -- native-fallback-and-
        report-success would otherwise hide a caller-side bug (OWNER PR #2365
        review P1 finding).
        """
        for failure_class in sorted(_CONTRACT_POLICY_VIOLATION_FAILURE_CLASSES):
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

    def test_fail_closed_contract_policy_violation_exclusion_documented(self):
        """The agent definition must document that contract/policy-violation
        failure_class values (agy_invocation_policy_denied /
        request_policy_denied / request_schema_invalid /
        github_research_command_denied) are excluded from the fallback opt-in
        even when the caller explicitly allows fallback -- so the negative
        policy in the hermetic mirror and the SubAgent-owned prose cannot
        silently drift apart."""
        text = _read(_AGENT_MD_PATH)
        for failure_class in sorted(_CONTRACT_POLICY_VIOLATION_FAILURE_CLASSES):
            assert failure_class in text, (
                f"agent definition does not mention the excluded failure_class {failure_class!r}"
            )

    def test_fail_closed_rule_3_and_fallback_section_do_not_contradict(self):
        """OWNER PR #2365 review P0 finding: Evidence Handling Rule 3 (fail-
        close on wrapper ok: false) and the "AGY advisory native fallback"
        section must be a single, non-contradictory rule -- not two
        independent system-prompt instructions that can silently disagree.
        This is a textual invariant: Rule 3's own prose must reference the
        opt-in flag and defer to the fallback section for eligible failures,
        rather than stating an unconditional prohibition."""
        text = _read(_AGENT_MD_PATH)
        rule_3_match = re.search(
            r"#### Rule 3:.*?(?=\n#### |\Z)", text, re.DOTALL
        )
        assert rule_3_match, "Evidence Handling Rule 3 section not found"
        rule_3_text = rule_3_match.group(0)
        assert "agy_advisory_native_fallback_allowed" in rule_3_text, (
            "Rule 3 must reference agy_advisory_native_fallback_allowed so it "
            "does not silently contradict the AGY advisory native fallback section"
        )
        assert "AGY advisory native fallback" in rule_3_text, (
            "Rule 3 must explicitly defer to the AGY advisory native fallback "
            "section for eligible operational failures"
        )


# ---------------------------------------------------------------------------
# AC3: live consumer smoke (real, bounded SubAgent launch; composed proof --
# see module docstring for what this test does and does not prove)
# ---------------------------------------------------------------------------


def test_live_smoke_consumer_agy_timeout_native_fallback_when_allowed():
    """GIVEN a real, bounded `claude -p --agent codebase-investigator` launch
    (using --permission-mode dontAsk, matching the production frontmatter's
    permissionMode: dontAsk), a fake AGY delegation wrapper failure
    (ok: false, failure_class: agy_timeout) supplied as an already-completed
    test double, and an explicit agy_advisory_native_fallback_allowed: true
    opt-in
    WHEN the live SubAgent decides how to proceed per its own operating
    instructions (`.claude/agents/codebase-investigator.md`)
    THEN it transitions to bounded native investigation under the
    non-mutating investigation policy (no Edit/Write/MultiEdit tool_use
    observed), retrieves a unique sentinel marker placed inside the
    worktree, reports a CODEBASE_INVESTIGATION_RESULT_V1 that YAML-parses
    with all required fields and non-placeholder evidence verification
    metadata plus an explicit fallback disclosure, and leaves the worktree's
    tracked-file git status unchanged.

    This is the "consumer/fallback behavior" half of a composed proof (see
    module docstring); it does not launch the real AGY delegation wrapper
    itself.

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
agy_advisory_native_fallback_allowed: true input, carry out the native
fallback under the non-mutating investigation policy:

1. Use Read to read {sentinel_path} and confirm its exact content.
2. Use Bash to run `git rev-parse HEAD` (read-only, non-mutating) to resolve
   the current commit_sha.
3. Use Bash to run `sha256sum {sentinel_path}` (read-only, non-mutating) to
   compute the excerpt_sha256 of the file you just read.
4. You must not use Edit, Write, MultiEdit, or any Bash command that mutates
   files or git state (no writes, no git add/commit/checkout/reset, etc.).
5. Report the final CODEBASE_INVESTIGATION_RESULT_V1 (YAML) as your last
   message, inside a ```yaml code fence. It MUST include every one of:
   schema_version, status, investigation_route, evidence_refs,
   discovery_summary, impact_scope, failure_reason, source_evidence_result.
   The evidence_refs entry for {sentinel_path} MUST include commit_sha (the
   value from step 2), excerpt_sha256 (the value from step 3),
   verification_status: verified, and verification_method (name the method,
   e.g. native_fallback_bash_git_rev_parse_sha256sum). discovery_summary
   MUST explicitly state that the AGY delegation wrapper failed with
   failure_class: agy_timeout and that you completed this investigation via
   the native fallback route because agy_advisory_native_fallback_allowed
   was true.
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
        "dontAsk",
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

    result_dict = _extract_investigation_result(final_result_text)
    missing_fields = [f for f in _REQUIRED_RESULT_FIELDS if f not in result_dict]

    evidence_refs = result_dict.get("evidence_refs") or []
    evidence_verification_ok = False
    evidence_missing_fields: list[str] = []
    if isinstance(evidence_refs, list) and evidence_refs:
        first_evidence = evidence_refs[0] if isinstance(evidence_refs[0], dict) else {}
        evidence_missing_fields = [
            f
            for f in _REQUIRED_EVIDENCE_VERIFICATION_FIELDS
            if not first_evidence.get(f)
        ]
        evidence_verification_ok = not evidence_missing_fields

    discovery_summary = str(result_dict.get("discovery_summary") or "")
    fallback_disclosed = (
        "agy_timeout" in discovery_summary and "fallback" in discovery_summary.lower()
    )

    payload = {
        "argv": argv,
        "returncode": proc.returncode,
        "marker": marker,
        "sentinel_path": str(sentinel_path),
        "tool_uses": tool_uses,
        "mutating_tool_uses": mutating_tool_uses,
        "final_result_excerpt": final_result_text[:4000],
        "result_dict": result_dict,
        "missing_fields": missing_fields,
        "evidence_missing_fields": evidence_missing_fields,
        "fallback_disclosed": fallback_disclosed,
        "git_status_before": before_status,
        "git_status_after": after_status,
        "tracked_worktree_status_unchanged": before_status == after_status,
        "parse_errors": parse_errors,
    }

    verdict_ok = (
        proc.returncode == 0
        and not mutating_tool_uses
        and before_status == after_status
        and marker in final_result_text
        and not missing_fields
        and result_dict.get("status") == "ok"
        and evidence_verification_ok
        and fallback_disclosed
    )

    log_path = _write_runtime_verification_log(
        ac="AC3",
        verdict="PASS" if verdict_ok else "FAIL",
        reason=(
            "native fallback executed, sentinel evidence retrieved, final"
            " CODEBASE_INVESTIGATION_RESULT_V1 YAML-parsed with all required"
            " fields and verification metadata, fallback disclosed in"
            " discovery_summary, no mutating tool_use observed, tracked"
            " worktree status unchanged"
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
        "tracked worktree status (git status --porcelain) changed across the"
        f" live SubAgent invocation (mutation detected); see {log_path}"
    )
    assert marker in final_result_text, (
        f"sentinel marker {marker!r} not found in final SubAgent result; see {log_path}"
    )
    assert not missing_fields, (
        "final CODEBASE_INVESTIGATION_RESULT_V1 did not YAML-parse with all required"
        f" fields; missing={missing_fields}; parsed={result_dict}; see {log_path}"
    )
    assert result_dict.get("status") == "ok", (
        f"final SubAgent result did not report status: ok (got {result_dict.get('status')!r});"
        f" see {log_path}"
    )
    assert evidence_verification_ok, (
        "evidence_refs entry is missing non-placeholder verification metadata"
        f" ({_REQUIRED_EVIDENCE_VERIFICATION_FIELDS}); missing={evidence_missing_fields};"
        f" evidence_refs={evidence_refs}; see {log_path}"
    )
    assert fallback_disclosed, (
        "discovery_summary did not explicitly disclose the agy_timeout failure_class"
        f" and native fallback route; discovery_summary={discovery_summary!r}; see {log_path}"
    )
