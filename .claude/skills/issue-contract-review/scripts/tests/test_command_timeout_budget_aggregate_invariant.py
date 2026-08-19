"""
End-to-end tests for the aggregate invariant (Issue #2233 AC3, fix_delta
P0-3 replacement).

AC3: aggregate invariant (outer budget が command-level budget の合計 +
     cleanup tail を常に上回ること)が、異なる timeout を持つ 2 本の
     non-pure VC を negative control に含めたテストで検証されている。

fix_delta P0-3: the prior implementation of this test hand-constructed a
SECOND `command_timeout_budget/v1` entry (`override_seconds=60`) in local
test-only code and never fed it through the canonical plan / consumers /
executor -- so it never actually exercised production wiring. This version
instead:

  1. Builds a body with TWO non-pure VCs that resolve to GENUINELY
     DIFFERENT, LEGITIMATE, production-reachable budgets: one via the real
     `static_policy` authority (420s, Issue #2233 Background), one via
     `static_fallback` (150s) -- both produced by the SAME single call to
     `compute_canonical_vc_plan()`, not hand-assembled.
  2. Passes that SAME plan object/digest through
     `contract_readiness_check.py` (`compute_invocation_local_baseline_timeout()`
     / `effective_review_budget()`), `run_root_review_pipeline.py` (imports
     the identical function objects), `run_contract_review_once.py`
     (computes the SAME plan from the SAME body and passes its
     `plan_digest` to the executor subprocess), and the
     `baseline_vc_preflight.py` executor itself (`_main_impl()`, which
     recomputes the plan from the body it receives and verifies the digest
     against `--expected-plan-digest`).
  3. Asserts all paths observe the IDENTICAL `plan_digest`.
  4. Asserts the outer deadline is >= Sum(timeout_i + cleanup_tail_i) + margin.
  5. Asserts mutating the body (hence the plan/digest) causes a fail-closed
     `vc_plan_digest_mismatch` rejection BEFORE any subprocess is launched.

Runtime Verification Applicability: not_applicable
`compute_canonical_vc_plan()` / `derive_review_budget()` /
`effective_review_budget()` are side-effect-free; the one real subprocess
launch in this file (`_main_impl()` executing `test -f <missing>`) is a
fast, deterministic, non-VC-suite command used purely to prove the
digest-verification-then-execute (or digest-mismatch-then-reject) code
path, not a timing test.
"""

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parents[1] / "issue-contract-review" / "scripts"))
sys.path.insert(0, str(_SCRIPTS_DIR.parents[1] / "create-issue" / "scripts"))

import baseline_vc_preflight as bvp  # noqa: E402
import contract_readiness_check as crc  # noqa: E402

sys.path.insert(0, str(_SCRIPTS_DIR.parents[1] / "issue-refinement-loop" / "scripts"))
import run_root_review_pipeline as rrp  # noqa: E402

_SLOW_STATIC_POLICY_COMMAND = (
    "uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v"
)

# Two NON-PURE VCs (neither `rg` nor `test -f|-d|-s`) with GENUINELY
# DIFFERENT resolved budgets: the first matches the real
# STATIC_PER_COMMAND_TIMEOUT_POLICY entry (420s, source: static_policy);
# the second falls back to static_fallback (150s). Neither value is
# hand-assembled in this test file -- both come from the SAME single
# `compute_canonical_vc_plan()` call.
_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY = (
    "## Verification Commands\n\n"
    f"```bash\n$ {_SLOW_STATIC_POLICY_COMMAND}\n```\n\n"
    "```bash\n$ pnpm build\n```\n"
)


def test_two_non_pure_vcs_resolve_to_genuinely_different_legitimate_budgets():
    """Sanity precondition for the rest of this file: the fixture body
    really does produce two DIFFERENT, production-reachable `source`
    values/timeouts -- not two budgets that happen to collapse to the same
    number."""
    plan = bvp.compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)
    assert len(plan["command_budgets"]) == 2
    sources = {b["source"] for b in plan["command_budgets"]}
    timeouts = {b["timeout_seconds"] for b in plan["command_budgets"]}
    assert sources == {"static_policy", "static_fallback"}
    assert len(timeouts) == 2  # genuinely different, not coincidentally equal


