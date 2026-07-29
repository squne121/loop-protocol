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
#
# Issue 1711 trust-boundary redesign: the protected workflow now separates a
# trusted-preflight job (resolves the subject PR's exact source identity --
# repository id/full_name, commit sha, tree sha -- exclusively from GitHub
# REST API responses, never from a checkout of PR-controlled code), an
# untrusted-execution job (checks out the target PR's exact head SHA into a
# "source" workspace, separate from a "harness" workspace checked out from
# the default branch; the harness's own setup-python-uv action and this
# producer script drive the real pytest execution against the source tree;
# no secrets/OIDC/write token are exposed to this job), and a trusted-receipt
# job (never checks out PR-controlled code; treats the untrusted execution
# artifact as untrusted data; independently re-resolves the trusted source
# identity a second time and rejects any drift before binding the source
# repository id/commit/tree/execution run+job ids into the schema-validated
# execution record and producer receipt).
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

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_FULL_NAME_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

# Issue 1711: the pytest target is inside the untrusted "source" checkout,
# while the interpreter / locked project environment / toolchain all come
# from the trusted "harness" checkout (cwd, when execute_command runs the
# resolved argv). "{source_root}" is a literal, reviewable placeholder inside
# the versioned manifest entry -- resolve_manifest_entry() never substitutes
# it, so manifest_sha256 stays a pure function of COMMAND_MANIFEST content
# alone, independent of the runtime directory layout. Only execute_command()
# substitutes the placeholder with the real absolute source_root path
# immediately before invoking subprocess.run.
COMMAND_MANIFEST: dict[str, dict[str, Any]] = {
    "uv.pytest.execution-record": {
        "argv": [
            "uv",
            "run",
            "--locked",
            "pytest",
            "{source_root}/scripts/agent-guards/tests/test_test_verdict_execution_record_workflow.py",
            "-q",
        ],
        "cwd": "repo_root",
        "timeout_seconds": 300,
        "covers_acs": ["AC1", "AC2", "AC3", "AC4", "AC5"],
    }
}

_SOURCE_ROOT_PLACEHOLDER = "{source_root}"
_SOURCE_CORE_FIELDS = ("repository_id", "repository_full_name", "commit_sha", "tree_sha")


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
    # resolved entry (computed over the unsubstituted "{source_root}"
    # placeholder form, so it never depends on runtime directory layout).
    # Raises KeyError for any command_id outside the allowlist. Returns dict.
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


def substitute_source_root(argv, source_root: Path):
    # Replaces the literal "{source_root}" placeholder with the real
    # absolute source_root path. This is the only argv mutation
    # execute_command performs; it never accepts caller-supplied argv
    # fragments (Issue 1711 AC1/AC4).
    resolved_source_root = str(Path(source_root).resolve())
    return [part.replace(_SOURCE_ROOT_PLACEHOLDER, resolved_source_root) for part in argv]


