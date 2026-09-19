"""
test_issue_metadata_write_capability.py

Issue #2584: `ISSUE_METADATA_WRITE_COMMAND_IDS` / `TMP_DIRECTORY_NODE_HOUSEKEEPING_
COMMAND_IDS` capability regression coverage.

`repair_action.apply` / `structural_repair_action.apply` / `authority_transport.
consume` can all route a real GitHub Issue-body mutation through the SAME
`edit_issue_txn.py` transaction core that `contract_update.run.with_anchor` /
`contract_update.run.with_human_context` already use. Before this Issue, a
successful mutation by any of the 3 non-`contract_update.run.*` members was
misreported as `unauthorized_write_path` (false negative) because
`_allowed_artifact_roots()` only granted the `artifacts/{issue}/issue-metadata/`
write root to the original 2 `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS`
members, and the repo-root `tmp/` directory-node housekeeping exemption was
similarly scoped to those same 2 commands only.

This module asserts, DIRECTLY (never via `rg` string-presence matching):

- AC1/AC9: `ISSUE_METADATA_WRITE_COMMAND_IDS` / `TMP_DIRECTORY_NODE_HOUSEKEEPING_
  COMMAND_IDS` are each a genuinely independent frozenset containing exactly the
  SAME 5 command_ids, and `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS` stays
  scoped to its original 2.
- AC2: `_allowed_artifact_roots()`'s actual return value reflects that
  membership for every command_id (both members and non-members).
- AC3/AC7: `command_registry.py` / `skill_runtime_command_policy.py`'s
  `allowed_write_roots` for the 3 additional command_ids match the SAME 2-root
  shape `contract_update.run.*` already uses.
- AC4/AC5/AC6/AC10: production-shaped mutation success/rejection scenarios,
  driven through the REAL `skill_runtime_exec.py` -> `run_refinement_
  preflight.py` -> `edit_issue_txn.py` -> `controlled_skill_mutation_exec.py`
  subprocess chain, with ONLY the GitHub network boundary (`gh`) faked.
- AC8: covered by the existing regression suites this Issue's Verification
  Commands re-run unmodified (`test_skill_runtime_policy_commands.py` /
  `test_skill_runtime_exec_unauthorized_write_path.py` /
  `test_skill_runtime_exec_stdout.py`).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GUARDS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_GUARDS_DIR))
sys.path.insert(0, str(REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"))

import skill_runtime_exec as sre  # noqa: E402
import command_registry  # noqa: E402
from skill_runtime_command_policy import SKILL_RUNTIME_COMMAND_POLICY_V2  # noqa: E402

_EXPECTED_FIVE_COMMAND_IDS = frozenset(
    {
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
        "authority_transport.consume",
        "repair_action.apply",
        "structural_repair_action.apply",
    }
)

_EXPECTED_TWO_ROOT_LIST = [
    ".claude/artifacts/issue-refinement-loop/{active_issue}/",
    "artifacts/{active_issue}/issue-metadata/",
]


# ---------------------------------------------------------------------------
# AC1 / AC9: direct frozenset-membership assertions (never `rg` string search)
# ---------------------------------------------------------------------------


def test_issue_metadata_write_command_ids_contains_exact_five() -> None:
    assert sre.ISSUE_METADATA_WRITE_COMMAND_IDS == _EXPECTED_FIVE_COMMAND_IDS


def test_tmp_directory_node_housekeeping_command_ids_matches_issue_metadata_set() -> None:
    # AC9: a SEPARATE frozenset carrying the SAME 5 command_ids, asserted by
    # direct set equality -- not inferred from a shared literal / string match.
    assert sre.TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS == _EXPECTED_FIVE_COMMAND_IDS
    assert sre.TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS == sre.ISSUE_METADATA_WRITE_COMMAND_IDS


def test_tmp_directory_node_housekeeping_is_not_the_same_object_as_issue_metadata_set() -> None:
    # Independence: equal membership, but genuinely separate frozenset objects
    # -- never one name aliasing the other.
    assert sre.TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS is not sre.ISSUE_METADATA_WRITE_COMMAND_IDS


def test_contract_update_mutation_dedicated_command_ids_unchanged_two_commands() -> None:
    # In Scope item 2 / Stop Conditions: this frozenset must remain scoped to
    # exactly its original 2 commands -- the new capabilities are independent
    # additions, never a widening of this set.
    assert sre.CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS == frozenset(
        {"contract_update.run.with_anchor", "contract_update.run.with_human_context"}
    )


def test_tmp_directory_node_housekeeping_is_not_same_object_as_contract_update_set() -> None:
    assert (
        sre.TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS
        is not sre.CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS
    )


# ---------------------------------------------------------------------------
# AC2: `_allowed_artifact_roots()` actual return value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command_id", sorted(_EXPECTED_FIVE_COMMAND_IDS))
def test_allowed_artifact_roots_grants_issue_metadata_root_for_each_member(
    tmp_path: Path, command_id: str
) -> None:
    roots = sre._allowed_artifact_roots(str(tmp_path), "12345", command_id)
    assert len(roots) == 2
    assert roots[0] == tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "12345"
    assert roots[1] == tmp_path / "artifacts" / "12345" / "issue-metadata"


@pytest.mark.parametrize(
    "command_id",
    ["preflight.run", "decide.run", "authority_transport.produce", "", "unknown.command_id"],
)
def test_allowed_artifact_roots_stays_single_root_for_non_members(tmp_path: Path, command_id: str) -> None:
    roots = sre._allowed_artifact_roots(str(tmp_path), "12345", command_id)
    assert len(roots) == 1
    assert roots[0] == tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "12345"


# ---------------------------------------------------------------------------
# AC9 static companion: the two `tmp/` housekeeping call sites gate on the
# two frozensets via genuinely INDEPENDENT `if` branches, never a merged
# boolean expression.
# ---------------------------------------------------------------------------


def test_tmp_directory_node_gating_is_two_independent_if_branches_in_source() -> None:
    snapshot_source = inspect.getsource(sre._snapshot_repo_paths)
    assert "if command_id in CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS:" in snapshot_source
    assert "if command_id in TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS:" in snapshot_source
    assert "CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS or" not in snapshot_source
    assert "TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS or" not in snapshot_source

    dispatch_source = inspect.getsource(sre._dispatch_child_and_check_postconditions)
    assert "if command_id in CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS:" in dispatch_source
    assert "if command_id in TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS:" in dispatch_source
    assert "CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS or" not in dispatch_source
    assert "TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS or" not in dispatch_source


# ---------------------------------------------------------------------------
# AC3 / AC7: registry / policy parity (direct dict comparison, never `rg`)
# ---------------------------------------------------------------------------


def test_command_registry_allowed_write_roots_matches_for_repair_action_commands() -> None:
    for command_id in ("repair_action.apply", "structural_repair_action.apply"):
        entry = command_registry.REGISTRY[command_id]
        assert entry["allowed_write_roots"] == _EXPECTED_TWO_ROOT_LIST, command_id


def test_skill_runtime_command_policy_allowed_write_roots_matches_for_all_three() -> None:
    eligible_command_ids = SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]
    for command_id in ("authority_transport.consume", "repair_action.apply", "structural_repair_action.apply"):
        entry = eligible_command_ids[command_id]
        assert entry["allowed_write_roots"] == _EXPECTED_TWO_ROOT_LIST, command_id


def test_contract_update_run_commands_still_use_the_same_two_root_shape() -> None:
    # Regression guard: the pre-existing 2 `contract_update.run.*` members'
    # own `allowed_write_roots` must stay byte-identical to the shape this
    # Issue widens the 3 new members to match -- never accidentally diverge.
    for command_id in ("contract_update.run.with_anchor", "contract_update.run.with_human_context"):
        registry_entry = command_registry.REGISTRY[command_id]
        assert registry_entry["allowed_write_roots"] == _EXPECTED_TWO_ROOT_LIST, command_id


# ---------------------------------------------------------------------------
# AC4 / AC5 / AC6 / AC10: production-shaped mutation success/rejection E2E.
#
# The full production chain (`skill_runtime_exec.py` -> real `command_registry.
# py` entry -> real `skill_runtime_command_policy.py` eligibility -> real
# `run_refinement_preflight.py` -> real `edit_issue_txn.py` -> real
# `controlled_skill_mutation_exec.py`) is exercised via a REAL subprocess
# chain. ONLY the GitHub network boundary (`gh`) is faked -- every script in
# between is the genuine production file, copied verbatim into a scratch git
# repository.
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.venv/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


# A minimal, no-network fake `gh` (Python, for portable JSON handling).
#
# Its fake "remote Issue" state is deliberately persisted OUTSIDE the git
# working tree (`SKILL_RUNTIME_TEST_FAKE_GH_STATE_DIR`, a directory this
# fixture places as a SIBLING of the scratch repo, never inside it). If this
# test-only state file lived inside the repo, `controlled_skill_mutation_
# exec.py`'s OWN inner `_check_no_tracked_changes()` postcondition (fully
# independent of `skill_runtime_exec.py`'s outer check this Issue widens)
# would see the state file's content change during the mutation and
# misreport a `postcondition_tracked_changes_detected` failure -- a fixture
# artifact, never evidence of a genuine unauthorized write.
_FAKE_GH_SOURCE = '''#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

argv = sys.argv[1:]


def _state_path(issue: str) -> Path:
    state_dir = os.environ.get("SKILL_RUNTIME_TEST_FAKE_GH_STATE_DIR")
    if not state_dir:
        print("SKILL_RUNTIME_TEST_FAKE_GH_STATE_DIR not set", file=sys.stderr)
        raise SystemExit(70)
    return Path(state_dir) / issue / "fake_remote_issue.json"


def _fail(msg, code=64):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


if len(argv) >= 3 and argv[0] == "issue" and argv[1] == "view":
    issue = argv[2]
    state = json.loads(_state_path(issue).read_text(encoding="utf-8"))
    print(json.dumps({"title": state["title"], "body": state["body"], "updatedAt": state["updatedAt"]}))
    raise SystemExit(0)

if argv and argv[0] == "api":
    m = None
    for tok in argv:
        mm = re.match(r"^repos/[^/]+/[^/]+/issues/(\\d+)$", tok)
        if mm:
            m = mm
            break
    if m is None:
        _fail(f"unexpected_fake_gh_argv_no_issue_segment: {argv}")
    issue = m.group(1)
    state_path = _state_path(issue)
    if "--method" in argv:
        method_idx = argv.index("--method")
        method = argv[method_idx + 1] if method_idx + 1 < len(argv) else ""
        if method != "PATCH":
            _fail(f"unexpected_fake_gh_method: {method}")
        input_idx = argv.index("--input") + 1
        patch = json.loads(Path(argv[input_idx]).read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["title"] = patch["title"]
        state["body"] = patch["body"]
        state["updatedAt"] = "2026-09-20T00:00:01Z"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        # AC5/AC10(d) regression harness only: deterministically inject a
        # stray write AFTER the real PATCH lands, at a caller-chosen relative
        # path -- proving the outer unauthorized-write check can fire on a
        # span where the inner mutation genuinely already succeeded.
        stray_rel = os.environ.get("SKILL_RUNTIME_TEST_INJECT_STRAY_WRITE_AFTER_PATCH")
        if stray_rel:
            stray_path = Path.cwd() / stray_rel
            stray_path.parent.mkdir(parents=True, exist_ok=True)
            stray_path.write_text("stray-unauthorized-write\\n", encoding="utf-8")
        print(json.dumps({"ok": True}))
        raise SystemExit(0)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "title": state["title"],
                "body": state["body"],
                "updatedAt": state["updatedAt"],
                "isPullRequest": False,
            }
        )
    )
    raise SystemExit(0)

_fail(f"unexpected_fake_gh_argv: {argv}")
'''


def _install_fixture(repo_root: Path, trusted_gh_bin: Path) -> None:
    """Installs the REAL registry/policy/executor/producer/transaction chain
    (verbatim copies), mirroring the existing `_install_repair_action_apply_
    fixture()` convention in `test_skill_runtime_exec_stdout.py`."""
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        "scripts/agent-guards/controlled_skill_mutation_exec.py",
        "scripts/agent-guards/controlled_skill_mutation_policy.py",
    ):
        _write_text(repo_root / rel, (REPO_ROOT / rel).read_text())

    for rel_dir in (
        ".claude/skills/issue-refinement-loop/scripts",
        ".claude/skills/issue-refinement-loop/schemas",
        ".claude/skills/edit-issue/scripts",
        ".claude/skills/issue-contract-review/scripts",
        ".claude/skills/create-issue/scripts",
    ):
        src = REPO_ROOT / rel_dir
        dst = repo_root / rel_dir
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)

    src_issue_template = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
    if src_issue_template.is_dir():
        shutil.copytree(src_issue_template, repo_root / ".github" / "ISSUE_TEMPLATE", dirs_exist_ok=True)
    # `docs/dev/github-ops.md`'s `ISSUE_KIND_POLICY_V1` block is the SSOT
    # `contract_readiness_check.py`'s existing-issue-readiness check loads.
    shutil.copytree(REPO_ROOT / "docs" / "dev", repo_root / "docs" / "dev", dirs_exist_ok=True)

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations


class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    # repair_action.apply / structural_repair_action.apply are NOT
    # root-no-worktree eligible (same boundary as authority_transport.consume).
    return {"issue_number": issue_number, "path": root_realpath}
""",
    )

    # PATCH 1: `skill_runtime_exec.py`'s `_safe_path_entries()` -- trusts the
    # fixture-owned fake `gh` directory (every child dispatch's own env PATH
    # is sanitized to exactly this list, so the caller's ambient PATH is
    # never inherited).
    executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    executor_source = executor_path.read_text(encoding="utf-8")
    default_safe_path_return = (
        '    return _dedupe_path_entries([*_trusted_toolchain_dirs("uv"), *_SYSTEM_STANDARD_PATH_DIRS])\n'
    )
    fixture_safe_path_return = (
        f"    return _dedupe_path_entries([{str(trusted_gh_bin)!r}, *_trusted_toolchain_dirs(\"uv\"), "
        "*_SYSTEM_STANDARD_PATH_DIRS])\n"
    )
    assert default_safe_path_return in executor_source, "safe_path_return literal not found"
    executor_source = executor_source.replace(default_safe_path_return, fixture_safe_path_return)
    _write_text(executor_path, executor_source)

    # PATCH 2: `controlled_skill_mutation_exec.py`'s `_GH_TRUSTED_PATHS` --
    # this module resolves `gh` via its OWN trusted-path constant, independent
    # of the outer dispatch's env PATH.
    controlled_executor_path = repo_root / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
    controlled_executor_source = controlled_executor_path.read_text(encoding="utf-8")
    default_gh_trusted_paths = '_GH_TRUSTED_PATHS = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"'
    fixture_gh_trusted_paths = (
        f'_GH_TRUSTED_PATHS = {str(trusted_gh_bin)!r} + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"'
    )
    assert default_gh_trusted_paths in controlled_executor_source, "_GH_TRUSTED_PATHS literal not found"
    _write_text(
        controlled_executor_path,
        controlled_executor_source.replace(default_gh_trusted_paths, fixture_gh_trusted_paths),
    )

    trusted_gh_bin.mkdir(parents=True, exist_ok=True)
    gh_script = trusted_gh_bin / "gh"
    gh_script.write_text(_FAKE_GH_SOURCE, encoding="utf-8")
    gh_script.chmod(0o755)

    for rel in ("pyproject.toml", "uv.lock"):
        _write_text(repo_root / rel, (REPO_ROOT / rel).read_text())
    # Fully materialize the project environment BEFORE the executor snapshots
    # the fixture repository -- `repair_action.apply` / `structural_repair_
    # action.apply` are not in `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`, so
    # (unlike the 4 preflight profiles + 2 contract_update profiles) they get
    # no automatic pre-dispatch `.venv` warm-up; a cold `uv run` mid-dispatch
    # would otherwise write new `.venv/` package files inside the monitored
    # window and be misreported as an unauthorized write.
    subprocess.run(["uv", "sync", "--locked"], cwd=str(repo_root), check=True, capture_output=True, text=True)

    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install issue-metadata-write-capability fixture", cwd=repo_root)


