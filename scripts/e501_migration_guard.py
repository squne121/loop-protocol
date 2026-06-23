#!/usr/bin/env python3
"""E501 migration guard tooling (#1138 Child 1).

`verify-diff` is the single, fail-closed pass/fail entrypoint that enforces the
trust boundary required before any area-scoped E501 cleanup child PR is allowed
to land. It resolves base/head refs to immutable SHAs, computes the merge-base
(the canonical baseline), builds a NUL-delimited changed-file manifest (no
``splitlines``), restricts the in-scope Python changes to status ``M`` only,
verifies AST equivalence between base and head, scans for newly added or widened
lint suppressions, runs Ruff in an isolated, pinned configuration on both sides,
ratchets the E501 counts, and emits a single versioned machine-readable report
(``e501-migration-guard/v1``) to stdout. Any error along the way is treated as a
fail-closed failure.

The Ruff toolchain is locked: production has no way to substitute the Ruff
binary. A test-only override exists but is gated behind two explicit environment
variables and surfaced in the report (``ruff.cmd_source`` / ``non_default_ruff_cmd``),
so a CI run or follow-up PR cannot forge a passing gate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

GUARD_VERSION = "1.1.0"
REPORT_SCHEMA = "e501-migration-guard/v1"

# The guard's own project root (where ``uv.lock`` lives). Ruff is launched via
# ``uv run --locked`` from here so the pinned toolchain is used regardless of
# which target repo's blobs are being scanned (blobs are passed by absolute
# path, so Ruff's working directory is irrelevant to the measurement).
GUARD_PROJECT_ROOT = Path(__file__).resolve().parents[1]

GUARD_SELF_PATHS = (
    "scripts/e501_migration_guard.py",
    "scripts/tests/test_e501_migration_guard.py",
)

# Fixed, locked Ruff invocation. The outer launcher is ``uv run --locked`` so the
# Ruff version is pinned by ``uv.lock``; the inner ``ruff check`` arguments are
# fully isolated from any repository configuration so suppression config cannot
# weaken the measurement.
DEFAULT_RUFF_CMD = ("uv", "run", "--locked", "ruff")
RUFF_FIXED_ARGS = (
    "check",
    "--isolated",
    "--select",
    "E501",
    "--line-length",
    "120",
    "--target-version",
    "py312",
    "--no-preview",
    "--ignore-noqa",
    "--no-respect-gitignore",
    "--no-cache",
    "--output-format",
    "json",
)
# Suppression-related flags that must never appear in the guard's Ruff command.
FORBIDDEN_RUFF_CLI_FLAGS = (
    "--config",
    "--ignore",
    "--extend-ignore",
    "--per-file-ignores",
    "--exit-zero",
    "--add-noqa",
)

# Test-only Ruff override. Both env vars are required; production never sets them,
# so the gate cannot be forged from the CLI surface (there is no --ruff-cmd flag).
TEST_RUFF_ALLOW_ENV = "E501_GUARD_ALLOW_TEST_RUFF"
TEST_RUFF_CMD_ENV = "E501_GUARD_RUFF_CMD"

SUBPROCESS_TIMEOUT = 120
STDERR_CAP = 8192

EXIT_PASS = 0
EXIT_POLICY_FAIL = 1
EXIT_USAGE = 2
EXIT_TOOL_ERROR = 3

_CODE_RE = re.compile(r"[A-Z]+[0-9]+")


class GuardError(Exception):
    """Fail-closed internal/tool error (never confused with a clean result)."""


@dataclass
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run(argv: list[str], cwd: str | None, timeout: int = SUBPROCESS_TIMEOUT) -> RunResult:
    """Run a subprocess with ``shell=False`` and a hard timeout (fail-closed)."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GuardError(f"command not found: {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GuardError(f"command timed out after {timeout}s: {shlex.join(argv)}") from exc
    return RunResult(proc.returncode, proc.stdout or b"", proc.stderr or b"")


def _git(repo_root: str, args: list[str], timeout: int = SUBPROCESS_TIMEOUT) -> RunResult:
    return _run(["git", "-C", repo_root, *args], cwd=None, timeout=timeout)


def _git_ok(repo_root: str, args: list[str]) -> str:
    res = _git(repo_root, args)
    if res.returncode != 0:
        raise GuardError(
            f"git {shlex.join(args)} failed (exit {res.returncode}): "
            f"{res.stderr.decode('utf-8', 'replace')[:STDERR_CAP]}"
        )
    return res.stdout.decode("utf-8", "surrogateescape")


# --------------------------------------------------------------------------- #
# Ruff command resolution (production-locked, test-gated)
# --------------------------------------------------------------------------- #


def resolve_ruff_cmd() -> tuple[tuple[str, ...], str]:
    """Return (ruff_cmd, source). Non-default is only possible when BOTH the
    allow flag and the override env are set -- a condition production never meets."""
    allow = os.environ.get(TEST_RUFF_ALLOW_ENV) == "1"
    override = os.environ.get(TEST_RUFF_CMD_ENV)
    if allow and override:
        return tuple(shlex.split(override)), "env_test_override"
    return DEFAULT_RUFF_CMD, "default"


# --------------------------------------------------------------------------- #
# Path / scope canonicalisation
# --------------------------------------------------------------------------- #


def normalize_scope(raw: str) -> str:
    """Canonicalise a --scope prefix, rejecting unsafe forms (fail-closed)."""
    if raw is None or raw == "" or "\x00" in raw or "\\" in raw:
        raise GuardError(f"invalid --scope value (empty/backslash/NUL): {raw!r}")
    p = PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts:
        raise GuardError(f"invalid --scope value (absolute or parent traversal): {raw!r}")
    norm = p.as_posix()
    if norm in (".", ""):
        raise GuardError(f"invalid --scope value (empty or current dir): {raw!r}")
    return norm


def canon_path(path: str) -> str:
    """Canonicalise a git path for comparison (collapse ``//`` and ``/./``)."""
    return PurePosixPath(path).as_posix()


GUARD_SELF_CANON = frozenset(canon_path(p) for p in GUARD_SELF_PATHS)


def _is_guard_self(path: str) -> bool:
    return canon_path(path) in GUARD_SELF_CANON


def _in_scope(path: str, scope_prefixes: tuple[str, ...]) -> bool:
    cp = canon_path(path)
    for prefix in scope_prefixes:
        if cp == prefix or cp.startswith(prefix + "/"):
            return True
    return False


def scope_overlaps(scopes: tuple[str, ...]) -> list[str]:
    findings: list[str] = []
    uniq = list(dict.fromkeys(scopes))
    if len(uniq) != len(scopes):
        findings.append("duplicate scope prefixes provided")
    for a in uniq:
        for b in uniq:
            if a != b and b.startswith(a + "/"):
                findings.append(f"scope {b!r} is nested under {a!r}")
    return findings


# --------------------------------------------------------------------------- #
# Git plumbing helpers
# --------------------------------------------------------------------------- #


def resolve_sha(repo_root: str, ref: str) -> str:
    out = _git_ok(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()
    if len(out) != 40 or any(c not in "0123456789abcdef" for c in out):
        raise GuardError(f"ref {ref!r} did not resolve to a commit SHA: {out!r}")
    return out


def merge_base(repo_root: str, base_sha: str, head_sha: str) -> str:
    """Resolve the merge-base. There is no ``HEAD^`` fallback: missing = fail."""
    res = _git(repo_root, ["merge-base", base_sha, head_sha])
    if res.returncode != 0:
        raise GuardError(
            "merge-base could not be resolved (unrelated histories?); "
            "no HEAD^ fallback is permitted"
        )
    out = res.stdout.decode("utf-8", "surrogateescape").strip()
    if len(out) != 40:
        raise GuardError(f"merge-base produced an unexpected value: {out!r}")
    return out


@dataclass
class ChangedEntry:
    status_code: str
    status_field: str
    path: str
    old_path: str | None = None


def parse_name_status_z(data: bytes) -> list[ChangedEntry]:
    """Parse ``git diff --name-status -z`` output without ``splitlines``."""
    tokens = data.split(b"\x00")
    entries: list[ChangedEntry] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == b"":
            i += 1
            continue
        status_field = tok.decode("utf-8", "surrogateescape")
        code = status_field[0]
        if code in ("R", "C"):
            if i + 2 >= n:
                raise GuardError("truncated rename/copy record in name-status output")
            old = tokens[i + 1].decode("utf-8", "surrogateescape")
            new = tokens[i + 2].decode("utf-8", "surrogateescape")
            entries.append(ChangedEntry(code, status_field, new, old))
            i += 3
        else:
            if i + 1 >= n:
                raise GuardError("truncated record in name-status output")
            path = tokens[i + 1].decode("utf-8", "surrogateescape")
            entries.append(ChangedEntry(code, status_field, path))
            i += 2
    return entries


def changed_manifest(repo_root: str, merge_base_sha: str, head_sha: str) -> list[ChangedEntry]:
    res = _git(
        repo_root,
        ["diff", "--name-status", "--find-renames", "--find-copies", "-z", merge_base_sha, head_sha],
    )
    if res.returncode != 0:
        raise GuardError(
            f"git diff failed (exit {res.returncode}): "
            f"{res.stderr.decode('utf-8', 'replace')[:STDERR_CAP]}"
        )
    return parse_name_status_z(res.stdout)


def blob_at(repo_root: str, sha: str, path: str) -> bytes:
    res = _git(repo_root, ["cat-file", "blob", f"{sha}:{path}"])
    if res.returncode != 0:
        raise GuardError(f"could not read blob {sha}:{path}")
    return res.stdout


def blob_id_at(repo_root: str, sha: str, path: str) -> str | None:
    res = _git(repo_root, ["rev-parse", f"{sha}:{path}"])
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", "replace").strip()


def file_mode_at(repo_root: str, sha: str, path: str) -> str | None:
    out = _git_ok(repo_root, ["ls-tree", sha, "--", path]).strip()
    if not out:
        return None
    return out.split(" ", 1)[0].strip()


# --------------------------------------------------------------------------- #
# Scope / status checks
# --------------------------------------------------------------------------- #


@dataclass
class StatusReport:
    ok: bool
    violations: list[dict[str, str]]
    target_files: list[str]


def check_status_scope(entries: list[ChangedEntry], scope_prefixes: tuple[str, ...]) -> StatusReport:
    violations: list[dict[str, str]] = []
    targets: list[str] = []
    for e in entries:
        path = canon_path(e.path)
        old_is_self = e.old_path is not None and _is_guard_self(e.old_path)
        if _is_guard_self(path) or old_is_self:
            violations.append({"path": path, "reason": "guard_self_change", "status": e.status_field})
            continue
        if not _in_scope(path, scope_prefixes):
            violations.append({"path": path, "reason": "out_of_scope", "status": e.status_field})
            continue
        if not path.endswith(".py"):
            violations.append({"path": path, "reason": "non_python_in_scope", "status": e.status_field})
            continue
        if e.status_code != "M":
            violations.append({"path": path, "reason": "non_modified_status", "status": e.status_field})
            continue
        targets.append(path)
    targets.sort()
    return StatusReport(ok=not violations, violations=violations, target_files=targets)


# --------------------------------------------------------------------------- #
# AST equivalence
# --------------------------------------------------------------------------- #


def decode_source(data: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError as exc:
        raise GuardError(f"could not detect source encoding: {exc}") from exc
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise GuardError(f"could not decode source as {encoding!r}: {exc}") from exc


def ast_fingerprint(data: bytes, label: str) -> str:
    src = decode_source(data)
    try:
        tree = ast.parse(src, filename=label, type_comments=True)
        compile(src, label, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        raise GuardError(f"AST/compile error for {label}: {exc}") from exc
    return ast.dump(tree, include_attributes=False)


@dataclass
class AstEquivResult:
    path: str
    equal: bool


def check_ast_equiv(
    repo_root: str, merge_base_sha: str, head_sha: str, targets: list[str]
) -> tuple[bool, list[AstEquivResult]]:
    results: list[AstEquivResult] = []
    ok = True
    for path in targets:
        for sha in (merge_base_sha, head_sha):
            mode = file_mode_at(repo_root, sha, path)
            if mode is None:
                raise GuardError(f"{path} missing at {sha[:12]} despite status M")
            if mode not in ("100644", "100755"):
                raise GuardError(f"{path} has non-regular git mode {mode} at {sha[:12]}")
        base_fp = ast_fingerprint(blob_at(repo_root, merge_base_sha, path), f"base:{path}")
        head_fp = ast_fingerprint(blob_at(repo_root, head_sha, path), f"head:{path}")
        equal = base_fp == head_fp
        ok = ok and equal
        results.append(AstEquivResult(path=path, equal=equal))
    return ok, results


# --------------------------------------------------------------------------- #
# Suppression scan (semantic comment compare + config)
# --------------------------------------------------------------------------- #

SUPPRESSION_FORMS = (
    "# noqa",
    "# ruff: noqa",
    "# flake8: noqa",
    "# ruff: ignore",
    "# ruff: disable",
    "# ruff: enable",
    "# ruff: file-ignore",
)


def suppression_form_signals(data: bytes) -> dict[str, dict[str, Any]]:
    """Aggregate, per suppression form, whether a blanket form appears and the
    SET of explicit selector codes. Semantic (order/whitespace/case insensitive)."""
    sig: dict[str, dict[str, Any]] = {f: {"blanket": False, "codes": set()} for f in SUPPRESSION_FORMS}
    try:
        for tok in tokenize.tokenize(io.BytesIO(data).readline):
            if tok.type != tokenize.COMMENT:
                continue
            norm = " ".join(tok.string.split())
            low = norm.lower()
            for form in SUPPRESSION_FORMS:
                if low.startswith(form):
                    codes = set(_CODE_RE.findall(norm[len(form):]))
                    if codes:
                        sig[form]["codes"] |= codes
                    else:
                        sig[form]["blanket"] = True
                    break
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        raise GuardError(f"could not tokenize for suppression scan: {exc}") from exc
    return sig


def suppression_widening(
    base_sig: dict[str, dict[str, Any]], head_sig: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return only semantic wideners: a blanket newly present, or codes added.
    Reordering, whitespace-only changes, and narrowing are intentionally ignored."""
    findings: list[dict[str, Any]] = []
    for form in SUPPRESSION_FORMS:
        b = base_sig[form]
        h = head_sig[form]
        if h["blanket"] and not b["blanket"]:
            findings.append({"form": form, "widening": "blanket_added"})
        new_codes = sorted(h["codes"] - b["codes"])
        if new_codes:
            findings.append({"form": form, "widening": "codes_added", "codes": new_codes})
    return findings


RUFF_CONFIG_BASENAMES = ("pyproject.toml", "ruff.toml", ".ruff.toml")


def _toml_load(data: bytes) -> dict[str, Any]:
    import tomllib

    return tomllib.loads(data.decode("utf-8"))


def _ruff_table(parsed: dict[str, Any], basename: str) -> dict[str, Any]:
    if basename == "pyproject.toml":
        return parsed.get("tool", {}).get("ruff", {}) or {}
    return parsed


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _extract_config_signals(table: dict[str, Any]) -> dict[str, Any]:
    lint = table.get("lint", {}) if isinstance(table.get("lint"), dict) else {}
    pycodestyle = lint.get("pycodestyle", {}) if isinstance(lint.get("pycodestyle"), dict) else {}
    pfi = lint.get("per-file-ignores", {})
    epfi = lint.get("extend-per-file-ignores", {})
    return {
        "ignore": set(_as_list(lint.get("ignore")) + _as_list(table.get("ignore"))),
        "extend-ignore": set(_as_list(lint.get("extend-ignore")) + _as_list(table.get("extend-ignore"))),
        "per-file-ignores": pfi if isinstance(pfi, dict) else {},
        "extend-per-file-ignores": epfi if isinstance(epfi, dict) else {},
        "exclude": set(_as_list(table.get("exclude"))),
        "extend-exclude": set(_as_list(table.get("extend-exclude"))),
        "include": set(_as_list(table.get("include"))),
        "extend-include": set(_as_list(table.get("extend-include"))),
        "force-exclude": table.get("force-exclude"),
        "extend": table.get("extend"),
        "line-length": table.get("line-length"),
        "max-line-length": pycodestyle.get("max-line-length"),
        "ignore-overlong-task-comments": pycodestyle.get("ignore-overlong-task-comments"),
        "task-tags": set(_as_list(lint.get("task-tags"))),
    }


def _config_widening(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    for key in ("ignore", "extend-ignore", "exclude", "extend-exclude", "include", "extend-include", "task-tags"):
        new = sorted(head[key] - base[key])
        if new:
            findings.append(f"{key} added: {new}")
    for key in ("per-file-ignores", "extend-per-file-ignores"):
        for pattern, codes in head[key].items():
            base_codes = set(_as_list(base[key].get(pattern)))
            head_codes = set(_as_list(codes))
            if pattern not in base[key] or head_codes - base_codes:
                findings.append(f"{key} widened for {pattern}: {sorted(head_codes)}")
    for key in ("line-length", "max-line-length"):
        bv, hv = base[key], head[key]
        if isinstance(hv, int) and (not isinstance(bv, int) or hv > bv):
            findings.append(f"{key} increased: {bv} -> {hv}")
    if head["ignore-overlong-task-comments"] and not base["ignore-overlong-task-comments"]:
        findings.append("ignore-overlong-task-comments enabled")
    if head["extend"] is not None and head["extend"] != base["extend"]:
        findings.append(f"extend inheritance added/changed: {head['extend']!r}")
    if head["force-exclude"] is not None and head["force-exclude"] != base["force-exclude"]:
        findings.append(f"force-exclude changed: {head['force-exclude']!r}")
    return findings


def scan_config_suppression(
    repo_root: str, merge_base_sha: str, head_sha: str, entries: list[ChangedEntry]
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    config_paths = {canon_path(e.path) for e in entries if os.path.basename(e.path) in RUFF_CONFIG_BASENAMES}
    results: list[dict[str, Any]] = []
    ok = True
    for path in sorted(config_paths):
        basename = os.path.basename(path)
        base_data = blob_at(repo_root, merge_base_sha, path) if file_mode_at(repo_root, merge_base_sha, path) else b""
        head_data = blob_at(repo_root, head_sha, path) if file_mode_at(repo_root, head_sha, path) else b""
        try:
            base_table = _ruff_table(_toml_load(base_data), basename) if base_data else {}
            head_table = _ruff_table(_toml_load(head_data), basename) if head_data else {}
        except Exception as exc:  # any TOML/parse error is fail-closed
            raise GuardError(f"could not parse ruff config {path}: {exc}") from exc
        findings = _config_widening(_extract_config_signals(base_table), _extract_config_signals(head_table))
        if findings:
            ok = False
            results.append({"path": path, "findings": findings})
    # Any change to a Ruff config file is itself disallowed for cleanup PRs
    # (defence in depth; status_scope also rejects it as out-of-scope/non-python).
    changed_config = sorted(config_paths)
    return ok and not changed_config, results, changed_config


def scan_suppressions(
    repo_root: str, merge_base_sha: str, head_sha: str, targets: list[str], entries: list[ChangedEntry]
) -> dict[str, Any]:
    comment_added: list[dict[str, Any]] = []
    ok = True
    for path in targets:
        base_sig = suppression_form_signals(blob_at(repo_root, merge_base_sha, path))
        head_sig = suppression_form_signals(blob_at(repo_root, head_sha, path))
        wid = suppression_widening(base_sig, head_sig)
        if wid:
            ok = False
            comment_added.append({"path": path, "widening": wid})
    config_ok, config_findings, changed_config = scan_config_suppression(
        repo_root, merge_base_sha, head_sha, entries
    )
    ok = ok and config_ok
    return {
        "ok": ok,
        "comment_added": comment_added,
        "config_widened": config_findings,
        "config_files_changed": changed_config,
        "config_scan_mode": "partial_static_scan",
        "comment_scan_mode": "semantic_set_compare",
    }


# --------------------------------------------------------------------------- #
# Ruff execution
# --------------------------------------------------------------------------- #


def _materialize_blobs(repo_root: str, sha: str, paths: list[str], dest: Path) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        data = blob_at(repo_root, sha, path)
        target = (dest / path).resolve()
        if not str(target).startswith(str(dest.resolve()) + os.sep):
            raise GuardError(f"refusing to materialize outside temp dir: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        out.append(target)
    return out


def _ruff_version(ruff_cmd: tuple[str, ...]) -> str:
    res = _run([*ruff_cmd, "--version"], cwd=str(GUARD_PROJECT_ROOT), timeout=SUBPROCESS_TIMEOUT)
    if res.returncode != 0:
        raise GuardError(
            f"could not determine ruff version: {res.stderr.decode('utf-8', 'replace')[:STDERR_CAP]}"
        )
    return res.stdout.decode("utf-8", "replace").strip()


@dataclass
class RuffOutcome:
    exit_code: int
    per_file: dict[str, int]
    total: int
    argv: list[str]
    diagnostics: list[dict[str, Any]]


def run_ruff(repo_root: str, sha: str, targets: list[str], ruff_cmd: tuple[str, ...], workdir: Path) -> RuffOutcome:
    """Run the pinned, isolated Ruff over ``targets`` extracted at ``sha``.

    exit 0 = clean, exit 1 = diagnostics. Anything else (exit 2, timeout, JSON
    decode failure) is a fail-closed tool error -- never silently "0 issues"."""
    for flag in ruff_cmd:
        if flag in FORBIDDEN_RUFF_CLI_FLAGS:
            raise GuardError(f"forbidden ruff suppression flag in command: {flag}")
    dest = workdir / sha[:12]
    dest.mkdir(parents=True, exist_ok=True)
    materialized = _materialize_blobs(repo_root, sha, targets, dest)
    argv = [*ruff_cmd, *RUFF_FIXED_ARGS, *[str(p) for p in materialized]]
    res = _run(argv, cwd=str(GUARD_PROJECT_ROOT), timeout=SUBPROCESS_TIMEOUT)
    if res.returncode not in (0, 1):
        raise GuardError(
            f"ruff exited {res.returncode} (config/internal error); refusing to treat as 0 issues: "
            f"{res.stderr.decode('utf-8', 'replace')[:STDERR_CAP]}"
        )
    per_file: dict[str, int] = {path: 0 for path in targets}
    diagnostics: list[dict[str, Any]] = []
    if res.returncode == 1:
        try:
            raw = json.loads(res.stdout.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GuardError(f"could not decode ruff JSON output: {exc}") from exc
        rel_by_abs = {str(p): path for p, path in zip(materialized, targets)}
        for diag in raw:
            filename = diag.get("filename", "")
            resolved = os.path.realpath(filename)
            rel = rel_by_abs.get(resolved) or rel_by_abs.get(filename)
            if rel is None:
                matches = [r for a, r in rel_by_abs.items() if a.endswith(filename) or filename.endswith(r)]
                if len(matches) != 1:
                    raise GuardError(f"could not map ruff diagnostic file {filename!r}")
                rel = matches[0]
            per_file[rel] = per_file.get(rel, 0) + 1
            loc = diag.get("location", {}) or {}
            diagnostics.append(
                {
                    "path": rel,
                    "code": diag.get("code"),
                    "row": loc.get("row"),
                    "column": loc.get("column"),
                    "message": diag.get("message", ""),
                }
            )
    diagnostics.sort(key=lambda d: (d["path"], d["row"] or 0, d["column"] or 0, d["code"] or ""))
    total = sum(per_file.values())
    return RuffOutcome(exit_code=res.returncode, per_file=per_file, total=total, argv=argv, diagnostics=diagnostics)


# --------------------------------------------------------------------------- #
# Ratchet
# --------------------------------------------------------------------------- #


@dataclass
class RatchetResult:
    ok: bool
    mode: str
    base_total: int
    head_total: int
    per_file: list[dict[str, Any]]
    violations: list[str]


def check_ratchet(base: RuffOutcome, head: RuffOutcome, targets: list[str], mode: str) -> RatchetResult:
    per_file: list[dict[str, Any]] = []
    violations: list[str] = []
    for path in targets:
        bc = base.per_file.get(path, 0)
        hc = head.per_file.get(path, 0)
        per_file.append({"path": path, "base_count": bc, "head_count": hc})
        if hc > bc:
            violations.append(f"per-file regression for {path}: {bc} -> {hc}")
    base_total = base.total
    head_total = head.total
    if head_total > base_total:
        violations.append(f"scope total regression: {base_total} -> {head_total}")
    if mode == "cleanup" and not head_total < base_total:
        violations.append(f"cleanup mode requires a strict decrease: {base_total} -> {head_total}")
    if mode == "completion" and head_total != 0:
        violations.append(f"completion mode requires 0 issues, found {head_total}")
    return RatchetResult(
        ok=not violations,
        mode=mode,
        base_total=base_total,
        head_total=head_total,
        per_file=sorted(per_file, key=lambda d: d["path"]),
        violations=violations,
    )


# --------------------------------------------------------------------------- #
# verify-diff orchestration
# --------------------------------------------------------------------------- #


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_diff(
    repo_root: str,
    base_ref: str,
    head_ref: str,
    scope_prefixes: tuple[str, ...],
    mode: str,
    ruff_cmd: tuple[str, ...],
    ruff_cmd_source: str,
) -> tuple[dict[str, Any], int]:
    """Run every check in order, fail-closed, and build the v1 report."""
    failures: list[str] = []
    scope_prefixes = tuple(normalize_scope(s) for s in scope_prefixes)
    trusted_ruff = ruff_cmd == DEFAULT_RUFF_CMD or ruff_cmd_source == "env_test_override"

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "guard_version": GUARD_VERSION,
        "python_version": ".".join(str(p) for p in sys.version_info[:3]),
        "mode": mode,
        "scope_prefixes": list(scope_prefixes),
        "scope_overlaps": scope_overlaps(scope_prefixes),
    }

    base_sha = resolve_sha(repo_root, base_ref)
    head_sha = resolve_sha(repo_root, head_ref)
    mb = merge_base(repo_root, base_sha, head_sha)
    report["refs"] = {
        "base_ref": base_ref,
        "head_ref": head_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base_sha": mb,
        "baseline_sha": mb,
        "base_ref_is_not_baseline": base_sha != mb,
    }

    entries = changed_manifest(repo_root, mb, head_sha)
    report["changed_files"] = sorted(
        ({"path": canon_path(e.path), "status": e.status_field} for e in entries),
        key=lambda d: d["path"],
    )

    status = check_status_scope(entries, scope_prefixes)
    report["checks"] = {"status_scope": {"ok": status.ok, "violations": status.violations}}
    if not status.ok:
        failures.append("status_scope")

    targets = status.target_files

    ast_ok, ast_results = check_ast_equiv(repo_root, mb, head_sha, targets)
    report["checks"]["ast_equiv"] = {
        "ok": ast_ok,
        "results": [{"path": r.path, "equal": r.equal} for r in ast_results],
    }
    if not ast_ok:
        failures.append("ast_equiv")

    suppression = scan_suppressions(repo_root, mb, head_sha, targets, entries)
    report["checks"]["suppression"] = suppression
    if not suppression["ok"]:
        failures.append("suppression")

    ruff_version = _ruff_version(ruff_cmd)
    with tempfile.TemporaryDirectory(prefix="e501-guard-") as tmp:
        workdir = Path(tmp)
        base_ruff = run_ruff(repo_root, mb, targets, ruff_cmd, workdir)
        head_ruff = run_ruff(repo_root, head_sha, targets, ruff_cmd, workdir)
    report["ruff"] = {
        "version": ruff_version,
        "argv": head_ruff.argv,
        "base_exit": base_ruff.exit_code,
        "head_exit": head_ruff.exit_code,
        "cmd_source": ruff_cmd_source,
        "non_default_ruff_cmd": ruff_cmd != DEFAULT_RUFF_CMD,
        "trusted": trusted_ruff,
    }
    if not trusted_ruff:
        failures.append("untrusted_ruff_cmd")

    ratchet = check_ratchet(base_ruff, head_ruff, targets, mode)
    report["checks"]["ratchet"] = {
        "ok": ratchet.ok,
        "mode": ratchet.mode,
        "base_total": ratchet.base_total,
        "head_total": ratchet.head_total,
        "per_file": ratchet.per_file,
        "violations": ratchet.violations,
    }
    if not ratchet.ok:
        failures.append("ratchet")

    report["diagnostics"] = {
        "base": {"per_file": dict(sorted(base_ruff.per_file.items())), "items": base_ruff.diagnostics},
        "head": {"per_file": dict(sorted(head_ruff.per_file.items())), "items": head_ruff.diagnostics},
    }
    report["blobs"] = [
        {
            "path": path,
            "base_blob": blob_id_at(repo_root, mb, path),
            "head_blob": blob_id_at(repo_root, head_sha, path),
        }
        for path in targets
    ]
    report["uv_lock_sha256"] = _sha256_file(GUARD_PROJECT_ROOT / "uv.lock")

    report["decision"] = "pass" if not failures else "fail"
    report["failures"] = sorted(failures)
    return report, (EXIT_PASS if not failures else EXIT_POLICY_FAIL)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _emit(report: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=False, indent=2))
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E501 migration guard (#1138 Child 1)")
    sub = parser.add_subparsers(dest="command", required=True)

    vd = sub.add_parser("verify-diff", help="single fail-closed pass/fail entrypoint")
    vd.add_argument("--repo-root", default=".")
    vd.add_argument("--base-ref", required=True)
    vd.add_argument("--head-ref", required=True)
    vd.add_argument("--scope", action="append", required=True, help="in-scope path prefix (repeatable)")
    vd.add_argument("--mode", choices=("ratchet", "cleanup", "completion"), default="ratchet")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if ns.command != "verify-diff":
        parser.error(f"unknown command {ns.command!r}")
        return EXIT_USAGE
    repo_root = os.path.abspath(ns.repo_root)
    ruff_cmd, ruff_cmd_source = resolve_ruff_cmd()
    try:
        report, exit_code = verify_diff(
            repo_root=repo_root,
            base_ref=ns.base_ref,
            head_ref=ns.head_ref,
            scope_prefixes=tuple(ns.scope),
            mode=ns.mode,
            ruff_cmd=ruff_cmd,
            ruff_cmd_source=ruff_cmd_source,
        )
    except GuardError as exc:
        error_report = {
            "schema": REPORT_SCHEMA,
            "guard_version": GUARD_VERSION,
            "decision": "fail",
            "failures": ["tool_error"],
            "error": str(exc),
        }
        _emit(error_report)
        print(f"[e501-migration-guard] tool error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    _emit(report)
    if exit_code == EXIT_PASS:
        print("[e501-migration-guard] decision: pass", file=sys.stderr)
    else:
        print(f"[e501-migration-guard] decision: fail ({report['failures']})", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
