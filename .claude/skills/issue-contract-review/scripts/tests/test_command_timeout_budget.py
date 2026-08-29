"""
Unit tests for `command_timeout_budget/v1` (Issue #2233 AC1 / AC5 / AC6).

AC1: `command_timeout_budget/v1` の各フィールド（command_hash /
     execution_key_hash / timeout_seconds / cleanup_tail_seconds / source /
     estimator_version / estimator_input_digest / sample_count /
     observed_p95_ms / policy_clamped）が実装されている。

AC5 (select with -k policy_ceiling): 推定値が per-command hard maximum を
     超えた場合、subprocess を起動せず typed non-retryable failure
     (`command_timeout_exceeds_policy`) を返す。

AC6 (select with -k provenance): result item に実効 timeout の provenance
     が出力される。

Runtime Verification Applicability: not_applicable
`compute_command_timeout_budget()` / `compute_canonical_vc_plan()` は
side-effect-free（subprocess を起動しない）ため、軽量ユニットテストのみで
完結する。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import baseline_vc_preflight as m  # noqa: E402


# ---------------------------------------------------------------------------
# AC1: command_timeout_budget/v1 schema
# ---------------------------------------------------------------------------


def test_command_timeout_budget_has_all_ac1_fields():
    budget = m.compute_command_timeout_budget("rg -n foo bar.py")
    expected_fields = {
        "schema",
        "command_hash",
        # fix_delta P1-2: renamed from `execution_key_hash` -- NOT the same
        # value/semantics as `compute_execution_key_hash()` (which
        # additionally binds argv/cwd/env/timeout/state epoch).
        "command_identity_hash",
        "timeout_seconds",
        "cleanup_tail_seconds",
        "source",
        "estimator_version",
        "estimator_input_digest",
        "sample_count",
        "observed_p95_ms",
        "policy_clamped",
    }
    assert expected_fields <= set(budget.keys())
    assert budget["schema"] == "command_timeout_budget/v1"


def test_command_timeout_budget_command_hash_matches_compute_command_hash():
    command = "uv run --locked pytest foo.py -v"
    budget = m.compute_command_timeout_budget(command)
    assert budget["command_hash"] == m.compute_command_hash(command)


def test_command_timeout_budget_distinct_commands_get_distinct_hashes():
    budget_a = m.compute_command_timeout_budget("uv run --locked pytest a.py -v")
    budget_b = m.compute_command_timeout_budget("uv run --locked pytest b.py -v")
    assert budget_a["command_hash"] != budget_b["command_hash"]
    assert budget_a["estimator_input_digest"] != budget_b["estimator_input_digest"]


def test_command_timeout_budget_estimator_input_digest_is_deterministic():
    command = "pnpm typecheck"
    first = m.compute_command_timeout_budget(command)
    second = m.compute_command_timeout_budget(command)
    assert first["estimator_input_digest"] == second["estimator_input_digest"]
    assert first["estimator_input_digest"].startswith("sha256:")


# ---------------------------------------------------------------------------
# AC5 (`-k policy_ceiling`): hard maximum -> typed non-retryable failure,
# subprocess NEVER launched.
# ---------------------------------------------------------------------------


def test_policy_ceiling_rejects_override_exceeding_max():
    """AC5: an explicit override exceeding MAX_PER_COMMAND_TIMEOUT_SECONDS
    is rejected with a typed, non-retryable error -- BEFORE any subprocess
    would be launched (this function never launches a subprocess itself,
    proving the rejection happens at budget-computation time)."""
    too_large = m.MAX_PER_COMMAND_TIMEOUT_SECONDS + 1
    try:
        m.compute_command_timeout_budget("pnpm test", override_seconds=too_large)
        assert False, "expected CommandTimeoutExceedsPolicyError"
    except m.CommandTimeoutExceedsPolicyError as exc:
        assert exc.error_code == "command_timeout_exceeds_policy"
        assert exc.requested_seconds == too_large
        assert exc.max_seconds == m.MAX_PER_COMMAND_TIMEOUT_SECONDS


def test_policy_ceiling_rejects_static_policy_entry_exceeding_max(monkeypatch):
    """AC5 (fix_delta): a `static_policy`-sourced entry is rejected through
    the SAME enforcement path as an explicit override if it exceeds the
    ceiling -- a single hard ceiling applies regardless of source. Uses
    monkeypatch to inject a deliberately-over-ceiling static policy entry
    rather than mutating the production `STATIC_PER_COMMAND_TIMEOUT_POLICY`
    table."""
    command = "pnpm build --deliberately-over-ceiling-fixture"
    too_large = m.MAX_PER_COMMAND_TIMEOUT_SECONDS + 500
    bad_policy = {command: too_large}
    try:
        m.compute_command_timeout_budget(command, static_policy=bad_policy)
        assert False, "expected CommandTimeoutExceedsPolicyError"
    except m.CommandTimeoutExceedsPolicyError as exc:
        assert exc.error_code == "command_timeout_exceeds_policy"


def test_static_policy_entry_below_ceiling_resolves_with_static_policy_source():
    """fix_delta P0-2: the trusted static policy authority -- a REAL
    production entry, not a test-only fabrication -- resolves to a budget
    ABOVE DEFAULT_PER_COMMAND_TIMEOUT_SECONDS (150s), proving Issue #2233's
    original failure mode (a legitimate single VC taking 271.31s getting
    killed by a fixed 150s cap) is addressed by a real authority."""
    slow_command = "uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v"
    assert slow_command in m.STATIC_PER_COMMAND_TIMEOUT_POLICY
    budget = m.compute_command_timeout_budget(slow_command)
    assert budget["source"] == "static_policy"
    assert budget["timeout_seconds"] > m.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS
    assert budget["timeout_seconds"] >= 271  # exceeds the measured 271.31s failure case
    assert budget["timeout_seconds"] <= m.MAX_PER_COMMAND_TIMEOUT_SECONDS


def test_static_policy_ceiling_dominates_every_curated_entry():
    """Structural guarantee: MAX_PER_COMMAND_TIMEOUT_SECONDS must always be
    >= every STATIC_PER_COMMAND_TIMEOUT_POLICY entry (enforced by an
    assertion at import time too); this test fixes the relationship for
    regression coverage independent of module import order."""
    assert m.MAX_PER_COMMAND_TIMEOUT_SECONDS >= max(
        m.STATIC_PER_COMMAND_TIMEOUT_POLICY.values()
    )


def test_resolved_seconds_non_positive_is_rejected_regardless_of_source():
    """fix_delta P2: a non-positive explicit override is rejected (not
    silently accepted, as the pre-fix_delta version did -- it checked only
    the ceiling, not a floor on non-positive values)."""
    for bad_value in (0, -1, -1000):
        try:
            m.compute_command_timeout_budget("pnpm test", override_seconds=bad_value)
            assert False, f"expected CommandTimeoutNonPositiveError for {bad_value}"
        except m.CommandTimeoutNonPositiveError as exc:
            assert exc.error_code == "command_timeout_non_positive"
            assert exc.requested_seconds == bad_value


def test_aggregate_hard_maximum_enforced_in_compute_canonical_vc_plan():
    """AC5 (fix_delta P1-1): the aggregate hard maximum
    (MAX_TOTAL_VERIFICATION_BUDGET_SECONDS) is a REAL production check
    inside compute_canonical_vc_plan(), not merely an informational
    constant -- a plan whose aggregate exceeds it is rejected BEFORE any
    subprocess is launched (proven here: compute_canonical_vc_plan() itself
    never launches a subprocess)."""
    # Build a body with enough occurrences of the static_policy slow command
    # that the aggregate (each occurrence contributing 420+15=435s) exceeds
    # MAX_TOTAL_VERIFICATION_BUDGET_SECONDS, while staying comfortably under
    # the UNRELATED occurrence-count policy_cap (40) so this test isolates
    # the aggregate-seconds check, not the occurrence-count check.
    slow_command = "uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v"
    # `compute_canonical_vc_plan()` does NOT itself enforce the UNRELATED
    # occurrence-count `policy_cap` (that is `contract_readiness_check.py`'s
    # `derive_review_budget()`'s job) -- so this fixture is free to use an
    # occurrence count larger than `MAX_VC_EXECUTION_SLOTS` to isolate the
    # aggregate-seconds check.
    occurrences_needed = (m.MAX_TOTAL_VERIFICATION_BUDGET_SECONDS // 435) + 2
    blocks = "\n\n".join(
        f"```bash\n$ {slow_command} --shard={i}\n```" for i in range(occurrences_needed)
    )
    # Each shard is a DISTINCT command text (so none dedupe in
    # command_budgets), but none matches the curated static_policy key
    # exactly (that key has no `--shard=`), so every occurrence resolves via
    # static_fallback... which would UNDER-shoot the aggregate ceiling at
    # 150s each. Instead, drive the aggregate over the ceiling using
    # override_seconds via global_override_seconds, which the aggregate
    # check applies uniformly regardless of source.
    body = "## Verification Commands\n\n" + blocks + "\n"
    try:
        m.compute_canonical_vc_plan(
            body, global_override_seconds=m.MAX_PER_COMMAND_TIMEOUT_SECONDS
        )
        assert False, "expected AggregateTimeoutExceedsPolicyError"
    except m.AggregateTimeoutExceedsPolicyError as exc:
        assert exc.error_code == "aggregate_timeout_exceeds_policy"
        assert exc.aggregate_timeout_seconds > m.MAX_TOTAL_VERIFICATION_BUDGET_SECONDS


def test_policy_ceiling_allows_value_exactly_at_max():
    """Boundary: exactly MAX_PER_COMMAND_TIMEOUT_SECONDS is accepted (not
    rejected) -- the ceiling is inclusive."""
    budget = m.compute_command_timeout_budget(
        "pnpm lint", override_seconds=m.MAX_PER_COMMAND_TIMEOUT_SECONDS
    )
    assert budget["timeout_seconds"] == m.MAX_PER_COMMAND_TIMEOUT_SECONDS


def test_compute_canonical_vc_plan_rejects_before_returning_when_over_ceiling():
    """AC5: compute_canonical_vc_plan() (the canonical plan producer) also
    rejects up front -- no `command_budgets` entry, no plan, is ever
    returned for a body whose resolved per-command budget exceeds the
    ceiling."""
    body = (
        "## Verification Commands\n\n"
        "```bash\n$ pnpm build\n```\n"
    )
    too_large = m.MAX_PER_COMMAND_TIMEOUT_SECONDS + 1
    try:
        m.compute_canonical_vc_plan(body, global_override_seconds=too_large)
        assert False, "expected CommandTimeoutExceedsPolicyError"
    except m.CommandTimeoutExceedsPolicyError:
        pass


def test_main_impl_rejects_policy_violation_without_launching_subprocess(tmp_path, capsys):
    """AC5 end-to-end: `baseline_vc_preflight.py --timeout-seconds <over-ceiling>`
    emits a typed non-retryable failure JSON and exits non-zero WITHOUT
    launching the VC subprocess (proven by the marker file never being
    created)."""
    marker_path = tmp_path / "marker.txt"
    body_path = tmp_path / "body.md"
    body_path.write_text(
        "## Verification Commands\n\n"
        f"```bash\n$ touch {marker_path}\n```\n",
        encoding="utf-8",
    )

    argv_backup = sys.argv[:]
    over_ceiling = str(m.MAX_PER_COMMAND_TIMEOUT_SECONDS + 1)
    sys.argv = [
        "baseline_vc_preflight.py",
        "--body-file",
        str(body_path),
        "--timeout-seconds",
        over_ceiling,
    ]
    try:
        exit_code = m._main_impl()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code != 0
    assert payload["status"] == "blocked"
    assert payload["failure_class"] == "command_timeout_exceeds_policy"
    assert payload["retryable"] is False
    assert payload["results"] == []
    assert not marker_path.exists(), (
        "subprocess must NOT be launched when the requested per-command "
        "timeout exceeds the policy ceiling"
    )


# ---------------------------------------------------------------------------
# AC6 (`-k provenance`): effective timeout provenance on result items
# ---------------------------------------------------------------------------


def test_main_impl_result_item_carries_timeout_provenance(tmp_path, capsys):
    """AC6: each result item's `timeout_provenance` reflects THIS command's
    own budget (timeout_seconds / cleanup_tail_seconds / source /
    estimator_version / estimator_input_digest)."""
    body_path = tmp_path / "body.md"
    body_path.write_text(
        "## Verification Commands\n\n"
        "```bash\n$ test -f /nonexistent-path-for-provenance-test\n```\n",
        encoding="utf-8",
    )

    argv_backup = sys.argv[:]
    sys.argv = ["baseline_vc_preflight.py", "--body-file", str(body_path)]
    try:
        m._main_impl()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    result_item = payload["results"][0]

    provenance = result_item["timeout_provenance"]
    assert provenance["timeout_seconds"] == m.DEFAULT_TIMEOUT_SECONDS
    assert provenance["cleanup_tail_seconds"] == m.CLEANUP_TAIL_SECONDS
    assert provenance["source"] == "static_fallback"
    assert provenance["estimator_version"] == m.COMMAND_TIMEOUT_BUDGET_ESTIMATOR_VERSION
    assert provenance["estimator_input_digest"].startswith("sha256:")


def test_main_impl_result_item_provenance_reflects_explicit_override(tmp_path, capsys):
    """AC6 + AC4: when `--timeout-seconds` is explicitly passed, the
    provenance `source` is `explicit_override` and `timeout_seconds`
    matches the CLI value exactly (not the static fallback)."""
    body_path = tmp_path / "body.md"
    body_path.write_text(
        "## Verification Commands\n\n"
        "```bash\n$ test -f /nonexistent-path-for-override-provenance\n```\n",
        encoding="utf-8",
    )

    argv_backup = sys.argv[:]
    sys.argv = [
        "baseline_vc_preflight.py",
        "--body-file",
        str(body_path),
        "--timeout-seconds",
        "45",
    ]
    try:
        m._main_impl()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    provenance = payload["results"][0]["timeout_provenance"]

    assert provenance["source"] == "explicit_override"
    assert provenance["timeout_seconds"] == 45



# ---------------------------------------------------------------------------
# Issue #2254 regression: `history_snapshot=None` (the pre-#2254 default,
# and every pre-#2254 call site) preserves byte-for-byte pre-#2254
# behavior -- the `history_estimate` source is purely additive.
# ---------------------------------------------------------------------------


def test_history_snapshot_none_default_preserves_pre_2254_static_fallback():
    """`compute_command_timeout_budget()` called with no `history_snapshot`
    argument at all (the exact call shape every pre-#2254 site used)
    resolves identically to before #2254: `static_fallback`, no
    `history_estimate` ever considered."""
    budget = m.compute_command_timeout_budget("pnpm typecheck")
    assert budget["source"] == "static_fallback"
    assert budget["timeout_seconds"] == m.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS
    assert budget["sample_count"] == 0
    assert budget["observed_p95_ms"] is None


def test_history_snapshot_none_default_preserves_pre_2254_static_policy():
    """A `static_policy`-curated command with `history_snapshot=None`
    still resolves via `static_policy`, unaffected by Issue #2254's
    additive fields."""
    slow_command = "uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v"
    budget = m.compute_command_timeout_budget(slow_command)
    assert budget["source"] == "static_policy"
    assert budget["timeout_seconds"] == m.STATIC_PER_COMMAND_TIMEOUT_POLICY[slow_command]


def test_compute_canonical_vc_plan_history_snapshot_none_default_matches_pre_2254_digest():
    """`compute_canonical_vc_plan()` called without `history_snapshot`
    (positional-compatible with every pre-#2254 call site) produces a
    `command_budgets[]` list whose entries are IDENTICAL (aside from the
    new additive keys) to explicitly passing `history_snapshot=None`."""
    body = "## Verification Commands\n\n```bash\n$ pnpm lint\n```\n"
    plan_implicit = m.compute_canonical_vc_plan(body)
    plan_explicit_none = m.compute_canonical_vc_plan(body, history_snapshot=None)
    assert plan_implicit["plan_digest"] == plan_explicit_none["plan_digest"]
    assert plan_implicit["command_budgets"][0]["source"] == "static_fallback"


def test_command_timeout_budget_additive_history_fields_present_and_null_by_default():
    """The Issue #2254 additive fields exist on EVERY budget entry
    (subset-check compatible with the pre-existing AC1 field-set test
    above) and are null/false/`snapshot_absent` when no history_snapshot
    was supplied -- never fabricated."""
    budget = m.compute_command_timeout_budget("pnpm build")
    assert budget["command_group_key"] is None
    assert budget["history_store_status"] == "snapshot_absent"
    assert budget["history_backoff_applied"] is False
    assert budget["timeout_backoff_floor_seconds"] is None
