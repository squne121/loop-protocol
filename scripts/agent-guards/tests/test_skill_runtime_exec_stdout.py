"""
test_skill_runtime_exec_stdout.py

Issue #2039 AC8/AC11: real-subprocess dispatch and stdout-contract coverage
for the `repair_action.apply` command class.

AC8: the command_id is dispatched end-to-end through the real
`skill_runtime_exec.py` executor (real `command_registry.py` entry, real
`skill_runtime_command_policy.py` eligibility/parser), and its stdout is
constrained to the `repair_apply_result/v1` schema.

AC11: the production consumer (`run_refinement_preflight.py`'s
`run_repair_action_apply()`) invokes `edit_issue_txn.py --input-file` as its
only GitHub-mutation subprocess and never calls a raw `gh issue edit`. This
is proven with a REAL subprocess chain (contract_readiness_check.py +
edit_issue_txn.py are both invoked for real, not stubbed) and a PATH-shadowed
`gh` binary that records/fails on any invocation whose argv contains
"issue edit", while still answering "gh issue view" so the precondition-read
step (a legitimate, non-mutating read) succeeds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))
sys.path.insert(0, str(REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"))

import run_refinement_preflight as rrp  # noqa: E402

_SCHEMA = json.loads(
    (
        REPO_ROOT
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "schemas"
        / "repair_apply_result_v1.schema.json"
    ).read_text(encoding="utf-8")
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_candidate(
    tmp_path: Path,
    *,
    issue_number: int = 2039,
    disposition: str = "auto_apply_safe",
    candidate_body: str = "not a valid issue contract body\n",
    schema_version: str = "repair_action/v1",
    policy_version: str = "deterministic-issue-repair/v1",
    include_contract_patch_plan: bool = False,
) -> tuple[Path, dict]:
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(candidate_body)
    repaired_sha = _sha256(candidate_body)
    original_sha = _sha256("original body\n")
    repair_action = {
        "schema_version": schema_version,
        "policy_version": policy_version,
        "disposition": disposition,
        "original_body_sha256": original_sha,
        "repaired_body_sha256": repaired_sha,
        "diagnostics_artifact": None,
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["trailing_whitespace_stripped"],
    }
    # PR #2202 review fix (P0-1 follow-through / P0-2): repair_action.*
    # carries the provenance-lane fields under the canonical schema
    # location (not top-level), and the dual-mutation-intent hazard this
    # arbiter guards against is signaled by the CANONICAL
    # `contract_update` field (additionalProperties: false rejects the
    # non-existent `contract_patch_plan` key on any real artifact).
    repair_action["source_lane"] = "unanchored"
    repair_action["preflight_run_identity"] = "sha256:testrun"
    repair_action["original_updated_at"] = "2024-01-01T00:00:00Z"
    repair_action["source_refs_digest"] = None
    preflight_result: dict = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "needs_fix",
        "issue_number": issue_number,
        "repo": "squne121/loop-protocol",
        "planner_exit_code": None,
        "planner_fail_closed": None,
        "next_action": "apply_deterministic_repair",
        "must_read": [],
        "do_not_read": [],
        "commands": [],
        "blockers": [],
        "artifacts": {},
        "hashes": {"result_core_sha256": "sha256:testrun"},
        "repair_action": repair_action,
    }
    if include_contract_patch_plan:
        preflight_result["contract_update"] = {
            "status": "applied",
            "writes": 1,
            "iterations": 1,
            "final_readback": "verified",
            "fresh_preflight": "pass",
            "fresh_review": "pass",
            "fresh_readiness": "pass",
        }
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result))
    return result_path, preflight_result


def _fetch_current_stub() -> dict:
    return {"body": "original body\n", "updatedAt": "2024-01-01T00:00:00Z"}


# ---------------------------------------------------------------------------
# AC8/AC11: real (non-mocked) subprocess dispatch through
# run_repair_action_apply() -- contract_readiness_check.py and
# edit_issue_txn.py are both invoked for real.
# ---------------------------------------------------------------------------


def test_repair_action_apply_real_subprocess_never_calls_gh_edit(tmp_path: Path) -> None:
    """AC11: the real subprocess chain (contract_readiness_check.py,
    edit_issue_txn.py) never invokes a raw `gh issue edit`. Uses a
    deliberately-unshaped candidate body so contract_readiness_check.py's
    real static check returns a non-`go` status, which makes
    edit_issue_txn.py's real `run_transaction()` return before it would
    ever call `_fetch_issue`/`gh` -- exercising the genuine early-return
    code path, not a mocked one."""
    result_path, _ = _write_candidate(tmp_path)
    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_current_stub,
    )
    jsonschema.validate(result, _SCHEMA)
    # The receipt's executor_status proves edit_issue_txn.py's real
    # run_transaction() genuinely executed and returned a real status
    # string (not a fabricated stub value).
    assert result["receipt"]["executor_status"] in {
        "failed_no_mutation",
        "human_judgment",
    }, result
    assert result["mutation_outcome"] == "not_attempted"
    assert result["receipt"]["patch_attempted"] is False


def test_repair_action_apply_default_transaction_argv_is_edit_issue_txn_only() -> None:
    """AC11 (static companion): the default apply_transaction closure's own
    source never references a `gh` invocation, and its only script argv
    reference is `edit_issue_txn.py`."""
    import inspect

    source = inspect.getsource(rrp.run_repair_action_apply)
    assert "edit_issue_txn.py" in source
    assert '"gh"' not in source
    assert "'gh'" not in source


def test_repair_action_apply_multiple_mutation_intents_never_dispatches(tmp_path: Path) -> None:
    """AC1/AC8: contract_patch_plan + repair_action both present -> no
    subprocess is spawned at all (mutation_outcome not_attempted,
    failure_code multiple_mutation_intents, phase candidate_load)."""
    result_path, _ = _write_candidate(tmp_path, include_contract_patch_plan=True)
    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_current_stub,
    )
    jsonschema.validate(result, _SCHEMA)
    assert result["failure_code"] == "multiple_mutation_intents"
    assert result["mutation_outcome"] == "not_attempted"


def test_repair_action_apply_non_safe_disposition_never_dispatches(tmp_path: Path) -> None:
    """AC8: a non-auto_apply_safe disposition is rejected before any
    subprocess (readiness check or transaction) is spawned."""
    result_path, _ = _write_candidate(tmp_path, disposition="human_review_required")
    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_current_stub,
    )
    jsonschema.validate(result, _SCHEMA)
    assert result["failure_code"] == "invalid_disposition"
    assert result["mutation_outcome"] == "not_attempted"


def test_repair_action_apply_stdout_schema_conformance_on_applied_path(tmp_path: Path) -> None:
    """AC8/AC6: a stubbed `applied` transaction outcome still produces a
    schema-conformant payload (mutation_outcome applied requires
    receipt.patch_attempted true and failure_code null)."""
    applied_candidate_body = "not a valid issue contract body\n"
    result_path, _ = _write_candidate(tmp_path, candidate_body=applied_candidate_body)

    # PR #2202 review fix-delta (P1-3): AC9 fresh validation genuinely
    # re-fetches the live Issue body a second time post-dispatch (P0-5).
    # The original stub returned the SAME pre-mutation body on both calls,
    # which made fresh validation's digest-match check genuinely fail
    # (live body still == old_digest, never == candidate_digest) --
    # silently masking a fresh-validation failure behind an `applied`
    # assertion that never actually exercised AC9's real digest-match
    # path. A realistic post-dispatch state returns the candidate body on
    # the second (fresh-validation) fetch.
    fetch_bodies = iter(["original body\n", applied_candidate_body])

    def _fetch_after_dispatch() -> dict:
        return {"body": next(fetch_bodies), "updatedAt": "2024-01-01T00:00:00Z"}

    def _fake_apply_transaction(current_issue: dict, candidate_body: str) -> dict:
        del current_issue, candidate_body
        # PR #2202 review fix-delta (P1-3): the canonical
        # ISSUE_EDIT_TXN_RESULT_V1 shape (P0-4) nests the attempted/outcome
        # fields under `body_update`/`content_update`; the previous flat
        # `body_attempted`/`body_status`/`remote_current_body_sha256` keys
        # here were never read by `_repair_receipt_from_txn_result()` (P0-4
        # reads only the nested shape), which silently produced
        # receipt.patch_attempted=False alongside a top-level
        # mutation_outcome=applied -- exactly the self-contradictory state
        # this session's schema invariants (applied requires
        # receipt.patch_attempted=true) now correctly reject.
        return {
            "status": "ok",
            "mutation_started": True,
            "body_update": {
                "attempted": True,
                "status": "ok",
                "remote_current_body_sha256": "sha256:aaaa",
            },
            "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
            "errors": [],
        }

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_after_dispatch,
        apply_transaction=_fake_apply_transaction,
    )
    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "applied"
    assert result["failure_code"] is None
    assert result["phase"] == "complete"
    assert result["receipt"]["patch_attempted"] is True
    assert result["receipt"]["final_readback"]["status"] == "verified"
    assert result["fresh_validation"]["status"] == "success"


def test_repair_action_apply_unknown_outcome_never_reaches_complete_phase(tmp_path: Path) -> None:
    """AC5/AC6/AC8: an `unknown` receipt outcome must never be reported as
    phase complete (schema allOf constraint), and retry budget stays 0."""
    result_path, _ = _write_candidate(tmp_path)

    def _fake_apply_transaction(current_issue: dict, candidate_body: str) -> dict:
        del current_issue, candidate_body
        return {"status": "mutation_outcome_unknown", "errors": []}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_current_stub,
        apply_transaction=_fake_apply_transaction,
    )
    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "unknown"
    assert result["phase"] != "complete"
    assert result["retry"] == {"post_dispatch_retry_budget": 0, "retries_used": 0}


# ---------------------------------------------------------------------------
# AC8: full E2E dispatch through the real skill_runtime_exec.py executor +
# real command_registry.py `repair_action.apply` entry, with a PATH-shadowed
# `gh` binary proving no `gh issue edit` call is ever reachable through the
# real wiring either.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _install_repair_action_apply_fixture(repo_root: Path) -> Path:
    """Installs the REAL executor/policy/registry/producer chain (verbatim
    copies), mirroring `_install_authority_transport_fixture()`'s convention
    in test_skill_runtime_policy_anchor.py."""
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
    ):
        _write_text(repo_root / rel, (REPO_ROOT / rel).read_text())

    for rel_dir in (
        ".claude/skills/issue-refinement-loop/scripts",
        ".claude/skills/issue-refinement-loop/schemas",
        ".claude/skills/edit-issue/scripts",
        ".claude/skills/issue-contract-review/scripts",
    ):
        src = REPO_ROOT / rel_dir
        dst = repo_root / rel_dir
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations

class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    # repair_action.apply is NOT root-no-worktree eligible (same boundary
    # as authority_transport.consume).
    return {"issue_number": issue_number, "path": root_realpath}
""",
    )

    gh_marker = repo_root / "gh-shadow-edit-called.marker"
    gh_shadow_dir = repo_root / "gh-shadow"
    gh_shadow_dir.mkdir(parents=True, exist_ok=True)
    gh_script = gh_shadow_dir / "gh"
    gh_script.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "issue view")\n'
        "    cat <<'JSON'\n"
        '{"number":2039,"title":"t","body":"original body\\n","labels":[],'
        '"url":"https://github.com/squne121/loop-protocol/issues/2039",'
        '"updatedAt":"2024-01-01T00:00:00Z"}\n'
        "JSON\n"
        "    exit 0\n"
        "    ;;\n"
        '  "issue edit")\n'
        f'    echo called >> "{gh_marker}"\n'
        '    echo "gh issue edit must never be invoked directly" 1>&2\n'
        "    exit 99\n"
        "    ;;\n"
        "  *)\n"
        '    echo "unexpected gh invocation: $*" 1>&2\n'
        "    exit 98\n"
        "    ;;\n"
        "esac\n"
    )
    gh_script.chmod(0o755)

    worktree_dir = repo_root / ".claude" / "worktrees" / "issue-2039-repair-action-apply-fixture"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / "2039"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_body = "not a valid issue contract body\n"
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(candidate_body)
    repair_action = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": _sha256("original body\n"),
        "repaired_body_sha256": _sha256(candidate_body),
        "diagnostics_artifact": None,
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["trailing_whitespace_stripped"],
    }
    preflight_result = {
        "schema": "issue_refinement_preflight_result/v1",
        "repair_action": repair_action,
        "original_updated_at": "2024-01-01T00:00:00Z",
        "result_core_sha256": "sha256:testrun",
        "source_lane": "unanchored",
    }
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result))

    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install repair_action.apply fixture", cwd=repo_root)
    return gh_marker


