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
    # semantic field, WITH full provenance (Issue #995 fix_delta P0-3: a
    # `text` field alone is no longer sufficient -- authority_kind,
    # source_repo, source_object_kind, source_object_id, source_revision are
    # all mandatory before a span can back an auto_apply_safe item).
    FULL_SOURCE_SPAN = {
        "text": "- Goal: g\n- Desired Destination: N/A",
        "source_url": "https://github.com/o/r/issues/1#comment",
        "line_start": 10,
        "line_end": 11,
        "authority_kind": "parent_issue",
        "source_repo": "o/r",
        "source_object_kind": "issue_body",
        "source_object_id": "1",
        "source_revision": "abc123",
    }
    items3 = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        source_spans={"parent-goal-ref": dict(FULL_SOURCE_SPAN)},
    )
    by_id3 = {i["field_id"]: i for i in items3}
    span_item = by_id3["parent-goal-ref"]
    assert span_item["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert span_item["derivation"] == ric.DERIVATION_SOURCE_SPAN_EXACT
    assert span_item["candidate_value"] == "- Goal: g\n- Desired Destination: N/A"
    assert span_item["source_url"] == "https://github.com/o/r/issues/1#comment"
    assert span_item["source_span"]["authority_kind"] == "parent_issue"
    assert span_item["source_span"]["source_text_sha256"] == span_item["candidate_digest"]
    assert span_item["source_span"]["candidate_sha256"] == span_item["candidate_digest"]

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
    # Issue #995 fix_delta (P0-1): status/next_action MUST agree with
    # `route_structural_repair_disposition(bundle)` -- a bare "pass"/
    # "proceed" alongside a structural_repair_action that reports
    # human_review_required is exactly the contradiction the OWNER's
    # REQUEST_CHANGES flagged, and the schema now rejects it (see the
    # negative case below).
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

    route = ric.route_structural_repair_disposition(bundle)
    routed_preflight = {**minimal_preflight, "status": route["status"], "next_action": route["next_action"]}
    jsonschema.validate(
        instance={**routed_preflight, "structural_repair_action": bundle},
        schema=preflight_schema,
    )

    # Negative case (P0-1): the SAME bundle with the wrapper's status left
    # at "pass"/"proceed" (instead of the routed status) is REJECTED by the
    # schema -- this is the exact contradiction the OWNER's REQUEST_CHANGES
    # flagged, and it must never validate again.
    if route["status"] != "pass":
        with pytest.raises(jsonschema.ValidationError):
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

    # Issue #995 fix_delta (OWNER REQUEST_CHANGES on AC8): the local
    # test-only `_opaque_consumer_read()` pseudo-reader (asserted only that
    # the payload round-trips through JSON) is replaced with the ACTUAL
    # production routing function `route_structural_repair_disposition()` --
    # the same function `run_refinement_preflight.py` would need to call to
    # connect this bundle to control-plane status/next_action (P0-1). This
    # proves opaque schema-interface readability AND exercises the real
    # producer-side consumer contract, not a test-local stand-in.
    assert round_tripped["schema_version"] == "structural_repair_action/v1"
    route = ric.route_structural_repair_disposition(round_tripped)
    assert route["status"] in {"pass", "needs_fix", "blocked"}
    assert route["next_action"] in {
        "proceed", "apply_deterministic_structural_repair", "human_judgment_required",
    }
    safe_field_ids = [
        item["field_id"] for item in round_tripped["items"]
        if item["disposition"] == "auto_apply_safe"
    ]
    for item in round_tripped["items"]:
        assert item["disposition"] in {"auto_apply_safe", "human_review_required"}
        if item["disposition"] == "auto_apply_safe":
            assert item["derivation"] in ric.CLOSED_DERIVATION_MODES
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
        "status": route["status"],
        "issue_number": 995,
        "repo": "squne121/loop-protocol",
        "planner_exit_code": 0,
        "planner_fail_closed": False,
        "next_action": route["next_action"],
        "must_read": [],
        "do_not_read": [],
        "commands": [],
        "blockers": [],
        "artifacts": {},
        "hashes": {},
        "structural_repair_action": bundle,
    }
    jsonschema.validate(instance=minimal_preflight, schema=preflight_schema)


