"""
tests/test_history_snapshot_production_wiring.py

Issue #2254 AC9 (runtime-verification: true): Production wiring.

Positive case: a REAL command is launched (`baseline_vc_preflight.py`'s
own production `_main_impl()`, invoked exactly as the CLI/subprocess
caller would -- NOT a hand-rolled reimplementation), which records a
genuine execution-duration sample into an isolated, `tmp_path`-scoped
SQLite history store (via `VC_RUNTIME_HISTORY_STORE_PATH`). After enough
samples exist to satisfy `nearest_rank_v1`'s minimum-sample-count
contract, a SUBSEQUENT invocation of the SAME production
`compute_canonical_vc_plan()` entrypoint -- fed the history snapshot that
invocation's own root-owned producer built from that SAME store --
resolves a per-command budget that has genuinely CHANGED (raised) because
of the recorded history, proving the wiring is real end-to-end
production wiring, not merely a unit-tested pure function.

Negative case (same module, Issue #2254 AC9 requirement): a cold-start
history store (missing entirely) degrades non-blocking to
`static_policy`/`static_fallback` -- the production
`produce_immutable_history_snapshot()` -> `compute_canonical_vc_plan()`
path never raises and never silently invents evidence.

Runtime Verification Applicability: immediate (Issue #2254
`## Runtime Verification Applicability`). Both a real subprocess launch
(via `baseline_vc_preflight.py`'s own `_main_impl()`/CLI entrypoint) and
real SQLite file I/O are exercised in a local temp directory; no network,
no external service. SKIP (exit 77 equivalent: `pytest.skip()`) applies
ONLY if `sqlite3` or `subprocess` are unavailable in the executing
Python -- both are Python stdlib and expected to always be present in
this repository's supported environments, so the skip path exists for
policy completeness (`docs/dev/runtime-verification-policy.md`) but is
not expected to trigger in CI/dev.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_BASELINE_VC_PREFLIGHT_PY = _SCRIPTS_DIR / "baseline_vc_preflight.py"

sys.path.insert(0, str(_SCRIPTS_DIR))

import baseline_vc_preflight as m  # noqa: E402
import vc_runtime_history as h  # noqa: E402


def _skip_if_environment_unavailable() -> None:
    try:
        import sqlite3 as _sqlite3  # noqa: F401
    except ImportError:
        pytest.skip("SKIP: sqlite3 unavailable in this Python -- cannot run AC9 production wiring VC.")
    if not shutil_which_subprocess_capable():
        pytest.skip("SKIP: subprocess launch unavailable in this environment.")


def shutil_which_subprocess_capable() -> bool:
    try:
        subprocess.run([sys.executable, "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


# A single, real, fast, PREFLIGHT-ALLOWED command (baseline_vc_preflight.py's
# static classifier only permits `python3 -m py_compile <file>` /
# `python3 -m pytest ...` for a bare `python3` invocation -- `python3 -c`
# inline code is explicitly blocked as unsafe_command). No static_policy
# entry exists for this exact text, so its budget resolves via
# static_fallback (150s) absent history evidence.
_COMPILE_TARGET = str(_SCRIPTS_DIR / "vc_runtime_history.py")
_REAL_COMMAND = f"python3 -m py_compile {_COMPILE_TARGET}"
_BODY = f"""## Verification Commands