def _write_remote_state(state_dir: Path, issue_number: str, body: str, title: str = "t") -> None:
    artifact_dir = state_dir / issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps({"title": title, "body": body, "updatedAt": "2024-01-01T00:00:00Z"}),
        encoding="utf-8",
    )


def _write_repair_action_preflight_result(
    repo_root: Path, issue_number: str, *, original_body: str, candidate_body: str
) -> str:
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(candidate_body, encoding="utf-8")
    repair_action = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": "sha256:" + _sha256(original_body),
        "repaired_body_sha256": "sha256:" + _sha256(candidate_body),
        "diagnostics_artifact": None,
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": ["test_fixture_repair"],
        "reason_codes": ["test_fixture"],
        "source_lane": "unanchored",
        "preflight_run_identity": "sha256:testrun",
        "original_updated_at": "2024-01-01T00:00:00Z",
        "source_refs_digest": None,
    }
    preflight_result = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "needs_fix",
        "issue_number": int(issue_number),
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
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result), encoding="utf-8")
    return str(result_path.relative_to(repo_root))


def _run_executor(
    repo: Path,
    *,
    command_id: str,
    issue_number: str,
    preflight_result_path: str,
    extra_env: "dict | None" = None,
) -> subprocess.CompletedProcess:
    apply_flag = (
        "--apply-repair-action" if command_id == "repair_action.apply" else "--apply-structural-repair-action"
    )
    argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id",
        command_id,
        "--issue-number",
        issue_number,
        "--repo",
        "squne121/loop-protocol",
        apply_flag,
        preflight_result_path,
    ]
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo),
        "LOOP_ISSUE_NUMBER": issue_number,
        # `SKILL_RUNTIME_TEST_*` is the ONE env-key-prefix `_sanitize_env()`
        # forwards verbatim through the outer dispatch's own PATH/env
        # sanitization -- this is how the fake `gh` (invoked several process
        # layers below) learns where to persist its fake remote-Issue state.
        "SKILL_RUNTIME_TEST_FAKE_GH_STATE_DIR": str(repo.parent / "gh-state"),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, env=env, check=False)


