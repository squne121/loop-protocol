from __future__ import annotations

import json
import importlib
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCHEMAS_DIR = SKILL_ROOT / "schemas"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "scope_signal_delta"

sys.path.insert(0, str(SCRIPTS_DIR))

delta = importlib.import_module("scope_signal_delta")
plan = importlib.import_module("plan_refinement_loop")


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _load_schema() -> dict:
    return json.loads(
        (SCHEMAS_DIR / "scope_signal_delta_v1.schema.json").read_text(encoding="utf-8")
    )


def _run_cli(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "scope_signal_delta.py")],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )


def test_schema_or_artifact():
    payload = _load_fixture("new_allowed_path_layer")
    input_validator = jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/scopeSignalDeltaInputV1", "$defs": _load_schema()["$defs"]}
    )
    assert list(input_validator.iter_errors(payload)) == []
    result = delta.compute_scope_signal_delta(payload)
    validator = jsonschema.Draft202012Validator(_load_schema())
    assert list(validator.iter_errors(result)) == []
    assert result["schema_version"] == "scope_signal_delta/v1"
    assert result["inputs"]["before_body_sha256"].startswith("sha256:")
    assert result["sections"]["allowed_paths"]["added"] == ["docs/dev/workflow.md"]


@pytest.mark.parametrize(
    "fixture_name",
    ["repeated_existing", "reordered", "whitespace", "fenced_code"],
)
def test_repeated_or_reordered_or_whitespace_or_fenced_code(fixture_name: str):
    payload = _load_fixture(fixture_name)
    result = delta.compute_scope_signal_delta(payload)
    assert result["legacy_scope_signal_guard"]["triggered"] is False
    assert result["legacy_scope_signal_guard"]["reason_code"] == "no_scope_signal"


def test_new_allowed_path_layer():
    payload = _load_fixture("new_allowed_path_layer")
    result = delta.compute_scope_signal_delta(payload)
    assert result["sections"]["allowed_paths"]["added_layers"] == ["docs"]
    assert result["legacy_scope_signal_guard"]["triggered"] is True
    assert result["legacy_scope_signal_guard"]["reason_code"] == "new_allowed_path_layer"


def test_projection_or_repeated_existing():
    payload = _load_fixture("new_allowed_path_layer")
    result = delta.compute_scope_signal_delta(payload)
    assert result["sections"]["allowed_paths"]["repeated_existing"] == [
        ".claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py"
    ]
    signal = next(
        item for item in result["signals"] if item["reason_code"] == "new_allowed_path_layer"
    )
    assert signal["triggered"] is True
    assert signal["triggering_lines"]
    assert signal["normalized_value"] == ["docs"]
    assert signal["triggering_lines"][0]["source_ref"].endswith(":after")


def test_cli_round_trip():
    payload = _load_fixture("new_allowed_path_layer")
    result = _run_cli(payload)
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    assert parsed["legacy_scope_signal_guard"]["reason_code"] == "new_allowed_path_layer"


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_fenced_acceptance_criteria_checkbox_is_ignored(fence: str):
    payload = {
        "before_body": "## Allowed Paths\n\n",
        "current_body": (
            "## Allowed Paths\n\n"
            "- `docs/dev/workflow.md`\n\n"
            "## Acceptance Criteria\n\n"
            f"{fence}text\n"
            "- [ ] AC1: 品質を改善する\n"
            f"{fence}\n"
        ),
        "after_body": (
            "## Allowed Paths\n\n"
            "- `docs/dev/workflow.md`\n\n"
            "## Acceptance Criteria\n\n"
            f"{fence}text\n"
            "- [ ] AC1: 品質を改善する\n"
            f"{fence}\n"
        ),
        "source_refs": {"before": "fixture:before", "current": "fixture:current", "after": "fixture:after"},
    }
    result = delta.compute_scope_signal_delta(payload)
    ac_signal = next(item for item in result["signals"] if item["reason_code"] == "new_unverifiable_ac")
    path_signal = next(item for item in result["signals"] if item["reason_code"] == "new_allowed_path_layer")
    assert ac_signal["triggered"] is False
    assert path_signal["triggered"] is True


def test_iter_section_lines_recognizes_indented_h2_and_closing_hashes():
    body = (
        "   ## Acceptance Criteria ###\n"
        "\n"
        "- [ ] AC1: 品質を改善する\n"
    )
    lines = delta.iter_section_lines(body, "Acceptance Criteria", semantic_only=True)
    assert [line.text for line in lines] == ["", "- [ ] AC1: 品質を改善する"]


