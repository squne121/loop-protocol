#!/usr/bin/env bash
# install_trust_root.sh — privileged-operator installer for the external
# publish-lane authorization trust root (Issue #1454, Phase A).
#
# This script is executed MANUALLY by a privileged operator (never by an
# agent). It builds an AUTHORIZATION_TCB_MANIFEST_V1 manifest from the tree
# of a trusted commit OID (in a source checkout the operator controls),
# copies the launcher runtime itself (trusted_hook_launcher.py /
# manifest_schema.py) from that SAME trusted commit tree, and atomically
# installs both under a release-based, root-owned trust root directory that
# is OUTSIDE the candidate repository.
#
# Layout produced under <trust_root_dir>:
#
#   <trust_root_dir>/
#     owner.json                 # {"owner_uid": N, "owner_gid": N} — fixed at first install
#     releases/<generation>-<manifest-digest>/
#       manifest.json               # AUTHORIZATION_TCB_MANIFEST_V1
#       trusted_hook_launcher.py    # runtime copy (from trusted_commit_oid tree)
#       manifest_schema.py          # runtime copy (from trusted_commit_oid tree)
#     active.json                 # {"active_generation": N, "manifest_relpath": ..., "launcher_relpath": ..., "manifest_schema_relpath": ...}
#     trusted_hook_launcher.py    # FIXED top-level path — atomically replaced each rotation.
#     manifest_schema.py          # FIXED top-level path — atomically replaced each rotation.
#
# The managed PreToolUse hook registration (/etc/codex/requirements.toml)
# uses the FIXED top-level path so the admin-managed hook command NEVER
# changes across manifest rotations:
#
#   /usr/bin/python3 -I <trust_root_dir>/trusted_hook_launcher.py --evidence-file <path>
#
# Activation is atomic: the release-scoped files are staged, chmod'd to
# owner-read-only, then the top-level trusted_hook_launcher.py /
# manifest_schema.py / active.json are each replaced via tmp+fsync+rename
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
#   - On first install, this script records the trust root's owner uid/gid
#     into <trust_root_dir>/owner.json. Every subsequent invocation (any
#     later rotation) re-verifies current ownership against that FIXED
#     recorded value and refuses to proceed on any mismatch
#     (`trust_root_owner_identity_changed`) — an owner uid/gid drift between
#     rotations is treated as tamper, not as an implicit re-bootstrap.
#   - All external binaries (git/python3/id/stat/hostname/env) are resolved
#     from a fixed absolute-path allowlist, never from ambient PATH. Every
#     `git` invocation against <source_repo_dir> runs with `--no-replace-objects`,
#     an explicit `--git-dir`, and an ALLOWLIST-constructed environment
#     (dedicated HOME, GIT_CONFIG_GLOBAL/SYSTEM=/dev/null,
#     GIT_CONFIG_NOSYSTEM=1) — GIT_DIR / GIT_COMMON_DIR / GIT_OBJECT_DIRECTORY /
#     GIT_ALTERNATE_OBJECT_DIRECTORIES / GIT_CONFIG_COUNT are never inherited.
#   - Component digests are computed by a FIXED python3 runtime using
#     `git cat-file --batch` raw object bytes directly (no shell pipeline
#     through `sha256sum`/`awk`).
#   - agents (Claude Code / Codex CLI sessions) MUST NOT execute this
#     script. It is listed as an explicit Stop Condition in Issue #1454.

set -euo pipefail

# ─── Fixed, non-PATH-based absolute binary resolution ────────────────────────

