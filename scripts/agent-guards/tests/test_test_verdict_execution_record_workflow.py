# Contract tests for the protected execution-record producer.
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/test-verdict-execution-record.yml"
PRODUCER = ROOT / "scripts/agent-ops/test_verdict_execution_record_producer.py"


def load_producer():
    spec = importlib.util.spec_from_file_location("producer", PRODUCER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_workflow_dict():
    return yaml.safe_load(WORKFLOW.read_text())


def resolved_manifest(producer):
    return producer.resolve_manifest_entry("uv.pytest.execution-record")


def source_fixture(
    commit_sha="b" * 40,
    tree_sha="d" * 40,
    repository_id=1,
    repository_full_name="owner/repo",
    run_id=1,
    job_id=2,
):
    return {
        "repository_id": repository_id,
        "repository_full_name": repository_full_name,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "execution_run_id": run_id,
        "execution_job_id": job_id,
    }


def source_core(source):
    return {k: source[k] for k in ("repository_id", "repository_full_name", "commit_sha", "tree_sha")}


def execution_result(
    producer,
    execution_id="exec-uv-pytest-execution-record",
    exit_code=0,
    status="pass",
    skipped=False,
    fallback_detected=False,
    timed_out=False,
):
    manifest = resolved_manifest(producer)
    return {
        "execution_id": execution_id,
        "command_id": "uv.pytest.execution-record",
        "argv_sha256": manifest["manifest_sha256"],
        "exit_code": exit_code,
        "status": status,
        "skipped": skipped,
        "fallback_detected": fallback_detected,
        "timed_out": timed_out,
        "stdout_sha256": "sha256:" + ("a" * 64),
        "stderr_sha256": "sha256:" + ("b" * 64),
    }


def build_record_from_fixture(producer, data, result, source=None):
    return producer.build_record_for_command(
        data["producer"],
        data["subject"],
        data["contract"],
        "uv.pytest.execution-record",
        result,
        source if source is not None else source_fixture(commit_sha=data["subject"]["pr_head_sha"]),
    )

def identity_fixture():
    return {
        "producer": {
            "workflow_path": ".github/workflows/test-verdict-execution-record.yml",
            "workflow_source_ref": "refs/heads/main",
            "workflow_source_sha": "a" * 40,
            "workflow_run_id": 1,
            "workflow_run_attempt": 1,
            "job_id": 2,
            "check_run_id": 3,
        },
        "subject": {
            "target_pr_number": 1640,
            "head_repository_id": 1,
            "pr_head_sha": "b" * 40,
        },
        "contract": {
            "linked_issue_number": 1646,
            "issue_body_sha256": "sha256:" + "c" * 64,
        },
    }


def matching_execution_artifact(record, artifact_id=42, name="n", head_sha="f" * 40):
    digest = "sha256:" + "e" * 64
    return {
        "artifact_id": artifact_id,
        "artifact_url": "https://example.invalid/artifact",
        "artifact_archive_digest": digest,
        "rest": {
            "id": artifact_id,
            "name": name,
            "expired": False,
            "digest": digest,
            "repository_id": 1,
            "head_repository_id": 1,
            "workflow_run_id": 1,
            "head_sha": head_sha,
        },
        "expected": {
            "repository_id": 1,
            "head_repository_id": 1,
            "workflow_run_id": 1,
            "head_sha": head_sha,
            "name": name,
        },
        "downloaded_payload_sha256": record["payload_sha256"],
    }


def test_given_closed_manifest_when_rendered_then_no_issue_shell_is_executed():
    text = WORKFLOW.read_text()
    assert "workflow_dispatch:" in text and "github.event.repository.default_branch" in text
    assert "eval" not in text and "bash -c" not in text
    assert "COMMAND_MANIFEST" in PRODUCER.read_text()
    assert "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10" in text


def test_given_workflow_when_rendered_then_setup_python_uv_is_used_before_uv_commands():
    import operator
    text = WORKFLOW.read_text()
    setup_index = text.find("setup-python-uv")
    first_uv_run_index = text.find("uv run --locked")
    assert setup_index != -1
    assert first_uv_run_index != -1
    assert operator.lt(setup_index, first_uv_run_index)


def test_given_workflow_when_rendered_then_upload_artifact_pin_supports_digest_output():
    text = WORKFLOW.read_text()
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in text
    assert "actions/upload-artifact@65462800fd760344b1a7b4382951275a0abb4808" not in text


def test_given_workflow_when_rendered_then_gh_api_calls_go_through_producer_gh_api_subcommand():
    import operator
    text = WORKFLOW.read_text()
    assert "gh api" not in text
    assert operator.ge(text.count("producer.py gh-api"), 2)


def test_given_bare_hex_digest_when_normalized_then_prefix_is_added():
    producer = load_producer()
    digest = producer.normalize_artifact_digest("a" * 64)
    assert digest == "sha256:" + ("a" * 64)


def test_given_prefixed_digest_when_normalized_then_prefix_is_not_duplicated():
    producer = load_producer()
    digest = producer.normalize_artifact_digest("sha256:" + ("a" * 64))
    assert digest == "sha256:" + ("a" * 64)


def test_given_malformed_digest_when_normalized_then_value_error_is_raised():
    producer = load_producer()
    raised = False
    try:
        producer.normalize_artifact_digest("not-a-digest")
    except ValueError:
        raised = True
    assert raised


def test_given_body_text_when_hashed_then_matches_utf8_no_trailing_newline():
    producer = load_producer()
    import hashlib
    body = "issue body without added newline"
    expected = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert producer.sha256_of(body) == expected


def test_given_unknown_command_id_when_resolved_then_key_error_is_raised():
    producer = load_producer()
    raised = False
    try:
        producer.resolve_manifest_entry("not-in-allowlist")
    except KeyError:
        raised = True
    assert raised


def test_given_command_manifest_when_resolved_then_covers_all_five_issue_1646_acs():
    producer = load_producer()
    resolved = resolved_manifest(producer)
    assert resolved["covers_acs"] == ["AC1", "AC2", "AC3", "AC4", "AC5"]


def test_given_command_manifest_argv_when_resolved_then_source_root_placeholder_is_present_and_unsubstituted():
    # Issue 1711 AC3: resolve_manifest_entry never substitutes the
    # source_root placeholder, so manifest_sha256 is a pure function of
    # COMMAND_MANIFEST content, independent of runtime directory layout.
    producer = load_producer()
    resolved = resolved_manifest(producer)
    assert any("{source_root}" in part for part in resolved["argv"])
    first = producer.resolve_manifest_entry("uv.pytest.execution-record")["manifest_sha256"]
    second = producer.resolve_manifest_entry("uv.pytest.execution-record")["manifest_sha256"]
    assert first == second


def test_given_execute_command_when_run_then_source_root_is_substituted_into_runtime_argv(monkeypatch, tmp_path):
    # AC3: execute_command must run pytest against the *source* checkout's
    # test file, not the harness's own -- captured here by asserting the
    # actual subprocess.run argv contains the resolved absolute source_root.
    producer = load_producer()
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = b"1 passed"
        stderr = b""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return FakeCompleted()

    monkeypatch.setattr(producer.subprocess, "run", fake_run)
    repo_root = tmp_path / "harness"
    source_root = tmp_path / "source"
    repo_root.mkdir()
    source_root.mkdir()
    result = producer.execute_command("uv.pytest.execution-record", repo_root, source_root)
    assert result["status"] == "pass"
    resolved_source_root = str(source_root.resolve())
    assert any(resolved_source_root in part for part in captured["argv"])
    assert not any("{source_root}" in part for part in captured["argv"])
    assert captured["cwd"] == repo_root


def test_given_execution_result_with_wrong_command_id_when_build_record_for_command_then_value_error():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    result["command_id"] = "a-different-command-id"
    raised = False
    try:
        build_record_from_fixture(producer, data, result)
    except ValueError:
        raised = True
    assert raised


def test_given_execution_result_with_tampered_argv_sha256_when_build_record_for_command_then_value_error():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    result["argv_sha256"] = "sha256:" + ("0" * 64)
    raised = False
    try:
        build_record_from_fixture(producer, data, result)
    except ValueError:
        raised = True
    assert raised


def test_given_matching_readbacks_when_built_then_record_and_receipt_are_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    assert record["pass_eligible"]
    ea = matching_execution_artifact(record)

    # AC2 chain binding: assemble-contract -> build-record -> final
    # assemble-contract -> build-receipt --schema all bind to one
    # independently-recomputed command_manifest_sha256. The final contract is
    # produced by an independent call to resolve_manifest_entry (mirroring a
    # second, separate assemble-contract CLI invocation), not copied from the
    # record.
    manifest = resolved_manifest(producer)
    final_contract = dict(data["contract"])
    final_contract["command_manifest_sha256"] = producer.resolve_manifest_entry(
        "uv.pytest.execution-record"
    )["manifest_sha256"]
    assert final_contract["command_manifest_sha256"] == record["contract"]["command_manifest_sha256"]

    receipt = producer.build_receipt(
        record, ea, record["subject"], final_contract, manifest["manifest_sha256"], source
    )
    assert receipt["pass_eligible"]
    assert record["payload_sha256"] != receipt["execution_artifact"]["artifact_archive_digest"]
    assert receipt["contract"]["command_manifest_sha256"] == manifest["manifest_sha256"]
    assert receipt["source"] == source


def test_given_drift_or_missing_coverage_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer, timed_out=True)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    assert not record["pass_eligible"]
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    receipt = producer.build_receipt(
        record,
        ea,
        record["subject"],
        record["contract"],
        manifest["manifest_sha256"],
        source,
    )
    assert not receipt["pass_eligible"]


