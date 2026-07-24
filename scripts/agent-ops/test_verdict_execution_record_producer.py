#!/usr/bin/env python3
# Fail-closed builder for protected TEST_VERDICT execution artifacts.
#
# The workflow supplies REST-readback snapshots. This module never parses or
# executes Issue Markdown. Commands are selected only from the versioned
# COMMAND_MANIFEST allowlist below, and it is the only code path that can
# turn a command_id into an argv / cwd / timeout_seconds / AC-coverage claim
# (Issue 1646 PR 1683 fix_delta P0-5). Callers cannot supply their own
# executions / per_ac / required_acs through the CLI. Those are derived
# exclusively from COMMAND_MANIFEST plus a single real execute_command()
# result.
#
# This is a bootstrap-only producer dedicated to the Issue 1646 contract test
# (scripts/agent-guards/tests/test_test_verdict_execution_record_workflow.py).
# It is not a generic per-AC-Issue producer. COMMAND_MANIFEST intentionally
# contains exactly one command_id whose covers_acs claim is a
# manifest-declared binding (Issue 1646 AC1-AC5 share the identical
# Verification Command), not an independently isolated per-AC execution.
from __future__ import annotations

import hashlib
import json
import operator
import re
import subprocess
from pathlib import Path
from typing import Any

SCHEMA = "TEST_VERDICT_EXECUTION_RECORD_V1"
RECEIPT_SCHEMA = "TEST_VERDICT_PRODUCER_RECEIPT_V1"

_SHA256_PREFIXED_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_BARE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

COMMAND_MANIFEST: dict[str, dict[str, Any]] = {
    "uv.pytest.execution-record": {
        "argv": [
            "uv",
            "run",
            "--locked",
            "pytest",
            "scripts/agent-guards/tests/test_test_verdict_execution_record_workflow.py",
            "-q",
        ],
        "cwd": "repo_root",
        "timeout_seconds": 300,
        "covers_acs": ["AC1", "AC2", "AC3", "AC4", "AC5"],
    }
}


def sha256_of(text: str):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_artifact_digest(value: str):
    if value is None:
        raise ValueError("artifact digest is empty")
    stripped = value.strip()
    if stripped.startswith("sha256:"):
        stripped = stripped[len("sha256:"):]
    if not _SHA256_BARE_HEX_RE.fullmatch(stripped):
        raise ValueError("not a well-formed sha256 hex digest: " + repr(value))
    return "sha256:" + stripped


def resolve_manifest_entry(command_id: str):
    # Resolve command_id against the COMMAND_MANIFEST allowlist and return
    # a copy augmented with manifest_sha256, the canonical digest of the
    # resolved entry. Raises KeyError for any command_id outside the
    # allowlist. Returns dict.
    if command_id not in COMMAND_MANIFEST:
        raise KeyError("command_id not in COMMAND_MANIFEST allowlist: " + repr(command_id))
    entry = COMMAND_MANIFEST[command_id]
    canonical_entry = {
        "command_id": command_id,
        "argv": list(entry["argv"]),
        "cwd": entry["cwd"],
        "timeout_seconds": entry["timeout_seconds"],
        "covers_acs": list(entry["covers_acs"]),
    }
    result = dict(canonical_entry)
    result["manifest_sha256"] = canonical_sha256(canonical_entry)
    return result


def execute_command(command_id: str, repo_root: Path):
    # Resolve command_id from COMMAND_MANIFEST and run it for real via
    # subprocess.run (shell=False). exit_code / status / timed_out and the
    # stdout / stderr digests are measured from the actual process. None of
    # them are ever self-attested before the process runs (fix_delta P0-4).
    # Returns dict.
    resolved = resolve_manifest_entry(command_id)
    if resolved["cwd"] == "repo_root":
        cwd = repo_root
    else:
        cwd = repo_root / resolved["cwd"]

    timed_out = False
    try:
        completed = subprocess.run(
            resolved["argv"],
            cwd=cwd,
            timeout=resolved["timeout_seconds"],
            shell=False,
            capture_output=True,
            text=False,
            check=False,
        )
        exit_code = completed.returncode
        stdout_bytes = completed.stdout or b""
        stderr_bytes = completed.stderr or b""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        stdout_bytes = exc.stdout or b""
        stderr_bytes = exc.stderr or b""
        if isinstance(stdout_bytes, str):
            stdout_bytes = stdout_bytes.encode("utf-8", errors="replace")
        if isinstance(stderr_bytes, str):
            stderr_bytes = stderr_bytes.encode("utf-8", errors="replace")

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    skipped = bool(re.search(r"\b[1-9]\d*\s+skipped\b", stdout_text))
    fallback_detected = "fallback" in stdout_text.lower()
    status = "pass" if (exit_code == 0 and not timed_out) else "fail"

    return {
        "execution_id": "exec-" + command_id.replace(".", "-"),
        "command_id": command_id,
        "argv_sha256": resolved["manifest_sha256"],
        "exit_code": exit_code,
        "status": status,
        "skipped": skipped,
        "fallback_detected": fallback_detected,
        "timed_out": timed_out,
        "stdout_sha256": "sha256:" + hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": "sha256:" + hashlib.sha256(stderr_bytes).hexdigest(),
    }


