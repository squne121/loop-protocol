#!/usr/bin/env python3
"""Governed Python invocation policy checker (Issue #1193).

Scans governed surfaces for dependency-bearing Python invocations that do not
comply with the project's ``uv run --locked`` policy.

Governed surfaces (path globs only — AC1/AC4b):
  - .github/workflows/**/*.yml and .github/actions/**/*.yml  (run: blocks)
  - docs/dev/**/*.md                                          (fenced code blocks)
  - .claude/skills/**/SKILL.md                               (fenced code blocks)
  - package.json                                             (scripts values)

Hardening (OWNER REQUEST_CHANGES, AC14-AC18):
  - Direct ``python3 -`` (heredoc/stdin) and ``python3 -c`` are no longer
    allowed by a broad prefix exception. Their heredoc body / code string is
    AST import-scanned; any non-stdlib import (excluding recursively
    stdlib-only repo-local modules) is a violation (AC14/AC4b).
  - Every simple command in a line / ``run:`` block is classified. The line is
    split on ``&&`` / ``||`` / ``;`` / ``|`` (quote- and substitution-aware) and
    each segment is classified (AC15).
  - Command / process substitution is decomposed and its inner commands are
    recursively classified so a hidden invocation cannot escape detection.
    Unbalanced / unparseable shell grammar (and ``shlex.split`` failure on a
    launcher-bearing segment) is reported fail-closed as
    ``unsupported_shell_grammar``; the old ``.split()`` fallback is removed
    (AC16).
  - Direct interpreter exceptions match the *complete* argv token list exactly
    (no prefix / glob / regex); ``scope: stdlib_only`` script exceptions are
    additionally proven by AST import scan of the target (AC4a/AC4d/AC17).

No external shell parser (bashlex / mvdan-sh) is used; the conservative custom
splitter fails closed on grammar it does not support (rationale recorded in
docs/dev/test-lane-policy.md).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURE_PREFIX = "scripts/ci/fixtures/python_invocation_policy"
TEST_FILE_EXCL = "scripts/ci/tests/test_python_invocation_policy.py"
CHECKER_FILE_EXCL = "scripts/ci/check_python_invocation_policy.py"
EXCEPTIONS_PATH = "scripts/ci/python_invocation_policy_exceptions.json"

SURFACE_GLOBS = [
    ".github/workflows/**/*.yml",
    ".github/workflows/**/*.yaml",
    ".github/actions/**/*.yml",
    ".github/actions/**/*.yaml",
    "docs/dev/**/*.md",
    "package.json",
]

SKILL_MD_PATTERN = ".claude/skills/**/SKILL.md"

_RE_FENCE_OPEN = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`~]*)\s*$")
_RE_FENCE_CLOSE_PREFIX = re.compile(r"^(\s*)(`{3,}|~{3,})\s*$")

# Only fenced blocks whose info-string is a shell language (or untagged) are
# scanned as shell. Blocks tagged yaml/json/python/markdown/etc. are prose or
# data examples, not invocations, and must not be parsed as shell commands.
SHELL_FENCE_LANGS = {
    "", "bash", "sh", "shell", "console", "shell-session", "shellsession", "zsh",
}
_RE_POLICY_EXAMPLE_COMMENT = re.compile(r"<!--\s*policy-example\s*-->", re.IGNORECASE)

# Heredoc detection: `<<DELIM`, `<<'DELIM'`, `<<-DELIM`, `<<"DELIM"`.
_RE_HEREDOC_START = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")

_RE_LAUNCHER = re.compile(r"(?<![\w./-])(?:python3?|uv)(?![\w-])")

# stdlib module names (recursive proof uses this as the allow-set).
_STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {"__future__"}


# ---------------------------------------------------------------------------
# Violation / Result types
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    file: str
    line_num: int
    line_text: str
    violation_type: str
    suggestion: str | None = None


@dataclass
class CheckResult:
    violations: list[Violation] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    exceptions_loaded: int = 0
    surface_count: int = 0


# ---------------------------------------------------------------------------
# Exceptions registry (AC4a / AC4c / AC4d / AC17)
# ---------------------------------------------------------------------------

EXCEPTION_ALLOWED_SCOPES = ("stdlib_only", "bootstrap")
EXCEPTION_ALLOWED_PROOFS = (
    "stdlib_import_scan",
    "code_hash",
    "heredoc_body_ast_scan",
    "no_target",
)
EXCEPTION_REQUIRED_FIELDS = ("id", "scope", "exact_argv", "reason", "proof")
# Proofs / argv shapes that bind an exception to a specific callsite.
_CALLSITE_PROOFS = ("code_hash", "heredoc_body_ast_scan")


def _is_callsite_bound(entry: dict) -> bool:
    """A heredoc / ``-c`` (callsite-bound) exception requires surface+locator."""
    if entry.get("proof") in _CALLSITE_PROOFS:
        return True
    argv = entry.get("exact_argv")
    if isinstance(argv, list) and argv and argv[-1] in ("-", "-c"):
        return True
    return False


def validate_exceptions_schema(data: dict) -> list[str]:
    """Validate the exceptions registry against the AC4a schema (fail-closed).

    Each entry MUST contain a unique ``id`` (str), ``scope`` in
    {stdlib_only, bootstrap}, ``exact_argv`` (non-empty list of str — the
    *complete* argv token list), ``reason`` (str) and ``proof`` in
    {stdlib_import_scan, code_hash, heredoc_body_ast_scan, no_target}.
    Callsite-bound (heredoc / ``-c``) entries additionally require ``surface``
    and ``locator`` strings. Returns a list of human-readable error strings; an
    empty list means valid.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["registry root must be a JSON object"]
    exceptions = data.get("exceptions")
    if not isinstance(exceptions, list):
        return ["registry must contain an 'exceptions' array"]
    seen_ids: set[str] = set()
    for idx, entry in enumerate(exceptions):
        if not isinstance(entry, dict):
            errors.append(f"exceptions[{idx}] must be an object")
            continue
        for field_name in EXCEPTION_REQUIRED_FIELDS:
            if field_name not in entry:
                errors.append(
                    f"exceptions[{idx}] missing required field '{field_name}'"
                )
        eid = entry.get("id")
        if "id" in entry:
            if not isinstance(eid, str) or not eid.strip():
                errors.append(f"exceptions[{idx}].id must be a non-empty string")
            elif eid in seen_ids:
                errors.append(f"exceptions[{idx}].id {eid!r} is not unique")
            else:
                seen_ids.add(eid)
        scope = entry.get("scope")
        if "scope" in entry and scope not in EXCEPTION_ALLOWED_SCOPES:
            errors.append(
                f"exceptions[{idx}].scope must be one of {EXCEPTION_ALLOWED_SCOPES},"
                f" got {scope!r}"
            )
        argv = entry.get("exact_argv")
        if "exact_argv" in entry:
            if not isinstance(argv, list) or not argv:
                errors.append(
                    f"exceptions[{idx}].exact_argv must be a non-empty array"
                )
            elif not all(isinstance(t, str) and t for t in argv):
                errors.append(
                    f"exceptions[{idx}].exact_argv must contain non-empty strings"
                )
        reason = entry.get("reason")
        if "reason" in entry and (not isinstance(reason, str) or not reason.strip()):
            errors.append(f"exceptions[{idx}].reason must be a non-empty string")
        proof = entry.get("proof")
        if "proof" in entry and proof not in EXCEPTION_ALLOWED_PROOFS:
            errors.append(
                f"exceptions[{idx}].proof must be one of {EXCEPTION_ALLOWED_PROOFS},"
                f" got {proof!r}"
            )
        if isinstance(entry, dict) and _is_callsite_bound(entry):
            for cb in ("surface", "locator"):
                val = entry.get(cb)
                if not isinstance(val, str) or not val.strip():
                    errors.append(
                        f"exceptions[{idx}] is callsite-bound and requires a"
                        f" non-empty '{cb}' string"
                    )
    return errors


