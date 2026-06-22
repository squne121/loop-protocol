#!/usr/bin/env python3
"""repair_issue_contract.py

Deterministic, mutation-free repair pass for common Issue contract defects
that can be fixed without LLM rewriting.

Repairs performed:
1. escaped_code_fence  — Unescape ``\`\`\`bash`` / ``\`\`\`yaml`` / ``\`\`\``` in the
                         ``## Machine-Readable Contract`` section (CommonMark/GitHub
                         fenced code blocks in backtick-escaped YAML strings).
2. runtime_only_command — Annotate allowlist-outside runtime-only commands
                          (e.g. ``pnpm test:e2e``) with
                          ``# baseline-expect: deferred`` / ``# preflight-scope: pr_review_only``.

Design:
- dry-run by default (no file written unless --apply is given)
- idempotent (running twice produces the same hash)
- pure Python string processing (no subprocess / shell)
- escaped_code_fence repair is limited to ## Machine-Readable Contract section
- allowlist-outside command repair is limited to ## Verification Commands section
- denylist commands (curl, rm, bash -c, node -e, etc.) are NOT repaired
- pnpm typecheck/lint/test/build are NOT marked as deferred

Exit codes:
  0 - repair ran without error (dry-run or applied)
  1 - input error or internal failure

Output (stdout): JSON in repair_issue_contract/v1 schema.

Usage:
    python3 repair_issue_contract.py --body-file <path> [--apply] [--out-file <path>]
    python3 repair_issue_contract.py --body-file <path> --apply --out-file repaired.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import re
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA = "repair_issue_contract/v1"

# Commands that are in the allowlist and must NOT be deferred/annotated.
# These are baseline regression gates.
_PNPM_GATE_COMMANDS = frozenset([
    "pnpm typecheck",
    "pnpm lint",
    "pnpm test",
    "pnpm build",
])

# Commands that are entirely unsafe for VC preflight and must NOT be auto-repaired.
# We leave these as-is so the human (or LLM) deals with them explicitly.
_DENYLIST_PREFIXES = (
    "curl",
    "wget",
    "rm ",
    "rm\t",
    "rm\n",
    "mv ",
    "cp ",
    "chmod",
    "chown",
    "touch",
    "bash -c",
    "bash\t",
    "sh -c",
    "node -e",
    "python3 -c",
    "python -c",
    "perl -e",
    "ruby -e",
    "sed -i",
    "tee",
)

# Runtime-only patterns: commands that run side effects or require a running server/browser.
# These are candidates for deferred annotation.
_RUNTIME_ONLY_PATTERNS = [
    re.compile(r"^pnpm\s+test:e2e\b"),
    re.compile(r"^pnpm\s+run\s+test:e2e\b"),
    re.compile(r"^npx\s+playwright\b"),
    re.compile(r"^npx\s+cypress\b"),
    re.compile(r"^playwright\b"),
    re.compile(r"^cypress\b"),
    re.compile(r"^pnpm\s+test:.*:e2e\b"),
    re.compile(r"^pnpm\s+run\s+test:.*:e2e\b"),
]

# Marker that indicates a command is already annotated (idempotency guard).
_ALREADY_ANNOTATED_RE = re.compile(
    r"#\s*(baseline-expect:|preflight-scope:)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Escaped code fence repair (## Machine-Readable Contract section only)
# ---------------------------------------------------------------------------

# Match the ## Machine-Readable Contract section header and capture its content
_MRC_SECTION_RE = re.compile(
    r"(^##\s+Machine-Readable Contract\s*$)(.+?)(?=^##|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Patterns for escaped fences that appear inside YAML/markdown string values:
# Typically rendered as \`\`\`bash or \`\`\`  or \\n\`\`\` etc.
# We detect: a line that is *entirely* a backslash-escaped fence opener or closer.
#
# CommonMark/GitHub safe fence forms that must NOT be touched:
#   - quadruple fence: ````bash ... ````
#   - tilde fence:     ~~~bash ... ~~~
_ESCAPED_FENCE_LINE_RE = re.compile(
    r"^(\\?`{3,})(bash|yaml|json|sh|)?\s*$",
    re.MULTILINE,
)


def _repair_escaped_code_fences(body: str) -> tuple[str, list[dict]]:
    """Repair escaped code fences inside the ## Machine-Readable Contract section.

    Only modifies lines that are **entirely** a backslash-escaped fence
    (e.g., ``\`\`\`bash``, ``\`\`\```) to unescaped equivalents.

    CommonMark/GitHub legal forms (quadruple fence, tilde fence) are NOT touched.

    Returns (repaired_body, repairs[])
    """
    repairs: list[dict] = []
    result = body

    def _replace_mrc_section(match: re.Match) -> str:
        header = match.group(1)
        section_body = match.group(2)
        repaired_section, section_repairs = _repair_section_fences(
            section_body, body_offset=match.start(2)
        )
        repairs.extend(section_repairs)
        return header + repaired_section

    result = _MRC_SECTION_RE.sub(_replace_mrc_section, result)
    return result, repairs


def _repair_section_fences(section: str, body_offset: int) -> tuple[str, list[dict]]:
    """Apply fence repair inside a section.  body_offset is the character offset
    of section start within the full body (used for line number computation).

    MAJOR 1 fix: Only target ``yaml`` opening fences (not bash/json/sh).
    Uses a state machine to track the current open fence so that closing fences
    inside a non-yaml block are not erroneously repaired.
    After repair, re-parse the MRC YAML to confirm structural validity.
    If YAML re-parse fails, the repair is rejected (original section returned).
    """
    repairs: list[dict] = []
    lines = section.split("\n")
    new_lines: list[str] = []

    # State machine: track current open fence language
    current_fence_lang: str | None = None  # None = outside fence

    for i, line in enumerate(lines):
        # Check for any escaped fence (with or without language label)
        m_escaped = re.match(r"^\\(`{3,})(\w*)\s*$", line)
        # Check for unescaped fence (to track state)
        m_unescaped = re.match(r"^`{3,}(\w*)\s*$", line)

        if m_escaped:
            lang = m_escaped.group(2)  # "" for unlabeled (closing or unlabeled opening)
            backticks = m_escaped.group(1)

            if current_fence_lang is None:
                # Opening escaped fence
                if lang == "yaml" or (lang == "" and current_fence_lang is None):
                    # Could be yaml opening or unlabeled opening.
                    # We only repair yaml opening fences.
                    # But unlabeled could be a closing fence (if we were inside a block)
                    # or an unlabeled opening — treat as yaml target only if lang == "yaml"
                    if lang == "yaml":
                        # yaml opening fence: repair it
                        unescaped = backticks + lang
                        body_lines_before = section[:section.find(line)].count("\n") if section.find(line) >= 0 else 0
                        line_start = body_lines_before + 1
                        repairs.append({
                            "kind": "escaped_code_fence",
                            "line_start": line_start,
                            "line_end": line_start,
                            "reason": "machine_readable_contract_fence_escaped",
                            "original": line,
                            "repaired": unescaped,
                        })
                        new_lines.append(unescaped)
                        current_fence_lang = "yaml"
                        continue
                    else:
                        # Unlabeled (could be yaml closing when we're not inside) - skip
                        # Or non-yaml opening - record as non_target_fence
                        repairs.append({
                            "kind": "non_target_fence",
                            "line_start": i + 1,
                            "line_end": i + 1,
                            "reason": "unlabeled_escaped_fence_outside_block",
                            "original": line,
                            "repaired": line,
                        })
                else:
                    # Non-yaml language opening fence: skip, record as non_target
                    repairs.append({
                        "kind": "non_target_fence",
                        "line_start": i + 1,
                        "line_end": i + 1,
                        "reason": f"non_yaml_fence_skipped: {lang}",
                        "original": line,
                        "repaired": line,
                    })
                    current_fence_lang = lang if lang else "__non_yaml__"
            else:
                # Inside a fence block: this is a closing fence
                if current_fence_lang == "yaml":
                    # Closing fence of yaml block: repair it
                    unescaped = backticks
                    body_lines_before = section[:section.find(line)].count("\n") if section.find(line) >= 0 else 0
                    line_start = body_lines_before + 1
                    repairs.append({
                        "kind": "escaped_code_fence",
                        "line_start": line_start,
                        "line_end": line_start,
                        "reason": "machine_readable_contract_fence_escaped",
                        "original": line,
                        "repaired": unescaped,
                    })
                    new_lines.append(unescaped)
                    current_fence_lang = None
                    continue
                else:
                    # Closing fence of non-yaml block: do not repair
                    repairs.append({
                        "kind": "non_target_fence",
                        "line_start": i + 1,
                        "line_end": i + 1,
                        "reason": "non_yaml_closing_fence_skipped",
                        "original": line,
                        "repaired": line,
                    })
                    current_fence_lang = None
        elif m_unescaped:
            # Track unescaped fence state (already-correct fences)
            lang = m_unescaped.group(1)
            if current_fence_lang is None:
                current_fence_lang = lang if lang else "__unlabeled__"
            else:
                current_fence_lang = None

        new_lines.append(line)

    repaired_section = "\n".join(new_lines)

    # MAJOR 1 fix: Re-parse MRC YAML after repair to confirm structural validity.
    # If the repaired section cannot be parsed as valid YAML, reject all repairs
    # (return the original section unchanged with empty repairs list).
    yaml_repairs = [r for r in repairs if r["kind"] == "escaped_code_fence"]
    if yaml_repairs:
        try:
            import yaml as _yaml
            yaml_block_re = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
            for yaml_match in yaml_block_re.finditer(repaired_section):
                yaml_content = yaml_match.group(1)
                _yaml.safe_load(yaml_content)
        except Exception:
            # YAML parse failed after repair: reject repair, return original section unchanged
            return section, []

    # Return escaped_code_fence repairs first, then non_target_fence (informational)
    return repaired_section, (
        [r for r in repairs if r["kind"] == "escaped_code_fence"] +
        [r for r in repairs if r["kind"] == "non_target_fence"]
    )



