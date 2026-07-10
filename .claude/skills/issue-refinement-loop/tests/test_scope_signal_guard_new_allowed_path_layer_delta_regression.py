from __future__ import annotations

import hashlib
import importlib
import json
import sys
from unittest import mock
from pathlib import Path

import jsonschema

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCHEMAS_DIR = SKILL_ROOT / "schemas"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "scope_signal_delta"

sys.path.insert(0, str(SCRIPTS_DIR))

delta = importlib.import_module("scope_signal_delta")
plan = importlib.import_module("plan_refinement_loop")
preflight = importlib.import_module("run_refinement_preflight")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_fixture(name: str) -> dict:
    return _load_json(FIXTURES_DIR / f"{name}.json")


def _load_plan_schema() -> dict:
    return _load_json(SCHEMAS_DIR / "refinement_loop_plan_v1.json")


def _planner_input(issue_body: str, known_context: dict | None = None) -> dict:
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": 1413,
            "title": "実装: regression",
            "body": issue_body,
            "labels": ["phase/implementation"],
        },
        "comments": [],
        "known_context": known_context or {},
        "now": "2026-07-10T00:00:00+00:00",
    }


def _valid_issue_body(*, allowed_paths: str, in_scope: str, acceptance_criteria: str) -> str:
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: implementation\n"
        "parent_issue: \"none\"\n"
        "goal_ref: \"regression\"\n"
        "change_kind: code\n"
        "preflight_scope: implementation\n"
        "```\n\n"
        "## Parent Issue\n\n"
        "なし\n\n"
        "## Parent Goal Ref\n\n"
        "なし\n\n"
        "## Current Validated Scope\n\n"
        "- regression\n\n"
        "## Remaining Parent Gaps\n\n"
        "なし\n\n"
        "## Outcome\n\n"
        "regression\n\n"
        "## In Scope\n\n"
        f"{in_scope}\n\n"
        "## Out of Scope\n\n"
        "- none\n\n"
        "## Acceptance Criteria\n\n"
        f"{acceptance_criteria}\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "$ uv run --locked pytest -q\n"
        "```\n\n"
        "## Allowed Paths\n\n"
        f"{allowed_paths}\n\n"
        "## Stop Conditions\n\n"
        "- Allowed Paths 外の変更が必要な場合\n\n"
        "## Required Skills\n\n"
        "- issue-refinement-loop\n\n"
        "## Runtime Verification Applicability\n\n"
        "decision: not_applicable\n"
    )


def _line_sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw_line(body: str, line_number: int) -> str:
    return body.splitlines()[line_number - 1]


def _fixture_input(issue_body: str) -> dict:
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": 1413,
        "repo": "squne121/loop-protocol",
        "now": "2026-07-10T00:00:00+00:00",
        "issue": {
            "number": 1413,
            "title": "実装: regression",
            "body": issue_body,
            "labels": ["phase/implementation"],
        },
        "comments": [],
        "anchor_comment_urls": [],
        "known_context": {
            "scope_signal_delta_required": True,
        },
    }


def _raw_snapshot(issue_body: str, fetched_at: str) -> dict:
    return {
        "schema_version": "raw_issue_snapshot/v1",
        "fetched_at": fetched_at,
        "issue_number": 1413,
        "repo": "squne121/loop-protocol",
        "issue": {
            "number": 1413,
            "title": "実装: regression",
            "body": issue_body,
            "labels": ["phase/implementation"],
            "html_url": "https://github.com/squne121/loop-protocol/issues/1413",
        },
        "comments": [],
    }


def _run_preflight_fixture(
    tmp_path: Path,
    *,
    current_body: str,
    previous_body: str | None = None,
) -> tuple[dict, int, dict]:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(_fixture_input(current_body), ensure_ascii=False),
        encoding="utf-8",
    )
    if previous_body is not None:
        preflight._materialize_immutable_snapshot(
            tmp_path,
            1413,
            _raw_snapshot(previous_body, "2026-07-09T00:00:00+00:00"),
        )
    with mock.patch.object(preflight, "_find_repo_root", return_value=tmp_path):
        result, exit_code = preflight.run_preflight(
            issue_number=1413,
            repo="squne121/loop-protocol",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )
    planner_input = _load_json(
        tmp_path
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / "1413"
        / "planner_input.json"
    )
    return result, exit_code, planner_input


