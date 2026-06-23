"""Behavioral tests for the E501 migration guard (#1138 Child 1).

Every pass/fail assertion drives the real ``verify-diff`` CLI in a subprocess
against a throwaway Git repository with genuine commit history, so a vacuous
(``collected 0 items``) green is impossible. Ruff is faked with deterministic
stubs where exit-code / malformed-output behaviour must be controlled, and the
real ``uv run --locked ruff`` toolchain is exercised in the real-ruff and
self-clean tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "e501_migration_guard.py"
REPO_ROOT = Path(__file__).resolve().parents[2]

# A line whose body alone exceeds the 120-column limit (trailing spaces count
# toward E501 length but do not change the AST -- ideal for ratchet control).
SHORT_LINE = "x = 1\n"
LONG_LINE = "x = 1" + " " * 130 + "\n"
# AST-identical to LONG_LINE (still >120) but a distinct blob, so a file can be
# "modified" with a flat E501 count (exercises cleanup/completion failure modes).
LONG_LINE2 = "x = 1" + " " * 131 + "\n"

# Fake ruff: count physical lines longer than 120 columns as E501 diagnostics.
FAKE_RUFF_E501 = r"""
import json, sys
args = sys.argv[1:]
if "--version" in args:
    print("ruff 0.0.0-fake")
    sys.exit(0)
