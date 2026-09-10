#!/usr/bin/env python3
"""Read-only diagnostic for Issue #2611's scope-rollup child-gh auth boundary.

The deterministic regression is the implementation authority.  This command is
only a supplemental live canary and intentionally emits and persists only
nonsecret classification/provenance summaries.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import run_scope_rollup_preflight as rsrp  # noqa: E402

SCHEMA = "SCOPE_ROLLUP_AUTH_CAPABILITY_RUNTIME_V1"
TRUSTED_REPO = "squne121/loop-protocol"
TRUSTED_HOST = "github.com"
ARTIFACT_NAME = "scope_rollup_auth_capability_runtime.json"


def _result(
    verdict: str,
    *,
    carrier_source: str | None,
    carrier_path_match: bool | None,
    failure_class: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "repository": TRUSTED_REPO,
        "host": TRUSTED_HOST,
        "carrier_source": carrier_source,
        "carrier_path_match": carrier_path_match,
        "execution_path": "direct_graphql",
        "fallback": False,
        "failure_class": failure_class,
    }


def _write_artifact(result: dict[str, Any]) -> None:
    """Persist the nonsecret summary under the invocation worktree only."""
    worktree = Path.cwd().resolve()
    artifact_dir = worktree / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = artifact_dir.resolve()
    if resolved_dir != worktree and worktree not in resolved_dir.parents:
        raise OSError("artifact directory is outside invocation worktree")
    (resolved_dir / ARTIFACT_NAME).write_text(
        json.dumps(result, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8"
    )


def _emit(result: dict[str, Any]) -> int:
    try:
        _write_artifact(result)
    except OSError:
        # Do not disclose an absolute path or exception text in the diagnostic.
        result = _result(
            "FAIL",
            carrier_source=result["carrier_source"],
            carrier_path_match=result["carrier_path_match"],
            failure_class="artifact_write_failed",
        )
    sys.stdout.write(json.dumps(result, ensure_ascii=True, sort_keys=True) + "\n")
    return 0 if result["verdict"] == "PASS" else (77 if result["verdict"] == "UNAVAILABLE/SKIP" else 1)


def _is_auth_failure(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in ("authentication", "not logged", "bad credentials", "token is invalid")
    )


def _parent_auth_available(gh_bin: str) -> bool:
    """Check only parent capability; deliberately discard raw child output."""
    try:
        completed = subprocess.run(
            [gh_bin, "auth", "status", "--hostname", TRUSTED_HOST],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.repo != TRUSTED_REPO or args.issue_number <= 0:
        return _emit(_result("FAIL", carrier_source=None, carrier_path_match=None, failure_class="invalid_target"))

    # The canary must not infer an XDG/HOME fallback.  Without a caller-selected
    # carrier it cannot test the exact boundary and is diagnostic-only unavailable.
    selected_config = os.environ.get("GH_CONFIG_DIR")
    if not selected_config:
        return _emit(
            _result(
                "UNAVAILABLE/SKIP",
                carrier_source=None,
                carrier_path_match=None,
                failure_class="config_carrier_unavailable",
            )
        )

    try:
        gh_bin = rsrp._resolve_trusted_gh_binary(str(_HERE.parent.parent))
    except rsrp.ScopeRollupPreflightError:
        return _emit(
            _result(
                "UNAVAILABLE/SKIP",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class="parent_capability_unavailable",
            )
        )

    if not _parent_auth_available(gh_bin):
        return _emit(
            _result(
                "UNAVAILABLE/SKIP",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class="parent_capability_unavailable",
            )
        )

    issue_args = [
        "issue",
        "view",
        str(args.issue_number),
        "--repo",
        args.repo,
        "--json",
        "number",
    ]
    issue_rc, issue_out, issue_err = rsrp._run_gh(gh_bin, issue_args)
    if issue_rc != 0:
        return _emit(
            _result(
                "FAIL",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class="authentication_failure" if _is_auth_failure(issue_err) else "issue_resource_failure",
            )
        )
    if _parse_json_object(issue_out) is None:
        return _emit(
            _result(
                "FAIL",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class="issue_resource_failure",
            )
        )

    graphql_rc, graphql_out, graphql_err = rsrp._run_gh(
        gh_bin,
        ["api", "graphql", "-f", "query=query { viewer { login } }"],
        timeout=rsrp.GRAPHQL_TIMEOUT_SECONDS,
    )
    if graphql_rc != 0:
        return _emit(
            _result(
                "FAIL",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class=("authentication_failure" if _is_auth_failure(graphql_err) else "graphql_resource_failure"),
            )
        )
    graphql_data = _parse_json_object(graphql_out)
    if graphql_data is None:
        return _emit(
            _result(
                "FAIL",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class="graphql_resource_failure",
            )
        )
    if graphql_data.get("errors"):
        return _emit(
            _result(
                "FAIL",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class="graphql_partial_error",
            )
        )
    viewer = graphql_data.get("data", {}).get("viewer") if isinstance(graphql_data.get("data"), dict) else None
    if not isinstance(viewer, dict) or not isinstance(viewer.get("login"), str):
        return _emit(
            _result(
                "FAIL",
                carrier_source="gh_config_dir",
                carrier_path_match=None,
                failure_class="graphql_resource_failure",
            )
        )

    # Both actual child responses were authenticated through _run_gh.  No path
    # value is emitted; equality is proven only inside the hermetic regression.
    return _emit(
        _result(
            "PASS",
            carrier_source="gh_config_dir",
            carrier_path_match=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
