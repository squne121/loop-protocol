#!/usr/bin/env python3
"""
Tests for VC quality guards (#648).

AC1: rg --include / --include=... → rg_option_mismatch blocked
     --include-zero NOT blocked (different option)
     grep --include NOT blocked (grep syntax, not rg)

AC2: rg search path too broad / unbounded → broad_search_path_unbounded blocked
     Allowed Paths explicitly covering broad dir → NOT blocked

AC3: shell operator tokens (&&, ||, bare |, ;) → compound_command_disallowed blocked
     quoted regex alternation "foo|bar" → NOT compound
     existing regex_literal_pipe_suspected (#589) NOT broken

AC4: existing asset hit where VC returns exit_code=0 but cannot identify target artifact
     → unexpected_pass blocked (no new top-level schema needed)

AC5: all AC1-AC4 test names pass
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional


# Paths to the scripts under test
_TESTS_DIR = Path(__file__).parent
_SKILLS_DIR = _TESTS_DIR.parent
_SCRIPTS_DIR = _SKILLS_DIR / "scripts"

PREFLIGHT_SCRIPT = _SCRIPTS_DIR / "baseline_vc_preflight.py"

# Add scripts dir to sys.path for direct imports
sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_preflight(body_content: str, issue_num: int = 999) -> dict:
    """Run baseline_vc_preflight on a string of body content via a temp file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body_content)
        fixture_file = f.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(PREFLIGHT_SCRIPT),
                "--body-file",
                fixture_file,
                "--issue",
                str(issue_num),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout, f"No output from preflight: stderr={result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(fixture_file)


def make_body(vc_block: str, allowed_paths: Optional[List[str]] = None) -> str:
    """Wrap a VC block in a minimal Issue body.

    allowed_paths: list of path strings for ## Allowed Paths section.
                   Defaults to ["some/path.py"] for backward compatibility.
    """
    if allowed_paths is None:
        allowed_paths = ["some/path.py"]
    paths_section = "\n".join(f"- {p}" for p in allowed_paths)
    return f"""## Outcome
Test outcome.

## Acceptance Criteria
- AC1: test

## Verification Commands

```bash
{vc_block}
```

## Allowed Paths
{paths_section}

## Stop Conditions
- none
"""


def _get_result_by_command_fragment(data: dict, fragment: str) -> dict:
    """Find the first result whose raw_command contains `fragment`."""
    for r in data["results"]:
        if fragment in r["raw_command"]:
            return r
    return {}


# ---------------------------------------------------------------------------
# AC1: rg_option_mismatch
# ---------------------------------------------------------------------------


