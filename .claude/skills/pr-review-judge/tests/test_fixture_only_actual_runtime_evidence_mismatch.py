#!/usr/bin/env python3
"""Regression tests for Issue #2807.

pr-review-judge の evidence-policy.md が、actual/canonical runtime acceptance を
要求する AC に対して fixture-only evidence（fixture PASS + real smoke SKIP や
PR body の `[x]` self-report）を代替として認めない instruction wiring を保持して
いることを固定する static regression test。

live pr-review-judge が実際に REQUEST_CHANGES を返すことまでは証明しない
（本 Issue の Runtime Verification Applicability: not_applicable 参照）。
"""

from pathlib import Path

EVIDENCE_POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "evidence-policy.md"
)


def _read_evidence_policy() -> str:
    assert EVIDENCE_POLICY_PATH.is_file(), (
        f"evidence-policy.md not found at {EVIDENCE_POLICY_PATH}"
    )
    return EVIDENCE_POLICY_PATH.read_text(encoding="utf-8")


def _reproduce_2801_fixture() -> dict:
    """#2801 AC11 / PR #2802 が観測した failure class を static fixture として再現する。

    実際の CI や外部プロセスは起動しない。evidence-policy.md の記述と突き合わせる
    ための metadata のみを持つ dict。
    """

    return {
        "ac_requires": "actual_canonical_compatible_proxy_selection",
        "assigned_vc": (
            "uv run --locked pytest "
            "scripts/claude-gpt/tests/test_proxy_model_compatibility.py"
            "::test_check_only_passes_with_compatible_proxy -q"
        ),
        "vc_evidence_source": "fixture_only",
        "vc_injects_fake_proxy_bin": True,
        "real_smoke_status": "SKIP",
        "pr_body_self_report": "[x]",
        "prior_iteration_verdict": "APPROVE",
    }


def test_fixture_pass_and_real_smoke_skip_is_insufficient_for_actual_runtime_ac():
    """GIVEN #2801 相当の fixture-only regression fixture
    WHEN evidence-policy.md の instruction wiring と照合する
    THEN fixture-only 証跡（+ real smoke SKIP + PR body self-report）だけでは
    actual-runtime AC を充足しないと明記されている（AC1 / AC6 / AC7）。
    """
    policy_text = _read_evidence_policy()
    fixture = _reproduce_2801_fixture()

    # AC1: fixture-only 証跡が actual-runtime AC の evidence-source と
    # 一致しない場合の instruction wiring が明示されていること。
    assert fixture["vc_evidence_source"] == "fixture_only"
    assert fixture["ac_requires"] == "actual_canonical_compatible_proxy_selection"
    assert (
        "actual / canonical / default runtime selection" in policy_text
        or "actual/canonical/default runtime selection" in policy_text
    )
    assert "fixture-only VC が actual-runtime AC に割り当てられている場合" in policy_text
    assert "REQUEST_CHANGES" in policy_text

    # AC6: PR body の `[x]` / Safety Claim / self-report だけでは
    # AC1〜AC4 の記述上の不足を覆せないこと。
    assert fixture["pr_body_self_report"] == "[x]"
    assert (
        "PR body の `[x]` チェック、Safety Claim、self-report、"
        "および fixture-only test の PASS だけでは、actual/canonical runtime "
        "を要求する AC を APPROVE する根拠にならない" in policy_text
    )

    # AC7: #2801 / PR #2802 の observed failure class（fixture PASS +
    # real smoke SKIP のまま APPROVE した旧 iteration 1）に、評価対象の
    # evidence-policy 記述に基づけば根拠が存在しないと明記されていること。
    assert fixture["real_smoke_status"] == "SKIP"
    assert fixture["prior_iteration_verdict"] == "APPROVE"
    assert "#2801" in policy_text and "PR #2802" in policy_text
    assert "旧 iteration 1 相当の判断は、本ポリシー適用後は根拠を持たない" in policy_text
    assert (
        "fixture PASS + real smoke SKIP / environment_blocked の組み合わせは、"
        "actual-runtime AC の充足として不十分である" in policy_text
    )


def test_canonical_smoke_nonzero_result_blocks_promotion_despite_fixture_pass():
    """GIVEN canonical smoke が proxy_model_catalog_incompatible / non-zero を返す
    WHEN 同一 head の fixture compatibility tests が PASS している
    THEN evidence-policy.md の instruction wiring は actual-runtime AC を
    PASS へ昇格させないと明記している（AC4）。
    """
    policy_text = _read_evidence_policy()

    canonical_smoke_result = {
        "cause": "proxy_model_catalog_incompatible",
        "exit_code": 1,
        "status": "failed",
    }
    same_head_fixture_tests = {
        "test_proxy_model_compatibility.py": "PASS",
    }

    # AC4: canonical smoke が failed / non-zero を返した metadata を用意する。
    assert canonical_smoke_result["cause"] == "proxy_model_catalog_incompatible"
    assert canonical_smoke_result["exit_code"] != 0
    assert all(
        result == "PASS" for result in same_head_fixture_tests.values()
    )

    assert "proxy_model_catalog_incompatible" in policy_text
    assert "non-zero exit" in policy_text
    assert (
        "actual-runtime AC を PASS / ready-for-merge に **昇格させない**"
        in policy_text
    )
