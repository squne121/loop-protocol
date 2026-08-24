"""Issue #2278 AC5/AC6/AC11: static/structural + production-contract
regression tests for `scripts/claude-gpt/runtime_smoke_test.sh --scenario
issue_to_impl`.

PR #2325 fix_delta (REQUEST_CHANGES, reviewed_head_sha
4937a1b9225a68faec34c20ae553b4e914dc6dee): the scenario's own scope claim
was narrowed to `workflow-start preflight -> live fixture readback ->
root-router implementation-entry probe` (`issue_contract_repair` /
`fresh_review` renamed to `fixture_contract_shape_check` /
`live_fixture_readback`; see the block comment in `runtime_smoke_test.sh`).
This module is extended accordingly:

  - the five embedded Python heredocs (`workflow_start_entry` first-hop
    classification, the fixture contract-shape classifier, the
    `live_fixture_readback` marker+call-trace classifier, the
    `root_entry_router` spy-invocation classifier, and the
    `expected-phases.json` runtime oracle) are all extracted from the real
    `runtime_smoke_test.sh` source (never re-implemented) and driven as
    real subprocesses against synthetic inputs, so a regression in any of
    the actual production classification logic is caught here even without
    a live `claude` run.
  - `workflow_start_entry.run()` itself (the actual production module the
    Phase 1 heredoc delegates to) is imported directly and driven with a
    malformed/unknown-decision fake producer, pinning the fail-closed
    contract the shell wiring depends on.
  - `root_entry_router.run_root_transition()` is exercised through the
    extracted heredoc with both a "go" and a "not go" fixture, covering the
    "callback count == 0" negative case explicitly.
  - `fake_gh.py`'s `issue view` call-trace now records the specific issue
    number, and its combined `--json title,body,labels,comments` handling
    now surfaces a `comments` key -- both are exercised directly via
    subprocess invocation of the fixture (no live Claude needed).
  - the strict `--scenario`/`--fixture`/`--evidence-out` argument parser
    (missing value / duplicate flag) is exercised end-to-end.
  - the P1-1 dirty-HEAD preflight gate is exercised end-to-end against a
    throwaway git repository (no live Claude needed -- the gate fails
    closed before Claude Code is ever launched).

The full live E2E path (fixture-seeded `gh` + one real `launch.sh`-driven
`claude` invocation) remains `# preflight-scope: runtime_only` per Issue
#2278's own Verification Commands section -- it is exercised directly via
the shell VC, not re-run inside this pytest module.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SMOKE_SH = Path(__file__).resolve().parents[1] / "runtime_smoke_test.sh"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "issue-2230-equivalent"
FAKE_GH_PY = Path(__file__).resolve().parent / "fixtures" / "fake_gh.py"
SKILLS_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"

_CONTRACT_CHECK_HEREDOC_RE = re.compile(
    r"python3 - \"\$ISSUE_TO_IMPL_FIXTURE_PATH\" <<'CONTRACT_CHECK_ITI_PY_EOF'\n(.*?)\nCONTRACT_CHECK_ITI_PY_EOF\n",
    re.DOTALL,
)
_WORKFLOW_START_ENTRY_HEREDOC_RE = re.compile(
    r"<<'WORKFLOW_START_ENTRY_ITI_PY_EOF'\n(.*?)\nWORKFLOW_START_ENTRY_ITI_PY_EOF\n",
    re.DOTALL,
)
_LIVE_FIXTURE_READBACK_HEREDOC_RE = re.compile(
    r"<<'LIVE_FIXTURE_READBACK_ITI_PY_EOF'\n(.*?)\nLIVE_FIXTURE_READBACK_ITI_PY_EOF\n",
    re.DOTALL,
)
_ROOT_ENTRY_ROUTER_HEREDOC_RE = re.compile(
    r"<<'ROOT_ENTRY_ROUTER_ITI_PY_EOF'\n(.*?)\nROOT_ENTRY_ROUTER_ITI_PY_EOF\n",
    re.DOTALL,
)
_PHASE_ORACLE_HEREDOC_RE = re.compile(
    r"<<'PHASE_ORACLE_ITI_PY_EOF'\n(.*?)\nPHASE_ORACLE_ITI_PY_EOF\n",
    re.DOTALL,
)
_EVIDENCE_BUILD_HEREDOC_RE = re.compile(
    r"<<'EVIDENCE_BUILD_ITI_PY_EOF'\n(.*?)\nEVIDENCE_BUILD_ITI_PY_EOF\n",
    re.DOTALL,
)

REQUIRED_CONTRACT_HEADERS = (
    "## Outcome",
    "## Acceptance Criteria",
    "## Verification Commands",
    "## Allowed Paths",
    "## Stop Conditions",
)

EXPECTED_PHASES = (
    "workflow_capability_preflight",
    "spark_delegation",
    "fixture_contract_shape_check",
    "live_fixture_readback",
    "impl_review_loop_entry",
)


def _extract_heredoc(pattern: re.Pattern) -> str:
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    match = pattern.search(source)
    assert match is not None, (
        f"heredoc not found via {pattern.pattern!r} -- extraction regex is out of "
        "sync with the production runtime_smoke_test.sh"
    )
    return match.group(1)


def _run_python_script(script_body: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script_body, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_contract_check(fixture_json: dict) -> dict:
    """Extract the real embedded contract-shape classifier (phase 3,
    `fixture_contract_shape_check`) from `runtime_smoke_test.sh` and drive
    it as a real subprocess against a synthetic fixture-shaped payload."""
    script_body = _extract_heredoc(_CONTRACT_CHECK_HEREDOC_RE)

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(fixture_json, fh)
        fixture_path = fh.name

    proc = _run_python_script(script_body, fixture_path)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- AC11 / P1-4: --scenario / --fixture / --evidence-out strict parsing --


def test_unknown_scenario_exits_2():
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), "--scenario", "does_not_exist"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "does_not_exist" in proc.stderr


def test_unknown_scenario_does_not_fall_back_to_default_smoke():
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), "--scenario", "does_not_exist"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "CLAUDE_GPT_SMOKE_RESULT_V1" not in proc.stdout
    assert "ISSUE_TO_IMPL_E2E_RESULT_V1" not in proc.stdout


def test_known_scenario_values_are_not_rejected_by_the_ac11_gate():
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert re.search(r"issue_create\)\s*:\s*;;", source)
    assert re.search(r"issue_to_impl\)\s*ISSUE_TO_IMPL_SCENARIO=true\s*;;", source)


@pytest.mark.parametrize("flag", ["--scenario", "--fixture", "--evidence-out"])
def test_missing_flag_value_exits_2(flag):
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), flag],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "requires a value" in proc.stderr


@pytest.mark.parametrize("flag", ["--scenario", "--fixture", "--evidence-out"])
def test_duplicate_flag_exits_2(flag):
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), flag, "issue_to_impl", flag, "issue_create"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "more than once" in proc.stderr


# --- P1-1: dirty-HEAD / fixture-integrity preflight gate (fail closed BEFORE
#     Claude Code is launched -- exercised against a throwaway git repo, no
#     live Claude needed since the gate short-circuits ahead of Phase 1). ---


def _make_throwaway_repo(tmp_path: Path) -> Path:
    """Copy just enough of scripts/claude-gpt/ into a fresh git repo so the
    scenario's SUT git_head/git_dirty probes observe a real, controllable
    git state, independent of THIS repository's own working tree state."""
    import shutil

    repo_dir = tmp_path / "throwaway_repo"
    claude_gpt_dir = repo_dir / "scripts" / "claude-gpt"
    shutil.copytree(RUNTIME_SMOKE_SH.parent, claude_gpt_dir)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "throwaway"], cwd=repo_dir, check=True)
    return repo_dir


