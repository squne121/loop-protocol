"""Behavioral coverage for Issue #2200 (control-plane preflight artifact
confinement + bounded cleanup, layered on top of the #2199/#2393
producer/consumer transport).

Covers:

- AC1: the REAL production dispatch entrypoint
  (`skill_runtime_exec._dispatch_child_and_check_postconditions()`) reaches
  the confinement validator for a production preflight profile -- never just
  the helper functions exercised in isolation.
- AC4: confinement bounds (component-wise no-follow, same-fd regular-file
  confirmation, per-file/aggregate size caps, count cap, owner/issue-number
  mismatch detection) reject each anomaly with the fixed reason codes, and a
  legitimate omission (no `ARTIFACT:` projection at all, or a well-formed
  artifact with no ownership field) is never misreported as an error. A
  plain JSON string value is never misclassified as a generated artifact.
- AC5: a confinement/cleanup violation blocks stdout publication entirely
  (the existing "検証 → 公開" ordering is preserved with cleanup folded in),
  while a clean pass publishes stdout and returns the CHILD's own exit code
  unchanged (a `warn`/`needs_fix`-shaped non-zero child exit is never
  conflated with a confinement/cleanup failure).
- Scope: the new checks are a byte-identical no-op for the 2 contract_update
  mutation profiles and for every command_id outside
  `ARTIFACT_CONFINEMENT_COMMAND_IDS`.
- PR #2557 review fix-delta (P1-1): when the artifact self-declares the
  canonical `schema_version` a real `run_refinement_preflight.py` result
  carries, the strengthened provenance check also validates the top-level
  `repo` field (when the caller supplies its own expected `repo`), the
  presence of `repair_action.preflight_run_identity`, and -- when a
  `repair_action.candidate_body_artifact` sidecar is present -- that
  sidecar's digest against `repair_action.repaired_body_sha256`. A
  non-canonical (or absent) `schema_version` keeps the pre-existing
  issue_number-only behavior unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
if str(AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_GUARDS_DIR))

import skill_runtime_exec as exec_mod  # noqa: E402


def _init_git_repo(project_root: Path) -> None:
    """`_dispatch_child_and_check_postconditions()` calls `_git_status_paths()`
    (via `_find_unauthorized_repo_changes()`), which requires a real Git
    repository at `dispatch_root` -- these dispatch-level tests exercise the
    REAL executor entrypoint (AC1), not a stub of it, so a minimal local
    repo is required."""
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    subprocess.run(["git", "init", "-q", str(project_root)], check=True, env=env)


def _make_artifact_dir(project_root: Path, issue_number: int) -> Path:
    artifact_dir = project_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _write_result_artifact(artifact_dir: Path, *, issue_number: "int | None" = None, extra: dict | None = None) -> Path:
    payload: dict = {"schema": "refinement_preflight_result/v1", "status": "pass"}
    if issue_number is not None:
        payload["issue_number"] = issue_number
    if extra:
        payload.update(extra)
    path = artifact_dir / "refinement_preflight_result_v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _FakeSupervision:
    def __init__(self, *, returncode: int, stdout: str) -> None:
        self.timed_out = False
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""
        self.cleanup_scope = exec_mod.CLEANUP_SCOPE_PROCESS_GROUP
        self.cleanup_status = exec_mod.CLEANUP_STATUS_NOT_STARTED
        self.termination = exec_mod.TERMINATION_NOT_NEEDED
        self.leader_reaped = True
        self.pid = None


# ---------------------------------------------------------------------------
# AC4: confinement bounds -- direct unit coverage of
# `_validate_artifact_confinement_bounds()`.
# ---------------------------------------------------------------------------


def test_given_no_artifact_paths_when_confinement_checked_then_legitimate_omission_passes(tmp_path):
    reason, offending = exec_mod._validate_artifact_confinement_bounds(str(tmp_path), "2200", [])
    assert reason is None
    assert offending == []


def test_given_well_formed_matching_owner_artifact_when_confinement_checked_then_accepted(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_result_artifact(artifact_dir, issue_number=2200)

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)]
    )
    assert reason is None
    assert offending == []


def test_given_artifact_with_no_ownership_field_when_confinement_checked_then_legitimate_omission_passes(tmp_path):
    """AC4: an artifact that never declares `issue_number` at all (e.g. a
    plain-text log some other status legitimately produces) must never be
    treated as a mismatch -- only a field that IS present and DOES disagree
    is a violation."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = artifact_dir / "notes.txt"
    artifact_path.write_text("not json at all, and not a generated result artifact", encoding="utf-8")

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)]
    )
    assert reason is None
    assert offending == []


