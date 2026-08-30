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
# PASS). This invocation deliberately omits ``--task`` (Issue #2240
# fix_delta P0-1), exercising the DEFAULT_TASK fallback path
# (``run_retrospective.py`` still investigates for real rather than
# returning a hardcoded empty-findings prompt -- an empty result here means
# the trivially-tiny temp repo genuinely had nothing to find, not that the
# observers were never asked to look).
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
# verify the run reached PublishRequest construction (packaging correctness).
# Issue #2240 fix_delta P1-4(a): a bare substring grep (the pre-fix version
# of this check) false-positive-passes on malformed inner JSON, on Claude
# merely mentioning the schema name in prose, on an error message that
# happens to contain "publish_request/v1", or on a wrong repository_id/
# base_sha -- it never actually parses either the outer claude
# --output-format json wrapper or the inner PublishRequest JSON it claims to
# find. This verifies both layers structurally instead.
# ---------------------------------------------------------------------------

VERIFY_SCRIPT="${WORKDIR}/verify_publish_request.py"
cat > "${VERIFY_SCRIPT}" <<'PYEOF'
import json
import subprocess
import sys

output_path, repo_dir, expected_repository_id = sys.argv[1], sys.argv[2], sys.argv[3]

expected_base_sha = subprocess.run(
    ["git", "-C", repo_dir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()

with open(output_path, "r", encoding="utf-8") as fh:
    raw_text = fh.read()

try:
    outer = json.loads(raw_text)
except json.JSONDecodeError as exc:
    print(f"FAIL: outer claude --output-format json output is not valid JSON: {exc}")
    sys.exit(1)

if not isinstance(outer, dict):
    print("FAIL: outer claude output is not a JSON object")
    sys.exit(1)

if outer.get("type") != "result":
    print(f"FAIL: outer type is not 'result' (got {outer.get('type')!r})")
    sys.exit(1)

if outer.get("subtype") != "success":
    print(f"FAIL: outer subtype is not 'success' (got {outer.get('subtype')!r})")
    sys.exit(1)

if outer.get("is_error"):
    print("FAIL: outer is_error is truthy")
    sys.exit(1)

result_text = outer.get("result")
if not isinstance(result_text, str) or not result_text.strip():
    print("FAIL: outer 'result' field is missing/empty")
    sys.exit(1)

# The prompt instructs Claude to print run_retrospective.py's raw stdout
# verbatim with no markdown fence -- tolerate an incidental fence wrap
# anyway, since this is exactly the kind of formatting drift a bare
# substring grep would silently paper over.
candidate_text = result_text.strip()
if candidate_text.startswith("```"):
    lines = candidate_text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    candidate_text = "\n".join(lines).strip()

try:
    inner = json.loads(candidate_text)
except json.JSONDecodeError as exc:
    print(f"FAIL: inner result text is not valid JSON: {exc}")
    sys.exit(1)

if not isinstance(inner, dict):
    print("FAIL: inner result is not a JSON object")
    sys.exit(1)

if "_smoke_fallback" in raw_text:
    print("FAIL: output contains a fallback-shaped field (_smoke_fallback); per fallback_policy this is never PASS")
    sys.exit(1)

if inner.get("status") == "failed":
    print(f"FAIL: run_retrospective.py reported a typed failure: {inner}")
    sys.exit(1)

if inner.get("schema_version") != "publish_request/v1":
    print(f"FAIL: inner schema_version is not 'publish_request/v1' (got {inner.get('schema_version')!r})")
    sys.exit(1)

if inner.get("repository_id") != expected_repository_id:
    print(f"FAIL: repository_id mismatch: expected {expected_repository_id!r}, got {inner.get('repository_id')!r}")
    sys.exit(1)

run_identity = inner.get("run_identity")
if not isinstance(run_identity, dict):
    print("FAIL: run_identity is missing/not an object")
    sys.exit(1)

actual_base_sha = run_identity.get("base_sha")
if actual_base_sha != expected_base_sha:
    print(f"FAIL: run_identity.base_sha mismatch: expected {expected_base_sha!r} (temp repo HEAD), got {actual_base_sha!r}")
    sys.exit(1)

candidate_records = inner.get("candidate_records")
if not isinstance(candidate_records, list):
    print("FAIL: candidate_records is not an array")
    sys.exit(1)

print(
    "PASS: run reached a publish_request/v1 PublishRequest envelope "
    f"(repository_id={inner.get('repository_id')!r}, base_sha={actual_base_sha!r}, "
    f"candidate_records={len(candidate_records)})"
)
sys.exit(0)
PYEOF

EXPECTED_REPOSITORY_ID="agent-retrospective-clean-install-smoke/portability-smoke"

set +e
uv run --project "${PLUGIN_ROOT}" --locked python3 "${VERIFY_SCRIPT}" \
  "${OUTPUT_FILE}" "${REPO_DIR}" "${EXPECTED_REPOSITORY_ID}"
VERIFY_EXIT=$?
set -e

if [ "${VERIFY_EXIT}" -ne 0 ]; then
  echo "FAIL: JSON-based PublishRequest verification failed (see output above)" >&2
  exit 1
fi

exit 0