def test_given_identity_drift_after_upload_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    assert record["pass_eligible"]
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    drifted_subject = dict(record["subject"], pr_head_sha="f" * 40)
    receipt = producer.build_receipt(
        record,
        ea,
        drifted_subject,
        record["contract"],
        manifest["manifest_sha256"],
        source,
    )
    assert not receipt["pass_eligible"]


def test_given_artifact_digest_missing_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    assert record["pass_eligible"]
    manifest = resolved_manifest(producer)
    receipt = producer.build_receipt(
        record,
        {},
        record["subject"],
        record["contract"],
        manifest["manifest_sha256"],
        source,
    )
    assert not receipt["pass_eligible"]


def test_given_artifact_id_mismatch_when_receipt_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    assert record["pass_eligible"]
    ea = matching_execution_artifact(record)
    ea["artifact_id"] = 999999
    manifest = resolved_manifest(producer)
    receipt = producer.build_receipt(
        record,
        ea,
        record["subject"],
        record["contract"],
        manifest["manifest_sha256"],
        source,
    )
    assert not receipt["pass_eligible"]


def test_given_artifact_digest_mismatch_when_receipt_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    assert record["pass_eligible"]
    ea = matching_execution_artifact(record)
    ea["artifact_archive_digest"] = "sha256:" + ("9" * 64)
    manifest = resolved_manifest(producer)
    receipt = producer.build_receipt(
        record,
        ea,
        record["subject"],
        record["contract"],
        manifest["manifest_sha256"],
        source,
    )
    assert not receipt["pass_eligible"]


