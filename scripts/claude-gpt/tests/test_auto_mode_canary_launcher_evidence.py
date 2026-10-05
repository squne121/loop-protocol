"""scripts/claude-gpt/tests/test_auto_mode_canary_launcher_evidence.py

Issue #2949: `auto_mode_canary` の evidence が、PR #2932（#2925）で撤去された launcher-owned
policy / readback（`preflight.sh` の auto-mode 出力 readback、launcher 生成 settings、固定
permission mode、local proxy binary version の接続先 server version 化）を、現在の観測値として
主張しないことを検証する。

#2843 所有 mode（`canonical-workflow-delegation` / `classifier-semantics`）と
`--baseline-policy-commit` は CLI compatibility のみ維持する。#2843 の policy-differential
diagnostic が現在も有効であることは、この test では保証しない。

Runtime Verification Applicability: immediate (AC7)。canary executable を実 subprocess として
`--mode agy --no-evidence` で起動し、exit code と stdio を観測する
（live ChatGPT / proxy / GitHub I/O は使わない。exit 77 は SKIP であり PASS ではない）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent
REPO_ROOT = SCRIPT_DIR.parent.parent
CANARY_PY = SCRIPT_DIR / "auto_mode_canary.py"
CANARY_SH = SCRIPT_DIR / "auto_mode_canary.sh"

# 撤去済み surface 由来の literal。再導入されていないことを検証する。
REMOVED_LITERALS = (
    "EXPECTED_AUTO_MODE_CHECK_SCHEMA",
    "CLAUDE_GPT_AUTO_MODE_PREFLIGHT_RESULT_V2",
    "auto-mode-check",
    "--auto-mode-check-json",
    "--settings-path",
    "settings_sha256",
    "auto_mode_defaults_digest",
    "effective_config_digest",
    "auto_mode_readback_ok",
    "auto_mode_check_schema_mismatch",
    "auto_mode_check_observed_schema",
    "unavailable_not_provided",
)

REMOVED_POLICY_KEYS = (
    "permission_mode",
    "settings_sha256",
    "auto_mode_defaults_digest",
    "effective_config_digest",
    "auto_mode_readback_ok",
    "auto_mode_check_schema_mismatch",
    "auto_mode_check_observed_schema",
    "classify_all_shell",
)

OWNED_MODES = (
    "canonical-workflow-delegation",
    "classifier-semantics",
    "issue-editor-permission",
)


def _load_canary_module():
    spec = importlib.util.spec_from_file_location("auto_mode_canary_launcher_evidence_under_test", CANARY_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass の postponed annotation 解決に必要
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def canary():
    return _load_canary_module()


def _option_strings(parser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def _mode_choices(parser) -> tuple[str, ...]:
    for action in parser._actions:
        if "--mode" in action.option_strings:
            return tuple(action.choices)
    raise AssertionError("--mode が argparse に存在しない")


def _run_canary(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--locked", "python3", str(CANARY_PY), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_canary_sh_usage_matches_argparse_modes(canary):
    sh_text = CANARY_SH.read_text(encoding="utf-8")
    assert "auto-mode-check" not in sh_text

    usage_match = re.search(r"--mode \{([^}]+)\}", sh_text)
    assert usage_match is not None, "wrapper の usage コメントに --mode {...} がない"
    usage_modes = tuple(usage_match.group(1).split("|"))
    assert set(usage_modes) == set(_mode_choices(canary.build_parser()))

    # usage に書いた option が実際の argparse に存在する
    options = _option_strings(canary.build_parser())
    usage_block = sh_text.split("# Usage:", 1)[1].split("# Exit code", 1)[0]
    for flag in re.findall(r"--[a-z][a-z0-9-]*", usage_block):
        assert flag in options, f"usage の {flag} が argparse に存在しない"


def test_effective_policy_has_no_removed_surface_readback(canary):
    # 撤去済み literal は canary 本体に存在しない
    source = CANARY_PY.read_text(encoding="utf-8")
    for literal in REMOVED_LITERALS:
        assert literal not in source, f"撤去済み literal が残っている: {literal}"

    # 撤去済み引数は argparse にも存在しない
    options = _option_strings(canary.build_parser())
    assert "--auto-mode-check-json" not in options
    assert "--settings-path" not in options

    # _effective_policy は引数なしで呼べ、撤去済み key と placeholder 値を持たない
    policy = canary._effective_policy()
    for key in REMOVED_POLICY_KEYS:
        assert key not in policy, f"撤去済み key が evidence に残っている: {key}"
    assert "unavailable_not_provided" not in json.dumps(policy)
    # 実際に観測できる file digest は残る
    for key in ("canary_script_sha256", "lib_sh_sha256", "preflight_sh_sha256"):
        assert key in policy


def test_owned_modes_remain_in_argparse_choices(canary):
    parser = canary.build_parser()
    choices = _mode_choices(parser)
    for mode in OWNED_MODES:
        assert mode in choices
    options = _option_strings(parser)
    assert "--baseline-policy-commit" in options
    assert "--observation-runs" in options
    # #2843 所有の baseline policy 経路の関数は削除・改変されていない
    assert callable(getattr(canary, "_lib_sh_policy_sha256", None))
    assert callable(getattr(canary, "splice_baseline_policy", None))


def test_canary_subprocess_evidence_output_and_exit_codes():
    help_result = _run_canary("--help")
    assert help_result.returncode == 0
    assert "--auto-mode-check-json" not in help_result.stdout
    assert "--settings-path" not in help_result.stdout
    assert "auto-mode-check" not in help_result.stdout
    assert "--baseline-policy-commit" in help_result.stdout

    # 撤去済み引数は invalid invocation (exit 2)
    removed = _run_canary("--mode", "agy", "--no-evidence", "--auto-mode-check-json", "x.json")
    assert removed.returncode == 2
    removed_settings = _run_canary("--mode", "agy", "--no-evidence", "--settings-path", "x.json")
    assert removed_settings.returncode == 2

    result = _run_canary("--mode", "agy", "--no-evidence")
    # SKIP (77) は PASS へ昇格しない
    assert result.returncode == 77, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["exit_classification"] == "skip"

    for key in REMOVED_POLICY_KEYS:
        assert key not in evidence["effective_policy"]
    assert "proxy_version" not in evidence["sut_revision"]
    assert evidence["sut_revision"]["connected_server_version"] == "not_observed"
    for removed_key in ("permission_mode", "settings_sha256", "auto_mode_readback_ok"):
        assert removed_key not in result.stdout
    assert "unavailable_not_provided" not in result.stdout


def test_sut_revision_does_not_claim_local_binary_as_connected_server(canary):
    revision = canary._sut_revision()
    assert "proxy_version" not in revision
    assert revision["connected_server_version"] == "not_observed"
    # 新しい connected-server probe を追加していない: PATH 上の proxy binary を実行しない
    source = CANARY_PY.read_text(encoding="utf-8")
    assert '_version("claude-code-proxy"' not in source
