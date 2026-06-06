"""
Tests for termination-policy.md contract compliance.

SSOT: .claude/skills/issue-refinement-loop/references/termination-policy.md

AC11 (Issue #647): LOOP_HANDOFF_RESULT_V1 の 4 フィールド
  - checked_body_sha256
  - checker_exit_code
  - missing_sections
  - missing_contract_keys

が termination-policy.md (SSOT) 内の LOOP_HANDOFF_RESULT_V1 セクションに定義されていること、
および loop_handoff_result_v1.json の JSON Schema にも宣言されていることを検証する。
"""

import json
import pathlib
import re

POLICY_PATH = (
    pathlib.Path(__file__).parent.parent / "references" / "termination-policy.md"
)

SCHEMA_PATH = (
    pathlib.Path(__file__).parent.parent / "schemas" / "loop_handoff_result_v1.json"
)

# AC11 で定義が必要な 4 フィールド
AC11_FIELDS = [
    "checked_body_sha256",
    "checker_exit_code",
    "missing_sections",
    "missing_contract_keys",
]


def _read_policy() -> str:
    return POLICY_PATH.read_text(encoding="utf-8")


def _extract_loop_handoff_section(content: str) -> str:
    """termination-policy.md から LOOP_HANDOFF_RESULT_V1 セクション本文を抽出する。"""
    # "## LOOP_HANDOFF_RESULT_V1" セクションの開始から次の "## " まで
    match = re.search(
        r"## LOOP_HANDOFF_RESULT_V1.*?(?=\n## |\Z)",
        content,
        re.DOTALL,
    )
    if match is None:
        return ""
    return match.group(0)


def test_loop_handoff_result_v1_ac11_fields_are_in_ssot() -> None:
    """
    LOOP_HANDOFF_RESULT_V1 セクション内に AC11 の 4 フィールドが定義されていることを検証する。

    Checks:
    - termination-policy.md が存在する
    - LOOP_HANDOFF_RESULT_V1 セクションが存在する
    - 4 フィールド全てがセクション内に記述されている
    """
    assert POLICY_PATH.exists(), (
        f"termination-policy.md が見つかりません: {POLICY_PATH}"
    )

    content = _read_policy()
    section = _extract_loop_handoff_section(content)

    assert section, (
        "termination-policy.md に '## LOOP_HANDOFF_RESULT_V1' セクションが見つかりません"
    )

    missing = [field for field in AC11_FIELDS if field not in section]
    assert not missing, (
        f"LOOP_HANDOFF_RESULT_V1 セクションに以下の AC11 フィールドが見つかりません: {missing}\n"
        f"セクション内容（先頭 500 文字）:\n{section[:500]}"
    )


def test_loop_handoff_json_schema_declares_ac11_fields() -> None:
    """
    loop_handoff_result_v1.json の LOOP_HANDOFF_RESULT_V1 properties に
    AC11 の 4 フィールドが宣言されていることを検証する。

    注: 4 フィールドは optional（required 外）とする。
    既存フィクスチャとの後方互換を維持するため required には含めない。

    Checks:
    - schemas/loop_handoff_result_v1.json が存在する
    - LOOP_HANDOFF_RESULT_V1.properties に 4 フィールドが含まれる
    """
    assert SCHEMA_PATH.exists(), (
        f"loop_handoff_result_v1.json が見つかりません: {SCHEMA_PATH}"
    )

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    handoff_schema = schema["properties"]["LOOP_HANDOFF_RESULT_V1"]
    properties = handoff_schema.get("properties", {})

    missing_from_properties = [f for f in AC11_FIELDS if f not in properties]
    assert not missing_from_properties, (
        f"LOOP_HANDOFF_RESULT_V1.properties に以下の AC11 フィールドが見つかりません: "
        f"{missing_from_properties}"
    )


def test_termination_policy_references_664_for_runtime_enforcement() -> None:
    """
    runtime enforcement は #664 の責務である旨が termination-policy.md に明記されていることを検証する。
    """
    assert POLICY_PATH.exists(), (
        f"termination-policy.md が見つかりません: {POLICY_PATH}"
    )

    content = _read_policy()
    assert "#664" in content, (
        "termination-policy.md に '#664' への参照が見つかりません。"
        "runtime enforcement は #664 の責務である旨が明記されている必要があります。"
    )
