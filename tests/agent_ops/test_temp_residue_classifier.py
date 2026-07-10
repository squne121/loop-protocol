"""Tests for scripts/agent-ops/temp_residue_classifier.py and
temp_residue_marker.py (Issue #1417).

Coverage groups map 1:1 onto the Issue #1417 Verification Commands -k
selectors:
  classification_schema, owner_marker_schema, read_only_no_mutation,
  report_only_matrix, eligible_for_delete_advisory, git_state_mixed_unknown,
  scan_limits_partial, reason_codes_priority, post_merge_cleanup_integration
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER_PATH = REPO_ROOT / "scripts" / "agent-ops" / "temp_residue_classifier.py"
MARKER_PATH = REPO_ROOT / "scripts" / "agent-ops" / "temp_residue_marker.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


marker_mod = _load_module("temp_residue_marker_under_test", MARKER_PATH)
classifier_mod = _load_module("temp_residue_classifier_under_test", CLASSIFIER_PATH)


# --------------------------------------------------------------------------- #
# Fixtures: a throwaway git repo with tmp/, .claude/tmp/, and alias roots.
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", "https://github.com/squne121/loop-protocol.git")
    return root


def _make_valid_marker(
    session_id: str,
    target_relpath: str,
    *,
    repository: str = "squne121/loop-protocol",
    expires_delta: timedelta = timedelta(hours=1),
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema": "temp_residue_owner/v1",
        "marker_id": "tro-11111111-1111-1111-1111-111111111111",
        "repository": repository,
        "session_id": session_id,
        "target_relpath": target_relpath,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + expires_delta).isoformat().replace("+00:00", "Z"),
        "nonce": "0123456789abcdef",
        "producer": {"kind": "self_claim", "version": "1"},
    }


def _write_marker(session_dir: Path, marker: dict) -> Path:
    marker_path = session_dir / marker_mod.MARKER_FILENAME
    marker_path.write_text(json.dumps(marker))
    marker_path.chmod(0o600)
    return marker_path


def _run_classify(repo: Path, session_id: str | None = "session-a", **kwargs):
    limits = classifier_mod.ScanLimits(**kwargs) if kwargs else classifier_mod.ScanLimits()
    return classifier_mod.run_classification(str(repo), limits, session_id)


def _entry(result: dict, path: str) -> dict:
    for e in result["entries"]:
        if e["path"] == path:
            return e
    raise AssertionError(f"no entry for path={path!r}; entries={result['entries']}")


# --------------------------------------------------------------------------- #
# classification_schema
# --------------------------------------------------------------------------- #
class TestClassificationSchema:
    def test_classification_schema_top_level_fields(self, repo: Path):
        result = _run_classify(repo)
        assert result["schema"] == "temp_residue_classification/v1"
        assert result["scan_status"] in ("ok", "partial", "error")
        assert "generated_at" in result
        assert result["project_root"]["source"] == "script_location"
        assert isinstance(result["entries"], list)
        assert isinstance(result["errors"], list)

    def test_classification_schema_entry_required_fields(self, repo: Path):
        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-1").mkdir()
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-1")
        for field in (
            "path", "folder_class", "entry_type", "tracked_state", "ignored_state",
            "ownership_marker", "recommendation", "primary_reason_code",
            "reason_codes", "observation",
        ):
            assert field in entry

    def test_classification_schema_schema_file_validates_against_json_schema(self, repo: Path):
        pytest.importorskip("jsonschema")
        from jsonschema import Draft202012Validator

        schema_path = REPO_ROOT / "schemas" / "temp_residue_classification_v1.schema.json"
        with schema_path.open() as f:
            schema = json.load(f)
        Draft202012Validator.check_schema(schema)

        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-1").mkdir()
        result = _run_classify(repo)
        errors = list(Draft202012Validator(schema).iter_errors(result))
        assert not errors, errors


# --------------------------------------------------------------------------- #
# owner_marker_schema
# --------------------------------------------------------------------------- #
class TestOwnerMarkerSchema:
    def test_owner_marker_schema_schema_file_is_valid_draft_2020_12(self):
        pytest.importorskip("jsonschema")
        from jsonschema import Draft202012Validator

        schema_path = REPO_ROOT / "schemas" / "temp_residue_owner_v1.schema.json"
        with schema_path.open() as f:
            schema = json.load(f)
        Draft202012Validator.check_schema(schema)

    def test_owner_marker_schema_valid_marker_accepted(self):
        marker = _make_valid_marker("session-a", "tmp/session-1")
        ok, reason = marker_mod.validate_marker_schema(marker)
        assert ok, reason

    def test_owner_marker_schema_duplicate_json_key_rejected(self, tmp_path: Path):
        marker_path = tmp_path / marker_mod.MARKER_FILENAME
        marker_path.write_text('{"schema": "temp_residue_owner/v1", "schema": "dup"}')
        marker_path.chmod(0o600)
        result = marker_mod.read_marker_file(str(marker_path))
        assert result.state == marker_mod.STATE_MALFORMED

    def test_owner_marker_schema_nan_infinity_rejected(self, tmp_path: Path):
        marker_path = tmp_path / marker_mod.MARKER_FILENAME
        marker_path.write_text('{"schema": "temp_residue_owner/v1", "value": NaN}')
        marker_path.chmod(0o600)
        result = marker_mod.read_marker_file(str(marker_path))
        assert result.state == marker_mod.STATE_MALFORMED

    def test_owner_marker_schema_oversized_marker_rejected(self, tmp_path: Path):
        marker_path = tmp_path / marker_mod.MARKER_FILENAME
        marker_path.write_text("x" * 100)
        marker_path.chmod(0o600)
        result = marker_mod.read_marker_file(str(marker_path), max_bytes=10)
        assert result.state == marker_mod.STATE_MALFORMED

    def test_owner_marker_schema_symlink_marker_rejected(self, tmp_path: Path):
        real = tmp_path / "real.json"
        real.write_text(json.dumps(_make_valid_marker("s", "tmp/session-1")))
        link = tmp_path / marker_mod.MARKER_FILENAME
        link.symlink_to(real)
        result = marker_mod.read_marker_file(str(link))
        assert result.state == marker_mod.STATE_UNTRUSTED

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
    def test_owner_marker_schema_group_other_writable_marker_rejected(self, tmp_path: Path):
        marker_path = tmp_path / marker_mod.MARKER_FILENAME
        marker_path.write_text(json.dumps(_make_valid_marker("s", "tmp/session-1")))
        marker_path.chmod(0o666)
        result = marker_mod.read_marker_file(str(marker_path))
        assert result.state == marker_mod.STATE_UNTRUSTED

    def test_owner_marker_schema_missing_marker_is_absent(self, tmp_path: Path):
        result = marker_mod.read_marker_file(str(tmp_path / marker_mod.MARKER_FILENAME))
        assert result.state == marker_mod.STATE_ABSENT


# --------------------------------------------------------------------------- #
# read_only_no_mutation
# --------------------------------------------------------------------------- #
class TestReadOnlyNoMutation:
    def test_read_only_no_mutation_classifier_never_calls_destructive_primitives(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-1"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-1"))
        (repo / ".tmp").mkdir()
        (repo / ".tmp" / "residue").mkdir()

        with mock.patch("os.unlink") as m_unlink, \
             mock.patch("os.rmdir") as m_rmdir, \
             mock.patch("shutil.rmtree") as m_rmtree:
            _run_classify(repo)

        m_unlink.assert_not_called()
        m_rmdir.assert_not_called()
        m_rmtree.assert_not_called()

    def test_read_only_no_mutation_classifier_never_spawns_mutating_subprocess(self, repo: Path):
        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-1").mkdir()

        real_run = subprocess.run
        seen_argv: list[list[str]] = []

        def spy_run(argv, *args, **kwargs):
            seen_argv.append(list(argv))
            return real_run(argv, *args, **kwargs)

        with mock.patch("subprocess.run", side_effect=spy_run):
            _run_classify(repo)

        forbidden_git_subcommands = {"rm", "clean", "checkout", "reset", "branch", "worktree"}
        for argv in seen_argv:
            if argv and argv[0] == "git" and len(argv) > 1:
                assert argv[1] not in forbidden_git_subcommands, argv


# --------------------------------------------------------------------------- #
# report_only_matrix (AC4)
# --------------------------------------------------------------------------- #
class TestReportOnlyMatrix:
    def test_report_only_matrix_approved_root_itself_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        result = _run_classify(repo)
        entry = _entry(result, "tmp")
        assert entry["recommendation"] == "report_only"
        assert entry["primary_reason_code"] == "root_itself"

    def test_report_only_matrix_alias_root_itself_is_report_only(self, repo: Path):
        (repo / ".tmp").mkdir()
        result = _run_classify(repo)
        entry = _entry(result, ".tmp")
        assert entry["recommendation"] == "report_only"

    def test_report_only_matrix_alias_child_with_valid_marker_is_still_report_only(self, repo: Path):
        (repo / ".tmp").mkdir()
        session = repo / ".tmp" / "session-1"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", ".tmp/session-1"))
        result = _run_classify(repo)
        entry = _entry(result, ".tmp/session-1")
        assert entry["recommendation"] == "report_only"
        assert "denied_alias_report_only_policy" in entry["reason_codes"]

    def test_report_only_matrix_marker_unknown_child_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-unowned").mkdir()
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-unowned")
        assert entry["recommendation"] == "report_only"
        assert entry["primary_reason_code"] == "marker_absent"

    def test_report_only_matrix_malformed_marker_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-bad"
        session.mkdir()
        (session / marker_mod.MARKER_FILENAME).write_text("{not json")
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-bad")
        assert entry["recommendation"] == "report_only"

    def test_report_only_matrix_foreign_session_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-foreign"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-other", "tmp/session-foreign"))
        result = _run_classify(repo, session_id="session-a")
        entry = _entry(result, "tmp/session-foreign")
        assert entry["recommendation"] == "report_only"
        assert "marker_session_mismatch" in entry["reason_codes"]

    def test_report_only_matrix_target_mismatch_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-mismatch"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/other-target"))
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-mismatch")
        assert entry["recommendation"] == "report_only"
        assert "marker_target_mismatch" in entry["reason_codes"]

    def test_report_only_matrix_symlink_session_dir_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        real_dir = repo / "real-session"
        real_dir.mkdir()
        link = repo / "tmp" / "session-link"
        link.symlink_to(real_dir)
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-link")
        assert entry["recommendation"] == "report_only"
        assert entry["entry_type"] == "symlink"

    def test_report_only_matrix_special_file_in_session_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-fifo"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-fifo"))
        os.mkfifo(session / "pipe")
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-fifo")
        assert entry["recommendation"] == "report_only"
        assert "special_file_present" in entry["reason_codes"]

    def test_report_only_matrix_tracked_content_present_is_report_only(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-tracked"
        session.mkdir()
        (session / "keep.txt").write_text("keep\n")
        _git(repo, "add", "tmp/session-tracked/keep.txt")
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-tracked")
        assert entry["recommendation"] == "report_only"
        assert entry["tracked_state"] == "all"
        assert "tracked_content_present" in entry["reason_codes"]


# --------------------------------------------------------------------------- #
# eligible_for_delete_advisory (AC5)
# --------------------------------------------------------------------------- #
class TestEligibleForDeleteAdvisory:
    def test_eligible_for_delete_advisory_owned_session_untracked_non_symlink_is_eligible(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-owned"
        session.mkdir()
        (session / "scratch.txt").write_text("scratch\n")
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-owned"))
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-owned")
        assert entry["recommendation"] == "eligible_for_delete"
        assert entry["primary_reason_code"] == "owned_session_eligible"

    def test_eligible_for_delete_advisory_claude_tmp_owned_session_is_eligible(self, repo: Path):
        (repo / ".claude").mkdir()
        (repo / ".claude" / "tmp").mkdir()
        session = repo / ".claude" / "tmp" / "session-owned"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", ".claude/tmp/session-owned"))
        result = _run_classify(repo)
        entry = _entry(result, ".claude/tmp/session-owned")
        assert entry["recommendation"] == "eligible_for_delete"

    def test_eligible_for_delete_advisory_eligible_recommendation_is_not_deletion(self, repo: Path):
        """Invariant: this classifier module never deletes anything, even for
        an eligible_for_delete verdict — advisory-only guarantee."""
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-owned"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-owned"))
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-owned")
        assert entry["recommendation"] == "eligible_for_delete"
        assert session.exists()  # never removed


# --------------------------------------------------------------------------- #
# git_state_mixed_unknown (AC6)
# --------------------------------------------------------------------------- #
class TestGitStateMixedUnknown:
    def test_git_state_mixed_unknown_mixed_tracked_untracked_is_some(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-mixed"
        session.mkdir()
        (session / "tracked.txt").write_text("a\n")
        (session / "untracked.txt").write_text("b\n")
        _git(repo, "add", "tmp/session-mixed/tracked.txt")
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-mixed")
        assert entry["tracked_state"] == "some"

    def test_git_state_mixed_unknown_git_failure_yields_unknown_and_report_only(self, repo: Path, monkeypatch):
        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-x").mkdir()

        real_run = subprocess.run

        def fake_run(argv, *args, **kwargs):
            # Only the batched git-state-index calls (ls-files / status) are
            # simulated as unavailable here; project_root validation
            # (`rev-parse --show-toplevel`) and repository slug resolution
            # must keep working so this test isolates git_state_unknown
            # specifically, rather than also collapsing project_root
            # validation (covered separately by TestProjectRootValidation).
            if argv and argv[0] == "git" and len(argv) > 1 and argv[1] in ("ls-files", "status"):
                raise OSError("git unavailable")
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(classifier_mod.subprocess, "run", fake_run)
        result = _run_classify(repo)
        assert result["project_root"]["validated"] is True
        entry = _entry(result, "tmp/session-x")
        assert entry["tracked_state"] == "unknown"
        assert entry["ignored_state"] == "unknown"
        assert entry["recommendation"] == "report_only"
        assert "git_state_unknown" in entry["reason_codes"]

    def test_git_state_mixed_unknown_no_content_is_none_state(self, repo: Path):
        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-empty").mkdir()
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-empty")
        assert entry["tracked_state"] == "none"
        assert entry["ignored_state"] == "none"

    def test_git_state_mixed_unknown_all_ignored_is_all_state(self, repo: Path):
        (repo / ".gitignore").write_text("tmp/session-ignored/**\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore")
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-ignored"
        session.mkdir()
        (session / "junk.log").write_text("x\n")
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-ignored")
        assert entry["ignored_state"] == "all"


# --------------------------------------------------------------------------- #
# scan_limits_partial (AC7)
# --------------------------------------------------------------------------- #
class TestScanLimitsPartial:
    def test_scan_limits_partial_max_entries_triggers_partial_scan_status(self, repo: Path):
        (repo / "tmp").mkdir()
        for i in range(5):
            (repo / "tmp" / f"session-{i}").mkdir()
        result = _run_classify(repo, max_entries=1)
        assert result["scan_status"] == "partial"
        assert any(e.get("reason_code") == "scan_limit_exceeded" for e in result["errors"]) or result["errors"]

    def test_scan_limits_partial_deadline_triggers_partial_scan_status(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-deep"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-deep"))
        for i in range(50):
            (session / f"f{i}.txt").write_text("x\n")
        result = _run_classify(repo, deadline_seconds=0.0)
        assert result["scan_status"] == "partial"
        assert result["errors"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
    def test_scan_limits_partial_permission_denied_yields_scan_error(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-noperm"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-noperm"))
        (session / "locked").mkdir()
        (session / "locked").chmod(0o000)
        try:
            result = _run_classify(repo)
            assert result["scan_status"] == "partial"
            entry = _entry(result, "tmp/session-noperm")
            assert entry["recommendation"] == "report_only"
            assert "scan_incomplete" in entry["reason_codes"]
            assert "permission_denied" in entry["reason_codes"]
            assert any(e.get("reason_code") == "permission_denied" for e in result["errors"])
        finally:
            (session / "locked").chmod(0o700)


# --------------------------------------------------------------------------- #
# reason_codes_priority (AC8)
# --------------------------------------------------------------------------- #
class TestReasonCodesPriority:
    def test_reason_codes_priority_multiple_reasons_preserved_and_ordered(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-multi"
        session.mkdir()
        (session / "tracked.txt").write_text("a\n")
        _git(repo, "add", "tmp/session-multi/tracked.txt")
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-multi")
        assert len(entry["reason_codes"]) >= 2
        assert entry["primary_reason_code"] == entry["reason_codes"][0]

    def test_reason_codes_priority_primary_reason_code_matches_priority_table(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-priority"
        session.mkdir()
        (session / "tracked.txt").write_text("a\n")
        _git(repo, "add", "tmp/session-priority/tracked.txt")
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-priority"))
        result = _run_classify(repo)
        entry = _entry(result, "tmp/session-priority")
        codes = entry["reason_codes"]
        priorities = [classifier_mod._priority_key(c) for c in codes]
        assert priorities == sorted(priorities)

    def test_reason_codes_priority_deterministic_repeated_run(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-stable"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-stable"))
        r1 = _run_classify(repo)
        r2 = _run_classify(repo)
        paths1 = [e["path"] for e in r1["entries"]]
        paths2 = [e["path"] for e in r2["entries"]]
        assert paths1 == paths2
        e1 = _entry(r1, "tmp/session-stable")
        e2 = _entry(r2, "tmp/session-stable")
        assert e1["reason_codes"] == e2["reason_codes"]
        assert e1["recommendation"] == e2["recommendation"]


# --------------------------------------------------------------------------- #
# post_merge_cleanup_integration (AC9)
# --------------------------------------------------------------------------- #
class TestPostMergeCleanupIntegration:
    def test_post_merge_cleanup_integration_classify_git_state_includes_field(
        self, repo: Path, monkeypatch
    ):
        script = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "scripts" / "classify-git-state.py"
        monkeypatch.chdir(repo)
        proc = subprocess.run(
            [sys.executable, str(script), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert "temp_residue_classification" in payload
        trc = payload["temp_residue_classification"]
        # A controlled test fixture (real git repo, readable cwd) must always
        # produce a successful classifier run; `null` here would silently
        # mask a classifier failure as success (Issue #1417 PR #1427 review).
        assert trc is not None, "classifier failure (null) must not be treated as success"
        assert trc.get("schema") == "temp_residue_classification/v1"

    def test_post_merge_cleanup_integration_yaml_format_also_includes_field(self, repo: Path, monkeypatch):
        """The default (`--format yaml`) execution path used by
        post-merge-cleanup/SKILL.md step 1 must also carry the classification,
        not only `--format json` (Issue #1417 PR #1427 review)."""
        script = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "scripts" / "classify-git-state.py"
        monkeypatch.chdir(repo)
        proc = subprocess.run(
            [sys.executable, str(script), "--format", "yaml"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "temp_residue_classification:" in proc.stdout
        assert "schema: temp_residue_classification/v1" in proc.stdout

    def test_post_merge_cleanup_integration_empty_result_distinguished_from_failure(self, repo: Path, monkeypatch):
        script = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "scripts" / "classify-git-state.py"
        monkeypatch.chdir(repo)
        proc = subprocess.run(
            [sys.executable, str(script), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        trc = payload["temp_residue_classification"]
        assert trc is not None
        assert trc["scan_status"] in ("ok", "partial", "error")
        assert trc["entries"] == [] or isinstance(trc["entries"], list)

    def test_post_merge_cleanup_integration_classifier_failure_yields_null(
        self, repo: Path, monkeypatch
    ):
        """When the classifier itself raises (import/exec failure),
        `classify_temp_residue()` must catch it and surface `None` — never a
        fabricated success-shaped payload — so `is not None` reliably
        distinguishes a real run from a failed one."""
        script_path = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "scripts" / "classify-git-state.py"
        spec = importlib.util.spec_from_file_location("classify_git_state_under_test", script_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["classify_git_state_under_test"] = module
        spec.loader.exec_module(module)

        monkeypatch.chdir(repo)

        fake_mod = types.ModuleType("temp_residue_classifier")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated classifier construction failure")

        fake_mod.ScanLimits = _boom
        monkeypatch.setitem(sys.modules, "temp_residue_classifier", fake_mod)

        result = module.classify_temp_residue()
        assert result is None


# --------------------------------------------------------------------------- #
# symlink_component_escape (Issue #1417 PR #1427 review P0-1)
# --------------------------------------------------------------------------- #
class TestSymlinkComponentEscape:
    def test_symlink_component_escape_claude_symlink_is_not_followed(self, repo: Path, tmp_path: Path):
        """`.claude` itself being a symlink to an out-of-repo directory must
        not let the classifier traverse outside the repository."""
        outside = tmp_path / "outside-secret"
        outside.mkdir()
        (outside / "tmp").mkdir()
        (outside / "tmp" / "leaked").mkdir()
        (repo / ".claude").symlink_to(outside, target_is_directory=True)
        result = _run_classify(repo)
        for entry in result["entries"]:
            assert "leaked" not in entry["path"]
            assert "outside-secret" not in entry["path"]

    def test_symlink_component_escape_mid_scan_root_swap_is_refused(self, repo: Path, tmp_path: Path):
        """A component swapped to a symlink between project_root resolution
        and the walk (simulated by pre-creating the symlink before the run)
        must be refused via O_NOFOLLOW, not silently followed."""
        outside = tmp_path / "outside-secret-2"
        outside.mkdir()
        (repo / "tmp").symlink_to(outside, target_is_directory=True)
        result = _run_classify(repo)
        entry = _entry(result, "tmp")
        assert entry["entry_type"] == "symlink"
        assert entry["recommendation"] == "report_only"


# --------------------------------------------------------------------------- #
# invalid_filename_encoding (Issue #1417 PR #1427 review P0-4)
# --------------------------------------------------------------------------- #
class TestInvalidFilenameEncoding:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX raw byte filenames only")
    def test_invalid_filename_encoding_undecodable_name_is_skipped_safely(self, repo: Path):
        (repo / "tmp").mkdir()
        bad_name = os.fsdecode(b"session-\xff\xfe")
        os.mkdir(os.path.join(str(repo / "tmp"), bad_name))
        result = _run_classify(repo)
        assert result["scan_status"] == "partial"
        assert any(e.get("reason_code") == "invalid_filename_encoding" for e in result["errors"])
        # The emitted JSON payload itself must be strict UTF-8 encodable.
        json.dumps(result, ensure_ascii=False).encode("utf-8", errors="strict")
        for entry in result["entries"]:
            entry["path"].encode("utf-8", errors="strict")


# --------------------------------------------------------------------------- #
# project_root_validation (Issue #1417 PR #1427 review P0-5)
# --------------------------------------------------------------------------- #
class TestProjectRootValidation:
    def test_project_root_validation_git_totally_unavailable_fails_closed(self, repo: Path, monkeypatch):
        """If `git` itself is unavailable, project_root cannot be validated,
        so the whole scan must fail closed (no entries), not merely mark
        git-derived fields as unknown while still emitting candidates."""
        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-x").mkdir()

        def fake_run(*_args, **_kwargs):
            raise OSError("git unavailable")

        monkeypatch.setattr(classifier_mod.subprocess, "run", fake_run)
        result = _run_classify(repo)
        assert result["project_root"]["validated"] is False
        assert result["entries"] == []
        assert result["scan_status"] in ("partial", "error")

    def test_project_root_validation_non_git_directory_is_not_validated(self, tmp_path: Path):
        plain_dir = tmp_path / "not-a-repo"
        plain_dir.mkdir()
        result = _run_classify(plain_dir, session_id="session-a")
        assert result["project_root"]["validated"] is False
        assert result["scan_status"] in ("partial", "error")
        assert result["entries"] == []

    def test_project_root_validation_explicit_root_not_toplevel_is_not_validated(self, repo: Path):
        (repo / "tmp").mkdir()
        subdir = repo / "tmp"
        result = classifier_mod.run_classification(str(subdir), classifier_mod.ScanLimits(), "session-a")
        assert result["project_root"]["validated"] is False
        assert result["entries"] == []

    def test_project_root_validation_success_marks_validated_true(self, repo: Path):
        (repo / "tmp").mkdir()
        result = _run_classify(repo)
        assert result["project_root"]["validated"] is True


# --------------------------------------------------------------------------- #
# git_state_batched_index (Issue #1417 PR #1427 review P0-3)
# --------------------------------------------------------------------------- #
class TestGitStateBatchedIndex:
    def test_git_state_batched_index_uses_at_most_two_git_subprocess_calls_for_state(self, repo: Path):
        (repo / "tmp").mkdir()
        for i in range(10):
            session = repo / "tmp" / f"session-{i}"
            session.mkdir()
            (session / "f.txt").write_text("x\n")

        real_run = subprocess.run
        git_state_calls: list[list[str]] = []

        def spy_run(argv, *args, **kwargs):
            if argv and argv[0] == "git" and len(argv) > 1 and argv[1] in ("ls-files", "status"):
                git_state_calls.append(list(argv))
            return real_run(argv, *args, **kwargs)

        with mock.patch("subprocess.run", side_effect=spy_run):
            _run_classify(repo)

        assert len(git_state_calls) == 2, git_state_calls

    def test_git_state_batched_index_deadline_before_git_calls_yields_unknown(self, repo: Path):
        (repo / "tmp").mkdir()
        (repo / "tmp" / "session-x").mkdir()
        result = _run_classify(repo, deadline_seconds=0.0)
        assert result["scan_status"] == "partial"
        assert result["errors"]


# --------------------------------------------------------------------------- #
# max_depth_completion_state (Issue #1417 PR #1427 review P0-2)
# --------------------------------------------------------------------------- #
class TestMaxDepthCompletionState:
    def test_max_depth_completion_state_truncation_marks_scan_incomplete(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-deep"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-deep"))
        nested = session
        for i in range(5):
            nested = nested / f"level-{i}"
            nested.mkdir()
        result = _run_classify(repo, max_depth=1)
        assert result["scan_status"] == "partial"
        entry = _entry(result, "tmp/session-deep")
        assert entry["recommendation"] == "report_only"
        assert "scan_incomplete" in entry["reason_codes"]
        assert any(e.get("reason_code") == "max_depth_exceeded" for e in result["errors"])

    def test_max_depth_completion_state_within_bound_is_not_marked_incomplete(self, repo: Path):
        (repo / "tmp").mkdir()
        session = repo / "tmp" / "session-shallow"
        session.mkdir()
        _write_marker(session, _make_valid_marker("session-a", "tmp/session-shallow"))
        (session / "f.txt").write_text("x\n")
        result = _run_classify(repo, max_depth=32)
        entry = _entry(result, "tmp/session-shallow")
        assert "scan_incomplete" not in entry["reason_codes"]
        assert entry["recommendation"] == "eligible_for_delete"


# --------------------------------------------------------------------------- #
# marker_toctou_and_schema_parity (Issue #1417 PR #1427 review P0-6)
# --------------------------------------------------------------------------- #
class TestMarkerTOCTOUAndSchemaParity:
    def test_marker_toctou_hard_linked_marker_is_untrusted(self, tmp_path: Path):
        marker_path = tmp_path / marker_mod.MARKER_FILENAME
        marker_path.write_text(json.dumps(_make_valid_marker("s", "tmp/session-1")))
        marker_path.chmod(0o600)
        other = tmp_path / "other-link.json"
        os.link(str(marker_path), str(other))
        result = marker_mod.read_marker_file(str(marker_path))
        assert result.state == marker_mod.STATE_UNTRUSTED

    def test_marker_toctou_dir_fd_relative_read_matches_path_based_read(self, tmp_path: Path):
        marker = _make_valid_marker("session-a", "tmp/session-1")
        _write_marker(tmp_path, marker)
        path_result = marker_mod.read_marker_file(str(tmp_path / marker_mod.MARKER_FILENAME))
        fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd_result = marker_mod.read_marker_file_at(fd)
        finally:
            os.close(fd)
        assert path_result.state == fd_result.state == marker_mod.STATE_VALID
        assert path_result.data == fd_result.data

    def test_marker_toctou_dir_fd_relative_symlink_rejected(self, tmp_path: Path):
        real = tmp_path / "real.json"
        real.write_text(json.dumps(_make_valid_marker("s", "tmp/session-1")))
        link = tmp_path / marker_mod.MARKER_FILENAME
        link.symlink_to(real)
        fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            result = marker_mod.read_marker_file_at(fd)
        finally:
            os.close(fd)
        assert result.state == marker_mod.STATE_UNTRUSTED

    def test_marker_schema_parity_marker_id_non_hex_suffix_rejected_by_both(self):
        """`tro-!!!!!!!!` is 12 chars (passes a naive length-only check) but
        must be rejected by the Python validator exactly as it would be by
        the JSON Schema's `^tro-[0-9a-fA-F-]{8,}$` pattern."""
        marker = _make_valid_marker("s", "tmp/session-1")
        marker["marker_id"] = "tro-!!!!!!!!"
        ok, reason = marker_mod.validate_marker_schema(marker)
        assert not ok
        assert reason == "marker_id_invalid"

    def test_marker_schema_parity_session_id_over_256_chars_rejected(self):
        marker = _make_valid_marker("s" * 257, "tmp/session-1")
        ok, reason = marker_mod.validate_marker_schema(marker)
        assert not ok
        assert reason == "session_id_invalid"

    def test_marker_schema_parity_target_relpath_outside_approved_roots_rejected(self):
        marker = _make_valid_marker("s", "tmp/session-1")
        marker["target_relpath"] = "not-approved/session-1"
        ok, reason = marker_mod.validate_marker_schema(marker)
        assert not ok
        assert reason == "target_relpath_invalid"

    def test_marker_schema_parity_jsonschema_agrees_with_python_validator(self):
        """Positive/negative corpus cross-checked against the JSON Schema
        directly, when jsonschema is available."""
        pytest.importorskip("jsonschema")
        from jsonschema import Draft202012Validator

        schema_path = REPO_ROOT / "schemas" / "temp_residue_owner_v1.schema.json"
        with schema_path.open() as f:
            schema = json.load(f)
        validator = Draft202012Validator(schema)

        positive = [_make_valid_marker("s", "tmp/session-1")]
        negative = []
        bad1 = _make_valid_marker("s", "tmp/session-1")
        bad1["marker_id"] = "tro-!!!!!!!!"
        negative.append(bad1)
        bad2 = _make_valid_marker("s" * 257, "tmp/session-1")
        negative.append(bad2)
        bad3 = _make_valid_marker("s", "tmp/session-1")
        bad3["target_relpath"] = "not-approved/session-1"
        negative.append(bad3)

        for marker in positive:
            schema_ok = validator.is_valid(marker)
            py_ok, _ = marker_mod.validate_marker_schema(marker)
            assert schema_ok is True
            assert py_ok is True
        for marker in negative:
            schema_ok = validator.is_valid(marker)
            py_ok, _ = marker_mod.validate_marker_schema(marker)
            assert schema_ok is False
            assert py_ok is False

    def test_marker_schema_parity_repository_none_does_not_wildcard_match(self):
        """An unresolved expected_repository (origin unknown) must never
        wildcard-match every marker's repository field."""
        marker = _make_valid_marker("session-a", "tmp/session-1", repository="squne121/loop-protocol")
        result_data = marker_mod.MarkerResult(
            state=marker_mod.STATE_VALID, data=marker, reason=None
        )
        evaluated = marker_mod._evaluate_marker_result(
            result_data,
            current_session_id="session-a",
            expected_target_relpath="tmp/session-1",
            expected_repository=None,
            now=None,
        )
        assert evaluated.state == marker_mod.STATE_MISMATCH
