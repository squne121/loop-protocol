"""Tests for the agy invocation argv positional structure allowlist.

Issue #1807 (AC9 permission-bypass-flag-rejection, follow-up of #1265):
`run_gemini_headless.py` builds the agy execution argv from a single
canonical function (`_build_agy_inner_argv()`), and validates it with
`_validate_agy_invocation_argv()` -- a position-based structure allowlist --
before it is ever passed to `subprocess.run()`. This is a fail-closed
defense-in-depth check against permission-bypass flags (e.g.
`--dangerously-skip-permissions`, confirmed to exist via live `agy --help`
output in `references/agy-headless-tool-use-investigation.md`) reaching the
agy subprocess through an unapproved trailing argv option, and against any
*future* unknown flag as well (it is a structural allowlist, not a denylist
keyed on today's known flag names).

Issue #1807 fix_delta (OWNER REQUEST_CHANGES, PR #1816,
https://github.com/squne121/loop-protocol/pull/1816#issuecomment-5090289331):
Blocker 1 -- `raw_command` must always be derived from the exact argv that
was validated and executed (including `--model`), never reconstructed
separately; Blocker 2 -- a rejected argv's option values must never be
echoed anywhere in the result (`stderr` / `warnings` / `failure_reason` /
`raw_command`); Medium 1 -- the `--model` value is checked against the
resolver's approved model chain, not merely syntactic validity; Medium 2 --
composition-level (not just per-function) integration coverage.

Test style mirrors test_agy_provider.py: importlib-based module load +
`unittest.mock.patch("subprocess.run", ...)` to avoid requiring the real
`agy` CLI in the test environment.
"""

from __future__ import annotations