# A production-shaped `go`-status Implementation Issue body. Verified (outside
# this test, during authoring) to pass BOTH `contract_readiness_check.py
# --mode static` AND `guard-issue-body.py`'s stricter pre-mutation guard
# (template/outcome-quality/body-validation/AC-VC-alignment/VC-shell checks)
# -- the two independent gates `edit_issue_txn.py`'s real transaction core
# runs before ever attempting a PATCH.
_CLEAN_CANDIDATE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: "v1"
issue_kind: implementation
parent_issue: none
goal_ref: "テスト用 valid implementation issue fixture"
change_kind: chore
```

## Parent Issue

なし

## Parent Goal Ref

- Goal: テスト用 valid fixture の目的（バリデーター検証）
- Desired Destination: N/A

## Current Validated Scope

- `example_script.py` の実装

## Remaining Parent Gaps

なし

## Outcome

`example_script.py` が `--dry-run` フラグを受け付け、副作用なしで実行結果を出力する。

## In Scope

- `example_script.py` の実装

## Out of Scope

- テストの追加

## Required Design References

- `docs/dev/agent-skill-boundaries.md`

## Acceptance Criteria

- [ ] AC1: `example_script.py` が存在し、`--dry-run` フラグを受け付けること
- [ ] AC2: `--dry-run` 実行時に exit 0 を返すこと

## Runtime Verification Applicability

decision: not_applicable
reason: "テスト用 fixture であり実行環境を持たないため"

## Verification Commands

```bash
# AC1
# baseline-expect: fail
$ test -f example_script.py