def pass_eligible(executions, per_ac, required_acs):
    # executions: list of dict, per_ac: list of dict, required_acs: list of str.
    # Returns bool.
    ids = set(entry.get("execution_id") for entry in executions)
    if not executions:
        return False
    for entry in executions:
        if entry.get("exit_code") != 0:
            return False
        if entry.get("status") != "pass":
            return False
        if entry.get("skipped"):
            return False
        if entry.get("fallback_detected"):
            return False
        if entry.get("timed_out"):
            return False
    coverage = {}
    for entry in per_ac:
        coverage[entry.get("ac")] = entry.get("execution_ids")
    if set(required_acs) != set(coverage):
        return False
    for values in coverage.values():
        if not values:
            return False
        if not set(values).issubset(ids):
            return False
    return True


def build_record(producer, subject, contract, executions, per_ac, required_acs):
    # Pure record assembly. Retained for fixture-driven unit tests that
    # exercise pass_eligible logic directly. The CLI never forwards raw
    # caller-supplied executions / per_ac to this function directly. See
    # build_record_for_command. Returns dict.
    record = {
        "schema": SCHEMA,
        "schema_version": 1,
        "producer": producer,
        "subject": subject,
        "contract": contract,
        "executions": executions,
        "per_ac": per_ac,
        "pass_eligible": pass_eligible(executions, per_ac, required_acs),
    }
    record["payload_sha256"] = canonical_sha256(record)
    return record


def build_record_for_command(producer, subject, contract, command_id, execution_result):
    # The only CLI-reachable record builder. executions / per_ac /
    # required_acs are derived exclusively from COMMAND_MANIFEST[command_id]
    # and a single real execution_result (produced by execute_command / the
    # execute CLI subcommand). The execution_result is rejected unless its
    # own command_id and argv_sha256 match the freshly resolved manifest
    # entry, so a caller cannot substitute a different command output for
    # this command_id coverage claim. Returns dict.
    resolved = resolve_manifest_entry(command_id)
    if execution_result.get("command_id") != command_id:
        raise ValueError(
            "execution_result.command_id does not match requested command_id: "
            + repr(execution_result.get("command_id")) + " != " + repr(command_id)
        )
    if execution_result.get("argv_sha256") != resolved["manifest_sha256"]:
        raise ValueError(
            "execution_result.argv_sha256 does not match the resolved "
            "COMMAND_MANIFEST entry digest, refusing to build a record from it"
        )
    per_ac = []
    for ac in resolved["covers_acs"]:
        per_ac.append({"ac": ac, "execution_ids": [execution_result["execution_id"]]})
    full_contract = dict(contract)
    full_contract["command_manifest_sha256"] = resolved["manifest_sha256"]
    return build_record(
        producer,
        subject,
        full_contract,
        [execution_result],
        per_ac,
        resolved["covers_acs"],
    )


def _is_positive_int(value):
    if isinstance(value, bool):
        return False
    if not isinstance(value, int):
        return False
    return operator.gt(value, 0)


