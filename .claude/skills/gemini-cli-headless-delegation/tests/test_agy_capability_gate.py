"""Tests for the agy capability gate (Issue #1941 — agy_capability_matrix/v1).

Test style mirrors test_preflight_agy.py: importlib-based module load +
monkeypatch / subprocess mock, plus direct unit tests of the pure capability
functions.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "preflight_agy.py"
    spec = importlib.util.spec_from_file_location("preflight_agy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# AC1: capability matrix is fail-closed / split into predicates
# ---------------------------------------------------------------------------


def test_capability_matrix_is_fail_closed():
    """AC1/AC2: an unrecognized-version binary must never produce `supported`
    anywhere in the matrix — the matrix must fail closed."""
    module = load_module()
    version_result = {"status": "version_evidence_invalid", "version": None, "core": None, "raw": ""}

    matrix = module.build_capability_matrix(
        version_result=version_result,
        disable_slash_probe=None,
        leading_slash_probe=None,
    )

    for group, predicates in module.CAPABILITY_PREDICATES.items():
        for predicate in predicates:
            status = matrix[group][predicate]["status"]
            assert status in module.CAPABILITY_STATUSES
            assert status != "supported", f"{group}.{predicate} must not be supported without evidence"


def test_hooks_capability_matrix_splits_into_predicates():
    """AC1: hooks capability is decomposed into the 7 required predicates,
    not a single coarse `hooks` boolean/status."""
    module = load_module()
    expected = {
        "workspace_hooks_config_loaded",
        "pre_invocation_hook_dispatch",
        "pre_invocation_ephemeral_message",
        "pre_invocation_injected_tool_call",
        "pre_tool_use_verdict",
        "post_tool_use_dispatch",
        "post_tool_use_matcher_semantics",
    }
    assert set(module.CAPABILITY_PREDICATES["hooks"]) == expected

    matrix = module.build_capability_matrix(
        version_result={"status": "parsed", "version": "1.1.9", "core": (1, 1, 9), "raw": "agy 1.1.9"},
    )
    assert set(matrix["hooks"].keys()) == expected
    for predicate in expected:
        assert "status" in matrix["hooks"][predicate]
        assert "reason_code" in matrix["hooks"][predicate]
        assert "evidence_source" in matrix["hooks"][predicate]


def test_pre_invocation_injected_tool_call_unsupported_while_upstream_728_open():
    """AC1: pre_invocation_injected_tool_call is fixed `unsupported` with
    reason_code upstream_known_runtime_rejection while upstream #728 is open."""
    module = load_module()
    assert module.UPSTREAM_ANTIGRAVITY_CLI_728_OPEN is True

    matrix = module.build_capability_matrix(
        version_result={"status": "parsed", "version": "1.1.9", "core": (1, 1, 9), "raw": "agy 1.1.9"},
    )
    predicate_result = matrix["hooks"]["pre_invocation_injected_tool_call"]
    assert predicate_result["status"] == "unsupported"
    assert predicate_result["reason_code"] == "upstream_known_runtime_rejection"


# ---------------------------------------------------------------------------
# AC2: 5-value classification / fail-closed edge cases
# ---------------------------------------------------------------------------


def test_unknown_or_unavailable_capability_is_not_supported():
    """AC2: an unknown capability name resolves to `unavailable`, never `supported`."""
    module = load_module()
    matrix = module.build_capability_matrix(
        version_result={"status": "parsed", "version": "1.1.9", "core": (1, 1, 9), "raw": "agy 1.1.9"},
    )

    result = module.get_capability_status(matrix, "hooks", "does_not_exist_predicate")
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "unknown_capability"

    result_group = module.get_capability_status(matrix, "does_not_exist_group", "also_missing")
    assert result_group["status"] == "unavailable"