def execute_command(command_id: str, repo_root: Path, source_root: Path):
    # Resolve command_id from COMMAND_MANIFEST and run it for real via
    # subprocess.run (shell=False). exit_code / status / timed_out and the
    # stdout / stderr digests are measured from the actual process. None of
    # them are ever self-attested before the process runs (fix_delta P0-4).
    # repo_root is the trusted harness checkout (cwd, locked uv project);
    # source_root is the untrusted target-PR checkout the manifest's pytest
    # target path is resolved against (Issue 1711 AC3/AC4). Returns dict.
    resolved = resolve_manifest_entry(command_id)
    if resolved["cwd"] == "repo_root":
        cwd = repo_root
    else:
        cwd = repo_root / resolved["cwd"]
    runtime_argv = substitute_source_root(resolved["argv"], source_root)

    timed_out = False
    try:
        completed = subprocess.run(
            runtime_argv,
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


def classify_contract_manifest_binding(record_contract, final_contract, expected_manifest_sha256):
    # Independently classifies the command_manifest_sha256 binding between the
    # record's own contract and the separately-assembled final contract
    # against a freshly recomputed expected value (from COMMAND_MANIFEST via
    # resolve_manifest_entry, never self-attested from either input). Returns
    # None when the binding is sound, otherwise a machine-readable reason
    # category string. This is the sole gate used by build_receipt to reject
    # missing / malformed / tampered / self-attested manifests and
    # final-contract self-copies (Issue 1711 regression fix).
    record_value = record_contract.get("command_manifest_sha256")
    final_value = final_contract.get("command_manifest_sha256")
    if record_value is None or final_value is None:
        return "command_manifest_sha256_missing"
    if not (
        isinstance(record_value, str)
        and isinstance(final_value, str)
        and _SHA256_PREFIXED_RE.fullmatch(record_value)
        and _SHA256_PREFIXED_RE.fullmatch(final_value)
    ):
        return "command_manifest_sha256_malformed"
    if record_value != final_value:
        return "command_manifest_sha256_tampered"
    if record_value != expected_manifest_sha256:
        return "command_manifest_sha256_self_attested"
    return None


def _source_core(source: dict | None):
    if not isinstance(source, dict):
        return None
    return {key: source.get(key) for key in _SOURCE_CORE_FIELDS}


def classify_source_binding(record_source, final_source):
    # Independently classifies the source repository/commit/tree binding
    # between the record's own source claim and a freshly, independently
    # re-resolved trusted source (Issue 1711 AC3/AC6). None of the compared
    # values are ever read back from the untrusted execution artifact --
    # both sides are trusted-job-resolved GitHub REST API data. Returns None
    # when sound, otherwise a machine-readable reason category string.
    record_core = _source_core(record_source)
    final_core = _source_core(final_source)
    if record_core is None or final_core is None:
        return "source_missing"
    for field in _SOURCE_CORE_FIELDS:
        if record_core.get(field) is None or final_core.get(field) is None:
            return "source_missing"
    if not (
        isinstance(record_core["commit_sha"], str)
        and _COMMIT_SHA_RE.fullmatch(record_core["commit_sha"])
        and isinstance(final_core["commit_sha"], str)
        and _COMMIT_SHA_RE.fullmatch(final_core["commit_sha"])
        and isinstance(record_core["tree_sha"], str)
        and _COMMIT_SHA_RE.fullmatch(record_core["tree_sha"])
        and isinstance(final_core["tree_sha"], str)
        and _COMMIT_SHA_RE.fullmatch(final_core["tree_sha"])
        and isinstance(record_core["repository_full_name"], str)
        and _REPO_FULL_NAME_RE.fullmatch(record_core["repository_full_name"])
        and isinstance(final_core["repository_full_name"], str)
        and _REPO_FULL_NAME_RE.fullmatch(final_core["repository_full_name"])
        and _is_positive_int(record_core["repository_id"])
        and _is_positive_int(final_core["repository_id"])
    ):
        return "source_malformed"
    if record_core != final_core:
        return "source_tampered"
    return None


def build_record(producer, subject, contract, executions, per_ac, required_acs, source):
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
        "source": source,
        "executions": executions,
        "per_ac": per_ac,
        "pass_eligible": pass_eligible(executions, per_ac, required_acs),
    }
    record["payload_sha256"] = canonical_sha256(record)
    return record


def build_record_for_command(producer, subject, contract, command_id, execution_result, source):
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
            "reason_category=execution_result_command_id_mismatch: "
            + repr(execution_result.get("command_id")) + " != " + repr(command_id)
        )
    if execution_result.get("argv_sha256") != resolved["manifest_sha256"]:
        raise ValueError(
            "reason_category=execution_result_argv_sha256_mismatch: execution_result.argv_sha256 "
            "does not match the resolved COMMAND_MANIFEST entry digest, refusing to build a record from it"
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
        source,
    )


def _is_positive_int(value):
    if isinstance(value, bool):
        return False
    if not isinstance(value, int):
        return False
    return operator.gt(value, 0)


def build_receipt(record, execution_artifact, final_subject, final_contract, expected_manifest_sha256, final_source):
    # Issue 1711 High fix: expected_manifest_sha256 and final_source are now
    # required (no optional fail-open path). Every receipt build always
    # independently classifies both the command_manifest_sha256 binding and
    # the source repository/commit/tree binding before considering
    # pass_eligible.
    manifest_reason = classify_contract_manifest_binding(record["contract"], final_contract, expected_manifest_sha256)
    if manifest_reason is not None:
        raise ValueError("reason_category=" + manifest_reason)
    source_reason = classify_source_binding(record.get("source"), final_source)
    if source_reason is not None:
        raise ValueError("reason_category=" + source_reason)
    stable = final_subject == record["subject"] and final_contract == record["contract"]

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
        "source": record.get("source"),
        "pass_eligible": bool(record["pass_eligible"] and stable and artifact_ok),
    }