def test_rg_include_is_rg_option_mismatch_blocked():
    """AC1: rg --include="*.py" must be classified as rg_option_mismatch and blocked."""
    body = make_body('rg --include="*.py" some_pattern .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py')
    data = run_preflight(body)
    assert data["results"], "Expected at least one result"
    r = data["results"][0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "rg_option_mismatch", f"Expected rg_option_mismatch, got {r['category']}"
    assert r["decision"] == "blocked"


def test_rg_include_equal_form_is_rg_option_mismatch_blocked():
    """AC1: rg --include=*.py (--flag=value form) must also be rg_option_mismatch."""
    body = make_body("rg --include=*.py some_pattern .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked"
    assert r["category"] == "rg_option_mismatch"


def test_rg_include_zero_is_not_rg_option_mismatch():
    """AC1: rg --include-zero is a valid rg option and must NOT trigger rg_option_mismatch."""
    # --include-zero is a valid rg option (NUL-separated output), not an error.
    # However it has no path, so it may be blocked as broad_search_path_unbounded.
    # The key assertion is: category != rg_option_mismatch.
    body = make_body("rg --include-zero some_pattern .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] != "rg_option_mismatch", (
        f"--include-zero must NOT be classified as rg_option_mismatch; got {r['category']}"
    )


def test_grep_include_is_not_rg_option_mismatch():
    """AC1: grep --include=*.py is valid grep syntax and must NOT trigger rg_option_mismatch."""
    # grep --include is a valid grep option; only rg --include is the mismatch.
    body = make_body("grep --include=*.py some_pattern .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] != "rg_option_mismatch", (
        f"grep --include must NOT be classified as rg_option_mismatch; got {r['category']}"
    )


# ---------------------------------------------------------------------------
# AC2: broad_search_path_unbounded
# ---------------------------------------------------------------------------


def test_rg_without_path_is_repo_wide_broad_search():
    """AC2: rg without path argument (searches entire repo) must be broad_search_path_unbounded blocked."""
    body = make_body("rg some_unique_pattern_for_test")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "broad_search_path_unbounded", f"Expected broad_search_path_unbounded, got {r['category']}"
    assert r["decision"] == "blocked"


def test_rg_dot_path_is_broad_search():
    """AC2: rg pattern . (repo root) must be broad_search_path_unbounded blocked."""
    body = make_body("rg some_unique_pattern_for_test .")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked"
    assert r["category"] == "broad_search_path_unbounded"


def test_rg_docs_root_is_broad_when_allowed_paths_are_specific():
    """AC2: rg pattern docs/ must be broad_search_path_unbounded when Allowed Paths are specific files."""
    # The Issue body's Allowed Paths only list specific files, not docs/ broadly.
    body = make_body("rg some_unique_pattern_for_test docs/")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "broad_search_path_unbounded", f"Expected broad_search_path_unbounded, got {r['category']}"


def test_rg_specific_allowed_file_is_not_broad():
    """AC2: rg pattern with a specific file path covered by Allowed Paths must NOT be broad_search_path_unbounded."""
    # A specific file path that exists in the repo, AND is in Allowed Paths
    specific_path = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    body = make_body(
        f"rg some_pattern {specific_path}",
        allowed_paths=[specific_path],
    )
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] != "broad_search_path_unbounded", (
        f"Specific file path in Allowed Paths must NOT be broad_search_path_unbounded; got {r['category']}"
    )


def test_rg_specific_allowed_dir_nested_is_not_broad():
    """AC2: rg with a specific nested directory path covered by Allowed Paths must NOT be broad_search_path_unbounded."""
    # The nested dir is listed in Allowed Paths → allowed (same path)
    specific_dir = ".claude/skills/issue-contract-review/"
    body = make_body(
        f"rg some_pattern {specific_dir}",
        allowed_paths=[specific_dir],
    )
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] != "broad_search_path_unbounded", (
        f"Specific nested directory in Allowed Paths must NOT be broad_search_path_unbounded; got {r['category']}"
    )


# ---------------------------------------------------------------------------
# AC3: compound_command_disallowed (+ #589 non-regression)
# ---------------------------------------------------------------------------


def test_shell_pipeline_is_compound_command_disallowed():
    """AC3: rg pattern | head -5 (bare pipe) must be compound_command_disallowed blocked."""
    body = make_body("rg some_pattern .claude/skills/ | head -5")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "compound_command_disallowed", f"Expected compound_command_disallowed, got {r['category']}"
    assert r["decision"] == "blocked"


def test_double_ampersand_is_compound_command_disallowed():
    """AC3: cmd1 && cmd2 must be compound_command_disallowed blocked."""
    body = make_body("rg pattern file && echo done")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked"
    assert r["category"] == "compound_command_disallowed"


def test_double_pipe_is_compound_command_disallowed():
    """AC3: cmd1 || cmd2 must be compound_command_disallowed blocked."""
    body = make_body("rg pattern file || echo not_found")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked"
    assert r["category"] == "compound_command_disallowed"


def test_semicolon_is_compound_command_disallowed():
    """AC3: cmd1 ; cmd2 must be compound_command_disallowed blocked."""
    body = make_body("rg pattern file; echo done")
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked"
    assert r["category"] == "compound_command_disallowed"


