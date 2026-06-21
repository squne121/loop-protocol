#!/usr/bin/env bash
# ci_test_performance_advisory.sh (Codex CLI variant)
# PreToolUse hook — CI/test-lane related path advisory (non-blocking / fail-open)
#
# When a CI/test-lane related path is detected in the tool input, emits
# CI_TEST_PERFORMANCE_ADVISORY_V1 wrapped in hookSpecificOutput.additionalContext
# as required by the Codex CLI PreToolUse hook stdout contract.
# Always exits 0 (fail-open). Never blocks tool calls.
#
# Schema: schemas/ci_test_performance_advisory_v1.schema.json
# Outer envelope: { "hookSpecificOutput": { "hookEventName": "PreToolUse", "additionalContext": "<inner_json_string>" } }

set -euo pipefail

# Runtime is always codex_cli for this hook
RUNTIME="codex_cli"

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
  # jq not available — fail-open (no output, exit 0)
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

# Build matched_paths JSON array using jq
MATCHED_JSON="$(printf '%s\n' "${MATCHED[@]}" | jq -R . | jq -s .)"

# Build inner payload JSON string
INNER_JSON="$(jq -n \
  --arg schema "CI_TEST_PERFORMANCE_ADVISORY_V1" \
  --arg runtime "$RUNTIME" \
  --argjson block false \
  --arg reason_code "ci_related_path" \
  --argjson matched_paths "$MATCHED_JSON" \
  --arg required_skill ".claude/skills/ci-test-performance/SKILL.md" \
  --arg expected_followup_contract "CI_TEST_PERFORMANCE_DECISION_V1" \
  --arg message "CI/test-lane related path detected. Read ci-test-performance before editing, then emit CI_TEST_PERFORMANCE_DECISION_V1 as PR evidence." \
  '{
    schema: $schema,
    block: $block,
    reason_code: $reason_code,
    matched_paths: $matched_paths,
    required_skill: $required_skill,
    expected_followup_contract: $expected_followup_contract,
    message: $message
  }')"

# Emit hookSpecificOutput envelope as required by Codex CLI PreToolUse stdout contract
jq -n \
  --arg hookEventName "PreToolUse" \
  --arg additionalContext "CI_TEST_PERFORMANCE_ADVISORY_V1 ${INNER_JSON}" \
  '{
    hookSpecificOutput: {
      hookEventName: $hookEventName,
      additionalContext: $additionalContext
    }
  }'

exit 0
