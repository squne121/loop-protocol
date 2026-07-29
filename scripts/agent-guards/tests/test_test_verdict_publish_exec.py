from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import zipfile
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


def make_receipt(executor, *, target_pr_number=42, linked_issue_number=1647, head="a" * 40):
    linked_body = "sha256:" + "b" * 64
    receipt = {
        "schema": "TEST_VERDICT_PRODUCER_RECEIPT_V1",
        "schema_version": 1,
        "execution_payload_sha256": "sha256:" + "e" * 64,
        "execution_artifact": {
            "artifact_id": 1,
            "artifact_url": "https://example.invalid/a",
            "artifact_archive_digest": "sha256:" + "d" * 64,
        },
        "producer": {
            "workflow_path": ".github/workflows/test-verdict-execution-record.yml",
            "workflow_source_ref": "refs/heads/main",
            "workflow_source_sha": "f" * 40,
            "workflow_run_id": 111,
            "workflow_run_attempt": 1,
            "job_id": 222,
            "check_run_id": 333,
        },
        "subject": {"target_pr_number": target_pr_number, "pr_head_sha": head, "head_repository_id": 1},
        "contract": {
            "linked_issue_number": linked_issue_number,
            "issue_body_sha256": linked_body,
            "command_manifest_sha256": "sha256:" + "c" * 64,
        },
        "source": {
            "repository_id": 1,
            "repository_full_name": "squne121/loop-protocol",
            "commit_sha": head,
            "tree_sha": "d" * 40,
            "execution_run_id": 111,
            "execution_job_id": 444,
        },
        "pass_eligible": True,
    }
    return receipt, linked_body


def valid_input(executor, *, target_pr_number=42, linked_issue_number=1647, head="a" * 40):
    receipt, linked_body = make_receipt(
        executor, target_pr_number=target_pr_number, linked_issue_number=linked_issue_number, head=head
    )
    receipt_sha = executor._canonical_sha256(receipt)
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
        "idempotency_key": (
            f"squne121/loop-protocol:{target_pr_number}:{linked_issue_number}:{head}:{receipt_sha}"
        ),
    }


def make_record(executor, per_ac=None):
    record = {
        "schema": "TEST_VERDICT_EXECUTION_RECORD_V1",
        "schema_version": 1,
        "producer": {"workflow_run_id": 111},
        "subject": {"target_pr_number": 42},
        "contract": {"linked_issue_number": 1647},
        "source": {
            "repository_id": 1,
            "repository_full_name": "squne121/loop-protocol",
            "commit_sha": "a" * 40,
            "tree_sha": "d" * 40,
            "execution_run_id": 111,
            "execution_job_id": 444,
        },
        "executions": [{"execution_id": "exec-1", "exit_code": 0, "status": "pass"}],
        "per_ac": per_ac if per_ac is not None else [{"ac": "AC1", "execution_ids": ["exec-1"]}],
        "pass_eligible": True,
    }
    digest = executor._canonical_sha256(record)
    record["payload_sha256"] = digest
    return record


def zip_bytes_for(record: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("execution-record.json", json.dumps(record))
    return buf.getvalue()


def patch_common(monkeypatch, executor, request, *, record=None):
    # First call = pre-mutation duplicate precheck (expect none yet). Any
    # subsequent call = the AC10 post-POST marker recheck (expect exactly
    # the one comment this transaction just posted).
    marker_calls = {"n": 0}

    def fake_marker_matches(*a):
        marker_calls["n"] += 1
        if marker_calls["n"] == 1:
            return [], ""
        return [{"id": 77}], ""

    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", fake_marker_matches)
    monkeypatch.setattr(executor, "_verify_producer_run_and_job", lambda *a, **k: "")
    monkeypatch.setattr(executor, "_verify_execution_artifact_metadata", lambda *a, **k: "")
    monkeypatch.setattr(
        executor, "_download_and_verify_artifact_archive", lambda *a, **k: (record or make_record(executor), "")
    )
    monkeypatch.setattr(executor, "_fetch_pr_head_sha", lambda *a, **k: (request["expected_head_sha"], ""))
    monkeypatch.setattr(
        executor, "_fetch_linked_issue_body_sha256", lambda *a, **k: (request["linked_issue_body_sha256"], "")
    )
    monkeypatch.setattr(executor, "_fetch_authenticated_login", lambda *a, **k: ("owner", ""))


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

    patch_common(monkeypatch, executor, request)

    posted_bodies = {}

    def _post(pr_number, repo, body, gh_bin, env):
        posted_bodies["body"] = body
        return {"id": 77}, ""

    def _readback(comment_id, repo, gh_bin, env):
        return {
            "id": 77,
            "html_url": "https://example.invalid/comment/77",
            "body": posted_bodies["body"],
            "user": {"login": "owner"},
        }, ""

    monkeypatch.setattr(executor, "_post_test_verdict_comment", _post)
    monkeypatch.setattr(executor, "_readback_test_verdict_comment", _readback)
    monkeypatch.setattr(executor, "_check_no_tracked_changes", lambda *a: [])
    monkeypatch.setattr(executor, "_issue_metadata_marker_path", lambda *a: tmp_path / "marker.json")
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, *a, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result["ok"]["comment_id"] == 77
    assert (tmp_path / "marker.json").exists()
    assert "TEST_VERDICT_MACHINE/v2" in posted_bodies["body"]
    assert marker in posted_bodies["body"]
    assert f"target_pr_number: {request['target_pr_number']}" in posted_bodies["body"]
    assert "per_ac_coverage" in posted_bodies["body"]


def test_given_pre_publish_head_drift_when_publish_then_post_is_not_attempted(monkeypatch):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([], ""))
    monkeypatch.setattr(executor, "_verify_producer_run_and_job", lambda *a, **k: "")
    monkeypatch.setattr(executor, "_verify_execution_artifact_metadata", lambda *a, **k: "")
    monkeypatch.setattr(
        executor, "_download_and_verify_artifact_archive", lambda *a, **k: (make_record(executor), "")
    )
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
    patch_common(monkeypatch, executor, request)
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