def test_iter_section_lines_requires_closer_length_to_match_outer_fence():
    body = (
        "## Acceptance Criteria\n\n"
        "````text\n"
        "- [ ] AC1: 品質を改善する\n"
        "```\n"
        "````\n"
        "- [ ] AC2: 既存スコープを維持する\n"
    )
    lines = delta.iter_section_lines(body, "Acceptance Criteria", semantic_only=True)
    assert [line.text for line in lines] == ["", "- [ ] AC2: 既存スコープを維持する"]


def test_four_space_indented_fence_marker_is_not_treated_as_fence_opener():
    body = (
        "## Acceptance Criteria\n\n"
        "    ```text\n"
        "- [ ] AC1: 品質を改善する\n"
    )
    lines = delta.iter_section_lines(body, "Acceptance Criteria", semantic_only=True)
    assert any(line.text == "- [ ] AC1: 品質を改善する" for line in lines)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "before_body": "x",
                "current_body": "y",
                "after_body": "z",
                "source_refs": {"before": "a", "current": "b"},
            },
            "source_refs.after is required",
        ),
        (
            {
                "before_body": "x",
                "current_body": "y",
                "after_body": "z",
                "source_refs": {"before": "a", "current": "b", "after": "c"},
                "unexpected": True,
            },
            "unknown input fields: unexpected",
        ),
    ],
)
def test_invalid_input_contract(payload: dict, message: str):
    result = _run_cli(payload)
    assert result.returncode == 2
    assert message in result.stderr


# --- Issue #1327 iteration-2 (B1/B2/B4): nested-prefix / cross-implementation ---
# regression coverage for _extract_in_scope_layers() itself (not only the
# plan_refinement_loop.py legacy fallback subprocess path).

_B1_PREFIXES = (".claude/", "docs/", "src/", "scripts/", "tests/", ".github/")


def test_nested_prefix_not_double_counted_in_delta_helper():
    """B1-1: before already has `.claude`; after adds a single path token that
    also contains `tests/` as an embedded substring
    (`.claude/skills/foo/tests/test_bar.py`). `tests` must not appear in
    added_layers because the token itself does not start with `tests/`."""
    payload = _load_fixture("nested_prefix_not_double_counted")
    result = delta.compute_scope_signal_delta(payload)
    added_layers = result["sections"]["in_scope"]["added_layers"]
    assert "tests" not in added_layers
    assert added_layers == []
    assert result["legacy_scope_signal_guard"]["triggered"] is False
    assert result["legacy_scope_signal_guard"]["reason_code"] == "no_scope_signal"


def test_single_nested_token_with_empty_before_yields_claude_layer_only():
    """B1-2: before is empty; after has only the single nested path token.
    The resulting after-side layer set must be exactly {".claude"}, not
    {".claude", "tests"}."""
    payload = _load_fixture("single_token_no_before")
    result = delta.compute_scope_signal_delta(payload)
    after_layers = set(result["sections"]["in_scope"]["after_layers"])
    assert after_layers == {".claude"}
    assert "tests" not in after_layers


def test_fenced_code_nested_path_ignored_in_scope_section():
    """B1-3: a nested path mentioned only inside a fenced code block within
    the In Scope section must not be extracted as a layer at all."""
    payload = _load_fixture("fenced_code_nested_path_ignored_in_scope")
    result = delta.compute_scope_signal_delta(payload)
    assert result["sections"]["in_scope"]["before_layers"] == []
    assert result["sections"]["in_scope"]["after_layers"] == []
    assert result["sections"]["in_scope"]["added_layers"] == []
    assert result["legacy_scope_signal_guard"]["triggered"] is False
    assert result["legacy_scope_signal_guard"]["reason_code"] == "no_scope_signal"


def test_multiple_independent_tokens_same_bullet_triggers_in_delta_helper():
    """B4 (delta side): a single bullet referencing two independent path
    tokens (`.claude/skills/foo` and `docs/foo.md`) must still trigger
    new_in_scope_area (true-positive must not be broken by the nested-prefix
    fix)."""
    payload = _load_fixture("multiple_independent_tokens_same_bullet")
    result = delta.compute_scope_signal_delta(payload)
    after_layers = set(result["sections"]["in_scope"]["after_layers"])
    assert after_layers == {".claude", "docs"}
    assert result["legacy_scope_signal_guard"]["triggered"] is True
    assert result["legacy_scope_signal_guard"]["reason_code"] == "new_in_scope_area"