def test_regex_alternation_pipe_is_not_compound():
    """AC3 + #589 non-regression: rg -n 'foo|bar' (quoted regex alternation) must NOT be compound_command_disallowed."""
    # Quoted pipe inside rg pattern is regex alternation, not a shell pipeline.
    specific_file = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    body = make_body(f'rg -n "foo|bar" {specific_file}')
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] != "compound_command_disallowed", (
        f"Quoted regex alternation must NOT be compound_command_disallowed; got {r['category']}"
    )


def test_regex_literal_pipe_suspected_still_works():
    """AC3 + #589 non-regression: rg 'foo\\|bar' must still be regex_literal_pipe_suspected."""
    # This tests that the #589 behavior is preserved: backslash-pipe in rg pattern
    # is still detected as regex_literal_pipe_suspected.
    specific_file = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    body = make_body(f'rg "foo\\\\|bar" {specific_file}')
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] == "regex_literal_pipe_suspected", (
        f"Backslash-pipe in rg pattern must still be regex_literal_pipe_suspected; got {r['category']}"
    )


# ---------------------------------------------------------------------------
# AC4: unexpected_pass (existing asset hit without target artifact identification)
# ---------------------------------------------------------------------------


def test_existing_asset_hit_is_unexpected_pass():
    """AC4: rg returning exit_code=0 by hitting existing assets (not new artifact) → unexpected_pass blocked.

    This fixture demonstrates the case where a VC like:
      rg 'def classify_result' .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py
    passes at baseline because 'classify_result' already exists in the existing implementation.
    The VC cannot distinguish between the existing asset and the new target artifact.

    The expected behavior is: exit_code=0 (rg found a match) → unexpected_pass / blocked.
    """
    # classify_result is a function that already exists in baseline_vc_preflight.py.
    # Running rg on it returns exit 0 even before any new implementation.
    existing_file = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    # Use a pattern that definitely exists in the current codebase.
    # Set Allowed Paths to include the file so broad path check doesn't block it first.
    body = make_body(f'rg "def classify_result" {existing_file}', allowed_paths=[existing_file])
    data = run_preflight(body)
    # This should run (not be statically blocked) and return exit_code=0
    r = data["results"][0]
    # The key assertion: exit_code=0 + not regression_gate → unexpected_pass
    assert r["classification"] == "unexpected_pass", (
        f"Expected unexpected_pass for existing asset hit, got {r['classification']} "
        f"(category={r['category']}, exit_code={r['exit_code']})"
    )
    assert r["decision"] == "blocked", f"Expected decision=blocked, got {r['decision']}"
    assert r["category"] == "unexpected_pass"


def test_unexpected_pass_classification_no_new_schema():
    """AC4: unexpected_pass must use the existing classification schema (no new top-level key)."""
    existing_file = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    body = make_body(f'rg "def classify_result" {existing_file}', allowed_paths=[existing_file])
    data = run_preflight(body)

    # Verify the top-level schema hasn't changed
    required_keys = {"schema", "issue", "repo", "generated_at", "source", "status", "summary", "results", "errors"}
    assert required_keys.issubset(set(data.keys())), (
        f"Missing required top-level keys: {required_keys - set(data.keys())}"
    )

    # Verify no new unexpected top-level schema was added
    unexpected_keys = set(data.keys()) - required_keys
    assert not unexpected_keys, f"Unexpected new top-level keys: {unexpected_keys}"

    # The result item should have the standard classification fields
    r = data["results"][0]
    assert "classification" in r
    assert "category" in r
    assert "decision" in r
    assert r["category"] == "unexpected_pass"


# ---------------------------------------------------------------------------
# AC2 (PR review fix): Allowed Paths containment-based broad path detection
# ---------------------------------------------------------------------------


def test_rg_docs_dir_with_allowed_paths_docs_is_not_broad():
    """AC2: rg pattern docs/ with Allowed Paths: docs/ → NOT broad (same path = covered)."""
    body = make_body("rg some_pattern docs/", allowed_paths=["docs/"])
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] != "broad_search_path_unbounded", (
        f"docs/ with Allowed Paths docs/ must NOT be broad; got category={r['category']}"
    )


