"""Issue #2897 AC1/AC2 (review merge side): `check_issue_contract.py
--mode merge_readiness` projects a bounded `timeout_diagnostics`
(`TIMEOUT_DIAGNOSTICS_V1`) from a readiness `human_judgment` produced by a
command-level VC timeout, without fabricating a deterministic blocker and
without changing `failure_class` / the operator-only route.

Every test goes through the production conversion chain; no completed
`timeout_provenance` / readiness / review dict is hand-built:

    fake `baseline_vc_preflight.run_command()` return value (permitted seam 1,
    timeout sentinel `exit_code == -1` and `stderr == "timeout"`)
      -> REAL `baseline_vc_preflight.main()` result builder
      -> REAL `contract_readiness_check.main()` conversion
      -> REAL `check_issue_contract.main()` review check (`--file --json`)
      -> REAL `check_issue_contract.main()` `--mode merge_readiness`

Permitted seam 2 (process-launch mechanics only): the cooperative supervisor
that would spawn `baseline_vc_preflight.py` as a child process is replaced by
an adapter calling that SAME script's `main()` in-process.

The "unknown attribution" cases degrade an otherwise real artifact by
REMOVING or TAMPERING a binding field (a legacy / partial producer, or a
corrupted file between readiness and merge). They never inject a completed
`timeout_provenance`.
"""

from __future__ import annotations

import contextlib
import io
import json
import signal
import sys
from pathlib import Path
from unittest import mock

import pytest

