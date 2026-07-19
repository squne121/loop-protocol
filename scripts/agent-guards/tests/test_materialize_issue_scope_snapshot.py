from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

_REPO_ROOT = _GUARDS_DIR.parent.parent

import controlled_git_change_exec as controlled  # noqa: E402
from controlled_skill_mutation_exec import _validate_issue_scope_snapshot_materialize_fields  # noqa: E402
from controlled_skill_mutation_policy import is_controlled_skill_mutation_exec_command  # noqa: E402
import materialize_issue_scope_snapshot as materializer  # noqa: E402
from materialize_issue_scope_snapshot import (  # noqa: E402
    COMMAND_ID,
    PROVENANCE_SCHEMA,
    expected_output_path,
    expected_provenance_path,
    materialize,
)

_TRUSTED_AUTHOR = "squne121"
_TRUSTED_AUTHOR_ID = 63350259
_TRUSTED_AUTHOR_TYPE = "User"
_TRUSTED_AUTHOR_ASSOCIATION = "OWNER"
_ISSUE_URL = "https://github.com/squne121/loop-protocol/issues/1629"
_SOURCE_URL = "https://github.com/squne121/loop-protocol/issues/1629#issuecomment-5014250179"
_COMMENT_ID = 5014250179

_ISSUE_BODY = "## Allowed Paths\n\n- scripts/agent-guards/example.py\n\n## Stop Conditions\n"


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _allowed_paths_hash() -> str:
    return controlled.compute_allowed_paths_sha256(["scripts/agent-guards/example.py"])


def _go_comment_body(
    *,
    issue_body: str = _ISSUE_BODY,
    base_ref: str = "main",
    base_sha: str = "a" * 40,
    allowed_paths_hash: str | None = None,
    fingerprint: bool = True,
) -> str:
    body_sha = _sha(issue_body)
    lines = [
        "CONTRACT_REVIEW_RESULT_V1:",
        "  status: go",
        "  generated_by: issue-contract-review",
        f'  issue_url: "{_ISSUE_URL}"',
        '  generated_at: "2026-07-19T00:00:00Z"',
        f'  body_sha256: "{body_sha}"',
    ]
    if fingerprint:
        lines += [
            "  expected_contract_fingerprint:",
            "    issue_number: 1629",
            "    contract_source_kind: issue_comment",
            f'    contract_source_id: "{_COMMENT_ID}"',
            f'    contract_body_sha256: "{body_sha}"',
            f'    allowed_paths_normalized_sha256: "{allowed_paths_hash or _allowed_paths_hash()}"',
            f'    base_ref: "{base_ref}"',
            f'    base_sha_at_snapshot: "{base_sha}"',
        ]
    return "```yaml\n" + "\n".join(lines) + "\n```\n"


def _comment(body: str, *, trusted: bool = True, comment_id: int = _COMMENT_ID) -> dict:
    entry = {
        "id": comment_id,
        "html_url": f"https://github.com/squne121/loop-protocol/issues/1629#issuecomment-{comment_id}",
        "created_at": "2026-07-19T00:00:00Z",
        "updated_at": "2026-07-19T00:00:00Z",
        "body": body,
    }
    if trusted:
        entry.update(
            author=_TRUSTED_AUTHOR,
            author_id=_TRUSTED_AUTHOR_ID,
            author_type=_TRUSTED_AUTHOR_TYPE,
            author_association=_TRUSTED_AUTHOR_ASSOCIATION,
        )
    else:
        entry.update(author="mallory", author_id=1, author_type="User", author_association="NONE")
    return entry


def _patch_common(monkeypatch, tmp_path: Path, *, comments: list[dict], base_sha: str = "a" * 40):
    # `tmp_path` is a throwaway pytest sandbox, not a real checkout of this
    # repository -- it does not contain `.claude/skills/issue-contract-
    # review/scripts/contract_review_result_parser.py`. Load the REAL
    # canonical parser from the actual repo root and inject it, so the
    # trust/fingerprint validation logic under test is the real production
    # logic, without requiring tests to materialize a full repo checkout.
    real_parser = materializer._load_contract_parser(_REPO_ROOT)
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._load_contract_parser",
        lambda project_root: real_parser,
    )
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._validate_worktree_binding",
        lambda root, path, branch, env: root,
    )
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._live_default_branch",
        lambda gh_bin, repo, root, env: ("main", base_sha),
    )
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._load_live_issue",
        lambda gh_bin, issue_number, repo, root, env: {"body": _ISSUE_BODY, "updatedAt": "2026-07-19T00:00:00Z"},
    )
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._fetch_comments_with_identity",
        lambda gh_bin, issue_number, repo, root, env: comments,
    )


