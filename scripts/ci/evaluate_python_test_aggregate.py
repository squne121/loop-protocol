#!/usr/bin/env python3
"""Deterministic evaluator for the ``python-test`` required aggregate (Issue #1824 P1-1).

PR #1824 review: the aggregate job's inline ``python3 - <<'PY' ... PY`` heredoc was
verified by ``scripts/ci/verify_python_test_lane.py`` only by checking that the
LITERAL STRING ``python_test_bench_aggregate_policy`` appears somewhere in the
step's ``run:`` text. Replacing the heredoc body with
``echo python_test_bench_aggregate_policy ok`` (doing NO real evaluation of
``needs.python-test-core.result`` / ``needs.codex-execpolicy.result``) still made
that string-search verifier pass.

This module is the single source of truth for the aggregate policy: given
``core_result`` / ``codex_result`` / ``bench_mode`` it decides pass/fail with NO
other inputs. ``scripts/ci/verify_python_test_lane.py`` now additionally requires
that the ``python-test`` job's step invokes THIS script with an EXACT, fixed argv
(not merely containing a substring) -- see ``check_aggregate_job`` /
``EXPECTED_AGGREGATE_ARGV``.
"""

from __future__ import annotations

import argparse
import sys

SCHEMA = "python_test_bench_aggregate_policy_v1"


def evaluate(*, core_result: str, codex_result: str, bench_mode: bool) -> tuple[bool, str]:
    """Return (ok, reason).

    bench_mode (python_test_bench workflow_dispatch): codex-execpolicy is
    intentionally skipped by its own `if:` condition (the dispatch input's
    documented contract is "run ONLY the python-test-core job"), so a
    codex_result of "skipped" is expected and must NOT fail the aggregate.
    Outside bench mode, BOTH dependency jobs must succeed.
    """
    if bench_mode:
        ok = core_result == "success"
        if codex_result != "skipped":
            reason = (
                f"bench_mode=True core={core_result!r} codex={codex_result!r} "
                "(expected codex-execpolicy result 'skipped' during python_test_bench dispatch)"
            )
        else:
            reason = f"bench_mode=True core={core_result!r} codex={codex_result!r}"
    else:
        ok = core_result == "success" and codex_result == "success"
        reason = f"bench_mode=False core={core_result!r} codex={codex_result!r}"
    return ok, reason


def _parse_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-result", required=True, help="needs.python-test-core.result")
    parser.add_argument("--codex-result", required=True, help="needs.codex-execpolicy.result")
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
        codex_result=args.codex_result,
        bench_mode=bench_mode,
    )
    print(f"{SCHEMA}: {reason} ok={ok}")
    if codex_warning_needed(args.codex_result, bench_mode):
        print(
            f"::warning::codex-execpolicy result={args.codex_result!r} during "
            "python_test_bench dispatch (expected 'skipped')"
        )
    return 0 if ok else 1


def codex_warning_needed(codex_result: str, bench_mode: bool) -> bool:
    return bench_mode and codex_result != "skipped"


if __name__ == "__main__":
    sys.exit(main())
