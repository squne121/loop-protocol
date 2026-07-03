#!/usr/bin/env python3
"""
vc_baseline_shape_compiler.py

Deterministically detects non-canonical pytest baseline-fail Verification
Command (VC) shapes in an Issue contract body and, where a safe rewrite is
possible, produces a machine-readable ``suggested_command`` in canonical
form (a node-id pointing at a test file that does not exist yet on the
baseline: ``missing_new_test_file.py::test_name``).

Forbidden (non-canonical) shapes this compiler recognizes (Issue #1285):
  1. ``existing_test_file.py -k test_new_name`` where ``test_new_name`` is a
     single bare identifier that does not already exist in
     ``existing_test_file.py``.
  2. ``existing_test_file.py::test_missing_name`` where
     ``test_missing_name`` is a simple top-level function node-id that does
     not exist in ``existing_test_file.py`` (verified via ``ast.parse()``,
     not by executing pytest).

This compiler intentionally does NOT change the exit-code contract of
``baseline_vc_preflight.py`` (exit 4 + "file or directory not found" only is
expected_baseline_fail; exit 5 "no tests collected" remains a hard block).
It exists purely to help Issue authors avoid ever producing the forbidden
shapes above by rewriting them into the canonical missing-file node-id form
before contract-review runs baseline VC preflight.

Output schema (fixed, see Issue #1285 body):

```json
{
  "schema": "vc_baseline_shape_compiler/v1",
  "status": "changed | already_canonical | not_autofixable | invalid_input",
  "rewrites": [
    {
      "ac": null,
      "line_number": 12,
      "reason_code": "pytest_dash_k_new_test_on_existing_file",
      "original_command": "...",
      "suggested_command": "...",
      "confidence": "high"
    }
  ],
  "errors": [],
  "warnings": []
}
```

Exit codes (CLI mode):
  0: JSON emitted successfully (status may be changed / already_canonical /
     not_autofixable)
  2: invalid_input (e.g. ``## Verification Commands`` section missing)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Optional

SCHEMA = "vc_baseline_shape_compiler/v1"

STATUS_CHANGED = "changed"
STATUS_ALREADY_CANONICAL = "already_canonical"
STATUS_NOT_AUTOFIXABLE = "not_autofixable"
STATUS_INVALID_INPUT = "invalid_input"

REASON_DASH_K_NEW_TEST = "pytest_dash_k_new_test_on_existing_file"
REASON_MISSING_NODE_ID = "pytest_missing_node_id_on_existing_file"
REASON_DASH_K_COMPLEX = "pytest_dash_k_complex_expression"
REASON_COMPLEX_NODE_ID = "pytest_complex_node_id_on_existing_file"
REASON_NO_SAFE_CANDIDATE = "pytest_no_safe_missing_file_candidate"
REASON_AST_PARSE_FAILED = "pytest_ast_parse_failed"

# AC1 constraint: -k value must be a single bare `test_*` identifier. Anything
# else (boolean expression, class selector, parametrized selector, quoted
# complex expression) is not_autofixable (AC3).
_BARE_IDENTIFIER_RE = re.compile(r"^test_[A-Za-z0-9_]*$")
# AC2 constraint: node-id after `::` must be a simple top-level function name
# (no nested `::` for class/method, no `[` for parametrization).
_SIMPLE_NODE_ID_RE = re.compile(r"^test_[A-Za-z0-9_]*$")

_MAX_CANDIDATE_ATTEMPTS = 20


# ─── Body / section parsing (self-contained; no cross-skill import) ─────────


def extract_section_lines(lines: list[str], heading: str) -> tuple[int, int]:
    """Return (start_line_index, end_line_index_exclusive) for a ## heading section."""
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == heading:
            start = i
            break
    if start is None:
        return (-1, -1)
    for i in range(start + 1, len(lines)):
        if re.match(r"^## ", lines[i]):
            return (start, i)
    return (start, len(lines))


def parse_allowed_paths(lines: list[str]) -> list[str]:
    """Extract allowed paths from ## Allowed Paths section (empty list if missing)."""
    start, end = extract_section_lines(lines, "## Allowed Paths")
    if start == -1:
        return []
    paths = []
    for i in range(start + 1, end):
        line = lines[i].strip()
        if line.startswith("- "):
            paths.append(line[2:].strip().strip("`"))
    return paths


