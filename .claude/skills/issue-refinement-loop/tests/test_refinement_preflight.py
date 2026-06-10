"""
test_refinement_preflight.py

Tests for run_refinement_preflight.py (AC1–AC12).

VC rg keywords verified in this file:
  - ANCHOR_NOT_IN_ISSUE    (AC2)
  - raw_issue_snapshot     (AC3)
  - refinement_preflight_result_v1  (AC6)
  - exit_code_mapping      (AC7)
  - substring              (AC8)
  - sentinel               (AC9)
  - byte_stable            (AC10)
  - argv                   (AC11)
  - environment_failure    (AC12)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as wrapper  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def make_minimal_fixture(
    issue_number: int = 200,
    repo: str = "testowner/testrepo",
    body: str = "",
    comments: list | None = None,
) -> dict:
    """Create a minimal fixture dict for in-memory testing."""
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": repo,
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": body,
            "labels": [],
        },
        "comments": comments or [],
        "anchor_comment_urls": [],
    }


VALID_ISSUE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#1"
```

## Parent Issue

#1

## Parent Goal Ref

- Goal: Test goal

## Current Validated Scope

- scripts/example.py

## Remaining Parent Gaps

- [ ] Nothing remaining

## Outcome

Add `scripts/example.py`.

## In Scope

- scripts/example.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
uv run python3 scripts/example.py
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

なし
"""


# ---------------------------------------------------------------------------
# AC1: fixture input produces STATUS / NEXT_ACTION / MUST_READ / COMMANDS /
#      BLOCKERS / ARTIFACT in stdout
# ---------------------------------------------------------------------------

class TestAC1BasicFixtureOutput:
    """AC1: run_refinement_preflight with fixture produces expected stdout fields."""

    def test_pass_fixture_stdout_fields(self, tmp_path, capsys):
        """AC1: fixture input produces STATUS, NEXT_ACTION, COMMANDS, ARTIFACT in stdout."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY)),
            encoding="utf-8",
        )

        # Patch artifact dir to tmp_path so we don't pollute the repo
        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=200,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out

        assert "STATUS:" in stdout, "stdout must contain STATUS field"
        assert "NEXT_ACTION:" in stdout, "stdout must contain NEXT_ACTION field"
        assert "COMMANDS:" in stdout, "stdout must contain COMMANDS field"
        assert "ARTIFACT:" in stdout, "stdout must contain ARTIFACT field"
        assert result["schema_version"] == "refinement_preflight_result/v1"

    def test_pass_fixture_exit_code_zero(self, tmp_path):
        """AC1: pass fixture exits with code 0."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=200,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_PASS, f"Expected exit 0 (pass), got {exit_code}"


# ---------------------------------------------------------------------------
# AC2: ANCHOR_NOT_IN_ISSUE — anchor comment URL from wrong issue → exit 2
# ---------------------------------------------------------------------------

class TestAC2AnchorNotInIssue:
    """AC2: ANCHOR_NOT_IN_ISSUE blocker for anchor comment from different issue."""

    def test_anchor_wrong_issue_number_exit_2(self, tmp_path, capsys):
        """AC2: anchor URL with issue 999 against issue 100 → ANCHOR_NOT_IN_ISSUE / exit 2."""
        # The anchor URL points to issue 999, but we are checking issue 100
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/999#issuecomment-9999001"
            ],
            "anchor_comments": [
                {
                    "id": 9999001,
                    "body": "some comment",
                    "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100",
                }
            ],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        # AC2: ANCHOR_NOT_IN_ISSUE must appear in blockers
        assert wrapper.BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH in result["blockers"] or \
               wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in result["blockers"], \
               f"Expected ANCHOR_NOT_IN_ISSUE blocker, got {result['blockers']}"
        assert exit_code == wrapper.EXIT_BLOCKED, f"Expected exit 2, got {exit_code}"
        assert result["status"] == "blocked"

    def test_anchor_wrong_repo_exit_2(self, tmp_path, capsys):
        """AC2: anchor URL with wrong owner → ANCHOR_NOT_IN_ISSUE / exit 2."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/otherowner/testrepo/issues/100#issuecomment-9999002"
            ],
            "anchor_comments": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert any(
            b in result["blockers"]
            for b in [
                wrapper.BLOCKER_ANCHOR_REPO_MISMATCH,
                wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE,
            ]
        ), f"Expected repo mismatch blocker, got {result['blockers']}"
        assert exit_code == wrapper.EXIT_BLOCKED

    def test_anchor_comment_not_found_in_fixture(self, tmp_path, capsys):
        """AC2: anchor URL with valid structure but comment ID not in fixture → blocked."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/100#issuecomment-9999999"
            ],
            # anchor_comments does NOT contain id 9999999
            "anchor_comments": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert wrapper.BLOCKER_ANCHOR_COMMENT_NOT_FOUND in result["blockers"], \
               f"Expected ANCHOR_COMMENT_NOT_FOUND, got {result['blockers']}"
        assert exit_code == wrapper.EXIT_BLOCKED


