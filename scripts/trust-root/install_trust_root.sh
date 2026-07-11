#!/usr/bin/env bash
# install_trust_root.sh — privileged-operator installer for the external
# publish-lane authorization trust root (Issue #1454, Phase A).
#
# This script is executed MANUALLY by a privileged operator (never by an
# agent). It builds an AUTHORIZATION_TCB_MANIFEST_V1 manifest from the tree
# of a trusted commit OID (in a source checkout the operator controls) and
# atomically installs it — plus the launcher source and this installer
# itself — under a release-based, root-owned trust root directory that is
# OUTSIDE the candidate repository.
#
# Layout produced under <trust_root_dir>:
#
#   <trust_root_dir>/
#     releases/<generation>-<manifest-digest>/
#       manifest.json          # AUTHORIZATION_TCB_MANIFEST_V1
#     active.json               # {"active_generation": N, "manifest_relpath": "releases/.../manifest.json"}
#
# Activation is a single atomic step: active.json is written to a tmp file
# in the same directory, fsync'd, then renamed over the previous active.json
# (POSIX rename(2) is atomic within the same filesystem — see `rename(2)`).
#
# Usage:
#   install_trust_root.sh <trust_root_dir> <source_repo_dir> <trusted_commit_oid> \
#       <repository_slug> <component_path> [<component_path> ...]
#
# Notes:
#   - <trust_root_dir> MUST already exist and be owned by the intended trust
#     root owner account (this script does not itself perform privilege
#     escalation / chown — that is the privileged operator's responsibility
#     before invoking this script, e.g. via `sudo -u trust-root-owner`).
#   - This script refuses to run if the CALLER's effective uid is the SAME
#     as the owner uid of <trust_root_dir> (`runtime_euid_must_differ_from_owner`).
#     This enforces a minimal separation of duties: the account that owns
#     (and could therefore silently mutate) the trust root must not be the
#     same account invoking the rotation, so a single compromised/agent
#     account cannot both own and rotate the trust anchor unattended.
#   - agents (Claude Code / Codex CLI sessions) MUST NOT execute this
#     script. It is listed as an explicit Stop Condition in Issue #1454.

set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: $0 <trust_root_dir> <source_repo_dir> <trusted_commit_oid> <repository_slug> <component_path> [<component_path> ...]" >&2
  exit 64
fi

TRUST_ROOT_DIR="$1"; shift
SOURCE_REPO_DIR="$1"; shift
TRUSTED_COMMIT_OID="$1"; shift
REPOSITORY_SLUG="$1"; shift

if [ "$#" -lt 1 ]; then
  echo "error: at least one <component_path> is required" >&2
  exit 64
fi

