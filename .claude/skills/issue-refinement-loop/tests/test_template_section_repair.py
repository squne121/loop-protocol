"""
test_template_section_repair.py

Issue #995: repair_issue_contract.py の structural-repair 拡張
(template-derived required section / Machine-Readable Contract key の
detector・classifier・proposal producer)を検証する。

8 つの関数は Issue #995 の `## Verification Commands` に列挙された名前と
一致させ、pytest node-id が Issue 本文と一致するようにトップレベル関数と
する:
  AC1: test_required_fields_include_id_value_order_and_template_digest
  AC2: test_all_missing_required_fields_are_reported_in_one_batch
  AC3: test_semantic_field_without_exact_source_requires_human_review
  AC4: test_exact_template_value_can_be_auto_apply_safe
  AC5: test_duplicate_heading_or_conflicting_sources_are_ambiguous
  AC6: test_structural_repair_provenance_validates_against_schema
  AC7: test_producer_path_does_not_invoke_github_mutation
  AC8: test_2039_opaque_action_compatibility_does_not_claim_transaction
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SCRIPTS_DIR))

import repair_issue_contract as ric  # noqa: E402


TEMPLATE_PATH = str(REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "implementation.yml")
TEMPLATE_TEXT = Path(TEMPLATE_PATH).read_text(encoding="utf-8")


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fixture bodies
# ---------------------------------------------------------------------------

BODY_MISSING_MANY_REQUIRED_FIELDS = """\
## Machine-Readable Contract

```yaml
issue_kind: implementation
parent_issue: "none"
goal_ref: "g"
change_kind: code
```

## Outcome

## Verification Commands

- `pnpm test`
"""

BODY_DUPLICATE_OUTCOME = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "g"
change_kind: code
```

## Outcome

first outcome text

## Verification Commands

- `pnpm test`

## Outcome

second outcome text (duplicate heading)
"""

BODY_PLACEHOLDER_ONLY_SECTION = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "g"
change_kind: code
```

## Parent Issue

#42 または none

## Verification Commands

- `pnpm test`
"""

BODY_ALL_PRESENT_NON_EMPTY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "g"
change_kind: code
```

## Parent Issue

none

## Parent Goal Ref

- Goal: g

## Current Validated Scope

- x

## Remaining Parent Gaps

なし

## Outcome

Some outcome.

## Runtime Verification Applicability

- decision: not_applicable

## In Scope

- x

## Out of Scope

- y

## Acceptance Criteria

- [ ] AC1

## Verification Commands

- `pnpm test`

## Allowed Paths

- `foo.py`

## Stop Conditions

- none

## Required Skills

