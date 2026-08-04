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
  and instead fixes current consumer behavior against the real
  `route_loop_verdict_v2(reviewer_verdict, live_mergeability)` 2-argument API
  and its 10 documented branches (AC11), verified by actually importing and
  executing the production module rather than string-matching alone.
- `check_pr_review_gates.py`'s `finalize_verdict()` is confirmed to be pure
  deterministic gate-boolean aggregation (no subprocess/gh/LLM calls), which
  is explicitly distinguished from the semantic-findings-generation
  prohibition (AC6).

AC1 and AC6/AC11 import the referenced production modules directly (skill
resolver check, `route_loop_verdict_v2`, `check_pr_review_gates`); the
remaining tests are pure text/YAML fixture checks against the repository's
own tracked files.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENT_PATH = REPO_ROOT / ".claude" / "agents" / "pr-reviewer.md"
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "SKILL.md"
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts"
ROUTE_LOOP_VERDICT_V2_PATH = (
    REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts" / "route_loop_verdict_v2.py"
)
CHECK_PR_REVIEW_GATES_PATH = SCRIPTS_DIR / "check_pr_review_gates.py"


def _import_module_from_path(module_name: str, path: Path):
    import sys

    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass field resolution (e.g. Literal[...]
    # string annotations) looks the module up via sys.modules[cls.__module__].
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


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


def test_frontmatter_skills_normalized_exact_and_skill_resolves() -> None:
    """GIVEN pr-reviewer.md frontmatter
    WHEN parsed with a strict duplicate-key-rejecting loader and normalized
    THEN skills == ["pr-review-judge"] exactly, AND the referenced skill
      actually resolves: pr-review-judge/SKILL.md exists, its frontmatter
      `name` matches the directory name (`pr-review-judge`), and it does not
      carry `disable-model-invocation: true` (which would make the preload
      inert)."""
    text = AGENT_PATH.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter_strict(text)
    normalized = _normalize_skills(frontmatter)
    assert normalized == ["pr-review-judge"]

    assert SKILL_PATH.is_file(), f"referenced skill does not exist: {SKILL_PATH}"
    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    skill_frontmatter = _parse_frontmatter_strict(skill_text)
    assert skill_frontmatter.get("name") == "pr-review-judge" == SKILL_PATH.parent.name
    assert skill_frontmatter.get("disable-model-invocation") is not True


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
    "TOCTOU",
    "toctou",
]

_SHADOW_TEST_PATTERNS = [
    "PYTEST_CURRENT_TEST",
    'sys.modules.get("pytest")',
    "sys.modules['pytest']",
]

# GitHub mutation command tokens. A subprocess/gh invocation is treated as a
# forbidden mutation call when its argument list contains "gh" together with
# both members of one of these token pairs (order-insensitive), regardless of
# how the argv list is constructed (literal list, f-string join, etc. -- see
# _iter_subprocess_command_literals below for what this AST scan can see).
_MUTATION_TOKEN_PAIRS = [
    ("pr", "review"),
    ("issue", "edit"),
]

_SUBPROCESS_CALL_ATTRS = {"run", "call", "check_call", "check_output", "Popen"}


def _production_script_files() -> list[Path]:
    assert SCRIPTS_DIR.is_dir()
    return sorted(
        p
        for p in SCRIPTS_DIR.glob("*.py")
        if p.is_file() and p.parent == SCRIPTS_DIR
    )


