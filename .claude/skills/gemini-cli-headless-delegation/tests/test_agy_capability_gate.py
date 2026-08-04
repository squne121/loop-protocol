"""Tests for the agy capability gate (Issue #1941 — agy_capability_matrix/v1).

Test style mirrors test_preflight_agy.py: importlib-based module load +
monkeypatch / subprocess mock, plus direct unit tests of the pure capability
functions.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_real_agy_binary_on_path(monkeypatch):
    """Force hermetic binary resolution for every test in this file.

    Issue #1941 fix_delta P1-1: `run_preflight()` now resolves the agy binary
    via `shutil.which()` exactly once at the very start. Tests must never
    depend on whether a real `agy` binary happens to be installed on the host
    PATH -- force resolution to fail so `run_preflight()` falls back to the
    raw (mocked) binary name, matching every existing `fake_run` fixture's
    `module._resolve_binary()`-based argv expectations. Individual tests that
    need to exercise real resolution (e.g. the binary-identity drift
    integration test) re-monkeypatch `shutil.which` locally to override this.
    """
    monkeypatch.setattr(shutil, "which", lambda *_a, **_kw: None)


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
        "pre_invocation_ephemeral_message_injection",
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


def test_pre_invocation_ephemeral_message_injection_is_never_hardcoded_unsupported():
    """Issue #1979: unlike `pre_invocation_injected_tool_call`, this predicate
    is NOT tied to upstream #728 (which only breaks toolCall injectSteps) --
    it must never resolve `unsupported` for the `upstream_known_runtime_rejection`
    reason. It resolves `inconclusive` via the generic deferred-to-live-run
    branch instead (no hardcoded claim of `supported` without evidence)."""
    module = load_module()
    assert module.UPSTREAM_ANTIGRAVITY_CLI_728_OPEN is True

    matrix = module.build_capability_matrix(
        version_result={"status": "parsed", "version": "1.1.9", "core": (1, 1, 9), "raw": "agy 1.1.9"},
    )
    predicate_result = matrix["hooks"]["pre_invocation_ephemeral_message_injection"]
    assert predicate_result["status"] != "unsupported"
    assert predicate_result["reason_code"] != "upstream_known_runtime_rejection"


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
    """AC9: identical (binary_identity_before, binary_identity_check,
    config_digest) keys reuse the cached evidence bundle within the same
    process (no recomputation); the cache is a plain in-process module dict
    with no on-disk persistence."""
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

    first = module.get_or_compute_capability_matrix(binary_identity, binary_identity, config_digest, compute)
    second = module.get_or_compute_capability_matrix(binary_identity, binary_identity, config_digest, compute)
    assert first == second
    assert call_count["n"] == 1

    other_identity = dict(binary_identity, sha256="d" * 64)
    third = module.get_or_compute_capability_matrix(other_identity, other_identity, config_digest, compute)
    assert call_count["n"] == 2
    assert third != first

    # Never persisted to disk — verify by asserting no on-disk cache file
    # attribute/method exists on the module for this purpose.
    assert not hasattr(module, "_CAPABILITY_DISK_CACHE_PATH")


def test_capability_probe_cache_bypassed_when_pre_run_and_pre_probe_identity_differ():
    """AC9/P1-7: if the pre-run and pre-runtime-probe identities differ
    (drift occurred before the cached-matrix decision was even made),
    `compute_fn()` must always run rather than reusing an unrelated cache
    entry that happens to match one of the two identities alone."""
    module = load_module()
    module._CAPABILITY_MEMO_CACHE.clear()

    identity_a = {
        "realpath": "/usr/local/bin/agy",
        "sha256": "a" * 64,
        "size": 42,
        "mtime_ns": 1,
        "platform": "Linux",
        "arch": "x86_64",
    }
    identity_b = dict(identity_a, sha256="b" * 64)
    config_digest = "digest-a"

    call_count = {"n": 0}

    def compute():
        call_count["n"] += 1
        return {"computed": call_count["n"]}

    # Prime a legitimate (non-drifted) cache entry for identity_b alone.
    module.get_or_compute_capability_matrix(identity_b, identity_b, config_digest, compute)
    assert call_count["n"] == 1

    # A run that drifted from identity_a to identity_b mid-run must NOT reuse
    # the identity_b-only cache entry above -- compute_fn() must run again.
    module.get_or_compute_capability_matrix(identity_a, identity_b, config_digest, compute)
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P1-1: binary-identity binding to a single resolved
# path, verified via a REAL subprocess round-trip against a fake binary.
# ---------------------------------------------------------------------------

_REAL_SHUTIL_WHICH = shutil.which


def _write_fake_agy_script(path: Path, extra_marker: str = "") -> None:
    script = (
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  --version) echo 'agy 1.1.9';;\n"
        "  --help) echo 'Usage: agy [OPTIONS]'; echo '  -p, --print, --prompt  print mode';;\n"
        "  --disable-slash-commands)\n"
        "    shift\n"
        "    if [ \"$1\" = '--help' ]; then echo 'Usage: agy [OPTIONS]'; fi\n"
        "    if [ \"$1\" = '-p' ]; then echo 'LOOP_AGY_SMOKE_OK'; fi\n"
        "    ;;\n"
        "  -p) echo 'LOOP_AGY_SMOKE_OK';;\n"
        "  *) echo 'unknown';;\n"
        "esac\n"
        f"# marker:{extra_marker}\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def test_binary_identity_binding_catches_binary_swapped_out_from_under_resolved_path(monkeypatch, tmp_path):
    """P1-1 integration test: run_preflight() resolves the agy binary exactly
    once via a real subprocess round-trip against a fake/swappable binary.
    The binary at the resolved absolute path is swapped for different
    content mid-run (simulating a PATH/binary-tamper attack) -- proving the
    binary-identity fingerprint is bound to what was actually executed (not
    just re-derived from a name lookup) and that drift is caught, forcing
    every capability predicate to `evidence_invalid`."""
    module = load_module()
    # Override this file's autouse hermetic-resolution fixture: this test
    # specifically needs a real `shutil.which()` round-trip against a real
    # (fake) binary on disk.
    monkeypatch.setattr(shutil, "which", _REAL_SHUTIL_WHICH)

    bin_path = tmp_path / "agy"
    _write_fake_agy_script(bin_path, extra_marker="v1")

    monkeypatch.setenv("AGY_BIN", str(bin_path))
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path / "artifacts"))

    original_probe = module._run_disable_slash_commands_probe

    def swap_binary_then_probe(agy_bin):
        probe_result = original_probe(agy_bin)
        # Simulate a mid-run binary swap: overwrite the SAME resolved path
        # with different content (different sha256/size) after the cheap
        # probe already ran against it, but before the drift check re-stats
        # the path for `binary_identity_after`.
        _write_fake_agy_script(bin_path, extra_marker="v2-swapped")
        return probe_result

    monkeypatch.setattr(module, "_run_disable_slash_commands_probe", swap_binary_then_probe)

    result = module.run_preflight(compute_capabilities=True)

    assert result["ok"] is True
    assert result["binary_identity"]["sha256"] is not None
    assert result["binary_identity_after"]["sha256"] is not None
    assert result["binary_identity"]["sha256"] != result["binary_identity_after"]["sha256"]
    for group, predicates in module.CAPABILITY_PREDICATES.items():
        for predicate in predicates:
            entry = result["capabilities"][group][predicate]
            assert entry["status"] == "evidence_invalid", f"{group}.{predicate}: {entry}"
            assert entry["reason_code"] == "binary_identity_drift"


def test_binary_identity_binding_no_drift_when_binary_unchanged(monkeypatch, tmp_path):
    """P1-1 control case: the same real fake-binary round-trip with NO swap
    must NOT report drift -- proves the drift detector is not just always
    tripping."""
    module = load_module()
    monkeypatch.setattr(shutil, "which", _REAL_SHUTIL_WHICH)

    bin_path = tmp_path / "agy"
    _write_fake_agy_script(bin_path, extra_marker="stable")

    monkeypatch.setenv("AGY_BIN", str(bin_path))
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path / "artifacts"))

    result = module.run_preflight(compute_capabilities=True)

    assert result["ok"] is True
    assert result["binary_identity"]["sha256"] == result["binary_identity_after"]["sha256"]
    for entry in result["capabilities"]["disable_slash_commands"].values():
        assert entry["reason_code"] != "binary_identity_drift"


def test_run_preflight_binds_every_probe_to_the_single_resolved_path(monkeypatch, tmp_path):
    """P1-1: once resolved, every subsequent probe (version/help/smoke/
    capability) is invoked against the exact same resolved absolute path --
    never a bare re-resolved name -- verified by asserting every recorded
    argv[0] is identical to the initially resolved path."""
    module = load_module()
    monkeypatch.setattr(shutil, "which", _REAL_SHUTIL_WHICH)

    bin_path = tmp_path / "agy"
    _write_fake_agy_script(bin_path)
    monkeypatch.setenv("AGY_BIN", str(bin_path))
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path / "artifacts"))

    seen_argv0: set[str] = set()
    original_run = module._run

    def spy_run(argv, cwd=None, timeout=None, env=None):
        seen_argv0.add(argv[0])
        return original_run(argv, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(module, "_run", spy_run)

    result = module.run_preflight(compute_capabilities=True)

    assert result["ok"] is True
    resolved_path = str(bin_path.resolve())
    assert seen_argv0 == {resolved_path}


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P1-2: leading_slash_is_literal uses the exact
# production argv and prioritizes expansion-error evidence.
# ---------------------------------------------------------------------------


def test_leading_slash_probe_uses_exact_production_argv(monkeypatch):
    """P1-2: the probe argv must be exactly
    `agy --disable-slash-commands -p <prompt>` -- the production argv, not a
    bare `-p <prompt>` (which only tests default expansion behavior)."""
    module = load_module()
    captured: dict[str, list[str]] = {}

    def fake_run(argv, cwd=None, timeout=None, env=None):
        captured["argv"] = argv
        return _FakeCompleted(0, module.EXPECTED_SMOKE, "")

    monkeypatch.setattr(module, "_run", fake_run)
    module._run_leading_slash_literal_probe("agy")

    assert captured["argv"][0] == "agy"
    assert captured["argv"][1] == "--disable-slash-commands"
    assert captured["argv"][2] == "-p"


def test_leading_slash_probe_expansion_evidence_takes_priority_over_sentinel(monkeypatch):
    """P1-2: when the combined output contains BOTH the sentinel text AND
    explicit expansion-rejection evidence, expansion evidence must win --
    the probe must never be misclassified as `literal_confirmed`."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None, env=None):
        return _FakeCompleted(
            1,
            module.EXPECTED_SMOKE,
            "unknown command: /nonexistent-loop-agy-capability-probe",
        )

    monkeypatch.setattr(module, "_run", fake_run)
    probe = module._run_leading_slash_literal_probe("agy")

    assert probe["expansion_detected"] is True
    assert probe["literal_confirmed"] is False