- なし
"""


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------


def test_required_fields_include_id_value_order_and_template_digest():
    fields = ric.parse_issue_template_fields(TEMPLATE_TEXT, TEMPLATE_PATH)
    by_id = {f["field_id"]: f for f in fields}

    assert by_id["machine-readable-contract"]["order"] == 0
    assert by_id["stop-conditions"]["order"] > by_id["outcome"]["order"]

    for field in fields:
        assert field["template_path"] == TEMPLATE_PATH
        assert field["template_digest"] == "sha256:" + __import__("hashlib").sha256(
            TEMPLATE_TEXT.encode("utf-8")
        ).hexdigest()
        assert isinstance(field["order"], int)
        assert isinstance(field["required"], bool)

    # A field with a real template default value is captured byte-exact.
    vc_field = by_id["verification-commands"]
    assert vc_field["value"] is not None
    assert "pnpm typecheck" in vc_field["value"]

    # A field with only a `placeholder:` authoring hint has no `value`.
    parent_issue_field = by_id["parent-issue"]
    assert parent_issue_field["value"] is None
    assert parent_issue_field["required"] is True

    # issue_kind SSOT authoring keys (create-issue/references/body-authoring.md).
    assert ric.REQUIRED_CONTRACT_KEYS_BY_KIND["implementation"] == [
        "contract_schema_version", "issue_kind", "parent_issue", "goal_ref",
        "change_kind",
    ]


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------


def test_all_missing_required_fields_are_reported_in_one_batch():
    items = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )

    field_ids = [i["field_id"] for i in items]

    # All missing required sections are reported (not just the first one).
    assert "parent-issue" in field_ids
    assert "parent-goal-ref" in field_ids
    assert "current-validated-scope" in field_ids
    assert "remaining-parent-gaps" in field_ids
    assert "in-scope" in field_ids
    assert "out-of-scope" in field_ids
    assert "acceptance-criteria" in field_ids
    assert "allowed-paths" in field_ids
    # heading present but empty -- heading set membership alone must not
    # suppress detection.
    assert "outcome" in field_ids

    # "verification-commands" section IS present and non-empty: not reported.
    assert "verification-commands" not in field_ids

    # Deterministic ordering: template field order, then field id.
    ordered_pairs = [
        (i["template_field_order"], i["field_id"])
        for i in items
    ]
    assert ordered_pairs == sorted(ordered_pairs)

    # Re-running detection on the same body is idempotent (same batch).
    items_again = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    assert [i["field_id"] for i in items_again] == field_ids


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------


def test_semantic_field_without_exact_source_requires_human_review():
    items = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    by_id = {i["field_id"]: i for i in items}

    # "Outcome" is a free-form semantic field with no exact source: must be
    # human_review_required, and no prose is fabricated (no candidate_value).
    outcome_item = by_id["outcome"]
    assert outcome_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert outcome_item["derivation"] is None
    assert "candidate_value" not in outcome_item

    # "Acceptance Criteria" has `validations.required: true` but no template
    # `value:` default -- required-alone must not be treated as a safe
    # source of auto-generated prose.
    ac_item = by_id["acceptance-criteria"]
    assert ac_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert ac_item["derivation"] is None
    assert "candidate_value" not in ac_item


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------


def test_exact_template_value_can_be_auto_apply_safe():
    items = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    by_id = {i["field_id"]: i for i in items}

    template_fields = {
        f["field_id"]: f for f in ric.parse_issue_template_fields(TEMPLATE_TEXT, TEMPLATE_PATH)
    }

    # "Machine-Readable Contract" key -- wait, MRC section IS present in this
    # fixture (Pass): use the MRC required-key gap instead, which is missing
    # `contract_schema_version` in this fixture.
    key_item = by_id["machine-readable-contract.contract_schema_version"]
    assert key_item["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert key_item["derivation"] == ric.DERIVATION_TEMPLATE_VALUE_EXACT
    assert key_item["derivation"] in ric.CLOSED_DERIVATION_MODES
    assert key_item["candidate_value"] == "v1"
    assert key_item["candidate_digest"] == "sha256:" + __import__("hashlib").sha256(
        b"v1"
    ).hexdigest()

    # Missing entire "Stop Conditions" section: template ships a real
    # non-placeholder default -> byte-exact template_value_exact.
    body_missing_stop_conditions = BODY_ALL_PRESENT_NON_EMPTY.replace(
        "## Stop Conditions\n\n- none\n\n", ""
    )
    items2 = ric.detect_missing_template_sections(
        body_missing_stop_conditions,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    by_id2 = {i["field_id"]: i for i in items2}
    stop_item = by_id2["stop-conditions"]
    assert stop_item["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert stop_item["derivation"] == ric.DERIVATION_TEMPLATE_VALUE_EXACT
    assert stop_item["candidate_value"] == template_fields["stop-conditions"]["value"]

    # source_span_exact: a single authoritative source span supplied for a
    # semantic field.
    items3 = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        source_spans={
            "parent-goal-ref": {
                "text": "- Goal: g\n- Desired Destination: N/A",
                "source_url": "https://github.com/o/r/issues/1#comment",
                "line_start": 10,
                "line_end": 11,
            },
        },
    )
    by_id3 = {i["field_id"]: i for i in items3}
    span_item = by_id3["parent-goal-ref"]
    assert span_item["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert span_item["derivation"] == ric.DERIVATION_SOURCE_SPAN_EXACT
    assert span_item["candidate_value"] == "- Goal: g\n- Desired Destination: N/A"
    assert span_item["source_url"] == "https://github.com/o/r/issues/1#comment"

    # derived_scalar_exact: a validated syntactic scalar (parent issue #).
    items4 = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        known_scalars={"parent-issue": "#42"},
    )
    by_id4 = {i["field_id"]: i for i in items4}
    scalar_item = by_id4["parent-issue"]
    assert scalar_item["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert scalar_item["derivation"] == ric.DERIVATION_DERIVED_SCALAR_EXACT
    assert scalar_item["candidate_value"] == "#42"

    # A prose-shaped "scalar" (multi-word, long) is rejected -- never treated
    # as a syntactically unique scalar.
    items5 = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        known_scalars={"parent-issue": "this is prose, not a scalar, do not accept"},
    )
    by_id5 = {i["field_id"]: i for i in items5}
    assert by_id5["parent-issue"]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------


def test_duplicate_heading_or_conflicting_sources_are_ambiguous():
    # Duplicate heading.
    dup_items = ric.detect_missing_template_sections(
        BODY_DUPLICATE_OUTCOME,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    dup_by_id = {i["field_id"]: i for i in dup_items}
    outcome_dup = dup_by_id["outcome"]
    assert outcome_dup["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert outcome_dup["observed_cardinality"] == 2
    assert outcome_dup["expected_cardinality"] == 1
    assert "duplicate_heading" in outcome_dup["reason_codes"]

    # A safe item (missing "Stop Conditions" -- template value exact) still
    # appears in the SAME batch alongside the unsafe duplicate heading.
    mixed_body = BODY_DUPLICATE_OUTCOME  # has no Stop Conditions section
    mixed_items = ric.detect_missing_template_sections(
        mixed_body,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    mixed_by_id = {i["field_id"]: i for i in mixed_items}
    key_item = mixed_by_id["stop-conditions"]
    assert key_item["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert mixed_by_id["outcome"]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED

    # Placeholder-only section (content == template's authoring placeholder).
    placeholder_items = ric.detect_missing_template_sections(
        BODY_PLACEHOLDER_ONLY_SECTION,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    placeholder_by_id = {i["field_id"]: i for i in placeholder_items}
    parent_issue_item = placeholder_by_id["parent-issue"]
    assert parent_issue_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert "empty_or_placeholder_only_section" in parent_issue_item["reason_codes"]

    # Multiple-source conflict: two candidate spans for the same field.
    conflict_items = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        source_spans={
            "parent-goal-ref": [
                {"text": "- Goal: a", "source_url": "https://x/1"},
                {"text": "- Goal: b", "source_url": "https://x/2"},
            ],
        },
    )
    conflict_by_id = {i["field_id"]: i for i in conflict_items}
    conflict_item = conflict_by_id["parent-goal-ref"]
    assert conflict_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert "multiple_source_conflict" in conflict_item["reason_codes"]

    # A fully-present, non-empty body produces no items at all.
    clean_items = ric.detect_missing_template_sections(
        BODY_ALL_PRESENT_NON_EMPTY,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    assert clean_items == []


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------


def test_structural_repair_provenance_validates_against_schema():
    bundle = ric.build_structural_repair_bundle(
        BODY_DUPLICATE_OUTCOME,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        repo="squne121/loop-protocol",
        issue_number=995,
        original_updated_at="2026-08-16T00:00:00Z",
    )

    assert bundle["schema_version"] == "structural_repair_action/v1"
    assert bundle["policy_version"] == "template-derived-structural-repair/v1"
    assert bundle["disposition_summary"] in {
        "auto_apply_safe", "human_review_required", "no_missing_fields_detected",
    }

    # (1) repair_issue_contract_result_v1.schema.json: additive migration --
    # a base run_repair() payload extended with structural_repair_action
    # validates.
    ric_schema = _load_schema("repair_issue_contract_result_v1.schema.json")
    base_result = ric.run_repair(BODY_DUPLICATE_OUTCOME)
    payload = {**base_result, "structural_repair_action": bundle}
    jsonschema.validate(instance=payload, schema=ric_schema)

    # Backward compatibility: the pre-existing shape (WITHOUT
    # structural_repair_action) still validates -- additive, not breaking.
    jsonschema.validate(instance=base_result, schema=ric_schema)

    # (2) refinement_preflight_result_v1.schema.json: same additive field.
    preflight_schema = _load_schema("refinement_preflight_result_v1.schema.json")
    minimal_preflight = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "pass",
        "issue_number": 995,
        "repo": "squne121/loop-protocol",
        "planner_exit_code": 0,
        "planner_fail_closed": False,
        "next_action": "proceed",
        "must_read": [],
        "do_not_read": [],
        "commands": [],
        "blockers": [],
        "artifacts": {},
        "hashes": {},
    }
    jsonschema.validate(instance=minimal_preflight, schema=preflight_schema)
    jsonschema.validate(
        instance={**minimal_preflight, "structural_repair_action": bundle},
        schema=preflight_schema,
    )

    # An auto_apply_safe item without candidate_value/candidate_digest is
    # rejected by the schema (provenance integrity for the closed enum).
    invalid_bundle = json.loads(json.dumps(bundle))
    for item in invalid_bundle["items"]:
        if item["disposition"] == "auto_apply_safe":
            item.pop("candidate_value", None)
            item.pop("candidate_digest", None)
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(
                    instance={**minimal_preflight, "structural_repair_action": invalid_bundle},
                    schema=preflight_schema,
                )
            break


# ---------------------------------------------------------------------------
# AC7
# ---------------------------------------------------------------------------


def test_producer_path_does_not_invoke_github_mutation(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("structural repair producer must not shell out")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    # Exercise every public structural-repair entrypoint end-to-end.
    ric.parse_issue_template_fields(TEMPLATE_TEXT, TEMPLATE_PATH)
    ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    bundle = ric.build_structural_repair_bundle(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
    )
    assert bundle["items"]

    # Static check: none of the new structural-repair source ever references
    # `gh` / GitHub REST or GraphQL mutation call surfaces.
    for fn in (
        ric.parse_issue_template_fields,
        ric.detect_missing_template_sections,
        ric.build_structural_repair_bundle,
        ric._classify_missing_field,
        ric._classify_missing_contract_key,
    ):
        source = inspect.getsource(fn)
        assert "subprocess" not in source
        assert "gh api" not in source
        assert "gh issue" not in source
        assert "gh pr" not in source
        assert "requests." not in source
        assert "urllib" not in source


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------


def test_2039_opaque_action_compatibility_does_not_claim_transaction():
    bundle = ric.build_structural_repair_bundle(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        repo="squne121/loop-protocol",
        issue_number=995,
    )

    # #2039 (out of scope for #995) must be able to consume the bundle
    # opaquely: JSON round-trip (no Python-only objects), and read only the
    # documented top-level / item-level keys via the schema -- no transaction
    # or GitHub-mutation execution.
    round_tripped = json.loads(json.dumps(bundle))
    assert round_tripped == bundle

    def _opaque_consumer_read(payload: dict) -> list[str]:
        """Simulates a #2039-style opaque reader: only ever reads
        schema-documented fields, never executes a mutation/transaction."""
        assert payload["schema_version"] == "structural_repair_action/v1"
        safe_field_ids = []
        for item in payload["items"]:
            assert item["disposition"] in {"auto_apply_safe", "human_review_required"}
            if item["disposition"] == "auto_apply_safe":
                assert item["derivation"] in ric.CLOSED_DERIVATION_MODES
                safe_field_ids.append(item["field_id"])
        return safe_field_ids

    safe_field_ids = _opaque_consumer_read(round_tripped)
    assert isinstance(safe_field_ids, list)

    # The bundle never claims a transaction/consumer-classification outcome:
    # no key in the bundle or any item implies GitHub mutation success,
    # readback, or consumer classification.
    forbidden_substrings = (
        "applied", "mutation", "transaction", "readback", "dispatched",
        "posted", "merged", "committed",
    )
    serialized_keys = set()
    for item in bundle["items"]:
        serialized_keys.update(item.keys())
    serialized_keys.update(bundle.keys())
    for key in serialized_keys:
        lowered = key.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, f"bundle key {key!r} implies transaction/consumer outcome"

    # Schema-interface readability: validates against the additive schema
    # projection used above (AC6), independent of any GitHub state.
    preflight_schema = _load_schema("refinement_preflight_result_v1.schema.json")
    minimal_preflight = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "pass",
        "issue_number": 995,
        "repo": "squne121/loop-protocol",
        "planner_exit_code": 0,
        "planner_fail_closed": False,
        "next_action": "proceed",
        "must_read": [],
        "do_not_read": [],
        "commands": [],
        "blockers": [],
        "artifacts": {},
        "hashes": {},
        "structural_repair_action": bundle,
    }
    jsonschema.validate(instance=minimal_preflight, schema=preflight_schema)
