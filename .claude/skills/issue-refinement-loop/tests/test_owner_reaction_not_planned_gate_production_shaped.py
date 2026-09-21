"""
test_owner_reaction_not_planned_gate_production_shaped.py

Issue #2689 AC6 (runtime-verification) + PR #2697 OWNER review comment
#5755475318 P0-2 fix_delta: production-shaped test proving a single
producer -> consumer path WITHOUT the test code manually bridging the two
halves.

PR #2697 review history: the original version of this module dispatched a
REAL `owner_reaction.decide.fixture` subprocess (the producer) but then fed
its output dict DIRECTLY into `preflight._is_approved_owner_reaction_not_
planned_decision(...)` / `preflight._classify_heavy_mutation_gate(...)`
called from test code -- it never exercised this PR's own new production
wrapper (`_classify_heavy_mutation_gate_with_fresh_owner_reaction()`) or
`run_refinement_preflight.py`'s CLI `main()` / `cli_known_context`
materialization at all. The OWNER's review flagged this as a false-green:
the "consumer" half was never reached by real code.

This rewrite launches the REAL, canonical entrypoint --
`run_refinement_preflight.py` itself, as a genuine subprocess (never a
test-code function call) -- with the `--mutation-category` /
`--owner-user-id` / `--preview-binding-file` CLI flags this PR's P0-1
fix_delta added to `main()`. That single subprocess internally:

  1. materializes `known_context["mutation_category"]` /
     `known_context["owner_reaction_context"]` from those exact CLI flags
     (`main()`'s own new logic, #2689 P0-1);
  2. reaches `run_preflight()` -> `_classify_heavy_mutation_gate_with_
     fresh_owner_reaction()` (#2689 AC4/P0-1 wiring);
  3. which itself launches a SECOND real, nested subprocess to the REAL
     `owner_reaction_decision.py` CLI (`_run_owner_reaction_decision_
     fresh()`), reading the REAL `--preview-binding-file` artifact and
     resolving the REAL (fake-`gh`-backed) reaction state;
  4. and feeds that fresh result into `_classify_heavy_mutation_gate()`.

No test code ever calls `_is_approved_owner_reaction_not_planned_decision`,
`_classify_heavy_mutation_gate`, or `_classify_heavy_mutation_gate_with_
fresh_owner_reaction` directly -- the ONLY way this module observes the
gate's decision is by reading the `known_context["heavy_mutation_gate"]`
key `run_refinement_preflight.py` itself persists into the REAL
`planner_input.json` artifact (Issue #2689's own `_build_planner_input()`
threading of `known_context`), a genuine production artifact this same
real subprocess wrote to disk.

Per the fix_delta: fake is limited to the GitHub network boundary (the
`gh` CLI the nested `owner_reaction_decision.py` subprocess calls,
faked via `owner_reaction_dispatch_fixture.py`'s existing fake-`gh`
script/`SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE` convention -- the
SAME fixture asset #2694's own test suite already established). The OUTER
`run_refinement_preflight.py` Issue/comments read uses its own pre-existing
`--fixture` bypass (a local JSON snapshot, never a live `gh` call) -- this
is the SAME kind of GitHub-network-boundary-only fake, just for a
DIFFERENT (outer, non-owner-reaction) read this Issue's Allowed Paths does
not touch.

`owner_reaction_dispatch_fixture.py`'s `make_repo()` / `install_fixture()`
(extended by this same PR to additionally copy `run_refinement_preflight.py`
itself, plus its own `plan_refinement_loop.py` / `repair_issue_contract.py`
/ `schemas/` dependencies) / `write_preview_binding()` / `write_gh_state()`
are reused verbatim -- no new large-scale harness.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))

from owner_reaction_dispatch_fixture import (  # noqa: E402
    TRUSTED_REPO_SLUG,
    install_fixture,
    make_repo,
    write_gh_state,
    write_preview_binding,
)

_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import command_registry  # noqa: E402

_REPO_ROOT = _TESTS_DIR.parents[3]
_AGENT_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
sys.path.insert(0, str(_AGENT_GUARDS_DIR))
import skill_runtime_command_policy as policy  # noqa: E402

ISSUE_NUMBER = 2689
OWNER_USER_ID = 5150
COMMENT_ID = 9001
ANCHOR_COMMENT_ID = 9002
COMMENT_BODY = "owner reacts to the not_planned option here\n"
ANCHOR_BODY = "not_planned option anchor body\n"
ISSUE_BODY = "issue body snapshot for #2689\n"
REACTION_OPTION_MAP = {"+1": "close_not_planned_option"}
OPTIONS = {
    "close_not_planned_option": {
        "operation": "close_not_planned",
        "target": f"#{ISSUE_NUMBER}",
    }
}
REACTIONS = [{"id": 1, "content": "+1", "user": {"id": OWNER_USER_ID, "login": "owner"}}]


def _artifact_dir(repo: Path) -> Path:
    return repo / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)


def _install_scenario(repo: Path, trusted_gh_bin: Path) -> tuple[str, Path]:
    """Installs the real dispatch fixture plus this scenario's owner-reaction
    state, returning (preview_binding_relative_path, gh_state_path)."""
    install_fixture(repo, trusted_gh_bin)

    artifact_dir = _artifact_dir(repo)
    binding_path = artifact_dir / "preview_binding.json"
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
    preview_binding_rel = str(binding_path.relative_to(repo))

    gh_state_path = artifact_dir / "gh_state.json"
    write_gh_state(
        gh_state_path,
        repo=TRUSTED_REPO_SLUG,
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    return preview_binding_rel, gh_state_path


def _write_preflight_input_fixture(repo: Path) -> Path:
    """A minimal `refinement_preflight_input/v1` fixture (Issue #2689's OWN
    outer, non-owner-reaction GitHub-network-boundary fake) -- bypasses only
    the OUTER `gh issue view`/`gh api .../comments` reads
    `run_refinement_preflight.py` would otherwise perform; the NESTED
    `owner_reaction_decision.py` subprocess this Issue wires in is entirely
    unaffected by this flag and still performs its own (fake-`gh`-backed)
    reads."""
    fixture_path = repo / "preflight_input_fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "schema_version": "refinement_preflight_input/v1",
                "issue_number": ISSUE_NUMBER,
                "repo": TRUSTED_REPO_SLUG,
                "issue": {
                    "number": ISSUE_NUMBER,
                    "title": "test",
                    "body": ISSUE_BODY,
                    "labels": [],
                },
                "comments": [],
            }
        ),
        encoding="utf-8",
    )
    return fixture_path


def _run_preflight_real_subprocess(
    repo: Path,
    *,
    trusted_gh_bin: Path,
    gh_state_path: Path,
    fixture_path: Path,
    preview_binding_rel: str,
    owner_user_id: int,
    issue_number: int = ISSUE_NUMBER,
) -> subprocess.CompletedProcess[str]:
    """Launches the REAL, canonical `run_refinement_preflight.py` entrypoint
    (copied verbatim by `install_fixture()`) as a genuine subprocess -- this
    is the "real canonical entrypoint" invocation the fix_delta requires,
    never a Python-level function call into this module's own process."""
    script = repo / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py"
    env = dict(os.environ)
    env["PATH"] = str(trusted_gh_bin) + os.pathsep + env.get("PATH", "")
    # PR #2694's established fake-`gh` convention (owner_reaction_dispatch_
    # fixture.py's own `_FAKE_GH_SOURCE`): the ONLY GitHub-network-boundary
    # fake this test relies on for the NESTED owner_reaction_decision.py
    # subprocess `_run_owner_reaction_decision_fresh()` launches.
    env["SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE"] = str(gh_state_path)
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--issue-number", str(issue_number),
            "--repo", TRUSTED_REPO_SLUG,
            "--fixture", str(fixture_path),
            "--mutation-category", "not_planned",
            "--owner-user-id", str(owner_user_id),
            "--preview-binding-file", preview_binding_rel,
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _read_heavy_mutation_gate(repo: Path) -> dict:
    """Reads the REAL `known_context["heavy_mutation_gate"]` this same real
    subprocess persisted into the REAL `planner_input.json` artifact
    (`_build_planner_input()`'s own `known_context` threading) -- the ONLY
    way this test observes the gate's decision (never a direct call into
    `_classify_heavy_mutation_gate()` / `_classify_heavy_mutation_gate_
    with_fresh_owner_reaction()`)."""
    planner_input_path = _artifact_dir(repo) / "planner_input.json"
    assert planner_input_path.exists(), "planner_input.json artifact was not written by the real subprocess"
    data = json.loads(planner_input_path.read_text(encoding="utf-8"))
    known_context = data.get("known_context")
    assert isinstance(known_context, dict), data
    heavy_mutation_gate = known_context.get("heavy_mutation_gate")
    assert isinstance(heavy_mutation_gate, dict), known_context
    return heavy_mutation_gate


