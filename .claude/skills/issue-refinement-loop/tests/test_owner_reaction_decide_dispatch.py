"""
test_owner_reaction_decide_dispatch.py

Issue #2688: `owner_reaction.decide` / `owner_reaction.decide.fixture`
generic dispatch wiring into `scripts/agent-guards/skill_runtime_exec.py`'s
exact dispatch (the #1975 Out of Scope item this Issue closes).

AC coverage:
  AC1: owner_reaction.decide / owner_reaction.decide.fixture are wired into
       skill_runtime_exec.py's exact dispatch (proven by a REAL subprocess
       reaching the real owner_reaction_decision.py, not a pre-dispatch
       rejection).
  AC2: production-shaped and fixture profiles share the SAME owner-reaction
       dispatcher implementation (both reach the same first-hop script, and
       both produce the SAME selection outcome from the SAME underlying
       GitHub data).
  AC3: the full outer-argv -> exact command policy/parser ->
       command_registry.render_command() -> owner_reaction_decision.py ->
       owner_reaction_decision_result/v1 chain, verified by a real
       `skill_runtime_exec.py` subprocess (never a direct function call).
  AC5: the executor's first-hop script integrity/readback target matches
       the actual first-hop script the registry renders
       (owner_reaction_decision.py, NOT run_refinement_preflight.py).
  AC7: owner_reaction.decide.fixture returns a structured result via a real
       skill_runtime_exec.py subprocess.
  AC8: both production-shaped and fixture dispatch happy paths are verified
       from a real executor entrypoint. No live GitHub API is used -- the
       production profile fakes only the `gh` binary itself.

Every positive-path scenario below invokes the REAL
`scripts/agent-guards/skill_runtime_exec.py` as a subprocess (never
`command_registry.render_command()` called directly, and never the
dispatcher function imported and called in-process) -- see
`owner_reaction_dispatch_fixture.run_executor`.
"""

from __future__ import annotations

import json
from pathlib import Path

from owner_reaction_dispatch_fixture import (
    REPO_ROOT,
    install_fixture,
    make_repo,
    run_executor,
    write_gh_state,
    write_preview_binding,
)

ISSUE_NUMBER = 2688
OWNER_USER_ID = 4242
COMMENT_ID = 5001
ANCHOR_COMMENT_ID = 5002
COMMENT_BODY = "owner reacts here\n"
ANCHOR_BODY = "anchor options here\n"
ISSUE_BODY = "issue body snapshot\n"
REACTION_OPTION_MAP = {"+1": "option_a"}
OPTIONS = {"option_a": {"operation": "close", "target": "issue"}}
REACTIONS = [{"id": 1, "content": "+1", "user": {"id": OWNER_USER_ID}}]


def _artifact_dir(repo: Path) -> Path:
    return repo / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)


def _seed_preview_binding(repo: Path) -> str:
    binding_path = _artifact_dir(repo) / "preview_binding.json"
    write_preview_binding(
        binding_path,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reaction_option_map=REACTION_OPTION_MAP,
        options=OPTIONS,
    )
    return str(binding_path.relative_to(repo))


def _base_env(issue_number: int = ISSUE_NUMBER) -> dict:
    return {"LOOP_ISSUE_NUMBER": str(issue_number)}


def test_owner_reaction_decide_fixture_real_subprocess_returns_structured_result(tmp_path):
    """AC1/AC3/AC7/AC8 (fixture profile): a real skill_runtime_exec.py
    subprocess dispatches owner_reaction.decide.fixture through the real
    command_registry.py entry into the real owner_reaction_decision.py CLI,
    which answers via --gh-fixture-file (no real `gh` involved at all)."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel = _seed_preview_binding(repo)

    gh_fixture_path = _artifact_dir(repo) / "gh_fixture.json"
    write_gh_state(
        gh_fixture_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    gh_fixture_rel = str(gh_fixture_path.relative_to(repo))

    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env=_base_env(),
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["schema"] == "OWNER_REACTION_DECISION_RESULT_V1", payload
    assert payload["status"] == "selected", payload
    assert payload["selected_option_id"] == "option_a", payload
    assert payload["selected_option_metadata"] == OPTIONS["option_a"], payload
    assert payload["owner_reaction_contents"] == ["+1"], payload
    assert payload["fetched_reaction_count"] == 1, payload


def test_owner_reaction_decide_production_real_subprocess_returns_structured_result(tmp_path):
    """AC1/AC3/AC8 (production profile): a real skill_runtime_exec.py
    subprocess dispatches owner_reaction.decide through the real
    command_registry.py entry into the real owner_reaction_decision.py CLI,
    which reaches a real `gh` subprocess (faked only at the network
    boundary -- everything else, including this very subprocess launch, is
    real, per Issue #1975 AC7's precedent for the fixture sibling)."""
    repo = make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    install_fixture(repo, trusted_gh_bin)
    preview_binding_rel = _seed_preview_binding(repo)

    gh_state_path = tmp_path / "gh-state.json"
    write_gh_state(
        gh_state_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )

    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
        ],
        extra_env={
            **_base_env(),
            "SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE": str(gh_state_path),
        },
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["schema"] == "OWNER_REACTION_DECISION_RESULT_V1", payload
    assert payload["status"] == "selected", payload
    assert payload["selected_option_id"] == "option_a", payload


