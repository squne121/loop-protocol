#!/usr/bin/env bash
# secret_boundary_guard.sh — PreToolUse hook that blocks high-risk commands and sensitive path access.
#
# Exit codes:
#   0  — allow (not a blocked pattern)
#   2  — block (high-risk command or sensitive path detected)
#
# This script MUST NOT echo secret-like values, env dumps, or file content to stderr.
# Only a minimal structural message is emitted on block.

set -euo pipefail

# Read stdin JSON (PreToolUse hook payload)
INPUT=$(cat)

# Extract tool name and relevant parameter value.
# We use jq to parse; if jq is unavailable, fail closed (exit 2).
if ! command -v jq >/dev/null 2>&1; then
    echo "[secret_boundary_guard] jq not found — fail closed" >&2
    exit 2
fi

TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
# command input for Bash tool
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)
# path input for Read/Write/Edit/Grep/Glob tools
PATH_INPUT=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.path // .tool_input.pattern // empty' 2>/dev/null || true)

# ── Malformed / empty payload guard ──────────────────────────────────────────
# If TOOL_NAME is empty, stdin was not valid PreToolUse JSON — block (fail closed).
if [ -z "$TOOL_NAME" ]; then
    echo "[secret_boundary_guard] malformed stdin: tool_name missing — fail closed" >&2
    exit 2
fi

# ── High-risk Bash command patterns ──────────────────────────────────────────
# Block commands that could dump secrets or environment variables.
# We match against a fixed list of patterns.
# IMPORTANT: do NOT echo COMMAND or PATH_INPUT in stderr output.

if [ "$TOOL_NAME" = "Bash" ] && [ -n "$COMMAND" ]; then
    BLOCKED=0
    # cat .env / .env.* files
    if echo "$COMMAND" | grep -qE '\bcat\b[^|]*\.env(\.[^ ]*)?(\s|$)'; then
        BLOCKED=1
    fi
    # printenv — dumps all environment variables
    if echo "$COMMAND" | grep -qE '\bprintenv\b'; then
        BLOCKED=1
    fi
    # env — without arguments dumps all env
    # Use case-based POSIX matching to avoid grep -P (non-portable on macOS)
    case "$COMMAND" in
        env|"env "|"env	"*)
            BLOCKED=1 ;;
        *"|env"|*"|env "|*"&&env"|*"&&env "|*";env"|*";env ")
            BLOCKED=1 ;;
        *"||env"|*"||env ")
            BLOCKED=1 ;;
    esac
    # Also match env as first token or after shell separator via ERE
    if echo "$COMMAND" | grep -qE '(^|[|;&]|&&|\|\|)\s*env(\s|$)'; then
        BLOCKED=1
    fi
    # export -p — dumps all exported variables
    if echo "$COMMAND" | grep -qE '\bexport\s+-p\b'; then
        BLOCKED=1
    fi
    # gh secret list / gh secret view — lists/views GitHub secrets
    if echo "$COMMAND" | grep -qE '\bgh\s+secret\b'; then
        BLOCKED=1
    fi
    # set (shell builtin to dump all vars/functions)
    if echo "$COMMAND" | grep -qE '(^|[|;&]|&&|\|\|)\s*set(\s|$)'; then
        BLOCKED=1
    fi
    # python/node commands that dump process.env or os.environ
    if echo "$COMMAND" | grep -qE "python[0-9]*\s+-c\s+['\"]import os"; then
        BLOCKED=1
    fi
    if echo "$COMMAND" | grep -qE "node\s+-e\s+['\"].*process\.env"; then
        BLOCKED=1
    fi
    # Reading .env files via head/tail/less
    if echo "$COMMAND" | grep -qE '\b(head|tail|less|more)\b[^|]*\.env(\.[^ ]*)?(\s|$)'; then
        BLOCKED=1
    fi
    # sed/awk/perl reading .env files
    if echo "$COMMAND" | grep -qE '\b(sed|awk|perl)\b[^|]*\.env(\.[^ ]*)?(\s|$)'; then
        BLOCKED=1
    fi
    # sed/awk/perl/ruby/rg/grep/find reading credential files (.netrc, .npmrc, .pypirc, .aws/*, .kube/*, gcloud creds)
    if echo "$COMMAND" | grep -qE '\b(cat|sed|awk|perl|ruby|grep|rg|find|head|tail|less|more)\b[^|]*(\.netrc|\.npmrc|\.pypirc|\.aws/(credentials|config)|\.config/gcloud|\.kube/(config|credentials))'; then
        BLOCKED=1
    fi
    # Tilde-expanded credential paths (e.g. cat ~/.netrc, cat ~/path/.aws/credentials)
    if echo "$COMMAND" | grep -qE '\b(cat|sed|awk|perl|ruby|grep|rg|find|head|tail|less|more)\b[^|]*~/(\.netrc|\.npmrc|\.pypirc|\.aws/(credentials|config)|\.config/gcloud/application_default_credentials\.json|\.kube/(config|credentials))'; then
        BLOCKED=1
    fi
    # python3/python reading .env files or dumping os.environ
    if echo "$COMMAND" | grep -qE '\bpython3?\b[^|]*\.env(\.[^ ]*)?(\s|$)'; then
        BLOCKED=1
    fi
    if echo "$COMMAND" | grep -qE '\bpython3?\b[^|]*os\.environ'; then
        BLOCKED=1
    fi
    # xargs -a .env — indirect reading
    if echo "$COMMAND" | grep -qE '\bxargs\b[^|]*-a\s+\.env(\.[^ ]*)?(\s|$)'; then
        BLOCKED=1
    fi
    # source / . to load .env files
    if echo "$COMMAND" | grep -qE '(^|\s)\.\s+\.env(\.[^ ]*)?(\s|$)'; then
        BLOCKED=1
    fi
    if echo "$COMMAND" | grep -qE '\bsource\s+\.env(\.[^ ]*)?(\s|$)'; then
        BLOCKED=1
    fi

    if [ "$BLOCKED" -eq 1 ]; then
        echo "[secret_boundary_guard] blocked: high-risk Bash command pattern detected" >&2
        exit 2
    fi