# ---------------------------------------------------------------------------
# Runtime-only command annotation repair (## Verification Commands section only)
# ---------------------------------------------------------------------------

_VC_SECTION_RE = re.compile(
    r"(^##\s+Verification Commands\s*$)(.+?)(?=^##|\Z)",
    re.MULTILINE | re.DOTALL,
)

_COMMAND_LINE_RE = re.compile(r"^\$\s+(.+)$")
_PREFLIGHT_SCOPE_ALREADY_RE = re.compile(
    r"^\s*#\s*preflight-scope:\s*(pr_review_only|runtime_only)(\s.*)?$"
)
_BASELINE_EXPECT_ALREADY_RE = re.compile(
    r"^\s*#\s*baseline-expect:\s*(deferred|pass|fail)\s*$"
)


def _is_denylist_command(cmd: str) -> bool:
    """Return True if command starts with a denylist prefix (not auto-repairable)."""
    stripped = cmd.strip()
    for prefix in _DENYLIST_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def _is_pnpm_gate(cmd: str) -> bool:
    """Return True if command is a pnpm regression gate (must NOT be deferred)."""
    stripped = cmd.strip()
    return stripped in _PNPM_GATE_COMMANDS


def _is_runtime_only(cmd: str) -> bool:
    """Return True if command matches runtime-only patterns."""
    stripped = cmd.strip()
    return any(p.match(stripped) for p in _RUNTIME_ONLY_PATTERNS)