```bash
# AC1
$ {_REAL_COMMAND}
```
"""


def _run_baseline_vc_preflight_subprocess(body_file: Path, cwd: Path, store_path: Path) -> dict:
    env = dict(os.environ)
    env["VC_RUNTIME_HISTORY_STORE_PATH"] = str(store_path)
    result = subprocess.run(
        [
            sys.executable,
            str(_BASELINE_VC_PREFLIGHT_PY),
            "--body-file",
            str(body_file),
            "--cwd",
            str(cwd),
        ],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=60,
    )
    assert result.stdout, f"no stdout from baseline_vc_preflight.py; stderr={result.stderr}"
    return json.loads(result.stdout)


def test_production_wiring_real_launch_then_next_invocation_budget_changes(tmp_path, monkeypatch):
    """AC9 positive: one REAL subprocess launch (via the actual
    `baseline_vc_preflight.py` production entrypoint) records a genuine
    sample; after enough real samples accumulate, a SECOND REAL subprocess
    launch of the SAME production CLI entrypoint -- under NORMAL production
    defaults (no `--timeout-seconds` override, no artificially-lowered
    `per_command_timeout_seconds`) -- resolves a per-command budget that has
    genuinely CHANGED (raised, to `source: history_estimate`) because of
    that recorded history, proving the wiring is real end-to-end production
    wiring under REALISTIC defaults, not merely a unit-tested pure function
    exercised with a synthetic low static base.

    Issue #2254 fix_delta P0 blocker 8 (OWNER REQUEST_CHANGES
    https://github.com/squne121/loop-protocol/pull/2382#issuecomment-5458281756):
    the pre-fix_delta version of this test seeded `duration_ms=60000`
    samples and then called `compute_canonical_vc_plan()` DIRECTLY with a
    deliberately-low `per_command_timeout_seconds=1`, which only proved the
    helper function COULD select `history_estimate` against an artificial
    1s static base -- never that the SAME candidate would win against the
    REAL production static_fallback (150s). This version seeds
    `duration_ms=120000` samples (candidate
    `ceil(120000*1.5/1000)=180s`, which STRICTLY exceeds the normal 150s
    static_fallback base) and observes the SECOND real subprocess launch's
    OWN JSON output under fully normal defaults."""
    _skip_if_environment_unavailable()

    repo_root = _SCRIPTS_DIR.parents[3]  # cwd must be inside a real git repo
    store_path = tmp_path / "prod-wiring-store.sqlite3"
    body_file = tmp_path / "body.md"
    body_file.write_text(_BODY, encoding="utf-8")

    monkeypatch.setenv("VC_RUNTIME_HISTORY_STORE_PATH", str(store_path))

    # REAL launch #1: this IS "実 command を1回起動し" (AC9) -- the actual
    # baseline_vc_preflight.py subprocess, not a mock. Under normal
    # production defaults (no history yet), this resolves via
    # static_fallback (150s).
    result_1 = _run_baseline_vc_preflight_subprocess(body_file, repo_root, store_path)
    assert result_1["results"], result_1
    budget_1 = result_1["results"][0]["timeout_provenance"]
    assert budget_1["source"] == "static_fallback", budget_1
    assert budget_1["timeout_seconds"] == m.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS, budget_1

    # Confirm the write path actually persisted a real sample (production
    # wiring, not a no-op).
    conn_check = __import__("sqlite3").connect(str(store_path))
    count_after_one = conn_check.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn_check.close()
    assert count_after_one == 1

    # Seed 4 MORE samples directly via the SAME production
    # `vc_runtime_history.record_sample()` function (not a reimplementation)
    # using the SAME repo_root-normalized identity the real launch above
    # resolves internally (Issue #2254 fix_delta P0 blocker 2/3:
    # `baseline_vc_preflight.py`'s own executor resolves `repo_root` via
    # `resolve_repo_root_for_history(args.cwd)` -- mirrored here exactly so
    # these seeded samples land in the SAME bucket the real launches read
    # from/write to), so the `nearest_rank_v1` minimum-sample-count (5) is
    # met without needing 5 separate real subprocess round-trips (the FIRST
    # sample above is what makes this genuinely "one real launch -> one
    # recorded sample -> next invocation sees it").
    resolved_repo_root = m.resolve_repo_root_for_history(str(repo_root))
    group_key = h.compute_command_group_key(
        _REAL_COMMAND, str(repo_root), repo_root=resolved_repo_root
    )
    fingerprint = h.compute_environment_fingerprint(m._command_family(_REAL_COMMAND))
    for i in range(4):
        outcome = h.record_sample(
            store_path,
            execution_id=h.new_execution_id(),
            command_group_key=group_key,
            environment_fingerprint=fingerprint,
            status="success",
            # 120s -> ceil(120000*1.5/1000) = 180s candidate, which
            # STRICTLY exceeds the NORMAL production static_fallback base
            # (DEFAULT_PER_COMMAND_TIMEOUT_SECONDS=150s) -- demonstrating
            # the budget change under REALISTIC defaults (Issue #2254
            # fix_delta P0 blocker 8), not an artificially low
            # per_command_timeout_seconds override.
            command_hash=m.compute_command_hash(_REAL_COMMAND),
            duration_ms=120000,
            applied_timeout_ms=150000,
        )
        assert outcome["recorded"], outcome

    # REAL launch #2: the SAME production baseline_vc_preflight.py CLI
    # entrypoint, under FULLY NORMAL production defaults (no
    # --timeout-seconds override at all) -- proving the budget genuinely
    # CHANGES between invocations because of the recorded history, under
    # the SAME defaults a real caller uses in production.
    result_2 = _run_baseline_vc_preflight_subprocess(body_file, repo_root, store_path)
    assert result_2["results"], result_2
    budget_2 = result_2["results"][0]["timeout_provenance"]
    assert budget_2["source"] == "history_estimate", budget_2
    assert budget_2["timeout_seconds"] == 180, budget_2
    assert budget_2["timeout_seconds"] != budget_1["timeout_seconds"]


def test_production_wiring_cold_start_missing_store_degrades_non_blocking(tmp_path, monkeypatch):
    """AC9 negative (異常系, same module): a store that has never been
    written to at all (genuine cold start -- no file exists) degrades the
    production snapshot producer to `store_status: missing` and the
    canonical plan resolves via `static_fallback`, never raising and never
    fabricating history evidence."""
    _skip_if_environment_unavailable()

    repo_root = _SCRIPTS_DIR.parents[3]
    never_written_store = tmp_path / "never-written.sqlite3"
    assert not never_written_store.exists()
    monkeypatch.setenv("VC_RUNTIME_HISTORY_STORE_PATH", str(never_written_store))

    snapshot = m.produce_immutable_history_snapshot(_BODY, cwd=str(repo_root))
    assert snapshot["store_status"] == "missing"
    assert snapshot["records"] == {}

    plan = m.compute_canonical_vc_plan(_BODY, cwd=str(repo_root), history_snapshot=snapshot)
    budget = plan["command_budgets"][0]
    assert budget["source"] == "static_fallback"
    assert budget["timeout_seconds"] == m.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS

    # And the REAL subprocess entrypoint (--no-history-estimator NOT
    # passed) also completes normally against this never-written store --
    # no crash, no hang -- proving the degrade-to-fallback path is wired
    # into the actual CLI, not just the in-process function.
    body_file = tmp_path / "body_cold_start.md"
    body_file.write_text(_BODY, encoding="utf-8")
    result = _run_baseline_vc_preflight_subprocess(body_file, repo_root, never_written_store)
    assert result["results"], result
    assert result["results"][0]["timeout_provenance"]["source"] == "static_fallback"
