"""Issue #2912: pr_review_only VC through the canonical step4-adjudicate -> step5-terminal-gate path.

These tests drive the PRODUCTION entrypoints only: the real
``adjudicate_vc_result.py`` CLI (``step4-adjudicate`` / ``step5-terminal-gate`` /
``extract-vc-metadata``) through subprocesses, the real
``baseline_vc_preflight.py`` producer for the baseline authority, and the module
function ``adjudicate_vc_result()`` in-process for the layer that the CLI cannot
observe. Nothing in the adjudicator / persist / gate chain is mocked, no CI
identifier is filled into a fixture, and no skip / xfail marker is used.

Four concepts stay separate in this file:

1. baseline scope authorization (the producer skip envelope in the contract snapshot),
2. current-head execution facts (the independent test-runner report adapted per command),
3. reviewer semantic judgement (a reviewer verdict file), and
4. terminal approval (only ``step5-terminal-gate`` exit 0).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
SCRIPT_PATH = SKILL_DIR / "scripts" / "adjudicate_vc_result.py"
PRODUCER_PATH = ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
STEP2_DOC = SKILL_DIR / "steps" / "step-2-verification.md"
STEP4_DOC = SKILL_DIR / "steps" / "step-4-pr-review.md"
STEP5_DOC = SKILL_DIR / "steps" / "step-5-feedback-and-termination.md"

_spec = importlib.util.spec_from_file_location("adjudicate_vc_result_step4_pr_review_only_canonical_path", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


ISSUE_NUMBER = 2912
PR_NUMBER = 2930
GENERATED_AT = "2026-10-04T12:00:00Z"
CHANGED_PATHS = ["implemented.txt"]
ALLOWED_PATHS = ["tracked.txt", "implemented.txt", "implemented2.txt"]

# GitHub-specific trust markers. Their mere PRESENCE (even as null) selects the legacy route.
TRUST_MARKER_KEYS = (
    "workflow_run_id",
    "workflow_run_attempt",
    "check_run_id",
    "artifact",
    "artifact_payload",
    "artifact_payload_sha256",
    "producer_receipt",
    "receipt_sha256",
)

BODY_SINGLE = """## Allowed Paths
- tracked.txt
- implemented.txt

## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt
```
"""

BODY_MULTI = """## Allowed Paths
- tracked.txt
- implemented.txt

## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 1

# AC1
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 2

# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 3

# AC2, AC3
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 4
```
"""

BODY_MIXED = """## Allowed Paths
- tracked.txt
- implemented.txt
- implemented2.txt

## Verification Commands

```bash
# AC1
$ test -f implemented.txt

# AC2
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt

# AC3
# preflight-scope: runtime_only
$ rg -q fixture tracked.txt --max-count 1

# AC4
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 2

# AC4
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 3

# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 4

# AC5, AC6
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 5

# AC7
$ test -f implemented2.txt
```
"""

BODY_LEGACY_MIXED = """## Allowed Paths
- tracked.txt
- implemented.txt

## Verification Commands