def test_given_schema_files_when_loaded_then_required_identity_fields_exist():
    for filename in ("test-verdict-execution-record.schema.json", "test-verdict-producer-receipt.schema.json"):
        schema = json.loads((ROOT / "schemas" / filename).read_text())
        assert "pass_eligible" in schema["required"]
        assert "source" in schema["required"]
        assert schema["additionalProperties"] is False
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        source_schema = schema["properties"]["source"]
        assert source_schema["additionalProperties"] is False
        for field in (
            "repository_id",
            "repository_full_name",
            "commit_sha",
            "tree_sha",
            "execution_run_id",
            "execution_job_id",
        ):
            assert field in source_schema["required"]


def test_given_schema_files_when_validated_against_matching_record_then_no_errors():
    from jsonschema import Draft202012Validator

    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    record_schema = json.loads((ROOT / "schemas" / "test-verdict-execution-record.schema.json").read_text())
    Draft202012Validator.check_schema(record_schema)
    errors = list(Draft202012Validator(record_schema).iter_errors(record))
    assert not errors

    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    receipt = producer.build_receipt(
        record,
        ea,
        record["subject"],
        record["contract"],
        manifest["manifest_sha256"],
        source,
    )
    receipt_schema = json.loads((ROOT / "schemas" / "test-verdict-producer-receipt.schema.json").read_text())
    Draft202012Validator.check_schema(receipt_schema)
    errors = list(Draft202012Validator(receipt_schema).iter_errors(receipt))
    assert not errors


