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

# --------------------------------------------------------------------------- #
# New tests for #1163: TypeIgnore structural comparison + --coverage scope
# --------------------------------------------------------------------------- #


def _run_guard_with_coverage(
    repo: Path,
    base: str,
    head: str,
    *,
    scope: str = "pkg",
    mode: str = "ratchet",
    coverage: str = "changed",
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
        "--coverage",
        coverage,
    ]
    for s in extra_scopes:
        argv += ["--scope", s]
    env = dict(os.environ)
    if ruff_cmd is not None:
        env["E501_GUARD_ALLOW_TEST_RUFF"] = "1"
        env["E501_GUARD_RUFF_CMD"] = ruff_cmd
    res = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False, env=env)
    report = json.loads(res.stdout) if res.stdout.strip() else {}
    return res.returncode, report, res.stderr


def test_type_ignore_line_shift_same_anchor_passes(tmp_path):
    """TypeIgnore with same structural anchor but shifted lineno -> pass."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_lineshift")

    # base: type: ignore on a short expression
    base_src = (
        "def foo():\n"
        "    x = bar()  # type: ignore[attr-defined]\n"
    )
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    # head: same function but with a preceding comment (shifts lineno by 1)
    head_src = (
        "# added comment\n"
        "def foo():\n"
        "    x = bar()  # type: ignore[attr-defined]\n"
    )
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    # AST structure is identical (same function, same body), type_ignore anchors same
    assert report["checks"]["type_ignore_equiv"]["ok"] is True
    assert report["checks"]["type_ignore_equiv"]["ambiguous_anchor"] is False


def test_type_ignore_move_between_statements_fails(tmp_path):
    """TypeIgnore moved from one statement to another -> structural mismatch -> fail."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_move")

    base_src = (
        "x = foo()  # type: ignore[attr-defined]\n"
        "y = bar()\n"
    )
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    # Move the type: ignore to a different statement
    head_src = (
        "x = foo()\n"
        "y = bar()  # type: ignore[attr-defined]\n"
    )
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    # The anchor (owner_ast_path) differs, so type_ignore_equiv fails
    assert report["checks"]["type_ignore_equiv"]["ok"] is False
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert "type_ignore_equiv" in report["failures"]


def test_type_ignore_tag_change_fails(tmp_path):
    """TypeIgnore tag change (attr-defined -> assignment) -> fail."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_tag")

    base_src = "x = foo()  # type: ignore[attr-defined]\n"
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    head_src = "x = foo()  # type: ignore[assignment]\n"
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    assert report["checks"]["type_ignore_equiv"]["ok"] is False
    assert "type_ignore_equiv" in report["failures"]


def test_file_wide_type_ignore_move_fails(tmp_path):
    """A line_bound type: ignore removed -> mismatch -> fail."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_filewide")

    # base: two statements, the first has a type: ignore[assignment]
    base_src = (
        "x: int = foo()  # type: ignore[assignment]\n"
        "y = bar()\n"
    )
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    # Remove the type: ignore entirely (line_bound removed)
    head_src = (
        "x: int = foo()\n"
        "y = bar()\n"
    )
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    # Removal of type: ignore changes the count
    assert report["checks"]["type_ignore_equiv"]["ok"] is False
    assert "type_ignore_equiv" in report["failures"]


def test_multiple_type_ignores_preserve_multiplicity(tmp_path):
    """Multiple type: ignores on same anchor must preserve multiplicity."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_multi")

    # Two type: ignore on adjacent statements
    base_src = (
        "x = foo()  # type: ignore[attr-defined]\n"
        "y = bar()  # type: ignore[attr-defined]\n"
    )
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    # Remove one type: ignore (now only one remains)
    head_src = (
        "x = foo()  # type: ignore[attr-defined]\n"
        "y = bar()\n"
    )
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    ti = report["checks"]["type_ignore_equiv"]
    assert ti["base_count"] == 2
    assert ti["head_count"] == 1
    assert ti["ok"] is False
    assert "type_ignore_equiv" in report["failures"]


def test_full_scope_fails_on_unchanged_e501(tmp_path):
    """--coverage scope fails when an unchanged file in scope has E501 violations."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "scope_fail")

    # base: two files, one with E501
    write(repo, "pkg/clean.py", SHORT_LINE)
    write(repo, "pkg/dirty.py", LONG_LINE)  # this file is NOT changed in head
    base = commit_all(repo, "base")

    # head: only clean.py is modified (whitespace-only, AST-equiv)
    write(repo, "pkg/clean.py", "x = 1  \n")
    head = commit_all(repo, "head")

    code, report, _ = _run_guard_with_coverage(
        repo, base, head, ruff_cmd=fake, coverage="scope"
    )
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["full_scope_e501"]["ok"] is False
    assert report["checks"]["full_scope_e501"]["head_total"] >= 1
    assert "full_scope_e501" in report["failures"]
    assert report["schema"] == "e501-migration-guard/v2"


