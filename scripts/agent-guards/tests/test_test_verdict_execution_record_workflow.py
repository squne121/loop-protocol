# Contract tests for the protected execution-record producer.
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/test-verdict-execution-record.yml"
PRODUCER = ROOT / "scripts/agent-ops/test_verdict_execution_record_producer.py"


def load_producer():
    spec = importlib.util.spec_from_file_location("producer", PRODUCER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolved_manifest(producer):
    return producer.resolve_manifest_entry("uv.pytest.execution-record")


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


def build_record_from_fixture(producer, data, result):
    return producer.build_record_for_command(
        data["producer"],
        data["subject"],
        data["contract"],
        "uv.pytest.execution-record",
        result,
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
    setup_index = text.find("./.github/actions/setup-python-uv")
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
    record = build_record_from_fixture(producer, data, result)
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
        record, ea, record["subject"], final_contract, expected_manifest_sha256=manifest["manifest_sha256"]
    )
    assert receipt["pass_eligible"]
    assert record["payload_sha256"] != receipt["execution_artifact"]["artifact_archive_digest"]
    assert receipt["contract"]["command_manifest_sha256"] == manifest["manifest_sha256"]


def test_given_drift_or_missing_coverage_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer, timed_out=True)
    record = build_record_from_fixture(producer, data, result)
    assert not record["pass_eligible"]
    ea = matching_execution_artifact(record)
    receipt = producer.build_receipt(record, ea, record["subject"], record["contract"])
    assert not receipt["pass_eligible"]


def test_given_identity_drift_after_upload_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    record = build_record_from_fixture(producer, data, result)
    assert record["pass_eligible"]
    ea = matching_execution_artifact(record)
    drifted_subject = dict(record["subject"], pr_head_sha="f" * 40)
    receipt = producer.build_receipt(record, ea, drifted_subject, record["contract"])
    assert not receipt["pass_eligible"]


def test_given_artifact_digest_missing_when_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    record = build_record_from_fixture(producer, data, result)
    assert record["pass_eligible"]
    receipt = producer.build_receipt(record, {}, record["subject"], record["contract"])
    assert not receipt["pass_eligible"]


def test_given_artifact_id_mismatch_when_receipt_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    record = build_record_from_fixture(producer, data, result)
    assert record["pass_eligible"]
    ea = matching_execution_artifact(record)
    ea["artifact_id"] = 999999
    receipt = producer.build_receipt(record, ea, record["subject"], record["contract"])
    assert not receipt["pass_eligible"]


def test_given_artifact_digest_mismatch_when_receipt_built_then_receipt_is_not_pass_eligible():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    record = build_record_from_fixture(producer, data, result)
    assert record["pass_eligible"]
    ea = matching_execution_artifact(record)
    ea["artifact_archive_digest"] = "sha256:" + ("9" * 64)
    receipt = producer.build_receipt(record, ea, record["subject"], record["contract"])
    assert not receipt["pass_eligible"]


def test_given_schema_files_when_loaded_then_required_identity_fields_exist():
    for filename in ("test-verdict-execution-record.schema.json", "test-verdict-producer-receipt.schema.json"):
        schema = json.loads((ROOT / "schemas" / filename).read_text())
        assert "pass_eligible" in schema["required"]
        assert schema["additionalProperties"] is False
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_given_schema_files_when_validated_against_matching_record_then_no_errors():
    from jsonschema import Draft202012Validator

    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    record = build_record_from_fixture(producer, data, result)
    record_schema = json.loads((ROOT / "schemas" / "test-verdict-execution-record.schema.json").read_text())
    Draft202012Validator.check_schema(record_schema)
    errors = list(Draft202012Validator(record_schema).iter_errors(record))
    assert not errors

    ea = matching_execution_artifact(record)
    receipt = producer.build_receipt(record, ea, record["subject"], record["contract"])
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

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        producer_input = tmp / "producer-input.json"
        subject_input = tmp / "subject-input.json"
        contract_input = tmp / "contract-input.json"
        execution_result_file = tmp / "execution-result.json"
        producer_input.write_text(json.dumps(data["producer"]))
        subject_input.write_text(json.dumps(data["subject"]))
        contract_input.write_text(json.dumps(data["contract"]))
        execution_result_file.write_text(json.dumps(result))
        missing_receipt_input = tmp / "execution-artifact-input.json"
        record_output = tmp / "record-output.json"
        producer_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-record",
             "--command-id", "uv.pytest.execution-record",
             "--producer-input", str(producer_input),
             "--subject-input", str(subject_input),
             "--contract-input", str(contract_input),
             "--execution-result", str(execution_result_file),
             "--output", str(record_output)],
            capture_output=True,
            text=True,
        )
        assert producer_result.returncode == 0
        assert record_output.exists()

        receipt_output = tmp / "receipt-output.json"
        final_subject = tmp / "final-subject.json"
        final_contract = tmp / "final-contract.json"
        final_subject.write_text(json.dumps(data["subject"]))
        final_contract.write_text(json.dumps(data["contract"]))
        receipt_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-receipt",
             "--record", str(record_output),
             "--execution-artifact-input", str(missing_receipt_input),
             "--final-subject", str(final_subject),
             "--final-contract", str(final_contract),
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
    record = build_record_from_fixture(producer, data, result)
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
            expected_manifest_sha256=manifest["manifest_sha256"],
        )
    except ValueError as exc:
        raised = True
        assert "command_manifest_sha256_missing" in str(exc)
    assert raised