_REVIEW_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_CONTRACT_REVIEW_SCRIPTS = Path(__file__).resolve().parents[2] / "issue-contract-review" / "scripts"
for _path in (_REVIEW_SCRIPTS, _CONTRACT_REVIEW_SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import baseline_vc_preflight as bvp  # noqa: E402
import check_issue_contract as cic  # noqa: E402
import contract_readiness_check as crc  # noqa: E402

TIMEOUT_OUTCOME = (-1, "", "timeout", 1234, {})
NOT_FOUND_OUTCOME = (4, "", "ERROR: file or directory not found: x", 5, {})

_NEW_TEST_PATH = ".claude/skills/review-issue/tests/test_fixture_target_not_yet_created.py"
_PYTEST_VC = f"uv run --locked pytest {_NEW_TEST_PATH}::test_target"
_PURE_VC = "test -f README.md"
_OTHER_DIGEST = "sha256:" + "ab" * 32

_BODY_HEADER = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "timeout diagnostic projection fixture"
change_kind: workflow
```

## Outcome

Fixture for the timeout diagnostic projection.

## Acceptance Criteria

- [ ] AC1: the projection keeps the timed-out occurrence identity.
- [ ] AC2: the projection never attributes a timeout to another occurrence.

## Verification Commands

"""

_ALLOWED = f"""
## Allowed Paths

- {_NEW_TEST_PATH}
"""

# Two fenced blocks holding the SAME AC and the SAME command at the SAME
# block-relative line (`line + command_hash` would collide), preceded by one
# unrelated pure command so the canonical indexes are not trivially 0/1.
_TWO_BLOCK_BODY = (
    _BODY_HEADER
    + f"""```bash
# AC1
$ {_PURE_VC}
```

```bash
# AC1
# baseline-expect: fail
$ {_PYTEST_VC}
```

```bash
# AC1
# baseline-expect: fail
$ {_PYTEST_VC}
```
"""
    + _ALLOWED
)

# Two pure identical commands: occurrence 0 is the real execution and
# occurrence 1 is its dedup replay.
_DEDUP_BODY = (
    _BODY_HEADER
    + f"""```bash
# AC2
$ {_PURE_VC}
```

```bash
# AC2
$ {_PURE_VC}
```
"""
    + _ALLOWED
)


class _RunCommandSeam:
    """Seam 1: replaces `baseline_vc_preflight.run_command()` only."""

    def __init__(self, outcomes_by_call: dict[int, tuple]):
        self._outcomes_by_call = outcomes_by_call
        self.calls: list[tuple[str, int]] = []

    def __call__(self, command: str, timeout_seconds: int, cwd: str):
        call_index = len(self.calls)
        self.calls.append((command, timeout_seconds))
        return self._outcomes_by_call.get(call_index, NOT_FOUND_OUTCOME)


class _InProcessBaselineLauncher:
    """Seam 2: launch mechanics only. `degrade` (optional) removes / alters a
    binding field of the REAL preflight payload to model a legacy or partial
    producer; it is never used to supply a completed provenance."""

    def __init__(self, degrade=None):
        self._degrade = degrade
        self.raw_payloads: list[dict] = []

    def __call__(self, argv, *, timeout_seconds, cwd=None, env=None, **_ignored):
        assert Path(argv[1]).name == "baseline_vc_preflight.py", argv
        out, err = io.StringIO(), io.StringIO()
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.object(sys, "argv", [argv[1], *argv[2:]]):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    returncode = bvp.main()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        payload = json.loads(out.getvalue())
        self.raw_payloads.append(json.loads(out.getvalue()))
        stdout_text = out.getvalue()
        if self._degrade is not None:
            self._degrade(payload)
            stdout_text = json.dumps(payload)
        return bvp.SupervisedSubprocessResult(returncode, stdout_text, err.getvalue(), False, 0.0)


def _run_main(module_main, argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", argv):
        with contextlib.redirect_stdout(out):
            try:
                code = module_main() or 0
            except SystemExit as exc:  # check_issue_contract.main() exits
                code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue()


def _readiness_result(
    monkeypatch, tmp_path: Path, body: str, outcomes_by_call: dict[int, tuple], degrade=None
) -> tuple[dict, _InProcessBaselineLauncher]:
    launcher = _InProcessBaselineLauncher(degrade)
    monkeypatch.setattr(bvp, "run_command", _RunCommandSeam(outcomes_by_call))
    monkeypatch.setattr(crc, "_run_subprocess_with_cooperative_supervisor", launcher)
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    _code, stdout = _run_main(
        crc.main, ["contract_readiness_check.py", "--body-file", str(body_file), "--mode", "execute"]
    )
    return json.loads(stdout), launcher


def _review_result(tmp_path: Path, body: str) -> dict:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    _code, stdout = _run_main(
        cic.main, ["check_issue_contract.py", "--file", str(body_file), "--json"]
    )
    return json.loads(stdout)


def _merge(tmp_path: Path, review: dict, readiness: dict) -> tuple[int, dict | None]:
    review_file = tmp_path / "review_result.json"
    readiness_file = tmp_path / "readiness_result.json"
    output_file = tmp_path / "merged_review_result.json"
    review_file.write_text(json.dumps(review), encoding="utf-8")
    readiness_file.write_text(json.dumps(readiness), encoding="utf-8")
    if output_file.exists():
        output_file.unlink()
    code, _stdout = _run_main(
        cic.main,
        [
            "check_issue_contract.py",
            "--mode", "merge_readiness",
            "--review-result-file", str(review_file),
            "--readiness-result-file", str(readiness_file),
            "--readiness-artifact-path", str(readiness_file),
            "--iteration-id", "timeout_diagnostic_projection_test",
            "--output-file", str(output_file),
        ],
    )
    merged = json.loads(output_file.read_text(encoding="utf-8")) if output_file.exists() else None
    return code, merged


def _full_chain(monkeypatch, tmp_path, body, outcomes_by_call, degrade=None, tamper_readiness=None):
    readiness, launcher = _readiness_result(monkeypatch, tmp_path, body, outcomes_by_call, degrade)
    if tamper_readiness is not None:
        tamper_readiness(readiness)
    review = _review_result(tmp_path, body)
    code, merged = _merge(tmp_path, review, readiness)
    return review, readiness, merged, code, launcher


def test_inner_timeout_preserves_bounded_identity_without_blocker(monkeypatch, tmp_path):
    # call 0 = pure command, call 1 = first pytest block, call 2 = second
    # pytest block (times out).
    review, readiness, merged, code, launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: TIMEOUT_OUTCOME}
    )
    raw = launcher.raw_payloads[0]

    # Existing semantics are unchanged: human_judgment failure_class, a
    # needs-fix verdict and the timeout string in blocking_issues; no
    # deterministic blocker is fabricated for the timeout.
    assert readiness["status"] == "human_judgment"
    assert merged is not None and code == 1
    assert merged["failure_class"] == "contract_readiness_human_judgment"
    assert merged["verdict"] == "needs-fix"
    assert "Command exceeded timeout" in merged["blocking_issues"]
    assert merged["structured_blockers"] == review["structured_blockers"]
    assert not any(
        "timeout" in json.dumps(blocker).lower() for blocker in merged["structured_blockers"]
    )

    diagnostics = merged["timeout_diagnostics"]
    assert diagnostics["schema_version"] == "TIMEOUT_DIAGNOSTICS_V1"
    assert diagnostics["body_sha256"] == review["body_sha256"] == readiness["body_sha256"]
    assert diagnostics["canonical_plan_digest"] == raw["diagnostic_report"]["canonical_plan_digest"]
    assert diagnostics["results_count"] == len(raw["results"]) == 3
    assert diagnostics["total_timeout_occurrences"] == 1
    assert diagnostics["truncated_count"] == 0

    (occurrence,) = diagnostics["occurrences"]
    raw_item = raw["results"][2]
    assert occurrence["attribution"] == "attributed"
    assert occurrence["reason_code"] == "binding_verified"
    assert occurrence["occurrence_index"] == 2
    assert occurrence["line"] == raw_item["line"]
    assert occurrence["line_coordinate"] == "block_relative"
    assert occurrence["command_hash"] == raw_item["command_hash"]
    assert occurrence["execution_key_hash"] == raw_item["execution_key_hash"]
    assert occurrence["execution_source"] == "executed"
    assert occurrence["dedup_source_result_index"] is None
    assert occurrence["timeout_provenance"] == raw_item["timeout_provenance"]

    # Bounded: no raw command / output / environment in the diagnostic.
    serialized = json.dumps(diagnostics)
    assert _PYTEST_VC not in serialized and _PURE_VC not in serialized
    assert "runner_env_delta" not in serialized and "minimal_context" not in serialized
    assert len(serialized.encode("utf-8")) <= 16 * 1024


@pytest.mark.parametrize("timed_out_call, expected_index", [(2, 2), (1, 1)])
def test_same_hash_same_line_other_block_only_timed_out_occurrence_attributed(
    monkeypatch, tmp_path, timed_out_call, expected_index
):
    _review, _readiness, merged, _code, launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {timed_out_call: TIMEOUT_OUTCOME}
    )
    raw_results = launcher.raw_payloads[0]["results"]
    # Both pytest blocks collide on (ac, line, command_hash)...
    assert raw_results[1]["command_hash"] == raw_results[2]["command_hash"]
    assert raw_results[1]["line"] == raw_results[2]["line"]
    assert raw_results[1]["ac"] == raw_results[2]["ac"]

    # ...but only the block that really timed out is attributed, by index.
    diagnostics = merged["timeout_diagnostics"]
    assert [o["occurrence_index"] for o in diagnostics["occurrences"]] == [expected_index]
    assert diagnostics["total_timeout_occurrences"] == 1
    (occurrence,) = diagnostics["occurrences"]
    assert occurrence["attribution"] == "attributed"
    assert occurrence["execution_key_hash"] == raw_results[expected_index]["execution_key_hash"]
    other = 1 if expected_index == 2 else 2
    assert occurrence["execution_key_hash"] != raw_results[other]["execution_key_hash"]


def test_dedup_replay_is_distinguished_from_real_execution(monkeypatch, tmp_path):
    _review, _readiness, merged, _code, launcher = _full_chain(
        monkeypatch, tmp_path, _DEDUP_BODY, {0: TIMEOUT_OUTCOME}
    )
    raw_results = launcher.raw_payloads[0]["results"]
    assert raw_results[1]["runner"] == "dedup_replay"

    occurrences = merged["timeout_diagnostics"]["occurrences"]
    assert [o["occurrence_index"] for o in occurrences] == [0, 1]
    assert [o["execution_source"] for o in occurrences] == ["executed", "dedup_replay"]
    assert [o["dedup_source_result_index"] for o in occurrences] == [None, 0]
    assert all(o["attribution"] == "attributed" for o in occurrences)
    assert occurrences[0]["execution_key_hash"] == occurrences[1]["execution_key_hash"]


def _drop_provenance(payload):
    for item in payload["results"]:
        item.pop("timeout_provenance", None)


def _drop_execution_key(payload):
    for item in payload["results"]:
        item["execution_key_hash"] = None


def _not_computed_digest(payload):
    payload["diagnostic_report"] = bvp.not_computed_diagnostic_report()


def _bad_dedup_source_out_of_range(payload):
    for item in payload["results"]:
        if item.get("dedup"):
            item["dedup"]["source_result_index"] = 99


def _bad_dedup_source_wrong_target(payload):
    # A replay can only point at an earlier real execution; point it at itself.
    for index, item in enumerate(payload["results"]):
        if item.get("dedup"):
            item["dedup"]["source_result_index"] = index


def _tamper_digest(readiness):
    readiness["canonical_plan_digest"] = _OTHER_DIGEST


def _tamper_results_count(readiness):
    readiness["results_count"] = 1


_ONE_TIMEOUT = {2: TIMEOUT_OUTCOME}
_REPLAY_TIMEOUT = {0: TIMEOUT_OUTCOME}

# (id, body, outcomes, degrade-preflight, tamper-readiness, expected reason)
_UNKNOWN_CASES = [
    ("plan_digest_missing", _TWO_BLOCK_BODY, _ONE_TIMEOUT, _not_computed_digest, None,
     "plan_digest_missing"),
    ("plan_digest_mismatch", _TWO_BLOCK_BODY, _ONE_TIMEOUT, None, _tamper_digest,
     "plan_digest_mismatch"),
    ("provenance_missing", _TWO_BLOCK_BODY, _ONE_TIMEOUT, _drop_provenance, None,
     "provenance_missing"),
    ("execution_key_missing", _TWO_BLOCK_BODY, _ONE_TIMEOUT, _drop_execution_key, None,
     "execution_key_missing"),
    ("dedup_source_out_of_range", _DEDUP_BODY, _REPLAY_TIMEOUT, _bad_dedup_source_out_of_range,
     None, "dedup_binding_invalid"),
    ("dedup_source_wrong_target", _DEDUP_BODY, _REPLAY_TIMEOUT, _bad_dedup_source_wrong_target,
     None, "dedup_binding_invalid"),
    ("occurrence_index_out_of_range", _TWO_BLOCK_BODY, _ONE_TIMEOUT, None, _tamper_results_count,
     "occurrence_index_out_of_range"),
]


@pytest.mark.parametrize(
    "body, outcomes, degrade, tamper, expected_reason",
    [case[1:] for case in _UNKNOWN_CASES],
    ids=[case[0] for case in _UNKNOWN_CASES],
)
def test_mismatch_or_missing_binding_records_unknown_without_new_gate(
    monkeypatch, tmp_path, body, outcomes, degrade, tamper, expected_reason
):
    review, readiness, merged, code, _launcher = _full_chain(
        monkeypatch, tmp_path, body, outcomes, degrade=degrade, tamper_readiness=tamper
    )

    # No new gate: the merge succeeds with the very same routing facts as a
    # fully bound timeout (human_judgment failure_class, needs-fix verdict,
    # timeout string only in blocking_issues, no deterministic blocker).
    assert merged is not None and code == 1
    assert readiness["status"] == "human_judgment"
    assert merged["failure_class"] == "contract_readiness_human_judgment"
    assert merged["verdict"] == "needs-fix"
    assert "Command exceeded timeout" in merged["blocking_issues"]
    assert merged["structured_blockers"] == review["structured_blockers"]

    diagnostics = merged["timeout_diagnostics"]
    assert diagnostics["total_timeout_occurrences"] >= 1
    occurrences = diagnostics["occurrences"]
    if expected_reason == "dedup_binding_invalid":
        # Only the replay's binding is broken: the real execution (index 0)
        # stays attributed, and the replay is not guessed onto it.
        assert [o["attribution"] for o in occurrences] == ["attributed", "unknown"]
        assert occurrences[0]["occurrence_index"] == 0
        occurrences = occurrences[1:]
    for occurrence in occurrences:
        assert occurrence["attribution"] == "unknown"
        assert occurrence["reason_code"] == expected_reason
        # Unknown never carries completed identity / budget for a guessed VC.
        assert occurrence["occurrence_index"] is None
        assert occurrence["timeout_provenance"] is None
        assert occurrence["execution_key_hash"] is None
    assert expected_reason in cic.TIMEOUT_UNKNOWN_REASON_CODES


def test_body_sha_mismatch_still_fails_closed_without_diagnostic(monkeypatch, tmp_path):
    readiness, _launcher = _readiness_result(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: TIMEOUT_OUTCOME}
    )
    review = _review_result(tmp_path, _TWO_BLOCK_BODY)
    readiness["body_sha256"] = _OTHER_DIGEST

    with pytest.raises(ValueError, match="body_sha256 mismatch"):
        cic.merge_readiness_into_review_result(
            review,
            readiness,
            readiness_artifact_path="readiness_result.json",
            iteration_id="timeout_diagnostic_projection_test",
        )
    code, merged = _merge(tmp_path, review, readiness)
    assert code == 1
    assert merged is None


def test_non_timeout_human_judgment_gets_no_timeout_diagnostics(monkeypatch, tmp_path):
    # A non-timeout inner failure (exit 1, no recognised category) is a
    # human_judgment too; it must not produce `timeout_diagnostics`.
    _review, readiness, merged, _code, _launcher = _full_chain(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, {2: (1, "", "boom", 9, {})}
    )
    assert merged is not None
    assert "timeout_diagnostics" not in merged
    assert not any(error["category"] == "timeout" for error in readiness["errors"])


def test_more_than_sixteen_timeouts_are_truncated_within_size_bound():
    # Pure projection bound check on a readiness result with 20 timeout
    # errors whose bindings are all valid (no routing involved).
    digest = "sha256:" + "11" * 32
    errors = []
    for index in range(20):
        errors.append(
            {
                "rule_id": "VCP_TIMEOUT",
                "category": "timeout",
                "source_check": "baseline_vc_preflight",
                "line_start": 3,
                "source_payload": {
                    "occurrence_index": index,
                    "command_hash": "sha256:" + "22" * 32,
                    "execution_key_hash": "sha256:" + f"{index:02x}" * 32,
                    "execution_source": "executed",
                    "dedup_source_result_index": None,
                    "canonical_plan_digest": digest,
                    "timeout_provenance": {
                        "timeout_seconds": 150,
                        "cleanup_tail_seconds": 15,
                        "source": "static_fallback",
                        "estimator_version": "v2",
                        "estimator_input_digest": "sha256:" + "33" * 32,
                    },
                },
            }
        )
    readiness = {"errors": errors, "canonical_plan_digest": digest, "results_count": 20}
    diagnostics = cic.build_timeout_diagnostics(readiness, body_sha256="sha256:" + "44" * 32)
    assert len(diagnostics["occurrences"]) == 16
    assert diagnostics["truncated_count"] == 4
    assert diagnostics["total_timeout_occurrences"] == 20
    assert len(json.dumps(diagnostics).encode("utf-8")) <= 16 * 1024
