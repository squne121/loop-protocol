#!/usr/bin/env python3
"""Regression tests for Issue #2807.

canonical runtime acceptance evidence の必須フィールド束縛（AC3）、既存 runtime
verification assets の再利用のみで新しい permanent harness を追加しないこと
（AC8）、および既存 fixture tests が変更されず CI が network-dependent になら
ないこと（AC5）を固定する static regression test。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BODY_AUTHORING_PATH = (
    REPO_ROOT / ".claude" / "skills" / "create-issue" / "references" / "body-authoring.md"
)
EVIDENCE_POLICY_PATH = (
    REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "references" / "evidence-policy.md"
)
FIXTURE_TEST_PATH = (
    REPO_ROOT / "scripts" / "claude-gpt" / "tests" / "test_proxy_model_compatibility.py"
)
LAUNCH_SH_PATH = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"
RUNTIME_SMOKE_TEST_PATH = REPO_ROOT / "scripts" / "claude-gpt" / "runtime_smoke_test.sh"

REQUIRED_IDENTITY_FIELDS = [
    "run_head_sha",
    "git_dirty",
    "launcher hash",
    "absolute path / version / hash",
]

FORBIDDEN_NEW_SURFACE_MARKERS = [
    "new permanent daemon",
    "new_daemon",
    "generic_runtime_harness",
]


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file not found: {path}"
    return path.read_text(encoding="utf-8")


def test_evidence_policy_requires_current_head_proxy_identity_binding_and_no_new_harness():
    """GIVEN body-authoring.md / evidence-policy.md の canonical runtime
    acceptance evidence 記述
    WHEN current-head production launcher を fake proxy override なしで
    external process 起動した結果を要求する
    THEN actual selected proxy identity（run_head_sha + git_dirty + 実行
    command identity + launcher hash + selected proxy absolute path/version/
    hash）の記載を要求し、fixture proxy の path/version のみでは不十分と
    明記し、新しい harness を追加せず既存資産のみを参照する（AC3 / AC8）。
    """
    body_authoring_text = _read(BODY_AUTHORING_PATH)
    evidence_policy_text = _read(EVIDENCE_POLICY_PATH)

    # AC3: 両ドキュメントとも current-head production launcher を fake
    # proxy override なしで external process 起動した結果 + actual
    # selected proxy identity の必須フィールドを要求している。
    assert "fake proxy override なしで" in body_authoring_text or (
        "fake binary injection" in body_authoring_text
    )
    assert "fake proxy override なしで external process 起動した結果" in evidence_policy_text

    for field in REQUIRED_IDENTITY_FIELDS:
        assert field in body_authoring_text, f"missing field in body-authoring.md: {field}"
        assert field in evidence_policy_text, f"missing field in evidence-policy.md: {field}"

    assert "実行 command identity" in body_authoring_text
    assert "実行 command identity" in evidence_policy_text

    # fixture proxy の path/version のみでは不十分、という明記。
    assert (
        "fixture proxy の path/version のみでは不十分" in body_authoring_text
    )
    assert (
        "fixture proxy の path/version のみの evidence は、この evidence "
        "要件を **充足しない**" in evidence_policy_text
    )

    # AC8: 既存 runtime verification assets（launch.sh --check-only,
    # runtime_smoke_test.sh）への参照のみを追加し、新しい permanent
    # harness / daemon / network-required merge gate を追加しない。
    assert LAUNCH_SH_PATH.is_file()
    assert RUNTIME_SMOKE_TEST_PATH.is_file()
    assert "scripts/claude-gpt/launch.sh --check-only" in evidence_policy_text
    assert "scripts/claude-gpt/runtime_smoke_test.sh" in evidence_policy_text
    assert (
        "新しい permanent daemon、generic runtime harness、"
        "network-required merge gate の追加を要求しない" in evidence_policy_text
    )

    for marker in FORBIDDEN_NEW_SURFACE_MARKERS:
        assert marker not in evidence_policy_text
        assert marker not in body_authoring_text


def test_existing_fixture_tests_unchanged_and_ci_not_network_dependent():
    """GIVEN 既存 fixture tests（scripts/claude-gpt/tests/test_proxy_model_compatibility.py）
    WHEN Issue #2807 の変更を適用する
    THEN 既存 fixture tests は hermetic implementation-semantics coverage
    としてそのまま維持され、通常 CI は real ChatGPT account/network を
    必須にしない（AC5）。
    """
    # AC5: fixture test ファイル自体が Allowed Paths 外であり、本 Issue の
    # 実装で改変されていないことを確認する（存在確認 + 既存 hermetic
    # marker の維持確認）。
    fixture_test_text = _read(FIXTURE_TEST_PATH)
    assert (
        "depend on live network access or a real ChatGPT account/subscription"
        in fixture_test_text
    )
    assert "CLAUDE_GPT_PROXY_BIN" in fixture_test_text
    assert (
        "SKIP (exit 77) / environment_blocked" in fixture_test_text
        or "environment_blocked" in fixture_test_text
    )

    evidence_policy_text = _read(EVIDENCE_POLICY_PATH)
    assert (
        "既存の fixture tests（`scripts/claude-gpt/tests/test_proxy_model_compatibility.py` "
        "等）は hermetic implementation-semantics coverage としてそのまま維持し、"
        "廃止・改変しない" in evidence_policy_text
    )
    assert "通常 CI は real ChatGPT account / network を必須にしない" in evidence_policy_text