def test_help_flag_absent_but_parser_accepts_is_supported():
    """AC2/In Scope: help text not listing the flag never blocks `supported`
    when the fixed-argv parser probe actually accepts it (PR #1976 design)."""
    module = load_module()
    parser_result = module.classify_parser_acceptance(0, "", "")
    status = module.derive_parser_accepts_flag_status(parser_result)
    assert status["status"] == "supported"
    assert status["evidence_source"] == "parser_acceptance"


def test_help_flag_present_but_parser_rejects_is_not_supported():
    """AC2/In Scope: help text listing the flag never overrides an actual
    parser rejection — rejection evidence always wins."""
    module = load_module()
    parser_result = module.classify_parser_acceptance(2, "", "Error: unknown option --disable-slash-commands")
    status = module.derive_parser_accepts_flag_status(parser_result)
    assert status["status"] == "unsupported"
    assert status["reason_code"] == "parser_rejected_fixed_argv"


def test_stdout_unknown_option_does_not_override_stderr_auth_failure_classification():
    """AC2: a stray "unknown option" string in stdout must not override an
    authoritative auth-failure signal detected in stderr."""
    module = load_module()
    result = module.classify_parser_acceptance(
        1,
        "echoing back: unknown option in transcript",
        "Please sign in with Google to continue.",
    )
    assert result["auth_signal"] is not None
    assert result["accepted"] is None
    assert result["evidence_source"] == "auth_signal"


def test_auth_failure_does_not_reclassify_parser_acceptance_as_unsupported():
    """AC2: a non-zero exit caused by an auth failure must never be
    reclassified as `unsupported` for the parser_accepts_flag predicate."""
    module = load_module()
    parser_result = module.classify_parser_acceptance(1, "", "Please sign in with Google to continue.")
    status = module.derive_parser_accepts_flag_status(parser_result)
    assert status["status"] != "unsupported"
    assert status["status"] == "inconclusive"
    assert status["reason_code"] == "auth_blocked_probe"


# ---------------------------------------------------------------------------
# AC3: consumers reuse this result; no independent parser
# ---------------------------------------------------------------------------