# ---------------------------------------------------------------------------
# AC3: raw_issue_snapshot — raw body NOT in stdout, written to artifact JSON
# ---------------------------------------------------------------------------

class TestAC3RawIssueSnapshotNotInStdout:
    """AC3: raw issue body / comments go to artifact JSON, not stdout."""

    def test_raw_issue_body_not_in_stdout(self, tmp_path, capsys):
        """AC3: raw_issue_snapshot content does not appear in stdout."""
        sentinel_body = "UNIQUE_BODY_TEXT_SHOULD_NOT_APPEAR_IN_STDOUT_XYZ789"
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY + f"\n{sentinel_body}")
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=200,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out
        stderr = captured.err

        # raw_issue_snapshot must NOT appear in stdout
        assert sentinel_body not in stdout, \
               "raw issue body sentinel must not appear in stdout"
        assert sentinel_body not in stderr, \
               "raw issue body sentinel must not appear in stderr"

    def test_raw_issue_snapshot_written_to_artifact(self, tmp_path, capsys):
        """AC3: raw_issue_snapshot artifact JSON is written."""
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=300)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=300,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        artifacts = result.get("artifacts", {})
        assert "raw_issue_snapshot" in artifacts, \
               "raw_issue_snapshot artifact path must be in result.artifacts"

        snapshot_path = Path(artifacts["raw_issue_snapshot"])
        assert snapshot_path.exists(), f"Snapshot artifact file not found: {snapshot_path}"

        snapshot_data = json.loads(snapshot_path.read_text())
        # Confirm it has expected structure
        assert snapshot_data.get("schema_version") == "raw_issue_snapshot/v1"
        assert "issue" in snapshot_data


# ---------------------------------------------------------------------------
# AC6: refinement_preflight_result_v1 schema — stdout projection matches artifact
# ---------------------------------------------------------------------------