fi

# ── Sensitive path patterns for Read/Write/Edit/Grep/Glob/MultiEdit ──────────
# Block access to .env files, secrets/, credential paths, local token stores.

SENSITIVE_TOOLS="Read Write Edit Grep Glob MultiEdit"

# MultiEdit pathless fail-closed: MultiEdit で file_path が欠落または空文字の場合は
# 安全側に倒して exit 2 で遮断する（stderr に raw payload/path/secret-like value を出力しない）。
if [ "$TOOL_NAME" = "MultiEdit" ] && [ -z "$PATH_INPUT" ]; then
    echo "[secret_boundary_guard] blocked: MultiEdit with missing/empty file_path — fail closed" >&2
    exit 2
fi

if echo "$SENSITIVE_TOOLS" | grep -qw "$TOOL_NAME" && [ -n "$PATH_INPUT" ]; then
    BLOCKED=0
    # .env and .env.* files
    if echo "$PATH_INPUT" | grep -qE '(^|/)\.env(\.[^/]*)?$'; then
        BLOCKED=1
    fi
    # secrets/ directory
    if echo "$PATH_INPUT" | grep -qE '(^|/)secrets/'; then
        BLOCKED=1
    fi
    # credential file patterns
    if echo "$PATH_INPUT" | grep -qE '(^|/)(credentials|\.aws/credentials|\.aws/config|\.config/gcloud|\.config/gcloud/application_default_credentials\.json|\.kube/config|\.kube/credentials)(\.json|\.toml|\.yaml|\.yml)?$'; then
        BLOCKED=1
    fi
    # local token stores: settings.local.json
    if echo "$PATH_INPUT" | grep -qE '(^|/)settings\.local\.json$'; then
        BLOCKED=1
    fi
    # .env.local
    if echo "$PATH_INPUT" | grep -qE '(^|/)\.env\.local$'; then
        BLOCKED=1
    fi
    # .netrc — contains credentials
    if echo "$PATH_INPUT" | grep -qE '(^|/)\.netrc$'; then
        BLOCKED=1
    fi
    # .npmrc / .pypirc with potential tokens
    if echo "$PATH_INPUT" | grep -qE '(^|/)\.(npmrc|pypirc)$'; then
        BLOCKED=1
    fi

    if [ "$BLOCKED" -eq 1 ]; then
        echo "[secret_boundary_guard] blocked: sensitive path pattern detected in tool input" >&2
        exit 2
    fi
fi

# ── Allow ─────────────────────────────────────────────────────────────────────
exit 0