@pytest.mark.parametrize(
    ("line", "expected_layers"),
    [
        (
            "- `.claude/skills/foo/tests/test_bar.py` の配置を確認する",
            {".claude"},
        ),
        (
            "- `.claude/skills/foo` と `docs/foo.md` を更新する",
            {".claude", "docs"},
        ),
        (
            "| `.claude/skills/foo` | done | 備考 |",
            {".claude"},
        ),
    ],
)
def test_legacy_fallback_and_delta_helper_tokenization_agree(
    line: str, expected_layers: set[str]
):
    """B2: plan_refinement_loop.py's legacy fallback tokenizer
    (`_line_layer_prefixes`) and scope_signal_delta.py's delta helper
    tokenizer (`_extract_in_scope_layers` via `PATH_TOKEN_RE`) must agree on
    the set of layer prefixes extracted from the same In Scope line,
    including markdown table rows that contain `|` delimiters."""
    legacy_layers = {p.rstrip("/") for p in plan._line_layer_prefixes(line, _B1_PREFIXES)}
    delta_items = delta._extract_in_scope_layers(f"## In Scope\n{line}\n")
    delta_layers = {item["value"] for item in delta_items}
    assert legacy_layers == expected_layers
    assert delta_layers == expected_layers


# --- Issue #2296 fix_delta iteration 6 (P1-4): severity tags split out ----
# extract_directive_markers() no longer merges severity-tagged headings
# into its return value; that extraction now lives in the independent
# extract_severity_tags() function so a bare severity heading never, by
# itself, becomes a scope-authoritative directive marker.


def test_extract_severity_tags_detects_severity_tagged_heading():
    """GIVEN a comment body with an owner adversarial-review severity
    heading (## P0-1)
    WHEN extract_severity_tags runs
    THEN the uppercased tag is included in the returned tag list."""
    text = "## P0-1\n\nSomething is broken.\n\n- fix it"
    tags = delta.extract_severity_tags(text)
    assert "P0-1" in tags


def test_extract_severity_tags_detects_multiple_severity_tags_lowercase_heading():
    text = "### p1-3\nlower body\n\n#### p2\nnot a match (missing dash-number)"
    tags = delta.extract_severity_tags(text)
    assert "P1-3" in tags
    assert "P2" not in tags  # P2 alone (no "-N") is not the tagged pattern


def test_extract_severity_tags_without_severity_heading_is_empty():
    text = "Plain freeform comment with no heading at all."
    tags = delta.extract_severity_tags(text)
    assert tags == []


def test_extract_directive_markers_does_not_include_severity_tags():
    """P1-4: extract_directive_markers() must NOT surface severity-tagged
    headings -- only the fixed _DIRECTIVE_SECTION_MARKERS set."""
    text = "## P0-1\n\nSomething is broken.\n\n- fix it"
    markers = delta.extract_directive_markers(text)
    assert "P0-1" not in markers
    assert markers == []


def test_extract_directive_markers_without_severity_heading_is_unaffected():
    text = "Plain freeform comment with no heading at all."
    markers = delta.extract_directive_markers(text)
    assert markers == []


def test_extract_directive_markers_still_detects_fixed_markers_alongside_severity_tag():
    """A severity-tagged heading co-occurring with a fixed directive marker
    heading: extract_directive_markers() surfaces only the fixed marker;
    extract_severity_tags() surfaces only the severity tag."""
    text = "## P0-2\n\nRevised Acceptance Criteria below.\n\n- item"
    markers = delta.extract_directive_markers(text)
    assert "P0-2" not in markers
    assert "revised acceptance criteria" in markers
    tags = delta.extract_severity_tags(text)
    assert "P0-2" in tags


def test_bare_severity_heading_does_not_produce_explicit_directive_confidence():
    """P1-4 regression: a bare severity-tagged heading (e.g. '## P0-1') plus
    an unrelated bullet must NOT, by itself, produce a scope-authoritative
    directive_confidence:explicit classification via
    extract_directive_markers()/classify_directive_confidence() -- that
    conflated a severity label with an actual scope-change directive."""
    text = "## P0-1\n\n- please fix this"
    items = delta.extract_directive_items(text)
    assert items == ["please fix this"]
    confidence = delta.classify_directive_confidence(text)
    assert confidence == delta.DIRECTIVE_CONFIDENCE_INFERRED


