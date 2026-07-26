#!/usr/bin/env python3
"""audit_agy_auth_surface.py - static auth-surface / read-only-enforcement audit.

Issue #1778 (parent: #1265, source: adversarial re-audit of CLOSED #1494).

This script performs a **static, read-only** analysis of
`agy_permission_policy.py` (never `agy_permission_policy.py` itself is
edited, and never imported for its side effects -- only its own source text
is parsed). It detects two independent things and emits both as
`AGY_CAUSAL_CLAIM_MANIFEST_V1` findings:

1. **Auth reachability surface enumeration** (`kind: auth_surface`): every
   env var / symlink exposure `materialize_isolated_agy_workspace()` grants
   the isolated `agy` subprocess a path back to the real host's credential
   material or credential-adjacent state
   (`DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` /
   `GOOGLE_APPLICATION_CREDENTIALS` / `gcloud_adc_path` /
   `agy_oauth_token_path`), plus whether a nearby comment documents *why*
   that particular surface needs to be exposed (`rationale_present`).

2. **Unenforced "read_only"-named functions** (`kind: unenforced_read_only`,
   `severity: p0`): any function whose name contains `read_only` but whose
   body never calls an OS-level read-only enforcement primitive (`bwrap`,
   `chmod`, or a `mount`-family syscall/API) -- i.e. a function whose name
   *claims* an OS-enforced read-only boundary while its implementation is
   actually just `Path.symlink_to()` (a bare filesystem pointer with no
   enforcement of its own).

This is a read-only auditor: it never modifies `agy_permission_policy.py`
and always exits 0 (findings are informational; the fail-closed drift check
lives in `scripts/check_agy_causal_claim_drift.py`, not here). Exit codes:

  0 = analysis completed (regardless of whether findings were produced)
  2 = usage / input error (target file missing or unparsable)

Design references:
- Issue #1778 Source items 1-2
- `.claude/skills/gemini-cli-headless-delegation/scripts/agy_permission_policy.py`
- `.claude/skills/gemini-cli-headless-delegation/schemas/agy_causal_claim_manifest_v1.schema.json`
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_ID = "AGY_CAUSAL_CLAIM_MANIFEST_V1"
PRODUCER = "audit_agy_auth_surface"

_DEFAULT_TARGET = (
    ".claude/skills/gemini-cli-headless-delegation/scripts/agy_permission_policy.py"
)

# ---------------------------------------------------------------------------
# 1. Auth reachability surface enumeration
# ---------------------------------------------------------------------------

# Each surface is detected by a distinct, literal marker that only appears in
# the source when `materialize_isolated_agy_workspace()` actually grants that
# specific reachability path. These markers are the same identifiers the
# function itself uses (env var name string literals, or the read-only
# exposure helper function names) -- not an independent guess at behavior.
_AUTH_SURFACES: tuple[tuple[str, str], ...] = (
    ("DBUS_SESSION_BUS_ADDRESS", '"DBUS_SESSION_BUS_ADDRESS"'),
    ("XDG_RUNTIME_DIR", '"XDG_RUNTIME_DIR"'),
    ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS_ENV"),
    ("gcloud_adc_path", "_expose_gcloud_adc_read_only("),
    ("agy_oauth_token_path", "_expose_agy_oauth_token_read_only("),
)

_RATIONALE_WINDOW = 20  # lines to look backward for an explanatory comment


def _find_line_of(source_lines: list[str], marker: str) -> "int | None":
    for idx, line in enumerate(source_lines):
        if marker in line:
            return idx  # 0-based
    return None


def _rationale_present(source_lines: list[str], marker_line_idx: int) -> bool:
    """Return True if a non-trivial comment block precedes marker_line_idx.

    Heuristic: scan up to `_RATIONALE_WINDOW` lines backward for contiguous
    `#`-prefixed comment lines. "Non-trivial" means the collected comment
    text (stripped of `#` and whitespace) has more than 40 characters --
    long enough to be an explanation, not just a one-word label.
    """
    collected: list[str] = []
    idx = marker_line_idx
    steps = 0
    while idx >= 0 and steps < _RATIONALE_WINDOW:
        stripped = source_lines[idx].strip()
        if stripped.startswith("#"):
            collected.append(stripped.lstrip("#").strip())
        elif stripped == "":
            pass
        else:
            break
        idx -= 1
        steps += 1
    return len(" ".join(collected)) > 40


def _detect_auth_surfaces(source: str, source_path: str) -> list[dict[str, Any]]:
    source_lines = source.splitlines()
    findings: list[dict[str, Any]] = []
    for surface_name, marker in _AUTH_SURFACES:
        line_idx = _find_line_of(source_lines, marker)
        if line_idx is None:
            # Surface no longer present in the source -- do not fabricate a
            # finding for something that is not actually there.
            continue
        rationale = _rationale_present(source_lines, line_idx)
        findings.append(
            {
                "finding_id": f"auth_surface:{surface_name}",
                "kind": "auth_surface",
                "identifier": surface_name,
                "detail": (
                    f"materialize_isolated_agy_workspace() exposes the "
                    f"{surface_name} auth-reachability surface to the "
                    f"isolated agy subprocess."
                ),
                "severity": "info",
                "rationale_present": rationale,
                "source_file": source_path,
                "line": line_idx + 1,
            }
        )
    return findings


# ---------------------------------------------------------------------------
# 2. Unenforced "read_only"-named functions (P0 detection)
# ---------------------------------------------------------------------------

_OS_ENFORCEMENT_PATTERN = re.compile(
    r"\bbwrap\b|\bchmod\b|\.chmod\s*\(|\bos\.chmod\b|\bmount\s*\(|\bos\.mount\b",
    re.IGNORECASE,
)


def _detect_unenforced_read_only_functions(
    source: str, source_path: str
) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source, filename=source_path)
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise ValueError(f"could not parse {source_path}: {exc}") from exc

    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name_lower = node.name.lower()
        if "read_only" not in name_lower and "read-only" not in name_lower:
            continue
        segment = ast.get_source_segment(source, node) or ""
        enforced = bool(_OS_ENFORCEMENT_PATTERN.search(segment))
        if enforced:
            continue
        findings.append(
            {
                "finding_id": f"unenforced_read_only:{node.name}",
                "kind": "unenforced_read_only",
                "identifier": node.name,
                "detail": (
                    f"{node.name}() has 'read_only' in its name but its body "
                    f"contains no OS-level read-only enforcement call "
                    f"(bwrap / chmod / mount); it only calls "
                    f"Path.symlink_to() or equivalent, so the read-only "
                    f"boundary is not actually enforced by the OS."
                ),
                "severity": "p0",
                "source_file": source_path,
                "line": node.lineno,
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------


def build_manifest(target_file: Path, repo_relative: str) -> dict[str, Any]:
    source = target_file.read_text(encoding="utf-8")
    findings = _detect_auth_surfaces(source, repo_relative)
    findings.extend(_detect_unenforced_read_only_functions(source, repo_relative))

    summary: dict[str, int] = {}
    for finding in findings:
        summary[finding["kind"]] = summary.get(finding["kind"], 0) + 1

    status = "findings_detected" if findings else "ok"
    return {
        "schema": SCHEMA_ID,
        "producer": PRODUCER,
        "target_files": [repo_relative],
        "findings": findings,
        "summary": summary,
        "status": status,
    }


def _repo_relative(path: Path) -> str:
    """Best-effort repo-relative path string for manifest output."""
    resolved = path.resolve()
    parts = resolved.parts
    try:
        idx = parts.index(".claude")
        return "/".join(parts[idx:])
    except ValueError:
        return str(path)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=_DEFAULT_TARGET,
        help="Path to agy_permission_policy.py (repo-relative or absolute).",
    )
    args = parser.parse_args(argv)

    target_path = Path(args.target)
    if not target_path.is_absolute():
        # Resolve relative to CWD first (repo root when invoked normally);
        # fall back to resolving relative to this script's own repo layout.
        if not target_path.exists():
            script_repo_relative = (
                Path(__file__).resolve().parents[3] / args.target
            )
            if script_repo_relative.exists():
                target_path = script_repo_relative

    if not target_path.exists():
        print(
            json.dumps(
                {
                    "schema": SCHEMA_ID,
                    "producer": PRODUCER,
                    "error": f"target file not found: {args.target}",
                }
            )
        )
        return 2

    try:
        manifest = build_manifest(target_path, args.target)
    except ValueError as exc:
        print(
            json.dumps(
                {"schema": SCHEMA_ID, "producer": PRODUCER, "error": str(exc)}
            )
        )
        return 2

    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