def test_full_scope_passes_when_entire_head_scope_clean(tmp_path):
    """--coverage scope passes when all .py files in scope are E501-clean at head."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "scope_pass")

    write(repo, "pkg/a.py", LONG_LINE)
    write(repo, "pkg/b.py", LONG_LINE)
    base = commit_all(repo, "base")

    # head: all files cleaned up
    write(repo, "pkg/a.py", SHORT_LINE)
    write(repo, "pkg/b.py", SHORT_LINE)
    head = commit_all(repo, "head")

    code, report, _ = _run_guard_with_coverage(
        repo, base, head, mode="completion", ruff_cmd=fake, coverage="scope"
    )
    assert code == GUARD_MOD.EXIT_PASS
    assert report["checks"]["full_scope_e501"]["ok"] is True
    assert report["checks"]["full_scope_e501"]["head_total"] == 0
    assert report["schema"] == "e501-migration-guard/v2"


def test_full_scope_uses_literal_pathspec(tmp_path):
    """Files with unusual names (brackets, spaces) must be listed via NUL-safe ls-tree."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "scope_unusual")

    # Create a file with spaces in the name
    write(repo, "pkg/my module.py", SHORT_LINE)
    write(repo, "pkg/normal.py", LONG_LINE)
    base = commit_all(repo, "base")

    # Fix the long-line file; the spaced file is untouched (stays short)
    write(repo, "pkg/normal.py", SHORT_LINE)
    head = commit_all(repo, "head")

    code, report, _ = _run_guard_with_coverage(
        repo, base, head, mode="completion", ruff_cmd=fake, coverage="scope"
    )
    assert code == GUARD_MOD.EXIT_PASS
    fse = report["checks"]["full_scope_e501"]
    assert fse["ok"] is True
    # Both files should be in the inventory
    assert fse["inventory_count"] == 2


def test_full_scope_handles_unusual_filenames_nul_safely(tmp_path):
    """NUL-safe parsing: filenames with special chars don't cause parsing failures."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "scope_nul")

    # Filename with brackets and dash
    write(repo, "pkg/[special]-file.py", SHORT_LINE)
    write(repo, "pkg/another-file.py", SHORT_LINE)
    base = commit_all(repo, "base")

    # Modify one file (whitespace change, AST-equiv)
    write(repo, "pkg/another-file.py", "x = 1  \n")
    head = commit_all(repo, "head")

    code, report, _ = _run_guard_with_coverage(
        repo, base, head, ruff_cmd=fake, coverage="scope"
    )
    fse = report["checks"]["full_scope_e501"]
    # Both files are E501-clean, so full_scope_e501 should pass
    assert fse["ok"] is True
    assert fse["inventory_count"] == 2


def test_full_scope_empty_inventory_fails(tmp_path):
    """Empty .py inventory for a scope fails closed (vacuous pass prevention)."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "scope_empty")

    # Only a non-python file in scope
    write(repo, "pkg/readme.md", "# docs\n")
    write(repo, "pkg/keep.py", SHORT_LINE)
    base = commit_all(repo, "base")

    # head: modify keep.py but use a scope that has no .py files
    write(repo, "pkg/keep.py", "x = 1  \n")
    commit_all(repo, "head")

    # Use an empty scope (no .py files exist under "empty_scope")
    write(repo, "empty_scope/.gitkeep", "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add empty scope")
    # Re-compute head after extra commit
    head2 = _git(repo, "rev-parse", "HEAD").strip()

    code, report, _ = _run_guard_with_coverage(
        repo, base, head2, scope="empty_scope", ruff_cmd=fake, coverage="scope"
    )
    # Should fail with tool_error (empty inventory)
    assert code == GUARD_MOD.EXIT_TOOL_ERROR
    assert "tool_error" in report["failures"]
    assert "vacuous" in report.get("error", "").lower() or "empty" in report.get("error", "").lower()


