#!/usr/bin/env bash
# session_recording_policy_guard.sh
#
# Fail-closed guard wrapper for session recording policy checker.
# Runs on Stop / SubagentStop hooks to detect policy-related file changes
# and trigger the checker with uv run --locked python.
#
# stdin: JSON hook context from Claude hook system
# exit codes:
#   0 - no changes to watched files or checker pass
#   2 - repo error or checker failure
#
set -euo pipefail

# Watched file paths that require policy verification
readonly WATCHED_PATHS=(
	"docs/dev/session-recording-policy.md"
	"docs/dev/secret-policy.md"
	"docs/schemas/agent-session-manifest.schema.json"
	".claude/scripts/check_session_recording_policy.py"
	".claude/hooks/session_recording_policy_guard.sh"
	".claude/settings.json"
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

die() {
	local message="$1"
	echo "SESSION_RECORDING_POLICY_GUARD: FAILED" >&2
	echo "Reason: $message" >&2
	exit 2
}

# Resolve repo root from prioritized sources
# Priority: \$CLAUDE_PROJECT_DIR -> stdin cwd -> pwd
resolve_repo_root() {
	local repo_root=""

	# Try CLAUDE_PROJECT_DIR first
	if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]] && [[ -d "${CLAUDE_PROJECT_DIR}" ]]; then
		repo_root="${CLAUDE_PROJECT_DIR}"
	else
		# Try stdin cwd (read from file we created in main)
		if [[ -f "${_STDIN_CWD_FILE:-}" ]] && [[ -s "${_STDIN_CWD_FILE}" ]]; then
			repo_root=$(cat "${_STDIN_CWD_FILE}")
		fi

		# Fall back to pwd
		if [[ -z "$repo_root" ]] || [[ ! -d "$repo_root" ]]; then
			repo_root="$(pwd)"
		fi
	fi

	# Verify it's a git repo by running git rev-parse --show-toplevel
	if ! repo_root=$(cd "$repo_root" && git rev-parse --show-toplevel 2>/dev/null); then
		die "Not a git repository or git command failed: $repo_root"
	fi

	echo "$repo_root"
}

# Detect watched file changes in git repo
detect_changes() {
	local repo_root="$1"
	local -a changed_files=()

	# Check tracked changes
	while IFS= read -r file; do
		[[ -n "$file" ]] && changed_files+=("$file")
	done < <(git -C "$repo_root" diff --name-only --diff-filter=ACDMRT HEAD -- "${WATCHED_PATHS[@]}" 2>/dev/null || true)

	# Check untracked changes
	while IFS= read -r file; do
		[[ -n "$file" ]] && changed_files+=("$file")
	done < <(git -C "$repo_root" ls-files --others --exclude-standard -- "${WATCHED_PATHS[@]}" 2>/dev/null || true)

	# Return as space-separated list (allows empty string if no changes)
	printf '%s\n' "${changed_files[@]}" || true
}

# Run the policy checker
run_checker() {
	local repo_root="$1"

	if ! (cd "$repo_root" && uv run --locked python .claude/scripts/check_session_recording_policy.py docs/dev/session-recording-policy.md); then
		return 1
	fi
	return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Create a temporary directory for stdin files (cleaned up on exit)
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

_STDIN_TEMP_FILE="$tmpdir/stdin.json"
_STDIN_CWD_FILE="$tmpdir/cwd.txt"

cat > "$_STDIN_TEMP_FILE"

# Extract hook context from JSON
python3 -c "
import json, sys
try:
    stdin_data = json.load(open('$_STDIN_TEMP_FILE'))
    cwd = stdin_data.get('cwd', '')
    if cwd:
        with open('${_STDIN_CWD_FILE}', 'w') as f:
            f.write(cwd)
except:
    pass
" 2>/dev/null || true

# Check for stop_hook_active flag (8-time override mechanism)
# If stop_hook_active is true, short-circuit to avoid infinite blocking
readonly _STOP_HOOK_ACTIVE=$(python3 -c "
import json, sys
try:
    data = json.load(open('$_STDIN_TEMP_FILE'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
	exit 0
fi

# Resolve repo root
repo_root=$(resolve_repo_root) || die "Failed to resolve repository root"

# Detect changes to watched files
mapfile -t changed_files < <(detect_changes "$repo_root")

# If no changes, exit cleanly
if [[ ${#changed_files[@]} -eq 0 ]]; then
	exit 0
fi

# Changes detected — run checker
if ! run_checker "$repo_root"; then
	echo "SESSION_RECORDING_POLICY_GUARD: BLOCKED by policy checker failure" >&2
	echo "Changed files:" >&2
	printf '  - %s\n' "${changed_files[@]}" >&2
	echo "" >&2
	echo "Rerun checker with:" >&2
	echo "  (cd '$repo_root' && uv run --locked python .claude/scripts/check_session_recording_policy.py docs/dev/session-recording-policy.md)" >&2
	exit 2
fi

exit 0