def test_given_json_string_field_when_confinement_checked_then_not_misclassified_as_owner_mismatch(tmp_path):
    """AC4: arbitrary JSON string content (e.g. a `must_read` narrative
    field) must never be scanned for a stray issue number -- only the exact
    top-level `issue_number` field is consulted."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = artifact_dir / "custom.json"
    artifact_path.write_text(
        json.dumps({"must_read": "see issue_number 9999 for context", "status": "pass"}),
        encoding="utf-8",
    )

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)]
    )
    assert reason is None
    assert offending == []


def test_given_symlinked_artifact_when_confinement_checked_then_artifact_escape_rejected(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    real_target = tmp_path / "outside_secret.json"
    real_target.write_text(json.dumps({"issue_number": 2200}), encoding="utf-8")
    symlink_path = artifact_dir / "refinement_preflight_result_v1.json"
    symlink_path.symlink_to(real_target)

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(symlink_path)]
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE
    assert offending == [str(symlink_path)]


def test_given_symlinked_ancestor_component_when_confinement_checked_then_artifact_escape_rejected(tmp_path):
    """AC4: the no-follow walk must be COMPONENT-wise, not just a check on
    the leaf. A symlinked directory ancestor is rejected even though the
    leaf filename itself is an ordinary regular file."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    real_subdir = tmp_path / "real_subdir"
    real_subdir.mkdir()
    (real_subdir / "result.json").write_text(json.dumps({"issue_number": 2200}), encoding="utf-8")
    symlinked_subdir = artifact_dir / "linked_subdir"
    symlinked_subdir.symlink_to(real_subdir)
    leaf = symlinked_subdir / "result.json"

    reason, offending = exec_mod._validate_artifact_confinement_bounds(str(tmp_path), "2200", [str(leaf)])
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE


def test_given_non_regular_file_when_confinement_checked_then_artifact_escape_rejected(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    fifo_path = artifact_dir / "refinement_preflight_result_v1.json"
    os.mkfifo(fifo_path)

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(fifo_path)]
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE


def test_given_oversized_single_file_when_confinement_checked_then_artifact_oversized_rejected(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    oversized_path = artifact_dir / "oversized.json"
    oversized_path.write_bytes(b"0" * (exec_mod.ARTIFACT_CONFINEMENT_MAX_FILE_BYTES + 1))

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(oversized_path)]
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED


def test_given_too_many_artifact_paths_when_confinement_checked_then_artifact_oversized_rejected(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    paths = []
    for i in range(exec_mod.ARTIFACT_CONFINEMENT_MAX_COUNT + 1):
        p = artifact_dir / f"artifact_{i}.json"
        p.write_text(json.dumps({"issue_number": 2200}), encoding="utf-8")
        paths.append(str(p))

    reason, _offending = exec_mod._validate_artifact_confinement_bounds(str(tmp_path), "2200", paths)
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED


def test_given_mismatched_owner_field_when_confinement_checked_then_stale_artifact_rejected(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_result_artifact(artifact_dir, issue_number=9999)

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)]
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT
    assert offending == [str(artifact_path)]


# ---------------------------------------------------------------------------
# PR #2557 review fix-delta (P1-1): strengthened provenance checks for an
# artifact that self-declares the CANONICAL `schema_version` a real
# `run_refinement_preflight.py` result actually carries (never the
# test-only `"schema"` key the OTHER fixtures above use, which the real
# producer never emits and which this strengthened check therefore never
# recognizes -- keeping every existing test above byte-identical).
# ---------------------------------------------------------------------------