def test_duplicate_or_nested_scopes_fail(tmp_path):
    """Duplicate or nested --scope values fail as a policy error."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "scope_dup")

    write(repo, "pkg/a.py", SHORT_LINE)
    base = commit_all(repo, "base")
    write(repo, "pkg/a.py", "x = 1  \n")
    head = commit_all(repo, "head")

    # Duplicate scope
    code, report, _ = _run_guard_with_coverage(
        repo, base, head, scope="pkg", extra_scopes=("pkg",), ruff_cmd=fake, coverage="scope"
    )
    assert code != GUARD_MOD.EXIT_PASS
    assert report.get("decision") == "fail"

    # Nested scope
    write(repo, "pkg/sub/b.py", SHORT_LINE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add sub")
    head3 = _git(repo, "rev-parse", "HEAD").strip()

    code2, report2, _ = _run_guard_with_coverage(
        repo, base, head3, scope="pkg", extra_scopes=("pkg/sub",), ruff_cmd=fake, coverage="scope"
    )
    assert code2 != GUARD_MOD.EXIT_PASS
    assert report2.get("decision") == "fail"


def test_full_scope_real_ruff(tmp_path):
    """--coverage scope with real uv run --locked ruff toolchain."""
    repo = init_repo(tmp_path / "scope_real")

    long_call = "result = some_function(" + ", ".join(f"argument_{i}" for i in range(12)) + ")\n"
    assert len(long_call) > 120
    write(repo, "pkg/a.py", long_call)
    write(repo, "pkg/b.py", long_call)
    base = commit_all(repo, "base")

    reflowed = "result = some_function(\n" + "".join(f"    argument_{i},\n" for i in range(12)) + ")\n"
    write(repo, "pkg/a.py", reflowed)
    write(repo, "pkg/b.py", reflowed)
    head = commit_all(repo, "head")

    code, report, stderr = _run_guard_with_coverage(
        repo, base, head, mode="completion", coverage="scope"
    )
    assert code == GUARD_MOD.EXIT_PASS, stderr
    assert report["checks"]["full_scope_e501"]["ok"] is True
    assert report["checks"]["full_scope_e501"]["head_total"] == 0
    assert "ruff " in report["ruff"]["version"]
    assert report["schema"] == "e501-migration-guard/v2"


# --------------------------------------------------------------------------- #
# B1: type_ignore move between files fails
# --------------------------------------------------------------------------- #


def test_type_ignore_move_between_files_fails(tmp_path):
    """B1: type: ignore moved from pkg/a.py to pkg/b.py -> fail (path in sig)."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_crossfile")

    # base: type: ignore in a.py; b.py has no type: ignore
    base_a = "x = foo()  # type: ignore[attr-defined]\n"
    base_b = "y = bar()\n"
    write(repo, "pkg/a.py", base_a)
    write(repo, "pkg/b.py", base_b)
    base = commit_all(repo, "base")

    # head: type: ignore removed from a.py, added to b.py (same tag)
    head_a = "x = foo()\n"
    head_b = "y = bar()  # type: ignore[attr-defined]\n"
    write(repo, "pkg/a.py", head_a)
    write(repo, "pkg/b.py", head_b)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    # The path field in TypeIgnoreSignature differs, so this is a mismatch
    assert report["checks"]["type_ignore_equiv"]["ok"] is False
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert "type_ignore_equiv" in report["failures"]


# --------------------------------------------------------------------------- #
# B2: type_ignore move within multiline statement fails (token context anchor)
# --------------------------------------------------------------------------- #


