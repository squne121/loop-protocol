"""Issue #3074: opt-in Step 5 UNSTABLE reconciliation via a LIVE required-CI proof.

Self-contained: every fixture lives in this file or in ``tmp_path`` (a fake
``gh`` on PATH stands in for GitHub). Nothing depends on Remote Ops paths,
private receipts, or skip / xfail markers.

Fixture-PASS vs live acceptance: these tests run the production
``step5-terminal-gate`` CLI as a real subprocess and the real
``wait_ci_checks.py`` against a *fake* ``gh``. They prove the wiring and the
fail-closed behaviour; they are NOT evidence of a real GitHub required-CI
read. The immediate runtime verification (AC1) is recorded separately.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = TESTS_DIR.parent / "scripts"
CLI = SCRIPTS / "adjudicate_vc_result.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


route = _load("route_under_test_step5_unstable_proof", SCRIPTS / "route_loop_verdict_v2.py")
adj = _load("adjudicate_under_test_step5_unstable_proof", CLI)
# Reuse the production-wiring fixtures (same repo, no skip): dispatch state etc.
base = _load(
    "terminal_gate_regression_for_step5_unstable_proof",
    TESTS_DIR / "test_independent_vc_terminal_gate_regression.py",
)

HEAD = base.HEAD_A
OTHER = "f" * 40
PR = base.PR_NUMBER
REPO = "squne121/loop-protocol"
NAMES = ["python-test", "typecheck", "test", "lint", "build", "validate-generated-artifact"]
UNSTABLE_MSG = "merge_state_status_unstable_not_conflict_defer_to_ci_evaluator"


def _ci(head: str = HEAD, names: list[str] = NAMES, **over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "CI_WAIT_RESULT_V1",
        "status": "passed",
        "repo": REPO,
        "pr_number": PR,
        "head_sha": head,
        "current_head_sha": head,
        "required_only": True,
        "checks": [{"bucket": "pass", "name": n, "state": "SUCCESS", "workflow": "ci", "link": "x"} for n in names],
        "elapsed_seconds": 7,
        "interval_seconds": 1,
        "timeout_seconds": 35,
        "error_code": None,
        "message": "all required checks passed",
    }
    payload.update(over)
    return payload


def _live(
    status: str = "UNSTABLE", mergeable: str = "MERGEABLE", head: str = HEAD, ci: Any = None, expected_pr: Any = PR
):
    live: dict[str, Any] = {"head_sha": head, "mergeable": mergeable, "merge_state_status": status}
    if ci is not None:
        live["required_ci_proof"] = {"ci_wait_result": ci, "expected_pr_number": expected_pr}
    return live


def _rev(head: str = HEAD, verdict: str = "APPROVE", blockers: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"verdict": verdict, "reviewed_head_sha": head, "blockers": list(blockers), "warnings": []}


def _mut(ci: dict[str, Any], fn: Any) -> dict[str, Any]:
    ci = copy.deepcopy(ci)
    fn(ci)
    return ci


# --- router (pure two-arg API) -------------------------------------------------


def test_unstable_without_proof_stays_fail_closed():
    d = route.route_loop_verdict_v2(_rev(), _live())
    assert d.route == "fail_closed" and d.reason_code == UNSTABLE_MSG


def test_unstable_with_valid_internal_proof_is_approved():
    d = route.route_loop_verdict_v2(_rev(), _live(ci=_ci()))
    assert d.route == "approved" and not d.fail_closed


def test_proof_does_not_mutate_live_fields():
    live = _live(ci=_ci())
    before = copy.deepcopy(live)
    route.route_loop_verdict_v2(_rev(), live)
    assert live == before and live["merge_state_status"] == "UNSTABLE"


def _bad_cases():
    ci = _ci()
    yield "empty_checks", _ci(names=[])
    yield "failing_check", _mut(ci, lambda c: c["checks"][0].update(state="FAILURE", bucket="fail"))
    yield "pending_check", _mut(ci, lambda c: c["checks"][0].update(state="PENDING", bucket="pending"))
    yield "skipping_bucket", _mut(ci, lambda c: c["checks"][0].update(bucket="skipping"))
    yield "status_pending", _ci(status="pending", error_code="pending_timeout")
    yield "passed_with_error_code", _ci(error_code="gh_error")
    yield "status_failed", _ci(status="failed")
    yield "required_only_false", _ci(required_only=False)
    yield "required_only_string", _ci(required_only="true")
    yield "wrong_schema", _ci(schema="CI_WAIT_RESULT_V2")
    yield "other_repo", _ci(repo="someone/else")
    yield "other_pr", _ci(pr_number=PR + 1)
    yield "pr_bool", _ci(pr_number=True)
    yield "forged_head", _ci(head=OTHER)
    yield "head_ne_current", _ci(current_head_sha=OTHER)
    yield "duplicate_names", _ci(names=NAMES[:-1] + [NAMES[0]])
    yield "blank_name", _ci(names=NAMES[:-1] + ["  "])
    yield "checks_not_list", _ci(checks="all green")
    yield "check_not_dict", _mut(ci, lambda c: c["checks"].append("ok"))
    yield "missing_key", {k: v for k, v in ci.items() if k != "message"}
    yield "extra_key", _ci(passed=True)
    yield "boolean_only", True
    yield "none_payload", None


_BAD = list(_bad_cases())


@pytest.mark.parametrize("name,ci", _BAD, ids=[n for n, _ in _BAD])
def test_invalid_proof_never_approves(name, ci):
    live = _live()
    live["required_ci_proof"] = {"ci_wait_result": ci, "expected_pr_number": PR}
    d = route.route_loop_verdict_v2(_rev(), live)
    assert d.route == "fail_closed" and d.reason_code == UNSTABLE_MSG


@pytest.mark.parametrize("expected", [PR + 1, True, 0, str(PR), None])
def test_expected_pr_binding_must_match(expected):
    assert route.route_loop_verdict_v2(_rev(), _live(ci=_ci(), expected_pr=expected)).route == "fail_closed"


def test_malformed_proof_container_fail_closed():
    good = _live(ci=_ci())["required_ci_proof"]
    for proof in (True, "passed", [], {"ci_wait_result": _ci()}, {**good, "x": 1}):
        live = _live()
        live["required_ci_proof"] = proof
        assert route.route_loop_verdict_v2(_rev(), live).route == "fail_closed"


def test_proof_head_must_match_live_head():
    assert route.route_loop_verdict_v2(_rev(), _live(head=OTHER, ci=_ci())).route != "approved"


def test_stale_reviewed_head_not_approved():
    assert route.route_loop_verdict_v2(_rev(head=OTHER), _live(ci=_ci())).route == "route_stale_head_rereview"


def test_request_changes_and_blockers_not_approved():
    assert route.route_loop_verdict_v2(_rev(verdict="REQUEST_CHANGES"), _live(ci=_ci())).route == "continue_loop"
    assert route.route_loop_verdict_v2(_rev(blockers=("b",)), _live(ci=_ci())).route == "fail_closed"


@pytest.mark.parametrize(
    "status,mergeable,expected",
    [
        ("DIRTY", "MERGEABLE", "conflict_hard_stop"),
        ("UNSTABLE", "CONFLICTING", "conflict_hard_stop"),
        ("UNSTABLE", "UNKNOWN", "fail_closed"),
        ("BEHIND", "MERGEABLE", "route_to_update_branch"),
        ("BLOCKED", "MERGEABLE", "fail_closed"),
        ("DRAFT", "MERGEABLE", "fail_closed"),
        ("UNKNOWN", "MERGEABLE", "fail_closed"),
    ],
)
def test_proof_only_applies_to_unstable_mergeable(status, mergeable, expected):
    assert route.route_loop_verdict_v2(_rev(), _live(status=status, mergeable=mergeable, ci=_ci())).route == expected


@pytest.mark.parametrize("status", ["CLEAN", "HAS_HOOKS"])
def test_clean_and_has_hooks_still_approved_without_proof(status):
    assert route.route_loop_verdict_v2(_rev(), _live(status=status)).route == "approved"


def test_router_signature_unchanged():
    assert list(inspect.signature(route.route_loop_verdict_v2).parameters) == ["reviewer_verdict", "live_mergeability"]


# --- live helper (canonical checker invocation) -----------------------------------


class _Proc:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _line(payload: Any) -> str:
    return "noise\nCI_WAIT_RESULT_V1_JSON=" + json.dumps(payload) + "\n"


def test_live_helper_fixed_bounded_command():
    seen: dict[str, Any] = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _Proc(0, _line(_ci()))

    proof = adj._live_required_ci_proof(PR, HEAD, run=run)
    assert proof == {"ci_wait_result": _ci(), "expected_pr_number": PR}
    argv = seen["argv"]
    assert Path(argv[1]) == SCRIPTS / "wait_ci_checks.py"
    assert argv[2:] == [
        "--repo",
        REPO,
        "--pr",
        str(PR),
        "--head-sha",
        HEAD,
        "--required",
        "--interval",
        "5",
        "--timeout-seconds",
        "30",
    ]
    assert seen["kw"]["timeout"] == 55 and "shell" not in seen["kw"]


@pytest.mark.parametrize(
    "proc",
    [
        _Proc(1, _line(_ci())),
        _Proc(2, _line(_ci())),
        _Proc(0, ""),
        _Proc(0, "CI_WAIT_RESULT_V1_JSON={bad"),
        _Proc(0, _line(_ci()) + _line(_ci())),
        _Proc(0, "CI_WAIT_RESULT_V1_JSON=[1]"),
    ],
)
def test_live_helper_rejects_bad_runs(proc):
    assert adj._live_required_ci_proof(PR, HEAD, run=lambda *a, **k: proc) is None


def test_live_helper_timeout_and_oserror():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 55)

    def oserr(*a, **k):
        raise OSError("nope")

    assert adj._live_required_ci_proof(PR, HEAD, run=boom) is None
    assert adj._live_required_ci_proof(PR, HEAD, run=oserr) is None


@pytest.mark.parametrize(
    "over",
    [{"status": "pending"}, {"status": "failed"}, {"error_code": "auth"}, {"current_head_sha": OTHER}, {"checks": []}],
)
def test_live_helper_bad_payloads_do_not_approve_in_router(over):
    proof = adj._live_required_ci_proof(PR, HEAD, run=lambda *a, **k: _Proc(0, _line(_ci(**over))))
    assert route.route_loop_verdict_v2(_rev(), {**_live(), "required_ci_proof": proof}).route == "fail_closed"


# --- production CLI with a fake gh -------------------------------------------------

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
mode = os.environ["FAKE_GH_MODE"]
head = os.environ["FAKE_GH_HEAD"]
pr = os.environ["FAKE_GH_PR"]
names = json.loads(os.environ["FAKE_GH_CHECKS"])
inv = json.loads(os.environ["FAKE_GH_INVENTORY"])
if a[:2] in (["pr", "view"], ["pr", "checks"]) and a[2] != pr:
    sys.stderr.write("Could not resolve to a PullRequest"); sys.exit(1)
if a[:2] == ["pr", "view"]:
    if mode == "auth403":
        sys.stderr.write("HTTP 403: Forbidden"); sys.exit(1)
    if "headRefOid" in a:
        print("f" * 40 if mode == "head_changed" else head)
    else:
        print("main")
elif a[:2] == ["pr", "checks"]:
    st = {"pass": ("pass", "SUCCESS"), "fail": ("fail", "FAILURE"), "pending": ("pending", "PENDING")}[
        "fail" if mode == "failed" else "pending" if mode == "pending" else "pass"]
    print(json.dumps([{"name": n, "bucket": st[0], "state": st[1], "workflow": "ci", "link": "x"} for n in names]))
elif a[0] == "api" and "/rules/branches/" in a[1]:
    ctx = [{"context": n, "integration_id": 1} for n in inv]
    print(json.dumps([[{"type": "required_status_checks", "parameters": {"required_status_checks": ctx}}]]))
elif a[0] == "api" and "protection/required_status_checks" in a[1]:
    sys.stderr.write("Branch not protected"); sys.exit(1)
else:
    sys.stderr.write("unexpected " + repr(a)); sys.exit(1)
"""