import importlib.util
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _write_valid_hook_event_for_subprocess_env(kwargs: dict[str, Any], tool_name: str = "search_web") -> None:
    """Append a validated `agy_tool_provenance_v1` PreToolUse hook event line
    to the isolated-workspace hook events log file that this real
    `_run_agy()` invocation's `env` points at (Issue #2038 fix_delta
    iteration 2: the legacy stdout/marker parser now requires this
    corroboration before resolving `grounding_status == "grounded"`) --
    mirrors test_agy_provider.py's helper of the same name."""
    import hashlib
    import json

    env = kwargs.get("env") or {}
    hook_log_path = env.get("AGY_PROVENANCE_HOOK_LOG_PATH")
    if not hook_log_path:
        return
    path = Path(hook_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema": "agy_tool_provenance_v1",
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {
            "name": tool_name,
            "args_sha256": hashlib.sha256(b'{"query":"test"}').hexdigest(),
        },
        "conversationId": "conv-2038-fix-delta-test",
        "monotonic_ns": 1,
        "utc": "2026-08-09T00:00:00.000000Z",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _agy_request(**kwargs: Any) -> dict[str, Any]:
    """Return a minimal valid agy delegation request (mirrors
    test_agy_provider.py's helper of the same name)."""
    base = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
        "objective": "Smoke test for agy invocation argv allowlist integration",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# AC1: _validate_agy_invocation_argv() exists and enforces the positional
# structure allowlist directly (pure-function unit coverage).
# ---------------------------------------------------------------------------


def test_validate_agy_invocation_argv_exists() -> None:
    """AC1: the validator function is defined on the module."""
    assert hasattr(rgh, "_validate_agy_invocation_argv")
    assert callable(rgh._validate_agy_invocation_argv)


def test_build_agy_inner_argv_shapes() -> None:
    """AC1: `_build_agy_inner_argv()` produces the exact `[-p, prompt]` /
    `[-p, prompt, --model, model]` shapes the rest of the allowlist relies
    on. (Renamed from `test_build_agy_inner_argv_is_single_canonical_source`
    -- Issue #1807 fix_delta Medium 2: that name overstated what this test
    actually proves. It is *not*, on its own, proof that the real execution
    argv and the audit-display `raw_command` can never diverge -- that
    stronger claim now requires the actually-executed argv, which is why
    `raw_command` is derived by `_sanitize_agy_argv_for_audit()` from the
    validated argv itself (see
    `test_sanitize_agy_argv_for_audit_derives_from_validated_argv` and the
    `test_run_agy_raw_command_reflects_selected_model_end_to_end`
    composition test below), not solely from sharing this builder.)"""
    exec_argv = rgh._build_agy_inner_argv("agy", "hello world")
    assert exec_argv == ["agy", "-p", "hello world"]
    exec_argv_with_model = rgh._build_agy_inner_argv("agy", "hello world", "claude-sonnet-4-6")
    assert exec_argv_with_model == ["agy", "-p", "hello world", "--model", "claude-sonnet-4-6"]
    # `_build_agy_raw_command()` is a placeholder fallback only, retained for
    # request-validation failures that never reach a real argv -- it shares
    # the same builder shape, but is NOT the audit path used once a real
    # invocation argv exists (see `_get_agy_audit_raw_command()`).
    audit_argv = rgh._build_agy_raw_command("")
    assert audit_argv == rgh._build_agy_inner_argv("agy", "<prompt>")


def test_sanitize_agy_argv_for_audit_derives_from_validated_argv() -> None:
    """Issue #1807 fix_delta Blocker 1: `_sanitize_agy_argv_for_audit()`
    derives its output from the *exact* argv passed in -- including a
    `--model` flag -- rather than reconstructing a placeholder."""
    validated = rgh._build_agy_inner_argv("/usr/local/bin/agy", "some secret-ish prompt", "claude-sonnet-4-6")
    sanitized = rgh._sanitize_agy_argv_for_audit(validated)
    assert sanitized == ["agy", "-p", "<prompt>", "--model", "claude-sonnet-4-6"]
    # No-model shape is preserved too.
    validated_no_model = rgh._build_agy_inner_argv("agy", "some prompt")
    assert rgh._sanitize_agy_argv_for_audit(validated_no_model) == ["agy", "-p", "<prompt>"]


# ---------------------------------------------------------------------------
# AC2: test_permission_bypass_allowed
# Approved-structure argv (no flags, or --model only) passes the allowlist
# and agy invocation proceeds/succeeds as before.
# ---------------------------------------------------------------------------


def test_permission_bypass_allowed() -> None:
    """AC2: approved-structure argv (no trailing flags) passes validation,
    and `_run_agy()` still invokes agy successfully (no regression)."""
    approved_argv = rgh._build_agy_inner_argv("agy", "hello world")
    # Must not raise.
    rgh._validate_agy_invocation_argv(approved_argv)

    approved_with_model = rgh._build_agy_inner_argv("agy", "hello world", "claude-sonnet-4-6")
    rgh._validate_agy_invocation_argv(approved_with_model)

    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")

    with patch("subprocess.run", side_effect=mock_run):
        completed = rgh._run_agy("hello world", 30)

    assert completed.returncode == 0
    assert completed.stdout == "LOOP_AGY_SMOKE_OK"
    assert captured_cmd == ["agy", "-p", "hello world"]


# ---------------------------------------------------------------------------
# AC3: test_permission_bypass_rejected
# An argv with an unknown trailing option (--dangerously-skip-permissions)
# is rejected as a structure allowlist violation.
# ---------------------------------------------------------------------------


def test_permission_bypass_rejected() -> None:
    """AC3: an argv with `--dangerously-skip-permissions` mixed into the
    trailing options is rejected by `_validate_agy_invocation_argv()`."""
    bypass_argv = ["agy", "-p", "hello world", "--dangerously-skip-permissions"]
    try:
        rgh._validate_agy_invocation_argv(bypass_argv)
        raise AssertionError("expected AgyInvocationPolicyError to be raised")
    except rgh.AgyInvocationPolicyError:
        pass

    # Also reject the bypass flag appended after an otherwise-approved
    # --model clause (structure allowlist, not a substring/last-token check).
    bypass_with_model = [
        "agy",
        "-p",
        "hello world",
        "--model",
        "claude-sonnet-4-6",
        "--dangerously-skip-permissions",
    ]
    try:
        rgh._validate_agy_invocation_argv(bypass_with_model)
        raise AssertionError("expected AgyInvocationPolicyError to be raised")
    except rgh.AgyInvocationPolicyError:
        pass

    # And reject any other unrecognized trailing option, not just the one
    # known-real flag name (this is a structural allowlist, not a denylist).
    unknown_flag_argv = ["agy", "-p", "hello world", "--some-future-unknown-flag"]
    try:
        rgh._validate_agy_invocation_argv(unknown_flag_argv)
        raise AssertionError("expected AgyInvocationPolicyError to be raised")
    except rgh.AgyInvocationPolicyError:
        pass


def test_permission_bypass_flag_never_reaches_subprocess_run() -> None:
    """AC3: end-to-end -- if a permission-bypass flag were ever injected into
    `_run_agy()`'s command construction, the fail-closed guard raises before
    `subprocess.run()` is called (rather than silently executing it)."""
    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="should not be reached")

    def poisoned_builder(
        agy_bin: str, prompt: str, model: str | None = None, *, output_format: str | None = None
    ) -> list[str]:
        # Issue #2038 P0-1 fix_delta: _run_agy() now always calls
        # _build_agy_inner_argv() with an explicit output_format= keyword
        # (None for non-grounded_research profiles) -- this test double's
        # signature must accept it too, or the poisoning itself would raise
        # a TypeError instead of exercising the intended positional-allowlist
        # rejection path.
        return [agy_bin, "-p", prompt, "--dangerously-skip-permissions"]

    with patch.object(rgh, "_build_agy_inner_argv", side_effect=poisoned_builder):
        with patch("subprocess.run", side_effect=mock_run):
            try:
                rgh._run_agy("hello world", 30)
                raise AssertionError("expected AgyInvocationPolicyError to be raised")
            except rgh.AgyInvocationPolicyError:
                pass

    assert captured_cmd == [], "subprocess.run() must not be invoked when the argv is rejected"