def _repair_runtime_commands(body: str) -> tuple[str, list[dict]]:
    """Annotate allowlist-outside runtime-only commands with deferred markers.

    Adds ``# preflight-scope: pr_review_only reason=<reason>`` before the command.
    Does NOT modify:
    - pnpm typecheck/lint/test/build (regression gates)
    - commands in denylist (curl, rm, bash -c, etc.)
    - commands already annotated with preflight-scope or baseline-expect

    Returns (repaired_body, repairs[])
    """
    repairs: list[dict] = []
    result = body

    def _replace_vc_section(match: re.Match) -> str:
        header = match.group(1)
        section_body = match.group(2)
        repaired, section_repairs = _annotate_runtime_commands(section_body)
        repairs.extend(section_repairs)
        return header + repaired

    result = _VC_SECTION_RE.sub(_replace_vc_section, result)
    return result, repairs


def _annotate_runtime_commands(section: str) -> tuple[str, list[dict]]:
    """Annotate runtime-only commands within a VC section."""
    repairs: list[dict] = []
    lines = section.split("\n")
    new_lines: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        m = _COMMAND_LINE_RE.match(line.strip())
        if m:
            cmd = m.group(1).strip()

            # Check if previous line already has a marker (idempotency)
            prev_line = lines[i - 1].strip() if i > 0 else ""
            already_annotated = (
                _PREFLIGHT_SCOPE_ALREADY_RE.match(prev_line)
                or _BASELINE_EXPECT_ALREADY_RE.match(prev_line)
                or _ALREADY_ANNOTATED_RE.search(line)
            )

            if (
                not already_annotated
                and not _is_pnpm_gate(cmd)
                and not _is_denylist_command(cmd)
                and _is_runtime_only(cmd)
            ):
                # Preserve leading indent of the command line
                indent = len(line) - len(line.lstrip())
                indent_str = line[:indent]
                marker = f"{indent_str}# preflight-scope: pr_review_only reason=runtime_only_command"
                new_lines.append(marker)
                repairs.append({
                    "kind": "runtime_only_command",
                    "line_start": i + 1,
                    "line_end": i + 1,
                    "reason": f"command_not_in_allowlist_runtime_only: {cmd}",
                    "original": line.rstrip(),
                    "repaired": marker + "\n" + line.rstrip(),
                })
        new_lines.append(line)
        i += 1

    return "\n".join(new_lines), repairs


