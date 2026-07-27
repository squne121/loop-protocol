from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "scripts/agent-guards/controlled_skill_mutation_policy.py"
EXEC_PATH = ROOT / "scripts/agent-guards/controlled_skill_mutation_exec.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_executor():
    import sys

    policy = load_module("controlled_skill_mutation_policy", POLICY_PATH)
    sys.modules["controlled_skill_mutation_policy"] = policy
    return load_module("controlled_skill_mutation_exec_test", EXEC_PATH), policy


def valid_input(executor, *, target_pr_number=42, linked_issue_number=1647, head="a" * 40):
    linked_body = "sha256:" + "b" * 64
    receipt = {
        "schema": "TEST_VERDICT_PRODUCER_RECEIPT_V1",
        "schema_version": 1,
        "pass_eligible": True,
        "subject": {"target_pr_number": target_pr_number, "pr_head_sha": head, "head_repository_id": 1},
        "contract": {
            "linked_issue_number": linked_issue_number,
            "issue_body_sha256": linked_body,
            "command_manifest_sha256": "sha256:" + "c" * 64,
        },
        "execution_artifact": {
            "artifact_id": 1,
            "artifact_url": "https://example.invalid/a",
            "artifact_archive_digest": "sha256:" + "d" * 64,
        },
    }
    receipt_sha = executor._canonical_sha256(receipt)
    body = "TEST_VERDICT_MACHINE/v2\nstatus: pass"
    body_sha = hashlib.sha256(body.encode()).hexdigest()
    return {
        "schema": "TEST_VERDICT_PUBLISH_INPUT_V1",
        "issue_number": linked_issue_number,
        "repo": "squne121/loop-protocol",
        "target_pr_number": target_pr_number,
        "linked_issue_number": linked_issue_number,
        "expected_head_sha": head,
        "linked_issue_body_sha256": linked_body,
        "producer_receipt": receipt,
        "receipt_sha256": receipt_sha,
        "body": body,
        "body_sha256": body_sha,
        "idempotency_key": (
            f"squne121/loop-protocol:{target_pr_number}:{linked_issue_number}:"
            f"{head}:{receipt_sha}:{body_sha}"
        ),
    }


def test_given_closed_request_when_validated_then_target_pr_and_linked_issue_are_independent():
    executor, policy = load_executor()
    request = valid_input(executor)
    assert executor._validate_test_verdict_publish_fields(request, request["repo"], 1647) == ""
    assert policy.COMMAND_ID_TEST_VERDICT_PUBLISH in policy.ALL_COMMAND_IDS
    request["linked_issue_number"] = request["target_pr_number"]
    assert (
        executor._validate_test_verdict_publish_fields(request, request["repo"], 1647)
        == "test_verdict_publish_linked_issue_number_mismatch"
    )


def test_given_generic_issue_comment_command_when_test_verdict_is_requested_then_no_alias_exists():
    _, policy = load_executor()
    assert policy.COMMAND_ID_ISSUE_COMMENT_PUBLISH != policy.COMMAND_ID_TEST_VERDICT_PUBLISH
    assert policy.INPUT_SCHEMA_BY_COMMAND[policy.COMMAND_ID_ISSUE_COMMENT_PUBLISH] != "TEST_VERDICT_PUBLISH_INPUT_V1"


def test_given_current_receipt_when_publish_then_pre_and_post_head_and_comment_are_verified(monkeypatch, tmp_path):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    marker = executor._test_verdict_marker_str(request["idempotency_key"])
    rendered = f"{request['body']}\n\n{marker}\n"
    comment = {"id": 77, "html_url": "https://example.invalid/comment/77", "body": rendered, "user": {"login": "owner"}}
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([], ""))
    monkeypatch.setattr(executor, "_fetch_pr_head_sha", lambda *a, **k: (request["expected_head_sha"], ""))
    monkeypatch.setattr(
        executor, "_fetch_linked_issue_body_sha256", lambda *a, **k: (request["linked_issue_body_sha256"], "")
    )
    monkeypatch.setattr(executor, "_fetch_authenticated_login", lambda *a, **k: ("owner", ""))
    monkeypatch.setattr(executor, "_post_test_verdict_comment", lambda *a: ({"id": 77}, ""))
    monkeypatch.setattr(executor, "_readback_test_verdict_comment", lambda *a: (comment, ""))
    monkeypatch.setattr(executor, "_check_no_tracked_changes", lambda *a: [])
    monkeypatch.setattr(executor, "_issue_metadata_marker_path", lambda *a: tmp_path / "marker.json")
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, *a, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result["ok"]["comment_id"] == 77
    assert (tmp_path / "marker.json").exists()


def test_given_pre_publish_head_drift_when_publish_then_post_is_not_attempted(monkeypatch):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([], ""))
    monkeypatch.setattr(executor, "_fetch_pr_head_sha", lambda *a, **k: ("e" * 40, ""))
    monkeypatch.setattr(
        executor, "_fetch_linked_issue_body_sha256", lambda *a, **k: (request["linked_issue_body_sha256"], "")
    )
    monkeypatch.setattr(
        executor, "_post_test_verdict_comment", lambda *a: (_ for _ in ()).throw(AssertionError("must not post"))
    )
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result == {"failed": "test_verdict_pre_publish_head_mismatch"}


def test_given_postcondition_author_or_marker_or_body_mismatch_when_published_then_fail_closed(monkeypatch, tmp_path):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    marker = executor._test_verdict_marker_str(request["idempotency_key"])
    comment = {
        "id": 77,
        "html_url": "https://example.invalid/comment/77",
        "body": f"tampered\n\n{marker}\n",
        "user": {"login": "other"},
    }
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([], ""))
    monkeypatch.setattr(executor, "_fetch_pr_head_sha", lambda *a, **k: (request["expected_head_sha"], ""))
    monkeypatch.setattr(
        executor, "_fetch_linked_issue_body_sha256", lambda *a, **k: (request["linked_issue_body_sha256"], "")
    )
    monkeypatch.setattr(executor, "_fetch_authenticated_login", lambda *a, **k: ("owner", ""))
    monkeypatch.setattr(executor, "_post_test_verdict_comment", lambda *a: ({"id": 77}, ""))
    monkeypatch.setattr(executor, "_readback_test_verdict_comment", lambda *a: (comment, ""))
    monkeypatch.setattr(executor, "_issue_metadata_marker_path", lambda *a: tmp_path / "marker.json")
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, *a, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result == {"failed": "test_verdict_postcondition_body_mismatch"}


def test_given_duplicate_marker_when_publish_then_fail_before_post(monkeypatch):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([{}, {}], ""))
    monkeypatch.setattr(
        executor, "_post_test_verdict_comment", lambda *a: (_ for _ in ()).throw(AssertionError("must not post"))
    )
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result == {"failed": "test_verdict_duplicate_marker_conflict_pre_mutation"}
