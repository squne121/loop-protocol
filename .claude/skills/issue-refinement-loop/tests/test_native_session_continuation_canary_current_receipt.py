"""Issue #2938 focused tests: the session continuation canary against the CURRENT
``launch.sh --check-only`` receipt (``CLAUDE_GPT_LAUNCH_RESULT_V1``).

PR #2932 (#2925) removed ``model_alias_ok`` / ``CLAUDE_GPT_PREFLIGHT_RESULT_V1``
from the launcher receipt. The canary therefore must:

* proceed on a current ``status: ok`` + ``mode: check_only`` receipt;
* never claim "no provider fallback" (``provider_fallback: false``): provider
  fallback is not observable from the receipt, and
  ``connected_server.model_catalog_ok`` only shows that the required models are
  listed in ``/v1/models``. A PASS artifact records ``"unobserved"``; FAIL / SKIP
  artifacts carry no ``provider_fallback`` key;
* ignore a stale ``model_alias_ok`` key (this file is the only place where that
  key appears, as a negative control);
* treat ``status: failed`` (exit 7) and ``blocked`` / ``claude_binary_not_found``
  (exit 3) as SKIP (regression pins).

The runner is driven via ``main(argv)`` with only the subprocess boundary
(``_run``) and the worktree / executable identity helpers monkeypatched. No live
runner, claude-gpt, or claude CLI is started.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "issue-refinement-loop"
    / "scripts"
    / "run_native_session_continuation_canary.py"
)

CURRENT_OK_RECEIPT = {
    "schema": "CLAUDE_GPT_LAUNCH_RESULT_V1",
    "status": "ok",
    "mode": "check_only",
    "connected_server": {
        "base_url": "http://127.0.0.1:18765",
        "host": "127.0.0.1",
        "port": 18765,
        "reachable": True,
        "models_http_status": 200,
        "required_models": ["gpt-6-sol", "gpt-6-luna"],
        "missing_models": [],
        "model_catalog_ok": True,
        "classification": "ok",
        "version": "未確認",
        "version_note": "connected server version is not observable",
    },
    "local_proxy_binary_auxiliary": {
        "path": "/usr/local/bin/claude-code-proxy",
        "version": "0.0.0",
        "note": "auxiliary only; not necessarily the binary the connected server runs",
    },
    "launch_env": {"ANTHROPIC_AUTH_TOKEN": "placeholder"},
}


def _load_canary_module():
    spec = importlib.util.spec_from_file_location(
        "issue_2938_native_session_continuation_canary_current_receipt", RUNNER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_main_mocks(module, monkeypatch, tmp_path, *, check_only_result):
    """Mock only the subprocess / identity boundary and drive ``main()``.

    ``check_only_result`` is the ``(rc, stdout)`` returned for the launcher's
    ``--check-only`` invocation. Real launches return fixed session output.
    Returns the list of recorded ``_run`` argv lists.
    """
    monkeypatch.setattr(module, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(module, "_default_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(module, "verify_worktree_identity", lambda worktree_arg, repo_root: worktree_arg)
    monkeypatch.setattr(module, "prepare_output_dir", lambda output_dir: None)
    monkeypatch.setattr(module, "preflight_claude_available", lambda claude_bin: ("/fake/launch.sh", None))
    monkeypatch.setattr(module, "extract_claude_resolved_executable_sha256", lambda resolved_bin: "fakehash")
    monkeypatch.setattr(
        module,
        "classify_claude_structured_outcome",
        lambda exit_code, stdout, stderr, timed_out: ("ok", None),
    )
    monkeypatch.setattr(
        module,
        "is_terminal_success",
        lambda stdout: (True, None) if "TERMINAL_OK" in stdout else (False, "not terminal"),
    )
    monkeypatch.setattr(module, "marker_recalled", lambda stdout, marker: True)

    def fake_extract_parent_session_id(stdout: str) -> str | None:
        if "SESSION_A" in stdout:
            return "session-a"
        if "SESSION_C" in stdout:
            return "session-c"
        return None

    monkeypatch.setattr(module, "extract_claude_parent_session_id", fake_extract_parent_session_id)

    calls: list[list[str]] = []

    def fake_run(argv, *, cwd=None, timeout=None, input_text=None, env=None):
        calls.append(list(argv))
        if "--check-only" in argv:
            rc, out = check_only_result
            return rc, out, "", False
        if "--resume" in argv:
            return 0, "SESSION_A TERMINAL_OK", "", False
        if "--tools" in argv:
            return 0, "SESSION_A TERMINAL_OK", "", False
        return 0, "SESSION_C TERMINAL_OK", "", False

    monkeypatch.setattr(module, "_run", fake_run)
    return calls


def _run_main(module, tmp_path):
    output_dir = tmp_path / "evidence"
    exit_code = module.main(
        [
            "--worktree", str(tmp_path),
            "--claude-adapter", "claude-gpt",
            "--claude-bin", "/fake/launch.sh",
            "--output-dir", str(output_dir),
            "--timeout-seconds", "5",
        ]
    )
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    return exit_code, evidence


def test_current_check_only_ok_receipt_proceeds_without_model_alias(monkeypatch, tmp_path):
    module = _load_canary_module()
    assert "model_alias_ok" not in CURRENT_OK_RECEIPT
    calls = _install_main_mocks(
        module, monkeypatch, tmp_path, check_only_result=(0, json.dumps(CURRENT_OK_RECEIPT))
    )

    verdict, reason, receipt = module.preflight_claude_gpt("/fake/launch.sh", 5.0)
    assert (verdict, reason) == ("ok", None)
    assert receipt is not None and receipt["connected_server"]["model_catalog_ok"] is True

    calls.clear()
    exit_code, evidence = _run_main(module, tmp_path)
    assert exit_code == 0, evidence.get("errors")
    assert evidence["verdict"] == "PASS"
    # the canary proceeded past the check-only gate into the real launches
    assert any("--check-only" in argv for argv in calls)
    assert any("--resume" in argv for argv in calls)
    assert evidence["claude_gpt_launcher_receipt"]["connected_server"]["classification"] == "ok"


def test_pass_evidence_provider_fallback_is_unobserved_not_false(monkeypatch, tmp_path):
    module = _load_canary_module()
    # model_catalog_ok: true must NOT be turned into "no provider fallback".
    assert CURRENT_OK_RECEIPT["connected_server"]["model_catalog_ok"] is True
    _install_main_mocks(module, monkeypatch, tmp_path, check_only_result=(0, json.dumps(CURRENT_OK_RECEIPT)))

    exit_code, evidence = _run_main(module, tmp_path)
    assert exit_code == 0, evidence.get("errors")
    assert evidence["verdict"] == "PASS"
    assert "provider_fallback" in evidence
    assert evidence["provider_fallback"] == "unobserved"
    assert evidence["provider_fallback"] is not False
    assert evidence["provider_fallback"] is not True
    # runtime fallback is still a concrete, observed signal
    assert evidence["runtime_fallback"] is False


def test_stale_model_alias_ok_false_receipt_is_ignored(monkeypatch, tmp_path):
    module = _load_canary_module()
    stale_receipt = dict(CURRENT_OK_RECEIPT)
    stale_receipt["model_alias_ok"] = False  # removed by #2925; negative control only
    _install_main_mocks(module, monkeypatch, tmp_path, check_only_result=(0, json.dumps(stale_receipt)))

    exit_code, evidence = _run_main(module, tmp_path)
    assert exit_code == 0, evidence.get("errors")
    assert evidence["verdict"] == "PASS"
    assert evidence["provider_fallback"] == "unobserved"
    assert evidence["provider_fallback"] is not True
    assert not any("provider fallback" in error for error in evidence["errors"])
    assert not hasattr(module, "detect_provider_fallback")


@pytest.mark.parametrize(
    "reason,cause",
    [
        ("model_alias_not_resolved", "connected_server_model_catalog_incomplete"),
        ("connected_server_unreachable", "no_server_listening_or_not_responding"),
    ],
)
def test_check_only_failed_receipt_exit_7_is_skip(monkeypatch, tmp_path, reason, cause):
    module = _load_canary_module()
    failed = {
        "schema": "CLAUDE_GPT_LAUNCH_RESULT_V1",
        "status": "failed",
        "reason": reason,
        "cause": cause,
        "connected_server": {"reachable": True, "model_catalog_ok": False, "classification": "required_models_missing"},
    }
    calls = _install_main_mocks(module, monkeypatch, tmp_path, check_only_result=(7, json.dumps(failed)))

    exit_code, evidence = _run_main(module, tmp_path)
    assert exit_code == 77
    assert evidence["verdict"] == "SKIP"
    # neither PASS nor FAIL: no provider_fallback claim of any value
    assert "provider_fallback" not in evidence
    # SKIP at the check-only gate: no real session launch happened
    assert not any("--resume" in argv or "--tools" in argv for argv in calls)


def test_blocked_claude_binary_not_found_is_skip(monkeypatch, tmp_path):
    module = _load_canary_module()
    blocked = {"schema": "CLAUDE_GPT_LAUNCH_RESULT_V1", "status": "blocked", "reason": "claude_binary_not_found"}
    calls = _install_main_mocks(module, monkeypatch, tmp_path, check_only_result=(3, json.dumps(blocked)))

    exit_code, evidence = _run_main(module, tmp_path)
    assert exit_code == 77
    assert evidence["verdict"] == "SKIP"
    assert "provider_fallback" not in evidence
    assert not any("--resume" in argv or "--tools" in argv for argv in calls)


def test_fail_path_has_no_provider_fallback_key(monkeypatch, tmp_path):
    """FAIL path: an unknown launcher option (exit 2) is a caller bug -> FAIL, key absent."""
    module = _load_canary_module()
    blocked = {"schema": "CLAUDE_GPT_LAUNCH_RESULT_V1", "status": "blocked", "reason": "unknown_launcher_option"}
    _install_main_mocks(module, monkeypatch, tmp_path, check_only_result=(2, json.dumps(blocked)))

    exit_code, evidence = _run_main(module, tmp_path)
    assert exit_code == 1
    assert evidence["verdict"] == "FAIL"
    assert "provider_fallback" not in evidence
