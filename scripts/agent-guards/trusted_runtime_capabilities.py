"""scripts/agent-guards/trusted_runtime_capabilities.py

Non-mutating, name-agnostic capability wrapper around
`skill_runtime_exec._resolve_trusted_executable` (Issue #2273 AC2/AC3/AC4/
AC12).

This module does not introduce a second trust boundary or a second version
literal: it delegates the actual resolution/validation logic to
`skill_runtime_exec`, which remains the single canonical resolver (Issue
#2241/#2251/#2276/#2280 decision record). This module exists only to give
callers *outside* `skill_runtime_exec` (e.g.
`scripts/claude-gpt/workflow_capability_preflight.py`) a stable, public,
import-friendly entry point without reaching into `skill_runtime_exec`'s
private (`_`-prefixed) internals directly, and without duplicating any of
the trust-boundary logic itself.

`check_trusted_uv` performs no filesystem writes, no git operations, and no
network calls of its own -- it only resolves an executable path via
`shutil.which` (read-only PATH lookup) and, for names other than `uv`,
`skill_runtime_exec` may additionally invoke `<resolved> --version` as a
read-only subprocess to confirm the version banner (see
`_validate_trusted_executable_version` in `skill_runtime_exec.py`). Neither
step mutates repository state, so this module is safe to call from
non-mutating preflight contexts (Issue #2273 AC12).
"""

from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent

# Status codes returned by `check_trusted_uv`. These intentionally mirror
# the vocabulary used by Issue #2273 AC3 ("trusted_uv_missing" /
# "trusted_uv_version_mismatch") rather than re-exposing
# `skill_runtime_exec`'s internal `RuntimeError` message strings verbatim,
# so callers have a small, stable enum instead of a set of implementation
# detail strings that could change if `skill_runtime_exec` is refactored.
STATUS_OK = "ok"
STATUS_MISSING = "trusted_uv_missing"
STATUS_VERSION_MISMATCH = "trusted_uv_version_mismatch"


def _load_skill_runtime_exec():
    """Import `skill_runtime_exec` as a plain module (not a package --
    `scripts/agent-guards` has no `__init__.py`, matching the existing
    import pattern used by
    `scripts/agent-guards/tests/test_trusted_toolchain_isolated_home.py`)."""
    guards_dir_str = str(_GUARDS_DIR)
    inserted = guards_dir_str not in sys.path
    if inserted:
        sys.path.insert(0, guards_dir_str)
    try:
        import skill_runtime_exec  # type: ignore  # noqa: PLC0415

        return skill_runtime_exec
    finally:
        if inserted:
            try:
                sys.path.remove(guards_dir_str)
            except ValueError:
                pass


def check_trusted_uv(project_root: str) -> dict:
    """Resolve `uv` via the canonical trust boundary and report a
    structured, JSON-serializable result.

    Returns a dict with keys:
      - status: one of `STATUS_OK` / `STATUS_MISSING` / `STATUS_VERSION_MISMATCH`
      - reason: short machine-readable detail string (never contains
        credential material -- this function only ever handles executable
        paths and version banners, never tokens or secrets)
      - resolved_path: the trust-validated absolute path to `uv`, or None
        if resolution failed
    """
    mod = _load_skill_runtime_exec()
    try:
        resolved = mod._resolve_trusted_executable("uv", project_root)  # noqa: SLF001
    except RuntimeError as exc:
        reason = str(exc)
        if reason == "uv_version_mismatch":
            return {"status": STATUS_VERSION_MISMATCH, "reason": reason, "resolved_path": None}
        return {"status": STATUS_MISSING, "reason": reason, "resolved_path": None}
    return {"status": STATUS_OK, "reason": "resolved", "resolved_path": resolved}
