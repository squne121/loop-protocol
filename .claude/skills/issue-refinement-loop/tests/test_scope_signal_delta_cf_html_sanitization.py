"""#2333 regression tests: CF_HTML/clipboard contamination boundary.

Covers the anonymized structural shape of the Issue #2290 incident (a
CF_HTML clipboard envelope -- ``<!--StartFragment-->``/``<!--EndFragment-->``
markers wrapping HTML that duplicates a trailing Markdown rendition of the
same content) and a non-regression case proving legitimate GFM raw HTML
(``<details>``/``<summary>``) is never treated as contamination.
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
    # wrapper carrying a CF_HTML StartFragment/EndFragment envelope, followed
    # by a duplicated Markdown-only bullet.
    comment_body = _load_fixture_text("cf_html_envelope_contaminated.md")

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
    comment_body = _load_fixture_text("clean_details_block.md")

    # WHEN/THEN the existing plain-bullet extraction behavior is unchanged:
    # the bullet marker is stripped and the text is returned as-is (no
    # regression from the new CF_HTML boundary).
    items = delta.extract_directive_items(comment_body)
    assert items == ["Update the retry backoff constant to 500ms."]

    # A directive with no bullet markers at all is untouched either.
    plain = "Just a plain sentence with no bullet markers."
    assert delta.extract_directive_items(plain) == []


def test_production_chain_stops_or_canonicalizes_before_write():
    # GIVEN the real production call chain: a raw (contaminated) anchor
    # comment body flows through _build_scope_delta_authority_evidence()
    # (which calls extract_directive_items() internally) and then through
    # derive_contract_patch_operations().
    comment_body = _load_fixture_text("cf_html_envelope_contaminated.md")
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

    # THEN either no operation was produced (canonicalize-unable -> writes
    # == 0), or every produced operation's text is free of CF_HTML markers
    # and HTML wrapper tags.
    for operation in operations:
        text = operation["text"]
        assert "<!--StartFragment" not in text
        assert "<!--EndFragment" not in text
        assert "<html" not in text.lower()
        assert "<body" not in text.lower()


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