def test_rg_docs_dir_with_allowed_paths_specific_file_is_broad():
    """AC2: rg pattern docs/ with Allowed Paths: docs/foo.md → broad (docs/ is parent of docs/foo.md)."""
    body = make_body("rg some_pattern docs/", allowed_paths=["docs/foo.md"])
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked", (
        f"docs/ with Allowed Paths docs/foo.md must be blocked; got {r['classification']}"
    )
    assert r["category"] == "broad_search_path_unbounded", (
        f"Expected broad_search_path_unbounded; got {r['category']}"
    )


def test_rg_claude_skills_dir_with_allowed_some_path_is_broad():
    """AC2: rg pattern .claude/skills/ with Allowed Paths: some/path.py → broad (no containment)."""
    body = make_body("rg some_pattern .claude/skills/", allowed_paths=["some/path.py"])
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked", (
        f"Expected blocked; got {r['classification']}"
    )
    assert r["category"] == "broad_search_path_unbounded", (
        f"Expected broad_search_path_unbounded; got {r['category']}"
    )


def test_rg_src_dir_with_allowed_paths_src_file_is_broad():
    """AC2: rg pattern src/ with Allowed Paths: src/foo.ts → broad (src/ is parent of src/foo.ts)."""
    body = make_body("rg some_pattern src/", allowed_paths=["src/foo.ts"])
    data = run_preflight(body)
    r = data["results"][0]
    assert r["classification"] == "blocked", (
        f"Expected blocked; got {r['classification']}"
    )
    assert r["category"] == "broad_search_path_unbounded", (
        f"Expected broad_search_path_unbounded; got {r['category']}"
    )


def test_rg_src_file_with_allowed_paths_src_dir_is_not_broad():
    """AC2: rg pattern src/foo.ts with Allowed Paths: src/ → NOT broad (file is under src/)."""
    body = make_body("rg some_pattern src/foo.ts", allowed_paths=["src/"])
    data = run_preflight(body)
    r = data["results"][0]
    assert r["category"] != "broad_search_path_unbounded", (
        f"src/foo.ts with Allowed Paths src/ must NOT be broad; got category={r['category']}"
    )


# ---------------------------------------------------------------------------
# AC5: All named tests exist and are collected by pytest
# (This is verified implicitly by the presence of the test functions above,
# but we add an explicit meta-test to confirm the required test names exist.)
# ---------------------------------------------------------------------------


def test_all_required_test_names_exist_in_this_module():
    """AC5: Verify that all required test names from the contract are present in this module."""
    required_names = [
        "test_rg_include_is_rg_option_mismatch_blocked",
        "test_rg_include_zero_is_not_rg_option_mismatch",
        "test_grep_include_is_not_rg_option_mismatch",
        "test_regex_alternation_pipe_is_not_compound",
        "test_shell_pipeline_is_compound_command_disallowed",
        "test_rg_without_path_is_repo_wide_broad_search",
        "test_rg_docs_root_is_broad_when_allowed_paths_are_specific",
        "test_rg_specific_allowed_file_is_not_broad",
        "test_existing_asset_hit_is_unexpected_pass",
        # PR review fix: containment-based tests
        "test_rg_docs_dir_with_allowed_paths_docs_is_not_broad",
        "test_rg_docs_dir_with_allowed_paths_specific_file_is_broad",
        "test_rg_claude_skills_dir_with_allowed_some_path_is_broad",
        "test_rg_src_dir_with_allowed_paths_src_file_is_broad",
        "test_rg_src_file_with_allowed_paths_src_dir_is_not_broad",
    ]
    import inspect
    current_module = sys.modules[__name__]
    module_functions = {name for name, _ in inspect.getmembers(current_module, inspect.isfunction)}

    missing = [name for name in required_names if name not in module_functions]
    assert not missing, f"Required test names not found in module: {missing}"


# ---------------------------------------------------------------------------
# #683: extract_allowed_paths backtick normalization
# ---------------------------------------------------------------------------