# ---------------------------------------------------------------------------
# AC4: run_delegation() classifies the rejection into the new
# `agy_invocation_policy_denied` failure_class (distinct from
# `agy_permission_denied`), non-retryable.
# ---------------------------------------------------------------------------


def test_run_delegation_classifies_invocation_policy_denied() -> None:
    """AC4: when `_run_agy()` raises `AgyInvocationPolicyError`,
    `run_delegation()` returns `failure_class: agy_invocation_policy_denied`,
    not `agy_permission_denied` or `agy_unexpected_error`."""
    request = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
        "objective": "Smoke test for agy invocation policy denial classification",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
    }

    def raising_run_agy(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        raise rgh.AgyInvocationPolicyError("agy invocation argv trailing options must be empty or --model only")

    with patch.object(rgh, "_run_agy", side_effect=raising_run_agy):
        result = rgh.run_delegation(request)

    assert result["ok"] is False
    assert result["failure_class"] == "agy_invocation_policy_denied"
    assert result["failure_class"] != "agy_permission_denied"
    assert result["failure_class"] != "agy_unexpected_error"
    assert "agy_invocation_policy_denied" in (result["failure_reason"] or "")


# ---------------------------------------------------------------------------
# Issue #1807 fix_delta Medium 1: the --model value must be checked against
# the resolver's approved model chain, not merely be a syntactically-valid
# token.
# ---------------------------------------------------------------------------


def test_validate_agy_invocation_argv_rejects_model_outside_approved_set() -> None:
    """Medium 1: a syntactically-valid but unapproved --model value is
    rejected when an approved_models set is supplied."""
    approved = frozenset({"claude-sonnet-4-6", "claude-opus-4-2"})
    approved_argv = ["agy", "-p", "hello world", "--model", "claude-sonnet-4-6"]
    # Must not raise: value is in the approved set.
    rgh._validate_agy_invocation_argv(approved_argv, approved_models=approved)

    unapproved_argv = ["agy", "-p", "hello world", "--model", "some-unapproved-model-xyz"]
    try:
        rgh._validate_agy_invocation_argv(unapproved_argv, approved_models=approved)
        raise AssertionError("expected AgyInvocationPolicyError to be raised")
    except rgh.AgyInvocationPolicyError as exc:
        assert "some-unapproved-model-xyz" not in str(exc)

    # approved_models=None (default) preserves the pre-fix_delta
    # syntactic-only behavior -- no regression for callers that never pass it.
    rgh._validate_agy_invocation_argv(unapproved_argv)


def test_run_agy_grounded_research_passes_approved_model_chain() -> None:
    """Medium 1: `_run_agy()` (grounded_research profile) passes the
    resolver's full model_chain as approved_models to
    `_validate_agy_invocation_argv()`, not None."""
    captured: dict[str, Any] = {}
    real_validate = rgh._validate_agy_invocation_argv

    def spy_validate(argv: list[str], *, approved_models: Any = None) -> None:
        captured["approved_models"] = approved_models
        real_validate(argv, approved_models=approved_models)

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return _make_completed(0, stdout="ok")

    token = rgh._AGY_TOOL_PROFILE_CTX.set("grounded_research")
    try:
        with patch.object(rgh, "_validate_agy_invocation_argv", side_effect=spy_validate):
            with patch("subprocess.run", side_effect=mock_run):
                rgh._run_agy("test prompt", 30)
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    expected_chain, error = rgh.resolve_model_chain({"role": "grounded_research"})
    assert error is None
    assert captured["approved_models"] == frozenset(expected_chain)


# ---------------------------------------------------------------------------
# Issue #1807 fix_delta Medium 2: composition-level integration coverage
# (not just per-function unit tests).
# ---------------------------------------------------------------------------


def test_run_agy_raw_command_reflects_selected_model_end_to_end() -> None:
    """Medium 2 (1): the real execution argv (with --model) and the sanitized
    `raw_command` published for audit are derived from the same validated
    argv -- Blocker 1 end-to-end, through `_run_agy()` -> `run_delegation()`."""
    captured_cmd: dict[str, Any] = {"value": None}
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd["value"] = list(cmd)
        # Issue #2038 fix_delta iteration 2: a validated hook event is now
        # required to reach grounding_status "grounded" via this real
        # _run_agy() -> subprocess.run() path.
        _write_valid_hook_event_for_subprocess_env(kwargs)
        return _make_completed(0, stdout=grounded_output)

    with patch("subprocess.run", side_effect=mock_run):
        result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))

    assert result["ok"] is True
    exec_cmd = captured_cmd["value"]
    assert exec_cmd is not None
    assert "--model" in exec_cmd
    model_index = exec_cmd.index("--model")
    expected_model = exec_cmd[model_index + 1]

    # The published raw_command must include the SAME --model value that was
    # actually executed -- not a placeholder reconstruction that dropped it.
    assert result["raw_command"] == ["agy", "-p", "<prompt>", "--model", expected_model]


