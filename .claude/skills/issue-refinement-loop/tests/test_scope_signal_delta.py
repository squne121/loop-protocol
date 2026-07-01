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