# ---------------------------------------------------------------------------
# Adversarial matrix (Issue #995 fix_delta, OWNER REQUEST_CHANGES on PR #2206)
#
# These cover the OWNER-listed minimum adversarial test matrix. Some names
# were adapted slightly where the underlying production surface is a pure
# function rather than a live GitHub-backed consumer (Issue #995's Outcome
# explicitly keeps GitHub mutation/readback out of scope; see the PR
# comment for what remains partially/未 addressed).
# ---------------------------------------------------------------------------


def test_structural_human_review_cannot_validate_with_status_pass():
    """P0-1: a structural_repair_action with disposition_summary ==
    human_review_required can never validate against
    refinement_preflight_result_v1.schema.json alongside status: pass."""
    bundle = ric.build_structural_repair_bundle(
        BODY_MISSING_MANY_REQUIRED_FIELDS,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        repo="o/r",
        issue_number=1,
    )
    assert bundle["disposition_summary"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    preflight_schema = _load_schema("refinement_preflight_result_v1.schema.json")
    payload = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "pass",
        "issue_number": 1,
        "repo": "o/r",
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
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=preflight_schema)


def test_structural_auto_safe_routes_to_needs_fix():
    """P0-1: disposition_summary == auto_apply_safe routes to
    status: needs_fix / next_action: apply_deterministic_structural_repair,
    never a bare pass/proceed."""
    body_missing_stop_conditions = BODY_ALL_PRESENT_NON_EMPTY.replace(
        "## Stop Conditions\n\n- none\n\n", ""
    )
    bundle = ric.build_structural_repair_bundle(
        body_missing_stop_conditions,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH,
        repo="o/r",
        issue_number=1,
    )
    assert bundle["disposition_summary"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    route = ric.route_structural_repair_disposition(bundle)
    assert route["status"] == "needs_fix"
    assert route["next_action"] == "apply_deterministic_structural_repair"

    preflight_schema = _load_schema("refinement_preflight_result_v1.schema.json")
    ok_payload = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "needs_fix",
        "issue_number": 1,
        "repo": "o/r",
        "planner_exit_code": 0,
        "planner_fail_closed": False,
        "next_action": "apply_deterministic_structural_repair",
        "must_read": [],
        "do_not_read": [],
        "commands": [],
        "blockers": [],
        "artifacts": {},
        "hashes": {},
        "structural_repair_action": bundle,
    }
    jsonschema.validate(instance=ok_payload, schema=preflight_schema)
    bad_payload = {**ok_payload, "status": "pass", "next_action": "proceed"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_payload, schema=preflight_schema)


def test_real_2039_consumer_contract_handles_or_rejects_structural_action():
    """AC8 fix_delta: exercises the ACTUAL production routing function
    (`route_structural_repair_disposition`), not a test-local pseudo-reader.
    #2039's live GitHub-backed consumer classification/transaction remains
    out of #995's scope (Issue #995 Outcome) -- this test validates the
    schema-interface contract #2039 would consume, using the real producer
    function instead of a fabricated stand-in."""
    for body, expect_summary in (
        (BODY_ALL_PRESENT_NON_EMPTY, "no_missing_fields_detected"),
        (BODY_MISSING_MANY_REQUIRED_FIELDS, ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED),
    ):
        bundle = ric.build_structural_repair_bundle(
            body, issue_kind="implementation", template_text=TEMPLATE_TEXT,
            template_path=TEMPLATE_PATH, repo="o/r", issue_number=1,
        )
        assert bundle["disposition_summary"] == expect_summary
        route = ric.route_structural_repair_disposition(bundle)
        assert route["status"] in {"pass", "needs_fix", "blocked"}
        # Never a raw dict/bool: real closed-enum contract every caller can
        # branch on without inventing its own classification.
        assert isinstance(route["next_action"], str) and route["next_action"]


