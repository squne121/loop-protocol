#!/usr/bin/env bash
# clean_install_smoke.sh -- agent-retrospective plugin clean-install smoke
# (Issue #2240 AC5).
#
# Creates a temporary ``.claude/``-free Git repository (deliberately on a
# branch named ``portability-smoke``, NOT ``main``, to prove the plugin's
# base-SHA resolver does not hardcode ``git rev-parse main``), then launches
# ``claude --plugin-dir <this plugin's absolute path>`` and explicitly
# invokes the canonical namespaced ``/agent-retrospective:run`` Skill with
# ``--state-backend fixture``, verifying the run reaches ``PublishRequest``
# construction (packaging/wiring correctness only -- not model-quality; a
# non-empty ``findings``/``candidate_records`` result is NOT required for
# PASS).
#
# Exit codes:
#   0  PASS  -- the run reached a ``publish_request/v1`` envelope.
#   1  FAIL  -- claude/plugin invocation ran but did not reach
#               ``PublishRequest`` (a typed failure, a crash, or malformed
#               output). Never silently treated as PASS.
#   77 SKIP  -- ``claude`` CLI is not on PATH, or this claude CLI build does
#               not support ``--plugin-dir`` / ``claude plugin validate``.
#               stdout is prefixed with ``SKIP:``. SKIP is never reported as
#               PASS.
#
# Per this Issue's Runtime Verification Applicability fallback_policy: a
# mocked/stubbed claude invocation, or a result carrying a
# ``_smoke_fallback: true``-shaped field, is never treated as PASS by this
# script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts/ -> run/ -> skills/ -> agent-retrospective (this plugin's root)
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# ---------------------------------------------------------------------------
# preflight: claude CLI / --plugin-dir / plugin validate availability
# ---------------------------------------------------------------------------

if ! command -v claude >/dev/null 2>&1; then
  echo "SKIP: claude CLI not found on PATH"
  exit 77
fi

if ! claude --help 2>&1 | grep -q -- '--plugin-dir'; then
  echo "SKIP: this claude CLI build does not support --plugin-dir"
  exit 77
fi

if ! claude plugin validate --help >/dev/null 2>&1; then
  echo "SKIP: this claude CLI build does not support 'claude plugin validate'"
  exit 77
fi

# ---------------------------------------------------------------------------
# temp .claude/-free Git repository on a non-'main' branch
# ---------------------------------------------------------------------------

WORKDIR="$(mktemp -d)"
cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

REPO_DIR="${WORKDIR}/smoke-repo"
mkdir -p "${REPO_DIR}"

echo "== clean_install_smoke: temp repo setup =="
echo "repo_dir=${REPO_DIR}"

git init -q -b portability-smoke "${REPO_DIR}"
git -C "${REPO_DIR}" config user.email "smoke@example.invalid"
git -C "${REPO_DIR}" config user.name "agent-retrospective clean-install smoke"
printf '# clean-install smoke repository\n\nCreated by agent-retrospective plugin clean_install_smoke.sh (Issue #2240 AC5).\n' > "${REPO_DIR}/README.md"
git -C "${REPO_DIR}" add README.md
git -C "${REPO_DIR}" commit -q -m "initial commit (portability-smoke branch, deliberately not main)"

if [ -e "${REPO_DIR}/.claude" ]; then
  echo "FAIL: smoke repo unexpectedly contains a .claude/ path" >&2
  exit 1
fi

CURRENT_BRANCH="$(git -C "${REPO_DIR}" branch --show-current)"
if [ "${CURRENT_BRANCH}" != "portability-smoke" ]; then
  echo "FAIL: smoke repo branch is '${CURRENT_BRANCH}', expected 'portability-smoke'" >&2
  exit 1
fi

echo "branch=${CURRENT_BRANCH}"
echo "plugin_root=${PLUGIN_ROOT}"

# ---------------------------------------------------------------------------
# launch claude --plugin-dir from inside the temp repo, invoking the
# canonical namespaced /agent-retrospective:run skill explicitly
# ---------------------------------------------------------------------------

OUTPUT_FILE="${WORKDIR}/claude_output.json"

PROMPT=$(cat <<'PROMPT_EOF'
/agent-retrospective:run

Execute this plugin Skill's Procedure step 1 exactly as SKILL.md specifies,
with these explicit arguments (do not omit --state-backend, do not invent
different flag values):

uv run --project "${CLAUDE_PLUGIN_ROOT}" --locked python3 \
  "${CLAUDE_PLUGIN_ROOT}/skills/run/scripts/run_retrospective.py" \
  --repo-root "${CLAUDE_PROJECT_DIR}" \
  --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
  --repository-id agent-retrospective-clean-install-smoke/portability-smoke \
  --state-backend fixture

Run this single Bash command exactly once. Print its raw stdout verbatim as
your final response, with no additional commentary, prose, or markdown
fences wrapped around it.
PROMPT_EOF
)

echo "== clean_install_smoke: launching claude --plugin-dir =="

set +e
(
  cd "${REPO_DIR}"
  claude --plugin-dir "${PLUGIN_ROOT}" -p "${PROMPT}" --output-format json
) > "${OUTPUT_FILE}" 2>"${WORKDIR}/claude_stderr.log"
CLAUDE_EXIT=$?
set -e

echo "claude_exit_code=${CLAUDE_EXIT}"
echo "== claude stdout (bounded excerpt) =="
head -c 4000 "${OUTPUT_FILE}" || true
echo
echo "== claude stderr (bounded excerpt) =="
head -c 2000 "${WORKDIR}/claude_stderr.log" || true
echo

if [ "${CLAUDE_EXIT}" -ne 0 ]; then
  echo "FAIL: claude --plugin-dir invocation exited ${CLAUDE_EXIT}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# verify the run reached PublishRequest construction (packaging correctness)
# ---------------------------------------------------------------------------

if grep -q '_smoke_fallback' "${OUTPUT_FILE}"; then
  echo "FAIL: output contains a fallback-shaped field (_smoke_fallback); per fallback_policy this is never PASS" >&2
  exit 1
fi

if grep -q 'publish_request/v1' "${OUTPUT_FILE}"; then
  echo "PASS: run reached a publish_request/v1 PublishRequest envelope"
  exit 0
fi

echo "FAIL: run did not reach a publish_request/v1 PublishRequest envelope (see output above)" >&2
exit 1
