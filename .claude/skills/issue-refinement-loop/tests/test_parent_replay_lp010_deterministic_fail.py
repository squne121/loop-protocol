#!/usr/bin/env python3
"""Parent replay integration test for the LP010 fix (Issue #1704 AC7).

Background: Issue #1415's refinement loop (2026-07-24) hit a false
`reviewer_false_positive_suspected` -> `human_escalation` outcome because
`validate_issue_body.py`'s LP010 vacuously PASSED a body that
`check_issue_contract.py`'s C5_ac_vc_number_alignment correctly FAILED
(asterisk-bullet AC section, hyphen-only LP010 regex -- see Issue #1704
Background). This test confirms, end to end, that a real
`contract_readiness_check.py --mode static` run over a fixture body that
now trips the FIXED LP010 rule produces a `readiness_result` whose
`errors` contain an `LP010` entry, and that feeding a matching
`REVIEWER_BLOCKER_CLAIM_V1` (same `body_sha256`, `reviewer_blocker_code:
"lp010"`) into `parent_replay_binding.build_parent_replay_binding()`
(Issue #1532's parent-local replay integrity binding) yields
`deterministic_fail_confirmed` / `proceed_to_rewrite` -- NOT
`reviewer_claim_unbacked_by_deterministic_checker` /
`reviewer_false_positive_suspected`.

`parent_replay_binding.py` itself is NOT modified by Issue #1704 (Stop
Condition); this is an integration test only, exercising the existing
`ac_vc_number_mismatch` taxonomy entry (`readiness_rule_ids: ["LP010"]`,
`reviewer_codes: ["lp010", "ac_vc_number_mismatch", "c5"]`) in
`reviewer_claim_replay.py` against a real subprocess invocation of
`contract_readiness_check.py --mode static`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

# __file__ is at: <repo>/.claude/skills/issue-refinement-loop/tests/test_parent_replay_lp010_deterministic_fail.py
# parents: [0]=tests, [1]=issue-refinement-loop, [2]=skills, [3]=.claude, [4]=<repo root>
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ISSUE_REFINEMENT_LOOP_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
_CONTRACT_READINESS_CHECK_PY = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
)

sys.path.insert(0, str(_ISSUE_REFINEMENT_LOOP_SCRIPTS))

from parent_replay_binding import build_parent_replay_binding  # noqa: E402


# Same shape as Issue #1704's zero-VC-marker AC2/AC5/AC8 fixture: AC1-AC15,
# all asterisk-bullet, VC section with zero `# AC<N>` markers.
_LP010_TRIPPING_BODY = (
    "## Acceptance Criteria\n\n"
    + "\n".join(f"* [ ] AC{n}: item number {n}" for n in range(1, 16))
    + "\n\n## Verification Commands\n\n"
    "```bash\n"
    "$ echo \"no per-AC markers here\"\n"
    "```\n"
)


def _run_contract_readiness_check(tmp_path: Path, body: str) -> tuple[dict, int]:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_CONTRACT_READINESS_CHECK_PY),
            "--body-file",
            str(body_file),
            "--mode",
            "static",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(proc.stdout), proc.returncode


class TestLp010ErrorDrivesDeterministicFailConfirmed:
    def test_lp010_error_drives_deterministic_fail_confirmed(self, tmp_path: Path):
        """GIVEN a fixture body that trips the FIXED LP010 rule (Issue
        #1704) WHEN `contract_readiness_check.py --mode static` runs over
        it, and a `REVIEWER_BLOCKER_CLAIM_V1` claiming `lp010` for the
        SAME `body_sha256` is replayed through
        `build_parent_replay_binding()` THEN the replay verdict is
        `deterministic_fail_confirmed` with routing `proceed_to_rewrite`
        (NOT `reviewer_claim_unbacked_by_deterministic_checker` /
        `reviewer_false_positive_suspected`)."""
        readiness_result, exit_code = _run_contract_readiness_check(
            tmp_path, _LP010_TRIPPING_BODY
        )

        # contract_readiness_check.py: 1 == needs_fix (body-author-fixable errors)
        assert exit_code == 1
        assert readiness_result["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
        lp010_errors = [e for e in readiness_result["errors"] if e["rule_id"] == "LP010"]
        assert len(lp010_errors) > 0

        body_bytes = _LP010_TRIPPING_BODY.encode("utf-8")
        body_sha256 = "sha256:" + hashlib.sha256(body_bytes).hexdigest()
        assert readiness_result["body_sha256"] == body_sha256

        reviewer_blocker_claim = {
            "schema": "REVIEWER_BLOCKER_CLAIM_V1",
            "body_sha256": body_sha256,
            "blockers": [
                {
                    "reviewer_blocker_code": "lp010",
                    "message": "AC <=> VC number set mismatch (LP010)",
                    "line_start": lp010_errors[0]["line_start"],
                    "line_end": lp010_errors[0]["line_end"],
                }
            ],
        }

        artifact = build_parent_replay_binding(
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness_result,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            current_body_bytes=body_bytes,
            issue_url="https://github.com/squne121/loop-protocol/issues/1415",
            repository_full_name="squne121/loop-protocol",
            issue_number=1415,
            refinement_session_id="test-session-1704",
            iteration_id="iteration-1",
        )

        assert artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"
        assert artifact["replay_result"]["routing"] == "proceed_to_rewrite"
        assert artifact["replay_result"]["verdict"] != "reviewer_claim_unbacked_by_deterministic_checker"
        assert artifact["replay_result"]["verdict"] != "reviewer_false_positive_suspected"


class TestLp010ErrorRepeatedStateStaysDeterministicFailConfirmed:
    def test_lp010_repeated_replay_of_previous_state_stays_deterministic_fail_confirmed(
        self, tmp_path: Path
    ):
        """GIVEN the same LP010-tripping fixture body and the SAME
        REVIEWER_BLOCKER_CLAIM_V1 replayed TWICE in a row -- the second
        call feeds the first call's own `replay_next_state` back in as
        `previous_state` (simulating a parent orchestrator that persists
        state across iterations without the underlying Issue body ever
        changing) -- WHEN `build_parent_replay_binding()` runs both times
        THEN the second replay's verdict is STILL
        `deterministic_fail_confirmed` / `proceed_to_rewrite` (never
        `reviewer_false_positive_suspected` / `human_escalation`), and
        `consecutive_unbacked_count` in the resulting next_state stays
        `0` both times (PR #1717 review required_tests: parent replay
        repeated-state test). A deterministically-backed LP010 finding
        must never accumulate an "unbacked claim" streak just because
        the SAME real failure is replayed again."""
        readiness_result, exit_code = _run_contract_readiness_check(
            tmp_path, _LP010_TRIPPING_BODY
        )
        assert exit_code == 1
        lp010_errors = [e for e in readiness_result["errors"] if e["rule_id"] == "LP010"]
        assert len(lp010_errors) > 0

        body_bytes = _LP010_TRIPPING_BODY.encode("utf-8")
        body_sha256 = "sha256:" + hashlib.sha256(body_bytes).hexdigest()
        assert readiness_result["body_sha256"] == body_sha256

        reviewer_blocker_claim = {
            "schema": "REVIEWER_BLOCKER_CLAIM_V1",
            "body_sha256": body_sha256,
            "blockers": [
                {
                    "reviewer_blocker_code": "lp010",
                    "message": "AC <=> VC number set mismatch (LP010)",
                    "line_start": lp010_errors[0]["line_start"],
                    "line_end": lp010_errors[0]["line_end"],
                }
            ],
        }

        first_artifact = build_parent_replay_binding(
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness_result,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            current_body_bytes=body_bytes,
            issue_url="https://github.com/squne121/loop-protocol/issues/1415",
            repository_full_name="squne121/loop-protocol",
            issue_number=1415,
            refinement_session_id="test-session-1704-repeat",
            iteration_id="iteration-1",
        )

        assert first_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"
        assert first_artifact["replay_result"]["routing"] == "proceed_to_rewrite"
        assert first_artifact["replay_next_state"]["consecutive_unbacked_count"] == 0

        # Second replay: feed the FIRST call's own next_state back in as
        # previous_state -- same body, same claim, same lane.
        second_artifact = build_parent_replay_binding(
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness_result,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=first_artifact["replay_next_state"],
            current_body_bytes=body_bytes,
            issue_url="https://github.com/squne121/loop-protocol/issues/1415",
            repository_full_name="squne121/loop-protocol",
            issue_number=1415,
            refinement_session_id="test-session-1704-repeat",
            iteration_id="iteration-2",
        )

        assert second_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"
        assert second_artifact["replay_result"]["routing"] == "proceed_to_rewrite"
        assert second_artifact["replay_result"]["verdict"] != "reviewer_false_positive_suspected"
        assert second_artifact["replay_next_state"]["consecutive_unbacked_count"] == 0