```bash
# AC1
$ test -f implemented.txt

# AC2
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt
```
"""


# --- real producer / real metadata extractor ---------------------------------


@dataclass
class Scenario:
    """A baseline authority produced by the REAL baseline_vc_preflight.py."""

    name: str
    head: str
    body_sha256: str
    producer: dict[str, Any]
    snapshot: dict[str, Any]
    commands: list[dict[str, Any]]
    hashes: list[str]
    kinds: list[str] = field(default_factory=list)

    def indices(self, kind: str) -> list[int]:
        return [index for index, value in enumerate(self.kinds) if value == kind]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _build_scenario(tmp_path_factory: pytest.TempPathFactory, name: str, body: str) -> Scenario:
    base = tmp_path_factory.mktemp(f"scenario_{name}")
    repo = base / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    head = _git(repo, "rev-parse", "HEAD")
    # The body lives OUTSIDE the repo so it does not dirty the tree under test.
    body_file = base / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(PRODUCER_PATH),
            "--body-file",
            str(body_file),
            "--cwd",
            str(repo),
            "--format",
            "json",
            "--issue",
            str(ISSUE_NUMBER),
            "--repo",
            "squne121/loop-protocol",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout, f"producer emitted no stdout: {completed.stderr}"
    producer = json.loads(completed.stdout)
    assert producer["status"] == "pass", producer.get("errors")

    metadata_run = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "extract-vc-metadata", "--body-file", str(body_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert metadata_run.returncode == 0, metadata_run.stdout + metadata_run.stderr
    metadata = json.loads(metadata_run.stdout)
    commands = metadata["commands"]
    hashes = metadata["command_hashes"]
    # The parse-only metadata and the real producer agree on (ac, hash) and on order.
    assert [(row["ac"], row["command_hash"]) for row in producer["results"]] == [
        (row["ac"], row["command_hash"]) for row in commands
    ]
    assert hashes == [row["command_hash"] for row in commands]

    kinds = []
    for row in producer["results"]:
        if row["scope_class"] == "pr_review_only":
            kinds.append("pr_review_only")
        elif row["scope_class"] == "runtime_only":
            kinds.append("runtime_only")
        else:
            kinds.append("ordinary")
    snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": producer["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": producer["results"]}},
    }
    return Scenario(
        name=name,
        head=head,
        body_sha256=producer["source"]["body_sha256"],
        producer=producer,
        snapshot=snapshot,
        commands=commands,
        hashes=hashes,
        kinds=kinds,
    )


@pytest.fixture(scope="module")
def single(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "single", BODY_SINGLE)
    assert scenario.kinds == ["pr_review_only"]
    return scenario


@pytest.fixture(scope="module")
def multi(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "multi", BODY_MULTI)
    assert scenario.kinds == ["pr_review_only"] * 4
    assert [row["ac"] for row in scenario.commands] == ["AC1", "AC1", "AC_UNKNOWN", "AC2,AC3"]
    return scenario


@pytest.fixture(scope="module")
def mixed(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "mixed", BODY_MIXED)
    assert scenario.kinds == [
        "ordinary",
        "pr_review_only",
        "runtime_only",
        "pr_review_only",
        "pr_review_only",
        "pr_review_only",
        "pr_review_only",
        "ordinary",
    ]
    assert [row["ac"] for row in scenario.commands] == [
        "AC1",
        "AC2",
        "AC3",
        "AC4",
        "AC4",
        "AC_UNKNOWN",
        "AC5,AC6",
        "AC7",
    ]
    return scenario


@pytest.fixture(scope="module")
def legacy_mixed(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "legacy_mixed", BODY_LEGACY_MIXED)
    assert scenario.kinds == ["ordinary", "pr_review_only"]
    return scenario


# --- test-runner report builders (independent report: no trust marker) --------


def _row(command: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ac": command["ac"],
        "command": command["raw_command"],
        "command_hash": command["command_hash"],
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "artifact_present": "not_required",
        "human_review_required": False,
        "stop_condition_triggered": False,
        "notes": "",
    }
    row.update(overrides)
    return row


def _report(
    scenario: Scenario,
    *,
    row_overrides: dict[int, dict[str, Any]] | None = None,
    drop: tuple[int, ...] = (),
    extra_rows: list[dict[str, Any]] | None = None,
    descriptive: bool = True,
    head: str | None = None,
    body_sha256: str | None = None,
    issue_number: Any = ISSUE_NUMBER,
    pr_number: Any = PR_NUMBER,
    result: str = "PASS",
    **top_level: Any,
) -> dict[str, Any]:
    """The independent producer report: execution facts only.

    ``descriptive`` adds the descriptive-only ``producer_kind`` / ``repository`` /
    ``run_id`` / ``run_url`` (and count) fields, which are allowed but never required
    and never a trust marker.
    """
    report_head = head or scenario.head
    rows = [
        _row(command, **((row_overrides or {}).get(index, {})))
        for index, command in enumerate(scenario.commands)
        if index not in drop
    ]
    rows.extend(extra_rows or [])
    report: dict[str, Any] = {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "issue_number": issue_number,
        "pr_number": pr_number,
        "head_sha": report_head,
        "reviewed_head_sha": report_head,
        "diff_head_sha": report_head,
        "contract_body_sha256": body_sha256 if body_sha256 is not None else scenario.body_sha256,
        "generated_at": GENERATED_AT,
        "result": result,
        "runtime_ac_results": rows,
    }
    if descriptive:
        report.update(
            {
                "producer_kind": "test-runner",
                "repository": "squne121/loop-protocol",
                "run_id": "local-independent-run-2912",
                "run_url": "https://example.invalid/local/independent-run-2912",
                "verification_commands_pass": len(rows),
                "verification_commands_fail": 0,
                "verification_skipped_count": 0,
            }
        )
    report.update(top_level)
    return report


def _diff_summary(scenario: Scenario, **overrides: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "head_sha": scenario.head,
        "pr_number": PR_NUMBER,
        "changed_paths": list(CHANGED_PATHS),
    }
    summary.update(overrides)
    return summary


def _reviewer_verdict(head: str, verdict: str = "APPROVE", blockers: list[str] | None = None) -> dict[str, Any]:
    return {"verdict": verdict, "reviewed_head_sha": head, "blockers": blockers or [], "warnings": []}


def _live_mergeability(head: str) -> dict[str, Any]:
    return {"head_sha": head, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"}


class Workspace:
    """Caller-side files a root would pass to the real CLI (no mocks)."""

    def __init__(self, tmp_path: Path, scenario: Scenario) -> None:
        self.dir = tmp_path
        self.scenario = scenario
        self.loop_state = tmp_path / "loop_state.json"

    def write(self, name: str, value: Any) -> str:
        path = self.dir / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return str(path)

    def state(self) -> dict[str, Any]:
        return json.loads(self.loop_state.read_text(encoding="utf-8"))

    def run(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *argv], capture_output=True, text=True, check=False
        )
        assert completed.stdout.strip(), f"no stdout: rc={completed.returncode} stderr={completed.stderr}"
        return completed.returncode, json.loads(completed.stdout.strip().splitlines()[-1])

    def binding_args(
        self, *, head: str | None = None, body: str | None = None, hashes: list[str] | None = None, tag: str = "b"
    ) -> list[str]:
        return [
            "--loop-state-file",
            str(self.loop_state),
            "--expected-head-sha",
            head or self.scenario.head,
            "--expected-contract-body-sha256",
            body if body is not None else self.scenario.body_sha256,
            "--expected-command-hashes-file",
            self.write(f"hashes_{tag}.json", hashes if hashes is not None else self.scenario.hashes),
        ]

    def adjudicate(
        self,
        report: Any = None,
        *,
        report_path: str | None = None,
        diff_summary: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        allowed: list[str] | None = None,
        expected_issue: int | None = ISSUE_NUMBER,
        expected_pr: int | None = PR_NUMBER,
        extra_args: tuple[str, ...] = (),
        hashes: list[str] | None = None,
        tag: str = "adj",
    ) -> tuple[int, dict[str, Any]]:
        """Run `step4-adjudicate` (the only reviewer-dispatch entrance)."""
        scenario = self.scenario
        if report_path is None:
            report_path = self.write(f"report_{tag}.json", report if report is not None else _report(scenario))
        argv = [
            "step4-adjudicate",
            *self.binding_args(hashes=hashes, tag=tag),
            "--test-verdict-file",
            report_path,
            "--contract-snapshot-file",
            self.write(f"snapshot_{tag}.json", snapshot if snapshot is not None else scenario.snapshot),
            "--diff-summary-file",
            self.write(f"diff_{tag}.json", diff_summary if diff_summary is not None else _diff_summary(scenario)),
            "--allowed-paths-file",
            self.write(f"allowed_{tag}.json", allowed if allowed is not None else ALLOWED_PATHS),
            *extra_args,
        ]
        if expected_issue is not None:
            argv += ["--expected-issue-number", str(expected_issue)]
        if expected_pr is not None:
            argv += ["--expected-pr-number", str(expected_pr)]
        return self.run(argv)

    def terminal_gate(
        self,
        *,
        dispatch_seq: int,
        head: str | None = None,
        live_head: str | None = None,
        body: str | None = None,
        hashes: list[str] | None = None,
        verdict: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        head = head or self.scenario.head
        argv = [
            "step5-terminal-gate",
            *self.binding_args(head=head, body=body, hashes=hashes, tag="term"),
            "--reviewer-verdict-file",
            self.write("reviewer_verdict.json", verdict if verdict is not None else _reviewer_verdict(head)),
            "--live-mergeability-file",
            self.write("live_mergeability.json", _live_mergeability(live_head or head)),
            "--dispatch-seq",
            str(dispatch_seq),
        ]
        return self.run(argv)

    def binding_key(self) -> str:
        return mod.step4_binding_key(
            head_sha=self.scenario.head,
            contract_body_sha256=self.scenario.body_sha256,
            command_hashes=self.scenario.hashes,
        )


def _assert_fail_closed_no_dispatch(ws: Workspace, rc: int, payload: dict[str, Any], errors: list[str]) -> None:
    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False, payload
    assert payload["seq"] is None, payload
    assert payload["reason_code"] == "adjudication_missing_or_malformed", payload
    assert payload["adjudication"]["errors"] == errors, payload
    assert payload["adjudication"]["blocking"] is True
    state = ws.state()
    assert "dispatch" not in state
    assert state.get("vc_adjudication", {}) == {}


def _approve_and_assert(ws: Workspace, payload: dict[str, Any]) -> None:
    """Second process: step5-terminal-gate approves from the saved dispatch seq."""
    seq = payload["seq"]
    assert seq == 1
    rc, term = ws.terminal_gate(dispatch_seq=seq)
    assert rc == 0, term
    assert term["route"] == "approved", term


# --- AC1 ---------------------------------------------------------------------


@pytest.mark.parametrize("descriptive", [True, False], ids=["with_descriptive_fields", "minimal_report"])
def test_canonical_independent_path_reaches_terminal_approval(tmp_path, single, descriptive):
    ws = Workspace(tmp_path, single)
    report = _report(single, descriptive=descriptive)
    # The independent report carries no GitHub-specific trust marker at all.
    assert not set(TRUST_MARKER_KEYS) & set(report)
    baseline_item = single.snapshot["checks"]["vc_preflight"]["classifications"][0]
    assert baseline_item["category"] == "preflight_scope_pr_review_only"

    rc, payload = ws.adjudicate(report)

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True
    assert payload["reason_code"] is None
    assert payload["seq"] == 1
    assert payload["binding_key"] == ws.binding_key()
    assert payload["adjudication"]["blocking"] is False
    assert payload["adjudication"]["errors"] == []
    state = ws.state()
    assert state["dispatch"] == {"binding_key": ws.binding_key(), "seq": 1}
    stored = state["vc_adjudication"][ws.binding_key()]
    assert stored["overall_status"] == "pass" and stored["blocking"] is False
    assert [entry["reason_code"] for entry in stored["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]
    assert [entry["command_hash"] for entry in stored["per_ac"]] == single.hashes

    # A reviewer APPROVE plus the live binding is what approves, in another process.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0, term
    assert term["route"] == "approved"

    # step4-adjudicate exit 0 alone is only a dispatch permission: a reviewer
    # REQUEST_CHANGES on the very same dispatch never becomes terminal approval.
    rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict(single.head, "REQUEST_CHANGES", ["fix"]))
    assert rc == 1 and term["route"] != "approved", term


# --- AC2 ---------------------------------------------------------------------


def _per_ac_reason_codes(kinds: list[str], scenario: Scenario) -> list[str]:
    codes = []
    for kind, command in zip(kinds, scenario.commands):
        if kind == "pr_review_only":
            codes.append("pr_review_only_runtime_evidence_pass")
        elif kind == "runtime_only":
            codes.append("runtime_only_current_head_binding_pass")
        else:
            codes.append("expected_fail_resolved_on_current_head")
    return codes


@pytest.mark.parametrize("scenario_name", ["single", "multi", "mixed"])
def test_scope_mixture_and_order_are_preserved(tmp_path, request, scenario_name):
    scenario: Scenario = request.getfixturevalue(scenario_name)
    ws = Workspace(tmp_path, scenario)
    changed = ["implemented.txt", "implemented2.txt"] if scenario_name == "mixed" else list(CHANGED_PATHS)

    rc, payload = ws.adjudicate(_report(scenario), diff_summary=_diff_summary(scenario, changed_paths=changed))

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["reason_code"] is None
    assert payload["adjudication"]["errors"] == []
    stored = ws.state()["vc_adjudication"][ws.binding_key()]
    # per_ac keeps every resolved entry at the Issue declaration position, so its
    # command_hash column equals --expected-command-hashes-file, order included.
    assert [entry["command_hash"] for entry in stored["per_ac"]] == scenario.hashes
    assert [entry["ac"] for entry in stored["per_ac"]] == [row["ac"] for row in scenario.commands]
    assert [entry["reason_code"] for entry in stored["per_ac"]] == _per_ac_reason_codes(scenario.kinds, scenario)
    assert all(entry["status"] == "pass" and entry["blocking"] is False for entry in stored["per_ac"])
    _approve_and_assert(ws, payload)


def test_scope_mixture_and_order_are_preserved_label_echo_and_hash_reorder(tmp_path, multi, mixed):
    # Literal AC_UNKNOWN and comma-joined labels are echoed verbatim; renaming or
    # range-compressing a label breaks the (ac, command_hash) mapping fail-closed.
    ws = Workspace(tmp_path / "label", multi)
    ws.dir.mkdir()
    renamed = _report(multi)
    renamed["runtime_ac_results"][2]["ac"] = "AC3"
    rc, payload = ws.adjudicate(renamed)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == ["pr_review_only_coverage_mismatch"]

    # The persisted per_ac order is the Issue declaration order: the same set of expected
    # hashes in a different order is a different binding, so persist refuses to store the
    # adjudication and no dispatch is recorded.
    ws = Workspace(tmp_path / "order", mixed)
    ws.dir.mkdir()
    changed = _diff_summary(mixed, changed_paths=["implemented.txt", "implemented2.txt"])
    rc, payload = ws.adjudicate(_report(mixed), diff_summary=changed, hashes=list(reversed(mixed.hashes)))
    assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == []
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert "dispatch" not in ws.state() and ws.state().get("vc_adjudication", {}) == {}


# --- AC3 (step4-adjudicate CLI layer) ----------------------------------------


def _drop_pr_review_only_row(scenario: Scenario) -> dict[str, Any]:
    return _report(scenario, drop=(scenario.indices("pr_review_only")[0],))


STEP4_CASES: dict[str, dict[str, Any]] = {
    "report_not_found": {"scenario": "single", "errors": "input_file_not_found"},
    "adapt_failed_schema": {
        "scenario": "single",
        "errors": ["adapt_failed", "unsupported_source_schema:'NOT_A_TEST_VERDICT'"],
    },
    "pr_review_only_report_missing_row": {"scenario": "single", "errors": ["pr_review_only_coverage_mismatch"]},
    "duplicate_ac_command_hash": {"scenario": "single", "errors": ["duplicate_current_ac_command_hash:AC1"]},
    "unknown_extra_ac_row": {"scenario": "single", "errors": ["baseline_current_mapping_mismatch"]},
    "ordinary_row_missing": {"scenario": "mixed", "errors": ["baseline_current_mapping_mismatch"]},
    "unknown_ac_replaces_ordinary_row": {"scenario": "mixed", "errors": ["baseline_current_mapping_mismatch"]},
    "head_drift_diff_head": {"scenario": "single", "errors": ["pr_review_only_head_binding_mismatch"]},
    "head_drift_reviewed_head": {"scenario": "single", "errors": ["pr_review_only_head_binding_mismatch"]},
    "body_digest_drift": {"scenario": "single", "errors": ["pr_review_only_source_body_sha256_mismatch"]},
    "issue_number_drift": {"scenario": "single", "errors": ["pr_review_only_issue_number_mismatch"]},
    "pr_number_drift_diff_summary": {"scenario": "single", "errors": ["pr_review_only_pr_number_mismatch"]},
    "pr_number_drift_report": {"scenario": "single", "errors": ["pr_review_only_pr_number_mismatch"]},
    "expected_issue_number_missing": {"scenario": "single", "errors": ["pr_review_only_expected_issue_number_missing"]},
    "expected_pr_number_missing": {"scenario": "single", "errors": ["pr_review_only_expected_pr_number_missing"]},
    "changed_path_outside_allowed_paths": {
        "scenario": "single",
        "errors": ["pr_review_only_changed_paths_not_certified"],
    },
    "contract_not_go": {"scenario": "single", "errors": ["pr_review_only_contract_not_go"]},
    "report_result_not_pass": {"scenario": "single", "errors": ["pr_review_only_current_vc_result_not_pass"]},
}


@pytest.mark.parametrize("case", sorted(STEP4_CASES), ids=sorted(STEP4_CASES))
def test_step4_must_block_reasons(tmp_path, request, case):
    spec = STEP4_CASES[case]
    scenario: Scenario = request.getfixturevalue(spec["scenario"])
    ws = Workspace(tmp_path, scenario)
    kwargs: dict[str, Any] = {}
    report: Any = _report(scenario)
    expected = spec["errors"]

    if case == "report_not_found":
        missing = str(tmp_path / "no_such_report.json")
        kwargs["report_path"] = missing
        expected = [f"input_file_not_found:{missing}"]
    elif case == "adapt_failed_schema":
        report = {"schema": "NOT_A_TEST_VERDICT", "result": "PASS"}
    elif case == "pr_review_only_report_missing_row":
        report = _drop_pr_review_only_row(scenario)
    elif case == "duplicate_ac_command_hash":
        report = _report(scenario, extra_rows=[_row(scenario.commands[0])])
    elif case == "unknown_extra_ac_row":
        report = _report(
            scenario,
            extra_rows=[_row({"ac": "AC99", "raw_command": "echo unknown", "command_hash": "sha256:" + "9" * 64})],
        )
    elif case == "ordinary_row_missing":
        report = _report(scenario, drop=(scenario.indices("ordinary")[0],))
        kwargs["diff_summary"] = _diff_summary(scenario, changed_paths=["implemented.txt", "implemented2.txt"])
    elif case == "unknown_ac_replaces_ordinary_row":
        report = _report(
            scenario,
            drop=(0,),
            extra_rows=[_row({"ac": "AC99", "raw_command": "echo unknown", "command_hash": "sha256:" + "9" * 64})],
        )
        kwargs["diff_summary"] = _diff_summary(scenario, changed_paths=["implemented.txt", "implemented2.txt"])
    elif case == "head_drift_diff_head":
        kwargs["diff_summary"] = _diff_summary(scenario, head_sha="d" * 40)
    elif case == "head_drift_reviewed_head":
        report["reviewed_head_sha"] = "e" * 40
    elif case == "body_digest_drift":
        report = _report(scenario, body_sha256="sha256:" + "f" * 64)
    elif case == "issue_number_drift":
        report = _report(scenario, issue_number=9999)
    elif case == "pr_number_drift_diff_summary":
        kwargs["diff_summary"] = _diff_summary(scenario, pr_number=9999)
    elif case == "pr_number_drift_report":
        report = _report(scenario, pr_number=9999)
    elif case == "expected_issue_number_missing":
        kwargs["expected_issue"] = None
    elif case == "expected_pr_number_missing":
        kwargs["expected_pr"] = None
    elif case == "changed_path_outside_allowed_paths":
        kwargs["diff_summary"] = _diff_summary(scenario, changed_paths=["src/outside.py"])
    elif case == "contract_not_go":
        kwargs["snapshot"] = {**scenario.snapshot, "status": "no_go"}
    elif case == "report_result_not_pass":
        report = _report(scenario, result="PARTIAL")

    rc, payload = ws.adjudicate(report, **kwargs)

    _assert_fail_closed_no_dispatch(ws, rc, payload, expected)


def test_step4_must_block_reasons_malformed_report_json(tmp_path, single):
    ws = Workspace(tmp_path, single)
    broken = ws.write("broken_report.json", "{this is not json")

    rc, payload = ws.adjudicate(report_path=broken)

    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert len(payload["adjudication"]["errors"]) == 1
    assert payload["adjudication"]["errors"][0].startswith(f"input_json_error:{broken}:")
    assert "dispatch" not in ws.state()


# --- AC3 (adjudicate_vc_result() function layer) -----------------------------


def _envelope_current(scenario: Scenario, *, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A baseline_vc_preflight/v1 current payload carrying the REAL producer envelopes."""
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": ISSUE_NUMBER,
        "generated_at": GENERATED_AT,
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": scenario.body_sha256},
        "head_sha": scenario.head,
        "reviewed_head_sha": scenario.head,
        "results": copy.deepcopy(items if items is not None else scenario.producer["results"]),
    }