def test_aggregate_invariant_outer_deadline_covers_sum_of_real_budgets():
    """AC3: the outer deadline (`effective_review_budget()`'s
    `baseline_aggregate_seconds`, fix_delta P0-2) must be
    >= Sum(timeout_i + cleanup_tail_i) + margin, using the SAME plan
    (not a hand-assembled substitute)."""
    plan = bvp.compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)
    review_budget = crc.derive_review_budget(
        plan["command_occurrence_count"], policy_cap=plan["policy_cap"]
    )
    effective_budget = crc.effective_review_budget(review_budget, plan)

    real_sum = sum(
        b["timeout_seconds"] + b["cleanup_tail_seconds"] for b in plan["command_budgets"]
    )
    assert plan["aggregate_timeout_seconds"] == real_sum
    assert effective_budget.baseline_aggregate_seconds >= real_sum + crc._PLAN_AGGREGATE_MARGIN_SECONDS
    # Negative control: the UNFIXED #2207-formula-only value would NOT
    # necessarily dominate the real sum once a static_policy entry exceeds
    # DEFAULT_PER_COMMAND_TIMEOUT_SECONDS -- proving `effective_review_budget()`
    # is doing real work here, not a no-op.
    assert effective_budget.baseline_aggregate_seconds > review_budget.baseline_aggregate_seconds


def test_all_four_consumer_paths_observe_identical_plan_digest():
    """AC2/AC3 (fix_delta P0-1): re-derive the plan the way EACH of the 4
    consumer entrypoints does, and assert every one observes the IDENTICAL
    `plan_digest` for the SAME body:

      - `contract_readiness_check.py`: `_compute_canonical_vc_plan(body)`
        (imported binding, same function object as below)
      - `run_root_review_pipeline.py`: `_compute_canonical_vc_plan` (same
        function object, verified by identity in
        `test_run_contract_review_once_wrapper_wiring.py`)
      - `run_contract_review_once.py`: `compute_canonical_vc_plan(body_snapshot)`
        (same function, same body bytes)
      - `baseline_vc_preflight.py` executor: recomputes internally from its
        own `--body-file` contents and verifies against
        `--expected-plan-digest`
    """
    plan_a = crc._compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)
    plan_b = bvp.compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)
    assert plan_a["plan_digest"] == plan_b["plan_digest"]

    # Executor-side (subprocess-boundary) recomputation: same body bytes ->
    # same digest, verified via the SAME `verify_canonical_vc_plan_digest()`
    # helper the executor calls internally.
    bvp.verify_canonical_vc_plan_digest(plan_b, plan_a["plan_digest"])  # no raise


def test_executor_accepts_matching_plan_digest_and_launches_subprocess(tmp_path, capsys):
    """End-to-end (fix_delta P0-1/P0-3): the executor (`_main_impl()`)
    receives `--expected-plan-digest` computed by an (simulated) parent
    process from the SAME body, and DOES launch the VC subprocess when the
    digest matches (proven via a marker file the executed command creates)."""
    # `test -f <missing-path>` is a SAFE (allowlisted `test -f|-d|-s`
    # predicate) command that is EXPECTED to fail at baseline (exit 1 ->
    # `classification: expected_fail` -> overall `status: pass`) -- a
    # genuinely-executed, non-null exit_code proves the subprocess actually
    # launched and ran (an `unsafe_command`-classified command like `touch`
    # is rejected BEFORE execution by a pre-existing, unrelated safety gate
    # -- exit_code stays null -- which would not distinguish "digest
    # accepted, subprocess launched" from "digest accepted, subprocess
    # execution never reached"; a command that unexpectedly PASSES, like
    # `test -d <existing dir>`, is itself classified `blocked` by an
    # unrelated missing-annotation gate).
    missing_path = tmp_path / "digest-match-nonexistent-marker"
    body = (
        "## Verification Commands\n\n"
        f"```bash\n$ test -f {missing_path}\n```\n"
    )
    body_path = tmp_path / "body.md"
    body_path.write_text(body, encoding="utf-8")

    # Simulated parent: computes the plan from the SAME body bytes.
    parent_plan = bvp.compute_canonical_vc_plan(body)

    argv_backup = sys.argv[:]
    sys.argv = [
        "baseline_vc_preflight.py",
        "--body-file",
        str(body_path),
        "--expected-plan-digest",
        parent_plan["plan_digest"],
    ]
    try:
        exit_code = bvp._main_impl()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "pass"
    assert payload["results"][0]["exit_code"] == 1, (
        "subprocess MUST actually launch and exit non-null when the plan digest matches"
    )
    assert payload["results"][0]["classification"] == "expected_fail"


