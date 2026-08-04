"""Static contract-drift tests for the pr-reviewer / pr-review-judge pair (#1744).

These tests verify that:

- `pr-reviewer.md` frontmatter declares `skills: [pr-review-judge]` exactly
  (AC1), parsed with a strict, duplicate-key-rejecting YAML loader.
- `pr-reviewer.md` body no longer duplicates the detailed
  Allowed Paths Gate procedure that `pr-review-judge` owns, while identifier
  separation (`pr_number` is never conflated with `issue_number`) is
  preserved (AC3).
- deterministic processing scripts under `pr-review-judge/scripts/` do not
  perform semantic verdict generation, GitHub mutation, or re-implement the
  publisher's hash/identity/TOCTOU gates, and contain no test-only shadow
  implementations (AC6).
- `pr-reviewer.md` documents `agent_terminal_state` / `verdict` /
  `publish_event` / `merge_ready` as distinct axes (AC7).
- the `consumer_inventory` guard in `pr-review-judge/SKILL.md` no longer
  references the stale "#631/#632 のランタイム挙動完了まで" wait condition,
  and instead fixes current consumer behavior with the 8 production
  fixtures (AC11).

No production module import is required: these are pure text/YAML fixture
checks against the repository's own tracked files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENT_PATH = REPO_ROOT / ".claude" / "agents" / "pr-reviewer.md"
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "SKILL.md"
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts"


# ---------------------------------------------------------------------------
# Strict, duplicate-key-rejecting YAML loader (frontmatter parsing helper)
# ---------------------------------------------------------------------------


class _DuplicateKeyError(ValueError):
    def __init__(self, key: Any) -> None:
        self.key = key
        super().__init__(f"duplicate mapping key: {key!r}")


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys."""


def _strict_construct_mapping(loader: _StrictSafeLoader, node, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _strict_construct_mapping,
)


def _extract_frontmatter_text(markdown_text: str) -> str:
    match = re.match(r"^---\n(.*?\n)---\n", markdown_text, flags=re.DOTALL)
    assert match is not None, "frontmatter delimiters (---) not found"
    return match.group(1)


def _parse_yaml_strict(yaml_text: str) -> dict[str, Any]:
    loaded = yaml.load(yaml_text, Loader=_StrictSafeLoader)
    assert isinstance(loaded, dict), "yaml must parse to a mapping"
    return loaded


def _parse_frontmatter_strict(markdown_text: str) -> dict[str, Any]:
    frontmatter_text = _extract_frontmatter_text(markdown_text)
    return _parse_yaml_strict(frontmatter_text)


def _normalize_skills(frontmatter: dict[str, Any]) -> list[str] | None:
    """Normalize the `skills` field to a list[str], or None if absent/invalid."""
    if "skills" not in frontmatter:
        return None
    raw = frontmatter["skills"]
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return raw
    return None


# ---------------------------------------------------------------------------
# AC1: frontmatter skills == ["pr-review-judge"] (strict + negative fixtures)
# ---------------------------------------------------------------------------


def test_frontmatter_skills_normalized_exact() -> None:
    """GIVEN pr-reviewer.md frontmatter
    WHEN parsed with a strict duplicate-key-rejecting loader and normalized
    THEN skills == ["pr-review-judge"] exactly."""
    text = AGENT_PATH.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter_strict(text)
    normalized = _normalize_skills(frontmatter)
    assert normalized == ["pr-review-judge"]


def test_frontmatter_skills_missing_does_not_normalize_to_expected() -> None:
    """GIVEN frontmatter with no `skills` key
    WHEN normalized
    THEN the result is None, not ["pr-review-judge"]."""
    frontmatter = _parse_yaml_strict("name: pr-reviewer\ndescription: x\nmodel: sonnet\n")
    assert _normalize_skills(frontmatter) is None


def test_frontmatter_skills_disabled_empty_list_does_not_normalize_to_expected() -> None:
    """GIVEN frontmatter with `skills: []` (disabled)
    WHEN normalized
    THEN the result is not ["pr-review-judge"]."""
    frontmatter = _parse_yaml_strict("name: pr-reviewer\nskills: []\n")
    assert _normalize_skills(frontmatter) != ["pr-review-judge"]


