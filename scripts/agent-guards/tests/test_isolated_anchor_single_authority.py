"""Issue #2257 review fix_delta (iteration 1): real-subprocess coverage for
the production anchor-comment resolution path.

Issue #2872 update (read first): since the #2299 launcher contract shares
GitHub auth with the isolated child natively, production preflight reads use
native `gh` for every profile (an isolated HOME no longer selects the
anonymous credentialless transport). The pre-#2299 "empty GH_CONFIG_DIR +
credentialless is the only path" fixtures below therefore exercise the
credentialless GET-only transport through an EXPLICIT `transport=` argument
only (opt-in `github_live`); the isolated-session read-path contract itself
is pinned by `test_run_refinement_preflight_credentialless_transport.py`
(selector / call-path tests) and the live AC9 runtime test. The former live
`skill_runtime_exec.py` command-graph test asserted "zero `gh` invocations
with an empty GH_CONFIG_DIR", which encodes the superseded premise and had
also drifted from `_install_real_contract_update_fixture()`'s signature. It is
NOT deleted: its regression coverage (the REAL `skill_runtime_exec.py ->
command_registry.py -> run_refinement_preflight.py` command graph resolves the
anchor comment through ONE read authority) is preserved by the repaired,
hermetic `test_isolated_with_human_context_command_graph_resolves_anchor_via_
single_native_gh_authority` below (fake `gh` at the GitHub boundary, no
network), which complements (does not duplicate) the live AC9 runtime test.

`test_run_refinement_preflight_credentialless_transport.py::
test_ac5_exact_incident_replay_anchor_comment_5315264311_resolves_live`
already exercises `run_refinement_preflight._fetch_single_comment()`
against the real, unauthenticated GitHub REST API -- but it does so
**in-process** (the production module is `importlib`-loaded into the
pytest worker itself). A pr-review-judge REQUEST_CHANGES blocker on this
PR noted that an in-process call cannot fully stand in for the production
launch shape: `scripts/claude-gpt/launch.sh` starts a **fresh Python
interpreter subprocess** (via `skill_runtime_exec.py` -> the
`command_registry.py` "preflight.run.with_anchor" entry -> a rendered
`uv run python3 run_refinement_preflight.py ...` child process), not an
in-process function call inside an already-running host process.

This module adds that missing subprocess boundary. It intentionally does
NOT attempt to also stand up a full disposable git worktree that satisfies
`skill_runtime_exec.py`'s `required_cwd: canonical_main_root` /
`required_branch: default_branch` gates while making a genuinely live,
un-mocked GitHub REST call: that combination would require a real `uv
sync --locked` bootstrap of a throwaway repository on every test run,
which is slow and non-deterministic-network-flaky for what is, in
substance, the same anchor-comment-resolution assertion as AC5's existing
in-process test plus a subprocess boundary. Instead, two focused,
never-fabricated-logic checks are used together:

1. `test_isolated_anchor_comment_resolves_via_single_credentialless_authority`
   launches the REAL `run_refinement_preflight.py` production script file
   (never a copy, never a stub) as a genuine `uv run python3` child
   process, with a fresh isolated `HOME`, an empty `GH_CONFIG_DIR`,
   `GH_TOKEN`/`GITHUB_TOKEN` unset, and a fail-on-use `gh` marker
   executable at the front of `PATH` -- then has that subprocess resolve
   Issue #2197's real anchor comment 5315264311 via a real, unauthenticated
   GitHub REST call and report back whether the `gh` marker was ever
   invoked. This proves the credentialless single-authority transport
   holds across an actual process boundary, not just within the pytest
   worker's own interpreter.
2. `test_production_command_registry_wires_anchor_command_to_run_refinement_preflight_script`
   asserts, against the REAL (never reimplemented) `command_registry.py`
   and `skill_runtime_command_policy.py` production modules, that the
   `preflight.run.with_anchor` command class actually renders an argv that
   invokes this exact `run_refinement_preflight.py` script with
   `--anchor-comment-url`, and that the self-referential command string
   `skill_runtime_exec.py` builds for its own gate check
   (`--command-id preflight.run.with_anchor --issue-number ... --repo ...
   --anchor-comment-url ...`) is recognized by
   `is_exact_skill_runtime_anchor_executor_command`'s parser. Together with
   (1), this closes the `skill_runtime_exec.py` -> command registry ->
   `run_refinement_preflight.py` subprocess graph without re-deriving its
   safety-gate logic in test code.

PR #2260 review fix_delta (iteration 2) adds a THIRD test that closes the
remaining gap the above two (deliberately) leave open: neither (1) nor (2)
ever actually launches `skill_runtime_exec.py --command-id
preflight.run.with_human_context` itself as a subprocess. (1) loads
`run_refinement_preflight.py`'s `_fetch_single_comment()` directly via a
bespoke driver script -- the real production entrypoint (`skill_runtime_exec.py`)
never runs. (2) only inspects `render_command()`'s and the policy parser's
*static* output -- no process is ever spawned. `test_skill_runtime_exec_anchor.py`
already established (Issue #1498) the disposable-repo fixture that lets a
*genuine* `uv run python3 scripts/agent-guards/skill_runtime_exec.py
--command-id ...` subprocess reach the REAL `command_registry.py` ->
REAL `run_refinement_preflight.py` chain, backed by a real `uv sync --locked`
bootstrap and a `sitecustomize.py` process-boundary instrumentation hook
(`_install_real_contract_update_fixture()`). `test_ac5_ac6_...` below reuses
that exact helper (imported from the sibling module, never re-derived) instead
of hand-rolling a second disposable-repo bootstrap, then layers the isolated
Claude-GPT profile (fresh `HOME`, empty `GH_CONFIG_DIR`, `GH_TOKEN`/
`GITHUB_TOKEN` unset) on top of it and asserts, from OUTSIDE the child
process (so it cannot be spoofed by anything the child prints), that the `gh`
CLI was invoked zero times end-to-end while the real anchor comment resolves.

Because `skill_runtime_exec.py`'s own `_sanitize_env()` unconditionally
rebuilds `PATH` from a fixed, hardcoded allowlist of real system directories
before spawning its child (Issue #2241 hardening), a `PATH`-prepended marker
executable -- the technique test (1) above uses -- cannot survive into the
grandchild `run_refinement_preflight.py` process launched through
`skill_runtime_exec.py`: there is no way to write to `/usr/bin` or the other
allowlisted system directories from a test, nor would doing so be safe. This
is exactly why `_install_real_contract_update_fixture()`'s `sitecustomize.py`
approach is reused instead of the marker-executable approach: it intercepts
`subprocess.run`/`subprocess.Popen` at the Python call site inside the child
interpreter itself (via `PYTHONPATH`-triggered `sitecustomize` auto-import,
which CPython performs regardless of `PATH`), so it still catches a `gh`
invocation attempt even though `PATH` no longer contains an interceptable
marker.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_PREFLIGHT_SCRIPT = (
    REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py"
)
_COMMAND_REGISTRY_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
_AGENT_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"

assert _PREFLIGHT_SCRIPT.is_file(), f"production script missing: {_PREFLIGHT_SCRIPT}"

REPO = "squne121/loop-protocol"
ISSUE_NUMBER = 2197
ANCHOR_COMMENT_ID = 5315264311

# ---------------------------------------------------------------------------
# Issue #2317: structured `REASON_CODE:` compact-stdout predicate/parser,
# replacing the prior broad substring OR-chain (which matched the
# `GH_API_FAILURE` blocker text on both stdout AND stderr and therefore
# false-greened genuine failures). Only an exact-match, stdout-only,
# closed-set `REASON_CODE:` value is ever treated as transient.
# ---------------------------------------------------------------------------

_TRANSIENT_ENVIRONMENT_FAILURE_REASON_CODES = frozenset(
    {"rate_limited", "upstream_environment_failure", "transport_connectivity_failure"}
)


def _extract_reason_code_line_value(stdout: str) -> "str | None":
    """Extract the value of the sole `REASON_CODE:` line from compact
    stdout produced by the real production `_build_compact_stdout()`.

    Returns `None` (never transient) unless exactly one `REASON_CODE:`
    line is present -- absence or duplication both fail closed to "not
    transient" so genuine failures are never silently skipped."""
    matches = [
        line[len("REASON_CODE:") :].strip() for line in stdout.splitlines() if line.startswith("REASON_CODE:")
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _is_transient_environment_failure_reason_code(reason_code: "str | None") -> bool:
    """Closed-set exact-match predicate (Issue #2317 AC3/AC5/AC7): only the
    reason codes production has already classified as transient/
    environmental (`rate_limited` / `upstream_environment_failure` /
    `transport_connectivity_failure`) are treated as skip-eligible. Any
    unknown, future, malformed, or empty reason code is NOT transient
    (fail-closed) so genuine failures never become false-green skips."""
    if not reason_code:
        return False
    return reason_code in _TRANSIENT_ENVIRONMENT_FAILURE_REASON_CODES


def _extract_status_line_value(stdout: str) -> "str | None":
    """Extract the value of the sole `STATUS:` line from compact stdout.

    Returns `None` (never transient) unless exactly one `STATUS:` line is
    present -- absence or duplication both fail closed to "not transient"
    (PR #2319 review fix_delta iteration 1 P1: STATUS is held to the same
    exactly-once strictness as REASON_CODE; SOURCE/OPERATION are not, per
    OWNER decision)."""
    matches = [line[len("STATUS:") :].strip() for line in stdout.splitlines() if line.startswith("STATUS:")]
    if len(matches) != 1:
        return None
    return matches[0]


def _stdout_indicates_transient_environment_failure(stdout: str) -> bool:
    """Combine the extraction helpers and the closed-set predicate. `stderr`
    is never consulted (Issue #2317: stdout is the sole judgment source).

    PR #2319 review fix_delta iteration 1 P1: hardened to also require
    `STATUS: environment_failure` to appear exactly once (in addition to
    the pre-existing REASON_CODE exactly-once check) before ever
    considering the REASON_CODE value. A missing, duplicated, or
    non-`environment_failure` STATUS line fails closed to "not transient",
    matching Issue #2317's original pre-review request. SOURCE and
    OPERATION are not held to this same exactly-once strictness (OWNER
    explicitly scoped the hardening to STATUS and REASON_CODE only)."""
    if _extract_status_line_value(stdout) != "environment_failure":
        return False
    return _is_transient_environment_failure_reason_code(_extract_reason_code_line_value(stdout))


_GH_MARKER_SCRIPT = """#!/bin/sh
# Test-only marker executable: if this is ever invoked it proves the
# isolated-profile fetch path fell back to the `gh` CLI, which must never
# happen (Issue #2257 / Issue #2241 AC8).
echo -n "gh_marker_invoked" >> "$GH_MARKER_INVOCATION_FILE"
exit 7
"""

# The driver script never reimplements the credentialless transport or the
# isolated-profile detection: it only imports the REAL production
# `run_refinement_preflight.py` (via `importlib`, same unique-module-name
# convention as `test_run_refinement_preflight_credentialless_transport.py`
# so this never collides with another test file's own load of the same
# source file in the same pytest session) and calls its production
# `_fetch_single_comment()` function.
_DRIVER_SCRIPT = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location(
    "run_refinement_preflight_2257_isolated_anchor_single_authority_subprocess",
    r\"\"\"{preflight_script}\"\"\",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

# Issue #2872: the credentialless GET-only transport is no longer selected by
# any profile; it is exercised here through an explicit `transport=` argument.
transport = module._CredentiallessPreflightTransport()
data, err = module._fetch_single_comment("{repo}", {comment_id}, transport=transport)
print(json.dumps({{"data": data, "err": err}}))
"""


def _install_gh_marker_executable(tmp_path: Path, env: dict[str, str]) -> Path:
    marker_bin_dir = tmp_path / "marker-bin"
    marker_bin_dir.mkdir()
    gh_marker_path = marker_bin_dir / "gh"
    gh_marker_path.write_text(_GH_MARKER_SCRIPT, encoding="utf-8")
    gh_marker_path.chmod(gh_marker_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    invocation_marker_file = tmp_path / "gh_marker_invoked.marker"
    env["GH_MARKER_INVOCATION_FILE"] = str(invocation_marker_file)
    env["PATH"] = f"{marker_bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return invocation_marker_file


@pytest.mark.github_live
def test_explicit_credentialless_transport_resolves_anchor_across_subprocess_boundary(tmp_path: Path) -> None:
    """Pre-#2299 contract replay (Issue #2872: explicit credentialless
    transport, `github_live` opt-in, never a default/required gate).

    GIVEN a genuine `uv run python3` child subprocess of the REAL
    `run_refinement_preflight.py` production script (never a copy, never a
    stub), launched with a fresh isolated `HOME`, an empty `GH_CONFIG_DIR`,
    `GH_TOKEN`/`GITHUB_TOKEN` unset, and a fail-on-use `gh` marker
    executable at the front of `PATH`
    WHEN that subprocess resolves Issue #2197's real anchor comment
    5315264311 through an explicitly passed credentialless transport with NO
    transport mocking (a real, unauthenticated GitHub REST call made from an
    actual child process, not an in-process call)
    THEN the comment resolves successfully via the credentialless
    transport and the `gh` marker executable is never invoked -- proving
    the GET-only transport contract holds across a genuine process boundary
    (Issue #2257 AC5/AC6, PR review fix_delta iteration 1)."""
    driver_path = tmp_path / "driver.py"
    driver_path.write_text(
        _DRIVER_SCRIPT.format(preflight_script=str(_PREFLIGHT_SCRIPT), repo=REPO, comment_id=ANCHOR_COMMENT_ID),
        encoding="utf-8",
    )

    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    empty_gh_config_dir = tmp_path / "empty-gh-config-dir"
    empty_gh_config_dir.mkdir()

    env = dict(os.environ)
    env["HOME"] = str(isolated_home)
    env["GH_CONFIG_DIR"] = str(empty_gh_config_dir)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    invocation_marker_file = _install_gh_marker_executable(tmp_path, env)

    try:
        result = subprocess.run(
            ["uv", "run", "python3", str(driver_path)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        assert not invocation_marker_file.exists(), (
            "gh marker executable was invoked by the real subprocess -- the "
            "isolated profile fell back to the gh CLI, reproducing the "
            "Issue #2197 split-brain transport regression across a process "
            "boundary"
        )

    assert result.returncode == 0, f"driver subprocess failed: {result.stderr}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    data, err = payload["data"], payload["err"]

    if err and err.startswith("transport_failure:") and (
        "rate_limited" in err or "upstream_environment_failure" in err or "transport_connectivity_failure" in err
    ):
        pytest.skip(f"network/rate-limit/upstream unavailable in this environment: {err}")
    if err and err.startswith("transport_failure:") and "authentication" in err:
        pytest.fail(
            f"AC5/AC6 unmet: exact incident replay reproduced the Issue #2197 "
            f"auth-dependency misclassification across a real subprocess boundary: {err}"
        )

    assert err == "", f"AC5/AC6 unmet: anchor comment {ANCHOR_COMMENT_ID} did not resolve: {err}"
    assert data is not None
    assert str(data.get("id")) == str(ANCHOR_COMMENT_ID)
    assert data.get("issue_url") == f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"


def test_production_command_registry_wires_anchor_command_to_run_refinement_preflight_script() -> None:
    """GIVEN the REAL (never reimplemented) `command_registry.py` and
    `skill_runtime_command_policy.py` production modules
    WHEN the `preflight.run.with_anchor` command class is rendered and the
    self-referential command string `skill_runtime_exec.py` builds for its
    own exact-command gate check is parsed
    THEN the rendered argv invokes this exact `run_refinement_preflight.py`
    script with `--anchor-comment-url`, and the gate parser recognizes the
    command class -- confirming the `skill_runtime_exec.py` -> command
    registry -> `run_refinement_preflight.py` subprocess graph is actually
    wired together in the shipped production code (Issue #2257 AC5/AC6
    review blocker: the graph, not just the leaf transport function, must
    be covered)."""
    sys.path.insert(0, str(_COMMAND_REGISTRY_SCRIPTS_DIR))
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))
    try:
        import command_registry as reg
        import skill_runtime_command_policy as policy

        argv = reg.render_command(
            "preflight.run.with_anchor",
            {
                "issue_number": ISSUE_NUMBER,
                "repo": REPO,
                "anchor_comment_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{ANCHOR_COMMENT_ID}",
            },
        )
        assert argv == [
            "uv",
            "run",
            "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number",
            str(ISSUE_NUMBER),
            "--repo",
            REPO,
            "--anchor-comment-url",
            f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{ANCHOR_COMMENT_ID}",
        ]

        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                "scripts/agent-guards/skill_runtime_exec.py",
                "--command-id",
                "preflight.run.with_anchor",
                "--issue-number",
                str(ISSUE_NUMBER),
                "--repo",
                REPO,
                "--anchor-comment-url",
                f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{ANCHOR_COMMENT_ID}",
            ]
        )
        parsed = policy.parse_exact_skill_runtime_anchor_command(command_text, str(REPO_ROOT))
        assert parsed is not None, "skill_runtime_exec.py's own anchor command text was rejected by the gate parser"
        assert parsed.command_id == "preflight.run.with_anchor"
        assert parsed.issue_number == str(ISSUE_NUMBER)
        assert parsed.repo == REPO
    finally:
        sys.path.remove(str(_COMMAND_REGISTRY_SCRIPTS_DIR))
        sys.path.remove(str(_AGENT_GUARDS_DIR))
        sys.modules.pop("command_registry", None)
        sys.modules.pop("skill_runtime_command_policy", None)