# AC2
# baseline-expect: pass
$ uv run python3 example_script.py --dry-run
```

## Allowed Paths

- `example_script.py`

## Stop Conditions

実装中にこれらの状況が発生したら直ちに作業を停止し、Issue comment に状況を記録して人間の判断を待つ。

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Required Skills

なし
"""

# AC6 fixture: identical shape to `_CLEAN_CANDIDATE_BODY` but the VC block
# deliberately omits the `# baseline-expect:` annotations `repair_issue_
# contract.py`'s deterministic checker flags as an `auto_apply_safe`-class
# actionable repair. A mutation using THIS body as its candidate therefore
# still succeeds (the PATCH itself has no such requirement), but AC9 fresh
# validation (which reruns that SAME checker against the post-mutation live
# body) finds an actionable repair still remaining -> `status: failed`.
_ACTIONABLE_DEFECT_CANDIDATE_BODY = _CLEAN_CANDIDATE_BODY.replace(
    "```bash\n# AC1\n# baseline-expect: fail\n$ test -f example_script.py\n\n"
    "# AC2\n# baseline-expect: pass\n$ uv run python3 example_script.py --dry-run\n```",
    "```bash\n# AC1\n$ test -f example_script.py\n\n# AC2\n$ uv run python3 example_script.py --dry-run\n```",
)
assert _ACTIONABLE_DEFECT_CANDIDATE_BODY != _CLEAN_CANDIDATE_BODY