def test_frontmatter_skills_name_mismatch_does_not_normalize_to_expected() -> None:
    """GIVEN frontmatter with a typo'd skill name
    WHEN normalized
    THEN the result does not equal ["pr-review-judge"]."""
    frontmatter = _parse_yaml_strict("name: pr-reviewer\nskills:\n  - pr-review-judg\n")
    assert _normalize_skills(frontmatter) != ["pr-review-judge"]


def test_frontmatter_malformed_duplicate_key_yaml_raises() -> None:
    """GIVEN frontmatter YAML with a duplicate top-level key
    WHEN parsed with the strict loader
    THEN it raises instead of silently last-wins resolving."""
    malformed = "name: pr-reviewer\nname: pr-reviewer-dup\nskills:\n  - pr-review-judge\n"
    with pytest.raises(_DuplicateKeyError):
        _parse_yaml_strict(malformed)


def test_live_agent_frontmatter_has_no_duplicate_keys() -> None:
    """GIVEN the live pr-reviewer.md frontmatter
    WHEN parsed with the strict loader
    THEN it does not raise (no duplicate keys in production)."""
    text = AGENT_PATH.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter_strict(text)
    assert frontmatter["name"] == "pr-reviewer"


# ---------------------------------------------------------------------------
# AC3: no duplicated procedure sections; pr_number/issue_number not conflated
# ---------------------------------------------------------------------------


_DUPLICATED_PROCEDURE_MARKERS = [
    # Detailed Allowed Paths Gate algorithm text owned by
    # pr-review-judge/references/allowed-paths-gate.md -- must not be
    # duplicated verbatim in the thin agent binding.
    "git diff --name-status -M -z",
    "changed_files_source_policy",
    "audited_paths[]",
    "github_pull_request_files_api_with_previous_filename",
]


def test_agent_body_excludes_duplicated_procedure_sections_but_retains_publisher_number_separation() -> (
    None
):
    """GIVEN pr-reviewer.md body
    WHEN scanned for duplicated command/algorithm detail owned by
      pr-review-judge SKILL.md / references
    THEN none of those markers are present (the agent points to the skill
      instead of re-describing it), and if `issue_number` is mentioned at
      all it is never presented as interchangeable with `pr_number`
      (identifier separation, PR #1825)."""
    text = AGENT_PATH.read_text(encoding="utf-8")

    for marker in _DUPLICATED_PROCEDURE_MARKERS:
        assert marker not in text, f"duplicated procedure marker still present: {marker!r}"

    # pr_number must remain the sole required identifier in the Input section.
    assert "`pr_number`（必須）" in text

    # If issue_number appears anywhere, it must never appear on the same
    # line treated as equivalent to pr_number (i.e. it must be clearly
    # distinguished, not conflated). Since the current architecture routes
    # issue resolution through `Closes #N` in the PR body (owned by
    # pr-review-judge SKILL.md Step 1), pr-reviewer.md itself should not
    # introduce a competing issue_number input field.
    for line in text.splitlines():
        if "issue_number" in line:
            assert "pr_number" not in line, (
                "issue_number and pr_number must not be conflated on the same line: "
                f"{line!r}"
            )

    # The agent body must point to the skill as the owner of the detailed
    # procedure instead of re-describing it (DRY).
    assert "references/allowed-paths-gate.md" in text
    assert "複製しない" in text


def test_agent_body_line_count_is_thin() -> None:
    """GIVEN pr-reviewer.md
    WHEN counting lines
    THEN it stays well below a duplicated full-procedure size (regression
      guard against re-introducing routing matrices/argv blocks)."""
    text = AGENT_PATH.read_text(encoding="utf-8")
    line_count = len(text.splitlines())
    assert line_count < 90, f"pr-reviewer.md grew to {line_count} lines; check for re-duplication"


# ---------------------------------------------------------------------------
# AC6: deterministic scripts do not own semantic verdict / mutation
# ---------------------------------------------------------------------------


_FORBIDDEN_SCRIPT_SUBSTRINGS = [
    "gh pr review",
    "gh issue edit",
    "TOCTOU",
    "toctou",
]