# ---------------------------------------------------------------------------
# Issue #2812: `_BULLET_LINE_RE` / `extract_directive_items()` Markdown
# ORDERED-list marker (`1. `, `2. `, `1)`, `2)`) detection gap. Regression
# parent: #2778 (research) -- Issue #2730 (CF_HTML clipboard-paste
# `<ol><li>`) and Issue #2805 (native Markdown ordered list) both reproduce
# the same underlying gap: `_BULLET_LINE_RE` only ever matched unordered
# `-`/`*` bullets, so a structured `human_review_directive` expressed as an
# ordered list was misclassified as `ambiguous` and never reached
# `issue_editor_required`.
# ---------------------------------------------------------------------------


# AC1 ------------------------------------------------------------------------


def test_bullet_line_re_still_matches_unordered_dash_and_asterisk_markers():
    """GIVEN unordered bullet lines (the pre-existing `-`/`*` behavior)
    WHEN `_BULLET_LINE_RE` scans them
    THEN both marker styles still match (no regression from the ordered-
    list extension)."""
    assert delta._BULLET_LINE_RE.search("- unordered dash item") is not None
    assert delta._BULLET_LINE_RE.search("* unordered asterisk item") is not None


@pytest.mark.parametrize(
    "line",
    ["1. first ordered item", "2. second ordered item", "1) paren-style ordered item"],
)
def test_bullet_line_re_matches_new_ordered_list_markers(line):
    """AC1: GIVEN a Markdown ORDERED-list marker line (`1. `, `2. `, or the
    `1)` paren-style variant)
    WHEN `_BULLET_LINE_RE` scans it
    THEN it now matches -- the ordered-list detection gap (#2778/#2805) is
    closed."""
    assert delta._BULLET_LINE_RE.search(line) is not None


def test_bullet_line_re_does_not_match_non_list_prose_or_version_strings():
    """AC1 boundary: plain prose and a dotted version string (no marker +
    space at the line start) must not spuriously match the extended
    pattern."""
    assert delta._BULLET_LINE_RE.search("Not a bullet at all.") is None
    assert delta._BULLET_LINE_RE.search("Released version 1.2.3 today.") is None


# ---------------------------------------------------------------------------
# PR #2814 OWNER REQUEST_CHANGES fix_delta (findings A/B/C): line-local
# marker whitespace (never absorbing the next line's prose into a
# marker-only match) and ASCII-only/max-9-digit ordered-marker grammar,
# shared between the detector (`_BULLET_LINE_RE`) and the extractor
# (`_ORDERED_LIST_ITEM_PREFIX_RE` inside `extract_directive_items()`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,line",
    [
        ("ten_digit_marker", "1234567890. Please update X"),
        ("arabic_indic_digit_marker", "١. Please update X"),
        ("full_width_digit_marker", "１. Please update X"),
    ],
)
def test_bullet_line_re_rejects_non_gfm_ordered_markers(label, line):
    """Finding B: a 10+ digit run, an Arabic-Indic digit, or a full-width
    digit must never be accepted as a GFM ordered-list marker -- only
    ASCII `[0-9]{1,9}` followed by `.`/`)` is a valid marker."""
    assert delta._BULLET_LINE_RE.search(line) is None, label


def test_bullet_line_re_still_accepts_nine_digit_ordered_marker():
    """Finding B boundary: the maximum valid GFM ordered-marker digit
    count (9 digits) still matches."""
    assert delta._BULLET_LINE_RE.search("123456789. Please update X") is not None


def test_bullet_line_re_does_not_absorb_next_line_prose_into_marker_only_line():
    """Finding A: a marker-only line (`1.` with no content on the SAME
    line) must never match by consuming the newline and absorbing the
    NEXT line's prose into a single span -- the marker's surrounding
    whitespace is line-local (`[ \\t]`), never bare `\\s` (which also
    matches `\\n`)."""
    text = "## Revised Acceptance Criteria\n1.\nPlease update X\n"
    assert delta._BULLET_LINE_RE.search(text) is None


