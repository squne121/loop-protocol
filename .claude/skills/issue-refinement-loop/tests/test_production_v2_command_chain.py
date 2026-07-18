"""
test_production_v2_command_chain.py

Issue #1541 AC7: production E2E through the REAL parent-owned command
chain -- child `compact_review_result.py` CLI -> `emit_parent_review_envelope_v2.py`
production CLI (NOT the test-only `_assemble_v2_envelope()` f-string helper
from `test_parent_replay_isolation_runtime.py`) -> `validate_review_compact_output.py
--v2` CLI -> `reviewer_claim_replay_state_store.py --write-v2` CLI.

Each step is a genuine subprocess invocation of the production script, not a
bare Python function call -- this exercises the real producer -> parent
binding -> emitter -> V2 validator -> state writer chain end to end,
including a tamper case that must fail closed before any state write.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURES_DIR = SKILL_ROOT / "fixtures"
REPO_ROOT = SKILL_ROOT.parent.parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as cr  # noqa: E402

COMPACT_REVIEW_RESULT_SCRIPT = SCRIPTS_DIR / "compact_review_result.py"
PARENT_REPLAY_BINDING_SCRIPT = SCRIPTS_DIR / "parent_replay_binding.py"
EMIT_SCRIPT = SCRIPTS_DIR / "emit_parent_review_envelope_v2.py"
VALIDATE_SCRIPT = SCRIPTS_DIR / "validate_review_compact_output.py"
STATE_STORE_SCRIPT = SCRIPTS_DIR / "reviewer_claim_replay_state_store.py"

_REQUIRED_SCRIPTS = (
    COMPACT_REVIEW_RESULT_SCRIPT,
    PARENT_REPLAY_BINDING_SCRIPT,
    EMIT_SCRIPT,
    VALIDATE_SCRIPT,
    STATE_STORE_SCRIPT,
)
if not all(p.exists() for p in _REQUIRED_SCRIPTS):
    pytest.skip(
        "SKIP: production skill scripts not found -- command chain cannot be started",
        allow_module_level=True,
    )

REPO = "squne121/loop-protocol"
ISSUE_NUMBER = "1541"
SESSION_ID = "session-1541-chain"
ITERATION_ID = "iteration-1541-chain"


def _review_result_needs_fix(body_sha256: str) -> dict:
    raw = json.loads((FIXTURES_DIR / "review_result_needs_fix.json").read_text(encoding="utf-8"))
    raw["body_sha256"] = body_sha256
    raw["blocking_issues"] = [{"code": "missing_section", "message": "missing section"}]
    return raw


def _readiness_lp001(body_sha256: str) -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": body_sha256,
        "errors": [
            {
                "rule_id": "LP001",
                "source_check": "validate_issue_body",
                "category": "body_lint",
                "line_start": 1,
                "line_end": 1,
            }
        ],
    }


def _run_validate_intermediate_v1(*, run_dir: Path, child_stdout_bytes: bytes) -> tuple[int, dict]:
    """Issue #1541 PR #1557 OWNER REQUEST_CHANGES Blocker 1: the ONLY
    sanctioned way to classify/extract fields from raw child stdout BYTES
    -- the independent `review_compact.validate_intermediate_v1` command
    (rendered via `command_registry.render_command()`, never a hand-rolled
    argv list, so the SAME argv shape the real orchestrator would use is
    exercised here too). Never a manual `startswith()` / `json.loads()`
    extraction."""
    argv = cr.render_command("review_compact.validate_intermediate_v1", {"issue_number": ISSUE_NUMBER})
    proc = subprocess.run(
        argv,
        input=child_stdout_bytes,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=15,
    )
    stdout_text = proc.stdout.decode("utf-8")
    return proc.returncode, json.loads(stdout_text)


def _run_child_compact_review_result(*, child_dir: Path, review_result: dict) -> tuple[str, dict]:
    """Real child producer CLI, followed by real independent intermediate
    validation (Blocker 1) -- returns (child_stdout_text, reviewer_blocker_claim)
    where `reviewer_blocker_claim` is parsed from the validator's OWN
    `canonical_reviewer_blocker_claim` output field, never extracted by this
    test file's own `startswith()` / `json.loads()` on the raw child text."""
    input_file = child_dir / "raw_review_result.json"
    input_file.write_text(json.dumps(review_result), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_REVIEW_RESULT_SCRIPT),
            "--input-file",
            str(input_file),
            "--issue-number",
            ISSUE_NUMBER,
            "--repo-root",
            str(child_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(child_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    stdout_text = proc.stdout.rstrip("\n")

    intermediate_rc, intermediate_result = _run_validate_intermediate_v1(
        run_dir=child_dir, child_stdout_bytes=(stdout_text + "\n").encode("utf-8")
    )
    assert intermediate_rc == 0, intermediate_result
    assert intermediate_result["validation_status"] == "valid"
    assert intermediate_result["envelope_kind"] == "needs_fix_intermediate"
    canonical_claim_text = intermediate_result["canonical_reviewer_blocker_claim"]
    assert canonical_claim_text is not None
    claim = json.loads(canonical_claim_text)
    return stdout_text, claim


def _run_parent_replay_binding_process(
    *, parent_dir: Path, reviewer_blocker_claim: dict, readiness_result: dict, current_body_bytes: bytes
) -> dict:
    claim_file = parent_dir / "reviewer_blocker_claim.json"
    readiness_file = parent_dir / "readiness_result.json"
    body_file = parent_dir / "current_body.txt"
    claim_file.write_text(json.dumps(reviewer_blocker_claim), encoding="utf-8")
    readiness_file.write_text(json.dumps(readiness_result), encoding="utf-8")
    body_file.write_bytes(current_body_bytes)

    proc = subprocess.run(
        [
            sys.executable,
            str(PARENT_REPLAY_BINDING_SCRIPT),
            "--reviewer-blocker-claim-file",
            str(claim_file),
            "--readiness-result-file",
            str(readiness_file),
            "--previous-state-inline",
            "{}",
            "--current-body-file",
            str(body_file),
            "--issue-url",
            f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
            "--repository-full-name",
            REPO,
            "--issue-number",
            ISSUE_NUMBER,
            "--refinement-session-id",
            SESSION_ID,
            "--iteration-id",
            ITERATION_ID,
        ],
        capture_output=True,
        text=True,
        cwd=str(parent_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_emit_v2_cli(
    *, run_dir: Path, child_stdout_text: str, binding_artifact: dict, current_body_bytes: bytes
) -> tuple[int, bytes, str]:
    """REAL `emit_parent_review_envelope_v2.py` production CLI (subprocess)
    -- the production replacement for the test-only `_assemble_v2_envelope()`
    f-string helper."""
    binding_file = run_dir / "binding_artifact.json"
    body_file = run_dir / "current_body.txt"
    binding_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
    body_file.write_bytes(current_body_bytes)

    proc = subprocess.run(
        [
            sys.executable,
            str(EMIT_SCRIPT),
            "--issue-number",
            ISSUE_NUMBER,
            "--binding-artifact-file",
            str(binding_file),
            "--repository-full-name",
            REPO,
            "--refinement-session-id",
            SESSION_ID,
            "--iteration-id",
            ITERATION_ID,
            "--current-body-file",
            str(body_file),
        ],
        input=(child_stdout_text + "\n").encode("utf-8"),
        capture_output=True,
        cwd=str(run_dir),
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


def _run_validator_cli_v2(
    *, run_dir: Path, envelope_bytes: bytes, binding_artifact: dict, current_body_bytes: bytes
) -> tuple[int, dict]:
    binding_file = run_dir / "binding_artifact.json"
    body_file = run_dir / "current_body.txt"
    input_file = run_dir / "envelope.txt"
    binding_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
    body_file.write_bytes(current_body_bytes)
    input_file.write_bytes(envelope_bytes)

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SCRIPT),
            "--v2",
            "--issue-number",
            ISSUE_NUMBER,
            "--input-file",
            str(input_file),
            "--binding-artifact-file",
            str(binding_file),
            "--repository-full-name",
            REPO,
            "--refinement-session-id",
            SESSION_ID,
            "--iteration-id",
            ITERATION_ID,
            "--current-body-file",
            str(body_file),
        ],
        capture_output=True,
        text=True,
        cwd=str(run_dir),
        timeout=15,
    )
    return proc.returncode, json.loads(proc.stdout)


def _run_state_write_v2_cli(*, run_dir: Path, validation_result_v2: dict) -> tuple[int, dict]:
    state_dir = run_dir / "state"
    state_dir.mkdir(exist_ok=True)
    normalized_payload = validation_result_v2.get("normalized_payload") or {}
    digest = normalized_payload.get("PARENT_REPLAY_BINDING_DIGEST", "sha256:" + ("0" * 64))
    proc = subprocess.run(
        [
            sys.executable,
            str(STATE_STORE_SCRIPT),
            "--write-v2",
            "--state-dir",
            str(state_dir),
            "--repository-full-name",
            REPO,
            "--issue-number",
            ISSUE_NUMBER,
            "--refinement-session-id",
            SESSION_ID,
            "--validation-result-v2-inline",
            json.dumps(validation_result_v2),
            "--expected-parent-binding-digest",
            digest,
        ],
        capture_output=True,
        text=True,
        cwd=str(run_dir),
        timeout=15,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_production_v2_command_chain(tmp_path: Path):
    """Issue #1541 AC7: the FULL production chain, using the emitter CLI
    (not a test-only assembler), from child stdout through to a persisted
    V2 state file."""
    child_dir = tmp_path / "child_isolation_worktree"
    parent_dir = tmp_path / "parent_owned_inventory"
    emit_dir = tmp_path / "emit_run"
    validate_dir = tmp_path / "validate_run"
    child_dir.mkdir()
    parent_dir.mkdir()
    emit_dir.mkdir()
    validate_dir.mkdir()

    current_body_bytes = b"the current live Issue #1541 body snapshot for the production chain"
    current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
    review_result = _review_result_needs_fix(current_body_sha256)
    readiness_result = _readiness_lp001(current_body_sha256)

    # 1) CHILD: real production CLI, private directory.
    child_stdout_text, reviewer_blocker_claim = _run_child_compact_review_result(
        child_dir=child_dir, review_result=review_result
    )
    assert reviewer_blocker_claim["schema"] == "REVIEWER_BLOCKER_CLAIM_V1"
    assert "findings" not in reviewer_blocker_claim

    # 2) PARENT BINDING: real production CLI, separate directory.
    binding_artifact = _run_parent_replay_binding_process(
        parent_dir=parent_dir,
        reviewer_blocker_claim=reviewer_blocker_claim,
        readiness_result=readiness_result,
        current_body_bytes=current_body_bytes,
    )
    assert binding_artifact["schema"] == "PARENT_REPLAY_BINDING_ARTIFACT_V1"
    assert binding_artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"

    # 3) EMITTER: real production CLI -- NOT `_assemble_v2_envelope()`.
    rc, envelope_bytes, emit_stderr = _run_emit_v2_cli(
        run_dir=emit_dir,
        child_stdout_text=child_stdout_text,
        binding_artifact=binding_artifact,
        current_body_bytes=current_body_bytes,
    )
    assert rc == 0, emit_stderr
    assert emit_stderr == ""
    assert envelope_bytes != b""
    envelope_text = envelope_bytes.decode("utf-8")
    assert envelope_text.count("\n") == 15
    assert "PARENT_REPLAY_BINDING_DIGEST: " + binding_artifact["binding_digest"] in envelope_text

    # 4) VALIDATOR: real production CLI, independent binding artifact copy.
    rc, validation_result = _run_validator_cli_v2(
        run_dir=validate_dir,
        envelope_bytes=envelope_bytes,
        binding_artifact=binding_artifact,
        current_body_bytes=current_body_bytes,
    )
    assert rc == 0, validation_result
    assert validation_result["validation_status"] == "valid"
    assert validation_result["envelope_kind"] == "needs_fix_v2"

    # 5) STATE WRITE: real production CLI.
    rc, write_result = _run_state_write_v2_cli(run_dir=validate_dir, validation_result_v2=validation_result)
    assert rc == 0, write_result
    assert write_result["status"] == "ok"
    state_file = validate_dir / "state" / "reviewer_claim_replay_state.json"
    assert state_file.exists()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted == binding_artifact["replay_next_state"]


def test_production_v2_command_chain_tampered_binding_fails_before_state_write(tmp_path: Path):
    """A tampered binding artifact must be rejected by the emitter itself
    (contract-invalid, exit 1, empty stdout) -- the production chain never
    reaches the validator or the state writer with forged content."""
    child_dir = tmp_path / "child_isolation_worktree"
    parent_dir = tmp_path / "parent_owned_inventory"
    emit_dir = tmp_path / "emit_run"
    child_dir.mkdir()
    parent_dir.mkdir()
    emit_dir.mkdir()

    current_body_bytes = b"the current live Issue #1541 body snapshot for the tamper case"
    current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
    review_result = _review_result_needs_fix(current_body_sha256)
    readiness_result = _readiness_lp001(current_body_sha256)

    child_stdout_text, reviewer_blocker_claim = _run_child_compact_review_result(
        child_dir=child_dir, review_result=review_result
    )
    binding_artifact = _run_parent_replay_binding_process(
        parent_dir=parent_dir,
        reviewer_blocker_claim=reviewer_blocker_claim,
        readiness_result=readiness_result,
        current_body_bytes=current_body_bytes,
    )
    forged_artifact = dict(binding_artifact)
    forged_artifact["binding_digest"] = "sha256:" + "f" * 64

    rc, envelope_bytes, emit_stderr = _run_emit_v2_cli(
        run_dir=emit_dir,
        child_stdout_text=child_stdout_text,
        binding_artifact=forged_artifact,
        current_body_bytes=current_body_bytes,
    )
    assert rc == 1
    assert envelope_bytes == b""
    diagnostic = json.loads(emit_stderr)
    assert diagnostic["schema"] == "EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE"
    assert diagnostic["reason_code"] == "contract_invalid"
    assert any(v["code"] == "binding_artifact_digest_self_inconsistent" for v in diagnostic["violations"])


# ---------------------------------------------------------------------------
# Issue #1541 PR #1557 OWNER REQUEST_CHANGES Blocker 1: negative
# intermediate-validation cases. Each MUST be rejected by
# `review_compact.validate_intermediate_v1` (`validation_status: invalid`,
# `canonical_reviewer_blocker_claim: None`) -- and, since this test file's
# own `_run_child_compact_review_result()` no longer extracts the claim by
# hand, the caller structurally has nothing to feed
# `_run_parent_replay_binding_process()` in this situation. The
# `mock.patch` assertion below independently proves the parent binding
# subprocess is never even attempted for these malformed inputs.
# ---------------------------------------------------------------------------


def _valid_needs_fix_intermediate_lines(tmp_path: Path) -> list[str]:
    child_dir = tmp_path / "child_for_negative_case"
    child_dir.mkdir()
    current_body_bytes = b"body for a Blocker-1 negative intermediate case"
    current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
    review_result = _review_result_needs_fix(current_body_sha256)
    input_file = child_dir / "raw_review_result.json"
    input_file.write_text(json.dumps(review_result), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_REVIEW_RESULT_SCRIPT),
            "--input-file",
            str(input_file),
            "--issue-number",
            ISSUE_NUMBER,
            "--repo-root",
            str(child_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(child_dir),
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rstrip("\n").split("\n")


def _orchestrate_claim_extraction_and_maybe_bind(
    *, run_dir: Path, child_stdout_bytes: bytes, readiness_result: dict, current_body_bytes: bytes
) -> "dict | None":
    """Minimal orchestrator-shaped caller: validates the child intermediate
    FIRST via the real `validate_intermediate_v1` CLI, and calls the real
    parent-binding subprocess ONLY when that validation succeeded --
    mirroring exactly the production ordering this Blocker fixes (child
    stdout bytes -> intermediate validation -> parent binding). Returns the
    binding artifact dict on success, or `None` when the intermediate was
    rejected (in which case the binding subprocess is never invoked)."""
    intermediate_rc, intermediate_result = _run_validate_intermediate_v1(
        run_dir=run_dir, child_stdout_bytes=child_stdout_bytes
    )
    if intermediate_rc != 0 or intermediate_result["validation_status"] != "valid":
        return None
    claim = json.loads(intermediate_result["canonical_reviewer_blocker_claim"])
    return _run_parent_replay_binding_process(
        parent_dir=run_dir,
        reviewer_blocker_claim=claim,
        readiness_result=readiness_result,
        current_body_bytes=current_body_bytes,
    )


def _assert_binding_subprocess_never_started(tmp_path: Path, malformed_text: str) -> dict:
    """Validate `malformed_text` via the real `validate_intermediate_v1` CLI
    (must be rejected), then independently prove no parent-binding
    subprocess is ever launched for it: run the minimal orchestrator-shaped
    caller above with `subprocess.run` patched to raise if it is EVER
    invoked with `PARENT_REPLAY_BINDING_SCRIPT` in argv."""
    run_dir = tmp_path / f"negative_case_{uuid.uuid4().hex}"
    run_dir.mkdir()
    current_body_bytes = b"body for a Blocker-1 negative intermediate case"
    current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
    readiness_result = _readiness_lp001(current_body_sha256)

    real_run = subprocess.run

    def _guarded_run(argv, *args, **kwargs):
        if isinstance(argv, list) and str(PARENT_REPLAY_BINDING_SCRIPT) in [str(a) for a in argv]:
            raise AssertionError(
                "parent_replay_binding.py subprocess must NEVER be started for a "
                "validate_intermediate_v1-rejected child intermediate"
            )
        return real_run(argv, *args, **kwargs)

    with mock.patch("subprocess.run", side_effect=_guarded_run):
        result = _orchestrate_claim_extraction_and_maybe_bind(
            run_dir=run_dir,
            child_stdout_bytes=malformed_text.encode("utf-8"),
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
        )

    assert result is None
    intermediate_rc, intermediate_result = _run_validate_intermediate_v1(
        run_dir=run_dir, child_stdout_bytes=malformed_text.encode("utf-8")
    )
    assert intermediate_rc == 1, intermediate_result
    assert intermediate_result["validation_status"] == "invalid"
    assert intermediate_result["canonical_reviewer_blocker_claim"] is None
    return intermediate_result


def test_intermediate_validation_rejects_duplicate_field_mid_stream(tmp_path: Path):
    """9-line intermediate with a duplicate STATUS field partway through."""
    lines = _valid_needs_fix_intermediate_lines(tmp_path)
    lines.insert(4, lines[0])  # duplicate STATUS somewhere in the middle
    _assert_binding_subprocess_never_started(tmp_path, "\n".join(lines) + "\n")


def test_intermediate_validation_rejects_prose_around_claim_line(tmp_path: Path):
    """Prose injected both before and after an otherwise-valid 9-line
    intermediate (the claim line itself is untouched/valid)."""
    lines = _valid_needs_fix_intermediate_lines(tmp_path)
    text = "Here is my review, please see below:\n" + "\n".join(lines) + "\nThanks for reading.\n"
    _assert_binding_subprocess_never_started(tmp_path, text)


def test_intermediate_validation_rejects_claim_line_recorded_twice(tmp_path: Path):
    """The (valid) REVIEWER_BLOCKER_CLAIM line appears twice."""
    lines = _valid_needs_fix_intermediate_lines(tmp_path)
    claim_line = lines[-1]
    assert claim_line.startswith("REVIEWER_BLOCKER_CLAIM: ")
    lines.append(claim_line)
    _assert_binding_subprocess_never_started(tmp_path, "\n".join(lines) + "\n")


def test_intermediate_validation_rejects_valid_claim_line_out_of_order(tmp_path: Path):
    """Every field is individually well-formed (including the claim line),
    but the claim line is moved earlier than its canonical (last) position
    -- out-of-order field sequence."""
    lines = _valid_needs_fix_intermediate_lines(tmp_path)
    claim_line = lines.pop()
    lines.insert(1, claim_line)
    _assert_binding_subprocess_never_started(tmp_path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Issue #1541 PR #1557 OWNER REQUEST_CHANGES High-3: production E2E must
# exercise the SAME argv `command_registry.render_command()` would hand the
# real orchestrator -- not a hand-rolled `[sys.executable, str(SCRIPT), ...]`
# list constructed independently by this test file. This covers the emit
# step (the step central to Issue #1541's own scope); the registry
# contract test (`test_review_compact_emit_v2_registry_contract.py`)
# separately proves `render_command()` itself produces a safe,
# fully-resolved argv for every other command in this chain.
# ---------------------------------------------------------------------------


def test_production_emit_v2_step_uses_registry_rendered_argv(tmp_path: Path):
    artifact_base = REPO_ROOT / ".claude" / "artifacts" / "issue-refinement-loop" / ISSUE_NUMBER / "_test_high3_registry_argv"
    run_id = uuid.uuid4().hex
    run_dir = artifact_base / run_id
    run_dir.mkdir(parents=True)
    try:
        child_dir = tmp_path / "child_isolation_worktree"
        parent_dir = tmp_path / "parent_owned_inventory"
        child_dir.mkdir()
        parent_dir.mkdir()

        current_body_bytes = b"the current live Issue #1541 body snapshot for the High-3 registry-argv case"
        current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"
        review_result = _review_result_needs_fix(current_body_sha256)
        readiness_result = _readiness_lp001(current_body_sha256)

        child_stdout_text, reviewer_blocker_claim = _run_child_compact_review_result(
            child_dir=child_dir, review_result=review_result
        )
        binding_artifact = _run_parent_replay_binding_process(
            parent_dir=parent_dir,
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness_result,
            current_body_bytes=current_body_bytes,
        )

        binding_file = run_dir / "binding_artifact.json"
        body_file = run_dir / "current_body.txt"
        binding_file.write_text(json.dumps(binding_artifact), encoding="utf-8")
        body_file.write_bytes(current_body_bytes)

        # The argv this test actually executes comes from
        # `command_registry.render_command()` -- the SAME function/entry
        # (`review_compact.emit_v2`) the real orchestrator uses, not a
        # bespoke argv list assembled independently by this test.
        argv = cr.render_command(
            "review_compact.emit_v2",
            {
                "issue_number": ISSUE_NUMBER,
                "binding_artifact_file": str(binding_file.relative_to(REPO_ROOT)),
                "repo": REPO,
                "refinement_session_id": SESSION_ID,
                "iteration_id": ITERATION_ID,
                "current_body_file": str(body_file.relative_to(REPO_ROOT)),
            },
        )
        assert argv[:6] == ["uv", "run", "--locked", "--offline", "--no-sync", "python3"]

        proc = subprocess.run(
            argv,
            input=(child_stdout_text + "\n").encode("utf-8"),
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
        envelope_text = proc.stdout.decode("utf-8")
        assert envelope_text.count("\n") == 15
        assert "PARENT_REPLAY_BINDING_DIGEST: " + binding_artifact["binding_digest"] in envelope_text
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