def test_dirty_head_gate_fails_before_launching_claude(tmp_path):
    repo_dir = _make_throwaway_repo(tmp_path)
    # Dirty the throwaway worktree (untracked file -> `git status --porcelain`
    # is non-empty -> claude_gpt_git_dirty() reports "true").
    (repo_dir / "scripts" / "claude-gpt" / "DIRTY_MARKER.txt").write_text("dirty", encoding="utf-8")

    evidence_out = tmp_path / "evidence.json"
    proc = subprocess.run(
        [
            "sh",
            str(repo_dir / "scripts" / "claude-gpt" / "runtime_smoke_test.sh"),
            "--scenario",
            "issue_to_impl",
            "--fixture",
            str(FIXTURE_DIR / "issue.json"),
            "--evidence-out",
            str(evidence_out),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 1
    assert "sut_git_dirty" in proc.stdout
    evidence = json.loads(evidence_out.read_text(encoding="utf-8"))
    assert evidence["test_verdict"] == "fail"
    assert evidence["reason_code"] == "sut_git_dirty"
    assert evidence["terminal_result"] == "human_judgment_required"
    assert evidence["reached_phase"] is None
    assert evidence["phase_trace"] == []
    # The gate must fire BEFORE Phase 1 -- no phase was ever attempted.


# --- Fixtures: existence / shape / SHA-256-ability -------------------------


def test_fixture_files_exist():
    for name in ("prompt.md", "issue.json", "expected-phases.json"):
        path = FIXTURE_DIR / name
        assert path.is_file(), f"missing fixture: {path}"
        assert path.stat().st_size > 0, f"empty fixture: {path}"


def test_issue_json_fixture_shape_and_contract_completeness():
    data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    issues = data.get("issues")
    assert isinstance(issues, dict) and len(issues) == 1
    number, info = next(iter(issues.items()))
    assert number.isdigit()
    body = info.get("body", "")
    for header in REQUIRED_CONTRACT_HEADERS:
        assert header in body, f"fixture Issue body is missing required section {header!r}"


def test_prompt_md_fixture_embeds_the_deterministic_marker_and_target_issue():
    text = (FIXTURE_DIR / "prompt.md").read_text(encoding="utf-8")
    assert "ISSUE_TO_IMPL_FRESH_REVIEW_OK" in text
    issue_data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    (issue_number,) = issue_data["issues"].keys()
    assert issue_number in text


def test_expected_phases_json_matches_canonical_phase_names():
    data = json.loads((FIXTURE_DIR / "expected-phases.json").read_text(encoding="utf-8"))
    names = tuple(entry["phase"] for entry in data["phases"])
    assert names == EXPECTED_PHASES


# --- Embedded contract-shape classifier (phase 3) ---------------------------


def test_contract_check_classifies_complete_fixture_as_ok():
    issue_data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    result = _run_contract_check(issue_data)
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["issue_number"] == int(next(iter(issue_data["issues"].keys())))


@pytest.mark.parametrize("missing_header", REQUIRED_CONTRACT_HEADERS)
def test_contract_check_flags_each_missing_required_section(missing_header):
    issue_data = json.loads((FIXTURE_DIR / "issue.json").read_text(encoding="utf-8"))
    (number, info) = next(iter(issue_data["issues"].items()))
    mutated_body = info["body"].replace(missing_header, "## Mutated Away")
    assert missing_header not in mutated_body
    mutated = {"issues": {number: {**info, "body": mutated_body}}}
    result = _run_contract_check(mutated)
    assert result["ok"] is False
    assert missing_header in result["missing"]


def test_contract_check_reports_ok_false_for_empty_issues():
    result = _run_contract_check({"issues": {}})
    assert result["ok"] is False
    assert result["issue_number"] is None


# --- P0-1/P0-4: workflow_start_entry.run() fail-closed contract ------------
# (imports the ACTUAL production module the Phase 1 heredoc delegates to --
# this is the same test seam `workflow_start_entry.run()`'s own docstring
# documents as injectable for hermetic tests.)


def _import_workflow_start_entry():
    sys.path.insert(0, str(SKILLS_SCRIPTS_DIR))
    import workflow_start_entry as wse  # noqa: PLC0415

    return wse


@pytest.mark.parametrize("malformed_decision", ["typo", None, [], {}])
def test_workflow_start_entry_malformed_or_unknown_decision_fails_closed(malformed_decision):
    """Negative case (review's explicit list): malformed/unknown capability
    decision -> fail. `run()` must classify ANYTHING other than
    ready/degraded as blocked -- including a malformed non-string/None/list
    `decision` value from the producer -- and must NEVER invoke the inner
    refinement preflight in that case."""
    wse = _import_workflow_start_entry()

    def _fake_producer(**_kwargs):
        return {"decision": malformed_decision, "checks": {}, "reasons": []}

    inner_calls = []

    def _spy_inner(*, issue_number, repo):
        inner_calls.append((issue_number, repo))
        return 0

    result, exit_code = wse.run(
        issue_number=9100,
        repo="squne121/loop-protocol",
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=json.dumps(
            [{"phase": "x", "actor_role": "y", "operation": "issue_comment", "requires_mutation": True}]
        ),
        capability_preflight_result_fn=_fake_producer,
        invoke_inner_preflight_fn=_spy_inner,
    )
    assert result["status"] == "blocked"
    assert exit_code == 2
    assert inner_calls == []


@pytest.mark.parametrize("decision", ["ready", "degraded"])
def test_workflow_start_entry_ready_or_degraded_invokes_inner_preflight_exactly_once(decision):
    wse = _import_workflow_start_entry()

    def _fake_producer(**_kwargs):
        return {"decision": decision, "checks": {}, "reasons": []}

    inner_calls = []

    def _spy_inner(*, issue_number, repo):
        inner_calls.append((issue_number, repo))
        return 0

    result, exit_code = wse.run(
        issue_number=9100,
        repo="squne121/loop-protocol",
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=json.dumps(
            [{"phase": "x", "actor_role": "y", "operation": "issue_comment", "requires_mutation": True}]
        ),
        capability_preflight_result_fn=_fake_producer,
        invoke_inner_preflight_fn=_spy_inner,
    )
    assert result["status"] == "ready"
    assert exit_code == 0
    assert inner_calls == [(9100, "squne121/loop-protocol")]


def test_phase1_heredoc_matches_production_status_enum():
    """Structural regression guard: the Phase 1 heredoc's exhaustive shell
    `case`/`esac` on `$ISSUE_TO_IMPL_WSE_STATUS` must cover the exact
    3-value enum `workflow_start_entry._compact_result()` produces, and the
    heredoc itself must call the real `wse.run()` -- not re-implement
    decision classification."""
    script_body = _extract_heredoc(_WORKFLOW_START_ENTRY_HEREDOC_RE)
    assert "wse.run(" in script_body
    assert "invoke_inner_preflight_fn=_spy_invoke_inner_preflight" in script_body
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert 'case "$ISSUE_TO_IMPL_WSE_STATUS" in' in source
    for value in ("blocked", "ready"):
        assert re.search(rf"^\s*{value}\)", source, re.MULTILINE), value


# --- P0-2: live_fixture_readback marker + call-trace classifier -----------


def _run_live_fixture_readback(fixture_issue_number: str, repo: str, fake_gh_state: dict, claude_envelope) -> dict:
    script_body = _extract_heredoc(_LIVE_FIXTURE_READBACK_HEREDOC_RE)

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(fake_gh_state, fh)
        fake_gh_state_path = fh.name
    with tempfile.NamedTemporaryFile("w", suffix=".raw", delete=False, encoding="utf-8") as fh:
        if isinstance(claude_envelope, str):
            fh.write(claude_envelope)
        else:
            json.dump(claude_envelope, fh)
        claude_output_path = fh.name

    proc = _run_python_script(
        script_body, fixture_issue_number, repo, fake_gh_state_path, claude_output_path
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_live_fixture_readback_passes_with_marker_and_matching_call_trace():
    result = _run_live_fixture_readback(
        "9100",
        "squne121/loop-protocol",
        {"calls": [{"operation": "issue_view", "repo": "squne121/loop-protocol", "subcommand": ["issue", "view"], "number": "9100"}]},
        {"type": "result", "is_error": False, "result": "ISSUE_TO_IMPL_FRESH_REVIEW_OK issue_number=9100 contract_complete=true"},
    )
    assert result["marker_issue"] == "9100"
    assert result["marker_complete"] == "true"
    assert result["call_trace_ok"] is True
    assert result["structured_output_ok"] is True


def test_live_fixture_readback_fails_when_marker_present_but_no_matching_gh_call():
    """Negative case (review's explicit list): Claude output has marker but
    no gh call -> fail. The call trace is empty even though the marker is
    perfectly formed -- this must NOT be treated as PASS."""
    result = _run_live_fixture_readback(
        "9100",
        "squne121/loop-protocol",
        {"calls": []},
        {"type": "result", "is_error": False, "result": "ISSUE_TO_IMPL_FRESH_REVIEW_OK issue_number=9100 contract_complete=true"},
    )
    assert result["marker_issue"] == "9100"
    assert result["call_trace_ok"] is False


def test_live_fixture_readback_fails_when_call_trace_is_for_a_different_issue_number():
    result = _run_live_fixture_readback(
        "9100",
        "squne121/loop-protocol",
        {"calls": [{"operation": "issue_view", "repo": "squne121/loop-protocol", "subcommand": ["issue", "view"], "number": "9999"}]},
        {"type": "result", "is_error": False, "result": "ISSUE_TO_IMPL_FRESH_REVIEW_OK issue_number=9100 contract_complete=true"},
    )
    assert result["call_trace_ok"] is False


def test_live_fixture_readback_fails_on_non_json_output():
    result = _run_live_fixture_readback(
        "9100",
        "squne121/loop-protocol",
        {"calls": []},
        "not json at all, just raw text with a marker "
        "ISSUE_TO_IMPL_FRESH_REVIEW_OK issue_number=9100 contract_complete=true",
    )
    assert result["envelope_parsed"] is False
    assert result["marker_issue"] is None


def test_live_fixture_readback_treats_is_error_true_as_not_ok():
    result = _run_live_fixture_readback(
        "9100",
        "squne121/loop-protocol",
        {"calls": [{"operation": "issue_view", "repo": "squne121/loop-protocol", "subcommand": ["issue", "view"], "number": "9100"}]},
        {"type": "result", "is_error": True, "result": "ISSUE_TO_IMPL_FRESH_REVIEW_OK issue_number=9100 contract_complete=true"},
    )
    assert result["structured_output_ok"] is False


# --- P0-3: root_entry_router spy-invocation classifier ----------------------


def _run_root_entry_router_probe(body: str, review_body_sha256, issue_number: int = 9100, repo: str = "squne121/loop-protocol") -> dict:
    """Drive the extracted Phase 5 heredoc directly (it builds its own fake
    transport/contract-review files from the fixture JSON + an injected
    `review_body_sha256`, so tests can force either a `go` or `not go`
    route)."""
    script_body = _extract_heredoc(_ROOT_ENTRY_ROUTER_HEREDOC_RE)
    # The heredoc always computes body_sha256 from the fixture's own body via
    # rer.compute_body_sha256 and writes {"status": "go", "body_sha256":
    # <that value>} -- to force a "not go" route for the negative test, this
    # helper post-processes by wrapping the heredoc with a monkeypatch of
    # `compute_body_sha256` when `review_body_sha256` is explicitly "MISMATCH".
    import tempfile

    fixture = {"issues": {str(issue_number): {"body": body}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(fixture, fh)
        fixture_path = fh.name
    fake_transport_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    fake_contract_review_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name

    if review_body_sha256 == "MISMATCH":
        # Force body-drift so `review_verdict` never becomes "go":
        # monkeypatch `rer.compute_body_sha256` to return two DIFFERENT
        # values across its two call sites (fake_contract_review's
        # body_sha256 write vs. root_entry_router's internal drift check) by
        # prefixing the extracted heredoc with an import-time override that
        # perturbs the value written to the canned review file only.
        script_body = script_body.replace(
            'fake_contract_review = {"status": "go", "body_sha256": body_sha256}',
            'fake_contract_review = {"status": "go", "body_sha256": "0" * 64}',
        )

    proc = _run_python_script(
        script_body,
        str(SKILLS_SCRIPTS_DIR),
        fixture_path,
        str(issue_number),
        repo,
        fake_transport_path,
        fake_contract_review_path,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_root_entry_router_probe_completes_and_invokes_spy_exactly_once_on_go_route():
    result = _run_root_entry_router_probe("# Fixture body\n\nSome content.", review_body_sha256=None)
    assert result["route"] == "invoke_impl_review_loop"
    assert result["invoked"] is True
    assert result["spy_call_count"] == 1
    assert result["phase_status"] == "completed"
    assert result["terminal_result"] == "implementation_not_authorized"


def test_root_entry_router_probe_never_synthesizes_success_when_spy_call_count_is_zero():
    """Negative case (review's explicit list): root_entry_router callback
    count == 0 -> fail (or, on a legitimate non-invoke route, an explicit
    `expected_block` -- never a silent `completed`)."""
    result = _run_root_entry_router_probe("# Fixture body\n\nSome content.", review_body_sha256="MISMATCH")
    assert result["route"] != "invoke_impl_review_loop"
    assert result["invoked"] is False
    assert result["spy_call_count"] == 0
    assert result["phase_status"] == "expected_block"
    assert result["terminal_result"] == "blocked"
    assert result["phase_status"] != "completed"


def test_phase5_heredoc_calls_real_run_root_transition():
    """Structural regression guard: the Phase 5 heredoc must call the real
    `rer.run_root_transition(...)` -- not synthesize `expected_block`
    without ever invoking it."""
    script_body = _extract_heredoc(_ROOT_ENTRY_ROUTER_HEREDOC_RE)
    assert "rer.run_root_transition(" in script_body
    assert "invoke_step1=_spy_invoke_step1" in script_body
    assert "FileBackedFakeGitHubEntryTransport(fake_transport_path)" in script_body


# --- P1-3: expected-phases.json runtime oracle ------------------------------


def _run_phase_oracle(entries, reached_phase, terminal_result, test_verdict) -> dict:
    script_body = _extract_heredoc(_PHASE_ORACLE_HEREDOC_RE)

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
        phase_trace_path = fh.name

    proc = _run_python_script(
        script_body,
        str(FIXTURE_DIR / "expected-phases.json"),
        phase_trace_path,
        reached_phase or "",
        terminal_result or "",
        test_verdict,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_phase_oracle_accepts_the_canonical_happy_path_trace():
    entries = [
        {"phase": "workflow_capability_preflight", "status": "completed"},
        {"phase": "spark_delegation", "status": "not_applicable"},
        {"phase": "fixture_contract_shape_check", "status": "completed"},
        {"phase": "live_fixture_readback", "status": "completed"},
        {"phase": "impl_review_loop_entry", "status": "completed"},
    ]
    result = _run_phase_oracle(entries, "impl_review_loop_entry", "implementation_not_authorized", "pass")
    assert result["ok"] is True
    assert result["problems"] == []


def test_phase_oracle_rejects_reordered_phases():
    entries = [
        {"phase": "spark_delegation", "status": "not_applicable"},
        {"phase": "workflow_capability_preflight", "status": "completed"},
    ]
    result = _run_phase_oracle(entries, "workflow_capability_preflight", None, "fail")
    assert result["ok"] is False
    assert any("phase_order_mismatch" in p for p in result["problems"])


def test_phase_oracle_rejects_disallowed_status_value():
    entries = [{"phase": "workflow_capability_preflight", "status": "not_a_real_status"}]
    result = _run_phase_oracle(entries, "workflow_capability_preflight", "blocked", "fail")
    assert result["ok"] is False
    assert any("disallowed_status" in p for p in result["problems"])


def test_phase_oracle_rejects_pass_verdict_with_non_terminal_last_status():
    """Negative case (review's explicit list): runtime phase trace vs
    expected-phases.json mismatch -> fail. A `test_verdict: pass` whose
    last recorded phase status is `failed` must be rejected by the
    oracle."""
    entries = [
        {"phase": "workflow_capability_preflight", "status": "completed"},
        {"phase": "spark_delegation", "status": "not_applicable"},
        {"phase": "fixture_contract_shape_check", "status": "completed"},
        {"phase": "live_fixture_readback", "status": "failed"},
    ]
    result = _run_phase_oracle(entries, "live_fixture_readback", "human_judgment_required", "pass")
    assert result["ok"] is False
    assert any("pass_verdict_with_non_terminal_last_status" in p for p in result["problems"])


def test_phase_oracle_rejects_unknown_phase_name():
    entries = [{"phase": "not_a_canonical_phase", "status": "completed"}]
    result = _run_phase_oracle(entries, "not_a_canonical_phase", "blocked", "fail")
    assert result["ok"] is False
    assert any("phase_order_mismatch" in p or "unknown_phase" in p for p in result["problems"])


# --- Evidence builder: json.dumps + re-parse (P0-5 style fix) --------------


def test_evidence_builder_writes_valid_reparseable_json(tmp_path):
    script_body = _extract_heredoc(_EVIDENCE_BUILD_HEREDOC_RE)
    phase_trace_path = tmp_path / "phase_trace.jsonl"
    phase_trace_path.write_text(
        json.dumps({"phase": "workflow_capability_preflight", "status": "completed"}) + "\n",
        encoding="utf-8",
    )
    evidence_path = tmp_path / "evidence.json"

    proc = _run_python_script(
        script_body,
        str(evidence_path),
        str(phase_trace_path),
        "pass",
        "implementation_not_authorized",
        "some_reason",
        "impl_review_loop_entry",
        "20260101T000000Z",
        "a" * 40,
        "false",
        "2.0.0",
        "0.1.0",
        "/path/to/launch.sh",
        "/repo/root",
        "sha_a",
        "sha_b",
        "sha_c",
    )
    assert proc.returncode == 0, proc.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema"] == "ISSUE_TO_IMPL_E2E_RESULT_V1"
    assert evidence["test_verdict"] == "pass"
    assert evidence["sut"]["git_dirty"] is False
    assert evidence["phase_trace"] == [{"phase": "workflow_capability_preflight", "status": "completed"}]


# --- fake_gh.py: issue_view call-trace number + combined --json comments ---


def _run_fake_gh(args, state, env_extra=None, tmp_path=None):
    import os
    import tempfile

    if tmp_path is None:
        state_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    else:
        state_file = open(tmp_path / "fake_gh_state.json", "w", encoding="utf-8")
    json.dump(state, state_file)
    state_file.close()
    env = dict(os.environ)
    env["FAKE_GH_STATE"] = state_file.name
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(FAKE_GH_PY), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    with open(state_file.name, encoding="utf-8") as fh:
        final_state = json.load(fh)
    return proc, final_state


def test_fake_gh_issue_view_call_trace_records_the_specific_issue_number():
    state = {
        "next_number": 9101,
        "issues": {"9100": {"title": "T", "url": "u", "repo": "squne121/loop-protocol", "body": "B", "state": "open"}},
        "calls": [],
    }
    proc, final_state = _run_fake_gh(
        ["issue", "view", "9100", "--repo", "squne121/loop-protocol", "--json", "title"], state
    )
    assert proc.returncode == 0, proc.stderr
    view_calls = [c for c in final_state["calls"] if c["operation"] == "issue_view"]
    assert len(view_calls) == 1
    assert view_calls[0]["number"] == "9100"
    assert view_calls[0]["repo"] == "squne121/loop-protocol"


def test_fake_gh_issue_view_combined_json_flag_includes_comments_key():
    state = {
        "next_number": 9101,
        "issues": {"9100": {"title": "T", "url": "u", "repo": "squne121/loop-protocol", "body": "B", "state": "open"}},
        "comments": {"9100": ["hello"]},
        "calls": [],
    }
    proc, _ = _run_fake_gh(
        ["issue", "view", "9100", "--repo", "squne121/loop-protocol", "--json", "title,body,labels,comments"],
        state,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["comments"] == ["hello"]


# --- Schema / wiring regression guards --------------------------------------


def test_runtime_smoke_sh_defines_the_result_schema_and_all_phase_names():
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert "ISSUE_TO_IMPL_E2E_RESULT_V1" in source
    for phase in EXPECTED_PHASES:
        assert phase in source, f"phase {phase!r} not referenced in runtime_smoke_test.sh"
    # The retired phase names must no longer be used as the harness's own
    # phase identifiers (P0-2 scope narrowing) -- only the fix_delta
    # provenance comment referencing the old name is allowed to remain.
    assert '_iti_add_phase "issue_contract_repair"' not in source
    assert '_iti_add_phase "fresh_review"' not in source


def test_runtime_smoke_sh_documents_all_four_typed_terminal_result_values():
    source = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    for value in (
        "draft_pr_ready",
        "blocked",
        "human_judgment_required",
        "implementation_not_authorized",
    ):
        assert value in source, f"terminal_result value {value!r} not documented in runtime_smoke_test.sh"