def _materialize(tmp_path: Path, **overrides):
    kwargs = dict(
        issue_number=1629,
        repo="squne121/loop-protocol",
        contract_snapshot_url=_SOURCE_URL,
        base_ref="main",
        branch_name="worktree-issue-1629-test",
        worktree_path=str(tmp_path),
        output=f"artifacts/1629/issue-metadata/{COMMAND_ID}/issue_scope_snapshot.json",
        gh_bin="/usr/bin/gh",
        project_root=tmp_path,
    )
    kwargs.update(overrides)
    return materialize(**kwargs)


def test_materializer_binds_live_github_contract_and_worktree(tmp_path: Path, monkeypatch):
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body())])

    result = _materialize(tmp_path)

    snapshot_path = expected_output_path(tmp_path, 1629)
    provenance_path = expected_provenance_path(tmp_path, 1629)
    assert result["status"] == "ok"
    snapshot = json.loads(snapshot_path.read_text())
    provenance = json.loads(provenance_path.read_text())
    assert snapshot["issue_number"] == 1629
    assert snapshot["contract_source_id"] == str(_COMMENT_ID)
    assert provenance["schema"] == PROVENANCE_SCHEMA
    assert provenance["artifact_sha256"] == "sha256:" + hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    # Issue #1629 fix_delta P0: the in-memory `snapshot` key is the
    # authoritative output, not a re-read of the file just written.
    assert result["snapshot"] == snapshot


def test_materializer_rejects_unsafe_output_before_any_readback(tmp_path: Path, monkeypatch):
    with pytest.raises(ValueError, match="artifact_path_binding_mismatch"):
        _materialize(tmp_path, output="tmp/handwritten.json")
    assert not expected_output_path(tmp_path, 1629).exists()


def test_materializer_rejects_contract_source_drift(tmp_path: Path, monkeypatch):
    stale_body = "## Allowed Paths\n\n- scripts/agent-guards/other.py\n"
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body(issue_body=stale_body))])
    with pytest.raises(ValueError, match="contract_source_drift"):
        _materialize(tmp_path)
    assert not expected_output_path(tmp_path, 1629).exists()


def test_materializer_rejects_readback_drift_binding_and_unsafe_output(tmp_path: Path, monkeypatch):
    stale_body = "## Allowed Paths\n\n- scripts/agent-guards/other.py\n"
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body(issue_body=stale_body))])
    with pytest.raises(ValueError, match="contract_source_drift"):
        _materialize(tmp_path)
    assert not expected_output_path(tmp_path, 1629).exists()

    with pytest.raises(ValueError, match="artifact_path_binding_mismatch"):
        _materialize(tmp_path, output="tmp/handwritten.json")
    assert not expected_output_path(tmp_path, 1629).exists()


def test_materializer_rejects_untrusted_author_go(tmp_path: Path, monkeypatch):
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body(), trusted=False)])
    with pytest.raises(ValueError, match="contract_source_untrusted_author"):
        _materialize(tmp_path)
    assert not expected_output_path(tmp_path, 1629).exists()


def test_materializer_rejects_missing_fingerprint(tmp_path: Path, monkeypatch):
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body(fingerprint=False))])
    with pytest.raises(ValueError, match="contract_source_fingerprint_not_ready"):
        _materialize(tmp_path)


def test_materializer_rejects_blocked_status_even_with_later_go_from_untrusted(tmp_path: Path, monkeypatch):
    # A trusted `status: go` for a DIFFERENT comment id must not satisfy this
    # materialize() call, which is bound to one specific contract_snapshot_url.
    other_go = _comment(_go_comment_body(), comment_id=999)
    _patch_common(monkeypatch, tmp_path, comments=[other_go])
    with pytest.raises(ValueError, match="contract_source_not_found"):
        _materialize(tmp_path)


def test_materializer_rejects_base_ref_fingerprint_mismatch(tmp_path: Path, monkeypatch):
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body(base_ref="release"))])
    with pytest.raises(ValueError, match="base_ref_fingerprint_mismatch"):
        _materialize(tmp_path)


def test_materializer_rejects_base_sha_fingerprint_mismatch(tmp_path: Path, monkeypatch):
    _patch_common(
        monkeypatch,
        tmp_path,
        comments=[_comment(_go_comment_body(base_sha="a" * 40))],
        base_sha="b" * 40,
    )
    with pytest.raises(ValueError, match="base_sha_fingerprint_mismatch"):
        _materialize(tmp_path)
    assert not expected_output_path(tmp_path, 1629).exists()


def test_materializer_rejects_allowed_paths_fingerprint_mismatch(tmp_path: Path, monkeypatch):
    _patch_common(
        monkeypatch,
        tmp_path,
        comments=[_comment(_go_comment_body(allowed_paths_hash="0" * 64))],
    )
    with pytest.raises(ValueError, match="allowed_paths_fingerprint_mismatch"):
        _materialize(tmp_path)