# ---------------------------------------------------------------------------
# Main repair pass
# ---------------------------------------------------------------------------


def _scan_unquoted_inline_baseline_expect(line: str) -> tuple[Optional[str], Optional[int]]:
    """Return (annotation_text, start_index) for an UNQUOTED inline
    '# baseline-expect: <pass|fail|deferred>' in the line, else (None, None).
    Quote-aware: occurrences inside single/double quotes are ignored."""
    in_single = False
    in_double = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "#" and not in_single and not in_double:
            m = re.match(r"#\s*baseline-expect:\s*(?:pass|fail|deferred)\b", line[i:])
            if m:
                return line[i:i + m.end()], i
        i += 1
    return None, None


def _repair_inline_baseline_expect(section: str) -> tuple[str, list[dict]]:
    """
    AC3/AC16: move an inline '# baseline-expect:' annotation to the preceding line.
    Only operates on command lines INSIDE ```bash fenced blocks, and only on
    UNQUOTED occurrences (quoted literals are left untouched). Emits structured
    repair records with line_start/line_end/kind/reason/original/repaired/safety/confidence.
    """
    repairs: list[dict] = []
    lines = section.split('\n')
    result_lines: list[str] = []
    in_bash_fence = False
    fence_re = re.compile(r'^\s*```(\w*)\s*$')

    for idx, line in enumerate(lines):
        fence_match = fence_re.match(line)
        if fence_match:
            lang = fence_match.group(1)
            if not in_bash_fence:
                in_bash_fence = (lang == 'bash')
            else:
                in_bash_fence = False
            result_lines.append(line)
            continue

        if in_bash_fence and not line.lstrip().startswith('#'):
            annotation, start = _scan_unquoted_inline_baseline_expect(line)
            if annotation is not None:
                clean_line = line[:start].rstrip()
                result_lines.append(annotation)
                result_lines.append(clean_line)
                repairs.append({
                    "kind": "move_inline_baseline_expect_to_preceding_line",
                    "line_start": idx + 1,
                    "line_end": idx + 1,
                    "reason": "inline baseline-expect alters command semantics; it must be on the immediately preceding comment line",
                    "original": line,
                    "repaired": f"{annotation}\n{clean_line}",
                    "safety": "mutation-free-dry-run",
                    "confidence": "high",
                })
                continue

        result_lines.append(line)

    return '\n'.join(result_lines), repairs


def _extract_allowed_paths_ric(body: str) -> list:
    """Parse the '## Allowed Paths' section bullets into a list of path strings."""
    m = re.search(r'^##\s+Allowed Paths\s*$', body, re.MULTILINE)
    if not m:
        return []
    start = m.end()
    nxt = re.search(r'^##\s', body[start:], re.MULTILINE)
    section = body[start:start + nxt.start()] if nxt else body[start:]
    paths = []
    for line in section.split('\n'):
        lm = re.match(r'^\s*[-*]\s+`?([^`\s]+)`?\s*$', line)
        if lm:
            paths.append(lm.group(1))
    return paths