def load_exceptions(repo_root: Path, *, validate: bool = True) -> list[dict]:
    """Load the exceptions registry; fail-closed on schema violations (AC4c)."""
    exc_path = repo_root / EXCEPTIONS_PATH
    if not exc_path.exists():
        return []
    data = json.loads(exc_path.read_text(encoding="utf-8"))
    if validate:
        errors = validate_exceptions_schema(data)
        if errors:
            raise ValueError(
                "invalid python_invocation_policy_exceptions.json:\n  "
                + "\n  ".join(errors)
            )
    return data.get("exceptions", [])


def find_exception(argv_tokens: list[str], exceptions: list[dict]) -> dict | None:
    """Return the exception whose ``exact_argv`` equals ``argv_tokens`` exactly.

    Exact full-argv match (AC4d): no prefix / glob / regex. Path-qualified
    executables, option reordering, duplicates and extra tokens all fail to
    match.
    """
    for exc in exceptions:
        if argv_tokens == exc.get("exact_argv"):
            return exc
    return None


def matches_exception(argv_tokens: list[str], exceptions: list[dict]) -> bool:
    """Backward-compatible boolean wrapper over :func:`find_exception`."""
    return find_exception(argv_tokens, exceptions) is not None


# ---------------------------------------------------------------------------
# AST import scanning (proof for stdlib_only — AC4b / AC14)
# ---------------------------------------------------------------------------