def test_given_receipt_input_missing_when_producer_invoked_then_process_fails_closed():
    import subprocess
    import sys
    import tempfile

    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        producer_input = tmp / "producer-input.json"
        subject_input = tmp / "subject-input.json"
        contract_input = tmp / "contract-input.json"
        execution_result_file = tmp / "execution-result.json"
        source_input = tmp / "source-input.json"
        producer_input.write_text(json.dumps(data["producer"]))
        subject_input.write_text(json.dumps(data["subject"]))
        contract_input.write_text(json.dumps(data["contract"]))
        execution_result_file.write_text(json.dumps(result))
        source_input.write_text(json.dumps(source))
        missing_receipt_input = tmp / "execution-artifact-input.json"
        record_output = tmp / "record-output.json"
        producer_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-record",
             "--command-id", "uv.pytest.execution-record",
             "--producer-input", str(producer_input),
             "--subject-input", str(subject_input),
             "--contract-input", str(contract_input),
             "--execution-result", str(execution_result_file),
             "--source-input", str(source_input),
             "--output", str(record_output)],
            capture_output=True,
            text=True,
        )
        assert producer_result.returncode == 0
        assert record_output.exists()

        receipt_output = tmp / "receipt-output.json"
        final_subject = tmp / "final-subject.json"
        final_contract = tmp / "final-contract.json"
        final_source = tmp / "final-source.json"
        final_subject.write_text(json.dumps(data["subject"]))
        final_contract.write_text(json.dumps(data["contract"]))
        final_source.write_text(json.dumps(source))
        receipt_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-receipt",
             "--record", str(record_output),
             "--execution-artifact-input", str(missing_receipt_input),
             "--final-subject", str(final_subject),
             "--final-contract", str(final_contract),
             "--final-source", str(final_source),
             "--command-id", "uv.pytest.execution-record",
             "--output", str(receipt_output)],
            capture_output=True,
            text=True,
        )
        assert receipt_result.returncode != 0
        assert not receipt_output.exists()


# --- Issue 1711: command_manifest_sha256 propagation regression fix ---


def _final_contract_with_manifest(producer, data):
    final_contract = dict(data["contract"])
    final_contract["command_manifest_sha256"] = producer.resolve_manifest_entry(
        "uv.pytest.execution-record"
    )["manifest_sha256"]
    return final_contract


def test_given_independent_manifest_recompute_when_assembled_twice_then_both_recomputes_agree():
    # AC1: two independent CLI-equivalent recomputes of the same command_id
    # against COMMAND_MANIFEST must agree bit-for-bit, without either call
    # reading the other's output file.
    producer = load_producer()
    first = producer.resolve_manifest_entry("uv.pytest.execution-record")["manifest_sha256"]
    second = producer.resolve_manifest_entry("uv.pytest.execution-record")["manifest_sha256"]
    assert first == second
    assert first.startswith("sha256:")


def test_given_final_contract_is_self_copy_of_pre_execution_contract_when_build_receipt_then_rejected():
    # Regression guard: final_contract_self_copy. Reusing the pre-execution
    # contract (missing command_manifest_sha256, since only build-record's
    # command-id-bound recompute adds it) as the "final" contract must be
    # fail-closed rejected, not silently accepted as stable.
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    self_copied_final_contract = dict(data["contract"])  # pre-execution shape, no manifest sha
    raised = False
    try:
        producer.build_receipt(
            record,
            ea,
            record["subject"],
            self_copied_final_contract,
            manifest["manifest_sha256"],
            source,
        )
    except ValueError as exc:
        raised = True
        assert "command_manifest_sha256_missing" in str(exc)
    assert raised


def test_given_malformed_command_manifest_sha256_when_build_receipt_then_rejected():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    final_contract = _final_contract_with_manifest(producer, data)
    final_contract["command_manifest_sha256"] = "not-a-sha256"
    raised = False
    try:
        producer.build_receipt(
            record, ea, record["subject"], final_contract, manifest["manifest_sha256"], source
        )
    except ValueError as exc:
        raised = True
        assert "command_manifest_sha256_malformed" in str(exc)
    assert raised


def test_given_tampered_command_manifest_sha256_when_build_receipt_then_rejected():
    # record and final_contract disagree with each other -> tampered.
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    final_contract = _final_contract_with_manifest(producer, data)
    final_contract["command_manifest_sha256"] = "sha256:" + ("9" * 64)
    raised = False
    try:
        producer.build_receipt(
            record, ea, record["subject"], final_contract, manifest["manifest_sha256"], source
        )
    except ValueError as exc:
        raised = True
        assert "command_manifest_sha256_tampered" in str(exc)
    assert raised