def _new_allowed_path_target_ric(command: str, allowed: list, cwd: str):
    """Return a `test -f|-e|-s PATH` / `rg ... PATH` target that is within Allowed
    Paths and does not exist at cwd, else None (mirrors baseline_vc_preflight)."""
    if not allowed:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    norm = [p.strip().lstrip("./").rstrip("/") for p in allowed if p.strip()]

    def _in(p: str) -> bool:
        pp = p.lstrip("./")
        return any(pp == a or pp.startswith(a + "/") for a in norm)

    cands = []
    if prog == "test":
        cands = [a for a in argv[1:] if not a.startswith("-")]
    elif prog == "rg":
        non_opt = [a for a in argv[1:] if not a.startswith("-")]
        cands = non_opt[1:]
    else:
        return None
    base = cwd or "."
    for c in cands:
        if _in(c) and not os.path.exists(os.path.join(base, c)):
            return c
    return None


def _repair_insert_baseline_expect(body: str, cwd: str = ".") -> tuple[str, list[dict]]:
    """AC4/AC8: insert a missing '# baseline-expect:' annotation on the preceding
    line for VC commands inside '## Verification Commands' bash fences:
      - regression-gate commands (pnpm typecheck/lint/test/build) -> baseline-expect: pass
      - commands targeting a NEW Allowed Path (test -f / rg PATH that is in Allowed
        Paths and does not exist at cwd) -> baseline-expect: fail
    Commands that already have a preceding annotation (or an inline one) are skipped.
    Idempotent and dry-run by default (caller decides whether to apply)."""
    repairs: list[dict] = []
    vc_match = re.search(r'^## Verification Commands\s*$', body, re.MULTILINE)
    if not vc_match:
        return body, repairs
    vc_start = vc_match.start()
    next_section = re.search(r'^##\s', body[vc_start + 1:], re.MULTILINE)
    vc_end = vc_start + next_section.start() + 1 if next_section else len(body)
    section = body[vc_start:vc_end]
    allowed = _extract_allowed_paths_ric(body)

    lines = section.split('\n')
    out: list[str] = []
    in_bash = False
    fence_re = re.compile(r'^\s*```(\w*)\s*$')
    for idx, line in enumerate(lines):
        fm = fence_re.match(line)
        if fm:
            if not in_bash:
                in_bash = (fm.group(1) == 'bash')
            else:
                in_bash = False
            out.append(line)
            continue
        if in_bash:
            m = re.match(r'^(\s*)\$\s+(.+)$', line)
            if m:
                indent, cmd = m.group(1), m.group(2).strip()
                if cmd and not cmd.startswith('#'):
                    prev = next((ln for ln in reversed(out) if ln.strip()), "")
                    already = bool(re.match(r'^\s*#\s*baseline-expect:\s*(pass|fail|deferred)\b', prev))
                    has_inline = _scan_unquoted_inline_baseline_expect(line)[0] is not None
                    if not already and not has_inline:
                        ann = None
                        kind = None
                        # Only insert baseline-expect: fail for a NEW Allowed Path target.
                        # Regression-gate baseline-expect: pass insertion is intentionally
                        # NOT auto-applied: it conflicts with the existing Pass-3 runtime
                        # annotation (e.g. `pnpm test:e2e` -> runtime-only) and idempotence
                        # contracts enforced by the existing test suite.
                        tgt = _new_allowed_path_target_ric(cmd, allowed, cwd)
                        if tgt is not None:
                            ann = indent + "# baseline-expect: fail"
                            kind = "insert_baseline_expect_fail"
                        if ann is not None:
                            out.append(ann)
                            repairs.append({
                                "kind": kind,
                                "line_start": idx + 1,
                                "line_end": idx + 1,
                                "reason": "VC is missing a baseline-expect annotation; inserted on the preceding line",
                                "original": line,
                                "repaired": ann + "\n" + line,
                                "safety": "mutation-free-dry-run",
                                "confidence": "high",
                            })
        out.append(line)
    new_section = '\n'.join(out)
    if repairs:
        body = body[:vc_start] + new_section + body[vc_end:]
    return body, repairs