def test_bullet_line_re_does_not_absorb_next_line_prose_when_marker_has_trailing_whitespace():
    """Finding A variant: a marker line followed only by trailing
    whitespace (e.g. `1.    `) before the next line's prose must also
    never be absorbed into a single cross-line match."""
    text = "## Revised Acceptance Criteria\n1.    \nPlease update X\n"
    assert delta._BULLET_LINE_RE.search(text) is None


# AC2 ------------------------------------------------------------------------


def test_extract_directive_items_extracts_ordered_list_content_symmetrically_with_unordered():
    """AC2: GIVEN a body mixing unordered (`-`/`*`) and ordered (`1. `,
    `2. `) list lines
    WHEN `extract_directive_items()` runs
    THEN every line's content is extracted, in document order, regardless
    of marker style -- ordered-list extraction is symmetric with the
    pre-existing unordered extraction."""
    text = (
        "- unordered dash content\n"
        "* unordered asterisk content\n"
        "1. first ordered content\n"
        "2. second ordered content\n"
    )
    items = delta.extract_directive_items(text)
    assert items == [
        "unordered dash content",
        "unordered asterisk content",
        "first ordered content",
        "second ordered content",
    ]


def test_extract_directive_items_ordered_marker_alone_matches_unordered_stripping_behavior():
    """AC2: an ordered-list line with only marker + whitespace (no content)
    yields no item -- symmetric with the existing unordered `"- "` / `"* "`
    behavior (an empty-content bullet line is never appended)."""
    text = "1.    \n-    \n2. real content\n"
    assert delta.extract_directive_items(text) == ["real content"]


# ---------------------------------------------------------------------------
# PR #2814 OWNER REQUEST_CHANGES fix_delta (finding C): marker-only-line
# regression fixed at the extractor level -- `extract_directive_items()`
# must never fabricate an item from a marker-only line, and its verdict
# (empty list) must stay consistent with `_BULLET_LINE_RE.search()`
# returning no match on the same input (no detector/extractor semantic
# split).
# ---------------------------------------------------------------------------


def test_extract_directive_items_marker_only_line_yields_no_item_and_stays_consistent_with_detector():
    """Finding C: a bare `1.` marker-only line followed by prose on the
    NEXT line yields zero items from `extract_directive_items()` (each
    input line is processed independently after `splitlines()`, so no
    item is ever fabricated from just `"1."`), and this stays consistent
    with `_BULLET_LINE_RE.search()` also finding no match on the SAME
    raw text -- detector and extractor never disagree."""
    text = "## Revised Acceptance Criteria\n1.\nPlease update X\n"
    assert delta.extract_directive_items(text) == []
    assert delta._BULLET_LINE_RE.search(text) is None


def test_extract_directive_items_marker_with_trailing_whitespace_only_yields_no_item():
    """Finding C variant: a marker line followed only by trailing
    whitespace (`"1.    "`) before the next line's prose also yields zero
    items, staying consistent with the detector."""
    text = "## Revised Acceptance Criteria\n1.    \nPlease update X\n"
    assert delta.extract_directive_items(text) == []
    assert delta._BULLET_LINE_RE.search(text) is None


# AC3: positive representation matrix -----------------------------------


_CF_HTML_ORDERED_DIRECTIVE = (
    "<html>\n<body>\n<!--StartFragment-->\n<ol>\n"
    "<li>Extend the ordered list marker detection for structured "
    "directives.</li>\n</ol>\n<!--EndFragment-->\n</body>\n</html>"
    "## Revised Acceptance Criteria\n\n"
    "1. Extend the ordered list marker detection for structured "
    "directives.\n"
)
_CF_HTML_UNORDERED_DIRECTIVE = (
    "<html>\n<body>\n<!--StartFragment-->\n<ul>\n"
    "<li>Add retry handling to the sync worker for transient network "
    "failures.</li>\n</ul>\n<!--EndFragment-->\n</body>\n</html>"
    "## Revised Acceptance Criteria\n\n"
    "- Add retry handling to the sync worker for transient network "
    "failures.\n"
)