def test_actionable_defect_fixture_body_genuinely_differs_only_in_baseline_expect_annotations() -> None:
    # Fixture self-check: the two candidate bodies used by the E2E tests
    # below differ ONLY in the baseline-expect annotations, never accidental
    # drift elsewhere in the fixture body.
    clean_lines = _CLEAN_CANDIDATE_BODY.splitlines()
    dirty_lines = _ACTIONABLE_DEFECT_CANDIDATE_BODY.splitlines()
    assert len(clean_lines) - len(dirty_lines) == 2  # the 2 removed annotation lines


_ORIGINAL_BODY = "original stale body\n"


def test_repair_action_apply_cold_tmp_production_shaped_mutation_success_exit_0(tmp_path: Path) -> None:
    """AC4/AC10(b)(c): a genuinely COLD dedicated dispatch (no pre-existing
    `tmp/` directory at repo root) reaches a full production-shaped
    `edit_issue_txn.py` mutation success (real readiness check -> real guard
    -> real PATCH via `controlled_skill_mutation_exec.py` -> real fresh
    validation) with exit 0 / phase complete / failure_code null /
    mutation_outcome applied, and never reports `unauthorized_write_path`."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _install_fixture(repo, trusted_gh_bin)
    assert not (repo / "tmp").exists()

    issue_number = "900001"
    _write_remote_state(tmp_path / "gh-state", issue_number, _ORIGINAL_BODY)
    preflight_result_path = _write_repair_action_preflight_result(
        repo, issue_number, original_body=_ORIGINAL_BODY, candidate_body=_CLEAN_CANDIDATE_BODY
    )

    result = _run_executor(
        repo, command_id="repair_action.apply", issue_number=issue_number, preflight_result_path=preflight_result_path
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKILL_RUNTIME_FAIL:" not in result.stderr
    assert "unauthorized_write_path" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["phase"] == "complete"
    assert payload["failure_code"] is None
    assert payload["mutation_outcome"] == "applied"
    # AC10(c): the transaction's own `tmp/` scratch workspace was created and
    # used, and only the (now-empty) directory node survives.
    assert (repo / "tmp").is_dir()
    assert list((repo / "tmp").iterdir()) == []


def test_repair_action_apply_warm_tmp_production_shaped_mutation_success_exit_0(tmp_path: Path) -> None:
    """AC10(a): a WARM dispatch (`tmp/` already exists, with a pre-existing
    unrelated file inside it, before the dispatch even starts) reaches the
    SAME mutation success -- the pre-existing `tmp/` content (present on BOTH
    sides of the before/after diff, untouched by the transaction) is never
    misreported as an unauthorized write, and the housekeeping exemption is
    not merely a cold-start special case."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _install_fixture(repo, trusted_gh_bin)
    (repo / "tmp").mkdir(parents=True, exist_ok=True)
    (repo / "tmp" / "pre_existing_unrelated_file.txt").write_text("pre-existing\n", encoding="utf-8")

    issue_number = "900002"
    _write_remote_state(tmp_path / "gh-state", issue_number, _ORIGINAL_BODY)
    preflight_result_path = _write_repair_action_preflight_result(
        repo, issue_number, original_body=_ORIGINAL_BODY, candidate_body=_CLEAN_CANDIDATE_BODY
    )

    result = _run_executor(
        repo, command_id="repair_action.apply", issue_number=issue_number, preflight_result_path=preflight_result_path
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["phase"] == "complete"
    assert payload["failure_code"] is None
    assert payload["mutation_outcome"] == "applied"
    # The pre-existing file is untouched -- still there, byte-identical.
    assert (repo / "tmp" / "pre_existing_unrelated_file.txt").read_text(encoding="utf-8") == "pre-existing\n"


def test_repair_action_apply_residual_tmp_file_still_rejected(tmp_path: Path) -> None:
    """AC10(d): the `tmp/` housekeeping exemption covers ONLY the directory
    NODE's own identity, never blanket `tmp/**`. A file that newly appears
    inside `tmp/` DURING the dispatch (injected here immediately after a
    genuinely successful PATCH, via the SAME fake-`gh` mechanism AC5 uses)
    is still reported as `unauthorized_write_path`, exactly as it was before
    this Issue for `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS`."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _install_fixture(repo, trusted_gh_bin)

    issue_number = "900003"
    _write_remote_state(tmp_path / "gh-state", issue_number, _ORIGINAL_BODY)
    preflight_result_path = _write_repair_action_preflight_result(
        repo, issue_number, original_body=_ORIGINAL_BODY, candidate_body=_CLEAN_CANDIDATE_BODY
    )

    result = _run_executor(
        repo,
        command_id="repair_action.apply",
        issue_number=issue_number,
        preflight_result_path=preflight_result_path,
        extra_env={"SKILL_RUNTIME_TEST_INJECT_STRAY_WRITE_AFTER_PATCH": "tmp/unexpected-file"},
    )

    assert result.returncode != 0
    assert "SKILL_RUNTIME_FAIL:" in result.stderr, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "tmp/unexpected-file" in result.stderr


def test_repair_action_apply_stray_write_to_unrelated_root_rejected(tmp_path: Path) -> None:
    """AC5: a write outside every allowed root (injected here immediately
    after a genuinely successful PATCH) is still `unauthorized_write_path`,
    even though the write-root widening in this Issue now legitimizes
    `artifacts/{issue}/issue-metadata/` for this command_id."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _install_fixture(repo, trusted_gh_bin)

    issue_number = "900004"
    _write_remote_state(tmp_path / "gh-state", issue_number, _ORIGINAL_BODY)
    preflight_result_path = _write_repair_action_preflight_result(
        repo, issue_number, original_body=_ORIGINAL_BODY, candidate_body=_CLEAN_CANDIDATE_BODY
    )

    result = _run_executor(
        repo,
        command_id="repair_action.apply",
        issue_number=issue_number,
        preflight_result_path=preflight_result_path,
        extra_env={"SKILL_RUNTIME_TEST_INJECT_STRAY_WRITE_AFTER_PATCH": "unexpected_stray_file.txt"},
    )

    assert result.returncode != 0
    assert "SKILL_RUNTIME_FAIL:" in result.stderr, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unexpected_stray_file.txt" in result.stderr


def test_repair_action_apply_stray_write_to_other_issue_number_rejected(tmp_path: Path) -> None:
    """AC5/AC6 (In Scope item 6): a write into a DIFFERENT Issue number's own
    `issue-metadata/` directory (injected here immediately after a genuinely
    successful PATCH for THIS Issue) is still `unauthorized_write_path` --
    the per-Issue scoping in `_allowed_artifact_roots()` is never widened to
    "any issue-metadata/ directory"."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _install_fixture(repo, trusted_gh_bin)

    issue_number = "900005"
    other_issue_number = "9999"
    _write_remote_state(tmp_path / "gh-state", issue_number, _ORIGINAL_BODY)
    preflight_result_path = _write_repair_action_preflight_result(
        repo, issue_number, original_body=_ORIGINAL_BODY, candidate_body=_CLEAN_CANDIDATE_BODY
    )

    result = _run_executor(
        repo,
        command_id="repair_action.apply",
        issue_number=issue_number,
        preflight_result_path=preflight_result_path,
        extra_env={
            "SKILL_RUNTIME_TEST_INJECT_STRAY_WRITE_AFTER_PATCH": (
                f"artifacts/{other_issue_number}/issue-metadata/rogue.txt"
            )
        },
    )

    assert result.returncode != 0
    assert "SKILL_RUNTIME_FAIL:" in result.stderr, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert f"artifacts/{other_issue_number}/issue-metadata/rogue.txt" in result.stderr


def test_repair_action_apply_semantic_failure_never_promoted_to_exit_0(tmp_path: Path) -> None:
    """AC6: a legal write-root (this Issue's own `issue-metadata/`) never
    causes the outer executor to promote a child SEMANTIC failure to exit 0.
    The candidate body's PATCH genuinely succeeds (`mutation_outcome:
    applied`), but AC9 fresh validation (rerunning the SAME deterministic
    repair checker against the post-mutation live body) still finds an
    actionable repair remaining -- `phase: fresh_validation`, a non-null
    `failure_code`, and a non-zero process exit, exactly as it must whether
    or not the write-root happens to be legal."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _install_fixture(repo, trusted_gh_bin)

    issue_number = "900006"
    _write_remote_state(tmp_path / "gh-state", issue_number, _ORIGINAL_BODY)
    preflight_result_path = _write_repair_action_preflight_result(
        repo, issue_number, original_body=_ORIGINAL_BODY, candidate_body=_ACTIONABLE_DEFECT_CANDIDATE_BODY
    )

    result = _run_executor(
        repo, command_id="repair_action.apply", issue_number=issue_number, preflight_result_path=preflight_result_path
    )

    assert result.returncode != 0
    assert "SKILL_RUNTIME_FAIL:" not in result.stderr, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["mutation_outcome"] == "applied"
    assert payload["phase"] == "fresh_validation"
    assert payload["failure_code"] is not None
    assert payload["fresh_validation"]["status"] == "failed"
    assert payload["fresh_validation"]["actionable_repair_remaining"] is True


# ---------------------------------------------------------------------------
# AC4 (structural lane): `structural_repair_action.apply` shares the SAME
# `_dispatch_candidate_body_via_edit_txn()` transaction core `repair_action.
# apply` uses (never a second, independent GitHub-mutation implementation).
# ---------------------------------------------------------------------------

_STRUCTURAL_STOP_CONDITIONS_VALUE = """\
実装中にこれらの状況が発生したら直ちに作業を停止し、Issue comment に状況を記録して人間の判断を待つ。

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合
"""

# The real Issue #995 producer (`build_structural_repair_bundle()`) fills
# every field this template declares that the ORIGINAL body below omits
# (Verification Commands / Runtime Verification Applicability / Stop
# Conditions / Required Skills), synthesizing a single go-status whole body
# `structural_repair_action.apply` then dispatches through the SAME
# transaction core `repair_action.apply` uses.
_STRUCTURAL_TEMPLATE_TEXT = (
    """\
name: "Implementation Issue"
description: "test double"
body:
  - type: textarea
    id: machine-readable-contract
    attributes:
      label: "Machine-Readable Contract"
      value: |
        ```yaml
        contract_schema_version: v1
        issue_kind: implementation
        ```
    validations:
      required: true
  - type: textarea
    id: verification-commands
    attributes:
      label: "Verification Commands"
      value: |
        ```bash
        # AC1
        # baseline-expect: fail
        $ test -f example_script.py

        # AC2
        # baseline-expect: pass
        $ uv run python3 example_script.py --dry-run
        ```
    validations:
      required: true
  - type: textarea
    id: runtime-verification-applicability
    attributes:
      label: "Runtime Verification Applicability"
      value: |
        decision: not_applicable
        reason: "テスト用 fixture であり実行環境を持たないため"
    validations:
      required: true
  - type: textarea
    id: stop-conditions
    attributes:
      label: "Stop Conditions"
      value: |
"""
    + "\n".join(f"        {line}" if line else "" for line in _STRUCTURAL_STOP_CONDITIONS_VALUE.splitlines())
    + """
    validations:
      required: true
  - type: textarea
    id: required-skills
    attributes:
      label: "Required Skills"
      value: |
        なし
    validations:
      required: true
"""
)

_STRUCTURAL_ORIGINAL_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: "v1"
issue_kind: implementation
parent_issue: none
goal_ref: "テスト用 valid implementation issue fixture"
change_kind: chore
```

## Parent Issue

なし

## Parent Goal Ref

- Goal: テスト用 valid fixture の目的（バリデーター検証）
- Desired Destination: N/A

## Current Validated Scope

- `example_script.py` の実装

## Remaining Parent Gaps

なし

## Outcome

`example_script.py` が `--dry-run` フラグを受け付け、副作用なしで実行結果を出力する。

## In Scope

- `example_script.py` の実装

## Out of Scope

- テストの追加

## Required Design References

- `docs/dev/agent-skill-boundaries.md`

## Acceptance Criteria

- [ ] AC1: `example_script.py` が存在し、`--dry-run` フラグを受け付けること
- [ ] AC2: `--dry-run` 実行時に exit 0 を返すこと

## Allowed Paths

- `example_script.py`
"""


def _write_structural_preflight_result(repo_root: Path, issue_number: str, repo_slug: str) -> str:
    """Builds the `structural_repair_action/v1` bundle via the REAL Issue
    #995 producer (`repair_issue_contract.build_structural_repair_bundle()`)
    -- never a hand-typed digest dict -- so every digest `run_structural_
    repair_action_apply()` re-verifies is genuine."""
    scripts_dir = repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from repair_issue_contract import build_structural_repair_bundle

    bundle = build_structural_repair_bundle(
        _STRUCTURAL_ORIGINAL_BODY,
        issue_kind="implementation",
        template_text=_STRUCTURAL_TEMPLATE_TEXT,
        template_path=".github/ISSUE_TEMPLATE/implementation.yml",
        repo=repo_slug,
        issue_number=int(issue_number),
        original_updated_at="2024-01-01T00:00:00Z",
    )
    assert bundle["disposition_summary"] == "auto_apply_safe", bundle
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifact_dir / "structural_preflight_result.json"
    result_path.write_text(
        json.dumps({"schema": "issue_refinement_preflight_result/v1", "structural_repair_action": bundle}),
        encoding="utf-8",
    )
    return str(result_path.relative_to(repo_root))


def test_structural_repair_action_apply_production_shaped_mutation_success_exit_0(tmp_path: Path) -> None:
    """AC4 (structural lane): the real Issue #995 producer's `auto_apply_safe`
    bundle reaches a full production-shaped mutation success -- real
    per-item digest re-verification against the live body, real whole-body
    synthesis, real digest-bound readiness check, real `edit_issue_txn.py`
    dispatch through the SAME shared transaction core -- with exit 0 / phase
    complete / failure_code null / mutation_outcome applied, and never
    reports `unauthorized_write_path`."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _install_fixture(repo, trusted_gh_bin)

    issue_number = "900007"
    _write_remote_state(tmp_path / "gh-state", issue_number, _STRUCTURAL_ORIGINAL_BODY)
    preflight_result_path = _write_structural_preflight_result(repo, issue_number, "squne121/loop-protocol")

    result = _run_executor(
        repo,
        command_id="structural_repair_action.apply",
        issue_number=issue_number,
        preflight_result_path=preflight_result_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKILL_RUNTIME_FAIL:" not in result.stderr
    assert "unauthorized_write_path" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["phase"] == "complete"
    assert payload["failure_code"] is None
    assert payload["mutation_outcome"] == "applied"
    assert payload["items_applied"] > 0
