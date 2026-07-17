#!/usr/bin/env python3
"""Validate `.claude/settings.json` permissions.allow/ask/deny shape (Issue #1551).

repository policy: `claude_permission_edit_rule_canonicalization_v1`
  loop-protocol の permissions.allow / ask / deny では、path-scoped file
  mutation rule を Edit(...) に正規化し、scoped Write(...) を使用しない。

Scope (narrow, intentional):
  - Only `permissions.allow` / `permissions.ask` / `permissions.deny` arrays
    are inspected. `hooks` matcher strings (e.g. ``"Write|Edit"``) and any
    other top-level key are NOT inspected.
  - A scoped ``"Write(<specifier>)"`` entry inside allow/ask/deny is a
    repository-policy violation. A bare ``"Write"`` entry is accepted.
  - This validator does NOT claim runtime behavior change.

safety_claim:
  change_kind: configuration_canonicalization
  runtime_behavior_change_claimed: false
  bash_filesystem_boundary_claimed: false

Exit codes:
  0 — valid (no policy violation, well-formed shape)
  1 — policy violation (scoped Write(...) rule found in allow/ask/deny)
  2 — parse / shape error (malformed JSON, non-object root, non-string entry, ...)

Diagnostics are emitted as JSON-path-like strings, e.g. ``permissions.deny[4]``.
No secret values or unrelated configuration content are echoed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PERMISSION_ARRAY_KEYS = ("allow", "ask", "deny")

# Matches a scoped Write(...) rule, e.g. Write(assets/**), Write(.env)
# Does NOT match bare "Write" and does NOT match hooks matcher strings such
# as "Write|Edit" (no parenthesis specifier).
_SCOPED_WRITE_RE = re.compile(r"^Write\(.*\)$")


class SettingsPermissionsError(Exception):
    """Raised for parse / shape errors (exit code 2)."""


def load_settings(path: Path) -> dict:
    """Load and JSON-parse the settings file. Fail-closed on any parse error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsPermissionsError(f"failed to read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsPermissionsError(f"malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsPermissionsError("root value must be a JSON object")
    return data


def _validate_array_shape(container: dict, key: str, path_prefix: str) -> list[str]:
    """Validate that container[key], if present, is a list[str].

    Returns the list of string entries. Raises SettingsPermissionsError if the
    key is present but is not a list, or contains non-string elements.
    """
    if key not in container:
        return []
    value = container[key]
    if not isinstance(value, list):
        raise SettingsPermissionsError(f"{path_prefix}.{key} must be an array")
    for idx, entry in enumerate(value):
        if not isinstance(entry, str):
            raise SettingsPermissionsError(
                f"{path_prefix}.{key}[{idx}] must be a string, got {type(entry).__name__}"
            )
    return value


def find_scoped_write_violations(data: dict) -> list[str]:
    """Return a list of diagnostic strings for scoped Write(...) rules.

    Only inspects permissions.allow / permissions.ask / permissions.deny.
    Raises SettingsPermissionsError for shape violations (fail-closed).
    """
    permissions = data.get("permissions")
    if permissions is None:
        # No permissions block at all is a valid (if unusual) shape; nothing
        # to check.
        return []
    if not isinstance(permissions, dict):
        raise SettingsPermissionsError("permissions must be an object")

    violations: list[str] = []
    for key in PERMISSION_ARRAY_KEYS:
        entries = _validate_array_shape(permissions, key, "permissions")
        for idx, entry in enumerate(entries):
            if _SCOPED_WRITE_RE.match(entry):
                violations.append(f"permissions.{key}[{idx}]")
    return violations


def run_validation(settings_path: Path) -> tuple[int, list[str]]:
    """Run the full validation. Returns (exit_code, diagnostics)."""
    try:
        data = load_settings(settings_path)
        violations = find_scoped_write_violations(data)
    except SettingsPermissionsError as exc:
        return 2, [str(exc)]

    if violations:
        return 1, violations
    return 0, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that .claude/settings.json permissions.allow/ask/deny "
            "contain no scoped Write(path) rules (repository policy: "
            "claude_permission_edit_rule_canonicalization_v1)."
        )
    )
    parser.add_argument(
        "--settings",
        required=True,
        help="Path to the settings.json file to validate (e.g. .claude/settings.json)",
    )
    args = parser.parse_args(argv)

    settings_path = Path(args.settings)
    exit_code, diagnostics = run_validation(settings_path)

    if exit_code == 0:
        print("OK: no scoped Write(path) rule found in permissions.allow/ask/deny")
    elif exit_code == 1:
        print("POLICY VIOLATION: scoped Write(path) rule(s) found:", file=sys.stderr)
        for diag in diagnostics:
            print(f"  - {diag}", file=sys.stderr)
    else:
        print("PARSE/SHAPE ERROR:", file=sys.stderr)
        for diag in diagnostics:
            print(f"  - {diag}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