def _validate_against_schema(payload, schema_path):
    from jsonschema import Draft202012Validator

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(payload))
    if errors:
        messages = " and ".join(str(e) for e in errors[:5])
        raise ValueError("reason_category=schema_validation_failed: " + str(schema_path) + ": " + messages)


def _cmd_sha256_of(args):
    text = args.input.read_text(encoding="utf-8")
    print(sha256_of(text))
    return 0


def _cmd_execute(args):
    result = execute_command(args.command_id, args.repo_root, args.source_root)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    if result["status"] == "pass":
        return 0
    return 1


def _load_json_inputs(*paths: Path):
    # Shared fail-closed JSON loader for the build-record / build-receipt
    # CLIs (Issue 1711 AC5): any malformed input file is diagnosed with a
    # machine-readable reason_category before any output is written.
    values = []
    for path in paths:
        try:
            values.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise ValueError("reason_category=input_json_invalid: " + str(path) + ": " + str(exc)) from exc
    return values


def _cmd_build_record(args):
    try:
        producer, subject, contract, execution_result, source = _load_json_inputs(
            args.producer_input, args.subject_input, args.contract_input, args.execution_result, args.source_input
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    try:
        record = build_record_for_command(producer, subject, contract, args.command_id, execution_result, source)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.schema is not None:
        try:
            _validate_against_schema(record, args.schema)
        except ValueError as exc:
            print(str(exc))
            return 1
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    if record["pass_eligible"]:
        return 0
    print("reason_category=execution_record_not_pass_eligible")
    return 1


def _cmd_build_receipt(args):
    try:
        record, execution_artifact, final_subject, final_contract, final_source = _load_json_inputs(
            args.record, args.execution_artifact_input, args.final_subject, args.final_contract, args.final_source
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    expected_manifest_sha256 = resolve_manifest_entry(args.command_id)["manifest_sha256"]
    try:
        receipt = build_receipt(
            record,
            execution_artifact,
            final_subject,
            final_contract,
            expected_manifest_sha256,
            final_source,
        )
    except ValueError as exc:
        # Direct, machine-readable failure diagnostics (Issue 1711 AC5): no
        # partial/invalid receipt is written, so the subsequent
        # upload-artifact step's "no files found" error is never mistaken
        # for the real cause. The reason category is always printed here.
        print(str(exc))
        return 1
    if args.schema is not None:
        try:
            _validate_against_schema(receipt, args.schema)
        except ValueError as exc:
            print(str(exc))
            return 1
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    if receipt["pass_eligible"]:
        return 0
    print("reason_category=receipt_not_pass_eligible")
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


def _cmd_resolve_job_id_by_name(args):
    # Issue 1711 AC3/AC6: finds the untrusted-execution job's own job id
    # inside the same run/attempt jobs list already independently fetched
    # via gh-api, by exact job name match. Used by the trusted-receipt job
    # to bind source.execution_job_id to the job that actually performed the
    # untrusted PR-source execution (a different job than the one invoking
    # this command).
    jobs_data = json.loads(args.jobs_file.read_text(encoding="utf-8"))
    jobs = jobs_data.get("jobs") or []
    matched = [job for job in jobs if job.get("name") == args.job_name]
    if len(matched) != 1:
        print(
            "reason_category=source_execution_job_lookup_failed: expected exactly one job named "
            + args.job_name + ", found " + str(len(matched))
        )
        return 1
    job = matched[0]
    job_id = job.get("id")
    if not _is_positive_int(job_id):
        print("reason_category=source_execution_job_lookup_failed: job id missing or invalid")
        return 1
    args.output.write_text(json.dumps({"job_id": job_id}, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
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
    if args.command_id is not None:
        # Independent recompute: resolve_manifest_entry is called fresh here,
        # in a separate process invocation from build-record's own resolve,
        # so this contract assembly never copies command_manifest_sha256 from
        # any prior artifact (Issue 1711 AC1/AC2).
        resolved = resolve_manifest_entry(args.command_id)
        contract["command_manifest_sha256"] = resolved["manifest_sha256"]
    args.output.write_text(json.dumps(contract, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def resolve_trusted_source(subject_data, commit_data, expected_repository_full_name, expected_commit_sha):
    # Trusted-job-only resolution: derives repository_id / repository_full_name
    # / commit_sha / tree_sha exclusively from GitHub REST API responses
    # (the PR "head" object and the "GET commit" response's commit.tree.sha).
    # Never reads a checkout of the target PR's own code (Issue 1711 AC3/AC4
    # -- this is the fix for the previous self-attested
    # assemble-source-provenance design, which ran inside the untrusted
    # checkout). Raises ValueError with a reason_category on any mismatch or
    # malformed field. Returns dict.
    head = (subject_data or {}).get("head") or {}
    repo = head.get("repo") or {}
    repository_id = repo.get("id")
    repository_full_name = repo.get("full_name")
    commit_sha = commit_data.get("sha") if isinstance(commit_data, dict) else None
    tree_sha = ((commit_data or {}).get("commit") or {}).get("tree", {}).get("sha")

    if repository_full_name != expected_repository_full_name:
        raise ValueError(
            "reason_category=trusted_source_repository_mismatch: expected "
            + str(expected_repository_full_name) + " got " + str(repository_full_name)
        )
    if commit_sha != expected_commit_sha:
        raise ValueError(
            "reason_category=trusted_source_commit_sha_mismatch: expected "
            + str(expected_commit_sha) + " got " + str(commit_sha)
        )
    if head.get("sha") != expected_commit_sha:
        raise ValueError(
            "reason_category=trusted_source_subject_head_sha_mismatch: expected "
            + str(expected_commit_sha) + " got " + str(head.get("sha"))
        )
    if not _is_positive_int(repository_id):
        raise ValueError("reason_category=trusted_source_repository_id_invalid: " + repr(repository_id))
    if not (isinstance(commit_sha, str) and _COMMIT_SHA_RE.fullmatch(commit_sha)):
        raise ValueError("reason_category=trusted_source_commit_sha_malformed: " + repr(commit_sha))
    if not (isinstance(tree_sha, str) and _COMMIT_SHA_RE.fullmatch(tree_sha)):
        raise ValueError("reason_category=trusted_source_tree_sha_malformed: " + repr(tree_sha))

    return {
        "repository_id": repository_id,
        "repository_full_name": repository_full_name,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
    }


def _cmd_resolve_trusted_source(args):
    subject_data = json.loads(args.subject_file.read_text(encoding="utf-8"))
    commit_data = json.loads(args.commit_file.read_text(encoding="utf-8"))
    try:
        provenance = resolve_trusted_source(
            subject_data, commit_data, args.expected_repository_full_name, args.expected_commit_sha
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    args.output.write_text(json.dumps(provenance, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    if args.github_output_format:
        # Emits plain key=value lines (no other stdout content on the
        # success path) so the workflow step can redirect this command's
        # stdout directly into $GITHUB_OUTPUT without any inline shell
        # heredoc/Python snippet (Issue 1711 actionlint/shellcheck hygiene).
        for key in ("repository_id", "repository_full_name", "commit_sha", "tree_sha"):
            print(key + "=" + str(provenance[key]))
    return 0


def _cmd_assemble_source(args):
    # Merges a trusted source-core resolution (repository_id /
    # repository_full_name / commit_sha / tree_sha, from
    # resolve-trusted-source) with the execution run/job identity of the
    # untrusted-execution job that actually ran the manifest command,
    # producing the schema-shaped "source" object bound into the execution
    # record and producer receipt (Issue 1711 AC3/AC6).
    trusted_source = json.loads(args.trusted_source_file.read_text(encoding="utf-8"))
    run_id = int(args.execution_run_id)
    job_id = int(args.execution_job_id)
    if not _is_positive_int(run_id):
        print("reason_category=source_execution_run_id_invalid: " + repr(run_id))
        return 1
    if not _is_positive_int(job_id):
        print("reason_category=source_execution_job_id_invalid: " + repr(job_id))
        return 1
    source = dict(trusted_source)
    source["execution_run_id"] = run_id
    source["execution_job_id"] = job_id
    args.output.write_text(json.dumps(source, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return 0


def _cmd_assert_source_tree_matches(args):
    # Untrusted-execution job, pure git-plumbing defense-in-depth check
    # (Issue 1711 AC3): confirms the checked-out source tree's own commit sha
    # / tree sha (read via local `git rev-parse`, never via PR-controlled
    # code) match the trusted-preflight-resolved values, before any pytest
    # execution runs. This never invokes any PR-controlled action or script.
    actual_commit_sha = args.actual_commit_sha
    actual_tree_sha = args.actual_tree_sha
    failures = []
    if actual_commit_sha != args.expected_commit_sha:
        failures.append(
            "commit_sha mismatch: expected " + args.expected_commit_sha + " got " + str(actual_commit_sha)
        )
    if actual_tree_sha != args.expected_tree_sha:
        failures.append(
            "tree_sha mismatch: expected " + args.expected_tree_sha + " got " + str(actual_tree_sha)
        )
    if failures:
        for failure in failures:
            print("reason_category=source_tree_checkout_mismatch: " + failure)
        return 1
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
    p_exec.add_argument("--source-root", type=Path, required=True)
    p_exec.add_argument("--output", type=Path, required=True)
    p_exec.set_defaults(func=_cmd_execute)

    p_rec = sub.add_parser("build-record")
    p_rec.add_argument("--command-id", required=True)
    p_rec.add_argument("--producer-input", type=Path, required=True)
    p_rec.add_argument("--subject-input", type=Path, required=True)
    p_rec.add_argument("--contract-input", type=Path, required=True)
    p_rec.add_argument("--execution-result", type=Path, required=True)
    p_rec.add_argument("--source-input", type=Path, required=True)
    p_rec.add_argument("--schema", type=Path, required=False)
    p_rec.add_argument("--output", type=Path, required=True)
    p_rec.set_defaults(func=_cmd_build_record)

    p_receipt = sub.add_parser("build-receipt")
    p_receipt.add_argument("--record", type=Path, required=True)
    p_receipt.add_argument("--execution-artifact-input", type=Path, required=True)
    p_receipt.add_argument("--final-subject", type=Path, required=True)
    p_receipt.add_argument("--final-contract", type=Path, required=True)
    p_receipt.add_argument("--final-source", type=Path, required=True)
    p_receipt.add_argument("--command-id", required=True)
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

    p_rjbn = sub.add_parser("resolve-job-id-by-name")
    p_rjbn.add_argument("--jobs-file", type=Path, required=True)
    p_rjbn.add_argument("--job-name", required=True)
    p_rjbn.add_argument("--output", type=Path, required=True)
    p_rjbn.set_defaults(func=_cmd_resolve_job_id_by_name)

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
    p_acon.add_argument("--command-id", required=False, default=None)
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

    p_rts = sub.add_parser("resolve-trusted-source")
    p_rts.add_argument("--subject-file", type=Path, required=True)
    p_rts.add_argument("--commit-file", type=Path, required=True)
    p_rts.add_argument("--expected-repository-full-name", required=True)
    p_rts.add_argument("--expected-commit-sha", required=True)
    p_rts.add_argument("--output", type=Path, required=True)
    p_rts.add_argument("--github-output-format", action="store_true")
    p_rts.set_defaults(func=_cmd_resolve_trusted_source)

    p_asrc = sub.add_parser("assemble-source")
    p_asrc.add_argument("--trusted-source-file", type=Path, required=True)
    p_asrc.add_argument("--execution-run-id", required=True)
    p_asrc.add_argument("--execution-job-id", required=True)
    p_asrc.add_argument("--output", type=Path, required=True)
    p_asrc.set_defaults(func=_cmd_assemble_source)

    p_astm = sub.add_parser("assert-source-tree-matches")
    p_astm.add_argument("--actual-commit-sha", required=True)
    p_astm.add_argument("--actual-tree-sha", required=True)
    p_astm.add_argument("--expected-commit-sha", required=True)
    p_astm.add_argument("--expected-tree-sha", required=True)
    p_astm.set_defaults(func=_cmd_assert_source_tree_matches)

    args = parser.parse_args()
    return args.func(args)


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


if __name__ == "__main__":
    raise SystemExit(main())