# ---------------------------------------------------------------------------
# Issue #1647 Scope Delta AC5: full-schema receipt validation
# ---------------------------------------------------------------------------


def test_given_receipt_missing_producer_field_when_validated_then_schema_rejects():
    executor, _ = load_executor()
    request = valid_input(executor)
    del request["producer_receipt"]["producer"]
    request["receipt_sha256"] = executor._canonical_sha256(request["producer_receipt"])
    request["idempotency_key"] = (
        f"{request['repo']}:{request['target_pr_number']}:{request['linked_issue_number']}:"
        f"{request['expected_head_sha']}:{request['receipt_sha256']}"
    )
    err = executor._validate_test_verdict_publish_fields(request, request["repo"], 1647)
    assert err.startswith("test_verdict_publish_receipt_schema_invalid")


def test_given_receipt_missing_execution_payload_sha256_when_validated_then_schema_rejects():
    executor, _ = load_executor()
    request = valid_input(executor)
    del request["producer_receipt"]["execution_payload_sha256"]
    request["receipt_sha256"] = executor._canonical_sha256(request["producer_receipt"])
    request["idempotency_key"] = (
        f"{request['repo']}:{request['target_pr_number']}:{request['linked_issue_number']}:"
        f"{request['expected_head_sha']}:{request['receipt_sha256']}"
    )
    err = executor._validate_test_verdict_publish_fields(request, request["repo"], 1647)
    assert err.startswith("test_verdict_publish_receipt_schema_invalid")


def test_given_receipt_has_unknown_field_when_validated_then_schema_rejects():
    executor, _ = load_executor()
    request = valid_input(executor)
    request["producer_receipt"]["unexpected_extra_field"] = "nope"
    request["receipt_sha256"] = executor._canonical_sha256(request["producer_receipt"])
    request["idempotency_key"] = (
        f"{request['repo']}:{request['target_pr_number']}:{request['linked_issue_number']}:"
        f"{request['expected_head_sha']}:{request['receipt_sha256']}"
    )
    err = executor._validate_test_verdict_publish_fields(request, request["repo"], 1647)
    assert err.startswith("test_verdict_publish_receipt_schema_invalid")


# ---------------------------------------------------------------------------
# Issue #1647 Scope Delta AC6/AC7: GitHub Actions live readback + artifact
# archive digest recomputation
# ---------------------------------------------------------------------------


def test_given_live_readback_of_workflow_run_fails_when_publish_then_fail_closed_before_post(monkeypatch):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([], ""))
    monkeypatch.setattr(
        executor,
        "_verify_producer_run_and_job",
        lambda *a, **k: "test_verdict_publish_receipt_workflow_run_head_sha_mismatch",
    )
    monkeypatch.setattr(
        executor, "_post_test_verdict_comment", lambda *a: (_ for _ in ()).throw(AssertionError("must not post"))
    )
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result["failed"].startswith("test_verdict_publish_receipt_live_readback_failed")


