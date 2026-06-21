#!/usr/bin/env bash
# ci_test_performance_advisory.sh
# PreToolUse hook — CI/test-lane related path advisory (non-blocking / fail-open)
#
# When a CI/test-lane related path is detected in the tool input, emits
# CI_TEST_PERFORMANCE_ADVISORY_V1 as JSON to stdout.
# Always exits 0 (fail-open). Never blocks tool calls.
#
# Schema: schemas/ci_test_performance_advisory_v1.schema.json

set -euo pipefail

# Determine runtime (claude_code vs codex_cli)
RUNTIME="claude_code"
if [[ -n "${CODEX_ENV:-}" ]] || [[ -n "${CODEX_SANDBOX:-}" ]]; then
  RUNTIME="codex_cli"
fi

# CI/test-lane path patterns
CI_PATTERNS=(
  ".github/workflows/"
  "pyproject.toml"
  "uv.lock"
  "docs/dev/test-lane-policy.md"
  "docs/dev/ci-performance.md"
  ".claude/skills/ci-test-performance/"
  ".agents/skills/ci-test-performance/"
  ".codex/agents/"
  "schemas/"
)

# Read PreToolUse JSON from stdin
INPUT=""
if ! INPUT="$(cat)"; then
  # stdin read failure — fail-open
  exit 0
fi

# Extract file_path and command from tool_input
FILE_PATH=""
COMMAND=""

if command -v jq >/dev/null 2>&1; then
  FILE_PATH="$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")"
  COMMAND="$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null || echo "")"
else
  # jq not available — fail-open
  exit 0
fi

# Collect matched patterns
MATCHED=()
for pattern in "${CI_PATTERNS[@]}"; do
  if [[ "$FILE_PATH" == *"$pattern"* ]] || [[ "$COMMAND" == *"$pattern"* ]]; then
    MATCHED+=("$pattern")
  fi
done

# No match — exit silently
if [[ ${#MATCHED[@]} -eq 0 ]]; then
  exit 0
fi

# Build matched_paths JSON array
MATCHED_JSON="["
first=true
for m in "${MATCHED[@]}"; do
  if $first; then
    MATCHED_JSON+="\"${m}\""
    first=false
  else
    MATCHED_JSON+=",\"${m}\""
  fi
done
MATCHED_JSON+="]"

# Emit CI_TEST_PERFORMANCE_ADVISORY_V1 as advisory JSON to stdout
cat <<ADVISORY
{
  "schema": "CI_TEST_PERFORMANCE_ADVISORY_V1",
  "runtime": "${RUNTIME}",
  "event": "PreToolUse",
  "triggered": true,
  "block": false,
  "reason_code": "ci_related_path",
  "matched_paths": ${MATCHED_JSON},
  "required_skill": ".claude/skills/ci-test-performance/SKILL.md",
  "expected_followup_contract": "CI_TEST_PERFORMANCE_DECISION_V1",
  "message": "CI/test-lane related path detected. Read ci-test-performance before editing, then emit CI_TEST_PERFORMANCE_DECISION_V1 as PR evidence."
}
ADVISORY

exit 0
