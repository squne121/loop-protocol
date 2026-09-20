"""
test_owner_reaction_decide_failure_boundary.py

Issue #2688 AC9: at least one of {child non-zero exit, malformed structured
result} is exercised from the real outer `skill_runtime_exec.py` executor
boundary, and the outer executor must NOT convert it into false success.

`owner_reaction_decision.py`'s own `main()` returns exit code 1 whenever
`decide()` resolves to `status: environment_error` (still printing a
well-formed JSON document -- see its module docstring: "A nonzero subprocess
exit is fail-closed REGARDLESS of whether stdout happens to contain
parseable partial JSON"). `skill_runtime_exec.py`'s
`_dispatch_child_and_check_postconditions()` propagates the child's own
`returncode` verbatim (never remapped to 0) -- these tests pin that
propagation for the owner_reaction.decide.fixture real dispatch chain from
the OUTER subprocess boundary, using two independent failure shapes:

  1. child non-zero exit with a well-formed but `environment_error`-status
     JSON body (malformed reaction record -- Issue #1975 AC3(d))
  2. child non-zero exit with a stdout body that is not even valid JSON
     (a `--gh-fixture-file` that does not itself parse, so
     `owner_reaction_decision.py`'s OWN `argparse`/file-read layer fails
     before `decide()` ever runs)

Both scenarios assert (a) the outer executor's own exit code is non-zero,
and (b) the outer executor never fabricates a top-level
`"status": "selected"` (or any other false-success marker) around the
child's real failure.
"""

from __future__ import annotations

import json

from owner_reaction_dispatch_fixture import (
    install_fixture,
    make_repo,
    run_executor,
    write_gh_state,
    write_preview_binding,
)

ISSUE_NUMBER = 2690
OWNER_USER_ID = 4444
COMMENT_ID = 7001
ANCHOR_COMMENT_ID = 7002
COMMENT_BODY = "owner reacts here (failure boundary)\n"
ANCHOR_BODY = "anchor options here (failure boundary)\n"
ISSUE_BODY = "issue body snapshot (failure boundary)\n"
REACTION_OPTION_MAP = {"+1": "option_a"}
OPTIONS = {"option_a": {"operation": "close", "target": "issue"}}


def _artifact_dir(repo):
    return repo / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)


def _seed_preview_binding(repo):
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


def _env():
    return {"LOOP_ISSUE_NUMBER": str(ISSUE_NUMBER)}


def test_owner_reaction_decide_fixture_malformed_reaction_record_is_nonzero_exit_not_false_success(tmp_path):
    """Failure shape 1: a well-formed JSON document with a structurally
    invalid reaction record (`content` missing) -- owner_reaction_decision.py's
    own decide() catches ReactionRecordIntegrityFailure and returns
    `status: environment_error`, and its main() maps that to exit 1. The
    outer skill_runtime_exec.py subprocess boundary must reproduce that
    exact non-zero exit, and must never report `status: selected` (or any
    other false-success marker) despite receiving VALID JSON on stdout."""
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
        # `content` is missing entirely -- ReactionRecordIntegrityFailure.
        reactions=[{"id": 1, "user": {"id": OWNER_USER_ID}}],
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
        extra_env=_env(),
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    # The outer executor must reproduce the child's own real non-zero exit
    # -- never silently remapped to 0.
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["schema"] == "OWNER_REACTION_DECISION_RESULT_V1", payload
    assert payload["status"] == "environment_error", payload
    assert payload["reason_code"] == "reaction_record_integrity_failure", payload
    assert payload["status"] != "selected"
    assert payload.get("selected_option_id") is None


def test_owner_reaction_decide_fixture_malformed_gh_fixture_json_is_nonzero_exit_not_false_success(tmp_path):
    """Failure shape 2: `--gh-fixture-file` itself is not valid JSON at
    all -- owner_reaction_decision.py's own file-read/json.loads() layer
    raises before decide() ever runs, so its main() never even reaches the
    point of printing a well-formed OWNER_REACTION_DECISION_RESULT_V1 body.
    The outer skill_runtime_exec.py subprocess boundary must still
    propagate a genuine non-zero exit -- never fabricate a false-success
    stdout body in its place."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel = _seed_preview_binding(repo)

    gh_fixture_path = _artifact_dir(repo) / "gh_fixture.json"
    gh_fixture_path.parent.mkdir(parents=True, exist_ok=True)
    gh_fixture_path.write_text("{ not valid json ]", encoding="utf-8")
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
        extra_env=_env(),
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode != 0, (result.returncode, result.stdout, result.stderr)
    assert '"status": "selected"' not in result.stdout, result.stdout
    assert '"status":"selected"' not in result.stdout.replace(" ", ""), result.stdout


def test_owner_reaction_decide_fixture_missing_preview_binding_file_is_nonzero_exit_not_false_success(tmp_path):
    """A THIRD independent failure shape (belt-and-suspenders beyond AC9's
    minimum of one): --preview-binding-file points at a file that does not
    exist on disk at all. owner_reaction_decision.py's own main() catches
    this (OSError) and returns a well-formed
    `environment_error`/`preview_binding_file_unreadable` result with exit
    1 -- the outer executor must still propagate that real non-zero exit."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    _artifact_dir(repo).mkdir(parents=True, exist_ok=True)

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
        reactions=[{"id": 1, "content": "+1", "user": {"id": OWNER_USER_ID}}],
    )
    gh_fixture_rel = str(gh_fixture_path.relative_to(repo))
    missing_binding_rel = str((_artifact_dir(repo) / "does_not_exist.json").relative_to(repo))

    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", missing_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env=_env(),
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "environment_error", payload
    assert payload["reason_code"] == "preview_binding_file_unreadable", payload
