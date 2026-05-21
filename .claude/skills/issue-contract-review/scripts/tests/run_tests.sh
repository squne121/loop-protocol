#!/usr/bin/env bash
# run_tests.sh — check_blockers.sh の deterministic テストランナー
# Usage: bash scripts/tests/run_tests.sh
#   (カレントディレクトリは .claude/skills/issue-contract-review/ であること)
#
# または絶対パスから:
#   bash /path/to/scripts/tests/run_tests.sh

set -uo pipefail

# このスクリプトのディレクトリを取得
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"
CHECK_BLOCKERS="$SCRIPTS_DIR/check_blockers.sh"

PASS=0
FAIL=0

run_test() {
  local name="$1"
  local fake_gh="$2"
  local expected_exit="$3"
  local issue_number="${4:-100}"
  local repo="${5:-owner/repo}"

  local actual_exit=0
  GH_BIN="$fake_gh" bash "$CHECK_BLOCKERS" "$issue_number" "$repo" >/dev/null 2>&1 || actual_exit=$?

  if [[ "$actual_exit" -eq "$expected_exit" ]]; then
    echo "PASS: $name (exit=$actual_exit, expected=$expected_exit)"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name (exit=$actual_exit, expected=$expected_exit)"
    # 詳細を表示
    GH_BIN="$fake_gh" bash "$CHECK_BLOCKERS" "$issue_number" "$repo" 2>&1 || true
    FAIL=$((FAIL + 1))
  fi
}

echo "=== check_blockers.sh tests ==="
echo ""

# テストケース 1: native blocker が open → exit 非 0
run_test \
  "TC1: native blocker open → exit non-0" \
  "$FIXTURES_DIR/fake-gh-open-blocker" \
  1

# テストケース 2: native blocker が全 closed → exit 0
run_test \
  "TC2: native blocker all closed → exit 0" \
  "$FIXTURES_DIR/fake-gh-all-closed" \
  0

# テストケース 3: native/fallback mismatch → exit 非 0
run_test \
  "TC3: native/fallback mismatch → exit non-0" \
  "$FIXTURES_DIR/fake-gh-mismatch" \
  1

# テストケース 4: native API unavailable + fallback なし → exit 非 0
run_test \
  "TC4: native API unavailable + no fallback → exit non-0" \
  "$FIXTURES_DIR/fake-gh-native-unavail" \
  1

echo ""
echo "=== Results: PASS=$PASS FAIL=$FAIL ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
