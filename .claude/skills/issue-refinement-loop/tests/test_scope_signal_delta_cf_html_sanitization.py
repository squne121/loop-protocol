"""#2333 regression tests: CF_HTML/clipboard contamination boundary.

Covers the anonymized structural shape of the Issue #2290 incident (a
CF_HTML clipboard envelope -- ``<!--StartFragment-->``/``<!--EndFragment-->``
markers wrapping HTML that duplicates a trailing Markdown rendition of the
same content) and a non-regression case proving legitimate GFM raw HTML
(``<details>``/``<summary>``) is never treated as contamination.

#2333 fix_delta (OWNER REQUEST_CHANGES on PR #2336):

- P0-1: `extract_directive_markers()` / `detect_boundary_flags()` /
  `classify_directive_confidence()` must observe the SAME canonical view of
  a contaminated comment body as `extract_directive_items()` -- never a mix
  of the raw HTML+Markdown envelope and the canonicalized Markdown-only
  tail.
- P0-2: the envelope terminator must be located structurally (an outer
  ``<html>``/``<body>`` wrapper at the very start of the body, immediately
  followed by StartFragment, then the FIRST following EndFragment) -- not
  by taking the LAST ``<!--EndFragment-->`` occurrence anywhere in the
  string, which breaks when the trailing Markdown tail itself mentions the
  marker strings for explanatory purposes (exactly what the OWNER review
  comment that reported this bug does).

The fixtures below use the ``.txt`` extension (not ``.md``) so they are
never picked up by the repository's "Check changed Markdown files"
Japanese-content CI gate, which is scoped to changed ``*.md`` files -- these
fixtures are English test data, not prose/documentation (#2333 fix_delta P1).
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "cf_html_sanitization"

sys.path.insert(0, str(SCRIPTS_DIR))

delta = importlib.import_module("scope_signal_delta")
preflight = importlib.import_module("run_refinement_preflight")


def _load_fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_extract_directive_items_canonicalizes_cf_html_envelope():
    # GIVEN an anchor comment body shaped like the #2290 incident: an HTML
    # wrapper carrying a CF_HTML StartFragment/EndFragment envelope,
    # IMMEDIATELY followed (no blank line, matching the real #2290/#2333
    # observed shape) by a duplicated Markdown rendition that itself starts
    # with a heading.
    comment_body = _load_fixture_text("cf_html_envelope_contaminated.txt")

    # WHEN the directive-extraction boundary processes the raw comment body
    items = delta.extract_directive_items(comment_body)

    # THEN only the trailing Markdown bullet is extracted, with no
    # CF_HTML marker or HTML wrapper tag surviving into the result.
    assert items == [
        "Add retry handling to the sync worker for transient network failures."
    ]
    for item in items:
        assert "<!--StartFragment" not in item
        assert "<!--EndFragment" not in item
        assert "<html" not in item.lower()
        assert "<body" not in item.lower()
        assert "<ul" not in item.lower()


def test_extract_directive_items_preserves_clean_bullet_text():
    # GIVEN a normal bullet-form anchor directive that also happens to
    # contain legitimate GFM raw HTML (<details>/<summary>) but carries no
    # CF_HTML marker at all.
    comment_body = _load_fixture_text("clean_details_block.txt")

    # WHEN/THEN the existing plain-bullet extraction behavior is unchanged:
    # the bullet marker is stripped and the text is returned as-is (no
    # regression from the new CF_HTML boundary).
    items = delta.extract_directive_items(comment_body)
    assert items == ["Update the retry backoff constant to 500ms."]

    # A directive with no bullet markers at all is untouched either.
    plain = "Just a plain sentence with no bullet markers."
    assert delta.extract_directive_items(plain) == []


def test_plain_markdown_mentioning_marker_is_not_invalidated():
    # #2333 fix_delta P0-2: ordinary Markdown that merely *mentions* the
    # marker strings (e.g. explaining this exact technique in backticks),
    # with no envelope-open shape at the start of the body at all, must be
    # passed through byte-for-byte and its directive must NOT be
    # invalidated just because a StartFragment mention is present with no
    # matching EndFragment.
    mention_only = "- `<!--StartFragment-->` を扱う regression test を追加する"
    assert delta._canonicalize_cf_html_envelope(mention_only) == mention_only
    assert delta.extract_directive_items(mention_only) == [
        "`<!--StartFragment-->` を扱う regression test を追加する"
    ]


def test_genuine_envelope_open_without_terminator_is_rejected():
    # A genuine envelope OPEN (outer <html>/<body> wrapper immediately
    # followed by StartFragment, at the very start of the body) with no
    # EndFragment marker anywhere is fail-closed: the fragment boundary
    # cannot be uniquely determined, so the directive is disabled.
    broken = "<html>\n<body>\n<!--StartFragment-->\n<p>no terminator here</p>\n"
    assert delta._canonicalize_cf_html_envelope(broken) is None
    assert delta.extract_directive_items(broken) == []


def test_tail_mentioning_markers_in_backticks_is_still_canonicalized():
    # #2333 fix_delta P0-2 (P1 regression fixture): the trailing Markdown
    # duplicate itself explains the StartFragment/EndFragment technique in
    # backticks -- exactly the shape of the OWNER review comment that
    # reported this bug. The FIRST (not last) EndFragment marker after the
    # opening StartFragment must be used as the genuine terminator, so this
    # later, explanatory mention in the tail must never be mistaken for it.
    comment_body = _load_fixture_text("cf_html_tail_mentions_markers.txt")

    items = delta.extract_directive_items(comment_body)
    assert items == [
        "Add retry handling to the sync worker for transient network failures."
    ]


def test_markers_and_boundary_flags_use_same_canonical_view_as_directives():
    # #2333 fix_delta P0-1 (blocker): `extract_directive_markers()` and
    # `detect_boundary_flags()` must observe the SAME canonical Markdown-only
    # tail as `extract_directive_items()` -- never the raw HTML+Markdown
    # envelope. An `Allowed Paths` heading that exists ONLY in the HTML half
    # of the envelope (never in the canonical Markdown tail) must not
    # surface as a directive marker or flip the `expands_allowed_paths`
    # boundary flag.
    comment_body = _load_fixture_text("cf_html_html_only_allowed_paths.txt")

    markers = delta.extract_directive_markers(comment_body)
    assert "allowed paths" not in markers
    assert "revised acceptance criteria" in markers

    flags = delta.detect_boundary_flags(comment_body)
    assert flags["expands_allowed_paths"] is False

    confidence = delta.classify_directive_confidence(comment_body)
    assert confidence == delta.DIRECTIVE_CONFIDENCE_EXPLICIT


def test_production_chain_stops_or_canonicalizes_before_write():
    # GIVEN the real production call chain: a raw (contaminated) anchor
    # comment body flows through _build_scope_delta_authority_evidence()
    # (which calls extract_directive_items() internally) and then through
    # derive_contract_patch_operations().
    comment_body = _load_fixture_text("cf_html_envelope_contaminated.txt")
    anchor_url = (
        "https://github.com/squne121/loop-protocol/issues/2333#issuecomment-1"
    )
    comment_payload = {
        "id": 1,
        "author_association": "OWNER",
        "user": {"login": "owner-user", "type": "User"},
    }

    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=comment_payload,
        comment_body=comment_body,
        repo="squne121/loop-protocol",
        issue_number=2333,
        anchor_url=anchor_url,
        captured_at="2026-08-25T00:00:00Z",
    )
    assert evidence is not None

    # WHEN operations are derived from that evidence
    operations = delta.derive_contract_patch_operations([evidence])

    # THEN the generated operation set is EXACT, not merely "sanitized" --
    # #2333 fix_delta P1: a wrongly-mutated operation that writes clean text
    # into a completely different section would previously still PASS this
    # test, since it only checked for the ABSENCE of contamination markers,
    # never the semantic correctness of the operation itself.
    assert operations == [
        {
            "section": "Acceptance Criteria",
            "op": "append",
            "text": "Add retry handling to the sync worker for transient network failures.",
            "rationale": "Directive extracted from trusted review comment (revised ac)",
            "source_evidence_index": 0,
        }
    ]


def test_html_only_allowed_paths_directive_produces_zero_allowed_paths_operations():
    # #2333 fix_delta P1 (pins P0-1 cheaply): an `Allowed Paths` heading that
    # exists ONLY in the HTML half of the envelope -- never in the canonical
    # Markdown tail, which does not request Allowed Paths at all -- must
    # produce ZERO Allowed Paths operations. Before the P0-1 fix, the raw
    # marker (detected from the contaminated HTML half) could be recombined
    # with the canonicalized directive text to synthesize an
    # authorization-bearing Allowed Paths operation that was never actually
    # requested by the canonical (Markdown) directive.
    comment_body = _load_fixture_text("cf_html_html_only_allowed_paths.txt")
    anchor_url = (
        "https://github.com/squne121/loop-protocol/issues/2333#issuecomment-2"
    )
    comment_payload = {
        "id": 2,
        "author_association": "OWNER",
        "user": {"login": "owner-user", "type": "User"},
    }

    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=comment_payload,
        comment_body=comment_body,
        repo="squne121/loop-protocol",
        issue_number=2333,
        anchor_url=anchor_url,
        captured_at="2026-08-25T00:00:00Z",
    )
    assert evidence is not None
    assert "allowed paths" not in (evidence.get("directive_markers") or [])

    operations = delta.derive_contract_patch_operations([evidence])
    allowed_paths_operations = [
        operation for operation in operations if operation["section"] == "Allowed Paths"
    ]
    assert allowed_paths_operations == []


def test_contaminated_token_not_accepted_as_allowed_path_literal():
    # A token containing an HTML/CF_HTML marker character must never
    # normalize into an exact Allowed Paths literal.
    assert delta._normalize_exact_repository_path_literal(
        "scripts/<script>evil.py"
    ) is None
    assert delta._normalize_exact_repository_path_literal(
        "scripts/<!--StartFragment-->evil.py"
    ) is None

    # A directive mixing a contaminated token with prose must not silently
    # drop the bad token and keep going -- the whole directive yields no
    # exact path literal (mixed directives are not exact positive deltas).
    mixed = "Please add scripts/<script>alert(1)</script>.py to Allowed Paths."
    assert delta._extract_path_literals_from_text(mixed) == []

    # Sanity: an ordinary, uncontaminated backticked literal is unaffected.
    clean = "Please add `scripts/safe_module.py` to Allowed Paths."
    assert delta._extract_path_literals_from_text(clean) == ["scripts/safe_module.py"]


def test_existing_details_block_does_not_block_unrelated_clean_update():
    # GIVEN an Issue body that already contains legitimate GFM raw HTML
    # (<details>/<summary>) unrelated to any directive, and a clean
    # (non-contaminated) anchor directive targeting that Issue.
    body_with_details = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#1"
```

## Parent Issue

#1

## Parent Goal Ref

- Goal: Test goal

## Current Validated Scope

- scripts/example.py

## Remaining Parent Gaps

- [ ] Nothing remaining

## Runtime Verification Applicability

decision: not_applicable
reason: static-only verification is sufficient for this fixture

## Outcome

Add `scripts/example.py`.

<details>
<summary>Background</summary>
Legitimate raw HTML block, unrelated to any directive.
</details>

## In Scope

- scripts/example.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
# AC1
$ uv run python3 scripts/example.py
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- none

## Required Skills

none
"""

    # A clean (non-contaminated) anchor directive against this same body
    # extracts normally -- the new CF_HTML boundary is a no-op when no
    # marker is present.
    clean_directive = "- Regenerate the fixture checksum."
    assert delta.extract_directive_items(clean_directive) == [
        "Regenerate the fixture checksum."
    ]

    # The pre-existing <details> block in the Issue body must not, by
    # itself, flip the readiness gate: this is exercised via the same
    # contract_readiness_check.py the production candidate_readiness()
    # closure invokes (run_refinement_preflight.py).
    readiness_script = (
        SKILL_ROOT.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as handle:
        handle.write(body_with_details)
        candidate_path = handle.name

    completed = subprocess.run(
        [sys.executable, str(readiness_script), "--body-file", candidate_path, "--mode", "static"],
        capture_output=True,
        text=True,
        check=False,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "go", result