_CANONICAL_ORIGINAL_BODY = "original body\n"
_CANONICAL_REPAIRED_BODY = "repaired body\n"


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_canonical_result_with_repair_action(
    artifact_dir: Path,
    *,
    issue_number: int = 2200,
    repo: "str | None" = "squne121/loop-protocol",
    preflight_run_identity: "str | None" = "sha256:testrun",
    candidate_body_artifact: "Path | None | str" = "__default__",
    repaired_body_sha256: "str | None" = "__default__",
) -> Path:
    """Canonical-shaped fixture (Issue #2200 PR #2557 review item 3): unlike
    `_write_result_artifact()` above (test-only `"schema"` key), this uses
    the EXACT field names the real producer (`run_refinement_preflight.py`'s
    `_build_result()`/`SCHEMA_VERSION_RESULT`) emits at the top level
    (`schema_version`, `status`, `issue_number`, `repo`, `hashes`) and
    nested under `repair_action` (`preflight_run_identity`,
    `candidate_body_artifact`, `repaired_body_sha256`), so the NEW
    provenance-validating tests below are never accidentally green against
    a non-canonical shape."""
    candidate_path: "Path | None"
    if candidate_body_artifact == "__default__":
        candidate_path = artifact_dir / "candidate_body.md"
        candidate_path.write_text(_CANONICAL_REPAIRED_BODY, encoding="utf-8")
    else:
        candidate_path = candidate_body_artifact  # type: ignore[assignment]

    if repaired_body_sha256 == "__default__":
        repaired_body_sha256 = f"sha256:{_hex(_CANONICAL_REPAIRED_BODY)}"

    repair_action: dict = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": f"sha256:{_hex(_CANONICAL_ORIGINAL_BODY)}",
        "repaired_body_sha256": repaired_body_sha256,
        "candidate_body_artifact": str(candidate_path) if candidate_path is not None else None,
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["trailing_whitespace_stripped"],
        "source_lane": "unanchored",
        "preflight_run_identity": preflight_run_identity,
        "original_updated_at": "2024-01-01T00:00:00Z",
        "source_refs_digest": None,
    }
    payload: dict = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "needs_fix",
        "issue_number": issue_number,
        "hashes": {"result_core_sha256": "sha256:testrun"},
        "repair_action": repair_action,
    }
    if repo is not None:
        payload["repo"] = repo
    result_path = artifact_dir / "refinement_preflight_result_v1.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    return result_path


def test_given_canonical_schema_version_and_matching_fields_when_confinement_checked_then_accepted(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_canonical_result_with_repair_action(artifact_dir, issue_number=2200)

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)], repo="squne121/loop-protocol"
    )
    assert reason is None
    assert offending == []


def test_given_canonical_schema_version_repo_mismatch_when_confinement_checked_then_stale_artifact_rejected(tmp_path):
    """AC4 strengthening: a canonical-schema artifact that declares a
    FOREIGN `repo` is rejected exactly like an `issue_number` mismatch,
    when the caller supplies its own expected `repo`."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_canonical_result_with_repair_action(
        artifact_dir, issue_number=2200, repo="someone-else/other-repo"
    )

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)], repo="squne121/loop-protocol"
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT
    assert offending == [str(artifact_path)]


def test_given_no_expected_repo_supplied_when_confinement_checked_then_repo_field_never_consulted(tmp_path):
    """Backward compatibility: a caller that does not know its own expected
    `repo` (`repo=None`, the default) never has the artifact's declared
    `repo` field consulted at all -- a foreign `repo` value is a legitimate
    omission from THIS caller's point of view, never a mismatch."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_canonical_result_with_repair_action(
        artifact_dir, issue_number=2200, repo="someone-else/other-repo"
    )

    reason, offending = exec_mod._validate_artifact_confinement_bounds(str(tmp_path), "2200", [str(artifact_path)])
    assert reason is None
    assert offending == []