def _extract_vc_pytest_command_lines(
    lines: list[str], vc_start: int, vc_end: int
) -> list[tuple[int, str]]:
    """Return [(line_index, command_text)] for $-prefixed pytest command lines
    inside canonical fenced ```bash blocks within the VC section."""
    entries: list[tuple[int, str]] = []
    i = vc_start + 1
    in_block = False
    while i < vc_end:
        line = lines[i]
        if re.match(r"^```bash\s*$", line):
            in_block = True
            i += 1
            continue
        if in_block and re.match(r"^```\s*$", line):
            in_block = False
            i += 1
            continue
        if in_block:
            stripped = line.rstrip("\n").strip()
            if stripped.startswith("$ "):
                cmd = stripped[2:].strip()
                if "pytest" in shlex_safe_tokens(cmd):
                    entries.append((i, cmd))
        i += 1
    return entries


def shlex_safe_tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return []


# ─── pytest command parsing ──────────────────────────────────────────────────


def _parse_pytest_command(cmd: str) -> Optional[dict]:
    """Parse a pytest invocation into its structural parts.

    Returns None if `cmd` does not contain a recognizable `pytest` token.
    """
    tokens = shlex_safe_tokens(cmd)
    if "pytest" not in tokens:
        return None
    idx = tokens.index("pytest")
    prefix = tokens[: idx + 1]
    rest = tokens[idx + 1 :]

    path_arg: Optional[str] = None
    k_value: Optional[str] = None
    other_flags: list[str] = []

    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "-k":
            if i + 1 >= len(rest):
                return None
            k_value = rest[i + 1]
            i += 2
            continue
        if tok.startswith("-"):
            other_flags.append(tok)
            i += 1
            continue
        if path_arg is None:
            path_arg = tok
            i += 1
            continue
        other_flags.append(tok)
        i += 1

    if path_arg is None:
        return None

    return {
        "prefix": prefix,
        "path_arg": path_arg,
        "k_value": k_value,
        "other_flags": other_flags,
    }


def _collect_top_level_test_defs(py_path: Path) -> tuple[set[str], bool]:
    """Return (top_level_test_function_names, parse_ok)."""
    try:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return set(), False
    funcs: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            funcs.add(node.name)
    return funcs, True