def test_poisoned_builder_run_through_run_delegation_blocks_subprocess_and_classifies() -> None:
    """Medium 2 (2)+(3): a poisoned `_build_agy_inner_argv()` driven all the
    way through `run_delegation()` (not just `_run_agy()` directly) never
    reaches `subprocess.run()`, and is classified as
    `agy_invocation_policy_denied`."""
    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="should not be reached")

    def poisoned_builder(
        agy_bin: str, prompt: str, model: str | None = None, *, output_format: str | None = None
    ) -> list[str]:
        # Issue #2038 P0-1 fix_delta: _run_agy() now always calls
        # _build_agy_inner_argv() with an explicit output_format= keyword
        # (None for non-grounded_research profiles) -- this test double's
        # signature must accept it too, or the poisoning itself would raise
        # a TypeError instead of exercising the intended positional-allowlist
        # rejection path.
        return [agy_bin, "-p", prompt, "--dangerously-skip-permissions"]

    with patch.object(rgh, "_build_agy_inner_argv", side_effect=poisoned_builder):
        with patch("subprocess.run", side_effect=mock_run):
            result = rgh.run_delegation(_agy_request())

    assert captured_cmd == [], "subprocess.run() must not be invoked when the argv is rejected"
    assert result["ok"] is False
    assert result["failure_class"] == "agy_invocation_policy_denied"