def _iter_subprocess_command_literals(tree: ast.AST):
    """Yield the list of string literals passed as the command argv to any
    subprocess.run/call/check_call/check_output/Popen call found in `tree`
    (AST-based, so it is not fooled by whitespace/comment formatting of a
    plain substring grep)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_call = (
            isinstance(func, ast.Attribute)
            and func.attr in _SUBPROCESS_CALL_ATTRS
        ) or (
            isinstance(func, ast.Name) and func.id in _SUBPROCESS_CALL_ATTRS
        )
        if not is_subprocess_call or not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, (ast.List, ast.Tuple)):
            literals = [
                elt.value
                for elt in first_arg.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            if literals:
                yield literals


def _contains_forbidden_mutation_argv(literals: list[str]) -> str | None:
    if "gh" not in literals:
        return None
    lowered = {tok.lower() for tok in literals}
    for a, b in _MUTATION_TOKEN_PAIRS:
        if a in lowered and b in lowered:
            return f"gh {a} {b}"
    return None


def test_scripts_do_not_own_semantic_findings_but_gate_aggregation_is_allowed() -> None:
    """GIVEN every top-level production script under pr-review-judge/scripts/
    WHEN scanned via AST for subprocess/gh mutation-command invocations
      (`gh pr review` / `gh issue edit`), publisher hash/identity/TOCTOU gate
      re-implementation markers, and test-only shadow implementation markers
    THEN none are found -- but this prohibition does NOT extend to
      deterministic gate-boolean aggregation (see
      test_finalize_verdict_is_pure_deterministic_gate_aggregation below),
      which is explicitly allowed."""
    scripts = _production_script_files()
    assert scripts, "expected at least one production script to audit"

    for script in scripts:
        content = script.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(script))

        for literals in _iter_subprocess_command_literals(tree):
            forbidden = _contains_forbidden_mutation_argv(literals)
            assert forbidden is None, (
                f"{script.name} invokes a forbidden GitHub mutation command "
                f"via subprocess argv: {forbidden!r} ({literals!r})"
            )

        for forbidden in _FORBIDDEN_SCRIPT_SUBSTRINGS:
            assert forbidden not in content, f"{script.name} contains forbidden {forbidden!r}"
        for shadow_pattern in _SHADOW_TEST_PATTERNS:
            assert shadow_pattern not in content, (
                f"{script.name} contains a test-only shadow implementation marker: "
                f"{shadow_pattern!r}"
            )


def test_finalize_verdict_is_pure_deterministic_gate_aggregation() -> None:
    """GIVEN check_pr_review_gates.py's finalize_verdict()
    WHEN its AST body is inspected and it is executed against a fake gate
      result set with subprocess.run patched to raise on any invocation
    THEN it contains no Call to subprocess/gh/LLM/network primitives, and it
      runs to completion (never touching subprocess), producing a verdict
      purely from the boolean `status == FAIL` aggregation of its gates --
      confirming it is deterministic gate aggregation, not semantic findings
      generation or GitHub mutation."""
    assert CHECK_PR_REVIEW_GATES_PATH.is_file()
    module = _import_module_from_path(
        "pr_review_judge_check_pr_review_gates_ac6", CHECK_PR_REVIEW_GATES_PATH
    )

    source = CHECK_PR_REVIEW_GATES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CHECK_PR_REVIEW_GATES_PATH))
    finalize_verdict_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "finalize_verdict":
            finalize_verdict_node = node
            break
    assert finalize_verdict_node is not None, "finalize_verdict() not found in AST"

    forbidden_call_names = {"run", "call", "check_call", "check_output", "Popen", "urlopen", "request"}
    for node in ast.walk(finalize_verdict_node):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            assert name not in forbidden_call_names, (
                f"finalize_verdict() calls {name!r}; expected pure boolean gate "
                "aggregation with no subprocess/network calls"
            )

    import subprocess as _subprocess

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("finalize_verdict() must never invoke subprocess")

    original_run = _subprocess.run
    _subprocess.run = _raise_if_called
    try:
        checker = module.CheckPRReviewGates.__new__(module.CheckPRReviewGates)
        checker.result = module.PRReviewGateResult(
            gates=[
                module.GateResult(gate_id="g1", gate_name="g1", status=module.GateStatus.PASS.value),
                module.GateResult(gate_id="g2", gate_name="g2", status=module.GateStatus.FAIL.value),
            ],
        )
        checker.finalize_verdict()
        assert checker.result.verdict == module.Verdict.REQUEST_CHANGES.value

        checker.result.gates = [
            module.GateResult(gate_id="g1", gate_name="g1", status=module.GateStatus.PASS.value),
        ]
        checker.finalize_verdict()
        assert checker.result.verdict == module.Verdict.APPROVE.value
    finally:
        _subprocess.run = original_run


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

# route_loop_verdict_v2(reviewer_verdict, live_mergeability) representative
# branch exercises (Issue #1744 P0-2 / #1873). Chosen to hit each of the 10
# documented branches at least once.
_ROUTE_LOOP_VERDICT_V2_BRANCH_CASES = [
    (
        "conflict_mergeable_conflicting",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "CONFLICTING", "merge_state_status": "CLEAN"},
        "conflict_hard_stop",
    ),
    (
        "conflict_merge_state_status_dirty",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "DIRTY"},
        "conflict_hard_stop",
    ),
    (
        "human_review_required",
        {"verdict": "HUMAN_REVIEW_REQUIRED", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
        "route_human_escalation",
    ),
    (
        "request_changes",
        {"verdict": "REQUEST_CHANGES", "reviewed_head_sha": "a" * 40, "blockers": ["x"], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
        "continue_loop",
    ),
    (
        "approve_with_nonempty_blockers_is_fail_closed",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": ["oops"], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
        "fail_closed",
    ),
    (
        "approve_stale_reviewed_head_sha",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "b" * 40, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
        "route_stale_head_rereview",
    ),
    (
        "approve_mergeability_unknown",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "UNKNOWN", "merge_state_status": "CLEAN"},
        "fail_closed",
    ),
    (
        "approve_behind_synthesizes_update_branch",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "BEHIND"},
        "route_to_update_branch",
    ),
    (
        "approve_blocked_defers_to_ci_evaluator",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "BLOCKED"},
        "fail_closed",
    ),
    (
        "approve_unstable_defers_to_ci_evaluator",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "UNSTABLE"},
        "fail_closed",
    ),
    (
        "approve_draft_defers_to_ci_evaluator",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "DRAFT"},
        "fail_closed",
    ),
    (
        "approve_clean_is_approved",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
        "approved",
    ),
    (
        "approve_has_hooks_is_approved",
        {"verdict": "APPROVE", "reviewed_head_sha": "a" * 40, "blockers": [], "warnings": []},
        {"head_sha": "a" * 40, "mergeable": "MERGEABLE", "merge_state_status": "HAS_HOOKS"},
        "approved",
    ),
]


def test_consumer_inventory_matches_real_route_loop_verdict_v2_branches() -> None:
    """GIVEN pr-review-judge/SKILL.md consumer_inventory section AND the
      production route_loop_verdict_v2(reviewer_verdict, live_mergeability)
      module
    WHEN the stale #631/#632 wait condition is checked for absence in the
      doc, and route_loop_verdict_v2 is actually imported and executed
      against representative payloads for each documented branch
    THEN the stale marker is absent, the SKILL.md text documents the 10
      real branches (not an 8-fixture list requiring a `kind`/`action`
      reviewer self-report the real 2-argument API does not accept), and
      each real invocation resolves to the route this doc claims (not just
      a string match)."""
    text = SKILL_PATH.read_text(encoding="utf-8")

    for stale_marker in _STALE_WAIT_MARKERS:
        assert stale_marker not in text, f"stale wait condition marker still present: {stale_marker!r}"

    consumer_inventory_start = text.index("consumer_inventory")
    consumer_section = text[consumer_inventory_start:]
    assert "route_loop_verdict_v2(reviewer_verdict, live_mergeability)" in consumer_section
    assert "test_route_loop_verdict_v2.py" in consumer_section
    assert "test_route_loop_verdict_v2_merge_state_status_only.py" in consumer_section

    assert ROUTE_LOOP_VERDICT_V2_PATH.is_file()
    module = _import_module_from_path(
        "pr_review_judge_route_loop_verdict_v2_ac11", ROUTE_LOOP_VERDICT_V2_PATH
    )

    for name, reviewer_verdict, live_mergeability, expected_route in _ROUTE_LOOP_VERDICT_V2_BRANCH_CASES:
        decision = module.route_loop_verdict_v2(reviewer_verdict, live_mergeability)
        assert decision.route == expected_route, (
            f"{name}: expected route {expected_route!r}, got {decision.route!r} "
            f"(reason_code={decision.reason_code!r})"
        )