@pytest.mark.parametrize(
    "label,text",
    [
        (
            "markdown_unordered_dash",
            "## Revised Acceptance Criteria\n\n"
            "- Please extend the ordered list marker detection.\n",
        ),
        (
            "markdown_unordered_asterisk",
            "## Revised Acceptance Criteria\n\n"
            "* Please extend the ordered list marker detection.\n",
        ),
        (
            "markdown_native_ordered",
            "## Revised Acceptance Criteria\n\n"
            "1. Please extend the ordered list marker detection.\n"
            "2. Please also add regression tests for the new markers.\n",
        ),
        ("cf_html_ordered_paste", _CF_HTML_ORDERED_DIRECTIVE),
        ("cf_html_unordered_paste", _CF_HTML_UNORDERED_DIRECTIVE),
    ],
)
def test_positive_representation_matrix_yields_explicit_directive_confidence(label, text):
    """AC3: GIVEN each representative directive shape -- Markdown unordered
    `- `/`* `, native Markdown ordered `1. `/`2. `, and (via existing
    envelope canonicalization) CF_HTML clipboard-paste `<ol><li>`/`<ul><li>`
    -- accompanied by a genuine directive section marker
    WHEN `classify_directive_confidence()` runs
    THEN every representation yields `directive.confidence: explicit`
    (#2730/#2805 regression fixtures)."""
    confidence = delta.classify_directive_confidence(text)
    assert confidence == delta.DIRECTIVE_CONFIDENCE_EXPLICIT, label


# AC4: negative controls ---------------------------------------------------


@pytest.mark.parametrize(
    "label,text,operator_asserted_human_context",
    [
        (
            "plain_observation_list_no_marker_verb",
            "1. The button color is blue.\n2. The header font size is 14px.\n",
            True,
        ),
        (
            "failure_log_ordered_list",
            "1. Traceback (most recent call last):\n"
            "2. ValueError: invalid literal for int() with base 10.\n",
            True,
        ),
        (
            "todo_status_report_ordered_list",
            "1. TODO: revisit this later.\n2. Status: pending review.\n",
            True,
        ),
        (
            "mixed_list_no_imperative_directive_content",
            "- Observed a timeout after 30 seconds.\n1. Retry count is 3.\n",
            True,
        ),
        (
            "non_with_human_context_lane_comment",
            "1. Please fix the ordered list detection gap.\n"
            "2. Please add regression tests.\n",
            False,
        ),
    ],
)
def test_negative_controls_never_misclassify_as_explicit(
    label, text, operator_asserted_human_context
):
    """AC4: GIVEN a plain observation list, a failure log, a TODO/status
    report, an ordered/unordered list carrying no imperative directive
    content, and a genuine-looking directive on a NON-`with_human_context`
    lane (no known directive-section marker present in any case)
    WHEN `classify_directive_confidence()` runs
    THEN none of them are misclassified as `explicit` -- the existing
    `_has_semantic_directive_bullet()` imperative-verb/negation safeguard is
    not weakened by the new ordered-list detection."""
    confidence = delta.classify_directive_confidence(
        text, operator_asserted_human_context=operator_asserted_human_context
    )
    assert confidence != delta.DIRECTIVE_CONFIDENCE_EXPLICIT, label


# ---------------------------------------------------------------------------
# PR #2814 OWNER REQUEST_CHANGES fix_delta (finding C, classifier level):
# a marker-only line must never, by itself, be able to promote
# `classify_directive_confidence()` to `explicit` -- the failure class is
# fixed all the way up from the regex (`_BULLET_LINE_RE`) through the
# extractor (`extract_directive_items()`) to the classifier, not merely
# as an isolated regex unit test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,text",
    [
        (
            "marker_only_line_no_trailing_whitespace",
            "## Revised Acceptance Criteria\n1.\nPlease update X\n",
        ),
        (
            "marker_only_line_trailing_whitespace_only",
            "## Revised Acceptance Criteria\n1.    \nPlease update X\n",
        ),
    ],
)
def test_classify_directive_confidence_marker_only_line_does_not_promote_to_explicit(
    label, text
):
    """Finding C (classifier level): GIVEN a genuine directive section
    marker heading (so `extract_directive_markers()` is non-empty) followed
    by a marker-only ordered-list line (`1.` or `1.    `) and the directive
    prose only on the NEXT line
    WHEN `classify_directive_confidence()` runs
    THEN it must NOT be promoted to `explicit` on the strength of that
    marker-only line alone -- `has_bullets` and `extract_directive_items()`
    must agree that there is no structured bullet-list content here, so
    the result falls back to `ambiguous` (marker present, no structured
    list)."""
    assert delta.extract_directive_items(text) == [], label
    assert delta._BULLET_LINE_RE.search(text) is None, label
    confidence = delta.classify_directive_confidence(text)
    assert confidence == delta.DIRECTIVE_CONFIDENCE_AMBIGUOUS, label
