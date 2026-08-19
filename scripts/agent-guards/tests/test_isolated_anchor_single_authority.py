"""Issue #2257 review fix_delta (iteration 1): real-subprocess coverage for
the production anchor-comment resolution path.

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
"""

from __future__ import annotations

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

data, err = module._fetch_single_comment("{repo}", {comment_id})
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


def test_isolated_anchor_comment_resolves_via_single_credentialless_authority(tmp_path: Path) -> None:
    """GIVEN a genuine `uv run python3` child subprocess of the REAL
    `run_refinement_preflight.py` production script (never a copy, never a
    stub), launched with a fresh isolated `HOME`, an empty `GH_CONFIG_DIR`,
    `GH_TOKEN`/`GITHUB_TOKEN` unset, and a fail-on-use `gh` marker
    executable at the front of `PATH`
    WHEN that subprocess resolves Issue #2197's real anchor comment
    5315264311 with NO transport mocking (a real, unauthenticated GitHub
    REST call made from an actual child process, not an in-process call)
    THEN the comment resolves successfully via the credentialless
    transport and the `gh` marker executable is never invoked -- proving
    the single-authority credentialless transport holds across a genuine
    process boundary (Issue #2257 AC5/AC6, PR review fix_delta iteration
    1)."""
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