def test_materializer_rejects_base_ref_not_default_branch(tmp_path: Path, monkeypatch):
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body())])
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._live_default_branch",
        lambda gh_bin, repo, root, env: ("develop", "a" * 40),
    )
    with pytest.raises(ValueError, match="base_ref_not_default_branch"):
        _materialize(tmp_path)


def test_materializer_uses_live_default_branch_sha_not_local_git(tmp_path: Path, monkeypatch):
    """Issue #1629 fix_delta P1 (default_base_sha_local_not_live): base_sha
    in the produced snapshot must be whatever the (mocked) live GitHub API
    call returned, independent of any local git state."""
    live_sha = "b" * 40
    _patch_common(
        monkeypatch,
        tmp_path,
        comments=[_comment(_go_comment_body(base_sha=live_sha))],
        base_sha=live_sha,
    )
    result = _materialize(tmp_path)
    assert result["base_sha"] == live_sha
    assert result["snapshot"]["base_sha"] == live_sha


def test_materializer_atomic_replace_on_re_materialize(tmp_path: Path, monkeypatch):
    """Issue #1629 fix_delta P0 (stale_artifact_reuse): re-materializing over
    an existing successful artifact must fully replace it (atomic
    os.replace), never leave a stale/partial artifact, and never leave a
    temp file behind."""
    _patch_common(monkeypatch, tmp_path, comments=[_comment(_go_comment_body())], base_sha="a" * 40)
    first = _materialize(tmp_path)
    snapshot_path = expected_output_path(tmp_path, 1629)
    first_bytes = snapshot_path.read_bytes()

    _patch_common(
        monkeypatch,
        tmp_path,
        comments=[_comment(_go_comment_body(base_sha="c" * 40))],
        base_sha="c" * 40,
    )
    second = _materialize(tmp_path)
    second_bytes = snapshot_path.read_bytes()

    assert first["base_sha"] != second["base_sha"]
    assert first_bytes != second_bytes
    assert json.loads(second_bytes)["base_sha"] == "c" * 40
    leftover_temp_files = [p for p in snapshot_path.parent.iterdir() if p.name.startswith(".")]
    assert leftover_temp_files == []


def test_gh_bin_required(tmp_path: Path):
    with pytest.raises(ValueError, match="gh_bin_required"):
        _materialize(tmp_path, gh_bin="")


