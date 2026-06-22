#!/usr/bin/env python3
"""verify_scope_rollup_result.py

Thin wrapper around issue-refinement-loop's verify_scope_rollup_result.py so
impl-review-loop can call it through a single canonical path.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Tuple


def _load_issue_refinement_verifier():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "issue-refinement-loop"
        / "scripts"
        / "verify_scope_rollup_result.py"
    )
    spec = importlib.util.spec_from_file_location(
        "issue_refinement_verify_scope_rollup_result",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(result_json_path: str) -> Tuple[str, int]:
    verifier = _load_issue_refinement_verifier()
    return verifier.verify(result_json_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Verify ISSUE_SCOPE_ROLLUP_RESULT_V1 payload JSON by delegating to "
            "issue-refinement-loop verifier."
        )
    )
    parser.add_argument(
        "--result-json",
        required=True,
        help="Path to ISSUE_SCOPE_ROLLUP_PLAN_V2 JSON file to verify.",
    )
    args = parser.parse_args()

    output, exit_code = verify(args.result_json)
    print(output)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