def _adjudicate_function(scenario: Scenario, current: Any, test_verdict: Any, **kwargs: Any) -> dict[str, Any]:
    return mod.adjudicate_vc_result(
        contract_snapshot=scenario.snapshot,
        current_vc_result=current,
        diff_summary=_diff_summary(scenario),
        allowed_paths=list(ALLOWED_PATHS),
        test_verdict=test_verdict,
        expected_issue_number=kwargs.pop("expected_issue_number", ISSUE_NUMBER),
        expected_pr_number=kwargs.pop("expected_pr_number", PR_NUMBER),
        **kwargs,
    )


def _adapted_current(scenario: Scenario, report: dict[str, Any]) -> dict[str, Any]:
    converted, errors = mod.adapt_test_verdict_to_current_vc_result(report)
    assert converted is not None and errors == []
    return converted


def test_adjudicator_layer_must_block_reasons_skip_envelope_echo_is_not_execution(single):
    report = _report(single)
    assert not set(TRUST_MARKER_KEYS) & set(report)

    result = _adjudicate_function(single, _envelope_current(single), report)

    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["pr_review_only_independent_requires_executed_item:AC1"]
    assert result["per_ac"] == []


def test_adjudicator_layer_must_block_reasons_missing_test_verdict(single):
    # (h) test_verdict is None: an envelope current is `test_verdict_missing`,
    # an executed current is (as today) the per-item authorization mismatch.
    envelope = _adjudicate_function(single, _envelope_current(single), None)
    assert envelope["overall_status"] != "pass" and envelope["blocking"] is True
    assert envelope["errors"] == ["test_verdict_missing"]

    executed = _adjudicate_function(single, _adapted_current(single, _report(single)), None)
    assert executed["overall_status"] != "pass" and executed["blocking"] is True
    assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"]


