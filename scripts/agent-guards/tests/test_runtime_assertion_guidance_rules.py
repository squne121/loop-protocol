"""Issue #2852 AC11: the author-facing guidance (`body-authoring.md`) and the
policy documentation (`runtime-verification-policy.md`) must state the same
assertion-level applicability rules the shared evaluator enforces, and the old
escape valve must not survive as a generation rule.

Besides text assertions, the documented example is parsed with the REAL
`parse_runtime_assertion_bindings()` and the documented key sets are compared
with the evaluator's closed key sets, so the guidance cannot drift from the
checker.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import extension_surface_policy_matcher as matcher  # noqa: E402

BODY_AUTHORING = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "references" / "body-authoring.md"
POLICY_DOC = _REPO_ROOT / "docs" / "dev" / "runtime-verification-policy.md"

_DISPOSITIONS = ("dispositive", "non_dispositive_readiness_compat", "not_applicable")


@pytest.fixture(scope="module")
def authoring() -> str:
    return BODY_AUTHORING.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def policy() -> str:
    return POLICY_DOC.read_text(encoding="utf-8")


def _binding_section(text: str) -> str:
    start = text.index("### 実行時検証プロファイルの assertion binding 記法")
    end = text.index("## VC 作成ガイダンス")
    return text[start:end]


# --- the old escape valve / 3-field-only canonical must be gone ------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "escape valve",
        "未解決時の",
        "と **同一の** `ac` へ bind してよい",
        "同一の `ac` へ bind してよい",
        "`profile` / `assertion` / `ac` の3フィールドのみ",
        "3フィールドのみ",
        "output schema を実証できない",
    ],
)
def test_old_escape_valve_and_three_field_only_canonical_are_not_generation_rules(
    forbidden, authoring, policy
):
    """GIVEN the guidance documents
    WHEN searched for the retired escape valve / 3-field-only wording
    THEN none of it remains in either document."""
    assert forbidden not in authoring
    assert forbidden not in policy


# --- ordered generation rule -----------------------------------------------


def test_generation_rule_is_ordered_applicability_first_then_destination(authoring):
    """GIVEN the binding section of body-authoring.md
    WHEN the six generation steps are located
    THEN they appear in the contract order (policy candidate -> applicability -> destination ->
    compat -> not_applicable -> never a dummy AC)."""
    section = _binding_section(authoring)
    markers = [
        "1. policy から candidate assertion を得る",
        "2. candidate ごとに、今回の change surface に対する applicability を分類する",
        "3. applicable なものだけ、substantive verification の宛先（`ac`）を結ぶ",
        "4. 別の既存 AC が substantive verification を所有する場合に限り `non_dispositive_readiness_compat`",
        "5. 対象の振る舞い自体が今回の change surface に無い場合は、`not_applicable` と理由",
        "6. 構造上の完全性を作るためだけに、dummy AC",
    ]
    positions = [section.index(marker) for marker in markers]
    assert positions == sorted(positions)
    # applicability is classified BEFORE any destination is bound
    assert section.index("applicability を分類する") < section.index("宛先（`ac`）を結ぶ")


def test_dummy_ac_is_prohibited_and_invalid_not_applicable_reasons_are_listed(authoring):
    """GIVEN the binding section
    WHEN read
    THEN it prohibits dummy AC / artificial evidence and lists reasons that are NOT valid."""
    section = _binding_section(authoring)
    for token in ("dummy AC", "ダミー AC", "人工的な SubAgent", "空の runtime log", "無関係な marker"):
        assert token in section, token
    reason_sentence = next(
        line for line in section.splitlines() if line.startswith("`not_applicable` の `reason` に使えない理由")
    )
    for token in ("runner が無い", "認証できない", "test が未実装", "理由にならない"):
        assert token in reason_sentence, token
    assert "unverified" in reason_sentence


# --- documented notation is what the evaluator really accepts --------------


def _documented_binding_yaml(authoring: str) -> str:
    section = _binding_section(authoring)
    blocks = re.findall(r"```yaml\n(.*?)```", section, re.DOTALL)
    candidates = [b for b in blocks if "runtime_assertion_bindings:" in b]
    assert len(candidates) == 1
    return candidates[0]


def test_documented_example_parses_with_the_real_parser_into_the_three_dispositions(authoring):
    """GIVEN the YAML example in body-authoring.md
    WHEN parsed by the evaluator's real parser
    THEN there is no malformed entry and exactly the three dispositions are represented."""
    bindings, malformed = matcher.parse_runtime_assertion_bindings(_documented_binding_yaml(authoring))
    assert malformed == []
    assert [b.get("disposition") for b in bindings] == list(_DISPOSITIONS)
    compat = bindings[1]
    assert compat["demonstrated_by"] == "18" and "ac" not in compat and compat["reason"]
    not_applicable = bindings[2]
    assert "ac" not in not_applicable and "demonstrated_by" not in not_applicable and not_applicable["reason"]


def test_documented_key_sets_match_the_evaluator_closed_key_sets(policy):
    """GIVEN the disposition table in runtime-verification-policy.md
    WHEN each row's key set is compared with the evaluator's closed key sets
    THEN they are identical (the doc cannot drift from the checker)."""
    rows = {}
    for line in policy.splitlines():
        match = re.match(r"^\| `(?P<disposition>[a-z_]+)` \| .* \| (?P<keys>`profile`.*) \|$", line)
        if match and match["disposition"] in _DISPOSITIONS:
            keys = set(re.findall(r"`([a-z_]+)`", match["keys"].split("（")[0]))
            rows[match["disposition"]] = keys
    assert set(rows) == set(_DISPOSITIONS)
    for disposition, keys in rows.items():
        assert frozenset(keys) == matcher._RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS_BY_DISPOSITION[disposition]


# --- policy document -------------------------------------------------------


def test_policy_doc_states_declaration_rules_and_invariants(policy):
    """GIVEN the #2771 section of runtime-verification-policy.md
    WHEN read
    THEN it documents the 3 dispositions, the legacy default, demonstrated_by and the invariants."""
    start = policy.index("### profile の assertion binding における完全性の確認")
    end = policy.index("## 12. live runtime verification")
    section = policy[start:end]
    for disposition in _DISPOSITIONS:
        assert f"`{disposition}`" in section
    for token in (
        "legacy_default",
        "demonstrated_by",
        "disposition_source: explicit",
        "runtime PASS 根拠を表す field は一切持たない",
        "structural checker の PASS は runtime verification の PASS ではない",
        "`dispositive` の宣言は実行済み / 観測済みを意味しない",
        "`non_dispositive_readiness_compat` は runtime PASS ではない",
        "`not_applicable` は runtime PASS ではなく",
        "semantic sufficiency の証明ではない",
        "unverified であり、",
        "runner が無い",
        "evaluate_issue_risk_trigger()",
        "#2775",
        "#2841",
    ):
        assert token in section, token