_resolve_bin() {
  local name="$1"; shift
  local candidate
  for candidate in "$@"; do
    if [ -x "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  echo "error: no trusted absolute binary found for '$name' (checked: $*)" >&2
  exit 69
}

GIT_BIN="$(_resolve_bin git /usr/bin/git /usr/local/bin/git /bin/git)"
PYTHON_BIN="$(_resolve_bin python3 /usr/bin/python3 /usr/local/bin/python3 /bin/python3)"
ID_BIN="$(_resolve_bin id /usr/bin/id /bin/id)"
STAT_BIN="$(_resolve_bin stat /usr/bin/stat /bin/stat)"
ENV_BIN="$(_resolve_bin env /usr/bin/env /bin/env)"
HOSTNAME_BIN="$(_resolve_bin hostname /usr/bin/hostname /bin/hostname)"

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

TRUST_ROOT_OWNER_UID="$("$STAT_BIN" -c '%u' "$TRUST_ROOT_DIR")"
TRUST_ROOT_OWNER_GID="$("$STAT_BIN" -c '%g' "$TRUST_ROOT_DIR")"
CALLER_EUID="$("$ID_BIN" -u)"

if [ "$CALLER_EUID" = "$TRUST_ROOT_OWNER_UID" ]; then
  echo "error: runtime_euid_must_differ_from_owner: caller euid ($CALLER_EUID) equals trust_root_dir owner uid ($TRUST_ROOT_OWNER_UID)" >&2
  exit 77
fi

# ─── Fixed owner identity: first install records it, later runs re-verify ───

OWNER_JSON="$TRUST_ROOT_DIR/owner.json"
if [ -f "$OWNER_JSON" ]; then
  RECORDED_OWNER_UID="$("$PYTHON_BIN" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["owner_uid"])' "$OWNER_JSON")"
  RECORDED_OWNER_GID="$("$PYTHON_BIN" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["owner_gid"])' "$OWNER_JSON")"
  if [ "$RECORDED_OWNER_UID" != "$TRUST_ROOT_OWNER_UID" ] || [ "$RECORDED_OWNER_GID" != "$TRUST_ROOT_OWNER_GID" ]; then
    echo "error: trust_root_owner_identity_changed: recorded owner uid/gid ($RECORDED_OWNER_UID/$RECORDED_OWNER_GID) does not match current trust_root_dir owner uid/gid ($TRUST_ROOT_OWNER_UID/$TRUST_ROOT_OWNER_GID)" >&2
    exit 78
  fi
else
  TMP_OWNER_JSON="$TRUST_ROOT_DIR/.owner.json.tmp.$$"
  "$PYTHON_BIN" -I - "$TMP_OWNER_JSON" "$TRUST_ROOT_OWNER_UID" "$TRUST_ROOT_OWNER_GID" <<'PYEOF'
import json
import os
import sys

tmp_path, owner_uid, owner_gid = sys.argv[1:4]
payload = {"owner_uid": int(owner_uid), "owner_gid": int(owner_gid)}
fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
PYEOF
  chmod 0444 "$TMP_OWNER_JSON"
  mv -f "$TMP_OWNER_JSON" "$OWNER_JSON"
fi

# ─── Trusted commit OID must be a full 40-hex commit object ─────────────────

if ! printf '%s' "$TRUSTED_COMMIT_OID" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "error: trusted_commit_oid must be a full 40-hex commit OID" >&2
  exit 65
fi

SOURCE_GIT_DIR="$("$PYTHON_BIN" -I -c 'import os,sys; print(os.path.realpath(os.path.join(sys.argv[1], ".git")))' "$SOURCE_REPO_DIR")"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

GIT_DEDICATED_HOME="$WORKDIR/git-home"
mkdir -p "$GIT_DEDICATED_HOME"

# ─── Restricted, allowlist-environment git invocation (never ambient PATH) ──
_git() {
  "$ENV_BIN" -i \
    PATH="/usr/bin:/bin" \
    HOME="$GIT_DEDICATED_HOME" \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_CONFIG_NOSYSTEM=1 \
    LC_ALL=C \
    "$GIT_BIN" --no-replace-objects "--git-dir=$SOURCE_GIT_DIR" "$@"
}

if ! _git rev-parse --verify --quiet "${TRUSTED_COMMIT_OID}^{commit}" --end-of-options >/dev/null; then
  echo "error: trusted_commit_oid does not resolve to a commit object in source_repo_dir" >&2
  exit 65
fi

# ─── Build the component digest list from the TRUSTED COMMIT's tree only ────
# Digests are computed by a FIXED python3 runtime reading raw git object
# bytes directly (git cat-file -p via subprocess) — no shell pipeline
# through sha256sum/awk (Issue #1454 fix_delta P1-4).

COMPONENTS_LIST_FILE="$WORKDIR/component_paths.txt"
printf '%s\n' "$@" > "$COMPONENTS_LIST_FILE"

MANIFEST_COMPONENTS_JSON="$WORKDIR/components.json"

"$PYTHON_BIN" -I - \
  "$GIT_BIN" "$SOURCE_GIT_DIR" "$GIT_DEDICATED_HOME" "$TRUSTED_COMMIT_OID" \
  "$COMPONENTS_LIST_FILE" "$MANIFEST_COMPONENTS_JSON" <<'PYEOF'
import hashlib
import json
import subprocess
import sys

git_bin, git_dir, dedicated_home, commit_oid, components_list_file, out_path = sys.argv[1:7]

env = {
    "PATH": "/usr/bin:/bin",
    "HOME": dedicated_home,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
}


def run_git(*args, binary=False):
    argv = [git_bin, "--no-replace-objects", f"--git-dir={git_dir}", *args]
    return subprocess.run(argv, env=env, capture_output=True, text=not binary, timeout=30)


with open(components_list_file, "r", encoding="utf-8") as f:
    component_paths = [line.strip() for line in f if line.strip()]

components = []
for path in component_paths:
    ls = run_git("ls-tree", commit_oid, "--", path)
    if ls.returncode != 0 or not ls.stdout.strip():
        print(f"error: component path not found in trusted commit tree: {path}", file=sys.stderr)
        sys.exit(67)
    line = ls.stdout.strip()
    meta, _, entry_path = line.partition("\t")
    if entry_path != path:
        print(f"error: component path mismatch in tree listing: {path}", file=sys.stderr)
        sys.exit(67)
    parts = meta.split()
    if len(parts) != 3:
        print(f"error: unparseable ls-tree entry for: {path}", file=sys.stderr)
        sys.exit(67)
    mode, obj_type, blob_sha = parts
    if obj_type != "blob" or mode not in ("100644", "100755"):
        print(f"error: component is not a regular blob (mode={mode} type={obj_type}): {path}", file=sys.stderr)
        sys.exit(67)
    blob = run_git("cat-file", "-p", blob_sha, binary=True)
    if blob.returncode != 0:
        print(f"error: unable to read blob bytes for: {path}", file=sys.stderr)
        sys.exit(67)
    digest = hashlib.sha256(blob.stdout).hexdigest()
    components.append({"path": path, "sha256": digest})

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(components, f)
PYEOF

# ─── Determine next monotonic generation ─────────────────────────────────────

ACTIVE_JSON="$TRUST_ROOT_DIR/active.json"
if [ -f "$ACTIVE_JSON" ]; then
  PREV_GENERATION="$("$PYTHON_BIN" -I -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["active_generation"]))' "$ACTIVE_JSON")"
  GENERATION=$((PREV_GENERATION + 1))
else
  GENERATION=1
fi

ISSUED_BY="$("$ID_BIN" -un)@$("$HOSTNAME_BIN")"

MANIFEST_JSON="$WORKDIR/manifest.json"
"$PYTHON_BIN" -I - "$MANIFEST_COMPONENTS_JSON" "$MANIFEST_JSON" "$REPOSITORY_SLUG" "$TRUSTED_COMMIT_OID" "$GENERATION" "$ISSUED_BY" <<'PYEOF'
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

# ─── Fetch the runtime component sources (trusted_hook_launcher.py /
# manifest_schema.py) from the SAME trusted commit tree — never from the
# operator's own possibly-modified working tree bytes.

LAUNCHER_SRC="$WORKDIR/trusted_hook_launcher.py"
SCHEMA_SRC="$WORKDIR/manifest_schema.py"

_git show "${TRUSTED_COMMIT_OID}:scripts/trust-root/trusted_hook_launcher.py" > "$LAUNCHER_SRC"
_git show "${TRUSTED_COMMIT_OID}:scripts/trust-root/manifest_schema.py" > "$SCHEMA_SRC"

MANIFEST_DIGEST="$(
  "$PYTHON_BIN" -I -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$MANIFEST_JSON"
)"
RELEASE_DIR="$TRUST_ROOT_DIR/releases/${GENERATION}-${MANIFEST_DIGEST}"