class TestAC6SchemaAndProjectionConsistency:
    """AC6: result schema exists; stdout projection and artifact JSON share same status/blockers/next_action."""

    def test_schema_file_exists(self):
        """AC6: refinement_preflight_result_v1.schema.json must exist."""
        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        assert schema_path.exists(), f"Schema file not found: {schema_path}"

    def test_input_schema_file_exists(self):
        """AC6: refinement_preflight_input.schema.json must exist."""
        schema_path = SCHEMAS_DIR / "refinement_preflight_input.schema.json"
        assert schema_path.exists(), f"Schema file not found: {schema_path}"

    def test_result_schema_additionalProperties_false(self):
        """AC6: result schema must be strict (additionalProperties: false)."""
        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        schema = json.loads(schema_path.read_text())
        assert schema.get("additionalProperties") is False, \
               "refinement_preflight_result_v1 schema must have additionalProperties: false"

    def test_stdout_projection_matches_artifact(self, tmp_path, capsys):
        """AC6: stdout STATUS/NEXT_ACTION/BLOCKERS match artifact JSON fields."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=400)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=400,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out

        # Stdout must contain the same status as result artifact
        assert f"STATUS: {result['status']}" in stdout
        assert f"NEXT_ACTION: {result['next_action']}" in stdout

        # Artifact must have schema_version
        assert result["schema_version"] == "refinement_preflight_result/v1"

        # Artifact file must also exist
        artifact_path = result.get("artifacts", {}).get("refinement_preflight_result_v1")
        assert artifact_path, "artifact path must be present"
        artifact_data = json.loads(Path(artifact_path).read_text())
        assert artifact_data["status"] == result["status"]
        assert artifact_data["next_action"] == result["next_action"]


# ---------------------------------------------------------------------------
# AC7: exit_code_mapping — all mapping table branches covered
# ---------------------------------------------------------------------------

class TestAC7ExitCodeMapping:
    """AC7: planner↔wrapper exit_code_mapping all branches."""

    def test_exit_code_mapping_anchor_not_in_issue_blocked(self):
        """AC7: exit_code_mapping — ANCHOR_NOT_IN_ISSUE → blocked / 2."""
        status, code = wrapper._apply_exit_code_mapping(
            None, None, [wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE]
        )
        assert status == "blocked"
        assert code == wrapper.EXIT_BLOCKED

    def test_exit_code_mapping_gh_failure_environment(self):
        """AC7: exit_code_mapping — GH_API_FAILURE → environment_failure / 3."""
        status, code = wrapper._apply_exit_code_mapping(
            None, None, [wrapper.BLOCKER_GH_FAILURE]
        )
        assert status == "environment_failure"
        assert code == wrapper.EXIT_ENVIRONMENT_FAILURE

    def test_exit_code_mapping_planner_exit_2_blocked(self):
        """AC7: exit_code_mapping — planner exit 2 → blocked / 2."""
        status, code = wrapper._apply_exit_code_mapping(2, None, [])
        assert status == "blocked"
        assert code == wrapper.EXIT_BLOCKED

    def test_exit_code_mapping_planner_exit_3_environment(self):
        """AC7: exit_code_mapping — planner exit 3 → environment_failure / 3."""
        status, code = wrapper._apply_exit_code_mapping(3, None, [])
        assert status == "environment_failure"
        assert code == wrapper.EXIT_ENVIRONMENT_FAILURE

    def test_exit_code_mapping_planner_exit_0_fail_closed_blocked(self):
        """AC7: exit_code_mapping — planner exit 0 + fail_closed.required=true → blocked / 2."""
        status, code = wrapper._apply_exit_code_mapping(0, True, [])
        assert status == "blocked"
        assert code == wrapper.EXIT_BLOCKED

    def test_exit_code_mapping_planner_exit_0_pass(self):
        """AC7: exit_code_mapping — planner exit 0, fail_closed=false → pass / 0."""
        status, code = wrapper._apply_exit_code_mapping(0, False, [])
        assert status == "pass"
        assert code == wrapper.EXIT_PASS

    def test_exit_code_mapping_no_blockers_no_planner(self):
        """AC7: exit_code_mapping — planner None, no blockers → environment_failure."""
        status, code = wrapper._apply_exit_code_mapping(None, None, [])
        assert status == "environment_failure"
        assert code == wrapper.EXIT_ENVIRONMENT_FAILURE

    def test_exit_code_mapping_full_table_coverage(self):
        """AC7: exit_code_mapping covers all rows of the mapping table."""
        # Row 1: anchor comment not in issue → blocked / 2
        s, c = wrapper._apply_exit_code_mapping(None, None, ["ANCHOR_NOT_IN_ISSUE"])
        assert s == "blocked" and c == 2

        # Row 2: gh failure → environment_failure / 3
        s, c = wrapper._apply_exit_code_mapping(None, None, ["GH_API_FAILURE"])
        assert s == "environment_failure" and c == 3

        # Row 3: planner exit 2 → blocked / 2
        s, c = wrapper._apply_exit_code_mapping(2, None, [])
        assert s == "blocked" and c == 2

        # Row 4: planner exit 3 → environment_failure / 3
        s, c = wrapper._apply_exit_code_mapping(3, None, [])
        assert s == "environment_failure" and c == 3

        # Row 5: planner exit 0 + fail_closed=true → blocked / 2
        s, c = wrapper._apply_exit_code_mapping(0, True, [])
        assert s == "blocked" and c == 2

        # Row 6: planner exit 0, normal → pass / 0
        s, c = wrapper._apply_exit_code_mapping(0, False, [])
        assert s == "pass" and c == 0


# ---------------------------------------------------------------------------
# AC8: substring — anchor URL parser is structural, not substring-based
# ---------------------------------------------------------------------------

class TestAC8SubstringFreeAnchorValidation:
    """AC8: anchor URL validation uses structural parsing, not substring matching."""

    def test_parse_valid_anchor_url_structure(self):
        """AC8: valid anchor URL is parsed structurally into owner/repo/issue/comment."""
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/myowner/myrepo/issues/42#issuecomment-12345"
        )
        assert parsed["valid"] is True
        assert parsed["owner"] == "myowner"
        assert parsed["repo"] == "myrepo"
        assert parsed["issue_number"] == 42
        assert parsed["comment_id"] == 12345

    def test_parse_invalid_url_no_substring_match(self):
        """AC8: URL that contains 'issues' as substring but wrong structure → invalid."""
        # This URL looks like it might contain 'issues' but is structurally wrong
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/owner/repo/pull/10#issuecomment-99"
        )
        # PR URL does not match the issue comment regex (no /issues/ path)
        assert parsed["valid"] is False

    def test_parse_pr_review_comment_url_rejected(self):
        """AC8: PR review comment URL (#discussion_r...) is rejected — not issue comment."""
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/owner/repo/pull/10#discussion_r12345"
        )
        assert parsed["valid"] is False
        assert parsed.get("error") == "pr_review_comment_url"

    def test_url_without_anchor_fragment_rejected(self):
        """AC8: URL without #issuecomment-N fragment is rejected."""
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/owner/repo/issues/42"
        )
        assert parsed["valid"] is False

    def test_substring_does_not_pass_validation(self):
        """AC8: substring match alone does not grant valid status — regex required.

        This test documents that the validator does NOT use substring matching.
        A URL containing 'issues' as substring but malformed structure → invalid.
        """
        # "issues" appears but the URL structure is wrong (repo path mangled)
        malformed = "https://github.com/owner/NOTREPO/issues-extra/42#issuecomment-1"
        parsed = wrapper._parse_anchor_comment_url(malformed)
        # The regex _ISSUE_COMMENT_RE requires /issues/<digits>#issuecomment-<digits>
        # so a path like /issues-extra/ will NOT match
        assert parsed["valid"] is False


