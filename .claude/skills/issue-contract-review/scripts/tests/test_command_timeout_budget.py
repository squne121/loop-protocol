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
        "execution_key_hash",
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


def test_policy_ceiling_rejects_history_estimate_exceeding_max():
    """AC5: a (future) history-based estimate exceeding the ceiling is
    rejected through the SAME enforcement path as an explicit override --
    a single hard ceiling applies regardless of source."""
    too_large = m.MAX_PER_COMMAND_TIMEOUT_SECONDS + 500
    try:
        m.compute_command_timeout_budget("pnpm build", estimated_seconds=too_large)
        assert False, "expected CommandTimeoutExceedsPolicyError"
    except m.CommandTimeoutExceedsPolicyError as exc:
        assert exc.error_code == "command_timeout_exceeds_policy"


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
