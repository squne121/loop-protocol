#!/usr/bin/env python3
"""Deterministic evaluator for the ``python-test`` required aggregate (Issue #1824 P1-1).

PR #1824 review: the aggregate job's inline ``python3 - <<'PY' ... PY`` heredoc was
verified by ``scripts/ci/verify_python_test_lane.py`` only by checking that the
LITERAL STRING ``python_test_bench_aggregate_policy`` appears somewhere in the
step's ``run:`` text. Replacing the heredoc body with
``echo python_test_bench_aggregate_policy ok`` (doing NO real evaluation of
``needs.python-test-core.result``) still made that string-search verifier pass.

This module is the single source of truth for the aggregate policy: given
``core_result`` / ``bench_mode`` it decides pass/fail with NO other inputs.
``scripts/ci/verify_python_test_lane.py`` now additionally requires that the
``python-test`` job's step invokes THIS script with an EXACT, fixed argv
(not merely containing a substring) -- see ``check_aggregate_job`` /
``EXPECTED_AGGREGATE_ARGV``.

Issue #2161 (native Codex CLI retirement): ``jobs.python-test`` used to depend
on both ``python-test-core`` and ``codex-execpolicy``; the latter job (and its
``--codex-result`` argument / ``codex_result`` decision input here) was removed
along with native Codex CLI. ``python-test`` now depends on ``python-test-core``
only.
"""

from __future__ import annotations

import argparse
import sys

SCHEMA = "python_test_bench_aggregate_policy_v1"


def evaluate(*, core_result: str, bench_mode: bool) -> tuple[bool, str]:
    """Return (ok, reason).

    Issue #2161: python-test's sole dependency is now python-test-core, so
    the aggregate policy is a direct pass-through of its result. bench_mode
    is retained as an accepted parameter for call-site/CLI compatibility; it
    no longer changes the decision.
    """
    ok = core_result == "success"
    reason = f"bench_mode={bench_mode} core={core_result!r}"
    return ok, reason


def _parse_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-result", required=True, help="needs.python-test-core.result")
    parser.add_argument(
        "--bench-mode",
        required=True,
        help="github.event.inputs.python_test_bench, as a literal 'true'/'false'/'' string",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bench_mode = _parse_bool(args.bench_mode)
    ok, reason = evaluate(
        core_result=args.core_result,
        bench_mode=bench_mode,
    )
    print(f"{SCHEMA}: {reason} ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