def test_extract_allowed_paths_backtick():
    """AC1: extract_allowed_paths strips backtick wrapping and annotations."""
    from baseline_vc_preflight import extract_allowed_paths

    body = """## Allowed Paths

- `src/render/CanvasRenderer.ts`（敵描画ループに HP ラベル描画呼び出しを追加）
- `src/render/renderUtils.ts`（新規・数値フォーマットと描画ヘルパー）
- `tests/render/renderUtils.test.ts`（新規・format / bounds / save-restore の回帰テスト）
- `tests/e2e/m2-combat-mvp.spec.ts`（最大 HP 敵の描画 bounds 検証を追加）
- `docs/product/features/combat-core.md`（数値表示ポリシーを追記）
"""
    paths = extract_allowed_paths(body)
    assert paths == [
        "src/render/CanvasRenderer.ts",
        "src/render/renderUtils.ts",
        "tests/render/renderUtils.test.ts",
        "tests/e2e/m2-combat-mvp.spec.ts",
        "docs/product/features/combat-core.md",
    ]


def test_rg_not_broad_with_backtick_allowed_paths():
    """AC2: rg with file in backtick-annotated Allowed Paths is not broad."""
    from baseline_vc_preflight import extract_allowed_paths, _rg_has_broad_search_path

    body = """## Allowed Paths

- `tests/render/renderUtils.test.ts`（新規・回帰テスト）
- `src/render/renderUtils.ts`（新規・ヘルパー）
"""
    allowed_paths = extract_allowed_paths(body)
    # These should NOT be broad (specific files in Allowed Paths)
    assert not _rg_has_broad_search_path(
        ["rg", "-n", "formatCombatNumber", "tests/render/renderUtils.test.ts"],
        allowed_paths=allowed_paths,
    )
    assert not _rg_has_broad_search_path(
        ["rg", "-n", "save|restore", "src/render/renderUtils.ts"],
        allowed_paths=allowed_paths,
    )


def test_rg_broad_paths_still_detected():
    """AC3: True broad paths (., /, no path, parent dir) still detected."""
    from baseline_vc_preflight import _rg_has_broad_search_path

    allowed_paths = ["tests/render/renderUtils.test.ts", "src/render/renderUtils.ts"]
    # No path argument → broad
    assert _rg_has_broad_search_path(["rg", "pattern"], allowed_paths=allowed_paths)
    # '.' → broad
    assert _rg_has_broad_search_path(["rg", "pattern", "."], allowed_paths=allowed_paths)
    # Parent directory of allowed path → broad
    assert _rg_has_broad_search_path(["rg", "pattern", "tests/render"], allowed_paths=allowed_paths)
    assert _rg_has_broad_search_path(["rg", "pattern", "src"], allowed_paths=allowed_paths)


def test_extract_allowed_paths_markdown_bullet_variants():
    """AC1 extension: +/* bullets are also handled."""
    from baseline_vc_preflight import extract_allowed_paths

    body = """## Allowed Paths

* `src/render/CanvasRenderer.ts`（描画）
+ `tests/render/foo.test.ts` (test)
- `docs/dev/body-authoring.md`
"""
    paths = extract_allowed_paths(body)
    assert paths == [
        "src/render/CanvasRenderer.ts",
        "tests/render/foo.test.ts",
        "docs/dev/body-authoring.md",
    ]


def test_extract_allowed_paths_plain_backtick_line_no_bullet():
    """AC1 extension: bare `path`（注釈）lines without bullet marker."""
    from baseline_vc_preflight import extract_allowed_paths

    body = """## Allowed Paths

`src/render/CanvasRenderer.ts`（敵描画）
"""
    paths = extract_allowed_paths(body)
    assert paths == ["src/render/CanvasRenderer.ts"]


def test_extract_allowed_paths_annotation_no_backtick():
    """AC1 extension: path（annotation）without backtick strips annotation."""
    from baseline_vc_preflight import extract_allowed_paths

    body = """## Allowed Paths

- src/render/CanvasRenderer.ts（描画ループに追加）
"""
    paths = extract_allowed_paths(body)
    assert paths == ["src/render/CanvasRenderer.ts"]
