#!/usr/bin/env bash
# verify_run_retrospective_live_cli.sh -- SKIP(77)/FAIL(1)/PASS(0) wrapper
# around test_run_retrospective_live_cli.py (Issue #2301, AC2/AC5/AC6/AC7).
#
# SKIP (exit 77) is declared ONLY for the two documented skip_conditions
# (docs/dev/runtime-verification-policy.md / the live Issue's
# `## Runtime Verification Applicability` block):
#   1. `claude` binary not in PATH
#   2. `claude auth status` exits non-zero (pre-invocation auth unavailable)
#
# Once both preflight checks pass, this script hands off to pytest. From
# that point on, ANY failure (pytest non-zero exit, individual test
# assertion failure, real CLI invocation failure) is a real FAIL (exit 1) --
# never converted to a SKIP. SKIP never promotes to PASS.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"
TEST_FILE="$SCRIPT_DIR/test_run_retrospective_live_cli.py"

TESTED_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)"
echo "verify_run_retrospective_live_cli.sh: tested HEAD: $TESTED_HEAD"

SELECT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --select)
      if [ -z "${2:-}" ] || [ "${2#--}" != "$2" ]; then
        echo "verify_run_retrospective_live_cli.sh: --select requires a non-empty value" >&2
        exit 2
      fi
      SELECT="$2"
      shift 2
      ;;
    *)
      echo "verify_run_retrospective_live_cli.sh: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# --- skip_condition 1: claude binary not in PATH -----------------------
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
if [ -z "$CLAUDE_BIN" ]; then
  echo "SKIP: claude binary not found in PATH (skip_condition: claude binary not in PATH)"
  exit 77
fi

# --- skip_condition 2: claude auth status exit != 0 ---------------------
if ! claude auth status >/dev/null 2>&1; then
  echo "SKIP: claude auth status check failed (skip_condition: pre-invocation auth unavailability)"
  exit 77
fi

# --- from here on: any failure is a real FAIL, not a SKIP ---------------
PYTEST_ARGS=(-o "addopts=" -m claude_live -q "$TEST_FILE")
if [ -n "$SELECT" ]; then
  PYTEST_ARGS+=(-k "$SELECT")
fi

echo "verify_run_retrospective_live_cli.sh: resolved claude binary: $CLAUDE_BIN"
CLAUDE_VERSION="$(claude --version 2>&1)"
echo "verify_run_retrospective_live_cli.sh: claude --version: $CLAUDE_VERSION"

if ! (cd "$REPO_ROOT" && uv run --locked pytest "${PYTEST_ARGS[@]}"); then
  echo "FAIL: live CLI verification failed (see pytest output above)"
  exit 1
fi

echo "PASS: live CLI verification succeeded"
exit 0