def _valid_delta_input(name: str) -> dict:
    payload = json.loads(json.dumps(_load_fixture(name)))
    payload["source_refs"] = {
        "before": (
            "artifact:/tmp/.claude/artifacts/issue-refinement-loop/1413/"
            "snapshots/raw_issue_snapshot_prev.json#repo=squne121/loop-protocol&issue_number=1413"
        ),
        "current": (
            "artifact:/tmp/.claude/artifacts/issue-refinement-loop/1413/"
            "snapshots/raw_issue_snapshot_current.json#repo=squne121/loop-protocol&issue_number=1413"
        ),
        "after": (
            "artifact:/tmp/.claude/artifacts/issue-refinement-loop/1413/"
            "snapshots/raw_issue_snapshot_after.json#repo=squne121/loop-protocol&issue_number=1413"
        ),
    }
    return payload


def test_delta_no_change_two_top_level_dirs_returns_no_scope_signal():
    payload = _load_fixture("issue_1385_replay")
    result = delta.compute_scope_signal_delta(payload)
    assert result["legacy_scope_signal_guard"]["triggered"] is False
    assert result["legacy_scope_signal_guard"]["reason_code"] == "no_scope_signal"
    assert result["sections"]["allowed_paths"]["after_layers"] == ["src", "tests"]
    assert result["sections"]["allowed_paths"]["added_layers"] == []


def test_delta_added_docs_layer_triggers_new_allowed_path_layer():
    payload = _load_fixture("new_allowed_path_layer")
    result = delta.compute_scope_signal_delta(payload)
    signal = next(item for item in result["signals"] if item["reason_code"] == "new_allowed_path_layer")
    assert signal["triggered"] is True
    assert signal["normalized_value"] == ["docs"]
    assert result["legacy_scope_signal_guard"]["reason_code"] == "new_allowed_path_layer"