# ---------------------------------------------------------------------------
# PR #2260 review fix_delta (iteration 2) coverage, repaired for Issue #2872:
# real `skill_runtime_exec.py --command-id preflight.run.with_human_context`
# subprocess command graph. The pre-#2299 live variant (empty GH_CONFIG_DIR,
# "gh invoked zero times", real anonymous REST) is superseded by the #2299
# contract; this hermetic variant keeps the same failure-class coverage with
# an independent (outside-the-child) invocation record at the fake-`gh`
# boundary and no network.
# ---------------------------------------------------------------------------

_SKILL_RUNTIME_EXEC_ANCHOR_FIXTURES_MODULE = (
    REPO_ROOT / "scripts" / "agent-guards" / "tests" / "test_skill_runtime_exec_anchor.py"
)


def _load_skill_runtime_exec_anchor_fixtures():
    """Import `test_skill_runtime_exec_anchor.py`'s disposable-repo fixture
    helpers under a private module name (never re-deriving the disposable git
    repo / real `uv sync --locked` bootstrap / `sitecustomize.py` fake-`gh`
    boundary that module already establishes for the real
    `skill_runtime_exec.py` subprocess chain)."""
    spec = importlib.util.spec_from_file_location(
        "test_skill_runtime_exec_anchor_fixtures_for_2257_command_graph",
        str(_SKILL_RUNTIME_EXEC_ANCHOR_FIXTURES_MODULE),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_isolated_with_human_context_command_graph_resolves_anchor_via_single_native_gh_authority(
    tmp_path: Path, monkeypatch
) -> None:
    """GIVEN the REAL `skill_runtime_exec.py --command-id
    preflight.run.with_human_context` production command graph (privileged
    executor -> `command_registry.py` -> `run_refinement_preflight.py`),
    launched as a genuine `uv run python3` subprocess against a disposable
    fixture repository, with a fresh isolated `HOME` and a test-owned
    config-only `GH_CONFIG_DIR` (token env unset -- the stored `gh auth
    login` shape the #2299 launcher shares natively)
    WHEN that graph resolves the anchor comment
    THEN (Issue #2872 AC1/AC2/AC3) the Issue, comments and anchor comment are
    ALL read through the native `gh` boundary with the carried
    `GH_CONFIG_DIR` (verified independently, outside the child, from the fake
    `gh` boundary's own invocation record), the anchor resolves, and the
    credentialless transport is never the read authority (no
    `credentialless_transport` SOURCE / `rate_limited` environment failure)."""
    fixtures = _load_skill_runtime_exec_anchor_fixtures()
    repo = fixtures._make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = fixtures._install_real_contract_update_fixture(repo, trusted_gh_bin)
    execution_root = fixtures._materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
    artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    isolated_home = tmp_path / "isolated-home-command-graph"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "stored-gh-config-dir-command-graph"
    gh_config_dir.mkdir()
    env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": str(gh_config_dir),
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
    }

    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    result = fixtures._run_executor(
        repo,
        command_id="preflight.run.with_human_context",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=env,
    )

    # Independent, outside-the-child invocation record: the fixture's trusted
    # `gh` executable exits non-zero (66) unless the carried GH_CONFIG_DIR
    # matches, and appends one operation label + config-path identity per
    # successful call to these files.
    operations_file = artifact_dir / "fake_gh_operations.jsonl"
    config_states_file = artifact_dir / "fake_gh_config_states.jsonl"
    assert operations_file.is_file(), (
        "native gh was never invoked by the production command graph: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    operations = [json.loads(line) for line in operations_file.read_text(encoding="utf-8").splitlines()]
    config_states = [json.loads(line) for line in config_states_file.read_text(encoding="utf-8").splitlines()]
    assert config_states == ["expected_path"] * len(operations), (config_states, operations)
    # Issue + comments (the anchor resolves out of the same comments read) all
    # went through the one native authority; the read-only lane never mutates.
    assert "issue_view" in operations and "issue_comments_read" in operations, operations
    assert set(operations) <= {"issue_view", "issue_comments_read", "issue_comment_read"}, operations

    # The anchor resolved through that single native authority.
    raw_snapshot = json.loads((artifact_dir / "raw_issue_snapshot.json").read_text(encoding="utf-8"))
    anchor_state = raw_snapshot.get("anchor_comment")
    assert anchor_state is not None, raw_snapshot
    assert "issuecomment-1" in str(anchor_state.get("url", "")), anchor_state
    combined = f"{result.stdout}\n{result.stderr}"
    assert "credentialless_transport" not in combined, combined
    assert "BLOCKER_ANCHOR_COMMENT_NOT_FOUND" not in combined, combined
    assert "ANCHOR_NOT_IN_ISSUE" not in combined, combined


# ---------------------------------------------------------------------------
# Issue #2317: deterministic unit tests for the transient-environment-failure
# predicate/parser (AC3/AC4/AC5/AC7) defined above. The formatter-contract
# tests for the REASON_CODE/SOURCE/OPERATION stdout projection (AC1/AC2,
# against the REAL `_build_compact_stdout()`) live in
# `test_refinement_preflight.py`, which already owns formatter contract
# testing (PR #2319 review fix_delta iteration 1 P2: removed the
# near-duplicate copies and the `_load_run_refinement_preflight_module()`
# helper that were newly added to this file).
# ---------------------------------------------------------------------------


def test_transient_environment_failure_predicate_true_for_transient_reason_codes() -> None:
    """AC3: the predicate returns True for each closed-set transient
    reason code."""
    for code in ("rate_limited", "upstream_environment_failure", "transport_connectivity_failure"):
        stdout = f"STATUS: environment_failure\nREASON_CODE: {code}\nSOURCE: github_api\nOPERATION: fetch_issue"
        assert _is_transient_environment_failure_reason_code(_extract_reason_code_line_value(stdout)) is True


def test_transient_environment_failure_predicate_false_without_reason_code_line() -> None:
    """AC4: with no `REASON_CODE:` line present and only a
    `BLOCKERS: GH_API_FAILURE` entry, the predicate returns False (hard
    failure is preserved -- this is the exact regression this Issue
    fixes)."""
    stdout = "STATUS: blocked\nBLOCKERS:\n  - GH_API_FAILURE"
    assert _is_transient_environment_failure_reason_code(_extract_reason_code_line_value(stdout)) is False


def test_transient_environment_failure_predicate_false_for_genuine_reason_codes() -> None:
    """AC5: genuine reason codes outside the transient closed set return
    False (never silently skipped as transient)."""
    genuine_reason_codes = (
        "unexpected_authentication_dependency",
        "http_403_forbidden",
        "canonical_resource_missing",
        "gh_auth_required",
        "malformed_response_body",
        "transport_internal_error",
    )
    for code in genuine_reason_codes:
        stdout = f"STATUS: environment_failure\nREASON_CODE: {code}\nSOURCE: github_api\nOPERATION: fetch_issue"
        assert _is_transient_environment_failure_reason_code(_extract_reason_code_line_value(stdout)) is False


def test_transient_environment_failure_predicate_false_for_duplicate_or_empty_reason_code() -> None:
    """AC7: duplicate `REASON_CODE:` lines, or a `REASON_CODE:` line with
    an empty value, both cause the predicate to return False."""
    duplicate_stdout = (
        "STATUS: environment_failure\n"
        "REASON_CODE: rate_limited\n"
        "REASON_CODE: rate_limited\n"
        "SOURCE: github_api\n"
        "OPERATION: fetch_issue"
    )
    assert _is_transient_environment_failure_reason_code(_extract_reason_code_line_value(duplicate_stdout)) is False

    empty_value_stdout = "STATUS: environment_failure\nREASON_CODE: \nSOURCE: github_api\nOPERATION: fetch_issue"
    assert _is_transient_environment_failure_reason_code(_extract_reason_code_line_value(empty_value_stdout)) is False


def test_stdout_indicates_transient_environment_failure_requires_status_exactly_once() -> None:
    """PR #2319 review fix_delta iteration 1 P1: `_stdout_indicates_transient_
    environment_failure()` must also require `STATUS: environment_failure`
    to appear exactly once, in addition to the pre-existing REASON_CODE
    exactly-once check. Missing, duplicated, or non-`environment_failure`
    STATUS lines all fail closed to "not transient", even when a
    transient-eligible REASON_CODE line is present."""
    missing_status = "REASON_CODE: rate_limited\nSOURCE: credentialless_transport\nOPERATION: read_issue"
    assert _stdout_indicates_transient_environment_failure(missing_status) is False

    duplicate_status = (
        "STATUS: environment_failure\n"
        "STATUS: environment_failure\n"
        "REASON_CODE: rate_limited\n"
        "SOURCE: credentialless_transport\n"
        "OPERATION: read_issue"
    )
    assert _stdout_indicates_transient_environment_failure(duplicate_status) is False

    wrong_status = "STATUS: blocked\nREASON_CODE: rate_limited\nSOURCE: credentialless_transport\nOPERATION: read_issue"
    assert _stdout_indicates_transient_environment_failure(wrong_status) is False

    well_formed = (
        "STATUS: environment_failure\n"
        "REASON_CODE: rate_limited\n"
        "SOURCE: credentialless_transport\n"
        "OPERATION: read_issue"
    )
    assert _stdout_indicates_transient_environment_failure(well_formed) is True