def _top_module(name: str) -> str:
    return name.split(".")[0]


def _resolve_local_module(
    mod: str, importer_path: Path | None, repo_root: Path | None
) -> Path | None:
    candidates: list[Path] = []
    if importer_path is not None:
        d = Path(importer_path).parent
        candidates.append(d / f"{mod}.py")
        candidates.append(d / mod / "__init__.py")
    if repo_root is not None:
        candidates.append(Path(repo_root) / f"{mod}.py")
    for c in candidates:
        if c.is_file():
            return c
    return None


def _check_module(
    mod: str,
    importer_path: Path | None,
    repo_root: Path | None,
    seen: set[Path],
) -> list[str]:
    if not mod or mod in _STDLIB:
        return []
    local = _resolve_local_module(mod, importer_path, repo_root)
    if local is None:
        return [mod]
    if local in seen:
        return []
    seen.add(local)
    try:
        code = local.read_text(encoding="utf-8")
    except OSError:
        return [mod]
    res = ast_nonstdlib_imports(code, local, repo_root, seen)
    if res is None:
        # repo-local module that does not parse -> cannot prove stdlib-only.
        return [mod]
    return res


def ast_nonstdlib_imports(
    code: str,
    importer_path: Path | None,
    repo_root: Path | None,
    seen: set[Path] | None = None,
) -> list[str] | None:
    """Return non-stdlib top-level imports in ``code``.

    Returns an empty list when the code is (recursively) stdlib-only, a
    non-empty list of offending module names otherwise, and ``None`` when the
    code cannot be parsed as Python.
    """
    if seen is None:
        seen = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bad += _check_module(
                    _top_module(alias.name), importer_path, repo_root, seen
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import -> repo-local; best-effort treated as ok.
                continue
            if node.module:
                bad += _check_module(
                    _top_module(node.module), importer_path, repo_root, seen
                )
    return bad


def _inline_code_has_dependency(
    code: str, repo_root: Path | None, surface_path: Path | None
) -> bool:
    """True if inline code imports a non-stdlib module or cannot be parsed."""
    res = ast_nonstdlib_imports(code, surface_path, repo_root, set())
    return res is None or len(res) > 0


# ---------------------------------------------------------------------------
# uv run helpers
# ---------------------------------------------------------------------------

def _is_locked_uv_run(tokens: list[str]) -> bool:
    if len(tokens) < 3:
        return False
    flags = []
    i = 2
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            break
        if t.startswith("-"):
            flags.append(t)
            i += 1
        elif t in ("python", "python3", "pytest"):
            break
        else:
            break
    return "--locked" in flags


def _get_uv_run_command(tokens: list[str]) -> list[str]:
    if len(tokens) < 3:
        return []
    i = 2
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            i += 1
            break
        if t.startswith("-"):
            i += 1
        else:
            break
    return tokens[i:]


def _looks_like_script(arg: str) -> bool:
    return arg.endswith(".py")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _check_exception_proof(
    exc: dict,
    tokens: list[str],
    repo_root: Path | None,
    surface_path: Path | None,
    heredoc_body: str | None,
) -> tuple[bool, str]:
    proof = exc.get("proof")
    if proof == "no_target":
        return False, "exception_match"
    if proof == "stdlib_import_scan":
        if repo_root is None:
            return False, "exception_match"
        script = None
        for tok in tokens[1:]:
            if _looks_like_script(tok):
                script = tok
                break
        if script is None:
            return False, "exception_match"
        path = Path(repo_root) / script
        if not path.is_file():
            return False, "exception_match"
        res = ast_nonstdlib_imports(
            path.read_text(encoding="utf-8"), path, repo_root, set()
        )
        if res is None or len(res) > 0:
            return True, "exception_proof_failed"
        return False, "exception_match"
    if proof in ("heredoc_body_ast_scan",):
        if heredoc_body and _inline_code_has_dependency(
            heredoc_body, repo_root, surface_path
        ):
            return True, "exception_proof_failed"
        return False, "exception_match"
    # code_hash and unknown proofs are accepted by schema; treat as vetted.
    return False, "exception_match"


def classify_invocation(
    tokens: list[str],
    exceptions: list[dict],
    repo_root: Path | None = None,
    surface_path: Path | None = None,
    heredoc_body: str | None = None,
) -> tuple[bool, str]:
    """Classify a single command invocation.

    Returns (is_violation, violation_type_or_reason).
    """
    if not tokens:
        return False, "no_tokens"

    head = tokens[0]

    # -- direct python / python3 --
    if head in ("python", "python3"):
        if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "pytest":
            return True, "direct_python_m_pytest"
        exc = find_exception(tokens, exceptions)
        if exc is not None:
            return _check_exception_proof(
                exc, tokens, repo_root, surface_path, heredoc_body
            )
        # inline -c
        if len(tokens) >= 2 and tokens[1] == "-c":
            code = tokens[2] if len(tokens) >= 3 else ""
            if _inline_code_has_dependency(code, repo_root, surface_path):
                return True, "heredoc_c_dependency"
            return False, "inline_stdlib_ok"
        # heredoc / stdin
        if len(tokens) >= 2 and tokens[1] == "-":
            body = heredoc_body or ""
            if _inline_code_has_dependency(body, repo_root, surface_path):
                return True, "heredoc_c_dependency"
            return False, "inline_stdlib_ok"
        # script
        if (
            len(tokens) >= 2
            and not tokens[1].startswith("-")
            and _looks_like_script(tokens[1])
        ):
            return True, "direct_python_script"
        return False, "no_script_arg"

    # -- uv run ... --
    if head == "uv" and len(tokens) >= 2 and tokens[1] == "run":
        locked = _is_locked_uv_run(tokens)
        sub = _get_uv_run_command(tokens)
        if not sub:
            return False, "uv_run_no_subcommand"
        sub_cmd = sub[0]

        if sub_cmd == "pytest":
            if not locked:
                return True, "uv_run_pytest_no_locked"
            return False, "uv_run_locked_pytest_ok"

        if sub_cmd in ("python", "python3"):
            if len(sub) >= 3 and sub[1] == "-m" and sub[2] == "pytest":
                return True, "uv_run_python_m_pytest"
            if len(sub) >= 2 and sub[1] == "-c":
                if locked:
                    return False, "uv_run_locked_inline_ok"
                code = sub[2] if len(sub) >= 3 else ""
                if _inline_code_has_dependency(code, repo_root, surface_path):
                    return True, "uv_run_inline_no_locked_dependency"
                return False, "uv_run_inline_stdlib_ok"
            if len(sub) >= 2 and sub[1] == "-":
                if locked:
                    return False, "uv_run_locked_inline_ok"
                if _inline_code_has_dependency(
                    heredoc_body or "", repo_root, surface_path
                ):
                    return True, "uv_run_inline_no_locked_dependency"
                return False, "uv_run_inline_stdlib_ok"
            if (
                len(sub) >= 2
                and not sub[1].startswith("-")
                and _looks_like_script(sub[1])
            ):
                if not locked:
                    return True, "uv_run_python_script_no_locked"
                return False, "uv_run_locked_python_script_ok"
            return False, "uv_run_python_inline"

        return False, "uv_run_other_command"

    return False, "not_python_invocation"


# ---------------------------------------------------------------------------
# Conservative custom shell splitter (no external parser — AC15 / AC16)
# ---------------------------------------------------------------------------

_PREFIX_WORDS = {
    "if", "then", "elif", "else", "fi", "while", "until", "do", "done",
    "!", "sudo", "env", "time", "command", "exec", "nohup", "builtin",
}
_RE_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*(?:\s+|$)")
_RE_REDIRECT_TOKEN = re.compile(r"^(?:[0-9]*[<>]|&>|>>)")


def _quotes_balanced(s: str) -> bool:
    in_s = in_d = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_s:
            if c == "'":
                in_s = False
        elif in_d:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_d = False
        else:
            if c == "'":
                in_s = True
            elif c == '"':
                in_d = True
        i += 1
    return not in_s and not in_d


def _contains_launcher(s: str) -> bool:
    return _RE_LAUNCHER.search(s) is not None


def split_compound(cmd: str) -> list[str]:
    """Split a command into simple commands on && || ; | & and newlines.

    Quote-, substitution- (``$(...)`` / ``<(...)`` / backticks) and GitHub
    expression (``${{ ... }}``) aware: operators inside those spans do not
    split.
    """
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(cmd)
    in_s = in_d = False
    paren = 0
    brace = 0  # ${{ }} depth
    while i < n:
        c = cmd[i]
        two = cmd[i:i + 2]
        three = cmd[i:i + 3]
        if in_s:
            buf.append(c)
            if c == "'":
                in_s = False
            i += 1
            continue
        if in_d:
            if c == "\\" and i + 1 < n:
                buf.append(cmd[i:i + 2])
                i += 2
                continue
            buf.append(c)
            if c == '"':
                in_d = False
            i += 1
            continue
        if three == "${{":
            brace += 1
            buf.append(three)
            i += 3
            continue
        if two == "}}" and brace > 0:
            brace -= 1
            buf.append(two)
            i += 2
            continue
        if c == "'":
            in_s = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_d = True
            buf.append(c)
            i += 1
            continue
        if two == "$(" or two == "<(":
            paren += 1
            buf.append(two)
            i += 2
            continue
        if c == "(" and paren > 0:
            paren += 1
            buf.append(c)
            i += 1
            continue
        if c == ")" and paren > 0:
            paren -= 1
            buf.append(c)
            i += 1
            continue
        if paren == 0 and brace == 0:
            if two in ("&&", "||"):
                parts.append("".join(buf))
                buf = []
                i += 2
                continue
            if c in (";", "\n", "|", "&"):
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def extract_substitutions(cmd: str) -> tuple[list[str], str, bool]:
    """Extract top-level command/process substitution bodies.

    Returns (inner_command_strings, command_with_substitutions_replaced,
    malformed). ``$( )`` and backticks are expanded inside double quotes (bash
    semantics) but not inside single quotes; ``<( )`` only outside quotes.
    ``malformed`` is True when a substitution is not closed.
    """
    subs: list[str] = []
    out: list[str] = []
    i, n = 0, len(cmd)
    in_s = in_d = False
    while i < n:
        c = cmd[i]
        two = cmd[i:i + 2]
        if in_s:
            out.append(c)
            if c == "'":
                in_s = False
            i += 1
            continue
        if c == "'" and not in_d:
            in_s = True
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_d = not in_d
            out.append(c)
            i += 1
            continue
        if two == "$(" or (two == "<(" and not in_d):
            depth = 1
            j = i + 2
            inner: list[str] = []
            ss = dd = False
            while j < n and depth > 0:
                cj = cmd[j]
                if ss:
                    inner.append(cj)
                    if cj == "'":
                        ss = False
                    j += 1
                    continue
                if dd:
                    inner.append(cj)
                    if cj == '"':
                        dd = False
                    j += 1
                    continue
                if cj == "'":
                    ss = True
                    inner.append(cj)
                    j += 1
                    continue
                if cj == '"':
                    dd = True
                    inner.append(cj)
                    j += 1
                    continue
                if cmd[j:j + 2] in ("$(", "<("):
                    depth += 1
                    inner.append(cmd[j:j + 2])
                    j += 2
                    continue
                if cj == "(":
                    depth += 1
                    inner.append(cj)
                    j += 1
                    continue
                if cj == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                    inner.append(cj)
                    j += 1
                    continue
                inner.append(cj)
                j += 1
            if depth != 0:
                out.append(" __SUB__ ")
                return subs, "".join(out), True
            subs.append("".join(inner))
            out.append(" __SUB__ ")
            i = j
            continue
        if c == "`" and not in_s:
            j = i + 1
            inner = []
            while j < n and cmd[j] != "`":
                inner.append(cmd[j])
                j += 1
            if j >= n:
                out.append(" __SUB__ ")
                return subs, "".join(out), True
            subs.append("".join(inner))
            out.append(" __SUB__ ")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return subs, "".join(out), False


def _strip_prefix(cmd: str) -> str:
    s = cmd.strip()
    while True:
        m = _RE_ASSIGNMENT.match(s)
        if m:
            s = s[m.end():].strip()
            continue
        m = re.match(r"^(\S+)(?:\s+|$)", s)
        if m and m.group(1) in _PREFIX_WORDS:
            s = s[m.end():].strip()
            continue
        break
    return s


def _trim_redirects(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for t in tokens:
        if _RE_REDIRECT_TOKEN.match(t):
            break
        out.append(t)
    return out


def _trim_to_launcher(tokens: list[str]) -> list[str]:
    for idx, t in enumerate(tokens):
        if t in ("python", "python3"):
            return tokens[idx:]
        if t == "uv" and idx + 1 < len(tokens) and tokens[idx + 1] == "run":
            return tokens[idx:]
    return []


def _scan_simple_command(
    sc: str,
    exceptions: list[dict],
    repo_root: Path | None,
    surface_path: Path | None,
    heredoc_body: str | None,
) -> list[tuple[str, str]]:
    """Scan one simple command (no top-level operators). Returns [(vtype, snippet)]."""
    vios: list[tuple[str, str]] = []
    if sc.lstrip().startswith("#"):
        # Shell comment — not a command.
        return vios
    subs, replaced, malformed = extract_substitutions(sc)
    for inner in subs:
        vios += _scan_command(inner, None, exceptions, repo_root, surface_path)
    if malformed and _contains_launcher(sc):
        vios.append(("unsupported_shell_grammar", sc))
        return vios
    cleaned = _strip_prefix(replaced)
    if cleaned.lstrip().startswith("#") or not _contains_launcher(cleaned):
        return vios
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        # No .split() fallback (AC16): fail-closed on a launcher-bearing line.
        vios.append(("unsupported_shell_grammar", sc))
        return vios
    tokens = _trim_redirects(tokens)
    tokens = _trim_to_launcher(tokens)
    if not tokens:
        return vios
    is_v, vt = classify_invocation(
        tokens, exceptions, repo_root, surface_path, heredoc_body
    )
    if is_v:
        vios.append((vt, sc))
    return vios


def _scan_command(
    text: str,
    heredoc_body: str | None,
    exceptions: list[dict],
    repo_root: Path | None,
    surface_path: Path | None,
) -> list[tuple[str, str]]:
    vios: list[tuple[str, str]] = []
    for sc in split_compound(text):
        vios += _scan_simple_command(
            sc, exceptions, repo_root, surface_path, heredoc_body
        )
    return vios


def scan_command_types(
    text: str,
    heredoc_body: str | None = None,
    exceptions: list[dict] | None = None,
    repo_root: Path | None = None,
    surface_path: Path | None = None,
) -> list[str]:
    """Test helper: return the list of violation types for a shell command."""
    return [
        vt
        for vt, _ in _scan_command(
            text, heredoc_body, exceptions or [], repo_root, surface_path
        )
    ]


# ---------------------------------------------------------------------------
# Legacy single-command extractor (kept for unit tests; no .split fallback)
# ---------------------------------------------------------------------------

def _extract_argv_from_line(line: str) -> list[str] | None:
    """Extract the first Python invocation argv from a single shell command."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return None
    for launcher in ("uv run", "python3 ", "python3\t", "python ", "python\t"):
        idx = stripped.find(launcher)
        if idx == -1:
            continue
        if idx > 0 and stripped[idx - 1].isalnum():
            continue
        if launcher.lstrip().startswith("python") and stripped[:idx].rstrip().endswith("uv"):
            continue
        candidate = stripped[idx:]
        heredoc_match = re.search(r"\s+<<", candidate)
        if heredoc_match:
            candidate = candidate[:heredoc_match.start()].strip()
        pipe_match = re.search(r"(?<!\|)\|(?!\|)", candidate)
        if pipe_match:
            candidate = candidate[:pipe_match.start()].strip()
        try:
            tokens = shlex.split(candidate)
        except ValueError:
            return None
        if not tokens:
            continue
        return _trim_to_launcher(tokens) or tokens
    return None


# ---------------------------------------------------------------------------
# Surface content extractors
# ---------------------------------------------------------------------------

def _assemble_logical(units: list[tuple[int, str]]) -> Iterator[tuple[int, str]]:
    """Join physical lines into logical commands.

    Handles trailing ``\\`` continuations and multi-line quoted strings (e.g. a
    multi-line ``python3 -c "..."``).
    """
    i = 0
    n = len(units)
    while i < n:
        start_lineno, text = units[i]
        buf = text
        i += 1
        while i < n:
            if buf.rstrip().endswith("\\"):
                buf = buf.rstrip()[:-1] + " " + units[i][1].strip()
                i += 1
                continue
            if not _quotes_balanced(buf) and _contains_launcher(buf):
                buf = buf + "\n" + units[i][1]
                i += 1
                continue
            break
        yield (start_lineno, buf)


def _iter_yaml_units(content: str) -> Iterator[tuple[int, str, str | None]]:
    """Yield (line_num, command_text, heredoc_body) from YAML run: blocks."""
    lines = content.splitlines()
    in_run = False
    run_indent: int | None = None
    run_line_re = re.compile(r'^(\s*)(?:-\s+)?run:\s*(.*)$')
    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_run:
            m = run_line_re.match(line)
            if m:
                rest = m.group(2).strip()
                if rest in ("|", ">", "|2", ">2", "|-", ">-", "|+", ">+", ""):
                    in_run = True
                    run_indent = None
                    i += 1
                    continue
                if rest:
                    yield (i + 1, rest, None)
                    i += 1
                    continue
            i += 1
            continue
        stripped = line.lstrip()
        if not stripped:
            i += 1
            continue
        cur_indent = len(line) - len(stripped)
        if run_indent is None:
            run_indent = cur_indent
        if cur_indent < run_indent:
            in_run = False
            run_indent = None
            continue
        # Assemble a logical command (continuation + multi-line quotes).
        start = i + 1
        buf = stripped
        i += 1
        while i < len(lines):
            nxt = lines[i]
            nxt_stripped = nxt.lstrip()
            if buf.rstrip().endswith("\\"):
                buf = buf.rstrip()[:-1] + " " + nxt_stripped
                i += 1
                continue
            if (
                not _quotes_balanced(buf)
                and _contains_launcher(buf)
                and not _RE_HEREDOC_START.search(buf)
            ):
                buf = buf + "\n" + nxt_stripped
                i += 1
                continue
            break
        body: str | None = None
        hd = _RE_HEREDOC_START.search(buf)
        if hd:
            delim = hd.group(1)
            body_lines: list[str] = []
            while i < len(lines):
                if lines[i].strip() == delim:
                    i += 1
                    break
                body_lines.append(lines[i])
                i += 1
            body = textwrap.dedent("\n".join(body_lines))
        yield (start, buf, body)


def iter_yaml_run_lines(content: str) -> Iterator[tuple[int, str]]:
    """Compat wrapper: yield (line_num, command_text) for YAML run: commands."""
    for line_num, text, _body in _iter_yaml_units(content):
        yield (line_num, text)


def iter_markdown_code_lines(content: str) -> Iterator[tuple[int, str]]:
    """Yield (line_num, line_text) for lines in non-exempted fenced code blocks."""
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence_m = _RE_FENCE_OPEN.match(line)
        if fence_m:
            fence_chars = fence_m.group(2)
            fence_len = len(fence_chars)
            fence_char = fence_chars[0]
            lang = (fence_m.group(3) or "").strip().lower()
            is_example = lang not in SHELL_FENCE_LANGS
            if not is_example:
                j = i - 1
                while j >= 0 and not lines[j].strip():
                    j -= 1
                if j >= 0 and _RE_POLICY_EXAMPLE_COMMENT.search(lines[j]):
                    is_example = True
            i += 1
            while i < len(lines):
                close_line = lines[i]
                close_m = _RE_FENCE_CLOSE_PREFIX.match(close_line)
                if (
                    close_m
                    and close_m.group(2)[0] == fence_char
                    and len(close_m.group(2)) >= fence_len
                ):
                    i += 1
                    break
                if not is_example:
                    yield (i + 1, close_line)
                i += 1
            continue
        i += 1


def _markdown_shell_blocks(content: str) -> Iterator[list[tuple[int, str]]]:
    """Yield each shell-language (or untagged) fenced block as a list of lines.

    Mirrors :func:`iter_markdown_code_lines` block selection but groups lines by
    block so multi-line assembly cannot cross a fence boundary.
    """
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence_m = _RE_FENCE_OPEN.match(line)
        if fence_m:
            fence_chars = fence_m.group(2)
            fence_len = len(fence_chars)
            fence_char = fence_chars[0]
            lang = (fence_m.group(3) or "").strip().lower()
            is_example = lang not in SHELL_FENCE_LANGS
            if not is_example:
                j = i - 1
                while j >= 0 and not lines[j].strip():
                    j -= 1
                if j >= 0 and _RE_POLICY_EXAMPLE_COMMENT.search(lines[j]):
                    is_example = True
            i += 1
            block: list[tuple[int, str]] = []
            while i < len(lines):
                close_line = lines[i]
                close_m = _RE_FENCE_CLOSE_PREFIX.match(close_line)
                if (
                    close_m
                    and close_m.group(2)[0] == fence_char
                    and len(close_m.group(2)) >= fence_len
                ):
                    i += 1
                    break
                if not is_example:
                    block.append((i + 1, close_line))
                i += 1
            if block:
                yield block
            continue
        i += 1


def _assemble_block(block: list[tuple[int, str]]) -> Iterator[tuple[int, str]]:
    """Assemble logical commands within a single fenced block.

    Joins ``\\`` continuations and multi-line quoted strings, bounded by the
    block. A line that is purely a comment never starts a multi-line
    accumulation (prevents apostrophes in prose/comments from running away).
    """
    i = 0
    n = len(block)
    while i < n:
        start_lineno, text = block[i]
        i += 1
        if text.lstrip().startswith("#"):
            yield (start_lineno, text)
            continue
        buf = text
        while i < n:
            if buf.rstrip().endswith("\\"):
                buf = buf.rstrip()[:-1] + " " + block[i][1].strip()
                i += 1
                continue
            if not _quotes_balanced(buf):
                buf = buf + "\n" + block[i][1]
                i += 1
                continue
            break
        yield (start_lineno, buf)


def iter_package_json_lines(content: str) -> Iterator[tuple[int, str]]:
    """Yield (line_num, line_text) for script values in package.json."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return
    scripts = data.get("scripts", {})
    lines = content.splitlines()
    for _name, value in scripts.items():
        if not isinstance(value, str):
            continue
        for line_num, line in enumerate(lines, 1):
            if value in line:
                yield (line_num, value)
                break
        else:
            yield (0, value)


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def should_exclude(file_path: str, repo_root: str) -> bool:
    rel = os.path.relpath(file_path, repo_root).replace("\\", "/")
    if rel.startswith(FIXTURE_PREFIX):
        return True
    if rel == TEST_FILE_EXCL:
        return True
    if rel == CHECKER_FILE_EXCL:
        return True
    return False


def _make_suggestion(vtype: str) -> str | None:
    table = {
        "uv_run_pytest_no_locked": "Replace with: uv run --locked pytest ...",
        "uv_run_python_m_pytest": "Replace with: uv run --locked pytest ...",
        "direct_python_m_pytest": "Replace with: uv run --locked pytest ...",
        "uv_run_python_script_no_locked": "Add --locked: uv run --locked python3 <script> ...",
        "direct_python_script": "Register an exact_argv exception or migrate to: uv run --locked python3 <script>",
        "heredoc_c_dependency": "Inline code imports a non-stdlib module; use: uv run --locked python - / python -c",
        "uv_run_inline_no_locked_dependency": "Add --locked to the inline uv run python invocation",
        "exception_proof_failed": "Registered stdlib_only exception target imports a non-stdlib module",
        "unsupported_shell_grammar": "Rewrite using grammar the checker can parse (no unbalanced/unsupported substitution)",
    }
    return table.get(vtype)


def scan_file(
    file_path: str,
    repo_root: str,
    exceptions: list[dict],
) -> list[Violation]:
    rel = os.path.relpath(file_path, repo_root).replace("\\", "/")
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    root_path = Path(repo_root)
    surface_path = Path(file_path)
    violations: list[Violation] = []

    def emit(line_num: int, text: str, found: list[tuple[str, str]]) -> None:
        for vtype, _snip in found:
            violations.append(
                Violation(
                    file=rel,
                    line_num=line_num,
                    line_text=text.strip().replace("\n", " ")[:200],
                    violation_type=vtype,
                    suggestion=_make_suggestion(vtype),
                )
            )

    if rel.endswith((".yml", ".yaml")):
        for line_num, text, body in _iter_yaml_units(content):
            found = _scan_command(text, body, exceptions, root_path, surface_path)
            emit(line_num, text, found)
    elif rel.endswith(".md"):
        for block in _markdown_shell_blocks(content):
            for line_num, text in _assemble_block(block):
                found = _scan_command(text, None, exceptions, root_path, surface_path)
                emit(line_num, text, found)
    elif rel == "package.json":
        for line_num, value in iter_package_json_lines(content):
            found = _scan_command(value, None, exceptions, root_path, surface_path)
            emit(line_num, value, found)

    return violations


# ---------------------------------------------------------------------------
# Surface discovery
# ---------------------------------------------------------------------------

def collect_surface_files(repo_root: Path) -> list[str]:
    files: list[str] = []
    root = str(repo_root)

    for pattern in SURFACE_GLOBS:
        for p in sorted(repo_root.glob(pattern)):
            if p.is_file():
                files.append(str(p))

    for p in sorted(repo_root.glob(SKILL_MD_PATTERN)):
        if p.is_file():
            files.append(str(p))

    seen: set[str] = set()
    result: list[str] = []
    for f in files:
        k = os.path.realpath(f)
        if k not in seen:
            seen.add(k)
            result.append(f)

    filtered = []
    for f in result:
        rel = os.path.relpath(f, root).replace("\\", "/")
        if ".claude/worktrees/" in rel:
            continue
        if should_exclude(f, root):
            continue
        filtered.append(f)

    return filtered


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_check(repo_root: Path, strict: bool = False) -> CheckResult:
    exceptions = load_exceptions(repo_root)
    surface_files = collect_surface_files(repo_root)

    result = CheckResult(exceptions_loaded=len(exceptions))

    for file_path in surface_files:
        result.scanned_files.append(
            os.path.relpath(file_path, str(repo_root)).replace("\\", "/")
        )
        result.surface_count += 1
        result.violations.extend(scan_file(file_path, str(repo_root), exceptions))

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Python invocation policy on governed surfaces.",
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    result = run_check(repo_root, strict=args.strict)

    if args.json_output:
        out = {
            "scanned_files": result.scanned_files,
            "surface_count": result.surface_count,
            "exceptions_loaded": result.exceptions_loaded,
            "violation_count": len(result.violations),
            "violations": [
                {
                    "file": v.file,
                    "line_num": v.line_num,
                    "line_text": v.line_text,
                    "violation_type": v.violation_type,
                    "suggestion": v.suggestion,
                }
                for v in result.violations
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        if result.violations:
            print(f"Python invocation policy violations ({len(result.violations)}):")
            for v in result.violations:
                print(f"  {v.file}:{v.line_num}  [{v.violation_type}]")
                print(f"    {v.line_text[:120]}")
                if v.suggestion:
                    print(f"    => {v.suggestion}")
        else:
            print(
                f"OK: 0 violations in {result.surface_count} governed surface files "
                f"({result.exceptions_loaded} exceptions loaded)"
            )

    return 1 if result.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