def test_policy_denial_does_not_expose_rejected_argument_values() -> None:
    """Medium 2 (4) / Blocker 2: a rejected argv's option values never
    appear anywhere in the result -- `stderr`, `failure_reason`, any
    `warnings` entry, or `raw_command` -- even for a hypothetical future
    builder defect that smuggled a secret-bearing option in."""
    secret = "sk-private-value"

    def poisoned_builder(
        agy_bin: str, prompt: str, model: str | None = None, *, output_format: str | None = None
    ) -> list[str]:
        # Issue #2038 P0-1 fix_delta: _run_agy() now always calls
        # _build_agy_inner_argv() with an explicit output_format= keyword
        # (None for non-grounded_research profiles) -- this test double's
        # signature must accept it too, or the poisoning itself would raise
        # a TypeError instead of exercising the intended positional-allowlist
        # rejection path.
        return [agy_bin, "-p", prompt, "--api-key", secret]

    with patch.object(rgh, "_build_agy_inner_argv", side_effect=poisoned_builder):
        with patch("subprocess.run", side_effect=AssertionError("subprocess.run must not be called")):
            result = rgh.run_delegation(_agy_request())

    assert result["ok"] is False
    assert result["failure_class"] == "agy_invocation_policy_denied"
    assert secret not in (result["stderr"] or "")
    assert secret not in (result["failure_reason"] or "")
    assert all(secret not in warning for warning in (result["warnings"] or []))
    assert secret not in str(result["raw_command"])
    assert secret not in str(result)


def test_bwrap_prefix_uses_only_validated_inner_argv(tmp_path: Path) -> None:
    """Medium 2 (5): when the isolated-workspace `bwrap` read-only prefix is
    used, the actual subprocess argv is exactly
    `bwrap_prefix + validated_inner_argv` -- the bwrap wiring never bypasses
    or duplicates the Issue #1807 allowlist-validated inner command."""
    bwrap_prefix = ["bwrap", "--dev-bind", "/", "/", "--tmpfs", "/fake/scratch"]

    fake_workspace = types.SimpleNamespace(
        env={},
        workspace_dir=tmp_path,
        agy_oauth_token_bwrap_prefix=bwrap_prefix,
    )

    captured_cmd: dict[str, Any] = {"value": None}

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd["value"] = list(cmd)
        return _make_completed(0, stdout="ok")

    with patch.object(
        rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
    ):
        with patch("shutil.rmtree"):
            token = rgh._AGY_TOOL_PROFILE_CTX.set("no_tools")
            try:
                with patch("subprocess.run", side_effect=mock_run):
                    rgh._run_agy("hello world", 30)
            finally:
                rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    exec_cmd = captured_cmd["value"]
    assert exec_cmd is not None
    expected_inner_argv = rgh._build_agy_inner_argv("agy", "hello world")
    assert exec_cmd == bwrap_prefix + expected_inner_argv


# ---------------------------------------------------------------------------
# Issue #1928 (implementing the #1918 policy decision): AGY headless prompts
# whose leading token is a slash-command are rejected fail-closed, before
# any invocation argv is built and long before subprocess.run() could run.
# ---------------------------------------------------------------------------

_AGY_REJECT_CLASS_PROMPTS: list[tuple[str, str]] = [
    ("single_leading_slash", "/plan implement X"),
    ("leading_whitespace_plus_slash", "   /plan implement X"),
    ("bom_plus_slash", "\ufeff/plan implement X"),
    ("stacked_leading_slash", "/plan /grill-me implement X"),
    ("unknown_command", "/unknown command"),
    ("workspace_skill_style", "/my-skill do the thing"),
    ("bare_slash_only", "/"),
    ("newline_then_slash", "\n/plan implement X"),
]


def test_agy_prompt_has_leading_slash_command_detects_reject_class() -> None:
    """AC1: `_agy_prompt_has_leading_slash_command()` returns True for every
    reject-class prompt in the #1918 policy decision."""
    for label, prompt in _AGY_REJECT_CLASS_PROMPTS:
        assert rgh._agy_prompt_has_leading_slash_command(prompt) is True, label


def test_run_agy_rejects_leading_slash_command_variants() -> None:
    """AC1: `_run_agy()` raises `AgyInvocationPolicyError` -- and never
    invokes `subprocess.run()` -- for every reject-class prompt (single
    leading slash, leading whitespace/BOM before the slash, stacked leading
    slash commands, unknown command, workspace/global skill-style command,
    a bare `/`, and a leading slash separated by a newline)."""
    for label, prompt in _AGY_REJECT_CLASS_PROMPTS:
        captured_cmd: list[Any] = []

        def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            captured_cmd.extend(cmd)
            return _make_completed(0, stdout="should not be reached")

        with patch("subprocess.run", side_effect=mock_run):
            try:
                rgh._run_agy(prompt, 30)
                raise AssertionError(f"expected AgyInvocationPolicyError to be raised for: {label}")
            except rgh.AgyInvocationPolicyError:
                pass

        assert captured_cmd == [], f"subprocess.run() must not be invoked for reject-class prompt: {label}"