case "$TRUST_ROOT_DIR" in
  /*) ;;
  *)
    echo "error: trust_root_dir must be an absolute path" >&2
    exit 65
    ;;
esac

if [ ! -d "$TRUST_ROOT_DIR" ]; then
  echo "error: trust_root_dir does not exist (create + chown it first): $TRUST_ROOT_DIR" >&2
  exit 66
fi

# ─── Separation of duties: runtime euid must differ from trust root owner ───

TRUST_ROOT_OWNER_UID="$(stat -c '%u' "$TRUST_ROOT_DIR")"
CALLER_EUID="$(id -u)"

if [ "$CALLER_EUID" = "$TRUST_ROOT_OWNER_UID" ]; then
  echo "error: runtime_euid_must_differ_from_owner: caller euid ($CALLER_EUID) equals trust_root_dir owner uid ($TRUST_ROOT_OWNER_UID)" >&2
  exit 77
fi

# ─── Trusted commit OID must be a full 40-hex commit object ─────────────────

if ! printf '%s' "$TRUSTED_COMMIT_OID" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "error: trusted_commit_oid must be a full 40-hex commit OID" >&2
  exit 65
fi

if ! git -C "$SOURCE_REPO_DIR" rev-parse --verify --quiet "${TRUSTED_COMMIT_OID}^{commit}" --end-of-options >/dev/null; then
  echo "error: trusted_commit_oid does not resolve to a commit object in source_repo_dir" >&2
  exit 65
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# ─── Build the component digest list from the TRUSTED COMMIT's tree only ────
# (never from the operator's own working tree bytes)

MANIFEST_COMPONENTS_JSON="$WORKDIR/components.json"
printf '[' > "$MANIFEST_COMPONENTS_JSON"

FIRST=1
for COMPONENT_PATH in "$@"; do
  if ! git -C "$SOURCE_REPO_DIR" cat-file -e "${TRUSTED_COMMIT_OID}:${COMPONENT_PATH}" 2>/dev/null; then
    echo "error: component path not found in trusted commit tree: $COMPONENT_PATH" >&2
    exit 67
  fi
  MODE_TYPE="$(git -C "$SOURCE_REPO_DIR" ls-tree "$TRUSTED_COMMIT_OID" -- "$COMPONENT_PATH" | awk '{print $1, $2}')"
  case "$MODE_TYPE" in
    "100644 blob"|"100755 blob") ;;
    *)
      echo "error: component is not a regular blob (mode/type=$MODE_TYPE): $COMPONENT_PATH" >&2
      exit 67
      ;;
  esac
  DIGEST="$(git -C "$SOURCE_REPO_DIR" show "${TRUSTED_COMMIT_OID}:${COMPONENT_PATH}" | sha256sum | awk '{print $1}')"
  if [ "$FIRST" -eq 0 ]; then
    printf ',' >> "$MANIFEST_COMPONENTS_JSON"
  fi
  FIRST=0
  printf '{"path":"%s","sha256":"%s"}' "$COMPONENT_PATH" "$DIGEST" >> "$MANIFEST_COMPONENTS_JSON"
done
printf ']' >> "$MANIFEST_COMPONENTS_JSON"

# ─── Determine next monotonic generation ─────────────────────────────────────

ACTIVE_JSON="$TRUST_ROOT_DIR/active.json"
if [ -f "$ACTIVE_JSON" ]; then
  PREV_GENERATION="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["active_generation"]))' "$ACTIVE_JSON")"
  GENERATION=$((PREV_GENERATION + 1))
else
  GENERATION=1
fi

ISSUED_BY="$(id -un)@$(hostname)"

MANIFEST_JSON="$WORKDIR/manifest.json"
python3 - "$MANIFEST_COMPONENTS_JSON" "$MANIFEST_JSON" "$REPOSITORY_SLUG" "$TRUSTED_COMMIT_OID" "$GENERATION" "$ISSUED_BY" <<'PYEOF'
import json
import sys

components_path, manifest_path, repository, trusted_commit_oid, generation, issued_by = sys.argv[1:7]

with open(components_path, "r", encoding="utf-8") as f:
    components = json.load(f)

manifest = {
    "manifest_version": "AUTHORIZATION_TCB_MANIFEST_V1",
    "repository": repository,
    "trusted_commit_oid": trusted_commit_oid,
    "components": components,
    "issued_by": issued_by,
    "generation": int(generation),
}

with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, sort_keys=True, indent=2)
    f.write("\n")
PYEOF

MANIFEST_DIGEST="$(sha256sum "$MANIFEST_JSON" | awk '{print $1}')"
RELEASE_DIR="$TRUST_ROOT_DIR/releases/${GENERATION}-${MANIFEST_DIGEST}"

mkdir -p "$TRUST_ROOT_DIR/releases"
if [ -d "$RELEASE_DIR" ]; then
  echo "error: release directory already exists (non-monotonic generation?): $RELEASE_DIR" >&2
  exit 68
fi
mkdir -p "$RELEASE_DIR"
cp "$MANIFEST_JSON" "$RELEASE_DIR/manifest.json"
chmod 0444 "$RELEASE_DIR/manifest.json"
chmod 0555 "$RELEASE_DIR"

# ─── Atomic activation: tmp + fsync + rename over active.json ───────────────

TMP_ACTIVE="$TRUST_ROOT_DIR/.active.json.tmp.$$"
python3 - "$TMP_ACTIVE" "$GENERATION" "$RELEASE_DIR" "$TRUST_ROOT_DIR" <<'PYEOF'
import json
import os
import sys

tmp_active, generation, release_dir, trust_root_dir = sys.argv[1:5]
manifest_relpath = os.path.relpath(os.path.join(release_dir, "manifest.json"), trust_root_dir)

payload = {
    "active_generation": int(generation),
    "manifest_relpath": manifest_relpath,
}

fd = os.open(tmp_active, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
PYEOF

chmod 0444 "$TMP_ACTIVE"
mv -f "$TMP_ACTIVE" "$ACTIVE_JSON"

echo "installed trust root generation=$GENERATION release_dir=$RELEASE_DIR" >&2
echo "INSTALL_TRUST_ROOT_RESULT_V1: {\"status\": \"ok\", \"generation\": $GENERATION, \"release_dir\": \"$RELEASE_DIR\"}"
