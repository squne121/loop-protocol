"""
Unit tests for explicit `--timeout-seconds` override precedence
(Issue #2233 AC4).

AC4: 既存の `--timeout-seconds` オプションが hard global override として
     command-level budget より優先されることがテストで検証されている。

Runtime Verification Applicability: not_applicable
side-effect-free unit tests over `compute_command_timeout_budget()` /
`compute_canonical_vc_plan()`, plus one end-to-end `_main_impl()` case that
exercises real (fast, `test -f`) subprocess execution to prove the override
actually reaches the executor, not merely the plan.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import baseline_vc_preflight as m  # noqa: E402


def test_explicit_override_wins_over_static_fallback_default():
    budget = m.compute_command_timeout_budget(
        "pnpm lint", override_seconds=45, default_seconds=150
    )
    assert budget["source"] == "explicit_override"
    assert budget["timeout_seconds"] == 45


def test_explicit_override_wins_even_when_estimated_seconds_also_supplied():
    """AC4: precedence is `override_seconds` > `estimated_seconds` >
    `default_seconds`. A caller supplying BOTH override and estimate must
    resolve to the override (the estimate is silently superseded, not
    merged/averaged)."""
    budget = m.compute_command_timeout_budget(
        "pnpm lint", override_seconds=45, estimated_seconds=120, default_seconds=150
    )
    assert budget["source"] == "explicit_override"
    assert budget["timeout_seconds"] == 45


def test_compute_canonical_vc_plan_global_override_applies_to_every_command():
    """AC4: `compute_canonical_vc_plan(..., global_override_seconds=N)`
    applies N to EVERY distinct command in the plan, not just the first."""
    body = (
        "## Verification Commands\n\n"
        "```bash\n$ pnpm typecheck\n```\n\n"
        "```bash\n$ pnpm lint\n```\n\n"
        "```bash\n$ pnpm build\n```\n"
    )
    plan = m.compute_canonical_vc_plan(body, global_override_seconds=50)
    assert len(plan["command_budgets"]) == 3
    for budget in plan["command_budgets"]:
        assert budget["source"] == "explicit_override"
        assert budget["timeout_seconds"] == 50


def test_no_override_resolves_to_static_fallback():
    """Sanity: when `global_override_seconds` is None (default), the plan
    falls back to `static_fallback`, never claiming a bogus override."""
    body = "## Verification Commands\n\n```bash\n$ pnpm lint\n```\n"
    plan = m.compute_canonical_vc_plan(body)
    assert plan["command_budgets"][0]["source"] == "static_fallback"


def test_main_impl_explicit_cli_override_reaches_executor(tmp_path, capsys):
    """AC4 end-to-end: `--timeout-seconds` explicitly passed on the CLI is
    a HARD GLOBAL override -- its value (not `DEFAULT_TIMEOUT_SECONDS`)
    reaches the actual subprocess execution path, verified via the
    provenance recorded on the executed result item."""
    body_path = tmp_path / "body.md"
    body_path.write_text(
        "## Verification Commands\n\n"
        "```bash\n$ test -f /nonexistent-path-for-override-precedence-test\n```\n",
        encoding="utf-8",
    )

    argv_backup = sys.argv[:]
    sys.argv = [
        "baseline_vc_preflight.py",
        "--body-file",
        str(body_path),
        "--timeout-seconds",
        "77",
    ]
    try:
        exit_code = m._main_impl()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    provenance = payload["results"][0]["timeout_provenance"]

    assert exit_code == 0  # `test -f <missing>` is a clean expected_fail
    assert provenance["source"] == "explicit_override"
    assert provenance["timeout_seconds"] == 77
    assert provenance["timeout_seconds"] != m.DEFAULT_TIMEOUT_SECONDS


def test_main_impl_without_explicit_flag_defaults_to_static_fallback(tmp_path, capsys):
    """Negative control for AC4: when `--timeout-seconds` is NOT passed at
    all, the resolved source must be `static_fallback`, not a spuriously
    claimed `explicit_override` (proves the sys.argv-presence detection is
    accurate, not always-true)."""
    body_path = tmp_path / "body.md"
    body_path.write_text(
        "## Verification Commands\n\n"
        "```bash\n$ test -f /nonexistent-path-for-no-override-test\n```\n",
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
    provenance = payload["results"][0]["timeout_provenance"]

    assert provenance["source"] == "static_fallback"
    assert provenance["timeout_seconds"] == m.DEFAULT_TIMEOUT_SECONDS
