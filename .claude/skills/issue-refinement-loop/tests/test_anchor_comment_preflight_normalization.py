import json
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import run_refinement_preflight as wrapper

_ORIGINAL_LOAD_SCHEMA = wrapper._load_schema


def _load_schema_without_input_validation(name: str):
    if name == "refinement_preflight_input.schema.json":
        return None
    return _ORIGINAL_LOAD_SCHEMA(name)


def _fixture() -> dict:
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": 100,
        "repo": "testowner/testrepo",
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": 100,
            "title": "Test",
            "body": (
                "## Machine-Readable Contract\n\n```yaml\n"
                "issue_kind: implementation\ncontract_schema_version: v1\n```\n\n"
                "## Parent Issue\n\nnone\n\n## Parent Goal Ref\n\n- Goal: test\n\n"
                "## Current Validated Scope\n\n- x\n\n## Remaining Parent Gaps\n\n"
                "- [ ] none\n\n## Outcome\n\nx\n\n## In Scope\n\n- x\n\n"
                "## Out of Scope\n\n- y\n\n## Acceptance Criteria\n\n- [ ] AC1: x\n\n"
                "## Verification Commands\n\n```bash\n# baseline-expect: fail\n"
                "$ test -f missing\n```\n\n## Allowed Paths\n\n- x\n\n"
                "## Runtime Verification Applicability\n\ndecision: not_applicable\n\n"
                "## Stop Conditions\n\n- z\n\n## Required Skills\n\n- なし\n"
            ),
            "labels": [],
        },
        "comments": [],
        "anchor_comment_urls": [
            "https://github.com/testowner/testrepo/issues/100#issuecomment-5551001"
        ],
        "anchor_comments": [
            {
                "id": 5551001,
                "body": "anchor comment",
                "html_url": "https://github.com/testowner/testrepo/issues/100#issuecomment-5551001",
                "url": "https://api.github.com/repos/testowner/testrepo/issues/comments/5551001",
                "user": {"login": "owner"},
                "author_association": "OWNER",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100",
            }
        ],
    }


def test_normalized_anchor_comment_state_blocks_missing_required_metadata(tmp_path):
    fixture = _fixture()
    fixture["anchor_comments"][0].pop("author_association")
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path), mock.patch.object(
        wrapper, "_load_schema", side_effect=_load_schema_without_input_validation
    ):
        result, exit_code = wrapper.run_preflight(100, "testowner/testrepo", [], path)
    assert exit_code == wrapper.EXIT_BLOCKED
    assert wrapper.BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID in result["blockers"]


def test_normalized_anchor_comment_state_blocks_invalid_datetime_format(tmp_path):
    fixture = _fixture()
    fixture["anchor_comments"][0]["created_at"] = "not-a-date"
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path), mock.patch.object(
        wrapper, "_load_schema", side_effect=_load_schema_without_input_validation
    ):
        result, exit_code = wrapper.run_preflight(100, "testowner/testrepo", [], path)
    assert exit_code == wrapper.EXIT_BLOCKED
    assert wrapper.BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID in result["blockers"]