def test_given_malformed_command_manifest_sha256_when_build_receipt_then_rejected():
    producer = load_producer()
    data = identity_fixture()
    result = execution_result(producer)
    record = build_record_from_fixture(producer, data, result)
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    final_contract = _final_contract_with_manifest(producer, data)
    final_contract["command_manifest_sha256"] = "not-a-sha256"
    raised = False
    try:
        producer.build_receipt(
            record, ea, record["subject"], final_contract, expected_manifest_sha256=manifest["manifest_sha256"]
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
    record = build_record_from_fixture(producer, data, result)
    ea = matching_execution_artifact(record)
    manifest = resolved_manifest(producer)
    final_contract = _final_contract_with_manifest(producer, data)
    final_contract["command_manifest_sha256"] = "sha256:" + ("9" * 64)
    raised = False
    try:
        producer.build_receipt(
            record, ea, record["subject"], final_contract, expected_manifest_sha256=manifest["manifest_sha256"]
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
    record = build_record_from_fixture(producer, data, result)
    record["contract"]["command_manifest_sha256"] = "sha256:" + ("7" * 64)
    ea = matching_execution_artifact(record)
    final_contract = dict(data["contract"])
    final_contract["command_manifest_sha256"] = "sha256:" + ("7" * 64)
    expected_manifest_sha256 = producer.resolve_manifest_entry("uv.pytest.execution-record")["manifest_sha256"]
    raised = False
    try:
        producer.build_receipt(
            record, ea, record["subject"], final_contract, expected_manifest_sha256=expected_manifest_sha256
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

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        producer_input = tmp / "producer-input.json"
        subject_input = tmp / "subject-input.json"
        contract_input = tmp / "contract-input.json"
        execution_result_file = tmp / "execution-result.json"
        producer_input.write_text(json.dumps(data["producer"]))
        subject_input.write_text(json.dumps(data["subject"]))
        contract_input.write_text(json.dumps(data["contract"]))
        execution_result_file.write_text(json.dumps(result))
        record_output = tmp / "record-output.json"
        producer_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-record",
             "--command-id", "uv.pytest.execution-record",
             "--producer-input", str(producer_input),
             "--subject-input", str(subject_input),
             "--contract-input", str(contract_input),
             "--execution-result", str(execution_result_file),
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
        final_subject.write_text(json.dumps(data["subject"]))
        final_contract.write_text(json.dumps(data["contract"]))
        receipt_output = tmp / "receipt-output.json"
        receipt_result = subprocess.run(
            [sys.executable, str(PRODUCER), "build-receipt",
             "--record", str(record_output),
             "--execution-artifact-input", str(execution_artifact_input),
             "--final-subject", str(final_subject),
             "--final-contract", str(final_contract),
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


def test_exact_source_provenance_binding():
    # AC3: the untrusted source job checks out only the target PR's exact
    # head SHA (a separate job from the trusted producer job), and the
    # trusted producer job independently rejects provenance that does not
    # bind to that exact head SHA / repository.
    text = WORKFLOW.read_text()
    assert "resolve-untrusted-source" in text
    assert "needs: resolve-untrusted-source" in text
    assert "ref: ${{ inputs.expected_head_sha }}" in text
    assert "assemble-source-provenance" in text
    assert "assert-source-provenance" in text

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        provenance_file = tmp / "source-provenance.json"
        provenance_file.write_text(json.dumps({
            "repository_full_name": "owner/repo",
            "commit_sha": "a" * 40,
            "tree_sha": "b" * 40,
        }))

        import subprocess
        import sys
        ok = subprocess.run(
            [sys.executable, str(PRODUCER), "assert-source-provenance",
             "--file", str(provenance_file),
             "--expected-repository-full-name", "owner/repo",
             "--expected-commit-sha", "a" * 40],
            capture_output=True, text=True,
        )
        assert ok.returncode == 0

        mismatched = subprocess.run(
            [sys.executable, str(PRODUCER), "assert-source-provenance",
             "--file", str(provenance_file),
             "--expected-repository-full-name", "owner/repo",
             "--expected-commit-sha", "c" * 40],
            capture_output=True, text=True,
        )
        assert mismatched.returncode == 1
        assert "reason_category=source_provenance_mismatch" in mismatched.stdout


def test_trusted_untrusted_execution_boundary():
    # AC4: the untrusted source job must not carry secrets, OIDC, or a write
    # token. Its permissions block is scoped to contents: read only, and it
    # never references github.token / secrets.* / id-token.
    text = WORKFLOW.read_text()
    untrusted_start = text.find("resolve-untrusted-source:")
    trusted_start = text.find("produce:")
    assert untrusted_start != -1 and trusted_start != -1 and untrusted_start < trusted_start
    untrusted_block = text[untrusted_start:trusted_start]
    assert "permissions:" in untrusted_block
    assert "contents: read" in untrusted_block
    assert "secrets." not in untrusted_block
    assert "github.token" not in untrusted_block
    assert "id-token" not in untrusted_block
    assert "GH_TOKEN" not in untrusted_block


def test_protected_default_branch_canary_readback():
    # AC7: static evidence that the protected canary contract exists --
    # workflow_dispatch-only trigger, default-branch guard on both jobs, and
    # REST readback / pass_eligible gating remain present. The live
    # bootstrap/post-merge canary dispatch and REST readback itself is
    # executed post-merge per the Runtime Verification Applicability
    # protected_default_branch_post_merge_canary procedure, not in this
    # local/CI pytest run.
    text = WORKFLOW.read_text()
    assert "workflow_dispatch:" in text
    assert text.count("github.event.repository.default_branch") == 2
    assert "Fetch uploaded producer-receipt artifact metadata via REST" in text
    assert "Gate the job on pass_eligible (fail closed)" in text
