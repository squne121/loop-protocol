#!/usr/bin/env python3
"""Regression tests for Issue #2807.

`body-authoring.md` の runtime AC/VC authoring guidance が、actual/canonical/
default runtime acceptance と fixture semantics を区別し、両方が必要な場合は
別々の AC/VC に分離する規則を明記していることを固定する static regression test。
"""

from pathlib import Path

BODY_AUTHORING_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "body-authoring.md"
)


def _read_body_authoring() -> str:
    assert BODY_AUTHORING_PATH.is_file(), (
        f"body-authoring.md not found at {BODY_AUTHORING_PATH}"
    )
    return BODY_AUTHORING_PATH.read_text(encoding="utf-8")


def test_actual_runtime_ac_cannot_be_satisfied_by_fixture_only_vc():
    """GIVEN body-authoring.md の runtime AC/VC authoring guidance
    WHEN actual/canonical/default runtime selection を要求する AC を起票する
    THEN fixture-only VC を唯一の evidence にせず、fixture semantics AC と
    canonical runtime acceptance AC を別々の AC/VC に分離する規則が明記されて
    いる（AC2）。
    """
    doc_text = _read_body_authoring()

    # AC2: actual/canonical/default runtime acceptance と fixture
    # semantics を区別する規則そのものが存在すること。
    assert "Actual/Canonical Runtime Acceptance と Fixture Semantics の AC 分離" in doc_text
    assert (
        "fixture proxy を明示注入する hermetic test（fixture-only VC）を、"
        "その AC の唯一の evidence にしてはならない" in doc_text
    )

    # 2種類の AC が明示的に定義されていること。
    assert "fixture semantics AC" in doc_text
    assert "canonical runtime acceptance AC" in doc_text
    assert (
        "catalog mismatch / repair / precedence 等のロジックを hermetic "
        "fixture" in doc_text
    )
    assert (
        "current-head production launcher を通常の binary resolution で "
        "external process として起動し、actual selected proxy identity と "
        "`--check-only` 相当の結果を観測する AC" in doc_text
    )

    # 両方が必要な場合は別 AC/VC に分離する規則が明記されていること。
    assert (
        "fixture semantics AC の PASS は canonical runtime acceptance AC の"
        "代替にならない" in doc_text
    )
    assert (
        "両方が必要な場合は、単一の AC/VC に混在させず、"
        "別々の AC と別々の VC に分離して起票する" in doc_text
    )

    # 静的シナリオ: 1 つの AC に fixture-only VC と real-smoke SKIP を
    # 同居させる #2801 型パターンが、分離規則違反として明記されていること。
    offending_pattern = {
        "ac_count": 1,
        "vc_evidence_sources": ["fixture_only"],
        "real_smoke": "SKIP",
    }
    assert offending_pattern["ac_count"] == 1
    assert offending_pattern["vc_evidence_sources"] == ["fixture_only"]
    assert (
        "1つの AC に fixture-only VC と「real smoke は SKIP でよい」という"
        "記述を同居させ、それを canonical runtime acceptance の充足として"
        "扱わない" in doc_text
    )