_AGY_ALLOW_CLASS_PROMPTS: list[tuple[str, str]] = [
    ("mid_prompt_slash_mention", "Explain /plan"),
    ("url_with_slash", "Read https://example.invalid/path"),
    ("absolute_path_mention", "The path is /tmp/example"),
    ("fenced_code_block_slash_command", "```\n/permissions\n```\nExplain what this does"),
    ("ordinary_japanese_prompt", "テストを実行して結果を要約してください"),
]


def test_agy_prompt_has_leading_slash_command_allows_allow_class() -> None:
    """AC2: `_agy_prompt_has_leading_slash_command()` returns False for every
    allow-class prompt (slash appears only mid-prompt, in a URL, a path, or a
    fenced code block; ordinary text)."""
    for label, prompt in _AGY_ALLOW_CLASS_PROMPTS:
        assert rgh._agy_prompt_has_leading_slash_command(prompt) is False, label


def test_run_agy_allows_non_leading_slash_prompts() -> None:
    """AC2: `_run_agy()` does not raise `AgyInvocationPolicyError` -- and
    reaches `subprocess.run()` normally -- for every allow-class prompt."""
    for label, prompt in _AGY_ALLOW_CLASS_PROMPTS:
        captured_cmd: list[Any] = []

        def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            captured_cmd.extend(cmd)
            return _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")

        with patch("subprocess.run", side_effect=mock_run):
            completed = rgh._run_agy(prompt, 30)

        assert completed.returncode == 0, label
        assert captured_cmd == ["agy", "-p", prompt], label


def test_run_agy_allows_prompt_with_existing_model_argument_canonical_argv() -> None:
    """AC2: the new leading-slash check does not interfere with the existing
    canonical `[-p, <prompt>, --model, <model>]` argv shape (Issue #1807).
    Exercises `_reject_agy_prompt_leading_slash_command()` composed with
    `_build_agy_inner_argv()` / `_validate_agy_invocation_argv()` directly
    (the same sequence `_run_agy()` runs), without going through
    `_run_agy()`'s isolated-workspace/bwrap materialization, which is
    orthogonal to this check and covered by its own existing tests."""
    prompt = "Explain /plan for this repo"

    # Must not raise: the slash is not in the leading-token position.
    rgh._reject_agy_prompt_leading_slash_command(prompt)

    argv = rgh._build_agy_inner_argv("agy", prompt, "claude-sonnet-4-6")
    rgh._validate_agy_invocation_argv(argv, approved_models=frozenset({"claude-sonnet-4-6"}))
    assert argv == ["agy", "-p", prompt, "--model", "claude-sonnet-4-6"]


def test_run_delegation_rejects_leading_slash_command_as_invocation_policy_denied() -> None:
    """AC1/AC3: end-to-end via `run_delegation()` -- a request whose prompt
    begins with a slash-command is classified into the existing
    `agy_invocation_policy_denied` failure_class (Issue #1807's class, reused
    rather than adding a new one), `subprocess.run()` is never invoked, and
    neither the raw prompt text nor a secret sentinel embedded in it appears
    anywhere in the result's `stderr` / `warnings` / `failure_reason`."""
    secret_sentinel = "LOOP_TEST_SECRET_SENTINEL_1928"
    prompt = f"/plan use token {secret_sentinel} to implement X"
    request = _agy_request(prompt=prompt)

    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="should not be reached")

    with patch("subprocess.run", side_effect=mock_run):
        result = rgh.run_delegation(request)

    assert captured_cmd == [], "subprocess.run() must not be invoked when the prompt is rejected"
    assert result["ok"] is False
    assert result["failure_class"] == "agy_invocation_policy_denied"
    assert result["warnings"][0].startswith("agy_invocation_policy_denied")
    assert (result["failure_reason"] or "").startswith("agy_invocation_policy_denied")

    surfaces = [
        result.get("stderr") or "",
        " ".join(result.get("warnings") or []),
        result.get("failure_reason") or "",
        str(result.get("raw_command")),
    ]
    for surface in surfaces:
        assert prompt not in surface
        assert secret_sentinel not in surface