def test_missing_delta_input_in_hard_stop_phase_is_fail_closed():
    issue_body = _valid_issue_body(
        allowed_paths="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`",
        in_scope="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` の既存修正を維持する",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    output, exit_code = plan.plan_refinement_loop(
        _planner_input(
            issue_body,
            {"current_phase": "preflight", "scope_signal_delta_required": True},
        )
    )
    assert exit_code == 0
    assert output["fail_closed"]["required"] is True
    assert output["fail_closed"]["reason_codes"] == ["missing_scope_signal_delta_input"]


def test_preflight_without_previous_snapshot_blocks_with_missing_scope_signal_delta_input(tmp_path: Path):
    issue_body = _valid_issue_body(
        allowed_paths="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`",
        in_scope="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` の既存修正を維持する",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    result, exit_code, planner_input = _run_preflight_fixture(
        tmp_path,
        current_body=issue_body,
        previous_body=None,
    )
    assert exit_code == preflight.EXIT_BLOCKED
    assert result["status"] == "blocked"
    assert result["planner_fail_closed_reason_codes"] == ["missing_scope_signal_delta_input"]
    assert planner_input["known_context"]["current_phase"] == "preflight"
    assert "scope_signal_delta_input" not in planner_input["known_context"]


def test_preflight_builds_scope_signal_delta_input_for_hard_stop_phase(tmp_path: Path):
    issue_body = _valid_issue_body(
        allowed_paths="- `src/main.ts`\n- `tests/main.test.ts`",
        in_scope="- `src/main.ts` と `tests/main.test.ts` の既存修正を維持する",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    result, exit_code, planner_input = _run_preflight_fixture(
        tmp_path,
        current_body=issue_body,
        previous_body=issue_body,
    )
    assert exit_code in (preflight.EXIT_PASS, preflight.EXIT_WARN)
    assert result["status"] in ("pass", "warn")
    delta_input = planner_input["known_context"]["scope_signal_delta_input"]
    assert planner_input["known_context"]["current_phase"] == "preflight"
    assert delta_input["before_body"] == issue_body
    assert delta_input["current_body"] == issue_body
    assert delta_input["after_body"] == issue_body
    assert delta_input["source_refs"]["before"].startswith("artifact:")
    assert delta_input["source_refs"]["current"].startswith("artifact:")
    assert delta_input["source_refs"]["before"] != delta_input["source_refs"]["current"]

    output, planner_exit_code = plan.plan_refinement_loop(planner_input)
    assert planner_exit_code == 0
    assert output["decisions"]["scope_signal_guard"]["triggered"] is False
    assert output["decisions"]["scope_signal_guard"]["reason_code"] == "no_scope_signal"


def test_evidence_spans_are_body_absolute_and_hash_raw_lines():
    after_body = (
        "## Allowed Paths\n"
        "\n"
        "- `docs/dev/workflow.md`\n"
        "\n"
        "## In Scope\n"
        "- `docs/dev/workflow.md` の契約を改善する\n"
        "\n"
        "## Acceptance Criteria\n"
        "- [ ] AC1: 品質を改善する\n"
    )
    payload = {
        "before_body": "## Allowed Paths\n\n## In Scope\n\n## Acceptance Criteria\n",
        "current_body": after_body,
        "after_body": after_body,
        "source_refs": {"before": "fixture:before", "current": "fixture:current", "after": "fixture:after"},
    }
    result = delta.compute_scope_signal_delta(payload)
    signals = {item["reason_code"]: item for item in result["signals"]}
    for reason_code in ("new_allowed_path_layer", "new_in_scope_area", "new_unverifiable_ac"):
        signal = signals[reason_code]
        assert signal["triggered"] is True
        line = signal["triggering_lines"][0]
        raw_line = _raw_line(after_body, line["start_line"])
        assert line["coordinate_space"] == "body_absolute_1_based"
        assert line["start_line"] == line["end_line"]
        assert line["text_sha256"] == _line_sha(raw_line)


def test_section_blank_lines_and_crlf_keep_absolute_line_numbers():
    payload = {
        "before_body": "## Allowed Paths\r\n\r\n",
        "current_body": "## Allowed Paths\r\n\r\n- `docs/dev/workflow.md`\r\n",
        "after_body": "## Allowed Paths\r\n\r\n- `docs/dev/workflow.md`\r\n",
        "source_refs": {"before": "fixture:before", "current": "fixture:current", "after": "fixture:after"},
    }
    result = delta.compute_scope_signal_delta(payload)
    signal = next(item for item in result["signals"] if item["reason_code"] == "new_allowed_path_layer")
    line = signal["triggering_lines"][0]
    assert line["coordinate_space"] == "body_absolute_1_based"
    assert line["start_line"] == 3
    assert line["end_line"] == 3


def test_delta_positive_planner_output_validates_refinement_loop_plan_schema():
    payload = _valid_delta_input("new_allowed_path_layer")
    issue_body = _valid_issue_body(
        allowed_paths=(
            "- `.claude/skills/issue-refinement-loop/scripts/"
            "plan_refinement_loop.py`\n"
            "- `docs/dev/workflow.md`"
        ),
        in_scope=(
            "- `.claude/skills/issue-refinement-loop/scripts/"
            "plan_refinement_loop.py` の scope 判定更新\n"
            "- `docs/dev/workflow.md` への契約追記"
        ),
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    output, exit_code = plan.plan_refinement_loop(
        _planner_input(issue_body, {"scope_signal_delta_input": payload, "current_phase": "preflight"})
    )
    assert exit_code == 0
    validator = jsonschema.Draft202012Validator(_load_plan_schema())
    assert list(validator.iter_errors(output)) == []


def test_issue_1385_replay_uses_delta_or_fails_closed_without_baseline():
    payload = _valid_delta_input("issue_1385_replay")
    issue_body = _valid_issue_body(
        allowed_paths="- `src/main.ts`\n- `tests/main.test.ts`",
        in_scope="- `src/main.ts` と `tests/main.test.ts` の既存修正を維持する",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    with_delta, exit_code = plan.plan_refinement_loop(
        _planner_input(issue_body, {"scope_signal_delta_input": payload, "current_phase": "preflight"})
    )
    assert exit_code == 0
    assert with_delta["decisions"]["scope_signal_guard"]["triggered"] is False
    assert with_delta["decisions"]["scope_signal_guard"]["reason_code"] == "no_scope_signal"

    without_delta, exit_code = plan.plan_refinement_loop(
        _planner_input(
            issue_body,
            {"current_phase": "preflight", "scope_signal_delta_required": True},
        )
    )
    assert exit_code == 0
    assert without_delta["fail_closed"]["required"] is True
    assert without_delta["fail_closed"]["reason_codes"] == ["missing_scope_signal_delta_input"]


def test_planner_true_positive_new_allowed_path_layer_not_new_in_scope_area():
    payload = _valid_delta_input("new_allowed_path_layer")
    issue_body = _valid_issue_body(
        allowed_paths=(
            "- `.claude/skills/issue-refinement-loop/scripts/"
            "plan_refinement_loop.py`\n"
            "- `docs/dev/workflow.md`"
        ),
        in_scope=(
            "- `.claude/skills/issue-refinement-loop/scripts/"
            "plan_refinement_loop.py` の scope 判定更新\n"
            "- `docs/dev/workflow.md` への契約追記"
        ),
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    output, exit_code = plan.plan_refinement_loop(
        _planner_input(
            issue_body,
            {"scope_signal_delta_input": payload, "current_phase": "preflight"},
        )
    )
    assert exit_code == 0
    decision = output["decisions"]["scope_signal_guard"]
    assert decision["triggered"] is True
    assert decision["reason_code"] == "new_allowed_path_layer"
    assert decision["evidence_spans"][0]["source"] == "known_context"
    assert decision["evidence_spans"][0]["body_version"] == "after"


def test_preflight_e2e_current_docs_layer_triggers_new_allowed_path_layer(tmp_path: Path):
    previous_body = _valid_issue_body(
        allowed_paths="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`",
        in_scope="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` の既存修正を維持する",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    current_body = _valid_issue_body(
        allowed_paths=(
            "- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`\n"
            "- `docs/dev/workflow.md`"
        ),
        in_scope=(
            "- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` の既存修正を維持する\n"
            "- `docs/dev/workflow.md` の契約を改善する"
        ),
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    _, exit_code, planner_input = _run_preflight_fixture(
        tmp_path,
        current_body=current_body,
        previous_body=previous_body,
    )
    assert exit_code in (preflight.EXIT_PASS, preflight.EXIT_WARN)
    output, planner_exit_code = plan.plan_refinement_loop(planner_input)
    assert planner_exit_code == 0
    assert output["decisions"]["scope_signal_guard"]["triggered"] is True
    assert output["decisions"]["scope_signal_guard"]["reason_code"] == "new_allowed_path_layer"


def test_preflight_e2e_sensitive_path_routes_to_security_gate(tmp_path: Path):
    previous_body = _valid_issue_body(
        allowed_paths="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`",
        in_scope="- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` の既存修正を維持する",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    current_body = _valid_issue_body(
        allowed_paths=(
            "- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`\n"
            "- `.github/workflows/python-test.yml`"
        ),
        in_scope=(
            "- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` の既存修正を維持する\n"
            "- `.github/workflows/python-test.yml` を更新する"
        ),
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    _, exit_code, planner_input = _run_preflight_fixture(
        tmp_path,
        current_body=current_body,
        previous_body=previous_body,
    )
    assert exit_code in (preflight.EXIT_PASS, preflight.EXIT_WARN)
    output, planner_exit_code = plan.plan_refinement_loop(planner_input)
    assert planner_exit_code == 0
    assert output["scope_signal_guard_decision_v2"]["route"] == "security_risk_gate_required"


def test_scope_signal_guard_decision_v2_schema_rejects_missing_required_fields():
    payload = _valid_delta_input("new_allowed_path_layer")
    issue_body = _valid_issue_body(
        allowed_paths=(
            "- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`\n"
            "- `docs/dev/workflow.md`"
        ),
        in_scope="- `docs/dev/workflow.md` への契約追記",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    output, exit_code = plan.plan_refinement_loop(
        _planner_input(issue_body, {"scope_signal_delta_input": payload, "current_phase": "preflight"})
    )
    assert exit_code == 0
    invalid = json.loads(json.dumps(output))
    invalid["scope_signal_guard_decision_v2"] = {"schema_version": "SCOPE_SIGNAL_GUARD_DECISION_V2"}
    validator = jsonschema.Draft202012Validator(_load_plan_schema())
    errors = list(validator.iter_errors(invalid))
    assert errors
    assert any("raw_signal" in error.message for error in errors)


def test_scope_signal_guard_decision_v2_schema_rejects_unknown_enum_and_extra_fields():
    payload = _valid_delta_input("new_allowed_path_layer")
    issue_body = _valid_issue_body(
        allowed_paths=(
            "- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`\n"
            "- `docs/dev/workflow.md`"
        ),
        in_scope="- `docs/dev/workflow.md` への契約追記",
        acceptance_criteria="- [ ] AC1: Existing scope remains stable",
    )
    output, exit_code = plan.plan_refinement_loop(
        _planner_input(issue_body, {"scope_signal_delta_input": payload, "current_phase": "preflight"})
    )
    assert exit_code == 0
    invalid = json.loads(json.dumps(output))
    invalid["scope_signal_guard_decision_v2"]["route"] = "bogus"
    invalid["scope_signal_guard_decision_v2"]["unexpected"] = True
    validator = jsonschema.Draft202012Validator(_load_plan_schema())
    errors = list(validator.iter_errors(invalid))
    assert errors
    assert any("bogus" in error.message or "unexpected" in error.message for error in errors)