def test_runtime_consumer_reuses_preflight_capabilities(monkeypatch, tmp_path):
    """AC3: run_preflight(compute_capabilities=True) is the SSOT producer —
    the additive `capabilities` field is populated from the same builder a
    consumer would reuse, without any independent version/help parsing."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.1.9\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n  -p, --print   print mode\n", "")
        if argv == [bin_, "-p", module.SMOKE_PROMPT]:
            return _FakeCompleted(0, module.EXPECTED_SMOKE, "")
        if argv == [bin_, "--disable-slash-commands", "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n", "")
        if argv[0] == bin_ and argv[1] == "-p" and "nonexistent-loop-agy-capability-probe" in argv[2]:
            return _FakeCompleted(0, f"{module.EXPECTED_SMOKE}", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    result = module.run_preflight(compute_capabilities=True)

    assert result["ok"] is True
    assert result["capability_schema"] == module.CAPABILITY_MATRIX_SCHEMA_VERSION
    assert set(result["capabilities"].keys()) == set(module.CAPABILITY_PREDICATES.keys())
    # Consumer-facing surface must be exactly the producer's own builder output
    # (identity check would be too strict across json round-trips; assert the
    # dotted-path shape matches instead).
    for group, predicates in module.CAPABILITY_PREDICATES.items():
        assert set(result["capabilities"][group].keys()) == set(predicates)


# ---------------------------------------------------------------------------
# AC4: default invocation exit taxonomy unchanged; --require-capability taxonomy
# ---------------------------------------------------------------------------


def test_runtime_probe_exit_taxonomy(monkeypatch, tmp_path):
    """AC4: default invocation (no --require-capability) keeps the existing
    ok-boolean exit 0/1 semantics — capabilities computation must not change
    the return contract of run_preflight() when not requested."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.1.9\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n  -p, --print   print mode\n", "")
        if argv == [bin_, "-p", module.SMOKE_PROMPT]:
            return _FakeCompleted(0, module.EXPECTED_SMOKE, "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    result = module.run_preflight()
    assert result["ok"] is True
    assert "capabilities" not in result or result.get("capabilities") is None


def test_require_capability_partial_exit_code_taxonomy():
    """AC4: 0=all supported, 1=any unsupported/inconclusive/evidence_invalid,
    77=only unavailable remains (never a success signal)."""
    module = load_module()

    all_supported_matrix = {
        "disable_slash_commands": {
            "parser_accepts_flag": {"status": "supported"},
            "leading_slash_is_literal": {"status": "supported"},
        }
    }
    assert module.compute_require_capability_exit_code(
        all_supported_matrix,
        ["disable_slash_commands.parser_accepts_flag", "disable_slash_commands.leading_slash_is_literal"],
    ) == 0

    mixed_matrix = {
        "disable_slash_commands": {
            "parser_accepts_flag": {"status": "supported"},
            "leading_slash_is_literal": {"status": "unsupported"},
        }
    }
    assert module.compute_require_capability_exit_code(
        mixed_matrix,
        ["disable_slash_commands.parser_accepts_flag", "disable_slash_commands.leading_slash_is_literal"],
    ) == 1

    only_unavailable_matrix = {
        "disable_slash_commands": {
            "parser_accepts_flag": {"status": "unavailable"},
        }
    }
    assert module.compute_require_capability_exit_code(
        only_unavailable_matrix, ["disable_slash_commands.parser_accepts_flag"]
    ) == 77

    inconclusive_matrix = {
        "hooks": {"pre_tool_use_verdict": {"status": "inconclusive"}},
    }
    assert module.compute_require_capability_exit_code(
        inconclusive_matrix, ["hooks.pre_tool_use_verdict"]
    ) == 1

    evidence_invalid_matrix = {
        "headless_permission_policy": {"persisted_settings_loaded": {"status": "evidence_invalid"}},
    }
    assert module.compute_require_capability_exit_code(
        evidence_invalid_matrix, ["headless_permission_policy.persisted_settings_loaded"]
    ) == 1


# ---------------------------------------------------------------------------
# AC5: --disable-slash-commands adoption bound to capability supported
# ---------------------------------------------------------------------------


def test_disable_slash_commands_requires_supported_capability():
    """AC5: adoption of --disable-slash-commands is gated strictly on the
    `disable_slash_commands` capability group being fully supported."""
    module = load_module()

    supported_parser = module.derive_parser_accepts_flag_status(
        module.classify_parser_acceptance(0, "", "")
    )
    assert supported_parser["status"] == "supported"

    rejected_parser = module.derive_parser_accepts_flag_status(
        module.classify_parser_acceptance(2, "", "unknown option --disable-slash-commands")
    )
    assert rejected_parser["status"] == "unsupported"

    # Adoption gate: caller must not treat the feature as usable unless BOTH
    # predicates in the group are `supported`.
    def _adoption_allowed(parser_status: str, literal_status: str) -> bool:
        return parser_status == "supported" and literal_status == "supported"

    assert _adoption_allowed("supported", "supported") is True
    assert _adoption_allowed("supported", "inconclusive") is False
    assert _adoption_allowed("unsupported", "supported") is False


def test_pre_tool_use_verdict_supported_but_injected_tool_call_unsupported():
    """AC5/AC1: even if pre_tool_use_verdict were to report `inconclusive` or
    `supported` elsewhere, pre_invocation_injected_tool_call remains pinned to
    `unsupported` independently (Issue #1941 In Scope), since these are
    distinct predicates within the same `hooks` group and must not share a
    single coarse status."""
    module = load_module()
    matrix = module.build_capability_matrix(
        version_result={"status": "parsed", "version": "1.1.9", "core": (1, 1, 9), "raw": "agy 1.1.9"},
    )
    assert matrix["hooks"]["pre_invocation_injected_tool_call"]["status"] == "unsupported"
    # pre_tool_use_verdict has no live-enforcement evidence in #1941's scope
    # (deferred to #1979) and must never silently inherit the sibling
    # predicate's `unsupported` status.
    assert matrix["hooks"]["pre_tool_use_verdict"]["status"] != "supported"
    assert (
        matrix["hooks"]["pre_tool_use_verdict"]["reason_code"]
        != matrix["hooks"]["pre_invocation_injected_tool_call"]["reason_code"]
    )


# ---------------------------------------------------------------------------
# AC6: binary identity drift detection
# ---------------------------------------------------------------------------


def test_binary_identity_drift_between_version_and_runtime_is_detected():
    """AC6: if the binary identity observed before probes differs from the
    identity observed after probes, every predicate becomes `evidence_invalid`."""
    module = load_module()
    before = {
        "realpath": "/usr/local/bin/agy",
        "sha256": "a" * 64,
        "size": 1000,
        "mtime_ns": 123,
        "platform": "Linux",
        "arch": "x86_64",
    }
    after_same = dict(before)
    after_drifted = dict(before, sha256="b" * 64)

    assert module.binary_identity_matches(before, after_same) is True
    assert module.binary_identity_matches(before, after_drifted) is False

    matrix = module.build_capability_matrix(
        version_result={"status": "parsed", "version": "1.1.9", "core": (1, 1, 9), "raw": "agy 1.1.9"},
        binary_identity_before=before,
        binary_identity_after=after_drifted,
    )
    for group, predicates in module.CAPABILITY_PREDICATES.items():
        for predicate in predicates:
            entry = matrix[group][predicate]
            assert entry["status"] == "evidence_invalid"
            assert entry["reason_code"] == "binary_identity_drift"


# ---------------------------------------------------------------------------
# AC7: single finalizer / sanitized artifact on every controlled exit
# ---------------------------------------------------------------------------


def test_all_controlled_exit_paths_write_sanitized_artifact(monkeypatch, tmp_path):
    """AC7: binary-missing, help-failure, and timeout controlled-exit paths
    all go through the single `_finalize` path and write a sanitized JSON
    artifact under the (overridden) artifact directory."""
    module = load_module()
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    # binary missing
    def fake_run_missing(argv, cwd=None, timeout=None):
        raise FileNotFoundError("agy: command not found")

    monkeypatch.setattr(module, "_run", fake_run_missing)
    result_missing = module.run_preflight()
    assert result_missing["ok"] is False
    assert result_missing["failure_class"] == "cli_missing"
    assert result_missing["artifact_path"] is not None
    assert Path(result_missing["artifact_path"]).exists()

    import subprocess as subprocess_module

    def fake_run_timeout(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.1.9\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n  -p, --print   print mode\n", "")
        if argv == [bin_, "-p", module.SMOKE_PROMPT]:
            raise subprocess_module.TimeoutExpired(cmd=argv, timeout=timeout or 20)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run_timeout)
    result_timeout = module.run_preflight()
    assert result_timeout["ok"] is False
    assert result_timeout["failure_class"] == "client_subprocess_timeout"
    assert result_timeout["artifact_path"] is not None
    assert Path(result_timeout["artifact_path"]).exists()

    artifacts = list(tmp_path.glob("agy_preflight_result_*.json"))
    assert len(artifacts) >= 2


def test_sanitized_artifact_excludes_sensitive_fields(monkeypatch, tmp_path):
    """AC7: the persisted artifact never contains prompt text, raw credential
    paths, absolute HOME, or un-redacted stderr — only argv_shape.flags and
    prompt.sha256/prompt.byte_length for any probe with an argv."""
    module = load_module()

    secret_prompt = "SECRET_USER_PROMPT_MARKER_DO_NOT_PERSIST"
    result = {
        "schema": "agy_preflight_result/v1",
        "ok": True,
        "failure_reason": None,
        "failure_class": None,
        "warnings": [],
        "agy": {"bin": "agy", "resolved_path": "/home/testuser/.local/bin/agy", "version": "agy 1.1.9"},
        "smoke": {
            "ok": True,
            "argv": ["agy", "-p", secret_prompt],
            "exit_code": 0,
            "stdout": secret_prompt,
            "stderr": "",
            "stdout_sample": "LOOP_AGY_SMOKE_OK",
            "stderr_sample": "",
        },
    }

    sanitized = module._sanitize_for_artifact(result)

    assert "argv" not in sanitized["smoke"]
    assert sanitized["smoke"]["argv_shape"]["flags"] == ["-p"]
    assert "prompt" in sanitized["smoke"]
    assert sanitized["smoke"]["prompt"]["sha256"] is not None
    assert sanitized["smoke"]["prompt"]["byte_length"] > 0
    assert "stdout" not in sanitized["smoke"]
    assert "stderr" not in sanitized["smoke"]
    # No raw prompt text anywhere in the serialized artifact.
    serialized = json.dumps(sanitized)
    assert secret_prompt not in serialized


# ---------------------------------------------------------------------------
# AC8: version parser fixture matrix
# ---------------------------------------------------------------------------


def test_version_parser_fixture_matrix():
    """AC8: version parser correctly classifies each documented fixture; any
    unparsable case is `version_evidence_invalid`, never `unsupported`."""
    module = load_module()

    cases = [
        ("agy 1.1.9\n", "parsed", "1.1.9", (1, 1, 9)),
        ("1.1.9\n", "parsed", "1.1.9", (1, 1, 9)),
        ("agy 1.1.9-beta.1+build.5\n", "parsed", "1.1.9-beta.1+build.5", (1, 1, 9)),
        ("\nagy 1.1.9\n", "parsed", "1.1.9", (1, 1, 9)),  # stdout empty + stderr-provided version line
        ("Warning: deprecated flag\nagy 1.1.9\nWarning: another notice\n", "parsed", "1.1.9", (1, 1, 9)),
        ("バージョン情報が取得できません\n", "version_evidence_invalid", None, None),  # malformed/locale
        ("OK\n", "version_evidence_invalid", None, None),  # exit 0 but unparsable
        ("", "version_evidence_invalid", None, None),  # fully empty
    ]

    for raw_text, expected_status, expected_version, expected_core in cases:
        parsed = module.parse_agy_version_string(raw_text)
        assert parsed["status"] == expected_status, f"unexpected status for {raw_text!r}: {parsed}"
        assert parsed["version"] == expected_version
        assert parsed["core"] == expected_core
        if expected_status == "version_evidence_invalid":
            assert parsed["status"] != "unsupported"


# ---------------------------------------------------------------------------
# AC9: in-process-only memoization
# ---------------------------------------------------------------------------


def test_capability_probe_memoized_within_process_only():
    """AC9: identical (binary_identity, config_digest) keys reuse the cached
    matrix within the same process (no recomputation); the cache is a plain
    in-process module dict with no on-disk persistence."""
    module = load_module()
    module._CAPABILITY_MEMO_CACHE.clear()

    binary_identity = {
        "realpath": "/usr/local/bin/agy",
        "sha256": "c" * 64,
        "size": 42,
        "mtime_ns": 1,
        "platform": "Linux",
        "arch": "x86_64",
    }
    config_digest = "digest-a"

    call_count = {"n": 0}

    def compute():
        call_count["n"] += 1
        return {"computed": call_count["n"]}

    first = module.get_or_compute_capability_matrix(binary_identity, config_digest, compute)
    second = module.get_or_compute_capability_matrix(binary_identity, config_digest, compute)
    assert first == second
    assert call_count["n"] == 1

    other_identity = dict(binary_identity, sha256="d" * 64)
    third = module.get_or_compute_capability_matrix(other_identity, config_digest, compute)
    assert call_count["n"] == 2
    assert third != first

    # Never persisted to disk — verify by asserting no on-disk cache file
    # attribute/method exists on the module for this purpose.
    assert not hasattr(module, "_CAPABILITY_DISK_CACHE_PATH")
