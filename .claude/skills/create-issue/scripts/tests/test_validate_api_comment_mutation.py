"""Unit tests for API comment mutation classification."""

import json
import subprocess
from pathlib import Path

VALIDATOR = Path(__file__).parent.parent / "validate_japanese_content.py"


def run_validator(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "python3", str(VALIDATOR), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_issue_comment_body_mutation_classified(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"body": "test body"}))

    result = run_validator(
        "--classify-api-mutation",
        str(payload_file),
        "--api-endpoint",
        "repos/owner/repo/issues/comments/123",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "BODY_MUTATION_ISSUE_COMMENT:owner:repo:123"


def test_pr_review_comment_body_mutation_classified(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"body": "test body"}))

    result = run_validator(
        "--classify-api-mutation",
        str(payload_file),
        "--api-endpoint",
        "repos/owner/repo/pulls/comments/456",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "BODY_MUTATION_PR_REVIEW_COMMENT:owner:repo:456"


def test_comment_endpoint_no_body_key_non_mutation(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"title": "test title"}))

    result = run_validator(
        "--classify-api-mutation",
        str(payload_file),
        "--api-endpoint",
        "repos/owner/repo/issues/comments/123",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "NOT_BODY_MUTATION"


def test_post_issue_comment_endpoint_non_mutation(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"body": "comment body"}))

    result = run_validator(
        "--classify-api-mutation",
        str(payload_file),
        "--api-endpoint",
        "repos/owner/repo/issues/123/comments",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "NOT_BODY_MUTATION"


def test_get_method_detected_as_get():
    result = run_validator(
        "--extract-api-command-method",
        "gh api repos/owner/repo/issues/comments/123 --method GET --input payload.json",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "GET"


def test_delete_method_detected_as_delete():
    result = run_validator(
        "--extract-api-command-method",
        "gh api repos/owner/repo/issues/comments/123 --method DELETE --input payload.json",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "DELETE"