def test_selected_not_planned_reaches_gate_via_real_subprocess(tmp_path):
    """#2689 AC6 / PR #2697 P0-2 fix_delta: the REAL production chain --
    `run_refinement_preflight.py` CLI `main()` (with the P0-1 `--mutation-
    category`/`--owner-user-id`/`--preview-binding-file` flags) ->
    `cli_known_context` -> `run_preflight()` ->
    `_classify_heavy_mutation_gate_with_fresh_owner_reaction()` -> a REAL
    NESTED `owner_reaction_decision.py` subprocess -> `_classify_heavy_
    mutation_gate()` -- reaches `status: allowed` from a SELECTED
    `close_not_planned` owner reaction, all inside ONE real top-level
    subprocess launch."""
    repo = make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    preview_binding_rel, gh_state_path = _install_scenario(repo, trusted_gh_bin)
    fixture_path = _write_preflight_input_fixture(repo)

    result = _run_preflight_real_subprocess(
        repo,
        trusted_gh_bin=trusted_gh_bin,
        gh_state_path=gh_state_path,
        fixture_path=fixture_path,
        preview_binding_rel=preview_binding_rel,
        owner_user_id=OWNER_USER_ID,
    )
    assert "Traceback" not in result.stderr or "SyntaxWarning" in result.stderr, result.stderr

    heavy_mutation_gate = _read_heavy_mutation_gate(repo)
    assert heavy_mutation_gate == {
        "mutation_category": "not_planned",
        "is_heavy_mutation": True,
        "status": "allowed",
        "fail_closed": False,
        "reason": "owner_reaction_not_planned_decision_present",
    }, heavy_mutation_gate
    assert "HEAVY_MUTATION_FAIL_CLOSED" not in result.stdout