def _synthesize_candidate(
    existing_path: Path,
    repo_root: Path,
    allowed_paths: set[str],
    used: set[str],
) -> Optional[str]:
    """Synthesize a canonical missing-file candidate path.

    Only returns a candidate that is (a) not already used in this run,
    (b) present verbatim in the Issue's Allowed Paths, and (c) does not
    already exist on the baseline (it must genuinely be a *missing* file so
    the resulting node-id is a real expected_baseline_fail).
    """
    try:
        rel_dir = existing_path.parent.relative_to(repo_root)
    except ValueError:
        rel_dir = existing_path.parent
    stem = existing_path.stem

    for attempt in range(1, _MAX_CANDIDATE_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f"_{attempt}"
        name = f"{stem}_new_test{suffix}.py"
        rel = str(rel_dir / name).replace("\\", "/")
        if rel in used:
            continue
        if rel not in allowed_paths:
            continue
        candidate_path = repo_root / rel
        if candidate_path.exists():
            continue
        return rel
    return None


def _rebuild_command(parsed: dict, new_path_arg: str) -> str:
    tokens = list(parsed["prefix"]) + [new_path_arg] + list(parsed["other_flags"])
    return " ".join(shlex.quote(t) if _needs_quote(t) else t for t in tokens)


def _needs_quote(token: str) -> bool:
    return bool(re.search(r"\s", token))


def classify_pytest_command(
    cmd: str,
    repo_root: Path,
    allowed_paths: set[str],
    used_candidates: set[str],
) -> Optional[dict]:
    """Classify a single pytest VC command line.

    Returns None when the command is out of scope for this compiler (not a
    forbidden shape), otherwise a dict with at least a "status" key.
    """
    parsed = _parse_pytest_command(cmd)
    if parsed is None:
        return None

    path_arg = parsed["path_arg"]
    k_value = parsed["k_value"]

    if "::" in path_arg:
        file_part, node_id = path_arg.split("::", 1)
    else:
        file_part, node_id = path_arg, None

    file_path = repo_root / file_part
    file_exists = file_path.is_file()

    # ── AC2 / AC4: node-id form ──────────────────────────────────────────
    if node_id is not None:
        if not file_exists:
            # missing_new_test_file.py::test_name → already canonical (AC4)
            return {"status": STATUS_ALREADY_CANONICAL}

        if not _SIMPLE_NODE_ID_RE.match(node_id):
            # class selector / parametrized selector on an existing file (AC3)
            return {"status": STATUS_NOT_AUTOFIXABLE, "reason_code": REASON_COMPLEX_NODE_ID}

        funcs, parse_ok = _collect_top_level_test_defs(file_path)
        if not parse_ok:
            return {"status": STATUS_NOT_AUTOFIXABLE, "reason_code": REASON_AST_PARSE_FAILED}

        if node_id in funcs:
            # Node-id already exists — not a baseline-fail shape, out of scope.
            return None

        candidate = _synthesize_candidate(file_path, repo_root, allowed_paths, used_candidates)
        if candidate is None:
            return {"status": STATUS_NOT_AUTOFIXABLE, "reason_code": REASON_NO_SAFE_CANDIDATE}
        used_candidates.add(candidate)
        suggested = _rebuild_command(parsed, f"{candidate}::{node_id}")
        return {
            "status": STATUS_CHANGED,
            "reason_code": REASON_MISSING_NODE_ID,
            "suggested_command": suggested,
        }

    # ── AC1 / AC3: -k form ───────────────────────────────────────────────
    if k_value is not None:
        if not _BARE_IDENTIFIER_RE.match(k_value):
            # boolean expression / class selector / parametrized selector /
            # quoted complex expression (AC3)
            return {"status": STATUS_NOT_AUTOFIXABLE, "reason_code": REASON_DASH_K_COMPLEX}

        if not file_exists:
            # -k against a nonexistent path is out of scope for this rule.
            return None

        funcs, parse_ok = _collect_top_level_test_defs(file_path)
        if not parse_ok:
            return {"status": STATUS_NOT_AUTOFIXABLE, "reason_code": REASON_AST_PARSE_FAILED}

        if k_value in funcs:
            # Selecting an existing test — not a baseline-fail shape.
            return None

        candidate = _synthesize_candidate(file_path, repo_root, allowed_paths, used_candidates)
        if candidate is None:
            return {"status": STATUS_NOT_AUTOFIXABLE, "reason_code": REASON_NO_SAFE_CANDIDATE}
        used_candidates.add(candidate)
        suggested = _rebuild_command(parsed, f"{candidate}::{k_value}")
        return {
            "status": STATUS_CHANGED,
            "reason_code": REASON_DASH_K_NEW_TEST,
            "suggested_command": suggested,
        }

    return None


def compile_body(body: str, repo_root: Path) -> dict:
    """Compile all pytest baseline-fail VC command lines in `body` into canonical form.

    Returns a dict matching the fixed `vc_baseline_shape_compiler/v1` schema.
    """
    lines = body.splitlines(keepends=True)
    vc_start, vc_end = extract_section_lines(lines, "## Verification Commands")
    if vc_start == -1:
        return {
            "schema": SCHEMA,
            "status": STATUS_INVALID_INPUT,
            "rewrites": [],
            "errors": ["## Verification Commands section not found"],
            "warnings": [],
        }

    allowed_paths = set(parse_allowed_paths(lines))
    command_entries = _extract_vc_pytest_command_lines(lines, vc_start, vc_end)

    rewrites: list[dict] = []
    warnings: list[str] = []
    used_candidates: set[str] = set()
    any_change = False
    any_not_autofixable = False

    for line_no, raw_cmd in command_entries:
        result = classify_pytest_command(raw_cmd, repo_root, allowed_paths, used_candidates)
        if result is None:
            continue
        if result["status"] == STATUS_ALREADY_CANONICAL:
            continue
        if result["status"] == STATUS_NOT_AUTOFIXABLE:
            any_not_autofixable = True
            warnings.append(
                f"line {line_no + 1}: not_autofixable ({result['reason_code']}): {raw_cmd}"
            )
            continue
        if result["status"] == STATUS_CHANGED:
            any_change = True
            rewrites.append(
                {
                    "ac": None,
                    "line_number": line_no + 1,
                    "reason_code": result["reason_code"],
                    "original_command": raw_cmd,
                    "suggested_command": result["suggested_command"],
                    "confidence": "high",
                }
            )

    if any_change:
        status = STATUS_CHANGED
    elif any_not_autofixable:
        status = STATUS_NOT_AUTOFIXABLE
    else:
        status = STATUS_ALREADY_CANONICAL

    return {
        "schema": SCHEMA,
        "status": status,
        "rewrites": rewrites,
        "errors": [],
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect and rewrite non-canonical pytest baseline-fail VC command "
            "shapes into canonical missing-file node-id form."
        )
    )
    parser.add_argument("--body-file", help="Input body file path (default: stdin)")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve VC file paths (default: cwd)",
    )
    args = parser.parse_args()

    if args.body_file:
        try:
            with open(args.body_file, "r", encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            print(f"[ERROR] Cannot read body file: {e}", file=sys.stderr)
            return 2
    else:
        body = sys.stdin.read()

    repo_root = Path(args.repo_root).resolve()
    result = compile_body(body, repo_root)
    print(json.dumps(result))

    if result["status"] == STATUS_INVALID_INPUT:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
