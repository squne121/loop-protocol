#!/usr/bin/env python3
"""
Tests for pr_review_marker_archive_exec.py (Issue #1602).

Node IDs referenced by the Issue's Verification Commands:
  AC1: test_rejects_noncanonical_marker_inputs_without_repo_mutation
  AC2: test_archives_only_remote_merged_and_review_id_bound_marker
  AC3: test_failure_state_machine_preserves_or_reports_indeterminate
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_AGENT_OPS_DIR = Path(__file__).resolve().parent.parent
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

import pr_review_marker_archive_exec as archive_exec


REPO = "squne121/loop-protocol"
PR_NUMBER = 1594
REVIEW_ID = 4728229839
EXPECTED_HEAD_SHA = "a" * 40
REVIEW_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}#pullrequestreview-{REVIEW_ID}"


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("placeholder\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _idempotency_key(repo: str, pr_number: int, head_sha: str, body_sha256: str) -> str:
    return f"{repo}:{pr_number}:{head_sha}:{body_sha256}"


def _marker_str(idempotency_key: str) -> str:
    marker_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"{archive_exec.PR_REVIEW_MARKER_PREFIX}{marker_hash}{archive_exec.PR_REVIEW_MARKER_SUFFIX}"


def _write_marker(
    root: Path,
    pr_number: int = PR_NUMBER,
    repo: str = REPO,
    review_id: int = REVIEW_ID,
    review_url: str = REVIEW_URL,
    expected_head_sha: str = EXPECTED_HEAD_SHA,
    idempotency_key: str | None = None,
    schema: str = archive_exec.MARKER_SCHEMA,
    extra: dict | None = None,
) -> Path:
    marker_dir = root / "artifacts" / str(pr_number) / "issue-metadata" / "pr_review.publish"
    marker_dir.mkdir(parents=True, exist_ok=True)
    idempotency_key = idempotency_key or _idempotency_key(
        repo, pr_number, expected_head_sha, "deadbeef" * 8
    )
    data = {
        "schema": schema,
        "pr_number": pr_number,
        "repo": repo,
        "idempotency_key": idempotency_key,
        "expected_head_sha": expected_head_sha,
        "review_id": review_id,
        "review_url": review_url,
        "published_at": "2026-07-01T00:00:00Z",
    }
    if extra:
        data.update(extra)
    marker_path = marker_dir / "pr_review_publish.marker.json"
    marker_path.write_text(json.dumps(data, ensure_ascii=False))
    return marker_path


def _valid_review_body(idempotency_key: str) -> str:
    return f"Looks good.\n\n{_marker_str(idempotency_key)}"


def _successful_gh_caller(idempotency_key: str, state: str = "COMMENTED"):
    def _caller(argv: list[str]):
        if "merge" in argv[-1]:
            return 0, "HTTP/2.0 204 No Content\r\n\r\n", ""
        assert f"reviews/{REVIEW_ID}" in argv[-1]
        body = {
            "id": REVIEW_ID,
            "html_url": REVIEW_URL,
            "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
            "state": state,
            "commit_id": EXPECTED_HEAD_SHA,
            "submitted_at": "2026-07-01T00:05:00Z",
            "body": _valid_review_body(idempotency_key),
        }
        return 0, json.dumps(body), ""

    return _caller


def _unmerged_gh_caller(argv: list[str]):
    if "merge" in argv[-1]:
        return 0, "HTTP/2.0 404 Not Found\r\n\r\n", ""
    raise AssertionError("review endpoint should not be called before merge check")


def _mismatched_review_gh_caller(argv: list[str]):
    if "merge" in argv[-1]:
        return 0, "HTTP/2.0 204 No Content\r\n\r\n", ""
    body = {
        "id": REVIEW_ID + 1,  # wrong id
        "html_url": REVIEW_URL,
        "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
        "state": "COMMENTED",
        "commit_id": EXPECTED_HEAD_SHA,
        "submitted_at": "2026-07-01T00:05:00Z",
        "body": "irrelevant",
    }
    return 0, json.dumps(body), ""


# ---------------------------------------------------------------------------
# AC1: dir-fd / inode-bound, schema-strict input validation. Non-canonical
# inputs must be rejected WITHOUT any repo-local mutation.
# ---------------------------------------------------------------------------


def test_rejects_noncanonical_marker_inputs_without_repo_mutation(tmp_path):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"

    # -- symlink marker: must be rejected via O_NOFOLLOW (never followed, so
    # the executor treats it identically to "no canonical marker present"
    # rather than dereferencing it) -- the symlink itself is left untouched
    # and no repo mutation nor archive write ever happens.
    marker_dir = tmp_path / "artifacts" / str(PR_NUMBER) / "issue-metadata" / "pr_review.publish"
    marker_dir.mkdir(parents=True)
    real_target = tmp_path / "outside_marker.json"
    real_target.write_text(json.dumps({"schema": archive_exec.MARKER_SCHEMA}))
    symlink_path = marker_dir / "pr_review_publish.marker.json"
    os.symlink(real_target, symlink_path)

    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "indeterminate_source_missing"
    assert os.path.islink(symlink_path)  # untouched -- never followed, never removed
    assert not archive_root.exists() or not any(archive_root.rglob("*.archive.json"))

    os.unlink(symlink_path)

    # -- hardlinked marker: must be rejected (nlink != 1), source untouched.
    canonical = _write_marker(tmp_path)
    hardlink_path = marker_dir / "another_name.json"
    os.link(canonical, hardlink_path)

    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "marker_hardlinked_rejected"
    assert canonical.exists()
    original_bytes = canonical.read_bytes()
    os.unlink(hardlink_path)
    assert canonical.read_bytes() == original_bytes  # untouched

    # -- schema mismatch: must be rejected, source untouched.
    _write_marker(tmp_path, schema="SOMETHING_ELSE_V1")
    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "marker_schema_mismatch"
    assert canonical.exists()

    # -- pr_number mismatch inside the JSON body: rejected.
    _write_marker(tmp_path, pr_number=PR_NUMBER, extra={"pr_number": PR_NUMBER + 1})
    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "marker_pr_number_mismatch"

    # -- no archive was ever created across all rejected attempts.
    assert not archive_root.exists() or not any(archive_root.rglob("*.archive.json"))


# ---------------------------------------------------------------------------
# AC2: only a remotely-merged PR with an exact, review_id-bound remote
# review may be archived.
# ---------------------------------------------------------------------------


def test_archives_only_remote_merged_and_review_id_bound_marker(tmp_path):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"

    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, "deadbeef" * 8)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    # -- unmerged PR: refused, marker untouched, nothing archived.
    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "remote_pr_not_merged"
    assert marker_path.exists()
    assert not archive_root.exists() or not any(archive_root.rglob("*.archive.json"))

    # -- merged, but review_id does not resolve to a matching review: refused.
    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_mismatched_review_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "remote_review_id_mismatch"
    assert marker_path.exists()

    # -- merged, review binding fully valid: archived, source removed,
    # archive envelope durable and content-addressed by marker sha256.
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_ARCHIVED
    assert result.archive_durable is True
    assert result.source_present_after == "false"
    assert not marker_path.exists()

    archived_files = list(archive_root.rglob("*.archive.json"))
    assert len(archived_files) == 1
    envelope = json.loads(archived_files[0].read_text())
    assert envelope["schema"] == "PR_REVIEW_MARKER_ARCHIVE_ENVELOPE_V1"
    assert envelope["pr_number"] == PR_NUMBER
    assert envelope["repo"] == REPO
    assert envelope["merged"] is True
    assert result.marker_sha256 == f"sha256:{envelope['marker_sha256'].removeprefix('sha256:')}"

    # -- idempotent retry: source already gone, single matching archive
    # present -> already_archived (not a failure, not a re-fetch of a
    # nonexistent source).
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_ALREADY_ARCHIVED
    assert result.source_present_after == "false"


def test_review_state_and_commit_binding_enforced(tmp_path):
    """AC2: even when review_id matches, wrong state/commit_id must refuse."""
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, "deadbeef" * 8)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key, state="DISMISSED"),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "remote_review_state_mismatch"
    assert marker_path.exists()


# ---------------------------------------------------------------------------
# AC3: honest failure-state classification across the
# SOURCE_VALIDATED -> ARCHIVE_PREPARED -> ARCHIVE_DURABLE -> SOURCE_REMOVED
# -> SOURCE_REMOVAL_DURABLE -> COMMITTED state machine.
# ---------------------------------------------------------------------------


def test_failure_state_machine_preserves_or_reports_indeterminate(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, "deadbeef" * 8)

    # -- failure strictly before ARCHIVE_DURABLE (remote check failure):
    # marker MUST be retained, status must not claim archived.
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)
    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert marker_path.exists()
    assert not archive_root.exists() or not any(archive_root.rglob("*.archive.json"))

    # -- failure AFTER archive is durable, source removal itself fails, but
    # source presence CAN be positively re-confirmed -> source_retained
    # (never silently claim success, never silently lose the marker).
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    def _raise_on_unlink(_validated):
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(archive_exec, "remove_source_with_recheck", _raise_on_unlink)
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_SOURCE_RETAINED
    assert result.archive_durable is True
    assert result.source_present_after == "true"
    assert marker_path.exists()
    monkeypatch.undo()

    # -- reset archive root (avoid collision reuse across sub-cases) and
    # re-establish a fresh marker for the indeterminate case.
    for f in archive_root.rglob("*.archive.json"):
        f.unlink()
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    def _raise_on_unlink_again(_validated):
        raise OSError("simulated unlink failure 2")

    def _presence_unconfirmable(_validated):
        return None  # the confirming stat call itself failed

    monkeypatch.setattr(archive_exec, "remove_source_with_recheck", _raise_on_unlink_again)
    monkeypatch.setattr(archive_exec, "source_still_present", _presence_unconfirmable)
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_INDETERMINATE
    assert result.archive_durable is True
    # The executor must NOT claim "marker retained" when it cannot prove it.
    assert result.status != archive_exec.STATUS_SOURCE_RETAINED
    monkeypatch.undo()

    # -- failure AFTER source removal (directory fsync fails): source is
    # already gone, so the executor must report indeterminate, not
    # source_retained and not a bare success claim.
    for f in archive_root.rglob("*.archive.json"):
        f.unlink()
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    def _raise_on_fsync(_validated):
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(archive_exec, "fsync_parent_dir", _raise_on_fsync)
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_INDETERMINATE
    assert result.reason_code == "source_directory_fsync_failed"
    assert not marker_path.exists()  # unlink itself succeeded before fsync failed


def test_archive_collision_with_different_bytes_is_refused(tmp_path):
    """AC3 P1-2 idempotency matrix: an existing archive at the same
    content-addressed locator but different recorded marker_sha256 must be
    treated as a collision, not silently accepted."""
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, "deadbeef" * 8)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    validated = archive_exec.validate_and_open_marker(tmp_path, PR_NUMBER)
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, validated.sha256)
    os.close(validated.marker_fd)
    os.close(validated.parent_dir_fd)

    bogus_envelope = {
        "schema": "PR_REVIEW_MARKER_ARCHIVE_ENVELOPE_V1",
        "repo": REPO,
        "pr_number": PR_NUMBER,
        "marker_sha256": "sha256:" + ("0" * 64),
    }
    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps(bogus_envelope))

    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "archive_collision_hash_mismatch"
    assert marker_path.exists()


def test_absent_source_without_prior_archive_is_indeterminate(tmp_path):
    """AC3 P1-2 idempotency matrix: source absent + no matching archive
    present -> indeterminate_source_missing, never a bare success claim."""
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "indeterminate_source_missing"


def test_untracked_precondition_refuses_tracked_marker(tmp_path):
    """AC1: a marker that is (unexpectedly) git-tracked must be refused,
    never removed."""
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    marker_path = _write_marker(tmp_path)
    subprocess.run(["git", "add", str(marker_path)], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "accidentally track marker"], cwd=tmp_path, check=True)

    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "git_tracked_file_conflict"
    assert marker_path.exists()


def test_cli_dry_run_never_mutates(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    marker_path = _write_marker(tmp_path)
    monkeypatch.setattr(archive_exec, "DEFAULT_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        archive_exec,
        "resolve_repo",
        lambda explicit_repo, project_root: REPO,
    )
    rc = archive_exec.main(["--pr-number", str(PR_NUMBER), "--dry-run", "--json"])
    assert rc == 0
    assert marker_path.exists()
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == archive_exec.STATUS_REFUSED
    assert out["reason_code"] == "dry_run_no_mutation"