mkdir -p "$TRUST_ROOT_DIR/releases"
if [ -d "$RELEASE_DIR" ]; then
  echo "error: release directory already exists (non-monotonic generation?): $RELEASE_DIR" >&2
  exit 68
fi
mkdir -p "$RELEASE_DIR"
cp "$MANIFEST_JSON" "$RELEASE_DIR/manifest.json"
cp "$LAUNCHER_SRC" "$RELEASE_DIR/trusted_hook_launcher.py"
cp "$SCHEMA_SRC" "$RELEASE_DIR/manifest_schema.py"
chmod 0444 "$RELEASE_DIR/manifest.json" "$RELEASE_DIR/trusted_hook_launcher.py" "$RELEASE_DIR/manifest_schema.py"
chmod 0555 "$RELEASE_DIR"

# ─── Atomic activation of the release pointer ────────────────────────────────

TMP_ACTIVE="$TRUST_ROOT_DIR/.active.json.tmp.$$"
"$PYTHON_BIN" -I - "$TMP_ACTIVE" "$GENERATION" "$RELEASE_DIR" "$TRUST_ROOT_DIR" <<'PYEOF'
import json
import os
import sys

tmp_active, generation, release_dir, trust_root_dir = sys.argv[1:5]


def relpath(name):
    return os.path.relpath(os.path.join(release_dir, name), trust_root_dir)


payload = {
    "active_generation": int(generation),
    "manifest_relpath": relpath("manifest.json"),
    "launcher_relpath": relpath("trusted_hook_launcher.py"),
    "manifest_schema_relpath": relpath("manifest_schema.py"),
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

# ─── Atomically replace the FIXED top-level launcher runtime copies ─────────
# These are the paths the admin-managed hook command references directly
# (Issue #1454 fix_delta P0-2): they never move across rotations, only their
# CONTENT changes, via tmp+fsync+rename.

TMP_LAUNCHER="$TRUST_ROOT_DIR/.trusted_hook_launcher.py.tmp.$$"
cp "$LAUNCHER_SRC" "$TMP_LAUNCHER"
chmod 0444 "$TMP_LAUNCHER"
mv -f "$TMP_LAUNCHER" "$TRUST_ROOT_DIR/trusted_hook_launcher.py"

TMP_SCHEMA="$TRUST_ROOT_DIR/.manifest_schema.py.tmp.$$"
cp "$SCHEMA_SRC" "$TMP_SCHEMA"
chmod 0444 "$TMP_SCHEMA"
mv -f "$TMP_SCHEMA" "$TRUST_ROOT_DIR/manifest_schema.py"

echo "installed trust root generation=$GENERATION release_dir=$RELEASE_DIR" >&2
echo "INSTALL_TRUST_ROOT_RESULT_V1: {\"status\": \"ok\", \"generation\": $GENERATION, \"release_dir\": \"$RELEASE_DIR\", \"launcher_path\": \"$TRUST_ROOT_DIR/trusted_hook_launcher.py\"}"
