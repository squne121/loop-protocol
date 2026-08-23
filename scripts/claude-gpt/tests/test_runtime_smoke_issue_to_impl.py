"""Issue #2278 AC5/AC6/AC11: static/structural regression tests for
`scripts/claude-gpt/runtime_smoke_test.sh --scenario issue_to_impl`.

The full live E2E path (fixture-seeded `gh` + one real `launch.sh`-driven
`claude` invocation) is `# preflight-scope: runtime_only` per Issue #2278's
own Verification Commands section -- it is exercised directly via the shell
VC, not re-run inside this pytest module (a live `claude` subprocess is not
something a CI-gated unit test suite should depend on). This module instead
covers the parts of the `issue_to_impl` scenario that are genuinely
static/deterministic and therefore safe + fast to assert in CI:

  - AC11: an unknown `--scenario` value is rejected with exit code 2 and
    never falls back to the default (flag-less) smoke scenario.
  - the three `issue-2230-equivalent` fixtures exist, are the expected
    shape, and are non-empty (AC3/AC5 fixture SHA-256 recording depends on
    them existing with stable, hashable content).
  - the embedded `CONTRACT_CHECK_ITI_PY_EOF` contract-completeness
    classifier (phase 3, `issue_contract_repair`) is extracted from the
    real `runtime_smoke_test.sh` source (never re-implemented) and driven
    against synthetic fixture-shaped JSON, so a regression in the actual
    production classifier logic is caught here even without a live run.
  - the `ISSUE_TO_IMPL_E2E_RESULT_V1` schema literal and the five canonical
    phase names appear in the shell script (wiring/naming regression
    guard).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SMOKE_SH = Path(__file__).resolve().parents[1] / "runtime_smoke_test.sh"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "issue-2230-equivalent"

_CONTRACT_CHECK_HEREDOC_RE = re.compile(
    r"python3 - \"\$ISSUE_TO_IMPL_FIXTURE_PATH\" <<'CONTRACT_CHECK_ITI_PY_EOF'\n(.*?)\nCONTRACT_CHECK_ITI_PY_EOF\n",
    re.DOTALL,
)

REQUIRED_CONTRACT_HEADERS = (
    "## Outcome",
    "## Acceptance Criteria",
    "## Verification Commands",
    "## Allowed Paths",
    "## Stop Conditions",
)

EXPECTED_PHASES = (
    "workflow_capability_preflight",
    "spark_delegation",
    "issue_contract_repair",
    "fresh_review",
    "impl_review_loop_entry",
)


def _run_contract_check(fixture_json: dict) -> dict:
    """Extract the real embedded contract-completeness classifier (phase 3,
    `issue_contract_repair`) from `runtime_smoke_test.sh` and drive it
    as a real subprocess against a synthetic fixture-shaped payload,
    exactly the same way the live shell scenario invokes it (`python3 -
    <fixture-path> <<heredoc`)."""
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    match = _CONTRACT_CHECK_HEREDOC_RE.search(source)
    assert match is not None, (
        "CONTRACT_CHECK_ITI_PY_EOF heredoc not found in runtime_smoke_test.sh -- "
        "extraction regex is out of sync with the production script"
    )
    script_body = match.group(1)

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(fixture_json, fh)
        fixture_path = fh.name

    proc = subprocess.run(
        [sys.executable, "-c", script_body, fixture_path],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- AC11: unknown --scenario values are rejected, never fall back --------


def test_unknown_scenario_exits_2():
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), "--scenario", "does_not_exist"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "does_not_exist" in proc.stderr


def test_unknown_scenario_does_not_fall_back_to_default_smoke():
    """The default (flag-less) smoke scenario prints `CLAUDE_GPT_SMOKE_RESULT_V1`
    (or SKIPs with exit 77) -- an unknown --scenario value must reach exit 2
    WITHOUT ever reaching that default-scenario code path."""
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), "--scenario", "does_not_exist"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "CLAUDE_GPT_SMOKE_RESULT_V1" not in proc.stdout
    assert "ISSUE_TO_IMPL_E2E_RESULT_V1" not in proc.stdout


def test_known_scenario_values_are_not_rejected_by_the_ac11_gate():
    """Sanity control: the AC11 gate must only reject genuinely unknown
    values, not the two scenarios it is supposed to recognize."""
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert re.search(r"issue_create\)\s*:\s*;;", source)
    assert re.search(r"issue_to_impl\)\s*ISSUE_TO_IMPL_SCENARIO=true\s*;;", source)


# --- Fixtures: existence / shape / SHA-256-ability -------------------------


def test_fixture_files_exist():
    for name in ("prompt.md", "issue.json", "expected-phases.json"):
        path = FIXTURE_DIR / name
        assert path.is_file(), f"missing fixture: {path}"
        assert path.stat().st_size > 0, f"empty fixture: {path}"


def test_issue_json_fixture_shape_and_contract_completeness():
    data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    issues = data.get("issues")
    assert isinstance(issues, dict) and len(issues) == 1
    number, info = next(iter(issues.items()))
    assert number.isdigit()
    body = info.get("body", "")
    for header in REQUIRED_CONTRACT_HEADERS:
        assert header in body, f"fixture Issue body is missing required section {header!r}"


def test_prompt_md_fixture_embeds_the_deterministic_marker_and_target_issue():
    text = (FIXTURE_DIR / "prompt.md").read_text(encoding="utf-8")
    assert "ISSUE_TO_IMPL_FRESH_REVIEW_OK" in text
    issue_data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    (issue_number,) = issue_data["issues"].keys()
    assert issue_number in text


def test_expected_phases_json_matches_canonical_phase_names():
    data = json.loads((FIXTURE_DIR / "expected-phases.json").read_text(encoding="utf-8"))
    names = tuple(entry["phase"] for entry in data["phases"])
    assert names == EXPECTED_PHASES


# --- Embedded contract-completeness classifier (phase 3) -------------------


def test_contract_check_classifies_complete_fixture_as_ok():
    issue_data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    result = _run_contract_check(issue_data)
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["issue_number"] == int(next(iter(issue_data["issues"].keys())))


@pytest.mark.parametrize("missing_header", REQUIRED_CONTRACT_HEADERS)
def test_contract_check_flags_each_missing_required_section(missing_header):
    issue_data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    (number, info) = next(iter(issue_data["issues"].items()))
    mutated_body = info["body"].replace(missing_header, "## Mutated Away")
    assert missing_header not in mutated_body
    mutated = {"issues": {number: {**info, "body": mutated_body}}}
    result = _run_contract_check(mutated)
    assert result["ok"] is False
    assert missing_header in result["missing"]


def test_contract_check_reports_ok_false_for_empty_issues():
    result = _run_contract_check({"issues": {}})
    assert result["ok"] is False
    assert result["issue_number"] is None


# --- Schema / wiring regression guards --------------------------------------


def test_runtime_smoke_sh_defines_the_result_schema_and_all_phase_names():
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert "ISSUE_TO_IMPL_E2E_RESULT_V1" in source
    for phase in EXPECTED_PHASES:
        assert phase in source, f"phase {phase!r} not referenced in runtime_smoke_test.sh"


def test_runtime_smoke_sh_documents_all_four_typed_terminal_result_values():
    """`draft_pr_ready` / `implementation_not_authorized` are documented as
    valid `terminal_result` values in the scenario's schema comment even
    though the current thin implementation only ever emits `blocked` /
    `human_judgment_required` at runtime (AC4's "positive scenario" requires
    reaching >= impl_review_loop_entry, which this bounded smoke harness
    intentionally does not execute a real mutation past -- see the phase 5
    comment block in runtime_smoke_test.sh). This is a documentation-
    completeness guard, not a claim that all four values are reachable
    today."""
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    for value in (
        "draft_pr_ready",
        "blocked",
        "human_judgment_required",
        "implementation_not_authorized",
    ):
        assert value in source, f"terminal_result value {value!r} not documented in runtime_smoke_test.sh"