def test_verify_producer_run_and_job_checks_run_repo_job_and_check_run_linkage(monkeypatch):
    executor, _ = load_executor()
    receipt, _ = make_receipt(executor)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        path = argv[argv.index("--hostname") + 2]
        if path == "repos/squne121/loop-protocol/actions/runs/111/attempts/1":
            payload = {"head_sha": "a" * 40, "repository": {"full_name": "squne121/loop-protocol"}}
        elif path == "repos/squne121/loop-protocol/actions/jobs/222":
            payload = {
                "run_id": 111,
                "head_sha": "a" * 40,
                "check_run_url": "https://api.github.com/repos/squne121/loop-protocol/check-runs/333",
            }
        elif path == "repos/squne121/loop-protocol/check-runs/333":
            payload = {"id": 333, "head_sha": "a" * 40}
        else:
            raise AssertionError(f"unexpected path: {path}")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    err = executor._verify_producer_run_and_job(receipt, "squne121/loop-protocol", "a" * 40, "gh", {})
    assert err == ""
    assert len(calls) == 3


def test_verify_producer_run_and_job_detects_head_sha_mismatch(monkeypatch):
    executor, _ = load_executor()
    receipt, _ = make_receipt(executor)

    def fake_run(argv, **kwargs):
        payload = {"head_sha": "b" * 40, "repository": {"full_name": "squne121/loop-protocol"}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    err = executor._verify_producer_run_and_job(receipt, "squne121/loop-protocol", "a" * 40, "gh", {})
    assert err == "test_verdict_publish_receipt_workflow_run_head_sha_mismatch"


def test_download_and_verify_artifact_archive_recomputes_digest_and_extracts_per_ac(monkeypatch):
    executor, _ = load_executor()
    receipt, _ = make_receipt(executor)
    record = make_record(executor, per_ac=[{"ac": "AC5", "execution_ids": ["exec-1"]}])
    archive = zip_bytes_for(record)
    digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    receipt["execution_artifact"]["artifact_archive_digest"] = digest
    receipt["execution_payload_sha256"] = record["payload_sha256"]

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=archive, stderr=b"")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    result, err = executor._download_and_verify_artifact_archive(receipt, "squne121/loop-protocol", "gh", {})
    assert err == ""
    assert result["per_ac"] == [{"ac": "AC5", "execution_ids": ["exec-1"]}]


def test_download_and_verify_artifact_archive_rejects_digest_mismatch(monkeypatch):
    executor, _ = load_executor()
    receipt, _ = make_receipt(executor)
    record = make_record(executor)
    archive = zip_bytes_for(record)
    receipt["execution_artifact"]["artifact_archive_digest"] = "sha256:" + "0" * 64

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=archive, stderr=b"")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    result, err = executor._download_and_verify_artifact_archive(receipt, "squne121/loop-protocol", "gh", {})
    assert result is None
    assert err == "test_verdict_publish_receipt_artifact_archive_digest_mismatch"


def test_given_artifact_archive_digest_mismatch_when_publish_then_fail_closed_before_post(monkeypatch):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([], ""))
    monkeypatch.setattr(executor, "_verify_producer_run_and_job", lambda *a, **k: "")
    monkeypatch.setattr(executor, "_verify_execution_artifact_metadata", lambda *a, **k: "")
    monkeypatch.setattr(
        executor,
        "_download_and_verify_artifact_archive",
        lambda *a, **k: (None, "test_verdict_publish_receipt_artifact_archive_digest_mismatch"),
    )
    monkeypatch.setattr(
        executor, "_post_test_verdict_comment", lambda *a: (_ for _ in ()).throw(AssertionError("must not post"))
    )
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result["failed"].startswith("test_verdict_publish_receipt_live_readback_failed")


# ---------------------------------------------------------------------------
# Issue #1647 Scope Delta AC8: deterministic body / cross-check
# ---------------------------------------------------------------------------


def test_cross_check_test_verdict_body_detects_mismatch():
    executor, _ = load_executor()
    body = (
        "TEST_VERDICT_MACHINE/v2\n"
        "target_pr_number: 99\n"
        "linked_issue_number: 1647\n"
        f"head_sha: {'a' * 40}\n"
        "artifact_id: 1\n"
        "result: PASS\n"
    )
    err = executor._cross_check_test_verdict_body(
        body,
        target_pr_number=42,
        linked_issue_number=1647,
        expected_head_sha="a" * 40,
        artifact_id=1,
        pass_eligible=True,
        per_ac_coverage=[],
    )
    assert err == "test_verdict_publish_body_pr_number_mismatch"