def test_given_canonical_schema_version_missing_run_identity_when_confinement_checked_then_stale_artifact_rejected(
    tmp_path,
):
    """AC4 strengthening: a canonical-schema artifact's `repair_action` that
    omits `preflight_run_identity` (an already-schema'd provenance field
    every real production result populates) can never be trusted as
    fresh."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_canonical_result_with_repair_action(
        artifact_dir, issue_number=2200, preflight_run_identity=None
    )

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)], repo="squne121/loop-protocol"
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT
    assert offending == [str(artifact_path)]


def test_given_canonical_schema_version_candidate_digest_mismatch_when_confinement_checked_then_stale_artifact_rejected(
    tmp_path,
):
    """AC4 strengthening: when `repair_action.candidate_body_artifact` is
    present, its digest is verified against `repair_action.repaired_body_sha256`
    -- a sidecar whose actual content disagrees with the recorded digest is
    rejected as stale, even though the artifact's own `issue_number`/`repo`
    match."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_canonical_result_with_repair_action(artifact_dir, issue_number=2200)
    # Corrupt the candidate body AFTER the digest was already recorded.
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text("a completely different body\n", encoding="utf-8")

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)], repo="squne121/loop-protocol"
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT
    assert offending == [str(artifact_path)]


def test_given_canonical_schema_version_missing_candidate_body_when_confinement_checked_then_stale_artifact_rejected(
    tmp_path,
):
    """AC4 strengthening: `repair_action.candidate_body_artifact` pointing
    at a non-existent path (e.g. already cleaned up, or never actually
    written) fails closed rather than being silently skipped."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    missing_candidate = artifact_dir / "never_written_candidate.md"
    artifact_path = _write_canonical_result_with_repair_action(
        artifact_dir, issue_number=2200, candidate_body_artifact=missing_candidate
    )

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)], repo="squne121/loop-protocol"
    )
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT
    assert offending == [str(artifact_path)]


def test_given_non_canonical_schema_version_when_confinement_checked_then_strengthened_checks_skipped(tmp_path):
    """Scope guard: an artifact whose `schema_version` is present but is
    NOT the exact canonical value never reaches the strengthened
    `preflight_run_identity`/digest checks -- only the pre-existing,
    schema-independent `issue_number`/`repo` top-level checks apply,
    byte-identical to before this fix_delta. This artifact's own
    `repair_action.preflight_run_identity` is absent (which WOULD be
    rejected if this were treated as the canonical schema), proving the
    strengthened path is genuinely skipped rather than coincidentally
    passing."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = artifact_dir / "repair_action_only.json"
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": "repair_action/v1",
                "issue_number": 2200,
                "repo": "squne121/loop-protocol",
                "repair_action": {"disposition": "auto_apply_safe"},
            }
        ),
        encoding="utf-8",
    )

    reason, offending = exec_mod._validate_artifact_confinement_bounds(
        str(tmp_path), "2200", [str(artifact_path)], repo="squne121/loop-protocol"
    )
    assert reason is None
    assert offending == []