def test_leading_slash_probe_literal_confirmed_requires_exit_zero(monkeypatch):
    """P1-2: sentinel presence alone (without exit 0 and without expansion
    evidence) is not sufficient for `literal_confirmed`."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None, env=None):
        return _FakeCompleted(1, module.EXPECTED_SMOKE, "")

    monkeypatch.setattr(module, "_run", fake_run)
    probe = module._run_leading_slash_literal_probe("agy")

    assert probe["literal_confirmed"] is False
    assert probe["expansion_detected"] is False


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P1-3: isolated probe env + cost-confirmation gate +
# memoization-before-probe ordering.
# ---------------------------------------------------------------------------


def test_leading_slash_probe_isolates_home_and_xdg_env(monkeypatch):
    """P1-3: the probe's subprocess env overrides HOME/XDG_* to an isolated
    temp root -- never the real user's HOME/XDG_* -- so the real
    `~/.gemini/config/` hooks/permissions/skills/plugins cannot be
    discovered or loaded by this model-backed probe."""
    module = load_module()
    monkeypatch.setenv("HOME", "/home/real-user-do-not-leak")
    captured_env: dict[str, str] = {}

    def fake_run(argv, cwd=None, timeout=None, env=None):
        captured_env.update(env or {})
        return _FakeCompleted(0, module.EXPECTED_SMOKE, "")

    monkeypatch.setattr(module, "_run", fake_run)
    module._run_leading_slash_literal_probe("agy")

    assert captured_env["HOME"] != "/home/real-user-do-not-leak"
    assert "isolated-home" in captured_env["HOME"]
    assert captured_env["XDG_CONFIG_HOME"].startswith(captured_env["HOME"])
    assert captured_env["XDG_CACHE_HOME"].startswith(captured_env["HOME"])
    assert captured_env["XDG_STATE_HOME"].startswith(captured_env["HOME"])


def _fake_run_for_capability_computation(module):
    def fake_run(argv, cwd=None, timeout=None, env=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.1.9\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n  -p, --print   print mode\n", "")
        if argv == [bin_, "-p", module.SMOKE_PROMPT]:
            return _FakeCompleted(0, module.EXPECTED_SMOKE, "")
        if argv == [bin_, "--disable-slash-commands", "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n", "")
        if argv[:2] == [bin_, "--disable-slash-commands"] and argv[2] == "-p":
            return _FakeCompleted(0, module.EXPECTED_SMOKE, "")
        raise AssertionError(f"unexpected command: {argv}")

    return fake_run


def test_runtime_probe_cost_gate_skips_by_default(monkeypatch, tmp_path):
    """P1-3: compute_capabilities=True does NOT invoke the model-backed
    leading_slash_literal probe unless the cost-confirmation env var is
    explicitly set -- the predicate resolves to `unavailable` with a
    cost-related reason_code instead."""
    module = load_module()
    monkeypatch.delenv(module.AGY_RUNTIME_PROBE_COST_CONFIRM_ENV_VAR, raising=False)
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    called = {"leading_slash": False}
    real_leading_slash = module._run_leading_slash_literal_probe

    def spy_leading_slash(agy_bin):
        called["leading_slash"] = True
        return real_leading_slash(agy_bin)

    monkeypatch.setattr(module, "_run_leading_slash_literal_probe", spy_leading_slash)
    monkeypatch.setattr(module, "_run", _fake_run_for_capability_computation(module))

    result = module.run_preflight(compute_capabilities=True)

    assert called["leading_slash"] is False
    status = module.get_capability_status(
        result["capabilities"], "disable_slash_commands", "leading_slash_is_literal"
    )
    assert status["status"] == "unavailable"
    assert status["reason_code"] == "runtime_probe_cost_unconfirmed"


def test_runtime_probe_cost_gate_runs_when_explicitly_confirmed(monkeypatch, tmp_path):
    """P1-3: setting the cost-confirmation env var actually runs the
    real probe and lets it contribute `supported` evidence."""
    module = load_module()
    monkeypatch.setenv(module.AGY_RUNTIME_PROBE_COST_CONFIRM_ENV_VAR, "1")
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(module, "_run", _fake_run_for_capability_computation(module))

    result = module.run_preflight(compute_capabilities=True)

    status = module.get_capability_status(
        result["capabilities"], "disable_slash_commands", "leading_slash_is_literal"
    )
    assert status["status"] == "supported"


def test_costly_probe_never_runs_twice_on_cache_hit(monkeypatch, tmp_path):
    """P1-3/P1-7: the memoization cache lookup happens BEFORE the costly
    probe -- a second run_preflight() call with an identical (unchanged)
    binary/config must reuse the cached bundle and never invoke the costly
    probe a second time, even with cost confirmed."""
    module = load_module()
    module._CAPABILITY_MEMO_CACHE.clear()
    monkeypatch.setenv(module.AGY_RUNTIME_PROBE_COST_CONFIRM_ENV_VAR, "1")
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    call_count = {"n": 0}
    real_leading_slash = module._run_leading_slash_literal_probe

    def counting_leading_slash(agy_bin):
        call_count["n"] += 1
        return real_leading_slash(agy_bin)

    monkeypatch.setattr(module, "_run_leading_slash_literal_probe", counting_leading_slash)
    monkeypatch.setattr(module, "_run", _fake_run_for_capability_computation(module))
    # Binary identity is None-realpath in this fully-mocked (no real file)
    # scenario, which is stable across both calls -- config digest is also
    # stable, so the second call is expected to be a cache hit.

    module.run_preflight(compute_capabilities=True)
    module.run_preflight(compute_capabilities=True)

    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P1-4: sanitization leakage — version_evidence.raw,
# agy.version, and every external-facing output surface.
# ---------------------------------------------------------------------------


def test_version_evidence_raw_and_agy_version_sanitized_in_return_value(monkeypatch, tmp_path):
    """P1-4: run_preflight()'s actual RETURN VALUE (not just the on-disk
    artifact) never carries the raw combined `agy --version` output
    verbatim -- version_evidence.raw is redacted and agy.version is limited
    to a normalized value plus a separately redacted bounded sample."""
    module = load_module()
    leaked_url = "https://accounts.google.com/o/oauth2/auth?code=SECRET123&state=xyz"

    def fake_run(argv, cwd=None, timeout=None, env=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, f"agy 1.1.9\nWarning: {leaked_url}\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n  -p, --print   print mode\n", "")
        if argv == [bin_, "-p", module.SMOKE_PROMPT]:
            return _FakeCompleted(0, module.EXPECTED_SMOKE, "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    result = module.run_preflight()

    serialized = json.dumps(result)
    assert leaked_url not in serialized
    assert "SECRET123" not in serialized
    assert result["agy"]["version"] == "1.1.9"
    assert "accounts.google.com" not in (result["agy"].get("version_raw_sample") or "")
    assert "accounts.google.com" not in (result["version_evidence"].get("raw") or "")


def test_capability_probe_raw_fields_sanitized_in_return_value(monkeypatch, tmp_path):
    """P1-4: capability_probes stdout/stderr/argv must never survive on
    run_preflight()'s return value -- which is exactly what `--json` /
    `--output-file` serialize in `main()` -- only the artifact-safe shape."""
    module = load_module()
    monkeypatch.setenv(module.AGY_RUNTIME_PROBE_COST_CONFIRM_ENV_VAR, "1")
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))
    secret_marker = "SECRET_CAPABILITY_PROBE_STDOUT_DO_NOT_LEAK"

    def fake_run(argv, cwd=None, timeout=None, env=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.1.9\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "Usage: agy [OPTIONS]\n  -p, --print   print mode\n", "")
        if argv == [bin_, "-p", module.SMOKE_PROMPT]:
            return _FakeCompleted(0, module.EXPECTED_SMOKE, "")
        if argv == [bin_, "--disable-slash-commands", "--help"]:
            return _FakeCompleted(0, f"Usage: agy [OPTIONS]\n{secret_marker}\n", "")
        if argv[:2] == [bin_, "--disable-slash-commands"] and argv[2] == "-p":
            return _FakeCompleted(0, module.EXPECTED_SMOKE, "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight(compute_capabilities=True)

    serialized = json.dumps(result)
    assert secret_marker not in serialized
    for probe in result["capability_probes"].values():
        if isinstance(probe, dict):
            assert "stdout" not in probe
            assert "stderr" not in probe
            assert "argv" not in probe


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P1-5: parser-rejection classification is narrow.
# ---------------------------------------------------------------------------


def test_generic_invalid_option_without_flag_name_is_not_parser_rejection():
    """P1-5: a bare "invalid option"/"unknown option" string that never
    names --disable-slash-commands must not be classified as a parser
    rejection -- it stays ambiguous/inconclusive."""
    module = load_module()
    result = module.classify_parser_acceptance(1, "", "Error: invalid option --some-unrelated-flag")
    assert result["accepted"] is None
    assert result["evidence_source"] == "exit_nonzero_unclassified"

    status = module.derive_parser_accepts_flag_status(result)
    assert status["status"] == "inconclusive"


def test_auth_evidence_in_stderr_takes_priority_over_generic_parser_error_string():
    """P1-5: an unrelated config warning containing "invalid option"
    alongside an authoritative auth-failure signal -- both in stderr this
    time, not split across stdout/stderr -- must classify as auth-blocked,
    never as a parser rejection."""
    module = load_module()
    result = module.classify_parser_acceptance(
        1,
        "",
        "Please sign in with Google to continue. (config warning: invalid option somewhere)",
    )
    assert result["auth_signal"] is not None
    assert result["accepted"] is None
    assert result["evidence_source"] == "auth_signal"

    status = module.derive_parser_accepts_flag_status(result)
    assert status["status"] == "inconclusive"
    assert status["reason_code"] == "auth_blocked_probe"


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P1-7: config digest includes the real hooks config.
# ---------------------------------------------------------------------------


def test_config_digest_changes_when_hooks_config_changes(tmp_path):
    """P1-7: the config digest must change when `.agents/hooks.json`
    changes -- previously it only covered `.claude/settings.json` and
    `.agents/mcp_config.json`, so a hooks config change never invalidated a
    cached capability matrix."""
    module = load_module()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".agents" / "mcp_config.json").write_text("{}", encoding="utf-8")

    digest_without_hooks = module.compute_config_digest(tmp_path)

    (tmp_path / ".agents" / "hooks.json").write_text('{"hooks": []}', encoding="utf-8")
    digest_with_hooks = module.compute_config_digest(tmp_path)

    assert digest_without_hooks != digest_with_hooks


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P2-1: controlled early exits still populate the
# capability matrix shape with per-predicate reasons (lower priority).
# ---------------------------------------------------------------------------


def test_cli_missing_early_exit_still_populates_capability_matrix(monkeypatch, tmp_path):
    """P2-1: a binary-missing early exit with compute_capabilities=True still
    returns a full-shaped matrix (every predicate present) instead of an
    absent/None `capabilities` field."""
    module = load_module()
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    def fake_run_missing(argv, cwd=None, timeout=None, env=None):
        raise FileNotFoundError("agy: command not found")

    monkeypatch.setattr(module, "_run", fake_run_missing)

    result = module.run_preflight(compute_capabilities=True)

    assert result["ok"] is False
    assert result["failure_class"] == "cli_missing"
    assert result["capabilities"] is not None
    for group, predicates in module.CAPABILITY_PREDICATES.items():
        assert set(result["capabilities"][group].keys()) == set(predicates)
    # --require-capability must never treat this as a success signal.
    exit_code = module.compute_require_capability_exit_code(
        result["capabilities"], ["disable_slash_commands.parser_accepts_flag"]
    )
    assert exit_code in (1, 77)


def test_help_failure_early_exit_populates_capability_matrix_with_help_unavailable_reason(monkeypatch, tmp_path):
    """P2-1: a help-failure early exit records a help-specific reason_code on
    the parser_accepts_flag predicate rather than a generic probe_not_run."""
    module = load_module()
    monkeypatch.setenv("AGY_PREFLIGHT_ARTIFACT_DIR", str(tmp_path))

    def fake_run(argv, cwd=None, timeout=None, env=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.1.9\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(2, "", "help failed")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)

    result = module.run_preflight(compute_capabilities=True)

    assert result["ok"] is False
    assert result["failure_class"] == "cli_incompatible"
    status = module.get_capability_status(result["capabilities"], "disable_slash_commands", "parser_accepts_flag")
    assert status["status"] == "unavailable"
    assert status["reason_code"] == "help_unavailable"


# ---------------------------------------------------------------------------
# Issue #1941 fix_delta P2-2: version parser rejects conflicting/unanchored
# version-shaped tokens.
# ---------------------------------------------------------------------------


def test_version_parser_ignores_unrelated_dependency_version_line():
    """P2-2: a leading unrelated warning line with its own version-shaped
    token (e.g. a dependency version) must not be picked over the real
    `agy <version>` line."""
    module = load_module()
    parsed = module.parse_agy_version_string("dependency 2.4.0 deprecated\nagy 1.1.9\n")
    assert parsed["status"] == "parsed"
    assert parsed["version"] == "1.1.9"
    assert parsed["core"] == (1, 1, 9)


def test_version_parser_rejects_multiple_conflicting_agy_anchored_versions():
    """P2-2: multiple version-shaped tokens both anchored to an `agy`
    program-name context line are ambiguous and must be rejected rather than
    guessed at."""
    module = load_module()
    parsed = module.parse_agy_version_string("agy 1.1.9\nagy 2.0.0\n")
    assert parsed["status"] == "version_evidence_invalid"
    assert parsed["version"] is None