def test_wrong_owner_user_id_fails_closed_via_real_subprocess(tmp_path):
    """The SAME real chain, but with an `--owner-user-id` that does not
    match the reacting user in the (fake-`gh`-backed) real reaction state --
    the REAL nested `owner_reaction_decision.py` subprocess resolves
    `unresolved`, and the REAL gate fails closed (never converts a
    non-matching principal into an implicit approval)."""
    repo = make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    preview_binding_rel, gh_state_path = _install_scenario(repo, trusted_gh_bin)
    fixture_path = _write_preflight_input_fixture(repo)

    result = _run_preflight_real_subprocess(
        repo,
        trusted_gh_bin=trusted_gh_bin,
        gh_state_path=gh_state_path,
        fixture_path=fixture_path,
        preview_binding_rel=preview_binding_rel,
        owner_user_id=OWNER_USER_ID + 1,
    )
    assert "Traceback" not in result.stderr or "SyntaxWarning" in result.stderr, result.stderr

    heavy_mutation_gate = _read_heavy_mutation_gate(repo)
    assert heavy_mutation_gate == {
        "mutation_category": "not_planned",
        "is_heavy_mutation": True,
        "status": "blocked",
        "fail_closed": True,
        "reason": "heavy_mutation_requires_owner_explicit_decision",
    }, heavy_mutation_gate
    assert "HEAVY_MUTATION_FAIL_CLOSED" in result.stdout


# ---------------------------------------------------------------------------
# Issue #2689 P0-1 fix_delta: the OUTER production transport wiring --
# `command_registry.render_command()` (real function) rendering the new
# mutation-gate flags, and `skill_runtime_command_policy`'s real exact-match
# parsers accepting the resulting argv -- for BOTH `preflight.run.with_
# human_context` and `contract_update.run.with_human_context`. This is a
# pure-function-level (no subprocess) check: the dedicated-worktree
# machinery those two command_ids require in real dispatch is out of this
# fix_delta's scope (`update_branch`/heavy-worktree fixtures are not part of
# this Issue's Allowed Paths), but the registry/policy pairing itself is
# real, unmodified production code exercised directly.
# ---------------------------------------------------------------------------


def _anchor_url(issue_number: int) -> str:
    return f"https://github.com/{TRUSTED_REPO_SLUG}/issues/{issue_number}#issuecomment-1"