_SHADOW_TEST_PATTERNS = [
    "PYTEST_CURRENT_TEST",
    'sys.modules.get("pytest")',
    "sys.modules['pytest']",
]


def _production_script_files() -> list[Path]:
    assert SCRIPTS_DIR.is_dir()
    return sorted(
        p
        for p in SCRIPTS_DIR.glob("*.py")
        if p.is_file() and p.parent == SCRIPTS_DIR
    )


def test_scripts_do_not_own_semantic_verdict_or_mutation() -> None:
    """GIVEN every top-level production script under pr-review-judge/scripts/
    WHEN scanned for forbidden GitHub mutation calls and publisher
      hash/identity/TOCTOU gate re-implementation markers
    THEN none are found."""
    scripts = _production_script_files()
    assert scripts, "expected at least one production script to audit"

    for script in scripts:
        content = script.read_text(encoding="utf-8")
        for forbidden in _FORBIDDEN_SCRIPT_SUBSTRINGS:
            assert forbidden not in content, f"{script.name} contains forbidden {forbidden!r}"
        for shadow_pattern in _SHADOW_TEST_PATTERNS:
            assert shadow_pattern not in content, (
                f"{script.name} contains a test-only shadow implementation marker: "
                f"{shadow_pattern!r}"
            )


def test_skill_documents_deterministic_script_prohibitions() -> None:
    """GIVEN pr-review-judge/SKILL.md
    WHEN scanned for the AC6 prohibitions statement
    THEN it explicitly documents that scripts do not: auto-generate semantic
      findings, call gh mutation commands, re-implement publisher hash/
      identity/TOCTOU gates, or contain test-only shadow implementations."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "semantic findings" in text or "semantic findings（コード品質・設計判断" in text
    assert "TOCTOU" in text
    assert "shadow implementation" in text


# ---------------------------------------------------------------------------
# AC7: agent_terminal_state / verdict / publish_event / merge_ready distinct
# ---------------------------------------------------------------------------


def test_terminal_state_verdict_publish_event_merge_ready_are_distinct_axes() -> None:
    """GIVEN pr-reviewer.md
    WHEN scanned for the four-axis distinction section
    THEN all four axis names are documented, each with their own value
      domain, and the file states they are independent axes (not a
      verdict == terminal_state conflation)."""
    text = AGENT_PATH.read_text(encoding="utf-8")

    for axis in ("agent_terminal_state", "verdict", "publish_event", "merge_ready"):
        assert f"`{axis}`" in text, f"missing axis marker: {axis}"

    assert "completed" in text
    assert "insufficient_context" in text
    assert "blocked" in text
    assert "APPROVE" in text and "REQUEST_CHANGES" in text
    assert "COMMENT" in text

    assert "別軸" in text, "expected an explicit 'distinct axes' statement"
    assert "同一視する記述は用いない" in text or "同一視" in text


# ---------------------------------------------------------------------------
# AC11: consumer_inventory fixture guard replaces the stale wait condition
# ---------------------------------------------------------------------------


_STALE_WAIT_MARKERS = ["#631", "#632", "ランタイム挙動完了まで"]

_PRODUCTION_CONSUMER_FIXTURES = [
    "APPROVE+CLEAN+no actions",
    "APPROVE+BEHIND+valid update_branch",
    "APPROVE+BEHIND+missing action",
    "REQUEST_CHANGES",
    "stale expected_head_sha",
    "multiple actions",
    "body-only action while BEHIND",
    "unknown action-executor-skill",
]


def test_consumer_inventory_guard_uses_fixture_not_stale_issue_reference() -> None:
    """GIVEN pr-review-judge/SKILL.md consumer_inventory section
    WHEN scanned
    THEN the stale #631/#632 wait condition is absent and all 8 production
      consumer fixtures are present instead."""
    text = SKILL_PATH.read_text(encoding="utf-8")

    for stale_marker in _STALE_WAIT_MARKERS:
        assert stale_marker not in text, f"stale wait condition marker still present: {stale_marker!r}"

    consumer_inventory_start = text.index("consumer_inventory")
    consumer_section = text[consumer_inventory_start:]

    for fixture in _PRODUCTION_CONSUMER_FIXTURES:
        assert fixture in consumer_section, f"missing production consumer fixture: {fixture!r}"