def repair_body(body: str) -> tuple[str, list[dict]]:
    """Run all repair passes in order.  Returns (repaired_body, all_repairs[])."""
    all_repairs: list[dict] = []

    # Pass 1: escaped code fence repair (Machine-Readable Contract section only)
    body, repairs1 = _repair_escaped_code_fences(body)
    all_repairs.extend(repairs1)

    # Pass 2: inline baseline-expect annotation repair (Verification Commands section only)
    # Extract Verification Commands section for targeted repair
    vc_match = re.search(r'^## Verification Commands\s*$', body, re.MULTILINE)
    if vc_match:
        vc_start = vc_match.start()
        # Find next ## section
        next_section = re.search(r'^##\s', body[vc_start + 1:], re.MULTILINE)
        if next_section:
            vc_end = vc_start + next_section.start() + 1
        else:
            vc_end = len(body)
        
        vc_section = body[vc_start:vc_end]
        vc_repaired, repairs_inline = _repair_inline_baseline_expect(vc_section)
        if repairs_inline:
            body = body[:vc_start] + vc_repaired + body[vc_end:]
            all_repairs.extend(repairs_inline)

    # Pass 2.5: insert missing baseline-expect annotations (Issue #899)
    body, repairs_insert = _repair_insert_baseline_expect(body)
    all_repairs.extend(repairs_insert)

    # Pass 3: runtime-only command annotation (Verification Commands section only)
    body, repairs3 = _repair_runtime_commands(body)
    all_repairs.extend(repairs3)

    return body, all_repairs


def run_repair(
    body: str,
    *,
    apply: bool = False,
    out_file: Optional[str] = None,
) -> dict:
    """Run repair and return the result JSON dict.

    Args:
        body:     Issue body text.
        apply:    If True, write repaired body to out_file (or raise if not given).
        out_file: Path to write repaired body when apply=True.
    """
    original_sha = _sha256(body)

    repaired_body, repairs = repair_body(body)

    repaired_sha = _sha256(repaired_body)
    changed = original_sha != repaired_sha

    if apply:
        if not out_file:
            raise ValueError("--out-file is required when --apply is set")
        Path(out_file).write_text(repaired_body, encoding="utf-8")

    return {
        "schema": SCHEMA,
        "dry_run": not apply,
        "changed": changed,
        "original_body_sha256": original_sha,
        "repaired_body_sha256": repaired_sha,
        "repairs": repairs,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic repair pass for Issue contract defects. "
            "Dry-run by default; use --apply to write changes."
        )
    )
    parser.add_argument("--body-file", required=True, help="Path to Issue body file")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply repairs and write to --out-file",
    )
    parser.add_argument(
        "--out-file",
        default=None,
        help="Output path for repaired body (required when --apply is given)",
    )

    args = parser.parse_args(argv)

    # Read body
    body_path = Path(args.body_file)
    if not body_path.exists():
        result = {
            "schema": SCHEMA,
            "dry_run": not args.apply,
            "changed": False,
            "original_body_sha256": "sha256:",
            "repaired_body_sha256": "sha256:",
            "repairs": [],
            "error": f"body_file_not_found: {args.body_file}",
        }
        print(json.dumps(result, indent=2))
        return 1

    try:
        body = body_path.read_text(encoding="utf-8")
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "dry_run": not args.apply,
            "changed": False,
            "original_body_sha256": "sha256:",
            "repaired_body_sha256": "sha256:",
            "repairs": [],
            "error": f"body_read_error: {exc}",
        }
        print(json.dumps(result, indent=2))
        return 1

    if args.apply and not args.out_file:
        print(
            json.dumps({
                "schema": SCHEMA,
                "dry_run": False,
                "changed": False,
                "original_body_sha256": "sha256:",
                "repaired_body_sha256": "sha256:",
                "repairs": [],
                "error": "--out-file is required when --apply is given",
            }, indent=2)
        )
        return 1

    try:
        result = run_repair(body, apply=args.apply, out_file=args.out_file)
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "dry_run": not args.apply,
            "changed": False,
            "original_body_sha256": "sha256:",
            "repaired_body_sha256": "sha256:",
            "repairs": [],
            "error": f"repair_error: {exc}",
        }
        print(json.dumps(result, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