def build_receipt(record, execution_artifact, final_subject, final_contract):
    record_contract = record.get("contract")
    record_manifest_sha256 = (
        record_contract.get("command_manifest_sha256")
        if isinstance(record_contract, dict)
        else None
    )
    final_manifest_sha256 = (
        final_contract.get("command_manifest_sha256")
        if isinstance(final_contract, dict)
        else None
    )
    manifest_is_bound = (
        isinstance(record_manifest_sha256, str)
        and bool(_SHA256_PREFIXED_RE.fullmatch(record_manifest_sha256))
        and final_manifest_sha256 == record_manifest_sha256
    )
    stable = (
        final_subject == record["subject"]
        and final_contract == record_contract
        and manifest_is_bound
    )

    reported = {
        "artifact_id": execution_artifact.get("artifact_id"),
        "artifact_url": execution_artifact.get("artifact_url"),
        "artifact_archive_digest": execution_artifact.get("artifact_archive_digest"),
    }
    rest = execution_artifact.get("rest") or {}
    expected = execution_artifact.get("expected") or {}

    artifact_id_ok = _is_positive_int(reported.get("artifact_id"))
    url_ok = bool(reported.get("artifact_url"))
    digest_value = reported.get("artifact_archive_digest")
    digest_pattern_ok = isinstance(digest_value, str) and bool(_SHA256_PREFIXED_RE.fullmatch(digest_value))

    rest_id_matches = artifact_id_ok and rest.get("id") == reported.get("artifact_id")
    rest_name_matches = bool(rest.get("name")) and rest.get("name") == expected.get("name")
    rest_repo_matches = (
        rest.get("repository_id") == expected.get("repository_id")
        and rest.get("head_repository_id") == expected.get("head_repository_id")
    )
    rest_run_matches = (
        rest.get("workflow_run_id") == expected.get("workflow_run_id")
        and rest.get("head_sha") == expected.get("head_sha")
    )
    rest_not_expired = rest.get("expired") is False
    rest_digest_value = rest.get("digest")
    rest_digest_matches = (
        digest_pattern_ok
        and isinstance(rest_digest_value, str)
        and bool(_SHA256_PREFIXED_RE.fullmatch(rest_digest_value))
        and rest_digest_value == digest_value
    )
    downloaded_payload_matches = (
        isinstance(execution_artifact.get("downloaded_payload_sha256"), str)
        and execution_artifact.get("downloaded_payload_sha256") == record.get("payload_sha256")
    )

    artifact_ok = all(
        [
            artifact_id_ok,
            url_ok,
            digest_pattern_ok,
            rest_id_matches,
            rest_name_matches,
            rest_repo_matches,
            rest_run_matches,
            rest_not_expired,
            rest_digest_matches,
            downloaded_payload_matches,
        ]
    )

    return {
        "schema": RECEIPT_SCHEMA,
        "schema_version": 1,
        "execution_payload_sha256": record["payload_sha256"],
        "execution_artifact": reported,
        "producer": record["producer"],
        "subject": final_subject,
        "contract": final_contract,
        "pass_eligible": bool(record["pass_eligible"] and stable and artifact_ok),
    }


def _validate_against_schema(payload, schema_path):
    from jsonschema import Draft202012Validator

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(payload))
    if errors:
        messages = " and ".join(str(e) for e in errors[:5])
        raise ValueError("schema validation failed against " + str(schema_path) + ": " + messages)


def _cmd_sha256_of(args):
    text = args.input.read_text(encoding="utf-8")
    print(sha256_of(text))
    return 0


def _cmd_execute(args):
    result = execute_command(args.command_id, args.repo_root)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    if result["status"] == "pass":
        return 0
    return 1


def _cmd_build_record(args):
    producer = json.loads(args.producer_input.read_text(encoding="utf-8"))
    subject = json.loads(args.subject_input.read_text(encoding="utf-8"))
    contract = json.loads(args.contract_input.read_text(encoding="utf-8"))
    execution_result = json.loads(args.execution_result.read_text(encoding="utf-8"))
    record = build_record_for_command(producer, subject, contract, args.command_id, execution_result)
    if args.schema is not None:
        _validate_against_schema(record, args.schema)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    if record["pass_eligible"]:
        return 0
    return 1


def _cmd_build_receipt(args):
    record = json.loads(args.record.read_text(encoding="utf-8"))
    execution_artifact = json.loads(args.execution_artifact_input.read_text(encoding="utf-8"))
    final_subject = json.loads(args.final_subject.read_text(encoding="utf-8"))
    final_contract = json.loads(args.final_contract.read_text(encoding="utf-8"))
    receipt = build_receipt(record, execution_artifact, final_subject, final_contract)
    if args.schema is not None:
        _validate_against_schema(receipt, args.schema)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    if receipt["pass_eligible"]:
        return 0
    return 1