def test_missing_entire_mrc_reports_section_and_all_required_keys():
    """P0-5: when the whole Machine-Readable Contract section is absent,
    EVERY required key for the issue_kind is still enumerated (not silently
    skipped), in addition to the section-level item itself."""
    body_no_mrc = """\
## Outcome

some text

## Verification Commands

- `pnpm test`
"""
    items = ric.detect_missing_template_sections(
        body_no_mrc, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    field_ids = {i["field_id"] for i in items}
    assert "machine-readable-contract" in field_ids  # section-level item
    for key in ric.REQUIRED_CONTRACT_KEYS_BY_KIND["implementation"]:
        key_field_id = f"machine-readable-contract.{key}"
        assert key_field_id in field_ids, f"missing key-level item for {key!r}"
        key_item = next(i for i in items if i["field_id"] == key_field_id)
        assert key_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
        assert "mrc_section_missing" in key_item["reason_codes"]


def test_actual_template_mrc_placeholders_are_not_auto_safe():
    """P0-4: the REAL implementation.yml Machine-Readable Contract
    scaffold's default `value` contains `<...>` and unselected `a|b|c`
    placeholders (parent_issue / goal_ref / change_kind) -- the whole
    section item must be human_review_required, never
    template_value_exact, when the section is entirely missing."""
    mrc_field = next(
        f for f in ric.parse_issue_template_fields(TEMPLATE_TEXT, TEMPLATE_PATH)
        if f["field_id"] == "machine-readable-contract"
    )
    assert ric._contains_placeholder_scaffold(mrc_field["value"])

    body_no_mrc = """\
## Outcome

some text

## Verification Commands

- `pnpm test`
"""
    items = ric.detect_missing_template_sections(
        body_no_mrc, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    mrc_item = next(i for i in items if i["field_id"] == "machine-readable-contract")
    assert mrc_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert mrc_item["derivation"] is None
    assert "candidate_value" not in mrc_item


def test_malformed_and_tilde_fenced_mrc_fail_closed():
    """P0-5: a tilde-fenced MRC (not the canonical ```` ```yaml ```` fence)
    and a malformed-YAML MRC both fail closed to human_review_required for
    every required key -- never silently skipped."""
    body_tilde = """\
## Machine-Readable Contract

~~~yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: g
change_kind: code
~~~

## Outcome

x

## Verification Commands

- `pnpm test`
"""
    items_tilde = ric.detect_missing_template_sections(
        body_tilde, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    key_ids_tilde = {i["field_id"] for i in items_tilde if i["field_id"].startswith("machine-readable-contract.")}
    for key in ric.REQUIRED_CONTRACT_KEYS_BY_KIND["implementation"]:
        assert f"machine-readable-contract.{key}" in key_ids_tilde

    body_malformed = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: [this is not, a mapping
```

## Outcome

x

## Verification Commands

- `pnpm test`
"""
    items_malformed = ric.detect_missing_template_sections(
        body_malformed, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    key_ids_malformed = {
        i["field_id"] for i in items_malformed
        if i["field_id"].startswith("machine-readable-contract.")
    }
    for key in ric.REQUIRED_CONTRACT_KEYS_BY_KIND["implementation"]:
        item = next(i for i in items_malformed if i["field_id"] == f"machine-readable-contract.{key}")
        assert item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert key_ids_malformed  # non-empty: never silently skipped


def test_mrc_null_empty_invalid_enum_and_duplicate_keys_are_invalid():
    """P0-5: null value, empty string value, and a duplicate mapping key in
    the MRC YAML are all treated as missing/invalid, never as a present
    valid value."""
    body_null_value = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: null
goal_ref: ""
change_kind: code
```

## Outcome

x

## Verification Commands

- `pnpm test`
"""
    items = ric.detect_missing_template_sections(
        body_null_value, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    by_id = {i["field_id"]: i for i in items}
    parent_issue_item = by_id["machine-readable-contract.parent_issue"]
    assert parent_issue_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    goal_ref_item = by_id["machine-readable-contract.goal_ref"]
    assert goal_ref_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED

    body_duplicate_key = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: g
change_kind: docs
change_kind: code
```

## Outcome

x

## Verification Commands

- `pnpm test`
"""
    items_dup = ric.detect_missing_template_sections(
        body_duplicate_key, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    for key in ric.REQUIRED_CONTRACT_KEYS_BY_KIND["implementation"]:
        item = next(i for i in items_dup if i["field_id"] == f"machine-readable-contract.{key}")
        assert item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
        assert any("duplicate_key" in code for code in item["reason_codes"])


def test_source_span_without_authority_url_span_digest_is_not_safe():
    """P0-3: a source span with only `text` (no authority_kind/source_repo/
    source_object_kind/source_object_id/source_revision) can never back an
    auto_apply_safe item."""
    items = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
        source_spans={
            "parent-goal-ref": {
                "text": "- Goal: g",
                "source_url": "https://x/1",
                "line_start": 1,
                "line_end": 1,
            },
        },
    )
    by_id = {i["field_id"]: i for i in items}
    item = by_id["parent-goal-ref"]
    assert item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert "candidate_value" not in item
    assert any("source_span_missing_" in code for code in item["reason_codes"])


def test_parent_issue_rejects_foobar_as_derived_scalar():
    """P0-3: known_scalars={"parent-issue": "foobar"} is rejected by the
    field-specific closed validator (`^(?:none|#[1-9][0-9]*)$`)."""
    items = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
        known_scalars={"parent-issue": "foobar"},
    )
    by_id = {i["field_id"]: i for i in items}
    assert by_id["parent-issue"]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert "derived_scalar_failed_field_specific_validator" in by_id["parent-issue"]["reason_codes"]


def test_unknown_issue_kind_fails_closed():
    """P0-5: an issue_kind outside the SSOT-parsed closed enum can never
    resolve a required-key set -- fails closed, never silently uses an
    empty required-keys list."""
    items = ric.detect_missing_template_sections(
        BODY_MISSING_MANY_REQUIRED_FIELDS, issue_kind="not-a-real-kind",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    unresolved = [i for i in items if "unknown_issue_kind" in i["reason_codes"]]
    assert unresolved
    assert all(i["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED for i in unresolved)


def test_commonmark_trailing_text_does_not_close_fence():
    """P1-1: the OWNER's concrete adversarial example -- a fenced code
    example containing a FAKE closer (` ```not-a-close `) must not close
    the real fence, so a spoofed `## Outcome` inside it is never parsed as
    a real section."""
    body = """\
## Notes

```text
```not-a-close
## Outcome
inside code
```

## Verification Commands

- `pnpm test`
"""
    sections = ric._parse_h2_sections(body)
    headings = [s["heading"] for s in sections]
    assert "Outcome" not in headings
    assert "Notes" in headings
    assert "Verification Commands" in headings


def test_valid_indented_and_closing_hash_h2_is_recognized():
    """P1-1: up to 3 leading spaces and an optional trailing closing-hash
    run are still a valid ATX H2 heading (CommonMark)."""
    body = """\
   ## Outcome ##

some text

## Verification Commands

- `pnpm test`
"""
    sections = ric._parse_h2_sections(body)
    headings = [s["heading"] for s in sections]
    assert "Outcome" in headings


def test_schema_rejects_summary_item_mismatch():
    """P1-2: disposition_summary: auto_apply_safe with items: [] is
    schema-invalid (minItems/contains invariant)."""
    schema = _load_schema("repair_issue_contract_result_v1.schema.json")
    base_result = ric.run_repair(BODY_ALL_PRESENT_NON_EMPTY)
    bad_bundle = {
        "schema_version": "structural_repair_action/v1",
        "policy_version": "template-derived-structural-repair/v1",
        "issue_kind": "implementation",
        "repo": "o/r",
        "issue_number": 1,
        "original_body_sha256": base_result["original_body_sha256"],
        "original_updated_at": None,
        "items": [],
        "disposition_summary": "auto_apply_safe",
    }
    payload = {**base_result, "structural_repair_action": bad_bundle}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_schema_rejects_null_auto_safe_identity():
    """P1-2: an auto_apply_safe item's original_body_sha256 must be a real
    sha256:-prefixed digest string, never null."""
    schema = _load_schema("repair_issue_contract_result_v1.schema.json")
    bundle = ric.build_structural_repair_bundle(
        BODY_ALL_PRESENT_NON_EMPTY.replace("## Stop Conditions\n\n- none\n\n", ""),
        issue_kind="implementation", template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH, repo="o/r", issue_number=1,
    )
    assert bundle["disposition_summary"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    broken = json.loads(json.dumps(bundle))
    for item in broken["items"]:
        if item["disposition"] == "auto_apply_safe":
            item["original_body_sha256"] = None
    base_result = ric.run_repair(BODY_ALL_PRESENT_NON_EMPTY)
    payload = {**base_result, "structural_repair_action": broken}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_schema_rejects_negative_or_reversed_spans():
    """P1-2/P0-3: a negative source_span line number is schema-invalid, and
    a reversed (line_end < line_start) span is rejected by the runtime
    provenance validator before ever reaching auto_apply_safe."""
    schema = _load_schema("repair_issue_contract_result_v1.schema.json")
    bundle = ric.build_structural_repair_bundle(
        BODY_ALL_PRESENT_NON_EMPTY.replace("## Stop Conditions\n\n- none\n\n", ""),
        issue_kind="implementation", template_text=TEMPLATE_TEXT,
        template_path=TEMPLATE_PATH, repo="o/r", issue_number=1,
    )
    broken = json.loads(json.dumps(bundle))
    for item in broken["items"]:
        if item["disposition"] == "auto_apply_safe":
            item["source_span"] = {"line_start": -1, "line_end": -2}
    base_result = ric.run_repair(BODY_ALL_PRESENT_NON_EMPTY)
    payload = {**base_result, "structural_repair_action": broken}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)

    ok, reasons = ric._validate_source_span_provenance({
        "authority_kind": "parent_issue", "source_repo": "o/r",
        "source_object_kind": "issue_body", "source_object_id": "1",
        "source_url": "https://x/1", "source_revision": "abc",
        "line_start": 10, "line_end": 5, "text": "x",
    })
    assert ok is False
    assert "source_span_invalid_line_range" in reasons


def test_ambiguous_insertion_anchor_requires_human_review():
    """P0-2: when NO neighbouring template-declared section (in either
    direction) is present exactly once in the body, the insertion anchor is
    ambiguous, and the item is forced to human_review_required regardless
    of its derivation-based classification."""
    # No H2 headings AT ALL (not even Machine-Readable Contract) -- every
    # template-declared neighbour is absent, so NO item can ever find a
    # preceding/following anchor present exactly once.
    body_no_headings = "just plain prose, no sections here.\n"
    items = ric.detect_missing_template_sections(
        body_no_headings, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    by_id = {i["field_id"]: i for i in items}

    # A field that would normally classify as human_review_required stays
    # so, AND its insertion is ambiguous (no anchor either way).
    ac_item = by_id["acceptance-criteria"]
    assert ac_item["insertion"]["disposition"] == "ambiguous"
    assert ac_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED

    # "Stop Conditions" would normally be template_value_exact / auto_apply_safe
    # (a real, non-placeholder template default) -- but with NO anchor
    # candidate anywhere in the body, it is force-downgraded to
    # human_review_required and its auto-safe-only fields are stripped.
    stop_item = by_id["stop-conditions"]
    assert stop_item["insertion"]["disposition"] == "ambiguous"
    assert stop_item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert stop_item["derivation"] is None
    assert "ambiguous_insertion_anchor" in stop_item["reason_codes"]
    assert "candidate_value" not in stop_item
    assert "candidate_digest" not in stop_item


def test_empty_section_preserves_observed_cardinality_one():
    """P1-3: an empty/placeholder-only section that IS present once in the
    body keeps observed_cardinality == 1 (an observed fact), distinct from
    content_state (a content-state judgment)."""
    items = ric.detect_missing_template_sections(
        BODY_PLACEHOLDER_ONLY_SECTION, issue_kind="implementation",
        template_text=TEMPLATE_TEXT, template_path=TEMPLATE_PATH,
    )
    by_id = {i["field_id"]: i for i in items}
    parent_issue_item = by_id["parent-issue"]
    assert parent_issue_item["observed_cardinality"] == 1
    assert parent_issue_item["content_state"] == "placeholder"


def test_ssot_policy_digest_changes_when_authoring_policy_changes():
    """P1-3: REQUIRED_CONTRACT_KEYS_BY_KIND is actually parsed from
    body-authoring.md (not an independent hand-transcribed literal) --
    re-parsing the SAME file content must reproduce the loaded policy
    exactly, and re-parsing a MODIFIED bullet list must diverge from the
    original loaded policy (proving the parser is load-bearing, not a
    coincidental copy)."""
    real_text = ric._BODY_AUTHORING_SSOT_PATH.read_text(encoding="utf-8")
    reparsed = ric._parse_required_contract_keys_ssot(real_text)
    assert reparsed == ric.REQUIRED_CONTRACT_KEYS_BY_KIND
    assert reparsed["implementation"] == ric.REQUIRED_CONTRACT_KEYS_BY_KIND["implementation"]

    mutated_text = real_text.replace(
        "`contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`",
        "`contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`, `extra_key`",
    )
    assert mutated_text != real_text, "fixture assumption broke: SSOT wording changed upstream"
    mutated_parsed = ric._parse_required_contract_keys_ssot(mutated_text)
    assert mutated_parsed["implementation"] != ric.REQUIRED_CONTRACT_KEYS_BY_KIND["implementation"]
    assert "extra_key" in mutated_parsed["implementation"]