def test_preflight_with_human_context_registry_argv_accepted_by_real_policy_parser():
    preview_binding_file = f".claude/artifacts/issue-refinement-loop/{ISSUE_NUMBER}/preview_binding.json"
    argv = command_registry.render_command(
        "preflight.run.with_human_context",
        {
            "issue_number": ISSUE_NUMBER,
            "repo": TRUSTED_REPO_SLUG,
            "anchor_comment_url": _anchor_url(ISSUE_NUMBER),
            "mutation_category": "not_planned",
            "owner_user_id": OWNER_USER_ID,
            "preview_binding_file": preview_binding_file,
        },
    )
    # `render_command()` renders the SKILL script's own argv, not the
    # executor's outer `--command-id ...` wrapper -- reconstruct the exact
    # outer invocation shape `skill_runtime_exec.py`'s own command_text
    # construction produces (mirrors that construction verbatim; see
    # `skill_runtime_exec.py`'s `elif is_anchor_command or is_contract_
    # update_command:` branch).
    command_text = " ".join(
        [
            "uv", "run", "python3", policy.SKILL_RUNTIME_EXEC_REL,
            "--command-id", "preflight.run.with_human_context",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", TRUSTED_REPO_SLUG,
            "--anchor-comment-url", _anchor_url(ISSUE_NUMBER),
            "--human-context-comment-url", _anchor_url(ISSUE_NUMBER),
            "--mutation-category", "not_planned",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_file,
        ]
    )
    parsed = policy.parse_exact_skill_runtime_anchor_command(command_text)
    assert parsed is not None
    assert parsed.mutation_category == "not_planned"
    assert parsed.owner_user_id == str(OWNER_USER_ID)
    assert parsed.preview_binding_file == preview_binding_file
    assert argv[-6:] == [
        "--mutation-category", "not_planned",
        "--owner-user-id", str(OWNER_USER_ID),
        "--preview-binding-file", preview_binding_file,
    ]


def test_contract_update_with_human_context_registry_argv_accepted_by_real_policy_parser():
    preview_binding_file = f".claude/artifacts/issue-refinement-loop/{ISSUE_NUMBER}/preview_binding.json"
    command_text = " ".join(
        [
            "uv", "run", "python3", policy.SKILL_RUNTIME_EXEC_REL,
            "--command-id", "contract_update.run.with_human_context",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", TRUSTED_REPO_SLUG,
            "--anchor-comment-url", _anchor_url(ISSUE_NUMBER),
            "--human-context-comment-url", _anchor_url(ISSUE_NUMBER),
            "--mutation-category", "not_planned",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_file,
        ]
    )
    parsed = policy.parse_exact_skill_runtime_contract_update_anchor_command(command_text)
    assert parsed is not None
    assert parsed.mutation_category == "not_planned"


def test_registry_transport_rejects_partial_group_at_policy_layer():
    """The policy parser rejects a partial mutation-gate group (only
    `--mutation-category`, no `--owner-user-id`/`--preview-binding-file`) --
    the SAME all-or-none contract `run_refinement_preflight.py`'s own
    `main()` and `skill_runtime_exec.py`'s own guard independently enforce."""
    command_text = " ".join(
        [
            "uv", "run", "python3", policy.SKILL_RUNTIME_EXEC_REL,
            "--command-id", "preflight.run.with_human_context",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", TRUSTED_REPO_SLUG,
            "--anchor-comment-url", _anchor_url(ISSUE_NUMBER),
            "--human-context-comment-url", _anchor_url(ISSUE_NUMBER),
            "--mutation-category", "not_planned",
        ]
    )
    assert policy.parse_exact_skill_runtime_anchor_command(command_text) is None


def test_registry_transport_rejects_disallowed_command_id():
    """`preflight.run.with_anchor` (no human-context lane) must never accept
    the mutation-gate transport triple."""
    command_text = " ".join(
        [
            "uv", "run", "python3", policy.SKILL_RUNTIME_EXEC_REL,
            "--command-id", "preflight.run.with_anchor",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", TRUSTED_REPO_SLUG,
            "--anchor-comment-url", _anchor_url(ISSUE_NUMBER),
            "--mutation-category", "not_planned",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", f".claude/artifacts/issue-refinement-loop/{ISSUE_NUMBER}/preview_binding.json",
        ]
    )
    assert policy.parse_exact_skill_runtime_anchor_command(command_text) is None