def _dot_get(data, pointer):
    node = data
    for part in pointer.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def gh_api_json(path):
    # Calls the gh CLI against the REST API and parses the JSON response.
    # Never uses shell redirection, subprocess captures stdout directly.
    # Returns parsed JSON value.
    completed = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("gh api failed for " + path + ": " + completed.stderr)
    return json.loads(completed.stdout)


def gh_api_bytes(path):
    # Same as gh_api_json but for binary responses (for example artifact
    # zip downloads). Returns bytes.
    completed = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("gh api failed for " + path + ": " + completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout


def _cmd_gh_api(args):
    if args.binary:
        data = gh_api_bytes(args.path)
        args.output.write_bytes(data)
    else:
        data = gh_api_json(args.path)
        args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def _cmd_assert_json_field(args):
    data = json.loads(args.file.read_text(encoding="utf-8"))
    actual = _dot_get(data, args.pointer)
    if str(actual) == args.equals:
        return 0
    print("mismatch at " + args.pointer + ": expected " + args.equals + " got " + str(actual))
    return 1


def _cmd_assert_issue_body_sha(args):
    data = json.loads(args.file.read_text(encoding="utf-8"))
    body = data.get("body") or ""
    actual = sha256_of(body)
    if actual == args.equals:
        return 0
    print("issue body sha mismatch: expected " + args.equals + " got " + actual)
    return 1


def _cmd_assert_stable_fields(args):
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    pointers = args.pointers.split(",")
    failures = []
    for pointer in pointers:
        before_value = _dot_get(before, pointer)
        after_value = _dot_get(after, pointer)
        if before_value != after_value:
            failures.append(pointer + ": " + str(before_value) + " became " + str(after_value))
    if failures:
        for failure in failures:
            print(failure)
        return 1
    return 0


def _extract_id_from_url(url):
    # Returns the trailing path segment of a REST URL as an int.
    segment = url.rstrip("/").split("/")[-1]
    return int(segment)


def _cmd_resolve_job_and_check(args):
    jobs_data = gh_api_json(
        "repos/" + args.repo + "/actions/runs/" + str(args.run_id)
        + "/attempts/" + str(args.run_attempt) + "/jobs"
    )
    jobs = jobs_data.get("jobs") or []
    matched = [job for job in jobs if job.get("runner_name") == args.runner_name]
    if len(matched) != 1:
        print("expected exactly one job matching runner_name " + args.runner_name + ", found " + str(len(matched)))
        return 1
    job = matched[0]
    check_run_url = job.get("check_run_url") or ""
    if not check_run_url:
        print("job has no check_run_url")
        return 1
    check_run_id = _extract_id_from_url(check_run_url)
    check_run = gh_api_json("repos/" + args.repo + "/check-runs/" + str(check_run_id))
    if check_run.get("head_sha") != args.expected_head_sha:
        print(
            "check-run head_sha mismatch: expected "
            + args.expected_head_sha
            + " got "
            + str(check_run.get("head_sha"))
        )
        return 1
    if check_run.get("id") != check_run_id:
        print("check-run id mismatch after independent readback")
        return 1
    args.output_job.write_text(json.dumps(job, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    args.output_check.write_text(json.dumps(check_run, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def _cmd_download_artifact(args):
    import io
    import zipfile

    data = gh_api_bytes("repos/" + args.repo + "/actions/artifacts/" + str(args.artifact_id) + "/zip")
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = archive.namelist()
    if len(names) != 1:
        print("expected exactly one file inside artifact archive, found " + str(len(names)))
        return 1
    inner_bytes = archive.read(names[0])
    record = json.loads(inner_bytes.decode("utf-8"))
    reported_payload_sha256 = record.get("payload_sha256")
    stripped_record = dict(record)
    stripped_record.pop("payload_sha256", None)
    recomputed = canonical_sha256(stripped_record)
    if recomputed != reported_payload_sha256:
        print("downloaded record payload_sha256 does not match its own recomputed digest")
        return 1
    args.output_json.write_text(json.dumps(record, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    args.output_payload_sha256.write_text(recomputed, encoding="utf-8")
    return 0




def _cmd_assemble_producer(args):
    job = json.loads(args.job_file.read_text(encoding="utf-8"))
    check = json.loads(args.check_file.read_text(encoding="utf-8"))
    producer = {
        "workflow_path": args.workflow_path,
        "workflow_source_ref": args.workflow_source_ref,
        "workflow_source_sha": args.workflow_source_sha,
        "workflow_run_id": int(args.run_id),
        "workflow_run_attempt": int(args.run_attempt),
        "job_id": job.get("id"),
        "check_run_id": check.get("id"),
    }
    args.output.write_text(json.dumps(producer, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def _cmd_assemble_subject(args):
    subject_data = json.loads(args.subject_file.read_text(encoding="utf-8"))
    subject = {
        "target_pr_number": int(args.target_pr_number),
        "head_repository_id": subject_data["head"]["repo"]["id"],
        "pr_head_sha": subject_data["head"]["sha"],
    }
    args.output.write_text(json.dumps(subject, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def _cmd_assemble_contract(args):
    issue_data = json.loads(args.issue_file.read_text(encoding="utf-8"))
    contract = {
        "linked_issue_number": int(args.linked_issue_number),
        "issue_body_sha256": sha256_of(issue_data.get("body") or ""),
    }
    if args.execution_record is not None:
        record = json.loads(args.execution_record.read_text(encoding="utf-8"))
        record_contract = record.get("contract")
        manifest_sha256 = (
            record_contract.get("command_manifest_sha256")
            if isinstance(record_contract, dict)
            else None
        )
        if not isinstance(manifest_sha256, str) or not _SHA256_PREFIXED_RE.fullmatch(manifest_sha256):
            raise ValueError("execution record has no valid command_manifest_sha256")
        contract["command_manifest_sha256"] = manifest_sha256
    args.output.write_text(json.dumps(contract, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def _cmd_assemble_execution_artifact(args):
    rest = json.loads(args.rest_file.read_text(encoding="utf-8"))
    downloaded_sha = args.downloaded_payload_sha256_file.read_text(encoding="utf-8").strip()
    execution_artifact = {
        "artifact_id": int(args.artifact_id),
        "artifact_url": args.artifact_url,
        "artifact_archive_digest": normalize_artifact_digest(args.artifact_digest),
        "rest": {
            "id": rest.get("id"),
            "name": rest.get("name"),
            "expired": rest.get("expired"),
            "digest": rest.get("digest"),
            "repository_id": (rest.get("workflow_run") or {}).get("repository_id"),
            "head_repository_id": (rest.get("workflow_run") or {}).get("head_repository_id"),
            "workflow_run_id": (rest.get("workflow_run") or {}).get("id"),
            "head_sha": (rest.get("workflow_run") or {}).get("head_sha"),
        },
        "expected": {
            "repository_id": int(args.expected_repository_id),
            "head_repository_id": int(args.expected_repository_id),
            "workflow_run_id": int(args.expected_run_id),
            "head_sha": args.expected_head_sha,
            "name": args.expected_name,
        },
        "downloaded_payload_sha256": downloaded_sha,
    }
    args.output.write_text(json.dumps(execution_artifact, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def main():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sha = sub.add_parser("sha256-of")
    p_sha.add_argument("--input", type=Path, required=True)
    p_sha.set_defaults(func=_cmd_sha256_of)

    p_exec = sub.add_parser("execute")
    p_exec.add_argument("--command-id", required=True)
    p_exec.add_argument("--repo-root", type=Path, required=True)
    p_exec.add_argument("--output", type=Path, required=True)
    p_exec.set_defaults(func=_cmd_execute)

    p_rec = sub.add_parser("build-record")
    p_rec.add_argument("--command-id", required=True)
    p_rec.add_argument("--producer-input", type=Path, required=True)
    p_rec.add_argument("--subject-input", type=Path, required=True)
    p_rec.add_argument("--contract-input", type=Path, required=True)
    p_rec.add_argument("--execution-result", type=Path, required=True)
    p_rec.add_argument("--schema", type=Path, required=False)
    p_rec.add_argument("--output", type=Path, required=True)
    p_rec.set_defaults(func=_cmd_build_record)

    p_receipt = sub.add_parser("build-receipt")
    p_receipt.add_argument("--record", type=Path, required=True)
    p_receipt.add_argument("--execution-artifact-input", type=Path, required=True)
    p_receipt.add_argument("--final-subject", type=Path, required=True)
    p_receipt.add_argument("--final-contract", type=Path, required=True)
    p_receipt.add_argument("--schema", type=Path, required=False)
    p_receipt.add_argument("--output", type=Path, required=True)
    p_receipt.set_defaults(func=_cmd_build_receipt)

    p_ghapi = sub.add_parser("gh-api")
    p_ghapi.add_argument("--path", required=True)
    p_ghapi.add_argument("--output", type=Path, required=True)
    p_ghapi.add_argument("--binary", action="store_true")
    p_ghapi.set_defaults(func=_cmd_gh_api)

    p_ajf = sub.add_parser("assert-json-field")
    p_ajf.add_argument("--file", type=Path, required=True)
    p_ajf.add_argument("--pointer", required=True)
    p_ajf.add_argument("--equals", required=True)
    p_ajf.set_defaults(func=_cmd_assert_json_field)

    p_aibs = sub.add_parser("assert-issue-body-sha")
    p_aibs.add_argument("--file", type=Path, required=True)
    p_aibs.add_argument("--equals", required=True)
    p_aibs.set_defaults(func=_cmd_assert_issue_body_sha)

    p_asf = sub.add_parser("assert-stable-fields")
    p_asf.add_argument("--before", type=Path, required=True)
    p_asf.add_argument("--after", type=Path, required=True)
    p_asf.add_argument("--pointers", required=True)
    p_asf.set_defaults(func=_cmd_assert_stable_fields)

    p_rjc = sub.add_parser("resolve-job-and-check")
    p_rjc.add_argument("--repo", required=True)
    p_rjc.add_argument("--run-id", required=True)
    p_rjc.add_argument("--run-attempt", required=True)
    p_rjc.add_argument("--runner-name", required=True)
    p_rjc.add_argument("--expected-head-sha", required=True)
    p_rjc.add_argument("--output-job", type=Path, required=True)
    p_rjc.add_argument("--output-check", type=Path, required=True)
    p_rjc.set_defaults(func=_cmd_resolve_job_and_check)

    p_dl = sub.add_parser("download-artifact")
    p_dl.add_argument("--repo", required=True)
    p_dl.add_argument("--artifact-id", required=True)
    p_dl.add_argument("--output-json", type=Path, required=True)
    p_dl.add_argument("--output-payload-sha256", type=Path, required=True)
    p_dl.set_defaults(func=_cmd_download_artifact)

    p_aprod = sub.add_parser("assemble-producer")
    p_aprod.add_argument("--workflow-path", required=True)
    p_aprod.add_argument("--workflow-source-ref", required=True)
    p_aprod.add_argument("--workflow-source-sha", required=True)
    p_aprod.add_argument("--run-id", required=True)
    p_aprod.add_argument("--run-attempt", required=True)
    p_aprod.add_argument("--job-file", type=Path, required=True)
    p_aprod.add_argument("--check-file", type=Path, required=True)
    p_aprod.add_argument("--output", type=Path, required=True)
    p_aprod.set_defaults(func=_cmd_assemble_producer)

    p_asub = sub.add_parser("assemble-subject")
    p_asub.add_argument("--subject-file", type=Path, required=True)
    p_asub.add_argument("--target-pr-number", required=True)
    p_asub.add_argument("--output", type=Path, required=True)
    p_asub.set_defaults(func=_cmd_assemble_subject)

    p_acon = sub.add_parser("assemble-contract")
    p_acon.add_argument("--issue-file", type=Path, required=True)
    p_acon.add_argument("--linked-issue-number", required=True)
    p_acon.add_argument("--execution-record", type=Path, required=False)
    p_acon.add_argument("--output", type=Path, required=True)
    p_acon.set_defaults(func=_cmd_assemble_contract)

    p_aea = sub.add_parser("assemble-execution-artifact")
    p_aea.add_argument("--rest-file", type=Path, required=True)
    p_aea.add_argument("--downloaded-payload-sha256-file", type=Path, required=True)
    p_aea.add_argument("--artifact-id", required=True)
    p_aea.add_argument("--artifact-url", required=True)
    p_aea.add_argument("--artifact-digest", required=True)
    p_aea.add_argument("--expected-repository-id", required=True)
    p_aea.add_argument("--expected-run-id", required=True)
    p_aea.add_argument("--expected-head-sha", required=True)
    p_aea.add_argument("--expected-name", required=True)
    p_aea.add_argument("--output", type=Path, required=True)
    p_aea.set_defaults(func=_cmd_assemble_execution_artifact)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