def test_cross_check_test_verdict_body_detects_per_ac_coverage_mismatch():
    executor, _ = load_executor()
    body = (
        "TEST_VERDICT_MACHINE/v2\n"
        "target_pr_number: 42\n"
        "linked_issue_number: 1647\n"
        f"head_sha: {'a' * 40}\n"
        "artifact_id: 1\n"
        "result: PASS\n"
        "per_ac_coverage:\n"
        "  - ac: AC1\n"
    )
    err = executor._cross_check_test_verdict_body(
        body,
        target_pr_number=42,
        linked_issue_number=1647,
        expected_head_sha="a" * 40,
        artifact_id=1,
        pass_eligible=True,
        per_ac_coverage=[{"ac": "AC1"}, {"ac": "AC2"}],
    )
    assert err == "test_verdict_publish_body_per_ac_coverage_mismatch"


def test_render_test_verdict_body_passes_its_own_cross_check():
    executor, _ = load_executor()
    receipt, _ = make_receipt(executor)
    per_ac = [{"ac": "AC1"}, {"ac": "AC2"}]
    body = executor._render_test_verdict_body(receipt, 42, 1647, "a" * 40, 1, per_ac)
    err = executor._cross_check_test_verdict_body(
        body,
        target_pr_number=42,
        linked_issue_number=1647,
        expected_head_sha="a" * 40,
        artifact_id=1,
        pass_eligible=True,
        per_ac_coverage=per_ac,
    )
    assert err == ""


# ---------------------------------------------------------------------------
# Issue #1647 Scope Delta AC9: idempotent retry fresh readback drift detection
# ---------------------------------------------------------------------------


def test_given_retry_path_head_drifts_between_readbacks_when_publish_then_stale(monkeypatch, tmp_path):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    marker = executor._test_verdict_marker_str(request["idempotency_key"])
    comment = {
        "id": 77,
        "html_url": "https://example.invalid/comment/77",
        "body": None,
        "user": {"login": "owner"},
    }
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([{"id": 77}], ""))
    monkeypatch.setattr(executor, "_verify_producer_run_and_job", lambda *a, **k: "")
    monkeypatch.setattr(executor, "_verify_execution_artifact_metadata", lambda *a, **k: "")
    monkeypatch.setattr(
        executor, "_download_and_verify_artifact_archive", lambda *a, **k: (make_record(executor), "")
    )
    monkeypatch.setattr(executor, "_fetch_authenticated_login", lambda *a, **k: ("owner", ""))

    head_calls = {"n": 0}

    def fake_head(*a, **k):
        head_calls["n"] += 1
        # First call (pre-publish precondition) matches expected head; second
        # call (AC9 fresh re-check inside the retry branch) has drifted.
        if head_calls["n"] == 1:
            return request["expected_head_sha"], ""
        return "e" * 40, ""

    monkeypatch.setattr(executor, "_fetch_pr_head_sha", fake_head)
    monkeypatch.setattr(
        executor, "_fetch_linked_issue_body_sha256", lambda *a, **k: (request["linked_issue_body_sha256"], "")
    )

    def fake_readback(comment_id, repo, gh_bin, env):
        rendered_body = executor._render_test_verdict_body(
            request["producer_receipt"],
            request["target_pr_number"],
            request["linked_issue_number"],
            request["expected_head_sha"],
            1,
            [{"ac": "AC1"}],
        )
        comment["body"] = f"{rendered_body}\n\n{marker}\n"
        return comment, ""

    monkeypatch.setattr(executor, "_readback_test_verdict_comment", fake_readback)
    monkeypatch.setattr(executor, "_issue_metadata_marker_path", lambda *a: tmp_path / "marker.json")
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, *a, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result["failed"].startswith("published_but_stale")
    assert not (tmp_path / "marker.json").exists()


# ---------------------------------------------------------------------------
# Issue #1647 Scope Delta AC10: marker pre-embed rejection + post-POST
# marker recheck
# ---------------------------------------------------------------------------


def test_given_body_already_contains_marker_prefix_when_publish_then_rejected_before_post(monkeypatch):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    patch_common(monkeypatch, executor, request)
    original_render = executor._render_test_verdict_body

    def _forged_body(receipt, target_pr_number, linked_issue_number, expected_head_sha, artifact_id, per_ac_coverage):
        base = original_render(
            receipt, target_pr_number, linked_issue_number, expected_head_sha, artifact_id, per_ac_coverage
        )
        return f"{base}\n{executor.TEST_VERDICT_MARKER_PREFIX}forged -->"

    monkeypatch.setattr(executor, "_render_test_verdict_body", _forged_body)
    monkeypatch.setattr(
        executor, "_post_test_verdict_comment", lambda *a: (_ for _ in ()).throw(AssertionError("must not post"))
    )
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result == {"failed": "test_verdict_publish_marker_preembedded_in_body"}


