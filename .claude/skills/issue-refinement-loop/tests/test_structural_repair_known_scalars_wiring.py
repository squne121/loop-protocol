"""
test_structural_repair_known_scalars_wiring.py

Issue #2431: `run_refinement_preflight.py`'s Step 0f structural-repair
producer previously never populated `known_scalars`/`source_spans` when
calling `build_structural_repair_bundle()` (repair_issue_contract.py), so a
derivation-eligible missing section fell through to `human_review_required`
even when it was actually safely derivable from data already available on
the SAME preflight run (#2426's manifestation).

This file exercises the REAL production wiring end-to-end (`run_preflight()`
via fixture mode, mocked planner only -- never a hand-constructed
`structural_repair_action` bundle) for the positive/negative behavior, plus
focused direct-function tests for the individual assembler preconditions
that would be impractical to isolate through the full pipeline alone.

  AC1: test_known_scalars_and_source_spans_reach_build_structural_repair_bundle_kwargs
  AC2: test_parent_issue_scalar_cross_populates_both_consumer_field_ids
  AC3: test_current_issue_authority_kind_enum_consistent_across_python_and_schemas
  AC4: test_current_issue_source_span_only_emitted_when_every_precondition_holds
  AC5: test_heading_alias_table_is_closed_one_way_exact_match_only
  AC6: test_combined_positive_fixture_classifies_auto_apply_safe_via_real_preflight
  AC7: test_negative_fixtures_stay_fail_closed
  AC8: test_regression_unknown_issue_kind_still_leaves_structural_repair_action_absent
  AC9: test_no_new_derivation_mode_added
  AC10: this file itself (see `## Verification Commands` in Issue #2431)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import jsonschema

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
_SCHEMAS_DIR = _SKILL_ROOT / "schemas"
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import repair_issue_contract as ric  # noqa: E402
import run_refinement_preflight as wrapper  # noqa: E402

_TEMPLATE_PATH = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "implementation.yml"
_TEMPLATE_TEXT = _TEMPLATE_PATH.read_text(encoding="utf-8")

REPO = "testowner/testrepo"

MOCK_PLAN_PASS = {
    "schema_version": "refinement_loop_plan/v1",
    "fail_closed": {"required": False, "reason_codes": []},
    "decisions": {},
}


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def _validate_against_result_schema(result: dict) -> None:
    schema = _load_schema("refinement_preflight_result_v1.schema.json")
    jsonschema.validate(instance=result, schema=schema)


def _seed_real_template(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "implementation.yml").write_text(_TEMPLATE_TEXT, encoding="utf-8")


# ---------------------------------------------------------------------------
# Body builder -- every required implementation.yml heading is present and
# non-empty by default; callers override/omit only the fields under test so
# every fixture produces EXACTLY the intended missing-field item(s).
# ---------------------------------------------------------------------------


def _build_body(
    *,
    parent_issue_mrc: "str | None" = "none",
    include_parent_issue_heading: bool = True,
    parent_issue_heading_value: str = "none",
    include_allowed_paths_heading: bool = True,
    include_proposed_allowed_paths_heading: bool = False,
    allowed_paths_value: str = "- src/example.ts",
    proposed_allowed_paths_value: str = "- src/example.ts",
    mrc_malformed: bool = False,
) -> str:
    lines: list[str] = ["## Machine-Readable Contract", ""]
    if mrc_malformed:
        # Keeps `issue_kind: implementation` regex-matchable (so
        # `_resolve_structural_repair_template()` still resolves a template
        # and the structural-repair pass actually runs) while making the
        # canonical MRC parser reject the block as malformed (duplicate
        # `parent_issue` key).
        lines += [
            "```yaml",
            "issue_kind: implementation",
            'parent_issue: "none"',
            'parent_issue: "none"',
            "goal_ref: g",
            "change_kind: code",
            "```",
            "",
        ]
    else:
        mrc_lines = ["```yaml", "contract_schema_version: v1", "issue_kind: implementation"]
        if parent_issue_mrc is not None:
            mrc_lines.append(f'parent_issue: "{parent_issue_mrc}"')
        mrc_lines += ["goal_ref: g", "change_kind: code", "```"]
        lines += mrc_lines + [""]

    if include_parent_issue_heading:
        lines += ["## Parent Issue", "", parent_issue_heading_value, ""]

    lines += [
        "## Parent Goal Ref", "", "- Goal: g", "- Desired Destination: N/A", "",
        "## Current Validated Scope", "", "- x", "",
        "## Remaining Parent Gaps", "", "- none", "",
        "## Outcome", "", "text", "",
        "## Runtime Verification Applicability", "", "- decision: not_applicable", "- reason: r", "",
        "## In Scope", "", "- x", "",
        "## Out of Scope", "", "- x", "",
        "## Acceptance Criteria", "", "- [ ] AC1", "",
        "## Verification Commands", "", "- `pnpm test`", "",
    ]

    if include_proposed_allowed_paths_heading:
        lines += ["## Proposed Allowed Paths", "", proposed_allowed_paths_value, ""]
    if include_allowed_paths_heading:
        lines += ["## Allowed Paths", "", allowed_paths_value, ""]

    lines += [
        "## Stop Conditions", "", "- none", "",
        "## Required Skills", "", "- none", "",
    ]
    return "\n".join(lines) + "\n"


def _write_fixture(
    tmp_path: Path,
    issue_number: int,
    body: str,
    *,
    anchor_comment_urls: "list[str] | None" = None,
    anchor_comments: "list[dict] | None" = None,
) -> Path:
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": REPO,
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {"number": issue_number, "title": "Test Issue", "body": body, "labels": []},
        "comments": [],
        "anchor_comment_urls": anchor_comment_urls or [],
        "anchor_comments": anchor_comments or [],
    }
    fixture_path = tmp_path / f"fixture-{issue_number}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    return fixture_path


def _run_preflight(
    tmp_path: Path,
    issue_number: int,
    body: str,
    *,
    anchor_comment_urls: "list[str] | None" = None,
    anchor_comments: "list[dict] | None" = None,
    known_context: "dict | None" = None,
) -> tuple[dict, int]:
    fixture_path = _write_fixture(
        tmp_path, issue_number, body,
        anchor_comment_urls=anchor_comment_urls, anchor_comments=anchor_comments,
    )
    _seed_real_template(tmp_path)
    with (
        mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
        mock.patch.object(wrapper, "_invoke_planner", return_value=(MOCK_PLAN_PASS, 0, "", "")),
    ):
        return wrapper.run_preflight(
            issue_number=issue_number,
            repo=REPO,
            anchor_comment_urls=anchor_comment_urls or [],
            fixture_path=fixture_path,
            known_context=known_context,
        )


def _items_by_field_id(result: dict) -> dict:
    sra = result.get("structural_repair_action")
    if not sra:
        return {}
    return {i["field_id"]: i for i in sra["items"]}


# ---------------------------------------------------------------------------
# AC1/AC6: the real production path (mocked planner only) actually wires
# known_scalars/source_spans into build_structural_repair_bundle()'s kwargs,
# and the resulting bundle classifies BOTH derivation-eligible missing
# sections as auto_apply_safe instead of human_review_required.
# ---------------------------------------------------------------------------


def test_known_scalars_and_source_spans_reach_build_structural_repair_bundle_kwargs(tmp_path):
    """AC1: `build_structural_repair_bundle()` is invoked with non-empty
    `known_scalars`/`source_spans` kwargs for a body that has a derivable
    `parent_issue` (via MRC) and a derivable `Allowed Paths` (via the
    `Proposed Allowed Paths` heading alias)."""
    body = _build_body(
        parent_issue_mrc="#42",
        include_parent_issue_heading=False,
        include_allowed_paths_heading=False,
        include_proposed_allowed_paths_heading=True,
    )
    captured: dict = {}
    real_build = ric.build_structural_repair_bundle

    def _spy(*args, **kwargs):
        captured["known_scalars"] = kwargs.get("known_scalars")
        captured["source_spans"] = kwargs.get("source_spans")
        return real_build(*args, **kwargs)

    with mock.patch.object(wrapper, "build_structural_repair_bundle", side_effect=_spy):
        result, exit_code = _run_preflight(tmp_path, 2431, body)

    assert captured["known_scalars"] == {
        "parent-issue": "#42",
        "machine-readable-contract.parent_issue": "#42",
    }, captured
    assert "allowed-paths" in captured["source_spans"], captured
    assert captured["source_spans"]["allowed-paths"]["authority_kind"] == "current_issue"
    assert exit_code == wrapper.EXIT_NEEDS_FIX
    _validate_against_result_schema(result)


# ---------------------------------------------------------------------------
# AC2: parent_issue cross-population into BOTH consumer field-ids.
# ---------------------------------------------------------------------------


def test_parent_issue_scalar_cross_populates_both_consumer_field_ids():
    body_mrc_only = _build_body(parent_issue_mrc="#42", include_parent_issue_heading=False)
    assert wrapper._resolve_parent_issue_known_scalar(body_mrc_only) == "#42"

    body_heading_only = _build_body(
        parent_issue_mrc=None, include_parent_issue_heading=True, parent_issue_heading_value="#99"
    )
    assert wrapper._resolve_parent_issue_known_scalar(body_heading_only) == "#99"

    assert wrapper._PARENT_ISSUE_KNOWN_SCALAR_FIELD_IDS == (
        "parent-issue", "machine-readable-contract.parent_issue",
    )


def test_parent_issue_via_mrc_cross_populates_missing_heading_field_via_real_preflight(tmp_path):
    """AC2 (production path): the MRC's OWN `parent_issue` value resolves
    the MISSING `## Parent Issue` heading item to auto_apply_safe /
    derived_scalar_exact -- never the OTHER direction silently invented."""
    body = _build_body(parent_issue_mrc="#42", include_parent_issue_heading=False)
    result, exit_code = _run_preflight(tmp_path, 2431, body)
    items = _items_by_field_id(result)
    assert items["parent-issue"]["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert items["parent-issue"]["derivation"] == ric.DERIVATION_DERIVED_SCALAR_EXACT
    assert items["parent-issue"]["candidate_value"] == "#42"
    assert exit_code == wrapper.EXIT_NEEDS_FIX


# ---------------------------------------------------------------------------
# AC3: `current_issue` authority_kind enum consistency (Python <-> both
# schemas).
# ---------------------------------------------------------------------------


def test_current_issue_authority_kind_enum_consistent_across_python_and_schemas():
    assert ric._SOURCE_SPAN_AUTHORITY_KINDS == frozenset(
        {"parent_issue", "owner_anchor", "design_reference", "current_issue"}
    )

    for schema_name in (
        "repair_issue_contract_result_v1.schema.json",
        "refinement_preflight_result_v1.schema.json",
    ):
        schema = _load_schema(schema_name)
        # Structural: locate the authority_kind enum object itself rather
        # than a substring match anywhere in the file.
        found = _find_authority_kind_enum(schema)
        assert found is not None, f"{schema_name}: authority_kind enum not found"
        assert set(found) == ric._SOURCE_SPAN_AUTHORITY_KINDS | {None}, (schema_name, found)


def _find_authority_kind_enum(node):
    if isinstance(node, dict):
        if "authority_kind" in node and isinstance(node["authority_kind"], dict):
            enum = node["authority_kind"].get("enum")
            if enum is not None:
                return enum
        for value in node.values():
            result = _find_authority_kind_enum(value)
            if result is not None:
                return result
    elif isinstance(node, list):
        for item in node:
            result = _find_authority_kind_enum(item)
            if result is not None:
                return result
    return None


# ---------------------------------------------------------------------------
# AC4: `current_issue` source_span_exact only emitted when every
# precondition holds.
# ---------------------------------------------------------------------------


def test_current_issue_source_span_only_emitted_when_every_precondition_holds():
    # Positive: alias present exactly once, canonical missing, non-empty.
    body_ok = _build_body(include_allowed_paths_heading=False, include_proposed_allowed_paths_heading=True)
    spans = wrapper._resolve_current_issue_heading_alias_source_spans(body_ok, repo=REPO, issue_number=1)
    assert "allowed-paths" in spans
    assert spans["allowed-paths"]["authority_kind"] == "current_issue"
    assert spans["allowed-paths"]["source_revision"] == ric._sha256(body_ok)

    # Negative: canonical heading already present -> no span (nothing to
    # derive; the field is not missing in the first place).
    body_canonical_present = _build_body(
        include_allowed_paths_heading=True, include_proposed_allowed_paths_heading=True
    )
    assert wrapper._resolve_current_issue_heading_alias_source_spans(
        body_canonical_present, repo=REPO, issue_number=1
    ) == {}

    # Negative: alias heading appears twice -> ambiguous, no span.
    body_dup_alias = _build_body(include_allowed_paths_heading=False, include_proposed_allowed_paths_heading=True)
    body_dup_alias += "\n## Proposed Allowed Paths\n\n- another/path.ts\n"
    assert wrapper._resolve_current_issue_heading_alias_source_spans(
        body_dup_alias, repo=REPO, issue_number=1
    ) == {}

    # Negative: alias section present but empty content -> no span.
    body_empty_alias = _build_body(include_allowed_paths_heading=False, include_proposed_allowed_paths_heading=False)
    body_empty_alias = body_empty_alias.replace(
        "## Stop Conditions", "## Proposed Allowed Paths\n\n\n## Stop Conditions"
    )
    assert wrapper._resolve_current_issue_heading_alias_source_spans(
        body_empty_alias, repo=REPO, issue_number=1
    ) == {}


def test_current_issue_source_span_digest_binds_to_original_body_sha256():
    """AC4's same-body/digest-match precondition: `source_revision` is
    exactly `sha256:` + the digest of the SAME body the span's text was
    read from, so a downstream consumer can independently confirm the span
    never crossed a stale/different body snapshot."""
    body = _build_body(include_allowed_paths_heading=False, include_proposed_allowed_paths_heading=True)
    spans = wrapper._resolve_current_issue_heading_alias_source_spans(body, repo=REPO, issue_number=7)
    expected_digest = ric._sha256(body)
    assert spans["allowed-paths"]["source_revision"] == expected_digest
    assert spans["allowed-paths"]["source_object_kind"] == "issue_body"
    assert spans["allowed-paths"]["source_object_id"] == "7"
    assert spans["allowed-paths"]["source_repo"] == REPO


# ---------------------------------------------------------------------------
# PR #2469 fix_delta iteration 3 (P1-1, human adversarial review):
# `line_start`/`line_end` must byte-exactly reconstruct
# `source_spans[...]["text"]` via `body.splitlines()[line_start-1:line_end]`
# -- NOT merely "the line right after the heading", which silently
# disagreed with the actual raw content start whenever a blank separator
# line sits between the `## Heading` and its content (the ordinary
# heading -> blank line -> bullets shape this repo's own issue templates
# use, and the SAME shape `_build_body()` already produces above).
# ---------------------------------------------------------------------------


def test_current_issue_source_span_line_range_reconstructs_text_single_line():
    """(a) Normal heading -> blank line -> single-line bullet content
    shape."""
    body = _build_body(include_allowed_paths_heading=False, include_proposed_allowed_paths_heading=True)
    spans = wrapper._resolve_current_issue_heading_alias_source_spans(body, repo=REPO, issue_number=1)
    span = spans["allowed-paths"]
    line_start, line_end = span["line_start"], span["line_end"]
    actual = "\n".join(body.splitlines()[line_start - 1:line_end])
    assert actual == span["text"]


def test_current_issue_source_span_line_range_reconstructs_text_multi_line():
    """(b) Multi-line content case: the alias section's content spans
    several bullet lines, so `line_end` must advance past `line_start` by
    exactly the right number of lines."""
    multi_line_value = "- src/example.ts\n- src/other.ts\n- src/third.ts"
    body = _build_body(
        include_allowed_paths_heading=False,
        include_proposed_allowed_paths_heading=True,
        proposed_allowed_paths_value=multi_line_value,
    )
    spans = wrapper._resolve_current_issue_heading_alias_source_spans(body, repo=REPO, issue_number=1)
    span = spans["allowed-paths"]
    assert span["text"] == multi_line_value
    line_start, line_end = span["line_start"], span["line_end"]
    assert line_end == line_start + 2
    actual = "\n".join(body.splitlines()[line_start - 1:line_end])
    assert actual == span["text"]


# ---------------------------------------------------------------------------
# PR #2469 fix_delta iteration 3 (P1-2, human adversarial review): a forged
# `current_issue` source span (claiming a `source_object_kind`/`source_repo`/
# `source_object_id`/`source_revision` the ONE trusted assembler in
# `run_refinement_preflight.py` never actually emits) must be rejected at
# `repair_issue_contract.py`'s OWN generic producer boundary
# (`build_structural_repair_bundle()`), never only by that one assembler --
# a forged span could, in principle, reach this module via a different
# caller entirely.
# ---------------------------------------------------------------------------


def test_forged_current_issue_source_span_rejected_at_generic_producer_boundary():
    issue_number = 900
    body = _build_body(
        parent_issue_mrc=None,
        include_parent_issue_heading=False,
        include_allowed_paths_heading=False,
        include_proposed_allowed_paths_heading=False,
    )
    forged_span = {
        "text": "- src/forged.ts",
        "source_url": f"https://github.com/{REPO}/issues/{issue_number}",
        "line_start": 1,
        "line_end": 1,
        "authority_kind": "current_issue",
        # Forged: the real assembler only ever emits "issue_body" for
        # `current_issue` -- this claims a comment backed it instead.
        "source_object_kind": "issue_comment",
        "source_repo": REPO,
        "source_object_id": str(issue_number),
        # `ric._sha256()` is already `sha256:`-prefixed.
        "source_revision": ric._sha256(body),
    }
    bundle = ric.build_structural_repair_bundle(
        body,
        issue_kind="implementation",
        template_text=_TEMPLATE_TEXT,
        template_path=str(_TEMPLATE_PATH),
        repo=REPO,
        issue_number=issue_number,
        source_spans={"allowed-paths": forged_span},
    )
    items_by_field = {i["field_id"]: i for i in bundle["items"]}
    item = items_by_field["allowed-paths"]
    assert item["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert item["derivation"] is None
    assert bundle["disposition_summary"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED


def test_current_issue_source_span_authority_validator_rejects_each_forgeable_field():
    """Direct unit coverage of `_validate_current_issue_source_span_authority()`
    itself, isolating each individual forgeable field one at a time."""
    issue_number = 901
    body = "## Allowed Paths placeholder body"
    # `ric._sha256()` is already `sha256:`-prefixed -- the SAME format
    # `build_structural_repair_bundle()`'s own `original_body_sha256`
    # variable holds, so no additional prefixing is applied here.
    original_body_sha256 = ric._sha256(body)
    valid_span = {
        "authority_kind": "current_issue",
        "source_object_kind": "issue_body",
        "source_repo": REPO,
        "source_object_id": str(issue_number),
        "source_revision": original_body_sha256,
    }
    assert ric._validate_current_issue_source_span_authority(
        valid_span, repo=REPO, issue_number=issue_number, original_body_sha256=original_body_sha256,
    )

    for mutated_field, bad_value in (
        ("source_object_kind", "issue_comment"),
        ("source_repo", "someone-else/other-repo"),
        ("source_object_id", str(issue_number + 1)),
        ("source_revision", "sha256:deadbeef"),
    ):
        forged = {**valid_span, mutated_field: bad_value}
        assert not ric._validate_current_issue_source_span_authority(
            forged, repo=REPO, issue_number=issue_number, original_body_sha256=original_body_sha256,
        ), mutated_field

    # A non-`current_issue` authority_kind is never narrowed by this
    # validator (additive extension only, other 3 kinds unaffected).
    other_kind_span = {**valid_span, "authority_kind": "owner_anchor", "source_repo": "anything"}
    assert ric._validate_current_issue_source_span_authority(
        other_kind_span, repo=REPO, issue_number=issue_number, original_body_sha256=original_body_sha256,
    )


# ---------------------------------------------------------------------------
# AC5: closed, one-way, exact heading alias table -- no fuzzy/substring
# matching, no other heading pairs.
# ---------------------------------------------------------------------------


def test_heading_alias_table_is_closed_one_way_exact_match_only():
    assert wrapper._STRUCTURAL_HEADING_ALIAS_TABLE == {"Proposed Allowed Paths": "Allowed Paths"}

    # A near-miss / fuzzy heading must NOT be treated as the alias.
    body = _build_body(include_allowed_paths_heading=False, include_proposed_allowed_paths_heading=False)
    body = body.replace(
        "## Stop Conditions", "## Suggested Allowed Paths\n\n- src/near-miss.ts\n\n## Stop Conditions"
    )
    assert wrapper._resolve_current_issue_heading_alias_source_spans(body, repo=REPO, issue_number=1) == {}

    # The reverse direction (canonical -> alias) must never be treated as
    # an alias pair either -- only the ONE closed, one-way mapping exists.
    assert "Allowed Paths" not in wrapper._STRUCTURAL_HEADING_ALIAS_TABLE


# ---------------------------------------------------------------------------
# AC6: combined positive fixture via the real production path.
# ---------------------------------------------------------------------------


def test_combined_positive_fixture_classifies_auto_apply_safe_via_real_preflight(tmp_path):
    """AC6: parent_issue resolvable (MRC), a heading-alias semantic source
    for Allowed Paths, plus a trusted human-context anchor establishing the
    overall reframe context -- the derivation-eligible missing sections
    classify as auto_apply_safe (never human_review_required)."""
    issue_number = 2431
    comment_id = 5555001
    url = f"https://github.com/{REPO}/issues/{issue_number}#issuecomment-{comment_id}"
    anchor_comment = {
        "id": comment_id,
        "body": "この Issue の Allowed Paths は Proposed Allowed Paths のとおりで確定です。",
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{issue_number}",
        "html_url": url,
        "url": f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}",
        "user": {"login": "owner-user", "type": "User"},
        "author_association": "OWNER",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    body = _build_body(
        parent_issue_mrc="#42",
        include_parent_issue_heading=False,
        include_allowed_paths_heading=False,
        include_proposed_allowed_paths_heading=True,
    )
    result, exit_code = _run_preflight(
        tmp_path, issue_number, body,
        anchor_comment_urls=[url],
        anchor_comments=[anchor_comment],
        known_context={"human_context_comment_urls": [url]},
    )
    sra = result["structural_repair_action"]
    assert sra["disposition_summary"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    items = _items_by_field_id(result)
    assert set(items) == {"parent-issue", "allowed-paths"}
    assert items["parent-issue"]["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert items["parent-issue"]["derivation"] == ric.DERIVATION_DERIVED_SCALAR_EXACT
    assert items["allowed-paths"]["disposition"] == ric.STRUCT_DISPOSITION_AUTO_APPLY_SAFE
    assert items["allowed-paths"]["derivation"] == ric.DERIVATION_SOURCE_SPAN_EXACT
    assert items["allowed-paths"]["source_span"]["authority_kind"] == "current_issue"
    assert result["status"] == "needs_fix"
    assert result["next_action"] == "apply_deterministic_structural_repair"
    assert exit_code == wrapper.EXIT_NEEDS_FIX
    _validate_against_result_schema(result)


# ---------------------------------------------------------------------------
# AC7: negative fixtures stay fail-closed.
# ---------------------------------------------------------------------------


class TestNegativeFixturesStayFailClosed:
    def test_no_source_authority_stays_human_review_required(self, tmp_path):
        body = _build_body(
            parent_issue_mrc=None,
            include_parent_issue_heading=False,
            include_allowed_paths_heading=False,
            include_proposed_allowed_paths_heading=False,
        )
        result, exit_code = _run_preflight(tmp_path, 100, body)
        items = _items_by_field_id(result)
        assert items["parent-issue"]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
        assert items["allowed-paths"]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
        assert result["status"] == "blocked"

    def test_multiple_distinct_byte_candidates_conflict_to_none(self):
        """Distinct-valued MRC vs. trusted-anchor candidates never guess --
        resolves to None (the field then falls through to
        human_review_required downstream, never a fabricated value)."""
        body = _build_body(parent_issue_mrc="#1", include_parent_issue_heading=False)
        anchor_body = "## Parent Issue\n\n#2\n"
        assert wrapper._resolve_parent_issue_known_scalar(
            body, trusted_anchor_body=anchor_body
        ) is None

    def test_agent_only_anchor_never_backs_auto_apply_safe(self, tmp_path):
        issue_number = 5
        comment_id = 999001
        url = f"https://github.com/{REPO}/issues/{issue_number}#issuecomment-{comment_id}"
        anchor_comment = {
            "id": comment_id,
            "body": "## Parent Issue\n\n#77\n",
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/{issue_number}",
            "html_url": url,
            "url": f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}",
            "user": {"login": "some-agent", "type": "Bot"},
            "author_association": "NONE",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        body = _build_body(parent_issue_mrc=None, include_parent_issue_heading=False)
        result, _exit_code = _run_preflight(
            tmp_path, issue_number, body,
            anchor_comment_urls=[url],
            anchor_comments=[anchor_comment],
            known_context={"agent_report_comment_urls": [url]},
        )
        items = _items_by_field_id(result)
        assert items["parent-issue"]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED

    def test_unlabeled_anchor_never_backs_auto_apply_safe(self, tmp_path):
        """No `human_context_comment_urls`/`agent_report_comment_urls`
        lane at all (the anchor is simply present, unlabeled) -- the
        existing canonical lane classifier fails closed to
        `generated_by_agent`, so the anchor's content must not be used."""
        issue_number = 6
        comment_id = 999002
        url = f"https://github.com/{REPO}/issues/{issue_number}#issuecomment-{comment_id}"
        anchor_comment = {
            "id": comment_id,
            "body": "## Parent Issue\n\n#77\n",
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/{issue_number}",
            "html_url": url,
            "url": f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}",
            "user": {"login": "owner-user", "type": "User"},
            "author_association": "OWNER",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        body = _build_body(parent_issue_mrc=None, include_parent_issue_heading=False)
        result, _exit_code = _run_preflight(
            tmp_path, issue_number, body,
            anchor_comment_urls=[url],
            anchor_comments=[anchor_comment],
            known_context=None,
        )
        items = _items_by_field_id(result)
        assert items["parent-issue"]["disposition"] == ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED

    def test_malformed_machine_readable_contract_stays_fail_closed(self, tmp_path):
        body = _build_body(mrc_malformed=True)
        result, exit_code = _run_preflight(tmp_path, 200, body)
        items = _items_by_field_id(result)
        # Every required MRC key is enumerated as human_review_required
        # when the MRC itself is malformed (pre-existing invariant,
        # unaffected by the new known_scalars/source_spans wiring).
        assert items["machine-readable-contract.parent_issue"]["disposition"] == (
            ric.STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
        )
        assert result["status"] == "blocked"


# ---------------------------------------------------------------------------
# AC8: unknown issue_kind / template resolution error / checker internal
# error / authority conflict remain blocked (regression).
# ---------------------------------------------------------------------------


def test_regression_unknown_issue_kind_still_leaves_structural_repair_action_absent(tmp_path):
    body = _build_body().replace("issue_kind: implementation", "issue_kind: totally_unknown_kind")
    result, _exit_code = _run_preflight(tmp_path, 300, body)
    assert result.get("structural_repair_action") is None


# ---------------------------------------------------------------------------
# AC9: no new derivation mode / structural schema model added.
# ---------------------------------------------------------------------------


def test_no_new_derivation_mode_added():
    assert ric.CLOSED_DERIVATION_MODES == frozenset({
        ric.DERIVATION_TEMPLATE_VALUE_EXACT,
        ric.DERIVATION_SOURCE_SPAN_EXACT,
        ric.DERIVATION_DERIVED_SCALAR_EXACT,
    })
    assert len(ric.CLOSED_DERIVATION_MODES) == 3
