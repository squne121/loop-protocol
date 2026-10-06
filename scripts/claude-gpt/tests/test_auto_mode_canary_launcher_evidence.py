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


# usage 記載の対象外とする option。`-h/--help` は argparse が自動で追加する標準 help であり、
# wrapper の Usage コメントには書かない（それ以外の option は全て usage に記載する）。
USAGE_EXCLUDED_OPTIONS = frozenset({"-h", "--help"})

# wrapper の Exit code 欄が一致すべき Python 側の公開 exit code 定数名。
PUBLIC_EXIT_CONSTANTS = (
    "EXIT_OK",
    "EXIT_FAIL",
    "EXIT_INVALID_INVOCATION",
    "EXIT_GC_PARTIAL",
    "EXIT_SKIP",
)


def _public_option_flags(parser) -> set[str]:
    """argparse の公開 option（`--help` を除く long option）の集合。"""
    return {opt for opt in _option_strings(parser) if opt not in USAGE_EXCLUDED_OPTIONS}


def _usage_block(sh_text: str) -> str:
    """`# Usage` 開始から `# Exit code` 直前までのコメント（通常 canary と explicit GC の両節を含む）。"""
    return sh_text.split("# Usage", 1)[1].split("# Exit code", 1)[0]


def _usage_flags(sh_text: str) -> set[str]:
    return set(re.findall(r"--[a-z][a-z0-9-]*", _usage_block(sh_text)))


def _usage_exit_codes(sh_text: str) -> set[int]:
    """`# Exit code` 欄の `#   <数値>  説明` 行から数値集合を抽出する。"""
    exit_block = sh_text.split("# Exit code", 1)[1].split("\n\n", 1)[0]
    return {int(code) for code in re.findall(r"^#\s+(\d+)\s", exit_block, flags=re.MULTILINE)}


def _usage_contract_diffs(
    usage_flags: set[str],
    argparse_flags: set[str],
    usage_codes: set[int],
    public_codes: set[int],
) -> list[str]:
    """usage と argparse / exit code 定数の双方向差分を返す（空なら一致）。"""
    diffs: list[str] = []
    if argparse_flags - usage_flags:
        diffs.append(f"argparse にあるが usage に無い option: {sorted(argparse_flags - usage_flags)}")
    if usage_flags - argparse_flags:
        diffs.append(f"usage にあるが argparse に無い option: {sorted(usage_flags - argparse_flags)}")
    if public_codes - usage_codes:
        diffs.append(f"公開 exit code だが usage に無い: {sorted(public_codes - usage_codes)}")
    if usage_codes - public_codes:
        diffs.append(f"usage にあるが公開 exit code でない: {sorted(usage_codes - public_codes)}")
    return diffs


def test_canary_sh_usage_matches_argparse_modes(canary):
    sh_text = CANARY_SH.read_text(encoding="utf-8")
    assert "auto-mode-check" not in sh_text

    usage_match = re.search(r"--mode \{([^}]+)\}", sh_text)
    assert usage_match is not None, "wrapper の usage コメントに --mode {...} がない"
    usage_modes = tuple(usage_match.group(1).split("|"))
    assert set(usage_modes) == set(_mode_choices(canary.build_parser()))

    # usage の option 集合 == argparse の公開 option 集合（--help 除外）、
    # usage の Exit code 集合 == Python の公開 EXIT_* 定数集合（双方向・完全一致）
    public_codes = {getattr(canary, name) for name in PUBLIC_EXIT_CONSTANTS}
    assert public_codes == {0, 1, 2, 3, 77}
    diffs = _usage_contract_diffs(
        _usage_flags(sh_text),
        _public_option_flags(canary.build_parser()),
        _usage_exit_codes(sh_text),
        public_codes,
    )
    assert not diffs, "wrapper usage が Python 公開契約とずれている: " + "; ".join(diffs)


def test_usage_contract_comparison_detects_drift(canary):
    # 比較ロジック自体が drift を検出できること（false-green 防止）。permanent harness にはしない。
    sh_text = CANARY_SH.read_text(encoding="utf-8")
    argparse_flags = _public_option_flags(canary.build_parser())
    usage_flags = _usage_flags(sh_text)
    usage_codes = _usage_exit_codes(sh_text)
    public_codes = {getattr(canary, name) for name in PUBLIC_EXIT_CONSTANTS}

    # 正例: 現状は差分なし
    assert _usage_contract_diffs(usage_flags, argparse_flags, usage_codes, public_codes) == []

    # 負例 1: argparse に usage 未記載の option が増えた
    assert _usage_contract_diffs(usage_flags, argparse_flags | {"--new-option"}, usage_codes, public_codes)

    # 負例 2: usage から option を 1 つ欠落させた（--dry-run / GC 用 option を含め各 option で検出）
    for flag in sorted(usage_flags):
        assert _usage_contract_diffs(usage_flags - {flag}, argparse_flags, usage_codes, public_codes), flag

    # 負例 3: usage に argparse に無い option が残っている
    assert _usage_contract_diffs(usage_flags | {"--removed-option"}, argparse_flags, usage_codes, public_codes)

    # 負例 4: exit code 3（GC partial）を欠落させた / 公開されない code を足した
    assert _usage_contract_diffs(usage_flags, argparse_flags, usage_codes - {3}, public_codes)
    assert _usage_contract_diffs(usage_flags, argparse_flags, usage_codes | {9}, public_codes)

    # 負例 5: 実 wrapper テキストから `--dry-run` / exit code 3 の行を消すと抽出結果にも反映される
    mutated = sh_text.replace("--dry-run", "").replace("#   3   ", "#   9   ")
    assert "--dry-run" not in _usage_flags(mutated)
    assert 3 not in _usage_exit_codes(mutated)
    assert _usage_contract_diffs(
        _usage_flags(mutated), argparse_flags, _usage_exit_codes(mutated), public_codes
    )


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