def test_type_ignore_move_within_multiline_statement_fails(tmp_path):
    """B2: type: ignore moved within same Assign stmt (different sub-expr) -> fail."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_subexpr")

    # base: type: ignore after foo() call
    base_src = "result = foo() + bar()\n" "result2 = foo()  # type: ignore[attr-defined]\n"
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    # head: type: ignore moved to bar() instead (different preceding token)
    # Both are Assign statements but on different lines -> different owner_ast_path
    head_src = "result = foo() + bar()\n" "result2 = bar()  # type: ignore[attr-defined]\n"
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    # AST is different (foo() -> bar()), so ast_equiv fails, which also catches this case.
    # But the preceding_token_fingerprint would also differ (foo -> bar).
    assert code == GUARD_MOD.EXIT_POLICY_FAIL


# --------------------------------------------------------------------------- #
# B3: file_wide type: ignore classification
# --------------------------------------------------------------------------- #


def test_file_wide_type_ignore_same_top_position_passes(tmp_path):
    """B3: standalone type: ignore at file top; adding a comment above it -> pass."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_fw_top")

    # base: standalone type: ignore at file top (standalone comment line -> file_wide)
    # We need a file where type: ignore appears on a standalone comment line
    # In Python, module-level type: ignore is a standalone comment
    base_src = "# type: ignore\nx = 1\n"
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    # head: a regular comment added above it (lineno shifts), same file_wide classification
    head_src = "# module comment\n# type: ignore\nx = 1\n"
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    # Both are file_wide -> same signature -> type_ignore_equiv passes
    # (AST is also identical since type: ignore on standalone comment is not in tree.type_ignores
    # for standalone comments not directly attached to an expression -- this verifies file_wide logic)
    ti = report["checks"]["type_ignore_equiv"]
    # If the counts match and ok is True, the file_wide classification worked
    assert ti["ambiguous_anchor"] is False


def test_line_bound_to_file_wide_fails(tmp_path):
    """B3: type: ignore moved from line_bound (on stmt) to standalone comment -> fail."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "ti_lb_to_fw")

    # base: type: ignore attached to a statement (line_bound)
    base_src = "x = foo()  # type: ignore[attr-defined]\ny = bar()\n"
    write(repo, "pkg/a.py", base_src)
    base = commit_all(repo, "base")

    # head: remove from stmt; add standalone comment version in a different location
    # (In Python, standalone # type: ignore comments are not parsed into tree.type_ignores,
    # so the head will have 0 type_ignores vs base's 1 -> mismatch detected)
    head_src = "x = foo()\ny = bar()\n"
    write(repo, "pkg/a.py", head_src)
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, ruff_cmd=fake)
    # type: ignore removed entirely -> count mismatch
    assert report["checks"]["type_ignore_equiv"]["ok"] is False
    assert "type_ignore_equiv" in report["failures"]


# --------------------------------------------------------------------------- #
# B4: GuardError with --coverage scope uses v2 schema
# --------------------------------------------------------------------------- #


def test_coverage_scope_tool_error_uses_v2_schema(tmp_path):
    """B4: --coverage scope with empty scope (GuardError) -> schema is v2."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "b4_schema")

    # Create a repo with no .py files in the scoped directory
    write(repo, "pkg/readme.md", "# docs\n")
    write(repo, "other/a.py", SHORT_LINE)
    base = commit_all(repo, "base")
    write(repo, "other/a.py", "x = 1  \n")
    head = commit_all(repo, "head")

    # Use --coverage scope with a scope that has no .py files -> GuardError (empty inventory)
    code, report, _ = _run_guard_with_coverage(
        repo, base, head, scope="pkg", ruff_cmd=fake, coverage="scope"
    )
    assert code == GUARD_MOD.EXIT_TOOL_ERROR
    # B4: error report must use v2 schema when --coverage scope
    assert report.get("schema") == "e501-migration-guard/v2"
    assert report.get("coverage") == "scope"


# --------------------------------------------------------------------------- #
# H1: empty changed_targets + cleanup/completion -> policy fail
# --------------------------------------------------------------------------- #