def test_adjudicator_layer_must_block_reasons_blanket_pass_without_current_evidence(single):
    report = _report(single)

    # (i) No current evidence at all (empty results), an envelope echo with a PASS
    # claim, and a report claiming PASS with no per-command rows are all refused.
    empty_results = _adjudicate_function(single, _envelope_current(single, items=[]), report)
    assert empty_results["overall_status"] != "pass" and empty_results["blocking"] is True
    assert empty_results["errors"] == ["pr_review_only_coverage_mismatch"]
    assert empty_results["per_ac"] == []

    rowless = _report(single, drop=(0,))
    assert rowless["result"] == "PASS" and rowless["runtime_ac_results"] == []
    converted = _adapted_current(single, rowless)
    assert converted["results"] == []
    blanket = _adjudicate_function(single, converted, rowless)
    assert blanket["overall_status"] != "pass" and blanket["blocking"] is True
    assert blanket["errors"] == ["pr_review_only_coverage_mismatch"]

    echo = _adjudicate_function(single, _envelope_current(single), report)
    assert echo["overall_status"] != "pass" and echo["errors"] == [
        "pr_review_only_independent_requires_executed_item:AC1"
    ]

    # And an executed independent PASS is the only thing that resolves the AC.
    ok = _adjudicate_function(single, _adapted_current(single, report), report)
    assert ok["overall_status"] == "pass" and ok["errors"] == []
    assert [entry["reason_code"] for entry in ok["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]


# --- AC4 ---------------------------------------------------------------------


def _dispatched(tmp_path: Path, scenario: Scenario) -> Workspace:
    """A workspace where the independent PASS was persisted and dispatch seq=1 was recorded."""
    ws = Workspace(tmp_path, scenario)
    rc, payload = ws.adjudicate(_report(scenario))
    assert rc == 0 and payload["seq"] == 1, payload
    return ws


def test_step5_must_block_reasons_reviewer_request_changes(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(
        dispatch_seq=1, verdict=_reviewer_verdict(dispatched.scenario.head, "REQUEST_CHANGES", ["blocker"])
    )

    assert rc == 1
    assert term["route"] != "approved"
    assert term["route"] == "continue_loop"


@pytest.mark.parametrize("stale_seq", [0, 2], ids=["older_seq", "newer_seq"])
def test_step5_must_block_reasons_stale_dispatch_seq(tmp_path, single, stale_seq):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(dispatch_seq=stale_seq)

    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "dispatch_seq_mismatch"
    assert term["rerun_required"]["pr_review"] is True


def test_step5_must_block_reasons_head_drift(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    drifted = "9" * 40

    rc, term = dispatched.terminal_gate(dispatch_seq=1, head=drifted)
    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"
    assert term["rerun_required"] == {"verification": True, "pr_review": True}

    # Split head: reviewer / live mergeability on a new head while the VC binding is the old one.
    rc, term = dispatched.terminal_gate(dispatch_seq=1, live_head=drifted)
    assert rc == 1 and term["route"] != "approved"


def test_step5_must_block_reasons_issue_body_drift(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(dispatch_seq=1, body="sha256:" + "8" * 64)

    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"


def test_step5_must_block_reasons_command_hash_drift(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(dispatch_seq=1, hashes=["sha256:" + "7" * 64])

    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"


def test_step5_must_block_reasons_stale_pass_after_failed_reverification(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    scenario = dispatched.scenario
    failing = _report(scenario, row_overrides={0: {"exit_code": 1, "status": "fail"}}, result="FAIL")

    rc, payload = dispatched.adjudicate(failing, tag="reverify")

    assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == ["pr_review_only_current_execution_not_pass:AC1"]
    assert dispatched.binding_key() not in dispatched.state().get("vc_adjudication", {})
    # The earlier reviewer APPROVE (seq 1) must not reuse the invalidated PASS.
    rc, term = dispatched.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "vc_gate_blocking"
    assert term["rerun_required"]["verification"] is True


# --- AC5 ---------------------------------------------------------------------

NON_PASS_ROWS = {
    "fail_exit_1": {"exit_code": 1, "status": "fail"},
    "skip_exit_77": {"exit_code": 77, "status": "skip"},
    "pass_status_non_zero_exit": {"exit_code": 2, "status": "pass"},
    "zero_exit_fail_status": {"exit_code": 0, "status": "fail"},
    "fallback_detected": {"fallback_detected": True},
    "human_review_required": {"human_review_required": True},
    "stop_condition_triggered": {"stop_condition_triggered": True},
}


@pytest.mark.parametrize("case", sorted(NON_PASS_ROWS), ids=sorted(NON_PASS_ROWS))
def test_execution_failure_is_fail_closed(tmp_path, single, case):
    ws = Workspace(tmp_path, single)
    report = _report(single, row_overrides={0: NON_PASS_ROWS[case]}, result="FAIL")
    report_path = ws.write("report_failure.json", report)
    before = Path(report_path).read_bytes()

    rc, payload = ws.adjudicate(report_path=report_path)

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])
    # The input report is never rewritten and nothing is covered by skip metadata.
    assert Path(report_path).read_bytes() == before
    assert payload["adjudication"]["overall_status"] == "indeterminate"
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(payload)
    # Nothing can be approved from this state either.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] != "approved", term


def test_execution_failure_is_fail_closed_keeps_ac_label_and_never_rewrites_to_pass(tmp_path, mixed):
    ws = Workspace(tmp_path, mixed)
    comma_index = [row["ac"] for row in mixed.commands].index("AC5,AC6")
    unknown_index = [row["ac"] for row in mixed.commands].index("AC_UNKNOWN")
    for tag, (index, label) in {"comma": (comma_index, "AC5,AC6"), "unknown": (unknown_index, "AC_UNKNOWN")}.items():
        report = _report(mixed, row_overrides={index: {"exit_code": 1, "status": "fail"}}, result="FAIL")
        original = copy.deepcopy(report)
        rc, payload = ws.adjudicate(
            report,
            diff_summary=_diff_summary(mixed, changed_paths=["implemented.txt", "implemented2.txt"]),
            tag=tag,
        )
        _assert_fail_closed_no_dispatch(ws, rc, payload, [f"pr_review_only_current_execution_not_pass:{label}"])
        assert json.loads((ws.dir / f"report_{tag}.json").read_text(encoding="utf-8")) == original

    # The adapter transcribes the failure verbatim; the function layer never turns it into PASS.
    report = _report(mixed, row_overrides={comma_index: {"exit_code": 1, "status": "fail"}}, result="FAIL")
    converted = _adapted_current(mixed, report)
    adapted_item = converted["results"][comma_index]
    assert (adapted_item["exit_code"], adapted_item["status"]) == (1, "fail")
    result = _adjudicate_function(mixed, converted, report)
    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["pr_review_only_current_execution_not_pass:AC5,AC6"]
    assert result["per_ac"] == []


# --- AC6 ---------------------------------------------------------------------


def _legacy_complete_test_verdict(scenario: Scenario) -> dict[str, Any]:
    """A GitHub-artifact-shaped verdict whose markers are all present and consistent."""
    command_hashes = sorted(row["command_hash"] for row in scenario.producer["results"])
    artifact_payload = {
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
        "head_sha": scenario.head,
        "reviewed_head_sha": scenario.head,
        "diff_head_sha": scenario.head,
        "contract_body_sha256": scenario.body_sha256,
        "command_hashes": command_hashes,
    }
    report = _report(scenario)
    report.update(
        {
            "workflow_run_id": 2912001,
            "workflow_run_attempt": 1,
            "check_run_id": 29120010,
            "artifact": {
                "name": "test-verdict-machine",
                "artifact_digest": "sha256:" + "a" * 64,
                "url": "https://github.com/squne121/loop-protocol/actions/runs/2912001/artifacts/1",
            },
            "artifact_payload": artifact_payload,
            "artifact_payload_sha256": mod._sha256(mod._canonical_json(artifact_payload)),
        }
    )
    return report


def test_independent_and_legacy_paths_are_separated_descriptive_fields_only_is_independent(single):
    # (a) Only producer_kind / repository / run_id / run_url (no marker key) -> independent route.
    report = _report(single, descriptive=True)
    assert {"producer_kind", "repository", "run_id", "run_url"} <= set(report)
    assert not set(TRUST_MARKER_KEYS) & set(report)

    result = _adjudicate_function(single, _adapted_current(single, report), report)

    assert result["overall_status"] == "pass" and result["errors"] == []
    assert [entry["reason_code"] for entry in result["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]
    # The same report on the independent route is an envelope echo refusal, which proves
    # the route (not the evidence shape) decided: legacy would have asked for test_verdict_*.
    echo = _adjudicate_function(single, _envelope_current(single), report)
    assert echo["errors"] == ["pr_review_only_independent_requires_executed_item:AC1"]


def test_independent_and_legacy_paths_are_separated_partial_markers_stay_legacy(tmp_path, single):
    # (b) A partial marker set is legacy: the existing test_verdict_* reason, not independent evidence.
    partial = _report(single, workflow_run_id=2912001)
    result = _adjudicate_function(single, _envelope_current(single), partial)
    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["test_verdict_workflow_run_attempt_invalid"]

    partial_artifact = _report(
        single, check_run_id=1, workflow_run_id=2, workflow_run_attempt=1, artifact={"name": "x"}
    )
    result = _adjudicate_function(single, _envelope_current(single), partial_artifact)
    assert result["errors"] == ["test_verdict_artifact_digest_invalid"]

    # Through the real CLI (executed current item): never `pr_review_only_independent_*`.
    ws = Workspace(tmp_path, single)
    rc, payload = ws.adjudicate(partial)
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])


@pytest.mark.parametrize("marker", TRUST_MARKER_KEYS)
@pytest.mark.parametrize(
    "placeholder", [None, "", "TODO-placeholder"], ids=["null", "empty_string", "placeholder_text"]
)
def test_independent_and_legacy_paths_are_separated_marker_key_with_empty_value_is_legacy(
    tmp_path, single, marker, placeholder
):
    # (c) The KEY alone selects the legacy route even when the value is null / "" / a placeholder.
    report = _report(single, **{marker: placeholder})
    assert marker in report

    function_result = _adjudicate_function(single, _envelope_current(single), report)
    assert function_result["overall_status"] == "indeterminate" and function_result["blocking"] is True
    assert len(function_result["errors"]) == 1
    assert function_result["errors"][0].startswith("test_verdict_"), function_result["errors"]
    assert "independent" not in function_result["errors"][0]

    ws = Workspace(tmp_path, single)
    rc, payload = ws.adjudicate(report)
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])


def test_independent_and_legacy_paths_are_separated_complete_markers_legacy_accepted(single):
    # (d) A complete, consistent marker set is still accepted by the legacy validation.
    verdict = _legacy_complete_test_verdict(single)
    assert set(TRUST_MARKER_KEYS) - {"producer_receipt", "receipt_sha256"} <= set(verdict)

    result = _adjudicate_function(single, _envelope_current(single), verdict)

    assert result["overall_status"] == "pass" and result["errors"] == [], result
    assert [entry["reason_code"] for entry in result["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]
    # An executed current item is not a legacy envelope even with complete markers.
    executed = _adjudicate_function(single, _adapted_current(single, verdict), verdict)
    assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"]


def test_independent_and_legacy_paths_are_separated_require_producer_receipt_forces_legacy(tmp_path, single):
    # (e) A marker-free independent report cannot use the independent route under
    # --require-producer-receipt; the legacy validation (incl. receipt) runs and fails closed.
    report = _report(single)

    without_flag = _adjudicate_function(single, _envelope_current(single), report)
    assert without_flag["errors"] == ["pr_review_only_independent_requires_executed_item:AC1"]

    with_flag = _adjudicate_function(single, _envelope_current(single), report, require_producer_receipt=True)
    assert with_flag["overall_status"] == "indeterminate" and with_flag["blocking"] is True
    assert with_flag["errors"] == ["test_verdict_workflow_run_id_invalid"]

    executed = _adjudicate_function(
        single, _adapted_current(single, report), report, require_producer_receipt=True
    )
    assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"]

    # Through the CLI the independent report that reaches the reviewer without the flag is refused with it.
    ws = Workspace(tmp_path, single)
    rc, payload = ws.adjudicate(report, extra_args=("--require-producer-receipt",))
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])

    # A complete marker set without a receipt still fails on the receipt under the flag.
    legacy = _adjudicate_function(
        single, _envelope_current(single), _legacy_complete_test_verdict(single), require_producer_receipt=True
    )
    assert legacy["errors"] == ["test_verdict_producer_receipt_missing"]


def test_independent_and_legacy_paths_are_separated_legacy_pins_unchanged(legacy_mixed):
    scenario = legacy_mixed
    ordinary_command, review_command = scenario.commands
    ordinary_current = {
        "ac": ordinary_command["ac"],
        "command_hash": ordinary_command["command_hash"],
        "raw_command": ordinary_command["raw_command"],
        "exit_code": 0,
        "classification": "expected_pass",
        "decision": "go",
        "scope_class": None,
        "runner": "exec",
        "failure_keys": [],
    }
    envelope = copy.deepcopy(scenario.producer["results"][1])
    assert envelope["scope_class"] == "pr_review_only" and envelope["runner"] == "skipped"
    current = _envelope_current(scenario, items=[ordinary_current, envelope])
    verdict = _legacy_complete_test_verdict(scenario)

    mixed_result = _adjudicate_function(scenario, current, verdict)

    # Legacy: in a mixed scope the pr_review_only resolved entry is EXCLUDED from per_ac.
    assert mixed_result["overall_status"] == "pass", mixed_result
    assert len(mixed_result["per_ac"]) == 1
    assert mixed_result["per_ac"][0]["ac"] == "AC1"
    assert mixed_result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"

    # Legacy: no test_verdict and an executed pr_review_only current is the authorization mismatch.
    executed_review = {
        "ac": review_command["ac"],
        "command_hash": review_command["command_hash"],
        "raw_command": review_command["raw_command"],
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "failure_keys": [],
    }
    executed_current = _envelope_current(scenario, items=[ordinary_current, executed_review])
    rejected = _adjudicate_function(scenario, executed_current, None)
    assert rejected["overall_status"] != "pass" and rejected["blocking"] is True
    assert rejected["errors"] == ["pr_review_only_current_authorization_mismatch:AC2"]


def test_independent_and_legacy_paths_are_separated_route_detection_fails_closed(tmp_path, single):
    # Refinement warning A: route detection must not treat these as independent.
    marker_free = _report(single)
    cases = {
        "wrapper_marker_free": {"TEST_VERDICT": copy.deepcopy(marker_free)},
        "wrapper_with_markers": {"TEST_VERDICT": {**copy.deepcopy(marker_free), "workflow_run_id": None}},
        "schema_mismatch_dict": {**copy.deepcopy(marker_free), "schema": "TEST_VERDICT_MACHINE/v1"},
        "empty_dict": {},
    }
    for name, verdict in cases.items():
        # Envelope current: legacy validation answers with the existing schema reason.
        result = _adjudicate_function(single, _envelope_current(single), verdict)
        assert result["overall_status"] == "indeterminate" and result["blocking"] is True, name
        assert result["errors"] == ["test_verdict_schema_mismatch"], (name, result["errors"])
        # Executed-looking current: legacy per-item authorization mismatch, never independent PASS.
        executed = _adjudicate_function(single, _adapted_current(single, marker_free), verdict)
        assert executed["overall_status"] != "pass" and executed["blocking"] is True, name
        assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"], (name, executed["errors"])

    # CLI layer: a wrapper that carries markers inside is unwrapped by the CLI and is legacy.
    ws = Workspace(tmp_path / "wrapper", single)
    ws.dir.mkdir()
    rc, payload = ws.adjudicate(cases["wrapper_with_markers"])
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])

    # CLI layer: a schema-mismatched dict and `{}` never reach the adjudicator (adapt fails).
    for name in ("schema_mismatch_dict", "empty_dict"):
        ws = Workspace(tmp_path / name, single)
        ws.dir.mkdir()
        rc, payload = ws.adjudicate(cases[name])
        assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
        assert payload["adjudication"]["errors"][0] == "adapt_failed", name
        assert payload["adjudication"]["errors"][1].startswith("unsupported_source_schema:"), name
        assert "dispatch" not in ws.state()


# --- AC7 ---------------------------------------------------------------------


def _squash(text: str) -> str:
    """Remove every whitespace character (incl. newlines and U+3000) before comparing."""
    return re.sub(r"\s+", "", text)


REQUIRED_PHRASES: dict[str, tuple[str, ...]] = {
    "step-2": (
        "独立経路では GitHub 固有 trust marker のキーを省略させる",
        "legacy publish 経路と `--require-producer-receipt` 指定時のみ GitHub 固有 trust marker は必須である",
    ),
    "step-4": (
        "`step4-adjudicate` の exit 0 は reviewer dispatch の許可であり、terminal approval ではない",
        "terminal approval（`termination_reason: approved` / `merge_ready: true`）の確定は "
        "`step5-terminal-gate` の exit 0 のみを根拠とする",
        "`--expected-issue-number` / `--expected-pr-number` は runtime_only または pr_review_only VC を含む場合は必須",
    ),
    "step-5": (
        "`step5-terminal-gate` に渡し、出力の `route` で分岐する",
        "terminal approval は `step5-terminal-gate` 経由に固定する",
    ),
}

FORBIDDEN_PHRASES: dict[str, tuple[str, ...]] = {
    "step-2": (),
    "step-4": ("`route_loop_verdict_v2()` の `live_mergeability` 引数として渡す",),
    "step-5": ("`route_loop_verdict_v2()` に渡し、返る `route` で分岐する",),
}

# Verbatim baseline sentences (main fc8bfe54) used to self-verify the checker.
BASELINE_STEP4_SENTENCE = (
    "mergeability（`mergeable` / `merge_state_status`）は control-plane が `gh pr view --json "
    "headRefOid,mergeable,mergeStateStatus` で都度直接取得し、`route_loop_verdict_v2()` の "
    "`live_mergeability` 引数として渡す（`step-5-mergeability-handling.md` 参照）。"
)
BASELINE_STEP5_SENTENCE = (
    "reviewer_verdict（`verdict`/`reviewed_head_sha`/`blockers`/`warnings`）と live_mergeability\n"
    "（`gh pr view` で取得した `mergeable`/`merge_state_status`）を `route_loop_verdict_v2()` に渡し、\n"
    "返る `route` で分岐する。詳細な `route` 一覧と判定条件は `step-5-mergeability-handling.md` を参照。"
)
ALLOWED_MENTIONS = {
    "step-2": "この節は `route_loop_verdict_v2_resolve_semantic_ambiguity()` を直接は呼ばない。",
    "step-4": (
        "`route_loop_verdict_v2()` によるルーティングの詳細は `step-5-mergeability-handling.md` を canonical とする。"
    ),
    "step-5": (
        "`step5-terminal-gate` は公開 wrapper `route_loop_verdict_v2_resolve_semantic_ambiguity()` を呼ぶ。"
        "approved 以外の route は `route_loop_verdict_v2()` の評価結果として再評価する。"
        "terminal approval を `route_loop_verdict_v2()` の直接呼び出し結果だけで確定してはならない。"
    ),
}


def _doc_problems(docs: dict[str, str]) -> list[str]:
    """Exact (whitespace-stripped) substring presence / absence. No classifier, no rg."""
    problems: list[str] = []
    for name, text in docs.items():
        squashed = _squash(text)
        for phrase in REQUIRED_PHRASES[name]:
            if _squash(phrase) not in squashed:
                problems.append(f"{name}: missing required: {phrase}")
        for phrase in FORBIDDEN_PHRASES[name]:
            if _squash(phrase) in squashed:
                problems.append(f"{name}: forbidden present: {phrase}")
    return problems


def _fixture_docs(*, with_required: bool, with_forbidden: bool) -> dict[str, str]:
    docs: dict[str, str] = {}
    for name in ("step-2", "step-4", "step-5"):
        parts = [ALLOWED_MENTIONS[name]]
        if with_required:
            # Required sentences laid out with arbitrary line breaks / spaces: only content counts.
            parts.extend(phrase.replace(" ", "\n  ") for phrase in REQUIRED_PHRASES[name])
        if with_forbidden:
            parts.extend(FORBIDDEN_PHRASES[name])
        docs[name] = "\n\n".join(parts)
    if with_forbidden:
        docs["step-4"] += "\n\n" + BASELINE_STEP4_SENTENCE
        docs["step-5"] += "\n\n" + BASELINE_STEP5_SENTENCE
    return docs


def test_step_docs_state_canonical_entrance_checker_self_verifies():
    # Post-implementation shape: required present, forbidden absent, allowed mentions tolerated.
    assert _doc_problems(_fixture_docs(with_required=True, with_forbidden=False)) == []

    # Baseline shape (verbatim old sentences, no required sentence): both kinds of problem appear.
    baseline_like = {
        "step-2": "",
        "step-4": BASELINE_STEP4_SENTENCE,
        "step-5": BASELINE_STEP5_SENTENCE
        + "\n\n### terminal approval は `step5-terminal-gate` 経由に固定する（Issue #2837）",
    }
    problems = _doc_problems(baseline_like)
    assert any(problem.startswith("step-2: missing required") for problem in problems)
    assert any(problem.startswith("step-4: missing required") for problem in problems)
    assert any(problem.startswith("step-4: forbidden present") for problem in problems)
    assert any(problem.startswith("step-5: missing required") for problem in problems)
    assert any(problem.startswith("step-5: forbidden present") for problem in problems)

    # Each required / forbidden phrase is individually load-bearing.
    for name, phrases in REQUIRED_PHRASES.items():
        for phrase in phrases:
            docs = _fixture_docs(with_required=True, with_forbidden=False)
            docs[name] = docs[name].replace(phrase.replace(" ", "\n  "), "")
            assert any("missing required" in problem for problem in _doc_problems(docs)), phrase
    for name, phrases in FORBIDDEN_PHRASES.items():
        for phrase in phrases:
            docs = _fixture_docs(with_required=True, with_forbidden=False)
            docs[name] += "\n" + phrase.replace(" ", "\n")
            assert any("forbidden present" in problem for problem in _doc_problems(docs)), phrase

    # A table split by `|` would not satisfy the single-sentence requirement (warning C).
    split = (
        "| `--expected-issue-number` / `--expected-pr-number` | "
        "runtime_only または pr_review_only VC を含む場合は必須 |"
    )
    docs = _fixture_docs(with_required=True, with_forbidden=False)
    docs["step-4"] = docs["step-4"].replace(REQUIRED_PHRASES["step-4"][2].replace(" ", "\n  "), "") + "\n" + split
    assert any("missing required" in problem for problem in _doc_problems(docs))


def test_step_docs_state_canonical_entrance(tmp_path):
    docs = {
        "step-2": STEP2_DOC.read_text(encoding="utf-8"),
        "step-4": STEP4_DOC.read_text(encoding="utf-8"),
        "step-5": STEP5_DOC.read_text(encoding="utf-8"),
    }

    assert _doc_problems(docs) == []
    # The retained, allowed texts are still present (they are outside the checked phrases).
    assert "route_loop_verdict_v2_resolve_semantic_ambiguity()" in docs["step-5"]
    assert "step-5-mergeability-handling.md" in docs["step-4"]


# --- #2924 REQUEST_CHANGES fix_delta (A: ordinary status / B: raw report / C: Step 2 triplet) ---


@pytest.mark.parametrize("status", ["skip", "fail"])
def test_mixed_ordinary_non_pass_status_with_zero_exit_is_not_promoted_to_pass(tmp_path, mixed, status):
    # Finding A (P1): an ordinary row whose own status is not "pass" must not be promoted
    # to PASS merely because exit_code == 0 while the report-level result is PASS.
    changed = ["implemented.txt", "implemented2.txt"]
    for tag, index in zip(("first", "last"), (mixed.indices("ordinary")[0], mixed.indices("ordinary")[-1])):
        ws = Workspace(tmp_path / tag, mixed)
        ws.dir.mkdir()
        report = _report(mixed, row_overrides={index: {"status": status, "exit_code": 0}})
        assert report["result"] == "PASS"

        rc, payload = ws.adjudicate(report, diff_summary=_diff_summary(mixed, changed_paths=changed))

        assert rc == 1, (tag, payload)
        assert payload["invoke_pr_reviewer"] is False, (tag, payload)
        assert payload["seq"] is None
        assert payload["adjudication"]["blocking"] is True
        assert "dispatch" not in ws.state()
        assert ws.state().get("vc_adjudication", {}) == {}


@pytest.mark.parametrize("flag", ["fallback_detected", "human_review_required", "stop_condition_triggered"])
@pytest.mark.parametrize("bad", ["missing", None, 0, "", "false"], ids=["missing", "null", "zero", "empty", "str"])
def test_raw_report_execution_flags_must_be_explicit_booleans(tmp_path, single, flag, bad):
    # Finding B-1 (P2): a missing / null / non-bool flag in the RAW report is not an explicit false.
    ws = Workspace(tmp_path, single)
    report = _report(single)
    if bad == "missing":
        del report["runtime_ac_results"][0][flag]
    else:
        report["runtime_ac_results"][0][flag] = bad

    rc, payload = ws.adjudicate(report)

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    # A truthy non-bool ("false") is coerced to True by the adapter and already blocked as a
    # non-pass execution; every other non-bool is rejected on the raw report. Both fail closed.
    expected = (
        ["pr_review_only_current_execution_not_pass:AC1"]
        if bad == "false"
        else [f"pr_review_only_report_flag_not_boolean:AC1:{flag}"]
    )
    assert payload["adjudication"]["errors"] == expected
    assert "dispatch" not in ws.state()


def test_raw_report_flags_are_checked_for_ordinary_rows_and_top_level(tmp_path, mixed):
    changed = _diff_summary(mixed, changed_paths=["implemented.txt", "implemented2.txt"])
    ordinary = mixed.indices("ordinary")[0]
    ws = Workspace(tmp_path / "row", mixed)
    ws.dir.mkdir()
    report = _report(mixed)
    del report["runtime_ac_results"][ordinary]["stop_condition_triggered"]
    rc, payload = ws.adjudicate(report, diff_summary=changed)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    assert payload["adjudication"]["errors"] == ["pr_review_only_report_flag_not_boolean:AC1:stop_condition_triggered"]

    ws = Workspace(tmp_path / "top", mixed)
    ws.dir.mkdir()
    rc, payload = ws.adjudicate(_report(mixed, human_review_required=0), diff_summary=changed)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    assert payload["adjudication"]["errors"] == ["pr_review_only_report_flag_not_boolean::human_review_required"]


@pytest.mark.parametrize("field", ["reviewed_head_sha", "diff_head_sha"])
@pytest.mark.parametrize("bad", ["missing", None, "", "d" * 40], ids=["missing", "null", "empty", "drift"])
def test_raw_report_head_fields_are_required_and_must_agree(tmp_path, single, field, bad):
    # Finding B-2 (P2): step-2 requires head_sha / reviewed_head_sha / diff_head_sha bound to one head.
    # reviewed_head_sha must not be back-filled from head_sha, and a drifting diff_head_sha is not ignored.
    ws = Workspace(tmp_path, single)
    report = _report(single)
    if bad == "missing":
        del report[field]
    else:
        report[field] = bad

    rc, payload = ws.adjudicate(report)

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == ["pr_review_only_head_binding_mismatch"]
    assert "dispatch" not in ws.state()


def _step2_message_template() -> str:
    text = STEP2_DOC.read_text(encoding="utf-8")
    block = re.search(r"```yaml\n(spawn_agent:.*?)```", text, flags=re.DOTALL)
    assert block is not None, "step-2 spawn_agent template not found"
    return block.group(1)


def test_step2_delegation_template_passes_ac_command_command_hash_triplets_verbatim():
    # Finding C (P1): test-runner must echo command_hash, so root passes (ac, raw_command, command_hash)
    # triplets from extract-vc-metadata verbatim; test-runner never guesses / recomputes / inherits it.
    template = _step2_message_template()
    squashed_template = _squash(template)
    assert _squash("Per-command inputs") in squashed_template
    assert _squash("echo these values exactly") in squashed_template
    for key in ("- ac:", "command:", "command_hash:"):
        assert _squash(key) in squashed_template, key
    # The old pair-only (label => command) shape must be gone from the template.
    assert "=>" not in template

    doc = _squash(STEP2_DOC.read_text(encoding="utf-8"))
    for phrase in (
        "`(ac, raw_command, command_hash)` の triplet",
        "`extract-vc-metadata` の `commands[]` から",
        "test-runner は `command_hash` を推測・再計算してはならず、親 context の暗黙継承に依存してはならない",
    ):
        assert _squash(phrase) in doc, phrase
    assert _squash("`(ac label, literal command)` の組") not in doc
