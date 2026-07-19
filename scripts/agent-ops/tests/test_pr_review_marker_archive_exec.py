#!/usr/bin/env python3
"""
Tests for pr_review_marker_archive_exec.py (Issue #1602).

Node IDs referenced by the Issue's Verification Commands:
  AC1: test_rejects_noncanonical_marker_inputs_without_repo_mutation
  AC2: test_archives_only_remote_merged_and_review_id_bound_marker
  AC3: test_failure_state_machine_preserves_or_reports_indeterminate
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_AGENT_OPS_DIR = Path(__file__).resolve().parent.parent
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

import pr_review_marker_archive_exec as archive_exec


REPO = "squne121/loop-protocol"
PR_NUMBER = 1594
REVIEW_ID = 4728229839
EXPECTED_HEAD_SHA = "a" * 40
REVIEW_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}#pullrequestreview-{REVIEW_ID}"
AUTHENTICATED_LOGIN = "review-bot"


def _init_git_repo(root: Path, origin_repo: str = REPO) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("placeholder\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    if origin_repo:
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/{origin_repo}.git"],
            cwd=root,
            check=True,
        )


def _idempotency_key(repo: str, pr_number: int, head_sha: str, body_sha256: str) -> str:
    return f"{repo}:{pr_number}:{head_sha}:{body_sha256}"


def _body_sha256(raw_body: str) -> str:
    return hashlib.sha256(raw_body.encode("utf-8")).hexdigest()


# The producer (controlled_skill_mutation_exec.py) constructs
# idempotency_key's trailing component as sha256(raw_body) -- the SAME raw
# body the rendered review body is built from (rendered = raw_body + "\n\n"
# + marker_str). Fixtures must keep this real, or the archive executor's
# recompute-and-compare (PR #1628 review P0-4) fails universally.
DEFAULT_RAW_BODY = "Looks good."
DEFAULT_BODY_SHA256 = _body_sha256(DEFAULT_RAW_BODY)


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
        repo, pr_number, expected_head_sha, DEFAULT_BODY_SHA256
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


def _valid_review_body(idempotency_key: str, raw_body: str = DEFAULT_RAW_BODY) -> str:
    return f"{raw_body}\n\n{_marker_str(idempotency_key)}"


def _successful_gh_caller(
    idempotency_key: str,
    state: str = "COMMENTED",
    review_author_login: str = AUTHENTICATED_LOGIN,
    authenticated_login: str = AUTHENTICATED_LOGIN,
    body: str | None = None,
):
    def _caller(argv: list[str]):
        if "merge" in argv[-1]:
            return 0, "HTTP/2.0 204 No Content\r\n\r\n", ""
        if argv[-1] == "user":
            return 0, json.dumps({"login": authenticated_login}), ""
        assert f"reviews/{REVIEW_ID}" in argv[-1]
        review_body = body if body is not None else _valid_review_body(idempotency_key)
        review = {
            "id": REVIEW_ID,
            "html_url": REVIEW_URL,
            "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
            "state": state,
            "commit_id": EXPECTED_HEAD_SHA,
            "submitted_at": "2026-07-01T00:05:00Z",
            "body": review_body,
            "user": {"login": review_author_login},
        }
        return 0, json.dumps(review), ""

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
        "user": {"login": AUTHENTICATED_LOGIN},
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

    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
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
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
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
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)

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
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    validated = archive_exec.validate_and_open_marker(tmp_path, PR_NUMBER)
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, validated.sha256)
    os.close(validated.marker_fd)
    os.close(validated.parent_dir_fd)

    bogus_envelope = {
        "schema": archive_exec.ARCHIVE_ENVELOPE_SCHEMA,
        "repo": REPO,
        "pr_number": PR_NUMBER,
        "source_relpath": archive_exec._source_relpath(PR_NUMBER),
        "marker_sha256": "sha256:" + ("0" * 64),
        "expected_head_sha": EXPECTED_HEAD_SHA,
        "idempotency_key": idempotency_key,
        "archived_at": "2026-01-01T00:00:00Z",
        "executor_version": "1",
        "merged": True,
        "review": {"id": REVIEW_ID},
    }
    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True, mode=0o700)
    dest.write_text(json.dumps(bogus_envelope))
    os.chmod(dest, 0o600)

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


# ---------------------------------------------------------------------------
# PR #1628 review P0-2: repo/origin binding enforced independently of the
# executor's own remote-merge/review checks.
# ---------------------------------------------------------------------------


def test_origin_binding_mismatch_refuses_before_any_mutation(tmp_path):
    """A worktree whose `origin` remote does not resolve to the declared
    `repo` must be refused before any marker validation or remote call."""
    _init_git_repo(tmp_path, origin_repo="someone-else/other-repo")
    archive_root = tmp_path / "state-root"
    marker_path = _write_marker(tmp_path)

    def _never_called(argv):
        raise AssertionError("gh must never be called before origin binding is verified")

    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_never_called, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "repo_origin_binding_mismatch"
    assert marker_path.exists()


def test_origin_untrusted_host_refused(tmp_path):
    """A non-github.com / non-canonical-scheme origin must be refused."""
    _init_git_repo(tmp_path, origin_repo=None)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://evil.example.com/squne121/loop-protocol.git"],
        cwd=tmp_path,
        check=True,
    )
    archive_root = tmp_path / "state-root"
    _write_marker(tmp_path)
    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_unmerged_gh_caller, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "repo_origin_untrusted_host_or_scheme"


# ---------------------------------------------------------------------------
# PR #1628 review P0-2 (required test 3): gh/git are resolved ONLY from a
# fixed, trusted PATH list -- a fake gh/git prepended to the ambient PATH
# must never be consulted, regardless of what the environment looks like.
# ---------------------------------------------------------------------------


def test_fake_gh_on_ambient_path_is_never_resolved(tmp_path, monkeypatch):
    fake_bin_dir = tmp_path / "fake_bin"
    fake_bin_dir.mkdir()
    fake_gh = fake_bin_dir / "gh"
    fake_gh.write_text("#!/bin/sh\necho MALICIOUS_GH_INVOKED\nexit 1\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    # -- Negative: the trusted-path resolver must never return the fake
    # binary, no matter where it sits in the ambient PATH.
    resolved = archive_exec._resolve_trusted_bin("gh")
    assert resolved is None or Path(resolved).parent != fake_bin_dir

    # -- Positive control: the SAME resolver function correctly finds a
    # binary when the fixture directory is explicitly passed as the trusted
    # list, proving the negative result above is due to PATH-independence
    # and not e.g. a broken which() call.
    resolved_trusted = archive_exec._resolve_trusted_bin("gh", trusted_path_dirs=str(fake_bin_dir))
    assert resolved_trusted == str(fake_gh)


def test_fake_git_on_ambient_path_is_never_resolved(tmp_path, monkeypatch):
    fake_bin_dir = tmp_path / "fake_bin_git"
    fake_bin_dir.mkdir()
    fake_git = fake_bin_dir / "git"
    fake_git.write_text("#!/bin/sh\necho MALICIOUS_GIT_INVOKED\nexit 0\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    resolved = archive_exec._resolve_trusted_bin("git")
    assert resolved is None or Path(resolved).parent != fake_bin_dir


def test_gh_subprocess_env_is_sanitized():
    env = archive_exec._build_sanitized_subprocess_env()
    for key in archive_exec._ENV_STRIP_KEYS:
        assert key not in env
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"


# ---------------------------------------------------------------------------
# PR #1628 review P0-1 (required tests 1 & 2): production resolver against
# `$HOME/.local/state`, and repo-internal / relative / symlink / other-UID /
# non-private rejection.
# ---------------------------------------------------------------------------


def test_production_home_state_resolver_succeeds_even_when_home_parent_is_root_owned(tmp_path):
    """The nearest-existing-ancestor boundary is $HOME itself (owned by the
    test process); ancestors ABOVE it (e.g. `/home` on a real multi-user
    Linux/WSL box, which is typically root-owned) must never be required to
    match the current uid."""
    fake_home = tmp_path / "home_dir"
    fake_home.mkdir()
    fake_project_root = tmp_path / "unrelated_repo"
    fake_project_root.mkdir()

    root = archive_exec.resolve_archive_root(fake_project_root, env={"HOME": str(fake_home)})

    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert str(root).startswith(str(fake_home))
    # Intermediate ancestors created by the resolver (.local, state, ...)
    # must also be private-mode and owned by the current user.
    for ancestor in (fake_home / ".local", fake_home / ".local" / "state"):
        assert stat.S_IMODE(ancestor.stat().st_mode) == 0o700
        assert ancestor.stat().st_uid == os.getuid()


def test_archive_root_inside_repository_is_refused(tmp_path):
    fake_project_root = tmp_path / "repo"
    fake_project_root.mkdir()
    state_home_inside_repo = fake_project_root / "state-inside-repo"

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.resolve_archive_root(
            fake_project_root, env={"XDG_STATE_HOME": str(state_home_inside_repo)}
        )
    assert excinfo.value.reason_code == "archive_root_inside_repository"


def test_archive_root_relative_xdg_state_home_refused(tmp_path):
    fake_project_root = tmp_path / "repo"
    fake_project_root.mkdir()
    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.resolve_archive_root(
            fake_project_root, env={"XDG_STATE_HOME": "relative/state/path"}
        )
    assert excinfo.value.reason_code == "archive_root_locator_not_absolute"


def test_archive_root_symlink_xdg_state_home_refused(tmp_path):
    fake_project_root = tmp_path / "repo"
    fake_project_root.mkdir()
    real_target = tmp_path / "real_state_target"
    real_target.mkdir()
    symlinked_state_home = tmp_path / "symlinked_state_home"
    os.symlink(real_target, symlinked_state_home)

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.resolve_archive_root(
            fake_project_root, env={"XDG_STATE_HOME": str(symlinked_state_home)}
        )
    assert excinfo.value.reason_code == "archive_root_locator_is_symlink"


def test_archive_root_other_uid_ancestor_refused(tmp_path, monkeypatch):
    fake_project_root = tmp_path / "repo"
    fake_project_root.mkdir()
    fake_home = tmp_path / "other_uid_home"
    fake_home.mkdir()

    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 999_999)
    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.resolve_archive_root(fake_project_root, env={"HOME": str(fake_home)})
    assert excinfo.value.reason_code == "archive_root_ancestor_owner_mismatch"


def test_archive_root_non_private_leaf_mode_refused(tmp_path):
    fake_project_root = tmp_path / "repo"
    fake_project_root.mkdir()
    state_home = tmp_path / "state_home"
    leaf = state_home / archive_exec.ARCHIVE_APP_SEGMENT / archive_exec.ARCHIVE_NAMESPACE
    leaf.mkdir(parents=True, mode=0o700)
    os.chmod(leaf, 0o755)  # world-readable leaf -- must be refused, not silently tightened

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.resolve_archive_root(fake_project_root, env={"XDG_STATE_HOME": str(state_home)})
    assert excinfo.value.reason_code == "archive_root_not_private_mode"


# ---------------------------------------------------------------------------
# PR #1628 review P0-3: forged/malformed pre-existing archive destinations
# must never be silently trusted (symlink / FIFO / directory / hardlink /
# same-content-hash-forged-JSON / concurrent-writer durability barrier).
# ---------------------------------------------------------------------------


def test_existing_archive_destination_symlink_rejected(tmp_path):
    archive_root = tmp_path / "state-root"
    marker_sha = "1" * 64
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, marker_sha)
    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"schema": archive_exec.ARCHIVE_ENVELOPE_SCHEMA}))
    os.symlink(elsewhere, dest)

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.write_archive_no_overwrite(archive_root, locator_rel, {"marker_sha256": f"sha256:{marker_sha}"})
    assert excinfo.value.reason_code == "existing_archive_symlink_rejected"
    assert os.path.islink(dest)  # untouched


def test_existing_archive_destination_directory_rejected(tmp_path):
    archive_root = tmp_path / "state-root"
    marker_sha = "2" * 64
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, marker_sha)
    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True, mode=0o700)
    dest.mkdir(mode=0o700)

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.write_archive_no_overwrite(archive_root, locator_rel, {"marker_sha256": f"sha256:{marker_sha}"})
    assert excinfo.value.reason_code == "existing_archive_not_regular_file"


def test_existing_archive_destination_fifo_rejected(tmp_path):
    archive_root = tmp_path / "state-root"
    marker_sha = "3" * 64
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, marker_sha)
    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True, mode=0o700)
    os.mkfifo(dest, 0o600)

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.write_archive_no_overwrite(archive_root, locator_rel, {"marker_sha256": f"sha256:{marker_sha}"})
    assert excinfo.value.reason_code == "existing_archive_not_regular_file"


def test_existing_archive_destination_hardlinked_rejected(tmp_path):
    archive_root = tmp_path / "state-root"
    marker_sha = "4" * 64
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, marker_sha)
    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True, mode=0o700)
    valid_envelope = {
        "schema": archive_exec.ARCHIVE_ENVELOPE_SCHEMA,
        "repo": REPO,
        "pr_number": PR_NUMBER,
        "source_relpath": archive_exec._source_relpath(PR_NUMBER),
        "marker_sha256": f"sha256:{marker_sha}",
        "expected_head_sha": EXPECTED_HEAD_SHA,
        "idempotency_key": "x:1:" + ("a" * 40) + ":" + ("b" * 64),
        "archived_at": "2026-01-01T00:00:00Z",
        "executor_version": "1",
        "merged": True,
        "review": {"id": REVIEW_ID},
    }
    dest.write_text(json.dumps(valid_envelope))
    os.chmod(dest, 0o600)
    hardlink_sibling = dest.parent / "sibling_hardlink.json"
    os.link(dest, hardlink_sibling)

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.write_archive_no_overwrite(archive_root, locator_rel, {"marker_sha256": f"sha256:{marker_sha}"})
    assert excinfo.value.reason_code == "existing_archive_hardlinked_rejected"


def test_existing_archive_forged_same_hash_json_but_wrong_schema_rejected(tmp_path):
    """A forged pre-existing archive that happens to be at the correct
    content-addressed locator (same marker sha) but is not schema-valid must
    still be refused -- the locator path alone is never sufficient trust."""
    archive_root = tmp_path / "state-root"
    marker_sha = "5" * 64
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, marker_sha)
    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True, mode=0o700)
    dest.write_text(json.dumps({"marker_sha256": f"sha256:{marker_sha}"}))  # missing required fields
    os.chmod(dest, 0o600)

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.write_archive_no_overwrite(archive_root, locator_rel, {"marker_sha256": f"sha256:{marker_sha}"})
    assert "existing_archive_schema" in excinfo.value.reason_code


def test_concurrent_writer_eexist_durability_barrier(tmp_path, monkeypatch):
    """A TOCTOU race where a concurrent writer wins the linkat() publish
    must be treated as already_archived (after fsync-then-strict-readback of
    the winner), never as a raw, unclassified crash."""
    archive_root = tmp_path / "state-root"
    marker_sha = "6" * 64
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, marker_sha)
    winner_envelope = {
        "schema": archive_exec.ARCHIVE_ENVELOPE_SCHEMA,
        "repo": REPO,
        "pr_number": PR_NUMBER,
        "source_relpath": archive_exec._source_relpath(PR_NUMBER),
        "marker_sha256": f"sha256:{marker_sha}",
        "expected_head_sha": EXPECTED_HEAD_SHA,
        "idempotency_key": "x:1:" + ("a" * 40) + ":" + ("b" * 64),
        "archived_at": "2026-01-01T00:00:00Z",
        "executor_version": "1",
        "merged": True,
        "review": {"id": REVIEW_ID},
    }

    real_link = os.link
    call_count = {"n": 0}

    def _link_then_race(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            dest = archive_root / locator_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(winner_envelope))
            os.chmod(dest, 0o600)
            raise FileExistsError()
        return real_link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(archive_exec.os, "link", _link_then_race)
    already_existed, path, existing = archive_exec.write_archive_no_overwrite(
        archive_root, locator_rel, {**winner_envelope, "marker_sha256": f"sha256:{'0' * 64}"}
    )
    assert already_existed is True
    assert existing is not None
    assert existing["marker_sha256"] == f"sha256:{marker_sha}"


# ---------------------------------------------------------------------------
# PR #1628 review P0-4: review body identity binding (remote body rewritten
# while keeping only the trailing marker literal; review author mismatch).
# ---------------------------------------------------------------------------


def test_remote_body_rewritten_with_trailing_marker_only_is_refused(tmp_path):
    """A body whose pre-marker content was rewritten (so its recomputed
    SHA-256 no longer matches the idempotency_key's body_sha256 component),
    while keeping only the trailing marker literal intact, must be refused
    rather than archived as identity-proven."""
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    rewritten_body = f"This text was swapped in after publish.\n\n{_marker_str(idempotency_key)}"
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key, body=rewritten_body),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "remote_review_body_sha256_mismatch"
    assert marker_path.exists()


def test_review_author_identity_mismatch_refused(tmp_path):
    """Even when review_id/url/state/commit/body-hash all validate, a
    review authored by a DIFFERENT identity than the one this process is
    authenticated as must be refused."""
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(
            idempotency_key, review_author_login="attacker-account", authenticated_login=AUTHENTICATED_LOGIN
        ),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "remote_review_author_identity_mismatch"
    assert marker_path.exists()


def test_malformed_idempotency_key_refused_before_remote_calls(tmp_path):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    marker_path = _write_marker(tmp_path, idempotency_key="not-a-valid-key")

    def _never_called(argv):
        raise AssertionError("gh must never be called with a malformed idempotency_key")

    result = archive_exec.run_archive(
        PR_NUMBER, REPO, tmp_path, gh_caller=_never_called, archive_root_override=archive_root
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "marker_idempotency_key_malformed"
    assert marker_path.exists()


# ---------------------------------------------------------------------------
# PR #1628 review P0-5: source presence classification must distinguish
# "absent" from "a different inode now occupies the canonical path".
# ---------------------------------------------------------------------------


def test_source_still_present_distinguishes_different_inode_from_absent(tmp_path):
    _init_git_repo(tmp_path)
    marker_path = _write_marker(tmp_path)
    validated = archive_exec.validate_and_open_marker(tmp_path, PR_NUMBER)
    try:
        assert (
            archive_exec.source_still_present(validated)
            == archive_exec.SOURCE_PRESENCE_SAME_INODE
        )

        os.unlink(marker_path)
        assert (
            archive_exec.source_still_present(validated)
            == archive_exec.SOURCE_PRESENCE_ABSENT
        )

        marker_path.write_text(json.dumps({"schema": "OTHER"}))
        assert (
            archive_exec.source_still_present(validated)
            == archive_exec.SOURCE_PRESENCE_DIFFERENT_INODE
        )
    finally:
        os.close(validated.marker_fd)
        os.close(validated.parent_dir_fd)


def test_different_inode_recreated_after_unlink_is_indeterminate_present(tmp_path, monkeypatch):
    """A canonical marker path recreated by a different process immediately
    after this executor's own unlink must be reported as indeterminate with
    source_present_after=true, never as a bare `archived` success."""
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)
    marker_dir = marker_path.parent

    real_remove = archive_exec.remove_source_with_recheck

    def _remove_then_race(validated):
        real_remove(validated)
        # Simulate a concurrent process recreating a DIFFERENT file at the
        # same canonical path immediately after our own unlink.
        (marker_dir / archive_exec.MARKER_FILE_NAME).write_text(json.dumps({"schema": "RACE"}))

    monkeypatch.setattr(archive_exec, "remove_source_with_recheck", _remove_then_race)
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_INDETERMINATE
    assert result.reason_code == "source_removal_readback_different_inode_present"
    assert result.source_present_after == "true"
    assert result.archive_durable is True


# ---------------------------------------------------------------------------
# PR #1628 review P1-2: bounded filesystem-error classification instead of
# raw, unclassified crashes; file descriptors are always closed.
# ---------------------------------------------------------------------------


def test_archive_write_enospc_is_bounded_refusal_source_untouched(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    real_fsync = os.fsync

    def _fsync_raises_enospc(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(archive_exec.os, "fsync", _fsync_raises_enospc)
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    monkeypatch.setattr(archive_exec.os, "fsync", real_fsync)
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "archive_write_no_space"
    assert marker_path.exists()


def test_archive_directory_create_permission_denied_is_bounded_refusal(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    def _mkdir_raises_eacces(path, mode=0o777):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(archive_exec.os, "mkdir", _mkdir_raises_eacces)
    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root / "not-yet-created",
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "archive_root_mkdir_failed"
    assert marker_path.exists()


def test_existing_archive_malformed_json_read_failure_is_bounded_refusal(tmp_path):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    marker_path = _write_marker(tmp_path, idempotency_key=idempotency_key)

    validated = archive_exec.validate_and_open_marker(tmp_path, PR_NUMBER)
    locator_rel = archive_exec.archive_locator_relpath(REPO, PR_NUMBER, validated.sha256)
    os.close(validated.marker_fd)
    os.close(validated.parent_dir_fd)

    dest = archive_root / locator_rel
    dest.parent.mkdir(parents=True, mode=0o700)
    dest.write_text("{not valid json")
    os.chmod(dest, 0o600)

    result = archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_successful_gh_caller(idempotency_key),
        archive_root_override=archive_root,
    )
    assert result.status == archive_exec.STATUS_REFUSED
    assert result.reason_code == "existing_archive_invalid_json"
    assert marker_path.exists()


def test_source_directory_fsync_failure_after_unlink_is_indeterminate_not_crash(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
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
    assert not marker_path.exists()


# ---------------------------------------------------------------------------
# PR #1628 review P2: strict HTTP status-line parsing (never a substring
# match), API version / Accept headers present, rc checked on the 204 path.
# ---------------------------------------------------------------------------


def test_merge_check_204_with_nonzero_rc_is_refused_not_trusted():
    def _caller(argv):
        return 1, "HTTP/2.0 204 No Content\r\n\r\n", "unexpected transport error"

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.remote_check_merged(REPO, PR_NUMBER, _caller)
    assert excinfo.value.reason_code == "remote_merge_check_204_with_nonzero_rc"


def test_merge_check_status_substring_in_unrelated_position_is_not_merged():
    """A status-shaped body that merely CONTAINS the digits 204 somewhere
    other than the actual HTTP status code must not be treated as merged."""

    def _caller(argv):
        return 0, "HTTP/2.0 200 OK\r\nX-Note: not-204-here\r\n\r\n{\"count\": 204}", ""

    with pytest.raises(archive_exec.ArchiveRefused) as excinfo:
        archive_exec.remote_check_merged(REPO, PR_NUMBER, _caller)
    assert excinfo.value.reason_code == "remote_merge_check_unexpected_response"


def test_merge_check_sends_api_version_and_accept_headers():
    captured = {}

    def _caller(argv):
        captured["argv"] = argv
        return 0, "HTTP/2.0 204 No Content\r\n\r\n", ""

    archive_exec.remote_check_merged(REPO, PR_NUMBER, _caller)
    argv = captured["argv"]
    assert "Accept: application/vnd.github+json" in argv
    assert f"X-GitHub-Api-Version: {archive_exec.GITHUB_API_VERSION}" in argv


# ---------------------------------------------------------------------------
# PR #1628 review P1-2: file descriptors must never leak across a refusal
# raised deep in the remote-validation chain.
# ---------------------------------------------------------------------------


def test_fds_closed_after_refusal_deep_in_validation_chain(tmp_path):
    _init_git_repo(tmp_path)
    archive_root = tmp_path / "state-root"
    idempotency_key = _idempotency_key(REPO, PR_NUMBER, EXPECTED_HEAD_SHA, DEFAULT_BODY_SHA256)
    _write_marker(tmp_path, idempotency_key=idempotency_key)

    archive_exec.run_archive(
        PR_NUMBER,
        REPO,
        tmp_path,
        gh_caller=_mismatched_review_gh_caller,
        archive_root_override=archive_root,
    )
    # Best-effort leak probe: the marker directory itself must still be
    # removable/renamable afterwards (an open, unclosed fd on Linux does not
    # block rename, but a leaked fd count is still the property under test
    # via /proc self-fd growth being bounded across repeated invocations).
    marker_dir = tmp_path / "artifacts" / str(PR_NUMBER) / "issue-metadata" / "pr_review.publish"
    proc_fd_dir = Path("/proc/self/fd")
    if proc_fd_dir.is_dir():
        before = len(list(proc_fd_dir.iterdir()))
        for _ in range(20):
            archive_exec.run_archive(
                PR_NUMBER,
                REPO,
                tmp_path,
                gh_caller=_mismatched_review_gh_caller,
                archive_root_override=archive_root,
            )
        after = len(list(proc_fd_dir.iterdir()))
        assert after - before < 10  # bounded, not linear in iteration count
    assert marker_dir.exists()