def test_production_and_fixture_share_same_dispatcher_and_selection(tmp_path):
    """AC2: production-shaped and fixture profiles share the SAME
    owner-reaction dispatcher implementation. Proven two ways:
      (a) functionally -- driving BOTH profiles from the IDENTICAL
          underlying GitHub data yields the IDENTICAL selection outcome;
      (b) structurally -- skill_runtime_exec.py's first-hop script_name
          selection resolves both command_ids through a SINGLE shared
          branch (not two independently-maintained branches that could
          silently diverge)."""
    repo = make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    install_fixture(repo, trusted_gh_bin)
    preview_binding_rel = _seed_preview_binding(repo)

    gh_fixture_path = _artifact_dir(repo) / "gh_fixture.json"
    write_gh_state(
        gh_fixture_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    gh_fixture_rel = str(gh_fixture_path.relative_to(repo))

    fixture_result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env=_base_env(),
    )
    production_result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
        ],
        extra_env={
            **_base_env(),
            "SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE": str(gh_fixture_path),
        },
    )
    assert fixture_result.returncode == 0, (fixture_result.returncode, fixture_result.stdout, fixture_result.stderr)
    assert production_result.returncode == 0, (
        production_result.returncode,
        production_result.stdout,
        production_result.stderr,
    )
    fixture_payload = json.loads(fixture_result.stdout)
    production_payload = json.loads(production_result.stdout)
    assert fixture_payload["status"] == production_payload["status"] == "selected"
    assert fixture_payload["selected_option_id"] == production_payload["selected_option_id"] == "option_a"

    # (b) structural: a single joint branch, not two independent ones.
    executor_source = (repo / "scripts" / "agent-guards" / "skill_runtime_exec.py").read_text(encoding="utf-8")
    assert (
        "elif is_owner_reaction_decide_command or is_owner_reaction_decide_fixture_command:" in executor_source
    ), executor_source
    assert executor_source.count('script_name = "owner_reaction_decision.py"') == 1, executor_source


def test_owner_reaction_decide_first_hop_targets_owner_reaction_decision_not_run_refinement_preflight(tmp_path):
    """AC5: the executor's first-hop script integrity/readback target is
    owner_reaction_decision.py, not run_refinement_preflight.py (the
    default target for every other command_id wired through
    skill_runtime_exec.py's main() dispatch). Proven by deliberately never
    installing run_refinement_preflight.py in this fixture repo at all --
    if the executor's script_name selection ever regressed to target that
    file for owner_reaction.decide, this dispatch would fail closed with
    `preflight_script_invalid` instead of succeeding."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    run_refinement_preflight_path = (
        repo / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py"
    )
    assert not run_refinement_preflight_path.exists()

    preview_binding_rel = _seed_preview_binding(repo)
    gh_fixture_path = _artifact_dir(repo) / "gh_fixture.json"
    write_gh_state(
        gh_fixture_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    gh_fixture_rel = str(gh_fixture_path.relative_to(repo))

    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env=_base_env(),
    )
    assert "preflight_script_invalid" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["schema"] == "OWNER_REACTION_DECISION_RESULT_V1", payload


def test_gh_config_dir_carrier_includes_production_but_not_fixture_owner_reaction(tmp_path):
    """PR #2694 review fix_delta (P0-1) structural regression: production
    `owner_reaction.decide` must be registered in `_sanitize_env()`'s
    `gh_config_dir_carrier_command_ids` allowlist (it is a real `gh api`
    consumer); the local-only `owner_reaction.decide.fixture` sibling (which
    never invokes `gh` at all) must NOT be added to the same allowlist."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    executor_source = (repo / "scripts" / "agent-guards" / "skill_runtime_exec.py").read_text(encoding="utf-8")
    start = executor_source.index("gh_config_dir_carrier_command_ids = frozenset(")
    end = executor_source.index("\n    )\n", start)
    block = executor_source[start:end]
    assert '"owner_reaction.decide"' in block, block
    assert '"owner_reaction.decide.fixture"' not in block, block