def test_given_post_marker_recheck_finds_more_than_one_when_publish_then_conflicted(monkeypatch, tmp_path):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    call_state = {"n": 0}

    def fake_marker_matches(*a):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return [], ""
        return [{"id": 77}, {"id": 78}], ""

    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", fake_marker_matches)
    monkeypatch.setattr(executor, "_verify_producer_run_and_job", lambda *a, **k: "")
    monkeypatch.setattr(executor, "_verify_execution_artifact_metadata", lambda *a, **k: "")
    monkeypatch.setattr(
        executor, "_download_and_verify_artifact_archive", lambda *a, **k: (make_record(executor), "")
    )
    monkeypatch.setattr(executor, "_fetch_pr_head_sha", lambda *a, **k: (request["expected_head_sha"], ""))
    monkeypatch.setattr(
        executor, "_fetch_linked_issue_body_sha256", lambda *a, **k: (request["linked_issue_body_sha256"], "")
    )
    monkeypatch.setattr(executor, "_fetch_authenticated_login", lambda *a, **k: ("owner", ""))

    posted_bodies = {}

    def _post(pr_number, repo, body, gh_bin, env):
        posted_bodies["body"] = body
        return {"id": 77}, ""

    def _readback(comment_id, repo, gh_bin, env):
        return {
            "id": 77,
            "html_url": "https://example.invalid/comment/77",
            "body": posted_bodies["body"],
            "user": {"login": "owner"},
        }, ""

    monkeypatch.setattr(executor, "_post_test_verdict_comment", _post)
    monkeypatch.setattr(executor, "_readback_test_verdict_comment", _readback)
    monkeypatch.setattr(executor, "_issue_metadata_marker_path", lambda *a: tmp_path / "marker.json")
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, *a, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result["failed"].startswith("test_verdict_publish_post_marker_recheck_failed")
    assert (tmp_path / "marker.json").exists()


# ---------------------------------------------------------------------------
# Issue #1647 Scope Delta AC11: linked Issue must not be a pull request
# ---------------------------------------------------------------------------


def test_fetch_linked_issue_body_sha256_rejects_pull_request(monkeypatch):
    executor, _ = load_executor()

    def fake_run(argv, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"body": "hello", "isPullRequest": True}), stderr=""
        )

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    sha, err = executor._fetch_linked_issue_body_sha256(1647, "squne121/loop-protocol", "gh", {})
    assert sha is None
    assert err == "test_verdict_linked_issue_is_pull_request"


def test_fetch_linked_issue_body_sha256_accepts_regular_issue(monkeypatch):
    executor, _ = load_executor()

    def fake_run(argv, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"body": "hello", "isPullRequest": False}), stderr=""
        )

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    sha, err = executor._fetch_linked_issue_body_sha256(1647, "squne121/loop-protocol", "gh", {})
    assert err == ""
    assert sha == "sha256:" + hashlib.sha256(b"hello").hexdigest()


def test_given_linked_issue_is_pull_request_when_publish_then_fail_closed(monkeypatch):
    executor, policy = load_executor()
    request = valid_input(executor)
    args = SimpleNamespace(
        issue_number=1647, command_id=policy.COMMAND_ID_TEST_VERDICT_PUBLISH, repo=request["repo"], dry_run=False
    )
    monkeypatch.setattr(executor, "_find_test_verdict_marker_matches", lambda *a: ([], ""))
    monkeypatch.setattr(executor, "_verify_producer_run_and_job", lambda *a, **k: "")
    monkeypatch.setattr(executor, "_verify_execution_artifact_metadata", lambda *a, **k: "")
    monkeypatch.setattr(
        executor, "_download_and_verify_artifact_archive", lambda *a, **k: (make_record(executor), "")
    )
    monkeypatch.setattr(executor, "_fetch_pr_head_sha", lambda *a, **k: (request["expected_head_sha"], ""))
    monkeypatch.setattr(
        executor, "_fetch_linked_issue_body_sha256", lambda *a, **k: (None, "test_verdict_linked_issue_is_pull_request")
    )
    monkeypatch.setattr(
        executor, "_post_test_verdict_comment", lambda *a: (_ for _ in ()).throw(AssertionError("must not post"))
    )
    result = executor._run_test_verdict_publish(
        args, request, "gh", lambda reason, **k: {"failed": reason}, lambda extra: {"ok": extra}
    )
    assert result == {"failed": "test_verdict_linked_issue_is_pull_request"}