def test_given_self_attested_command_manifest_sha256_when_build_receipt_then_rejected():
    # record and final_contract agree with each other but disagree with the
    # independently recomputed expected value -> self-attested / stale.
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    record["contract"]["command_manifest_sha256"] = "sha256:" + ("7" * 64)
    ea = matching_execution_artifact(record)
    final_contract = dict(data["contract"])
    final_contract["command_manifest_sha256"] = "sha256:" + ("7" * 64)
    expected_manifest_sha256 = producer.resolve_manifest_entry("uv.pytest.execution-record")["manifest_sha256"]
    raised = False
    try:
        producer.build_receipt(
            record, ea, record["subject"], final_contract, expected_manifest_sha256, source
        )
    except ValueError as exc:
        raised = True
        assert "command_manifest_sha256_self_attested" in str(exc)
    assert raised


def test_given_build_receipt_cli_with_missing_manifest_when_invoked_then_failure_reason_category_printed():
    import subprocess
    import sys
    import tempfile

    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        producer_input = tmp / "producer-input.json"
        subject_input = tmp / "subject-input.json"
        contract_input = tmp / "contract-input.json"
        execution_result_file = tmp / "execution-result.json"
        source_input = tmp / "source-input.json"
        producer_input.write_text(json.dumps(data["producer"]))
        subject_input.write_text(json.dumps(data["subject"]))
        contract_input.write_text(json.dumps(data["contract"]))
        execution_result_file.write_text(json.dumps(result))
        source_input.write_text(json.dumps(source))
        record_output = tmp / "record-output.json"
        producer_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-record",
             "--command-id", "uv.pytest.execution-record",
             "--producer-input", str(producer_input),
             "--subject-input", str(subject_input),
             "--contract-input", str(contract_input),
             "--execution-result", str(execution_result_file),
             "--source-input", str(source_input),
             "--output", str(record_output)],
            capture_output=True,
            text=True,
        )
        assert producer_result.returncode == 0

        execution_artifact_input = tmp / "execution-artifact-input.json"
        record_data = json.loads(record_output.read_text())
        ea = matching_execution_artifact(record_data)
        execution_artifact_input.write_text(json.dumps(ea))

        # final-contract deliberately mirrors the pre-execution shape, i.e.
        # missing command_manifest_sha256 (self-copy without independent
        # recompute) -- must be rejected with a printed reason_category, and
        # no receipt-output.json must be written.
        final_subject = tmp / "final-subject.json"
        final_contract = tmp / "final-contract.json"
        final_source = tmp / "final-source.json"
        final_subject.write_text(json.dumps(data["subject"]))
        final_contract.write_text(json.dumps(data["contract"]))
        final_source.write_text(json.dumps(source))
        receipt_output = tmp / "receipt-output.json"
        receipt_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-receipt",
             "--record", str(record_output),
             "--execution-artifact-input", str(execution_artifact_input),
             "--final-subject", str(final_subject),
             "--final-contract", str(final_contract),
             "--final-source", str(final_source),
             "--command-id", "uv.pytest.execution-record",
             "--output", str(receipt_output)],
            capture_output=True,
            text=True,
        )
        assert receipt_result.returncode == 1
        assert "reason_category=command_manifest_sha256_missing" in receipt_result.stdout
        assert not receipt_output.exists()


def test_given_workflow_when_rendered_then_legacy_failure_shape_is_preserved_on_receipt_build_failure():
    # AC5: a build-record / build-receipt step failure must be diagnosed
    # directly (machine-readable reason_category, explicit exit 1) before the
    # subsequent upload-artifact step ever runs, so it is never masked as a
    # generic "no files found" upload error.
    text = WORKFLOW.read_text()
    assert "steps.build-execution-record.outcome == 'failure'" in text
    assert "steps.build-producer-receipt.outcome == 'failure'" in text
    build_record_index = text.find("id: build-execution-record")
    diagnose_record_index = text.find("Diagnose execution-record build failure")
    upload_record_index = text.find("Upload execution record")
    assert build_record_index != -1
    assert diagnose_record_index != -1
    assert upload_record_index != -1
    assert build_record_index < diagnose_record_index < upload_record_index

    build_receipt_index = text.find("id: build-producer-receipt")
    diagnose_receipt_index = text.find("Diagnose producer-receipt build failure")
    upload_receipt_index = text.find("Upload producer receipt")
    assert build_receipt_index < diagnose_receipt_index < upload_receipt_index