def test_owner_reaction_decide_production_forwards_gh_config_dir_to_real_gh_subprocess(tmp_path):
    """PR #2694 review fix_delta (P0-1) behavioral regression: a caller-
    supplied GH_CONFIG_DIR (the normal `gh auth login` stored-credential
    carrier, distinct from an explicit GH_TOKEN/GITHUB_TOKEN) must reach the
    real `gh` subprocess `owner_reaction_decision.py` shells out to for the
    production profile -- proven end-to-end through a real
    skill_runtime_exec.py subprocess, never a direct function call."""
    repo = make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    install_fixture(repo, trusted_gh_bin)
    preview_binding_rel = _seed_preview_binding(repo)

    gh_state_path = tmp_path / "gh-state.json"
    write_gh_state(
        gh_state_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    gh_config_dir = tmp_path / "gh-config-dir"
    gh_config_dir.mkdir()
    observed_path = tmp_path / "observed-gh-config-dir.txt"

    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
        ],
        extra_env={
            **_base_env(),
            "SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE": str(gh_state_path),
            "GH_CONFIG_DIR": str(gh_config_dir),
            "SKILL_RUNTIME_TEST_OBSERVED_GH_CONFIG_DIR_FILE": str(observed_path),
        },
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "selected", payload
    assert observed_path.is_file(), "fake gh never ran / never observed GH_CONFIG_DIR"
    assert observed_path.read_text(encoding="utf-8") == str(gh_config_dir)


def test_owner_reaction_decide_dispatch_succeeds_without_active_issue_worktree(tmp_path, monkeypatch):
    """PR #2694 review fix_delta (P1-2): `owner_reaction.decide` is
    read-only (`allowed_write_roots: []`) and must be root-no-worktree
    eligible -- it must dispatch successfully even when no active issue
    worktree resolves at all (LOOP_ISSUE_NUMBER unset), unlike a real
    mutation command class such as `repair_action.apply`."""
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
    repo = make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    install_fixture(repo, trusted_gh_bin)
    preview_binding_rel = _seed_preview_binding(repo)

    gh_state_path = tmp_path / "gh-state.json"
    write_gh_state(
        gh_state_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )

    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
        ],
        # Deliberately NO `LOOP_ISSUE_NUMBER` -- resolve_active_issue()
        # returns (None, None), so this can only succeed via the
        # root-no-worktree eligibility path, never the active-issue-worktree
        # fallback.
        extra_env={"SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE": str(gh_state_path)},
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert "active_issue_worktree_missing" not in result.stderr, result.stderr
    assert "active_issue_mismatch" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "selected", payload


def test_owner_reaction_decide_fixture_dispatch_succeeds_without_active_issue_worktree(tmp_path, monkeypatch):
    """PR #2694 review fix_delta (P1-2): the fixture sibling must be
    root-no-worktree eligible too."""
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel = _seed_preview_binding(repo)

    gh_fixture_path = _artifact_dir(repo) / "gh_fixture.json"
    write_gh_state(
        gh_fixture_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    gh_fixture_rel = str(gh_fixture_path.relative_to(repo))

    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env={},
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert "active_issue_worktree_missing" not in result.stderr, result.stderr
    assert "active_issue_mismatch" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "selected", payload


def test_owner_reaction_decision_py_argv_template_unchanged_by_this_issue():
    """AC6 (static companion, full runtime coverage lives in
    test_owner_reaction_decision.py per the live Issue's own Verification
    Commands): command_registry.py's owner_reaction.decide /
    owner_reaction.decide.fixture argv/placeholders/stdout_contract are
    read directly from the REAL, unmodified production file -- this Issue
    never edits command_registry.py (it is outside this Issue's Allowed
    Paths)."""
    import importlib.util

    registry_path = (
        REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py"
    )
    spec = importlib.util.spec_from_file_location("owner_reaction_dispatch_registry_check", registry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entry = module.REGISTRY["owner_reaction.decide"]
    assert entry["execution_class"] == "exact_owner_reaction_decide"
    assert entry["stdout_contract"] == "owner_reaction_decision_result/v1"
    fixture_entry = module.REGISTRY["owner_reaction.decide.fixture"]
    assert fixture_entry["execution_class"] == "exact_owner_reaction_decide_fixture"
    assert fixture_entry["stdout_contract"] == "owner_reaction_decision_result/v1"