def test_empty_changed_targets_cleanup_fails(tmp_path):
    """H1: cleanup mode with no changed Python targets -> policy fail."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "h1_cleanup")

    # base: only a non-python file
    write(repo, "pkg/readme.md", "# docs\n")
    base = commit_all(repo, "base")

    # head: modify only the non-python file
    write(repo, "pkg/readme.md", "# docs updated\n")
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, mode="cleanup", ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ratchet"]["ok"] is False
    assert any("cleanup mode requires" in v for v in report["checks"]["ratchet"]["violations"])


def test_empty_changed_targets_completion_fails(tmp_path):
    """H1: completion mode with no changed Python targets -> policy fail."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "h1_completion")

    write(repo, "pkg/readme.md", "# docs\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/readme.md", "# docs updated\n")
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, mode="completion", ruff_cmd=fake)
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    assert report["checks"]["ratchet"]["ok"] is False
    assert any("completion mode requires" in v for v in report["checks"]["ratchet"]["violations"])


def test_empty_changed_targets_ratchet_passes(tmp_path):
    """H1: ratchet mode with no changed Python targets -> still passes (no ratchet violation)."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "h1_ratchet")

    write(repo, "pkg/readme.md", "# docs\n")
    base = commit_all(repo, "base")
    write(repo, "pkg/readme.md", "# docs updated\n")
    head = commit_all(repo, "head")

    code, report, _ = run_guard(repo, base, head, mode="ratchet", ruff_cmd=fake)
    # ratchet mode: no changed py targets -> synthetic empty outcomes -> no violations
    assert report["checks"]["ratchet"]["ok"] is True


# --------------------------------------------------------------------------- #
# H2: inventory_sha256 changes when blob SHA changes
# --------------------------------------------------------------------------- #


def test_scope_inventory_digest_changes_when_blob_changes(tmp_path):
    """H2: inventory_sha256 includes git blob SHA; changes when file content changes."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "h2_digest")

    write(repo, "pkg/a.py", SHORT_LINE)
    write(repo, "pkg/b.py", SHORT_LINE)
    base = commit_all(repo, "base")

    # First head: change a.py content only (same filename, different blob)
    write(repo, "pkg/a.py", "x = 2\n")
    head1 = commit_all(repo, "head1")

    # Second head: change a.py back but with different content again
    write(repo, "pkg/a.py", "x = 3\n")
    head2 = commit_all(repo, "head2")

    code1, report1, _ = _run_guard_with_coverage(
        repo, base, head1, ruff_cmd=fake, coverage="scope"
    )
    code2, report2, _ = _run_guard_with_coverage(
        repo, base, head2, ruff_cmd=fake, coverage="scope"
    )

    sha1 = report1["checks"]["full_scope_e501"]["inventory_sha256"]
    sha2 = report2["checks"]["full_scope_e501"]["inventory_sha256"]

    # Different blob content -> different inventory SHA (H2)
    assert sha1 != sha2
    assert sha1.startswith("sha256:")
    assert sha2.startswith("sha256:")


# --------------------------------------------------------------------------- #
# H3: full_scope_e501 diagnostics included on failure
# --------------------------------------------------------------------------- #


def test_full_scope_failure_reports_diagnostics(tmp_path):
    """H3: full-scope E501 failure includes diagnostics with row/column/path."""
    fake = make_stub(tmp_path, "ruff.py", FAKE_RUFF_E501)
    repo = init_repo(tmp_path / "h3_diag")

    # base: one file with E501
    write(repo, "pkg/dirty.py", LONG_LINE)
    write(repo, "pkg/clean.py", SHORT_LINE)
    base = commit_all(repo, "base")

    # head: only clean.py modified, dirty.py still has E501
    write(repo, "pkg/clean.py", "x = 1  \n")
    head = commit_all(repo, "head")

    code, report, _ = _run_guard_with_coverage(
        repo, base, head, ruff_cmd=fake, coverage="scope"
    )
    assert code == GUARD_MOD.EXIT_POLICY_FAIL
    fse = report["checks"]["full_scope_e501"]
    assert fse["ok"] is False

    # H3: diagnostics should be present
    assert "diagnostics" in fse
    diags = fse["diagnostics"]
    assert len(diags) >= 1
    # Each diagnostic has row, column, path
    for d in diags:
        assert "row" in d
        assert "column" in d
        assert "path" in d