def test_given_build_record_cli_with_bad_input_json_when_invoked_then_reason_category_printed():
    # AC5 Medium: input_json_invalid is distinguished from a generic
    # traceback / schema_validation_failed.
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        broken = tmp / "broken.json"
        broken.write_text("{not valid json")
        valid = tmp / "valid.json"
        valid.write_text("{}")
        record_output = tmp / "record-output.json"
        result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-record",
             "--command-id", "uv.pytest.execution-record",
             "--producer-input", str(broken),
             "--subject-input", str(valid),
             "--contract-input", str(valid),
             "--execution-result", str(valid),
             "--source-input", str(valid),
             "--output", str(record_output)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "reason_category=input_json_invalid" in result.stdout
        assert not record_output.exists()


def test_given_workflow_yaml_when_parsed_then_trusted_and_untrusted_jobs_are_structurally_separated():
    # AC3/AC4: parse the actual workflow YAML (not just substring search) and
    # confirm the three-job trust boundary: trusted-preflight (no source
    # checkout), untrusted-execution (needs trusted-preflight, no secrets),
    # produce (needs both, no source checkout).
    workflow = load_workflow_dict()
    jobs = workflow["jobs"]
    assert "trusted-preflight" in jobs
    assert "untrusted-execution" in jobs
    assert "produce" in jobs
    assert jobs["untrusted-execution"]["needs"] == "trusted-preflight"
    produce_needs = jobs["produce"]["needs"]
    assert set(produce_needs) == {"trusted-preflight", "untrusted-execution"}


def test_given_untrusted_execution_job_when_parsed_then_no_secrets_oidc_or_write_token_present():
    # AC4: structural (not string-substring) check that the untrusted job's
    # step list never references secrets./github.token/id-token, and its
    # permissions block is scoped to contents: read only.
    workflow = load_workflow_dict()
    job = workflow["jobs"]["untrusted-execution"]
    assert job.get("permissions") == {"contents": "read"}
    assert "env" not in job
    rendered = yaml.safe_dump(job)
    assert "secrets." not in rendered
    assert "github.token" not in rendered
    assert "id-token" not in rendered
    assert "GH_TOKEN" not in rendered


def test_given_untrusted_execution_job_when_parsed_then_source_and_harness_are_separate_checkouts():
    # Issue 1711 Blocker 1/Blocker 2: the untrusted-execution job must check
    # out the target PR source and the trusted harness into two distinct
    # paths, and every "uses:"/local-action reference in that job must come
    # from the harness path, never from the bare "./" (source) checkout.
    workflow = load_workflow_dict()
    job = workflow["jobs"]["untrusted-execution"]
    checkout_steps = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    paths = {s["with"].get("path") for s in checkout_steps}
    assert paths == {"harness", "source"}
    local_action_steps = [s for s in job["steps"] if str(s.get("uses", "")).startswith("./")]
    assert local_action_steps, "expected at least one local action reference"
    for step in local_action_steps:
        assert step["uses"].startswith("./harness/"), step["uses"]


def test_given_pr_controlled_setup_action_when_source_checkout_modifies_it_then_it_is_never_invoked():
    # Required test 1: even if the target PR rewrites
    # .github/actions/setup-python-uv/action.yml inside its own source
    # checkout, the untrusted-execution job never references
    # ./source/.github/actions/setup-python-uv -- only ./harness/... is used.
    workflow = load_workflow_dict()
    job = workflow["jobs"]["untrusted-execution"]
    rendered = yaml.safe_dump(job)
    assert "./source/.github/actions" not in rendered
    assert "./.github/actions" not in rendered


def test_given_source_checkout_ref_when_resolved_then_it_is_bound_to_trusted_preflight_output_not_raw_input():
    # Issue 1711 Blocker 4: the untrusted-execution job's source checkout ref
    # must be the trusted-preflight job's independently-resolved commit sha
    # output, never the raw dispatcher input directly.
    workflow = load_workflow_dict()
    job = workflow["jobs"]["untrusted-execution"]
    checkout_steps = [
        s for s in job["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout@") and s.get("with", {}).get("path") == "source"
    ]
    assert len(checkout_steps) == 1
    ref = checkout_steps[0]["with"]["ref"]
    assert ref == "${{ needs.trusted-preflight.outputs.head_commit_sha }}"
    assert "inputs.expected_head_sha" not in ref


def test_given_trusted_preflight_job_when_parsed_then_it_never_checks_out_target_pr_source():
    # Issue 1711 Blocker 1/Blocker 4: trusted-preflight only checks out
    # github.sha (the harness / default branch), never a PR-supplied ref.
    workflow = load_workflow_dict()
    job = workflow["jobs"]["trusted-preflight"]
    checkout_steps = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkout_steps) == 1
    assert checkout_steps[0]["with"]["ref"] == "${{ github.sha }}"