files = [a for a in args if a.endswith(".py")]
diags = []
for f in files:
    try:
        with open(f, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                n = len(line.rstrip("\n"))
                if n > 120:
                    diags.append({"filename": f, "code": "E501",
                                  "message": "Line too long (%d > 120)" % n,
                                  "location": {"row": i, "column": 121}})
    except OSError:
        pass
if diags:
    sys.stdout.write(json.dumps(diags))
    sys.exit(1)
sys.exit(0)
"""

FAKE_RUFF_EXIT2 = r"""
import sys
if "--version" in sys.argv:
    print("ruff 0.0.0-fake")
    sys.exit(0)
sys.stderr.write("ruff: internal configuration error\n")
sys.exit(2)
"""

FAKE_RUFF_BADJSON = r"""
import sys
if "--version" in sys.argv:
    print("ruff 0.0.0-fake")
    sys.exit(0)
sys.stdout.write("{ this is not json")
sys.exit(1)
"""


# --------------------------------------------------------------------------- #
# Module import (for a few pure-function unit checks)
# --------------------------------------------------------------------------- #


def _load_guard_module():
    spec = importlib.util.spec_from_file_location("e501_migration_guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e501_migration_guard"] = mod  # required before exec for dataclass + future annotations
    spec.loader.exec_module(mod)
    return mod


GUARD_MOD = _load_guard_module()


# --------------------------------------------------------------------------- #
# Git repo / CLI helpers
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return res.stdout


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def write_bytes(repo: Path, rel: str, data: bytes) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def commit_all(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def make_stub(tmp_path: Path, name: str, body: str) -> str:
    stub = tmp_path / name
    stub.write_text(body, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}"


def run_guard(
    repo: Path,
    base: str,
    head: str,
    *,
    scope: str = "pkg",
    mode: str = "ratchet",
    ruff_cmd: str | None = None,
    extra_scopes: tuple[str, ...] = (),
) -> tuple[int, dict, str]:
    argv = [
        sys.executable,
        str(GUARD),
        "verify-diff",
        "--repo-root",
        str(repo),
        "--base-ref",
        base,
        "--head-ref",
        head,
        "--scope",
        scope,
        "--mode",
        mode,
    ]
    for s in extra_scopes:
        argv += ["--scope", s]
    env = dict(os.environ)
    if ruff_cmd is not None:
        # Test-only Ruff override is gated behind two explicit env vars; the
        # production CLI exposes no --ruff-cmd flag (gate cannot be forged).
        env["E501_GUARD_ALLOW_TEST_RUFF"] = "1"
        env["E501_GUARD_RUFF_CMD"] = ruff_cmd
    res = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False, env=env)
    report = json.loads(res.stdout) if res.stdout.strip() else {}
    return res.returncode, report, res.stderr


# --------------------------------------------------------------------------- #
# Pure-function unit checks
# --------------------------------------------------------------------------- #


def test_parse_name_status_z_handles_rename_and_spaces():
    data = b"M\x00pkg/a b.py\x00R100\x00pkg/old.py\x00pkg/new.py\x00A\x00pkg/c.py\x00"
    entries = GUARD_MOD.parse_name_status_z(data)
    assert [(e.status_code, e.path, e.old_path) for e in entries] == [
        ("M", "pkg/a b.py", None),
        ("R", "pkg/new.py", "pkg/old.py"),
        ("A", "pkg/c.py", None),
    ]


def test_suppression_form_signals_distinguishes_blanket_from_coded():
    coded = GUARD_MOD.suppression_form_signals(b"x = 1  # noqa: E501\n")
    blanket = GUARD_MOD.suppression_form_signals(b"x = 1  # noqa\n")
    assert coded["# noqa"] == {"blanket": False, "codes": {"E501"}}
    assert blanket["# noqa"] == {"blanket": True, "codes": set()}


def test_suppression_widening_is_semantic_set_compare():
    base = GUARD_MOD.suppression_form_signals(b"x = 1  # noqa: E501, F401\n")
    reordered = GUARD_MOD.suppression_form_signals(b"x = 1  # noqa: F401, E501\n")
    widened = GUARD_MOD.suppression_form_signals(b"x = 1  # noqa: E501, F401, B008\n")
    assert GUARD_MOD.suppression_widening(base, reordered) == []  # reorder is not widening
    assert GUARD_MOD.suppression_widening(base, widened) == [
        {"form": "# noqa", "widening": "codes_added", "codes": ["B008"]}
    ]
    assert GUARD_MOD.suppression_widening(widened, base) == []  # narrowing is not widening


def test_resolve_ruff_cmd_is_production_locked(monkeypatch):
    monkeypatch.delenv("E501_GUARD_ALLOW_TEST_RUFF", raising=False)
    monkeypatch.delenv("E501_GUARD_RUFF_CMD", raising=False)
    assert GUARD_MOD.resolve_ruff_cmd() == (GUARD_MOD.DEFAULT_RUFF_CMD, "default")
    # Override env alone (without the allow flag) is ignored -> still default.
    monkeypatch.setenv("E501_GUARD_RUFF_CMD", "python evil.py")
    assert GUARD_MOD.resolve_ruff_cmd() == (GUARD_MOD.DEFAULT_RUFF_CMD, "default")
    # Both gates set -> override honoured, flagged as test override.
    monkeypatch.setenv("E501_GUARD_ALLOW_TEST_RUFF", "1")
    cmd, source = GUARD_MOD.resolve_ruff_cmd()
    assert cmd == ("python", "evil.py")
    assert source == "env_test_override"


def test_verify_diff_has_no_ruff_cmd_flag():
    # The production CLI must NOT expose a Ruff override surface (forgery vector).
    res = subprocess.run(
        [sys.executable, str(GUARD), "verify-diff", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "--ruff-cmd" not in res.stdout


# --------------------------------------------------------------------------- #
# AC1 -- single fail-closed orchestration of all checks
# --------------------------------------------------------------------------- #


def test_verify_diff_runs_all_checks_fail_closed(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "pkg/a.py", 'value = "before"\n')
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", 'value = "after"\n')  # string value change -> AST not equal
    head = commit_all(repo, "head")
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)

    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["decision"] == "fail"
    # All checks ran (orchestration is complete, in order).
    for name in ("status_scope", "ast_equiv", "suppression", "ratchet"):
        assert name in report["checks"], name
    assert "ast_equiv" in report["failures"]

    # Fail-closed on an internal error: unrelated histories (no merge-base).
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    write(repo, "pkg/z.py", "z = 1\n")
    orphan = commit_all(repo, "orphan-root")
    code3, report3, _ = run_guard(repo, base, orphan, ruff_cmd=fake)
    assert code3 == GUARD_MOD.EXIT_TOOL_ERROR
    assert "tool_error" in report3["failures"]
    assert "merge-base" in report3.get("error", "")


# --------------------------------------------------------------------------- #
# AC2 -- SHA pinning + NUL manifest, no HEAD^ fallback
# --------------------------------------------------------------------------- #


def test_verify_diff_pins_sha_and_builds_nul_manifest(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "pkg/a b.py", "x = 1\n")  # space in filename exercises NUL parsing
    base = commit_all(repo, "base")
    _git(repo, "branch", "feature")
    write(repo, "pkg/a b.py", "x = 1  \n")  # whitespace-only change, AST equal
    head = commit_all(repo, "head")
    _git(repo, "branch", "headbranch")
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)

    # Pass symbolic refs (branch names) and confirm they are pinned to SHAs.
    code, report, _ = run_guard(repo, "feature", "headbranch", ruff_cmd=fake)
    assert report["refs"]["base_sha"] == base
    assert report["refs"]["head_sha"] == head
    assert len(report["refs"]["base_sha"]) == 40
    assert report["refs"]["merge_base_sha"] == base
    paths = [c["path"] for c in report["changed_files"]]
    assert "pkg/a b.py" in paths  # NUL manifest preserved the spaced path

    # Missing merge-base must fail-closed with no HEAD^ fallback.
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    write(repo, "pkg/z.py", "z = 1\n")
    orphan = commit_all(repo, "orphan")
    code2, report2, _ = run_guard(repo, base, orphan, ruff_cmd=fake)
    assert code2 == GUARD_MOD.EXIT_TOOL_ERROR
    assert "merge-base" in report2.get("error", "")


# --------------------------------------------------------------------------- #
# AC3 -- status / scope / guard-self rejection
# --------------------------------------------------------------------------- #


def _reasons(report) -> set[str]:
    return {v["reason"] for v in report["checks"]["status_scope"]["violations"]}


def test_verify_diff_rejects_non_modified_and_guard_self_change(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)

    # (a) Added in-scope python file => rejected (only M allowed).
    repo = init_repo(tmp_path / "a")
    write(repo, "pkg/keep.py", "k = 1\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/added.py", "n = 1\n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert "non_modified_status" in _reasons(report)

    # (b) Rename => rejected.
    repo = init_repo(tmp_path / "b")
    write(repo, "pkg/orig.py", "o = 1\n")
    base = commit_all(repo, "base")
    _git(repo, "mv", "pkg/orig.py", "pkg/renamed.py")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert _reasons(report) & {"non_modified_status"}

    # (c) Out-of-scope change => rejected.
    repo = init_repo(tmp_path / "c")
    write(repo, "pkg/in.py", "i = 1\n")
    write(repo, "other/out.py", "o = 1\n")
    base = commit_all(repo, "base")
    write(repo, "other/out.py", "o = 2\n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert "out_of_scope" in _reasons(report)

    # (d) Guard self-change => rejected.
    repo = init_repo(tmp_path / "d")
    write(repo, "scripts/e501_migration_guard.py", "# guard\nv = 1\n")
    base = commit_all(repo, "base")
    write(repo, "scripts/e501_migration_guard.py", "# guard\nv = 2\n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, scope="scripts", ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert "guard_self_change" in _reasons(report)


# --------------------------------------------------------------------------- #
# AC4 -- AST equivalence (type comments + compile), tool-error distinction
# --------------------------------------------------------------------------- #


def test_ast_equiv_type_comments_and_compile(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)

    # Whitespace-only reflow keeps AST equal -> pass.
    repo = init_repo(tmp_path / "eq")
    write(repo, "pkg/a.py", "result = f(aaaa, bbbb, cccc, dddd)\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", "result = f(\n    aaaa, bbbb, cccc, dddd,\n)\n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_PASS
    assert report["checks"]["ast_equiv"]["ok"] is True

    # String value change -> not equal -> fail.
    repo = init_repo(tmp_path / "str")
    write(repo, "pkg/a.py", 's = "alpha"\n')
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", 's = "beta"\n')
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ast_equiv"]["ok"] is False

    # Type-comment change -> not equal (type_comments=True).
    repo = init_repo(tmp_path / "tc")
    write(repo, "pkg/a.py", "x = []  # type: list[int]\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", "x = []  # type: list[str]\n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ast_equiv"]["ok"] is False

    # Syntax error in head -> tool error (distinct from not-equal).
    repo = init_repo(tmp_path / "syn")
    write(repo, "pkg/a.py", "x = 1\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", "def broken(:\n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_TOOL_ERROR
    assert "tool_error" in report["failures"]


# --------------------------------------------------------------------------- #
# AC5 -- suppression scan: every comment form + config widening
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "suppress",
    [
        "# noqa",
        "# noqa: E501",
        "# ruff: noqa",
        "# flake8: noqa",
        "# ruff: ignore[E501]",
        "# ruff: disable[E501]",
        "# ruff: enable[E501]",
        "# ruff: file-ignore[E501]",
    ],
)
def test_suppression_scan_detects_all_forms(tmp_path, suppress):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / ("s" + str(abs(hash(suppress)))))
    write(repo, "pkg/a.py", "value = 1\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", f"value = 1  {suppress}\n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["suppression"]["ok"] is False
    assert report["checks"]["suppression"]["comment_added"]


def test_suppression_scan_detects_config_widening(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)

    # line-length increase via a newly added pyproject.toml.
    repo = init_repo(tmp_path / "cfg1")
    write(repo, "pkg/a.py", "x = 1\n")
    base = commit_all(repo, "base")
    write(repo, "pyproject.toml", "[tool.ruff]\nline-length = 400\n")
    head = commit_all(repo, "head")
    _, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert report["checks"]["suppression"]["ok"] is False
    assert report["checks"]["suppression"]["config_widened"]
    assert report["checks"]["suppression"]["config_files_changed"]  # any config change is rejected
    assert report["checks"]["suppression"]["config_scan_mode"] == "partial_static_scan"

    # per-file-ignores added for E501.
    repo = init_repo(tmp_path / "cfg2")
    write(repo, "pkg/a.py", "x = 1\n")
    write(repo, "ruff.toml", "line-length = 120\n")
    base = commit_all(repo, "base")
    write(
        repo,
        "ruff.toml",
        'line-length = 120\n[lint.per-file-ignores]\n"pkg/a.py" = ["E501"]\n',
    )
    head = commit_all(repo, "head")
    _, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert report["checks"]["suppression"]["ok"] is False
    assert report["checks"]["suppression"]["config_widened"]


# --------------------------------------------------------------------------- #
# AC6 -- Ruff isolation + exit-2 / malformed handling
# --------------------------------------------------------------------------- #


def test_ruff_exec_isolated_distinguishes_exit2(tmp_path):
    # exit 2 must be a fail-closed tool error, never "0 issues".
    repo = init_repo(tmp_path / "e2")
    write(repo, "pkg/a.py", "x = 1\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", "x = 1  \n")
    head = commit_all(repo, "head")
    exit2 = make_stub(tmp_path, "ruff2.py", FAKE_RUFF_EXIT2)
    code, report, _ = run_guard(repo, base, head, ruff_cmd=exit2)
    assert code == GUARD_MOD.EXIT_TOOL_ERROR
    assert "tool_error" in report["failures"]
    assert "exited 2" in report.get("error", "")

    # Malformed JSON from ruff exit 1 must also fail closed.
    badjson = make_stub(tmp_path, "ruffbad.py", FAKE_RUFF_BADJSON)
    code, report, _ = run_guard(repo, base, head, ruff_cmd=badjson)
    assert code == GUARD_MOD.EXIT_TOOL_ERROR
    assert "tool_error" in report["failures"]

    # A clean (E501-counting) run records an isolated, non-suppressed argv.
    ok = make_stub(tmp_path, "ruffok.py", FAKE_RUFF_E501)
    code, report, _ = run_guard(repo, base, head, ruff_cmd=ok)
    argv = report["ruff"]["argv"]
    assert "--isolated" in argv
    assert "--ignore-noqa" in argv
    for forbidden in GUARD_MOD.FORBIDDEN_RUFF_CLI_FLAGS:
        assert forbidden not in argv


# --------------------------------------------------------------------------- #
# AC7 -- ratchet per-file + scope total across modes
# --------------------------------------------------------------------------- #


def test_ratchet_per_file_and_scope_total(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)

    def build(sub: str, a_base: str, a_head: str, b_base: str, b_head: str):
        repo = init_repo(tmp_path / sub)
        write(repo, "pkg/a.py", a_base)
        write(repo, "pkg/b.py", b_base)
        base = commit_all(repo, "base")
        write(repo, "pkg/a.py", a_head)
        write(repo, "pkg/b.py", b_head)
        head = commit_all(repo, "head")
        return repo, base, head

    # Reduction (1 -> 0 total) passes in ratchet mode.
    repo, base, head = build("r1", LONG_LINE, SHORT_LINE, SHORT_LINE, SHORT_LINE)
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_PASS
    assert report["checks"]["ratchet"]["base_total"] == 1
    assert report["checks"]["ratchet"]["head_total"] == 0

    # Per-file regression fails even when the scope total stays flat.
    repo, base, head = build("r2", SHORT_LINE, LONG_LINE, LONG_LINE, SHORT_LINE)
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ratchet"]["base_total"] == 1
    assert report["checks"]["ratchet"]["head_total"] == 1
    assert any("per-file regression" in v for v in report["checks"]["ratchet"]["violations"])

    # cleanup mode requires a strict decrease (flat total -> fail).
    repo, base, head = build("r3", LONG_LINE, LONG_LINE2, SHORT_LINE, SHORT_LINE)
    code, report, _ = run_guard(repo, base, head, mode="cleanup", ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ratchet"]["base_total"] == 1
    assert report["checks"]["ratchet"]["head_total"] == 1

    repo, base, head = build("r4", LONG_LINE, SHORT_LINE, SHORT_LINE, SHORT_LINE)
    code, report, _ = run_guard(repo, base, head, mode="cleanup", ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_PASS

    # completion mode requires exactly zero.
    repo, base, head = build("r5", LONG_LINE, SHORT_LINE, SHORT_LINE, SHORT_LINE)
    code, report, _ = run_guard(repo, base, head, mode="completion", ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_PASS

    repo, base, head = build("r6", LONG_LINE, LONG_LINE2, SHORT_LINE, SHORT_LINE)
    code, report, _ = run_guard(repo, base, head, mode="completion", ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ratchet"]["head_total"] == 1


# --------------------------------------------------------------------------- #
# AC8 -- report schema completeness + JSON-only stdout
# --------------------------------------------------------------------------- #


def test_report_v1_schema_complete(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "rep")
    write(repo, "pkg/b.py", LONG_LINE)
    write(repo, "pkg/a.py", LONG_LINE)
    base = commit_all(repo, "base")
    write(repo, "pkg/b.py", SHORT_LINE)
    write(repo, "pkg/a.py", SHORT_LINE)
    head = commit_all(repo, "head")

    env = dict(os.environ)
    env["E501_GUARD_ALLOW_TEST_RUFF"] = "1"
    env["E501_GUARD_RUFF_CMD"] = fake
    argv = [
        sys.executable, str(GUARD), "verify-diff",
        "--repo-root", str(repo), "--base-ref", base, "--head-ref", head,
        "--scope", "pkg",
    ]
    res = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False, env=env)
    assert res.returncode == GUARD_MOD.EXIT_PASS, res.stdout + res.stderr
    # stdout must be JSON only.
    assert res.stdout.lstrip().startswith("{")
    report = json.loads(res.stdout)

    assert report["schema"] == "e501-migration-guard/v1"
    assert report["guard_version"]
    assert report["python_version"]
    assert report["ruff"]["version"]
    assert report["ruff"]["argv"]
    assert report["ruff"]["cmd_source"] == "env_test_override"
    assert report["ruff"]["non_default_ruff_cmd"] is True
    assert report["ruff"]["trusted"] is True
    for key in ("base_sha", "head_sha", "merge_base_sha", "baseline_sha"):
        assert len(report["refs"][key]) == 40
    assert report["refs"]["baseline_sha"] == report["refs"]["merge_base_sha"]
    assert report["uv_lock_sha256"].startswith("sha256:")  # read from the guard's own repo
    # Sorted paths.
    cf = [c["path"] for c in report["changed_files"]]
    assert cf == sorted(cf)
    base_pf = report["diagnostics"]["base"]["per_file"]
    assert list(base_pf) == sorted(base_pf)
    assert "items" in report["diagnostics"]["head"]
    assert [b["path"] for b in report["blobs"]] == sorted(b["path"] for b in report["blobs"])
    for b in report["blobs"]:
        assert b["head_blob"] and len(b["head_blob"]) == 40
    assert "ratchet" in report["checks"]
    assert report["decision"] == "pass"


# --------------------------------------------------------------------------- #
# AC9 -- PR #1136-style test destruction: pytest stays green, guard fails
# --------------------------------------------------------------------------- #


def test_e2e_mutation_pr1136_test_destruction(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "mut")
    base_src = "def compute():\n    return 3\n\n\ndef test_a():\n    assert compute() == 3\n"
    write(repo, "pkg/test_thing.py", base_src)
    base = commit_all(repo, "base")
    # Mutation: the assertion/body is gutted. pytest would still pass.
    head_src = "def compute():\n    return 3\n\n\ndef test_a():\n    pass\n"
    write(repo, "pkg/test_thing.py", head_src)
    head = commit_all(repo, "head")

    # (1) Plain pytest on the mutated head is green (the gap the guard closes).
    pytest_res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(repo / "pkg")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert pytest_res.returncode == 0, pytest_res.stdout + pytest_res.stderr

    # (2) verify-diff detects the AST destruction and fails.
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ast_equiv"]["ok"] is False
    assert "ast_equiv" in report["failures"]


# --------------------------------------------------------------------------- #
# Real ruff end-to-end (exercises the actual ``uv run --locked ruff`` path)
# --------------------------------------------------------------------------- #


def test_verify_diff_real_ruff_end_to_end(tmp_path):
    repo = init_repo(tmp_path / "real")
    long_call = "result = some_function(" + ", ".join(f"argument_{i}" for i in range(12)) + ")\n"
    assert len(long_call) > 120
    write(repo, "pkg/a.py", long_call)
    base = commit_all(repo, "base")
    reflowed = "result = some_function(\n" + "".join(f"    argument_{i},\n" for i in range(12)) + ")\n"
    write(repo, "pkg/a.py", reflowed)
    head = commit_all(repo, "head")

    code, report, stderr = run_guard(repo, base, head, mode="cleanup")  # real uv run --locked ruff
    assert code == GUARD_MOD.EXIT_PASS, stderr
    assert report["checks"]["ratchet"]["base_total"] >= 1
    assert report["checks"]["ratchet"]["head_total"] == 0
    assert "ruff " in report["ruff"]["version"]


# --------------------------------------------------------------------------- #
# AC11 -- the guard's own files are clean under ruff --select E,F --ignore E402
# --------------------------------------------------------------------------- #


def test_guard_files_clean_under_ruff_ef():
    res = subprocess.run(
        [
            "uv", "run", "--locked", "ruff", "check",
            "--select", "E,F", "--ignore", "E402",
            str(GUARD), str(Path(__file__).resolve()),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------- #
# Review hardening (PR #1148 adversarial review) -- scope canonicalisation,
# baseline clarity, audit-grade diagnostics.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [".", "..", "/abs", "pkg/../escape", "a\\b", ""])
def test_scope_validation_rejects_unsafe_inputs(tmp_path, bad):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / ("sv" + str(abs(hash(bad)))))
    write(repo, "pkg/a.py", "x = 1\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", "x = 1  \n")
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, scope=bad, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_TOOL_ERROR
    assert "tool_error" in report["failures"]
    assert "scope" in report.get("error", "")


def test_baseline_sha_is_merge_base_not_base_ref(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "baseline")
    write(repo, "pkg/a.py", "x = 1\n")
    fork = commit_all(repo, "fork")
    _git(repo, "branch", "feature")
    # base branch advances past the fork point.
    write(repo, "pkg/a.py", "x = 1  # base advances\n")
    base_tip = commit_all(repo, "base-advance")
    _git(repo, "checkout", "-q", "feature")
    write(repo, "pkg/a.py", "x = 1  \n")  # whitespace-only, AST equal to fork
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base_tip, head, ruff_cmd=fake)
    assert report["refs"]["base_sha"] == base_tip
    assert report["refs"]["merge_base_sha"] == fork
    assert report["refs"]["baseline_sha"] == fork
    assert report["refs"]["base_ref_is_not_baseline"] is True
    assert code == GUARD_MOD.EXIT_PASS


def test_report_includes_ruff_diagnostic_detail(tmp_path):
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "diagdetail")
    write(repo, "pkg/a.py", LONG_LINE)
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", LONG_LINE2)  # still 1 long line, AST equal, flat ratchet
    head = commit_all(repo, "head")
    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_PASS
    items = report["diagnostics"]["head"]["items"]
    assert len(items) == 1
    assert items[0]["code"] == "E501"
    assert items[0]["row"] == 1
    assert items[0]["column"] == 121
    assert "120" in items[0]["message"]