def test_given_production_dispatch_and_repo_mismatch_when_dispatched_then_publish_blocked(
    tmp_path, monkeypatch, capsys
):
    """AC1/AC5 end-to-end: the REAL `_dispatch_child_and_check_postconditions()`
    entrypoint, given its own known `repo`, rejects a canonical-schema
    artifact declaring a foreign `repo` BEFORE stdout publication."""
    _init_git_repo(tmp_path)
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_canonical_result_with_repair_action(
        artifact_dir, issue_number=2200, repo="someone-else/other-repo"
    )
    stdout = f"STATUS: needs_fix\nARTIFACT:\n  refinement_preflight_result_v1: {artifact_path}\n"

    monkeypatch.setattr(
        exec_mod,
        "_run_child_with_supervision",
        lambda *a, **k: _FakeSupervision(returncode=0, stdout=stdout),
    )

    exit_code = exec_mod._dispatch_child_and_check_postconditions(
        dispatch_root=str(tmp_path),
        issue_number=2200,
        command_id="preflight.run",
        child_argv=["true"],
        env={},
        timeout_seconds=5.0,
        binary_output=False,
        repo="squne121/loop-protocol",
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "STATUS: needs_fix" not in captured.out
    assert "reason_code=stale_artifact" in captured.err


# ---------------------------------------------------------------------------
# Category 1 (In Scope): bounded cleanup of unpublished failed-run leftovers.
# ---------------------------------------------------------------------------


def test_given_no_leftovers_when_cleanup_run_then_trivial_success(tmp_path):
    ok, reason, removed = exec_mod._bounded_cleanup_stale_artifact_leftovers(
        str(tmp_path), "2200", "preflight.run", not_before_mtime=0.0
    )
    assert ok is True
    assert reason is None
    assert removed == []


def test_given_stale_scratch_temp_leftover_when_cleanup_run_then_removed(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    leftover = artifact_dir / ".refinement_preflight_result_v1.json.abc123.tmp"
    leftover.write_text("{}", encoding="utf-8")
    # Force the leftover's mtime safely into the past relative to "now".
    past = 1.0
    os.utime(leftover, (past, past))

    ok, reason, removed = exec_mod._bounded_cleanup_stale_artifact_leftovers(
        str(tmp_path), "2200", "preflight.run", not_before_mtime=2_000_000_000.0
    )
    assert ok is True
    assert reason is None
    assert removed == [str(leftover)]
    assert not leftover.exists()


def test_given_in_flight_scratch_temp_when_cleanup_run_then_not_removed(tmp_path):
    """A scratch file whose mtime is at/after `not_before_mtime` belongs to
    THIS span's own still-running child and must never be removed."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    in_flight = artifact_dir / ".refinement_preflight_result_v1.json.def456.tmp"
    in_flight.write_text("{}", encoding="utf-8")

    ok, reason, removed = exec_mod._bounded_cleanup_stale_artifact_leftovers(
        str(tmp_path), "2200", "preflight.run", not_before_mtime=0.0
    )
    assert ok is True
    assert reason is None
    assert removed == []
    assert in_flight.exists()


def test_given_published_result_artifact_when_cleanup_run_then_never_touched(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    published = _write_result_artifact(artifact_dir, issue_number=2200)
    os.utime(published, (1.0, 1.0))

    ok, reason, removed = exec_mod._bounded_cleanup_stale_artifact_leftovers(
        str(tmp_path), "2200", "preflight.run", not_before_mtime=2_000_000_000.0
    )
    assert ok is True
    assert reason is None
    assert removed == []
    assert published.exists()


def test_given_symlinked_scratch_leftover_when_cleanup_run_then_artifact_escape_rejected(tmp_path):
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    real_target = tmp_path / "outside.tmp"
    real_target.write_text("x", encoding="utf-8")
    leftover_symlink = artifact_dir / ".refinement_preflight_result_v1.json.evil.tmp"
    leftover_symlink.symlink_to(real_target)

    ok, reason, removed = exec_mod._bounded_cleanup_stale_artifact_leftovers(
        str(tmp_path), "2200", "preflight.run", not_before_mtime=2_000_000_000.0
    )
    assert ok is False
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE
    assert removed == []
    assert real_target.exists()


def test_given_directory_scan_budget_exceeded_by_non_matching_entries_when_cleanup_run_then_artifact_oversized_rejected(
    tmp_path,
):
    """PR #2557 review fix-delta (P2-4): the directory-scan cost itself is
    bounded, independent of `ARTIFACT_CONFINEMENT_MAX_COUNT`'s post-filter
    candidate-count cap. A directory containing more than
    `_LEFTOVER_SCRATCH_DIRECTORY_SCAN_BUDGET` entries fails closed as
    oversized even when ZERO of those entries actually match the leftover
    scratch-temp naming pattern (i.e. this is a scan-cost bound, never a
    candidate-count bound in disguise)."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    for i in range(exec_mod._LEFTOVER_SCRATCH_DIRECTORY_SCAN_BUDGET + 1):
        (artifact_dir / f"unrelated_{i}.json").write_text("{}", encoding="utf-8")

    ok, reason, removed = exec_mod._bounded_cleanup_stale_artifact_leftovers(
        str(tmp_path), "2200", "preflight.run", not_before_mtime=2_000_000_000.0
    )
    assert ok is False
    assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED
    assert removed == []


def test_given_directory_within_scan_budget_when_cleanup_run_then_unaffected(tmp_path):
    """Non-regression: a directory with entries under the scan budget (and
    zero matching leftovers) still trivially succeeds -- the new bounded
    scan changes cost characteristics only, never behavior within bounds."""
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    for i in range(exec_mod._LEFTOVER_SCRATCH_DIRECTORY_SCAN_BUDGET - 1):
        (artifact_dir / f"unrelated_{i}.json").write_text("{}", encoding="utf-8")

    ok, reason, removed = exec_mod._bounded_cleanup_stale_artifact_leftovers(
        str(tmp_path), "2200", "preflight.run", not_before_mtime=2_000_000_000.0
    )
    assert ok is True
    assert reason is None
    assert removed == []


# ---------------------------------------------------------------------------
# AC1/AC5: the REAL production dispatch entrypoint
# (`_dispatch_child_and_check_postconditions`) reaches the confinement
# validator, preserves the existing "検証 → 公開" ordering, and separates
# confinement/cleanup failure from the child's own control-result exit code.
# ---------------------------------------------------------------------------


def test_given_production_profile_and_stale_artifact_when_dispatched_then_publish_blocked(
    tmp_path, monkeypatch, capsys
):
    _init_git_repo(tmp_path)
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_result_artifact(artifact_dir, issue_number=9999)
    stdout = f"STATUS: needs_fix\nARTIFACT:\n  refinement_preflight_result_v1: {artifact_path}\n"

    monkeypatch.setattr(
        exec_mod,
        "_run_child_with_supervision",
        lambda *a, **k: _FakeSupervision(returncode=0, stdout=stdout),
    )

    exit_code = exec_mod._dispatch_child_and_check_postconditions(
        dispatch_root=str(tmp_path),
        issue_number=2200,
        command_id="preflight.run",
        child_argv=["true"],
        env={},
        timeout_seconds=5.0,
        binary_output=False,
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "STATUS: needs_fix" not in captured.out
    assert "ARTIFACT:" not in captured.out
    assert "reason_code=stale_artifact" in captured.err


def test_given_production_profile_and_clean_artifact_when_dispatched_then_child_returncode_preserved(
    tmp_path, monkeypatch, capsys
):
    """AC5: a `needs_fix`-shaped non-zero-looking (but here zero-returncode,
    text-projected) control result is a legitimate outcome distinct from a
    confinement/cleanup failure -- confinement PASSING publishes stdout and
    returns the child's own returncode unchanged, whatever it is."""
    _init_git_repo(tmp_path)
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_result_artifact(artifact_dir, issue_number=2200)
    stdout = f"STATUS: needs_fix\nARTIFACT:\n  refinement_preflight_result_v1: {artifact_path}\n"

    monkeypatch.setattr(
        exec_mod,
        "_run_child_with_supervision",
        lambda *a, **k: _FakeSupervision(returncode=3, stdout=stdout),
    )

    exit_code = exec_mod._dispatch_child_and_check_postconditions(
        dispatch_root=str(tmp_path),
        issue_number=2200,
        command_id="preflight.run",
        child_argv=["true"],
        env={},
        timeout_seconds=5.0,
        binary_output=False,
    )

    assert exit_code == 3
    captured = capsys.readouterr()
    assert "STATUS: needs_fix" in captured.out
    assert str(artifact_path) in captured.out


def test_given_production_profile_and_leftover_scratch_when_dispatched_then_leftover_removed_after_publish_gate(
    tmp_path, monkeypatch, capsys
):
    """A pre-existing stale `.tmp` scratch leftover from a prior, crashed
    dispatch is removed as part of THIS dispatch's postcondition pass, and
    a genuinely clean current run still publishes normally."""
    _init_git_repo(tmp_path)
    artifact_dir = _make_artifact_dir(tmp_path, 2200)
    artifact_path = _write_result_artifact(artifact_dir, issue_number=2200)
    leftover = artifact_dir / ".refinement_preflight_result_v1.json.stale123.tmp"
    leftover.write_text("{}", encoding="utf-8")
    os.utime(leftover, (1.0, 1.0))
    stdout = f"STATUS: pass\nARTIFACT:\n  refinement_preflight_result_v1: {artifact_path}\n"

    monkeypatch.setattr(
        exec_mod,
        "_run_child_with_supervision",
        lambda *a, **k: _FakeSupervision(returncode=0, stdout=stdout),
    )

    exit_code = exec_mod._dispatch_child_and_check_postconditions(
        dispatch_root=str(tmp_path),
        issue_number=2200,
        command_id="preflight.run",
        child_argv=["true"],
        env={},
        timeout_seconds=5.0,
        binary_output=False,
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "STATUS: pass" in captured.out
    assert not leftover.exists()


def test_given_contract_update_profile_and_owner_mismatch_artifact_when_dispatched_then_confinement_is_noop(
    tmp_path, monkeypatch, capsys
):
    """Scope guard: the 2 contract_update mutation profiles never run the
    NEW confinement/cleanup checks -- only their existing #2393 allowed-root
    check applies. An artifact with a mismatched `issue_number` field is
    accepted here (a byte-identical no-op vs. pre-#2200 behavior) because
    `_validate_artifact_confinement_bounds` is never invoked for this
    command_id."""
    _init_git_repo(tmp_path)
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "2200"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "refinement_preflight_result_v1.json"
    artifact_path.write_text(json.dumps({"issue_number": 9999}), encoding="utf-8")
    stdout = f"STATUS: pass\nARTIFACT:\n  refinement_preflight_result_v1: {artifact_path}\n"

    monkeypatch.setattr(
        exec_mod,
        "_run_child_with_supervision",
        lambda *a, **k: _FakeSupervision(returncode=0, stdout=stdout),
    )

    exit_code = exec_mod._dispatch_child_and_check_postconditions(
        dispatch_root=str(tmp_path),
        issue_number=2200,
        command_id="contract_update.run.with_anchor",
        child_argv=["true"],
        env={},
        timeout_seconds=5.0,
        binary_output=False,
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "STATUS: pass" in captured.out


def test_given_command_id_outside_confinement_scope_when_checked_then_excluded():
    assert "repair_action.apply" not in exec_mod.ARTIFACT_CONFINEMENT_COMMAND_IDS
    assert "contract_update.run.with_anchor" not in exec_mod.ARTIFACT_CONFINEMENT_COMMAND_IDS
    assert "contract_update.run.with_human_context" not in exec_mod.ARTIFACT_CONFINEMENT_COMMAND_IDS
    for command_id in (
        "preflight.run",
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
    ):
        assert command_id in exec_mod.ARTIFACT_CONFINEMENT_COMMAND_IDS


@pytest.mark.parametrize(
    "reason_code",
    [
        exec_mod.ARTIFACT_CONFINEMENT_REASON_CLEANUP_FAILED,
        exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE,
        exec_mod.ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED,
        exec_mod.ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT,
    ],
)
def test_given_each_fixed_reason_code_when_emitted_then_stderr_carries_it_and_exit_is_2(reason_code, capsys):
    exit_code = exec_mod._emit_artifact_confinement_failure(2200, reason_code, ["some/path"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert f"reason_code={reason_code}" in captured.err
    assert "target_issue=2200" in captured.err