def test_executor_rejects_mutated_body_before_launching_subprocess(tmp_path, capsys):
    """AC5 / fix_delta P0-1 fail-closed negative control: if the body the
    executor actually reads has drifted from the body a parent process used
    to compute `--expected-plan-digest` (simulated here by mutating the
    body file's Verification Commands section after computing the parent's
    plan), the executor rejects BEFORE launching any subprocess -- proven
    by the marker file NEVER being created."""
    marker_path = tmp_path / "digest_mismatch_marker.txt"
    original_body = (
        "## Verification Commands\n\n"
        f"```bash\n$ touch {marker_path}\n```\n"
    )
    mutated_body = (
        "## Verification Commands\n\n"
        f"```bash\n$ touch {marker_path}\n```\n\n"
        "```bash\n$ pnpm lint\n```\n"
    )
    body_path = tmp_path / "body.md"
    body_path.write_text(mutated_body, encoding="utf-8")

    # Simulated parent computed its plan_digest from the ORIGINAL body,
    # before the mutation -- exactly the TOCTOU gap this mechanism guards.
    parent_plan = bvp.compute_canonical_vc_plan(original_body)

    argv_backup = sys.argv[:]
    sys.argv = [
        "baseline_vc_preflight.py",
        "--body-file",
        str(body_path),
        "--expected-plan-digest",
        parent_plan["plan_digest"],
    ]
    try:
        exit_code = bvp._main_impl()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code != 0
    assert payload["status"] == "blocked"
    assert payload["failure_class"] == "vc_plan_digest_mismatch"
    assert payload["retryable"] is False
    assert payload["results"] == []
    assert not marker_path.exists(), (
        "subprocess must NOT be launched when the recomputed plan digest "
        "does not match --expected-plan-digest"
    )