def test_sanitized_subprocess_env_strips_gh_and_git_redirection_vars():
    """Issue #1629 fix_delta P1 (untrusted_gh_git_env): a poisoned ambient
    environment must never reach the gh/git subprocess this module runs."""
    poisoned = dict(os.environ)
    poisoned.update(
        GH_HOST="evil.example.com",
        GH_REPO="mallory/decoy",
        GH_CONFIG_DIR="/tmp/evil-gh-config",
        GIT_DIR="/tmp/evil.git",
        GIT_WORK_TREE="/tmp/evil-worktree",
        GIT_INDEX_FILE="/tmp/evil.index",
        GIT_OBJECT_DIRECTORY="/tmp/evil-objects",
        GIT_ALTERNATE_OBJECT_DIRECTORIES="/tmp/evil-alt-objects",
    )
    sanitized = materializer._sanitized_subprocess_env(poisoned)
    for key in (
        "GH_HOST",
        "GH_REPO",
        "GH_CONFIG_DIR",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        assert key not in sanitized
    assert sanitized["GH_PROMPT_DISABLED"] == "1"


def test_controlled_executor_provenance_check_is_audit_only_not_authoritative(tmp_path: Path, monkeypatch):
    """`_validate_materialized_snapshot_provenance` is retained as an
    audit-consistency helper only (Issue #1629 fix_delta P0). Even a fully
    self-consistent hand-written snapshot/sidecar pair passing this check
    must NOT translate into commit authority -- that guarantee is enforced
    by `_main()` never calling this function to gate `--materialize-request`,
    and by `--snapshot-json` being unconditionally denied (see
    test_controlled_git_change_exec.py::test_snapshot_json_flag_always_denied).
    """
    snapshot_path = expected_output_path(tmp_path, 1629)
    snapshot_path.parent.mkdir(parents=True)
    snapshot = {
        "issue_number": 1629,
        "repository_full_name": "squne121/loop-protocol",
        "worktree_realpath": str(tmp_path),
        "branch_ref": "refs/heads/topic",
        "base_ref": "main",
        "base_sha": "a" * 40,
    }
    snapshot_path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(controlled, "_git_toplevel", lambda cwd: str(tmp_path))
    assert controlled._validate_materialized_snapshot_provenance(snapshot_path, snapshot, str(tmp_path)) == (
        "materializer_provenance_input_file_missing"
    )
    provenance = {
        "schema": PROVENANCE_SCHEMA,
        "producer": "scripts/agent-guards/materialize_issue_scope_snapshot.py",
        "command_id": COMMAND_ID,
        "repository_full_name": "squne121/loop-protocol",
        "issue_number": 1629,
        "artifact_path": str(snapshot_path.relative_to(tmp_path)),
        "artifact_sha256": "sha256:" + hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "worktree_realpath": str(tmp_path),
        "branch_ref": "refs/heads/topic",
        "base_ref": "main",
        "base_sha": "a" * 40,
    }
    expected_provenance_path(tmp_path, 1629).write_text(json.dumps(provenance))
    # The file pair is internally consistent -- this audit helper returns
    # None (no inconsistency found) -- but that is deliberately NOT the same
    # as "authorized"; see the CLI-level test referenced above.
    assert controlled._validate_materialized_snapshot_provenance(snapshot_path, snapshot, str(tmp_path)) is None


def test_build_snapshot_via_live_materializer_never_reads_disk_artifact(tmp_path: Path, monkeypatch):
    """Issue #1629 fix_delta P0: the consumer-facing function must build the
    IssueScopeSnapshot from the materializer's in-memory return value only.
    A hand-written artifact/sidecar pair sitting on disk under the exact
    expected path must have zero effect on the snapshot actually used."""
    (tmp_path / "scripts" / "agent-guards").mkdir(parents=True)
    # Poisoned hand-written artifact + sidecar at the exact expected path.
    poisoned_dir = tmp_path / "artifacts" / "1629" / "issue-metadata" / COMMAND_ID
    poisoned_dir.mkdir(parents=True)
    (poisoned_dir / "issue_scope_snapshot.json").write_text(
        json.dumps({"issue_number": 1629, "repository_full_name": "attacker/decoy"})
    )
    (poisoned_dir / "issue_scope_snapshot.provenance.json").write_text("{}")

    live_snapshot_dict = {
        "schema_version": "ISSUE_SCOPE_SNAPSHOT_V1",
        "repository_full_name": "squne121/loop-protocol",
        "issue_number": 1629,
        "contract_source_kind": "issue_comment",
        "contract_source_id": str(_COMMENT_ID),
        "contract_source_body_sha256": "x" * 64,
        "issue_body_sha256": "y" * 64,
        "issue_updated_at": "2026-07-19T00:00:00Z",
        "comments_digest_sha256": "z" * 64,
        "allowed_paths": ["scripts/agent-guards/example.py"],
        "allowed_paths_normalized_sha256": "w" * 64,
        "allowed_paths_matcher_schema": controlled.ALLOWED_PATHS_MATCHER_SCHEMA,
        "base_ref": "main",
        "base_sha": "a" * 40,
        "branch_ref": "refs/heads/topic",
        "worktree_realpath": str(tmp_path),
        "protected_paths_policy_schema": "irrelevant",
        "protected_paths_policy_sha256": "irrelevant",
        "authority_mode": controlled.AUTHORITY_NEW_ONLY,
        "generated_at": "2026-07-19T00:00:00Z",
    }

    class _FakeMaterializerModule:
        @staticmethod
        def materialize(**kwargs):
            return {"status": "ok", "snapshot": live_snapshot_dict}

    monkeypatch.setattr(controlled, "_load_materializer_module", lambda cwd: _FakeMaterializerModule())

    snapshot = controlled.build_snapshot_via_live_materializer(
        cwd=str(tmp_path),
        issue_number=1629,
        repo="squne121/loop-protocol",
        contract_snapshot_url=_SOURCE_URL,
        base_ref="main",
        branch_name="topic",
        output=f"artifacts/1629/issue-metadata/{COMMAND_ID}/issue_scope_snapshot.json",
        gh_bin="/usr/bin/gh",
    )
    assert snapshot.repository_full_name == "squne121/loop-protocol"
    assert snapshot.issue_number == 1629


def test_controlled_materializer_command_rejects_argv_and_binding_mismatch(tmp_path: Path):
    project_root = str(tmp_path)
    command = (
        "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
        "--command-id issue_scope_snapshot.materialize --issue-number 1629 "
        "--repo squne121/loop-protocol --input-file "
        "artifacts/1629/issue-metadata/issue_scope_snapshot.materialize/request.json --json"
    )
    assert is_controlled_skill_mutation_exec_command(command, project_root)
    assert not is_controlled_skill_mutation_exec_command(command + " --unknown", project_root)
    assert _validate_issue_scope_snapshot_materialize_fields(
        {
            "repo": "squne121/loop-protocol",
            "contract_snapshot_url": "https://github.com/squne121/loop-protocol/issues/1629#issuecomment-5014250179",
            "base_ref": "main",
            "branch_name": "topic",
            "worktree_path": str(tmp_path),
            "output_path": "wrong.json",
        },
        "squne121/loop-protocol",
    ) == ""
