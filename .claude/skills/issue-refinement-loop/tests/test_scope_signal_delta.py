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