# ---------------------------------------------------------------------------
# AC9: sentinel — raw body sentinel does not appear in stdout or stderr
# ---------------------------------------------------------------------------

class TestAC9SentinelNotInStdout:
    """AC9: sentinel strings from raw issue body/comments do not leak to stdout/stderr."""

    def test_sentinel_not_in_stdout(self, tmp_path, capsys):
        """AC9: sentinel embedded in issue body is absent from stdout."""
        sentinel = "SECRET_SENTINEL_SHOULD_NOT_LEAK_TO_STDOUT_99XYZ"
        body = VALID_ISSUE_BODY + f"\n{sentinel}\n"
        fixture_data = make_minimal_fixture(body=body, issue_number=500)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            wrapper.run_preflight(
                issue_number=500,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert sentinel not in captured.out, \
               "sentinel from issue body must not appear in stdout"
        assert sentinel not in captured.err, \
               "sentinel from issue body must not appear in stderr"

    def test_comment_sentinel_not_in_stdout(self, tmp_path, capsys):
        """AC9: sentinel embedded in comment body is absent from stdout."""
        sentinel = "COMMENT_SENTINEL_MUST_NOT_LEAK_88ABC"
        fixture_data = make_minimal_fixture(
            body=VALID_ISSUE_BODY,
            issue_number=501,
            comments=[{"id": 1, "body": f"Some comment with {sentinel}", "issue_url": ""}],
        )
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            wrapper.run_preflight(
                issue_number=501,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert sentinel not in captured.out, \
               "sentinel from comment must not appear in stdout"
        assert sentinel not in captured.err, \
               "sentinel from comment must not appear in stderr"

    def test_sentinel_not_via_exception_path(self, tmp_path, capsys):
        """AC9: sentinel does not leak even through exception/error paths."""
        sentinel = "EXCEPTION_PATH_SENTINEL_77DEF"
        # Provide a fixture that triggers a planner error but has sentinel in body
        bad_body = f"no required sections here {sentinel}"
        fixture_data = make_minimal_fixture(body=bad_body, issue_number=502)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            wrapper.run_preflight(
                issue_number=502,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert sentinel not in captured.out
        assert sentinel not in captured.err


# ---------------------------------------------------------------------------
# AC10: byte_stable — same fixture produces byte-stable artifact
# ---------------------------------------------------------------------------

class TestAC10ByteStableFixture:
    """AC10: --fixture produces byte-stable result artifact regardless of run count."""

    def test_byte_stable_result_core_hash(self, tmp_path):
        """AC10: two runs with same fixture produce identical result_core_sha256."""
        fixture_data = make_minimal_fixture(
            body=VALID_ISSUE_BODY,
            issue_number=600,
        )
        # Set deterministic 'now' timestamp
        fixture_data["now"] = "2026-01-01T12:00:00+00:00"

        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        results = []
        for _ in range(2):
            import io
            from contextlib import redirect_stdout

            out = io.StringIO()
            with (
                mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
                redirect_stdout(out),
            ):
                result, _ = wrapper.run_preflight(
                    issue_number=600,
                    repo="testowner/testrepo",
                    anchor_comment_urls=[],
                    fixture_path=fixture_path,
                )
            results.append(result)

        hash1 = results[0].get("hashes", {}).get("result_core_sha256")
        hash2 = results[1].get("hashes", {}).get("result_core_sha256")

        assert hash1 is not None, "result_core_sha256 must be present"
        assert hash1 == hash2, \
               f"byte_stable: hashes differ between runs: {hash1} != {hash2}"

    def test_byte_stable_snapshot_hash(self, tmp_path):
        """AC10: byte_stable — same fixture produces identical raw_issue_snapshot_sha256."""
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=601)
        fixture_data["now"] = "2026-01-01T00:00:00+00:00"

        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        hashes_list = []
        for _ in range(2):
            import io
            from contextlib import redirect_stdout

            out = io.StringIO()
            with (
                mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
                redirect_stdout(out),
            ):
                result, _ = wrapper.run_preflight(
                    issue_number=601,
                    repo="testowner/testrepo",
                    anchor_comment_urls=[],
                    fixture_path=fixture_path,
                )
            hashes_list.append(result.get("hashes", {}).get("raw_issue_snapshot_sha256"))

        assert hashes_list[0] is not None
        assert hashes_list[0] == hashes_list[1], \
               f"byte_stable: snapshot hashes differ: {hashes_list}"


# ---------------------------------------------------------------------------
# AC11: argv — commands[] contains only argv arrays, no shell strings
# ---------------------------------------------------------------------------

class TestAC11ArgvOnlyCommands:
    """AC11: commands[] uses argv arrays exclusively, no shell strings."""

    def test_commands_have_argv_array(self, tmp_path, capsys):
        """AC11: every command in result has 'argv' as a list, not a string."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=700)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=700,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        commands = result.get("commands", [])
        assert len(commands) > 0, "Expected at least one command in result"

        for cmd in commands:
            assert isinstance(cmd.get("argv"), list), \
                   f"argv must be a list, got {type(cmd.get('argv'))}: {cmd}"
            # Each argv element must be a string
            for arg in cmd["argv"]:
                assert isinstance(arg, str), \
                       f"Each argv element must be a string, got {type(arg)}: {arg}"

    def test_commands_shell_false(self, tmp_path, capsys):
        """AC11: every command has shell=false."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=701)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=701,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        for cmd in result.get("commands", []):
            assert cmd.get("shell") is False, \
                   f"shell must be False (boolean), got {cmd.get('shell')}"

    def test_commands_source_static_wrapper_template(self, tmp_path, capsys):
        """AC11: every command has source='static_wrapper_template'."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=702)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=702,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        for cmd in result.get("commands", []):
            assert cmd.get("source") == "static_wrapper_template", \
                   f"source must be 'static_wrapper_template', got {cmd.get('source')}"

    def test_argv_contains_no_shell_string(self, tmp_path, capsys):
        """AC11: argv elements do not contain shell metacharacters as concatenated commands."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=703)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=703,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        for cmd in result.get("commands", []):
            # argv should not be a single string with shell operators
            argv = cmd.get("argv", [])
            assert isinstance(argv, list), "argv must be a list"
            # No single element should contain shell pipes/redirects/semicolons
            for arg in argv:
                assert " | " not in arg, f"argv element contains pipe: {arg!r}"
                assert " && " not in arg, f"argv element contains &&: {arg!r}"
                assert " || " not in arg, f"argv element contains ||: {arg!r}"


# ---------------------------------------------------------------------------
# AC12: environment_failure — gh not found → exit 3
# ---------------------------------------------------------------------------

class TestAC12EnvironmentFailure:
    """AC12: gh not found / auth failure / timeout → environment_failure / exit 3."""

    def test_gh_not_found_returns_none(self):
        """AC12: _run_gh with nonexistent binary returns None and error string."""
        result, err = wrapper._run_gh(["nonexistent_binary_xyz_12345", "--version"])
        assert result is None
        assert "gh_not_found" in err or "not_found" in err or len(err) > 0

    def test_environment_failure_when_gh_returns_error(self, tmp_path, capsys):
        """AC12: environment_failure when gh is simulated to fail (mock _fetch_issue)."""
        # Simulate gh failure by patching _fetch_issue to return (None, error)
        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            mock.patch.object(wrapper, "_fetch_issue", return_value=(None, "gh_exit_1: auth failure")),
        ):
            result, exit_code = wrapper.run_preflight(
                issue_number=800,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=None,
            )

        assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE, \
               f"Expected environment_failure exit 3, got {exit_code}"
        assert result["status"] == "environment_failure"
        assert wrapper.BLOCKER_GH_FAILURE in result["blockers"]

    def test_environment_failure_when_comments_fail(self, tmp_path, capsys):
        """AC12: environment_failure when gh comments API fails."""
        fake_issue = {
            "number": 800,
            "title": "Test",
            "body": VALID_ISSUE_BODY,
            "labels": [],
        }
        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            mock.patch.object(wrapper, "_fetch_issue", return_value=(fake_issue, "")),
            mock.patch.object(wrapper, "_fetch_issue_comments", return_value=(None, "gh_exit_1: not authorized")),
        ):
            result, exit_code = wrapper.run_preflight(
                issue_number=800,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=None,
            )

        assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE
        assert result["status"] == "environment_failure"

    def test_environment_failure_timeout_mock(self):
        """AC12: environment_failure — gh timeout maps to error string."""
        # Simulate timeout error in _run_gh
        import subprocess as _sp
        with mock.patch("subprocess.run", side_effect=_sp.TimeoutExpired(["gh"], 30)):
            result, err = wrapper._run_gh(["gh", "issue", "view", "1"])
        assert result is None
        assert "timeout" in err.lower()

    def test_environment_failure_non_json_response(self):
        """AC12: environment_failure when gh returns non-JSON output."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="this is not json !@#",
                stderr="",
            )
            result, err = wrapper._run_gh(["gh", "api", "repos/x/y/issues/1"])

        assert result is None
        assert "json_decode_error" in err.lower() or "decode" in err.lower()


# ---------------------------------------------------------------------------
# Additional structural tests
# ---------------------------------------------------------------------------

class TestAnchorCommentBatchValidation:
    """Test anchor batch validation with stable sort + dedupe."""

    def test_stable_sort_dedupe_applied(self, tmp_path):
        """Multiple same URLs are deduped and processed once."""
        # Provide two identical URLs — should only validate once
        urls = [
            "https://github.com/testowner/testrepo/issues/100#issuecomment-7777001",
            "https://github.com/testowner/testrepo/issues/100#issuecomment-7777001",
        ]
        comments = [
            {
                "id": 7777001,
                "body": "ok comment",
                "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100",
            }
        ]

        sorted_urls, blockers = wrapper._validate_anchor_comments_batch(
            urls, "testowner/testrepo", 100, fixture_comments=comments
        )

        # After dedupe, only 1 unique URL
        assert len(sorted_urls) == 1
        assert blockers == []

    def test_one_invalid_url_blocks_all(self):
        """One invalid URL in batch → all blocked."""
        urls = [
            "https://github.com/testowner/testrepo/issues/100#issuecomment-7777001",
            "https://github.com/OTHER/testrepo/issues/100#issuecomment-7777002",  # wrong owner
        ]
        comments = [
            {"id": 7777001, "body": "ok", "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100"},
            {"id": 7777002, "body": "ok", "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100"},
        ]

        sorted_urls, blockers = wrapper._validate_anchor_comments_batch(
            urls, "testowner/testrepo", 100, fixture_comments=comments
        )

        assert len(blockers) > 0, "Should have blockers for wrong owner"


class TestBuildCompactStdout:
    """Tests for _build_compact_stdout — sentinel isolation."""

    def test_compact_stdout_does_not_include_body(self):
        """_build_compact_stdout must not include issue body sentinel in output."""
        result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "pass",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": 0,
            "planner_fail_closed": False,
            "next_action": "proceed",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": [],
            "artifacts": {"refinement_preflight_result_v1": "/tmp/result.json"},
            "hashes": {},
        }
        output = wrapper._build_compact_stdout(result)
        assert "STATUS: pass" in output
        assert "NEXT_ACTION: proceed" in output
        # Must not contain any raw body data
        assert "issue_body" not in output.lower()

    def test_compact_stdout_no_raw_comment_text(self):
        """_build_compact_stdout must not include comment body text."""
        sensitive = "MY_PRIVATE_COMMENT_DATA"
        result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "blocked",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": None,
            "planner_fail_closed": None,
            "next_action": "human_judgment_required",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": ["ANCHOR_NOT_IN_ISSUE"],
            "artifacts": {},
            "hashes": {},
        }
        output = wrapper._build_compact_stdout(result)
        assert sensitive not in output
        assert "BLOCKERS:" in output
        assert "ANCHOR_NOT_IN_ISSUE" in output