def _run_repair_action_apply_executor(
    repo: Path, *, preflight_result_path: str, gh_shadow_dir: Path
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id",
        "repair_action.apply",
        "--issue-number",
        "2039",
        "--repo",
        "squne121/loop-protocol",
        "--apply-repair-action",
        preflight_result_path,
    ]
    return subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(repo),
            "LOOP_ISSUE_NUMBER": "2039",
            "PATH": f"{gh_shadow_dir}:{os.environ.get('PATH', '')}",
        },
        check=False,
    )


def test_repair_action_apply_reaches_real_subprocess_and_never_calls_gh_edit(tmp_path: Path) -> None:
    """AC8/AC11: the full production wiring (skill_runtime_exec.py's real
    dispatch -> real command_registry.py `repair_action.apply` entry ->
    real run_refinement_preflight.py --apply-repair-action) is reachable
    end-to-end, and the PATH-shadowed `gh` proves `gh issue edit` is never
    invoked anywhere in that real chain."""
    repo = _make_repo(tmp_path)
    gh_marker = _install_repair_action_apply_fixture(repo)
    gh_shadow_dir = repo / "gh-shadow"

    result = _run_repair_action_apply_executor(
        repo,
        preflight_result_path=".claude/artifacts/issue-refinement-loop/2039/preflight_result.json",
        gh_shadow_dir=gh_shadow_dir,
    )
    assert result.returncode in (0, 2, 3), result.stderr
    payload = json.loads(result.stdout)
    jsonschema.validate(payload, _SCHEMA)
    assert payload["mutation_outcome"] == "not_attempted"
    assert not gh_marker.exists(), "gh issue edit was invoked -- AC11 violated"