def _fake_env(
    tmp: Path, mode: str = "passed", head: str = HEAD, names: list[str] = NAMES, inventory: list[str] = NAMES
):
    bindir = tmp / "fakebin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        FAKE_GH_MODE=mode,
        FAKE_GH_HEAD=head,
        FAKE_GH_PR=str(PR),
        FAKE_GH_CHECKS=json.dumps(names),
        FAKE_GH_INVENTORY=json.dumps(inventory),
    )
    return env


class Gate:
    """A dispatched workspace (seq=1, VC PASS persisted via step4-adjudicate) driving step5-terminal-gate."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.ws = base._dispatched(tmp)
        self.hashes = base._hashes(False)

    def run(self, *, live=None, reviewer=None, live_ci=False, seq=1, pr=PR, head=HEAD, extra=(), env=None):
        argv = [
            sys.executable,
            str(CLI),
            "step5-terminal-gate",
            *self.ws._binding_args(head, base.BODY_A, self.hashes, "t5"),
            "--reviewer-verdict-file",
            self.ws.write("rv.json", reviewer or base._reviewer_verdict(HEAD)),
            "--live-mergeability-file",
            self.ws.write("lm.json", live or _live()),
            "--dispatch-seq",
            str(seq),
        ]
        if live_ci:
            argv.append("--verify-required-ci-live")
        if pr is not None:
            argv += ["--expected-pr-number", str(pr)]
        argv += list(extra)
        c = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=90, check=False)
        lines = c.stdout.strip().splitlines()
        return c.returncode, (json.loads(lines[-1]) if lines else None), c.stderr


def test_cli_default_unstable_fail_closed(tmp_path):
    rc, out, _ = Gate(tmp_path).run()
    assert rc == 1 and out["route"] == "fail_closed" and out["reason_code"] == UNSTABLE_MSG


def test_cli_required_ci_result_file_flag_does_not_exist(tmp_path):
    rc, _, err = Gate(tmp_path).run(extra=["--required-ci-result-file", str(tmp_path / "x")])
    assert rc == 2 and "unrecognized" in err


def test_cli_live_checker_passes_and_approves(tmp_path):
    gate = Gate(tmp_path)
    live = _live()
    rc, out, err = gate.run(live=live, live_ci=True, env=_fake_env(tmp_path))
    assert rc == 0, (out, err)
    assert out["route"] == "approved" and out["fail_closed"] is False
    assert json.loads((tmp_path / "lm.json").read_text()) == live  # live file not rewritten
    assert live["merge_state_status"] == "UNSTABLE"


@pytest.mark.parametrize("mode", ["failed", "pending", "auth403", "head_changed"])
def test_cli_bad_live_statuses_never_approve(tmp_path, mode):
    rc, out, _ = Gate(tmp_path).run(live_ci=True, env=_fake_env(tmp_path, mode=mode))
    assert rc == 1 and out["route"] == "fail_closed"


def test_cli_missing_required_context_never_approves(tmp_path):
    # Ruleset requires 6 contexts but only the first has materialized -> checker must not pass.
    env = _fake_env(tmp_path, names=NAMES[:1], inventory=NAMES)
    rc, out, _ = Gate(tmp_path).run(live_ci=True, env=env)
    assert rc == 1 and out["route"] == "fail_closed"


def test_cli_one_check_fake_file_never_grants_approval(tmp_path):
    gate = Gate(tmp_path)
    fake = _ci(names=["x"])
    path = gate.ws.write("fake_ci.json", fake)
    rc, _, _ = gate.run(extra=["--required-ci-result-file", path])
    assert rc == 2
    smuggled = {**_live(), "required_ci_proof": {"ci_wait_result": fake, "expected_pr_number": PR}}
    assert gate.run(live=smuggled)[0] == 1
    rc, out, _ = gate.run(live=smuggled, live_ci=True, env=_fake_env(tmp_path, mode="failed"))
    assert rc == 1 and out["route"] == "fail_closed"


def test_cli_live_flag_requires_expected_pr_number(tmp_path):
    rc, _, err = Gate(tmp_path).run(live_ci=True, pr=None)
    assert rc == 2 and "--expected-pr-number" in err


def test_cli_live_wrong_pr_or_head_rejected(tmp_path):
    gate = Gate(tmp_path)
    env = _fake_env(tmp_path)
    assert gate.run(live_ci=True, pr=PR + 1, env=env)[0] == 1
    assert gate.run(live_ci=True, head=OTHER, env=env)[0] == 1


def test_cli_wrong_dispatch_seq_and_altered_reviewed_head(tmp_path):
    gate = Gate(tmp_path)
    env = _fake_env(tmp_path)
    rc, out, _ = gate.run(live_ci=True, seq=2, env=env)
    assert rc == 1 and out["reason_code"] == "dispatch_seq_mismatch"
    rc, out, _ = gate.run(live_ci=True, env=env, reviewer=base._reviewer_verdict(OTHER))
    assert rc == 1 and out["route"] == "route_stale_head_rereview"


def test_cli_dirty_conflicting_blocked_draft_not_approved(tmp_path):
    gate = Gate(tmp_path)
    env = _fake_env(tmp_path)
    assert gate.run(live_ci=True, env=env, live=_live(status="DIRTY"))[1]["route"] == "conflict_hard_stop"
    assert gate.run(live_ci=True, env=env, live=_live(mergeable="CONFLICTING"))[1]["route"] == "conflict_hard_stop"
    for st in ("BLOCKED", "DRAFT"):
        rc, out, _ = gate.run(live_ci=True, env=env, live=_live(status=st))
        assert rc == 1 and out["route"] == "fail_closed"


def test_cli_clean_still_approved_without_proof(tmp_path):
    rc, out, _ = Gate(tmp_path).run(live=_live(status="CLEAN"))
    assert rc == 0 and out["route"] == "approved"


@pytest.mark.parametrize("sub", ["step4-gate", "step4-adjudicate", "adapt", "adjudicate", "extract-vc-metadata"])
def test_flag_rejected_on_other_subcommands(sub):
    c = subprocess.run(
        [sys.executable, str(CLI), sub, "--verify-required-ci-live"], capture_output=True, text=True, check=False
    )
    assert c.returncode == 2 and "only accepted by step5-terminal-gate" in c.stderr


# --- direct Python gate ----------------------------------------------------------


def _gate_kwargs(gate: Gate) -> dict[str, Any]:
    return dict(
        expected_head_sha=HEAD,
        expected_contract_body_sha256=base.BODY_A,
        expected_command_hashes=gate.hashes,
        dispatch_seq=1,
    )


def test_direct_gate_ignores_embedded_live_proof(tmp_path):
    gate = Gate(tmp_path)
    live = {**_live(), "required_ci_proof": {"ci_wait_result": _ci(), "expected_pr_number": PR}}
    rc, out = adj.step5_terminal_gate(gate.ws.state(), base._reviewer_verdict(HEAD), live, **_gate_kwargs(gate))
    assert rc == 1 and out["route"] == "fail_closed"


def test_direct_gate_accepts_only_root_supplied_proof(tmp_path):
    gate = Gate(tmp_path)
    proof = {"ci_wait_result": _ci(), "expected_pr_number": PR}
    rc, out = adj.step5_terminal_gate(
        gate.ws.state(), base._reviewer_verdict(HEAD), _live(), _root_live_ci_proof=proof, **_gate_kwargs(gate)
    )
    assert rc == 0 and out["route"] == "approved"