def test_given_trusted_preflight_job_when_parsed_then_tree_sha_comes_from_rest_commit_lookup():
    # Issue 1711 Blocker 3: tree_sha must be derived from a trusted REST
    # "GET commit" call (repos/.../commits/{sha}), never from a checkout of
    # PR-controlled code (no `git rev-parse` / assemble-source-provenance
    # inside the untrusted job).
    workflow = load_workflow_dict()
    job = workflow["jobs"]["trusted-preflight"]
    rendered = yaml.safe_dump(job)
    assert "/commits/" in rendered
    assert "resolve-trusted-source" in rendered
    untrusted_job = workflow["jobs"]["untrusted-execution"]
    untrusted_rendered = yaml.safe_dump(untrusted_job)
    assert "resolve-trusted-source" not in untrusted_rendered
    assert "assemble-source-provenance" not in untrusted_rendered


def test_resolve_trusted_source_rejects_commit_sha_not_matching_expected():
    # Required test: if the trusted REST-resolved tree sha's commit_sha does
    # not match the dispatcher-supplied expected_head_sha, this is rejected
    # closed (independent of anything the target PR's own code claims).
    producer = load_producer()
    subject_data = {"head": {"sha": "a" * 40, "repo": {"id": 1, "full_name": "owner/repo"}}}
    commit_data = {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
    raised = False
    try:
        producer.resolve_trusted_source(subject_data, commit_data, "owner/repo", "c" * 40)
    except ValueError as exc:
        raised = True
        assert "trusted_source_commit_sha_mismatch" in str(exc)
    assert raised


def test_resolve_trusted_source_rejects_repository_full_name_mismatch():
    producer = load_producer()
    subject_data = {"head": {"sha": "a" * 40, "repo": {"id": 1, "full_name": "attacker/repo"}}}
    commit_data = {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
    raised = False
    try:
        producer.resolve_trusted_source(subject_data, commit_data, "owner/repo", "a" * 40)
    except ValueError as exc:
        raised = True
        assert "trusted_source_repository_mismatch" in str(exc)
    assert raised


def test_resolve_trusted_source_accepts_well_formed_matching_input():
    producer = load_producer()
    subject_data = {"head": {"sha": "a" * 40, "repo": {"id": 1, "full_name": "owner/repo"}}}
    commit_data = {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
    provenance = producer.resolve_trusted_source(subject_data, commit_data, "owner/repo", "a" * 40)
    assert provenance == {
        "repository_id": 1,
        "repository_full_name": "owner/repo",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }


def test_given_trusted_tree_sha_differs_from_artifact_tree_sha_when_receipt_built_then_rejected():
    # Required test 4: if the trusted-API-resolved tree_sha at receipt-build
    # time differs from the one bound into the record (source_tampered), the
    # receipt build is rejected closed.
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"], tree_sha="d" * 40)
    record = build_record_from_fixture(producer, data, result, source=source)
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    drifted_final_source = dict(source, tree_sha="9" * 40)
    raised = False
    try:
        producer.build_receipt(
            record, ea, record["subject"], record["contract"], manifest["manifest_sha256"], drifted_final_source
        )
    except ValueError as exc:
        raised = True
        assert "source_tampered" in str(exc)
    assert raised


def test_given_source_repository_id_missing_when_receipt_built_then_rejected():
    # Required test 5: source repository ID absence/mismatch is fail-closed.
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    source = source_fixture(commit_sha=data["subject"]["pr_head_sha"])
    record = build_record_from_fixture(producer, data, result, source=source)
    del record["source"]["repository_id"]
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    raised = False
    try:
        producer.build_receipt(record, ea, record["subject"], record["contract"], manifest["manifest_sha256"], source)
    except ValueError as exc:
        raised = True
        assert "source_missing" in str(exc)
    assert raised


def test_given_source_missing_entirely_when_receipt_built_then_rejected():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    record = build_record_from_fixture(producer, data, result, source=None)
    record["source"] = None
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    raised = False
    try:
        producer.build_receipt(record, ea, record["subject"], record["contract"], manifest["manifest_sha256"], None)
    except ValueError as exc:
        raised = True
        assert "source_missing" in str(exc)
    assert raised


def test_build_receipt_cli_requires_command_id_argument():
    # Required test 6: --command-id is required at the CLI level; omitting
    # it must fail closed at argparse time (exit code 2), not silently
    # default to a fail-open manifest binding skip.
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        placeholder = tmp / "placeholder.json"
        placeholder.write_text("{}")
        result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-receipt",
             "--record", str(placeholder),
             "--execution-artifact-input", str(placeholder),
             "--final-subject", str(placeholder),
             "--final-contract", str(placeholder),
             "--final-source", str(placeholder),
             "--output", str(tmp / "out.json")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "--command-id" in result.stderr
        assert not (tmp / "out.json").exists()


def test_given_trusted_preflight_job_fails_then_untrusted_execution_job_never_runs():
    # Required test 7: DAG-level guarantee -- untrusted-execution job's
    # `needs` on trusted-preflight means GitHub Actions will not start it if
    # trusted-preflight fails (default job failure propagation, no
    # `continue-on-error` / `if: always()` override on this dependency).
    workflow = load_workflow_dict()
    job = workflow["jobs"]["untrusted-execution"]
    assert job["needs"] == "trusted-preflight"
    assert job.get("if") != "always()"
    trusted_preflight = workflow["jobs"]["trusted-preflight"]
    for step in trusted_preflight["steps"]:
        assert step.get("continue-on-error") is not True


def test_given_source_execution_job_id_when_resolved_then_bound_to_untrusted_execution_job_name():
    # Required test 8 (partial, static): the trusted receipt job resolves
    # the untrusted-execution job's id by exact job name match, and binds it
    # into source.execution_job_id -- never self-attested from the untrusted
    # artifact.
    workflow = load_workflow_dict()
    produce_job = workflow["jobs"]["produce"]
    rendered = yaml.safe_dump(produce_job)
    assert "resolve-job-id-by-name" in rendered
    assert "untrusted-execution" in rendered
    assert "assemble-source" in rendered


def test_resolve_job_id_by_name_matches_exactly_one_job():
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        jobs_file = tmp / "jobs.json"
        jobs_file.write_text(json.dumps({
            "jobs": [
                {"id": 111, "name": "trusted-preflight"},
                {"id": 222, "name": "untrusted-execution"},
                {"id": 333, "name": "produce"},
            ]
        }))
        output = tmp / "out.json"
        result = subprocess.run(
            [sys.executable, str(PRODUCER), "resolve-job-id-by-name",
             "--jobs-file", str(jobs_file), "--job-name", "untrusted-execution", "--output", str(output)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert json.loads(output.read_text())["job_id"] == 222


def test_resolve_job_id_by_name_rejects_zero_or_multiple_matches():
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        jobs_file = tmp / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": [{"id": 111, "name": "other"}]}))
        output = tmp / "out.json"
        result = subprocess.run(
            [sys.executable, str(PRODUCER), "resolve-job-id-by-name",
             "--jobs-file", str(jobs_file), "--job-name", "untrusted-execution", "--output", str(output)],
            capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "reason_category=source_execution_job_lookup_failed" in result.stdout
        assert not output.exists()


def test_assert_source_tree_matches_rejects_mismatched_checkout():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(PRODUCER), "assert-source-tree-matches",
         "--actual-commit-sha", "a" * 40, "--actual-tree-sha", "b" * 40,
         "--expected-commit-sha", "a" * 40, "--expected-tree-sha", "c" * 40],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "reason_category=source_tree_checkout_mismatch" in result.stdout


def test_assert_source_tree_matches_accepts_matching_checkout():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(PRODUCER), "assert-source-tree-matches",
         "--actual-commit-sha", "a" * 40, "--actual-tree-sha", "b" * 40,
         "--expected-commit-sha", "a" * 40, "--expected-tree-sha", "b" * 40],
        capture_output=True, text=True,
    )
    assert result.returncode == 0


def test_protected_default_branch_canary_readback():
    # AC7: static evidence that the protected canary contract exists --
    # workflow_dispatch-only trigger, default-branch guard on all three
    # jobs, and REST readback / pass_eligible gating remain present. The
    # live bootstrap/post-merge canary dispatch and REST readback itself is
    # executed post-merge per the Runtime Verification Applicability
    # protected_default_branch_post_merge_canary procedure, not in this
    # local/CI pytest run.
    text = WORKFLOW.read_text()
    assert "workflow_dispatch:" in text
    assert text.count("github.event.repository.default_branch") == 3
    assert "Fetch uploaded producer-receipt artifact metadata via REST" in text
    assert "Gate the job on pass_eligible (fail closed)" in text