def test_all_four_consumer_paths_observe_identical_plan_digest_for_directory_scoped_rg(
    tmp_path, capsys
):
    """Issue #2232 Scope Delta P0-1 (OWNER REQUEST_CHANGES
    https://github.com/squne121/loop-protocol/pull/2255#issuecomment-5340600982):
    extend `test_all_four_consumer_paths_observe_identical_plan_digest()`
    above to a directory-scoped `rg` command inside an Allowed Path -- the
    case where `is_pure` (and therefore `plan_digest`) genuinely DIFFERS
    between context-free and context-aware classification, unlike the
    fixture above (two non-pure, `static_policy`-routed commands whose
    `is_pure` never flips). Confirms:

      1. context-free classification and context-aware (real cwd +
         Allowed Paths) classification genuinely DISAGREE for this fixture
         (precondition -- otherwise this test would not exercise the P0-1
         bug at all).
      2. `contract_readiness_check.py`, `run_root_review_pipeline.py`, and
         `run_contract_review_once.py`'s parent-side digest computation --
         all three now wired (Issue #2232 Scope Delta) to pass the SAME
         real `cwd` / Allowed-Paths-extracted-from-body context -- converge
         on the IDENTICAL `plan_digest`.
      3. Subprocess-boundary (executor) recomputation over a REAL
         `--body-file` / `--cwd` CLI invocation accepts that SAME digest.
      4. Subprocess-boundary NEGATIVE control: a digest computed with a
         STALE/DIFFERENT (context-free) classification context is REJECTED
         by the real child subprocess with `vc_plan_digest_mismatch` --
         proving this convergence check is not vacuous.
    """
    target_dir = tmp_path / "fixture_dir"
    target_dir.mkdir()
    (target_dir / "a.txt").write_text("pattern\n", encoding="utf-8")
    command = "rg -q pattern fixture_dir"

    body = (
        "## Allowed Paths\n\n"
        "- fixture_dir\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# baseline-expect: pass\n"
        f"$ {command}\n"
        "```\n"
    )

    # 1. Precondition: context genuinely flips classification for this body.
    context_free_plan = bvp.compute_canonical_vc_plan(body)
    context_aware_plan = bvp.compute_canonical_vc_plan(
        body, cwd=str(tmp_path), allowed_paths=["fixture_dir"]
    )
    assert context_free_plan["command_occurrences"][0]["is_pure"] is False
    assert context_aware_plan["command_occurrences"][0]["is_pure"] is True
    assert context_free_plan["plan_digest"] != context_aware_plan["plan_digest"]

    # Real Allowed Paths extraction from the body -- the SAME helper every
    # production consumer now reuses (Issue #2232 Scope Delta).
    allowed_paths_from_body = bvp.extract_allowed_paths(body)
    assert allowed_paths_from_body == ["fixture_dir"]

    # 2. Re-derive the plan the way EACH wired consumer now does, given the
    #    SAME real cwd/Allowed Paths context:
    #      - contract_readiness_check.py (both call sites)
    #      - run_root_review_pipeline.py (same function object)
    #      - run_contract_review_once.py's parent-side digest computation
    crc_plan = crc._compute_canonical_vc_plan(
        body, cwd=str(tmp_path), allowed_paths=allowed_paths_from_body
    )
    rrp_plan = rrp._compute_canonical_vc_plan(
        body, cwd=str(tmp_path), allowed_paths=allowed_paths_from_body
    )
    rcro_plan = bvp.compute_canonical_vc_plan(
        body, cwd=str(tmp_path), allowed_paths=allowed_paths_from_body
    )
    assert (
        crc_plan["plan_digest"]
        == rrp_plan["plan_digest"]
        == rcro_plan["plan_digest"]
        == context_aware_plan["plan_digest"]
    )

    # 3. Subprocess-boundary (executor) recomputation from the SAME body +
    #    the SAME real cwd/Allowed Paths, over a REAL CLI invocation.
    body_path = tmp_path / "body.md"
    body_path.write_text(body, encoding="utf-8")
    argv_backup = sys.argv[:]
    sys.argv = [
        "baseline_vc_preflight.py",
        "--body-file",
        str(body_path),
        "--cwd",
        str(tmp_path),
        "--issue",
        "999",
        "--expected-plan-digest",
        rcro_plan["plan_digest"],
    ]
    try:
        exit_code = bvp._main_impl()
    finally:
        sys.argv = argv_backup
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0, payload
    assert payload.get("failure_class") != "vc_plan_digest_mismatch", payload
    assert payload["results"][0]["exit_code"] == 0, (
        "subprocess MUST actually launch when the plan digest matches"
    )
    assert payload["results"][0]["classification"] == "expected_pass"

    # 4. Negative control (subprocess-boundary): a digest computed with a
    #    STALE (context-free) classification context is rejected BEFORE any
    #    VC subprocess launches -- proving the convergence check above is
    #    not vacuous.
    stale_digest = context_free_plan["plan_digest"]
    sys.argv = [
        "baseline_vc_preflight.py",
        "--body-file",
        str(body_path),
        "--cwd",
        str(tmp_path),
        "--issue",
        "999",
        "--expected-plan-digest",
        stale_digest,
    ]
    try:
        exit_code_stale = bvp._main_impl()
    finally:
        sys.argv = argv_backup
    captured_stale = capsys.readouterr()
    payload_stale = json.loads(captured_stale.out)
    assert exit_code_stale != 0
    assert payload_stale["status"] == "blocked"
    assert payload_stale["failure_class"] == "vc_plan_digest_mismatch"
    assert payload_stale["results"] == []


def test_aggregate_timeout_seconds_can_now_legitimately_exceed_old_worst_case():
    """Regression guard against the OLD (pre-fix_delta) structural
    guarantee, which the OWNER flagged as PROOF the original failure mode
    was unresolved: `aggregate_timeout_seconds` can now legitimately EXCEED
    `command_occurrence_count * (DEFAULT_PER_COMMAND_TIMEOUT_SECONDS +
    CLEANUP_TAIL_SECONDS)` once a `static_policy` entry applies -- this is
    the whole point of the fix (a genuinely slow, legitimate VC is allowed
    a real budget above the old fixed 150s cap)."""
    plan = bvp.compute_canonical_vc_plan(_TWO_NON_PURE_DIFFERENT_TIMEOUT_BODY)
    old_worst_case = plan["command_occurrence_count"] * (
        bvp.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS + bvp.CLEANUP_TAIL_SECONDS
    )
    assert plan["aggregate_timeout_seconds"] > old_worst_case
