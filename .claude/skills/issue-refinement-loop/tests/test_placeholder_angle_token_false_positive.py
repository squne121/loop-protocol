"""
test_placeholder_angle_token_false_positive.py

Issue #3015: `repair_issue_contract.py` の placeholder 判定が、正当な値に含まれる
山括弧記述（例: goal_ref 内の `sha256:<hex>`）を未記入 placeholder と誤判定しないこと、
かつ真の template 由来 placeholder の検出を維持することを固定する。

hermetic な in-process pytest。実 template は `.github/ISSUE_TEMPLATE` を読み取り専用で参照する。

Verification Commands の node 名と対応:
  AC1: test_legit_angle_description_not_placeholder
  AC2: test_template_default_placeholder_still_detected
  AC3: test_confirmed_template_default_not_placeholder
  AC4: test_production_path_legit_angle_routing
  AC5: test_production_path_true_blocker_preserved
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SCRIPTS_DIR))

import repair_issue_contract as ric  # noqa: E402

TEMPLATE_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
KINDS = ("implementation", "parent", "research")

MRC_GOAL_REF_ID = "machine-readable-contract.goal_ref"

# 実在する値に山括弧記述が含まれる正当な goal_ref（編集・書換えしてテストを通さない）。
LEGIT_GOAL_REFS = [
    "検証用 digest を sha256:<hex> 形式で比較し、<hex> 単体の記述も許容する",
    "<親 Goal> 補足: 親トラッカーの目的を前進させる",
]

# 各 kind の「実値」MRC（山括弧なし）。
_REAL_VALUES = {
    "implementation": {
        "contract_schema_version": "v1",
        "issue_kind": "implementation",
        "parent_issue": "none",
        "goal_ref": "実在する目的の記述",
        "change_kind": "code",
    },
    "parent": {
        "contract_schema_version": "v1",
        "issue_kind": "parent",
        "goal_ref": "実在する目的の記述",
        "change_kind": "workflow",
        "parent_mode": "delivery-rollup",
        "closure_mode": "child-complete",
    },
    "research": {
        "contract_schema_version": "v1",
        "issue_kind": "research",
        "parent_issue": "none",
        "goal_ref": "実在する目的の記述",
        "change_kind": "research-only",
    },
}


def _template_text(kind: str) -> str:
    return (TEMPLATE_DIR / f"{kind}.yml").read_text(encoding="utf-8")


def _template_path(kind: str) -> str:
    return str(TEMPLATE_DIR / f"{kind}.yml")


def _template_mrc_value(kind: str) -> str:
    field = next(
        f
        for f in ric.parse_issue_template_fields(_template_text(kind), _template_path(kind))
        if f["field_id"] == "machine-readable-contract"
    )
    return field["value"]


def _template_default(kind: str, key: str) -> str:
    block = _template_mrc_value(kind)
    m = re.search(r"```ya?ml\n(.*?)```", block, re.DOTALL)
    assert m, f"{kind}: template MRC fence not found"
    return yaml.safe_load(m.group(1))[key]


def _build_body(kind: str, mrc_overrides: dict | None = None, drop_keys: tuple = ()) -> str:
    """実 template の required section 全てを非空で含み、MRC のみ指定値に差し替えた本文を作る。"""
    mrc = dict(_REAL_VALUES[kind])
    mrc.update(mrc_overrides or {})
    for key in drop_keys:
        mrc.pop(key, None)
    lines = ["## Machine-Readable Contract", "", "```yaml"]
    for key, value in mrc.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines += ["```", ""]
    for field in ric.parse_issue_template_fields(_template_text(kind), _template_path(kind)):
        if not field["required"] or field["field_id"] == "machine-readable-contract":
            continue
        lines += [f"## {field['label']}", "", "実在する本文", ""]
    return "\n".join(lines)


def _items(body: str, kind: str, **kwargs) -> list[dict]:
    return ric.detect_missing_template_sections(
        body,
        issue_kind=kind,
        template_text=_template_text(kind),
        template_path=_template_path(kind),
        **kwargs,
    )


def _bundle(body: str, kind: str, **kwargs) -> dict:
    return ric.build_structural_repair_bundle(
        body,
        issue_kind=kind,
        template_text=_template_text(kind),
        template_path=_template_path(kind),
        **kwargs,
    )


def _mrc_item_ids(items: list[dict]) -> set[str]:
    return {i["field_id"] for i in items if i["field_id"].startswith("machine-readable-contract.")}


def _item(items: list[dict], field_id: str) -> dict | None:
    return next((i for i in items if i["field_id"] == field_id), None)


@pytest.fixture
def disable_angle_detection(monkeypatch):
    """山括弧判定だけを無効化する（検出要因の分離確認用）。"""
    monkeypatch.setattr(ric, "_PLACEHOLDER_ANGLE_TOKEN_RE", re.compile(r"(?!x)x"))


# ---------------------------------------------------------------------------
# AC1: 正当な山括弧記述は placeholder にしない（behavioral RED）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("goal_ref", LEGIT_GOAL_REFS)
def test_legit_angle_description_not_placeholder(kind, goal_ref):
    body = _build_body(kind, {"goal_ref": goal_ref})
    items = _items(body, kind)
    assert _item(items, MRC_GOAL_REF_ID) is None, items
    assert items == [], items


# ---------------------------------------------------------------------------
# AC2: 既存 placeholder 検出の維持（要因を 1 つずつ分離）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_template_default_placeholder_still_detected(kind, monkeypatch):
    """(a) template 既定の goal_ref だけが未記入で他 key は実値。"""
    default = _template_default(kind, "goal_ref")
    body = _build_body(kind, {"goal_ref": default})
    items = _items(body, kind)
    assert _mrc_item_ids(items) == {MRC_GOAL_REF_ID}, items
    assert _item(items, MRC_GOAL_REF_ID)["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    # 山括弧判定が検出要因であること（無効化すると検出されない）。
    monkeypatch.setattr(ric, "_PLACEHOLDER_ANGLE_TOKEN_RE", re.compile(r"(?!x)x"))
    assert _mrc_item_ids(_items(body, kind)) == set()


@pytest.mark.parametrize("kind", ["implementation", "research"])
@pytest.mark.parametrize("value", ["#<parent-issue-number>|none", "#<parent-issue-number>"])
def test_template_default_placeholder_still_detected_b_parent_issue(kind, value, monkeypatch):
    """(b) parent_issue だけが scaffold のまま（完全一致 default / 一部だけ残った値）。change_kind は実値。"""
    body = _build_body(kind, {"parent_issue": value})
    items = _items(body, kind)
    assert _mrc_item_ids(items) == {"machine-readable-contract.parent_issue"}, items
    assert _item(items, "machine-readable-contract.parent_issue")["disposition"] == (
        ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    )
    # 山括弧判定が検出要因であること（無効化すると検出されない）。
    monkeypatch.setattr(ric, "_PLACEHOLDER_ANGLE_TOKEN_RE", re.compile(r"(?!x)x"))
    assert _mrc_item_ids(_items(body, kind)) == set()


@pytest.mark.parametrize("key", ["parent_mode", "closure_mode"])
def test_template_default_placeholder_still_detected_c_required_token(key):
    """(c) parent の parent_mode / closure_mode が `<required: ...>` のまま。
    これは別経路 `_REQUIRED_PLACEHOLDER_RE` による検出の維持テストであり、
    山括弧判定の回帰検出の担保には数えない。"""
    default = _template_default("parent", key)
    assert ric._REQUIRED_PLACEHOLDER_RE.match(default.strip())
    body = _build_body("parent", {key: default})
    items = _items(body, "parent")
    assert _mrc_item_ids(items) == {f"machine-readable-contract.{key}"}, items
    assert _item(items, f"machine-readable-contract.{key}")["disposition"] == (
        ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    )


def _candidate_with_enum_resolved(kind: str) -> str:
    text = _template_mrc_value(kind)
    return text.replace("change_kind: docs|code|mixed|workflow|data", "change_kind: code")


def test_template_default_placeholder_still_detected_d_candidate_without_enum(monkeypatch):
    """(d) 補完候補全文に山括弧 scaffold だけが残り、change_kind の enum は実値に確定済み。"""
    candidate = _candidate_with_enum_resolved("implementation")
    assert "docs|code" not in candidate
    assert "<parent-issue-number>" in candidate
    field = {"field_id": "machine-readable-contract", "value": candidate}
    result = ric._classify_missing_field(field, {}, {})
    assert result["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert result.get("derivation") != ric.DERIVATION_TEMPLATE_VALUE_EXACT
    # 山括弧判定を無効化すると template_value_exact になる = enum 検出は要因ではない。
    monkeypatch.setattr(ric, "_PLACEHOLDER_ANGLE_TOKEN_RE", re.compile(r"(?!x)x"))
    broken = ric._classify_missing_field(field, {}, {})
    assert broken["derivation"] == ric.DERIVATION_TEMPLATE_VALUE_EXACT


def test_template_default_placeholder_still_detected_e_research_default_mrc(monkeypatch):
    """(e) enum を持たない research の既定 MRC 全文。"""
    candidate = _template_mrc_value("research")
    field = {"field_id": "machine-readable-contract", "value": candidate}
    result = ric._classify_missing_field(field, {}, {})
    assert result["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert result.get("derivation") != ric.DERIVATION_TEMPLATE_VALUE_EXACT
    monkeypatch.setattr(ric, "_PLACEHOLDER_ANGLE_TOKEN_RE", re.compile(r"(?!x)x"))
    broken = ric._classify_missing_field(field, {}, {})
    assert broken["derivation"] == ric.DERIVATION_TEMPLATE_VALUE_EXACT


@pytest.mark.parametrize("kind", ["implementation", "research"])
def test_template_default_placeholder_still_detected_f_closed_key_partial_angle(kind, monkeypatch):
    """(f) goal_ref 以外の closed-format key（change_kind）に山括弧を部分的に含む値。"""
    body = _build_body(kind, {"change_kind": "<x> code"})
    items = _items(body, kind)
    assert _mrc_item_ids(items) == {"machine-readable-contract.change_kind"}, items
    assert _item(items, "machine-readable-contract.change_kind")["disposition"] == (
        ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    )
    monkeypatch.setattr(ric, "_PLACEHOLDER_ANGLE_TOKEN_RE", re.compile(r"(?!x)x"))
    assert _mrc_item_ids(_items(body, kind)) == set()


def test_template_default_placeholder_still_detected_unresolvable_default_fail_closed():
    """template の同一 key 既定値を解決できない場合は現行の厳格判定へ fail-closed する。"""
    kind = "implementation"
    # MRC fence を持たない template を渡し、goal_ref の既定値を解決不能にする。
    template_text = _template_text(kind).replace("```yaml", "```text")
    body = _build_body(kind, {"goal_ref": "sha256:<hex> を含む説明"})
    items = ric.detect_missing_template_sections(
        body,
        issue_kind=kind,
        template_text=template_text,
        template_path=_template_path(kind),
    )
    assert MRC_GOAL_REF_ID in _mrc_item_ids(items), items


# ---------------------------------------------------------------------------
# AC3: 確定済み default（contract_schema_version: v1 等）は placeholder にしない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_confirmed_template_default_not_placeholder(kind):
    assert ric._contains_placeholder_scaffold("v1") is False
    block = _template_mrc_value(kind)
    assert ric._extract_template_declared_scalar(block, "contract_schema_version") == "v1"

    # contract_schema_version だけ欠落していても template_value_exact による補完が従来どおり可能。
    # （維持テストのため、山括弧判定の修正有無に依存しない実値 goal_ref を使う。）
    body = _build_body(kind, drop_keys=("contract_schema_version",))
    items = _items(body, kind)
    assert _mrc_item_ids(items) == {"machine-readable-contract.contract_schema_version"}, items
    item = _item(items, "machine-readable-contract.contract_schema_version")
    assert item["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert item["derivation"] == ric.DERIVATION_TEMPLATE_VALUE_EXACT
    assert item["candidate_value"] == "v1"

    # `v1` が MRC に実在する場合は item を生成しない。
    present = _items(_build_body(kind), kind)
    assert present == []


# ---------------------------------------------------------------------------
# AC4: production 経路（実 MRC parser -> detect/bundle -> route）の結合検証
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("goal_ref", LEGIT_GOAL_REFS)
def test_production_path_legit_angle_routing_i_no_repair_item(kind, goal_ref):
    """(i) 説明用の山括弧を含む正当な本文は repair item なしで pass / proceed。"""
    body = _build_body(kind, {"goal_ref": goal_ref})
    bundle = _bundle(body, kind)
    assert bundle["items"] == [], bundle["items"]
    assert bundle["disposition_summary"] == "no_missing_fields_detected"
    route = ric.route_structural_repair_disposition(bundle)
    assert route["status"] == "pass"
    assert route["next_action"] == "proceed"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("goal_ref", LEGIT_GOAL_REFS)
def test_production_path_legit_angle_routing_ii_true_blocker_only(kind, goal_ref):
    """(ii) goal_ref の山括弧記述を残したまま、known_scalars で導出されない別 key
    (`change_kind`) だけを欠落させる。item は欠落させた key のみで goal_ref を含まず、
    route は blocked / human_judgment_required。"""
    body = _build_body(kind, {"goal_ref": goal_ref}, drop_keys=("change_kind",))
    bundle = _bundle(body, kind)
    ids = {i["field_id"] for i in bundle["items"]}
    assert ids == {"machine-readable-contract.change_kind"}, ids
    assert MRC_GOAL_REF_ID not in ids
    assert bundle["disposition_summary"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    route = ric.route_structural_repair_disposition(bundle)
    assert route["status"] == "blocked"
    assert route["next_action"] == "human_judgment_required"


# ---------------------------------------------------------------------------
# AC5: 真の blocker の維持（山括弧説明文を含まない fixture）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_production_path_true_blocker_preserved(kind):
    body = _build_body(kind, drop_keys=("goal_ref",))
    assert "<" not in body
    bundle = _bundle(body, kind)
    ids = {i["field_id"] for i in bundle["items"]}
    assert ids == {MRC_GOAL_REF_ID}, ids
    assert bundle["items"][0]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    route = ric.route_structural_repair_disposition(bundle)
    assert route["status"] == "blocked"
    assert route["next_action"] == "human_judgment_required"


# ---------------------------------------------------------------------------
# P3-1: `_is_placeholder_mrc_scalar()` の 3 つの placeholder 判定分岐を独立に固定する
#   A: 単一山括弧トークン全体一致 / B: 未選択 enum / C: template default との完全一致
# いずれも template default が value と異なる（または A/B が他分岐に依存しない）入力で、
# 各分岐が他の分岐に依存せず単独で placeholder と判定されることを確認する。
# ---------------------------------------------------------------------------


def test_placeholder_branch_a_single_angle_token_independent_of_default():
    value = "<独自の未記入項目>"
    default = "正規の既定値"
    assert value != default
    assert ric._is_placeholder_mrc_scalar("goal_ref", value, default) is True


def test_placeholder_branch_b_unselected_enum_independent_of_angle_and_default():
    value = "docs|code"
    default = "正規の既定値"
    assert value != default
    assert ric._PLACEHOLDER_ANGLE_TOKEN_RE.search(value) is None
    assert ric._is_placeholder_mrc_scalar("goal_ref", value, default) is True


def test_placeholder_branch_c_template_default_exact_match_composite():
    default = "親: <未記入> / 子: <未記入>"
    # 単一トークン全体一致では検出できない構造であること（分岐 A 非依存）。
    assert ric._PLACEHOLDER_ANGLE_TOKEN_RE.fullmatch(default.strip()) is None
    assert ric._contains_unselected_enum_scaffold(default) is False
    assert ric._is_placeholder_mrc_scalar("goal_ref", f"  {default}  ", default) is True


def test_placeholder_branches_negative_control_partial_angle_is_legit():
    default = "親: <未記入> / 子: <未記入>"
    value = "検証用 digest を sha256:<hex> 形式で比較する"
    assert ric._is_placeholder_mrc_scalar("goal_ref", value, default) is False