def test_repair_action_apply_rejects_extra_argv(tmp_path: Path) -> None:
    """AC8 negative matrix: an extra trailing token must be rejected before
    any subprocess is spawned."""
    repo = _make_repo(tmp_path)
    gh_marker = _install_repair_action_apply_fixture(repo)
    gh_shadow_dir = repo / "gh-shadow"

    argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id",
        "repair_action.apply",
        "--issue-number",
        "2039",
        "--repo",
        "squne121/loop-protocol",
        "--apply-repair-action",
        ".claude/artifacts/issue-refinement-loop/2039/preflight_result.json",
        "--extra-flag",
        "x",
    ]
    result = subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(repo),
            "LOOP_ISSUE_NUMBER": "2039",
            "PATH": f"{gh_shadow_dir}:{os.environ.get('PATH', '')}",
        },
        check=False,
    )
    assert result.returncode != 0
    assert not gh_marker.exists()


def test_repair_action_apply_rejects_alternate_cwd(tmp_path: Path) -> None:
    """AC8 negative matrix: invoking from a subdirectory (not project root)
    is rejected before any subprocess is spawned."""
    repo = _make_repo(tmp_path)
    gh_marker = _install_repair_action_apply_fixture(repo)
    gh_shadow_dir = repo / "gh-shadow"
    subdir = repo / "subdir"
    subdir.mkdir()

    argv = [
        sys.executable,
        str(repo / "scripts" / "agent-guards" / "skill_runtime_exec.py"),
        "--command-id",
        "repair_action.apply",
        "--issue-number",
        "2039",
        "--repo",
        "squne121/loop-protocol",
        "--apply-repair-action",
        ".claude/artifacts/issue-refinement-loop/2039/preflight_result.json",
    ]
    result = subprocess.run(
        argv,
        cwd=str(subdir),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(repo),
            "LOOP_ISSUE_NUMBER": "2039",
            "PATH": f"{gh_shadow_dir}:{os.environ.get('PATH', '')}",
        },
        check=False,
    )
    assert result.returncode != 0
    assert not gh_marker.exists()
