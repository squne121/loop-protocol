"""Issue #2638: rg/grep exit_code==2 regex-syntax / invalid-option /
usage-error VC classification regression tests.

`classify_result()` in baseline_vc_preflight.py previously routed rg/grep
exit_code==2 regex/usage errors (e.g. `rg -n 'f"{repo}#{number}"' file` ->
`rg: regex parse error: ...`) through the generic terminal `unknown` /
`human_judgment` fallback, even though this is a deterministic,
body-author-fixable VC design mistake (invalid regex pattern or unsupported
CLI option), not a genuine environment/runtime failure. This caused
`issue-refinement-loop`'s canonical Step 2 route to stop for unnecessary
operator intervention (see Issue #2634).

This file verifies:
  - AC1/AC2: the new `vc_grep_syntax_error` category is returned (not
    unknown/human_judgment) for the bounded rg/grep exit-2 patterns, and
    survives `_finalize_and_store_job()`'s `# baseline-expect: ...`
    annotation post-processing across all 3 conditions (none / fail / pass)
    without being collapsed back to `human_judgment` /
    `baseline_regression_failed` (OWNER REQUEST_CHANGES P1-1).
  - AC3: an ACTUAL preflight result for this category, routed through
    `map_preflight_result_to_errors()`, yields an aggregate readiness
    status of `needs_fix` (a behavioral test, not a string-search VC that
    would false-pass against contract_readiness_check.py's pre-existing
    `needs_fix` mappings -- OWNER REQUEST_CHANGES P1-2).
  - AC5: the 4 bounded patterns (rg regex parse error, rg invalid option,
    grep regex error, grep invalid option) are each recognized, table-driven.

PR #2643 review (OWNER, https://github.com/squne121/loop-protocol/pull/2643#issuecomment-5699118823)
found 3 remaining classification-boundary gaps in the above and requested
fixes without redesigning the annotation post-processing / readiness
mapping already covered above. This file additionally verifies:
  - Finding 1 (P2): a grep invoked via an absolute path (`/usr/bin/grep`)
    is recognized the same as bare `grep`, even though some grep builds
    echo the VERBATIM `argv[0]` (not just its basename) in their
    diagnostic prefix.
  - Finding 2 (P2): representative additional grep/rg syntax/usage-error
    diagnoses (`Unmatched ( or \(`, `Invalid range end`, `option requires
    an argument`, `invalid max count`, rg's `missing value for flag`) are
    recognized, without absorbing exit_code==2 in general.
  - Finding 3 (P2): a `RIPGREP_CONFIG_PATH`-referenced config file
    containing an invalid option must not cause an otherwise-correct rg VC
    to be misclassified as `vc_grep_syntax_error` (negative control).

Stderr fixtures below were captured from REAL local subprocess invocations
(ripgrep 14.1.0 / GNU grep 3.11) against a plain existing file, not
hand-guessed strings -- see `baseline_vc_preflight.py`'s
`_RG_REGEX_PARSE_ERROR_LINE_TEMPLATE` / `_RG_INVALID_OPTION_LINE_TEMPLATE` /
`_GREP_REGEX_ERROR_LINE_TEMPLATE` / `_GREP_INVALID_OPTION_LINE_TEMPLATE`
docstrings for the exact commands used to capture them. The Finding
1/2/3 tests below additionally capture stderr LIVE (real subprocess, not
hardcoded) from whatever rg/grep build is actually installed in the
current environment, so they self-validate against this environment's
actual diagnostics rather than a potentially stale fixture string.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import baseline_vc_preflight  # noqa: E402
import contract_readiness_check  # noqa: E402

_SCRIPT_PATH = Path(__file__).parent.parent / "baseline_vc_preflight.py"

# ---------------------------------------------------------------------------
# Real stderr formats (captured from local rg 14.1.0 / grep (GNU grep) 3.11
# subprocess invocations -- see module docstring).
# ---------------------------------------------------------------------------

_RG_REGEX_PARSE_ERROR_STDERR = (
    'rg: regex parse error:\n'
    '    (?:f"{repo}#{number}")\n'
    '          ^\n'
    'error: repetition quantifier expects a valid decimal\n'
)
_RG_INVALID_OPTION_STDERR = "rg: unrecognized flag --definitely-invalid-option\n"
_GREP_REGEX_ERROR_STDERR = "grep: Invalid regular expression\n"
_GREP_INVALID_OPTION_STDERR = (
    "grep: unrecognized option '--definitely-invalid-option'\n"
    "Usage: grep [OPTION]... PATTERNS [FILE]...\n"
    "Try 'grep --help' for more information.\n"
)

# Corresponding VC command strings (argv[0] basename drives detection, per
# _detect_rg_grep_exit2_syntax_error()'s os.path.basename(argv[0]) style
# parsing -- NOT a raw substring search over the command text).
_RG_REGEX_PARSE_ERROR_COMMAND = "rg -n 'f\"{repo}#{number}\"' /etc/passwd"
_RG_INVALID_OPTION_COMMAND = "rg --definitely-invalid-option foo /etc/passwd"
_GREP_REGEX_ERROR_COMMAND = "grep -E '[' /etc/passwd"
_GREP_INVALID_OPTION_COMMAND = "grep --definitely-invalid-option foo /etc/passwd"


def _run_preflight_cli(body: str, body_file: Path, issue_num: int = 999) -> dict:
    """Run baseline_vc_preflight.py as a real subprocess (not a mock) against
    an inline Issue-body-shaped Markdown fixture written to `body_file`.

    A tempfile (via pytest's `tmp_path`) is used instead of a checked-in
    `fixtures/*.md` file because Issue #2638's Allowed Paths list only this
    test file itself, `baseline_vc_preflight.py`, and
    `contract_readiness_check.py` -- not the shared `fixtures/` directory.

    Mirrors `test_baseline_vc_preflight.py::run_preflight()`'s subprocess
    invocation shape (this suite intentionally does not import that helper
    across test files, keeping each test file's fixture plumbing
    self-contained)."""
    body_file.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--body-file",
            str(body_file),
            "--issue",
            str(issue_num),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout, f"No output: {result.stderr}"
    return json.loads(result.stdout)


def _run_preflight_cli_with_env(body: str, body_file: Path, env: dict, issue_num: int = 999) -> dict:
    """Like `_run_preflight_cli()` above, but launches the preflight CLI
    subprocess with a CALLER-SUPPLIED environment. Used by the Finding 3
    (`RIPGREP_CONFIG_PATH`) negative-control test below, which must set
    `RIPGREP_CONFIG_PATH` in the PARENT of the preflight CLI subprocess (to
    reproduce a user's shell environment) and confirm the isolation happens
    inside `run_command()`, not merely by coincidence of this test's own
    environment already lacking the variable."""
    body_file.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--body-file",
            str(body_file),
            "--issue",
            str(issue_num),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.stdout, f"No output: {result.stderr}"
    return json.loads(result.stdout)


def _live_subprocess_result(argv):
    """Run `argv` as a REAL subprocess (bypassing any interactive-shell
    aliasing/functions, since `subprocess.run()` with a list argv never
    goes through a shell) and return the completed process. Used by the
    Finding 1/2 tests below to capture this environment's ACTUAL rg/grep
    diagnostic text at test-run time, rather than a hardcoded fixture
    string that could silently drift from what a real invocation now
    produces."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=10)


def _vc_body(annotation_line, command: str) -> str:
    """Build a minimal Issue-body-shaped `## Verification Commands` section
    with a single fenced ```bash block containing exactly one VC command,
    optionally preceded by a `# baseline-expect: ...` annotation line."""
    lines = ["## Verification Commands", "", "```bash", "# AC1"]
    if annotation_line:
        lines.append(annotation_line)
    lines.append(f"$ {command}")
    lines.append("```")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# AC1(a): no annotation -- classify_result() returns the new category
# directly, and it maps to needs_fix via _PREFLIGHT_CATEGORY_TO_READINESS.
# ---------------------------------------------------------------------------


def test_rg_regex_parse_error_exit2_classified_as_needs_fix():
    """AC1(a): no annotation, rg exit 2 regex parse error -> classify_result()
    returns the new `vc_grep_syntax_error` category (NOT unknown/
    human_judgment), and that category maps to `needs_fix` via
    `_PREFLIGHT_CATEGORY_TO_READINESS` (contract_readiness_check.py)."""
    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=2,
        stdout="",
        stderr=_RG_REGEX_PARSE_ERROR_STDERR,
        command=_RG_REGEX_PARSE_ERROR_COMMAND,
        allowed_paths=None,
        static_policy_passed=True,
    )
    assert classification == "blocked", (classification, category, decision)
    assert category == baseline_vc_preflight.VC_GREP_SYNTAX_ERROR_CATEGORY
    assert category not in ("unknown",), category
    assert classification not in ("human_judgment",), classification
    assert decision == "blocked", decision
    assert fix_hint is not None

    mapped = contract_readiness_check._PREFLIGHT_CATEGORY_TO_READINESS.get(category)
    assert mapped == "needs_fix", f"Expected needs_fix mapping but got {mapped!r} for category={category!r}"


def test_rg_regex_parse_error_annotation_matrix(tmp_path):
    """AC1(b)/(c): full 3-condition annotation matrix (none / baseline-expect:
    fail / baseline-expect: pass) for the rg regex-parse-error pattern,
    exercised end-to-end through the real CLI subprocess (not a mock),
    confirming the classification survives `_finalize_and_store_job()`'s
    annotation post-processing in all 3 cases: stays `blocked` /
    `vc_grep_syntax_error` throughout, is never re-mapped to `go` (fail
    case) and is never collapsed to `human_judgment` /
    `baseline_regression_failed` (pass case)."""
    # (a) no annotation
    data_none = _run_preflight_cli(
        _vc_body(None, _RG_REGEX_PARSE_ERROR_COMMAND), tmp_path / "none.md"
    )
    r_none = data_none["results"][0]
    assert r_none["category"] == "vc_grep_syntax_error", r_none
    assert r_none["classification"] == "blocked", r_none
    assert r_none["decision"] == "blocked", r_none

    # (b) baseline-expect: fail -- must NOT become an expected/go baseline fail
    data_fail = _run_preflight_cli(
        _vc_body("# baseline-expect: fail", _RG_REGEX_PARSE_ERROR_COMMAND), tmp_path / "fail.md"
    )
    r_fail = data_fail["results"][0]
    assert r_fail["category"] == "vc_grep_syntax_error", r_fail
    assert r_fail["classification"] == "blocked", r_fail
    assert r_fail["decision"] == "blocked", r_fail
    assert r_fail["decision"] != "go", r_fail

    # (c) baseline-expect: pass -- must NOT be collapsed to baseline_regression_failed
    data_pass = _run_preflight_cli(
        _vc_body("# baseline-expect: pass", _RG_REGEX_PARSE_ERROR_COMMAND), tmp_path / "pass.md"
    )
    r_pass = data_pass["results"][0]
    assert r_pass["category"] == "vc_grep_syntax_error", r_pass
    assert r_pass["classification"] == "blocked", r_pass
    assert r_pass["decision"] == "blocked", r_pass
    assert r_pass["category"] != "baseline_regression_failed", r_pass
    assert r_pass["classification"] != "human_judgment", r_pass


# ---------------------------------------------------------------------------
# AC2: rg's own multi-line `rg: regex parse error:` stderr format (distinct
# from grep/egrep/fgrep's single-line format) is recognized, across the
# same 3 annotation conditions, via real subprocess execution.
# ---------------------------------------------------------------------------


def test_rg_multiline_error_format_recognized(tmp_path):
    """AC2: rg's own multi-line stderr format (`rg: regex parse error:`
    followed by additional context lines, distinct from grep/egrep/fgrep's
    single-line `grep: Invalid regular expression` format) is correctly
    recognized end-to-end via a REAL rg subprocess invocation (not a
    synthetic single-line stand-in), across all 3 annotation conditions."""
    # First: confirm the real captured stderr is genuinely multi-line
    # (regression guard against this test accidentally degrading to a
    # single-line fixture that would not exercise the multi-line anchor).
    assert _RG_REGEX_PARSE_ERROR_STDERR.count("\n") >= 2, _RG_REGEX_PARSE_ERROR_STDERR
    assert _RG_REGEX_PARSE_ERROR_STDERR.startswith("rg: regex parse error:")

    for idx, (annotation, expect_decision) in enumerate(
        (
            (None, "blocked"),
            ("# baseline-expect: fail", "blocked"),
            ("# baseline-expect: pass", "blocked"),
        )
    ):
        body_file = tmp_path / f"multiline_{idx}.md"
        data = _run_preflight_cli(_vc_body(annotation, _RG_REGEX_PARSE_ERROR_COMMAND), body_file)
        r = data["results"][0]
        assert r["exit_code"] == 2, r
        assert r["category"] == "vc_grep_syntax_error", (annotation, r)
        assert r["decision"] == expect_decision, (annotation, r)
        # The real multi-line rg stderr must be preserved (truncated head is fine)
        assert any("regex parse error" in line for line in r.get("stderr_head", [])), r


# ---------------------------------------------------------------------------
# AC3: a REAL preflight result for this category, routed through
# map_preflight_result_to_errors(), yields aggregate readiness == needs_fix.
# This is a behavioral pytest, not a string-search VC (OWNER REQUEST_CHANGES
# P1-2: a naive `rg -n 'needs_fix' contract_readiness_check.py` VC would
# false-pass against the file's 9+ PRE-EXISTING needs_fix mappings even with
# zero implementation of this Issue).
# ---------------------------------------------------------------------------


def test_syntax_error_preflight_result_maps_to_needs_fix():
    """AC3: an ACTUAL classify_result() output for a syntax-error case is
    embedded in a preflight_result payload and passed through
    `map_preflight_result_to_errors()`; the resulting AGGREGATE readiness
    status must be `needs_fix`, and the emitted error entry must carry the
    new category (not merely happen to match some pre-existing needs_fix
    category by coincidence)."""
    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=2,
        stdout="",
        stderr=_RG_REGEX_PARSE_ERROR_STDERR,
        command=_RG_REGEX_PARSE_ERROR_COMMAND,
        allowed_paths=None,
        static_policy_passed=True,
    )
    assert category == "vc_grep_syntax_error", category

    preflight_result = {
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "line": 1,
                "raw_command": _RG_REGEX_PARSE_ERROR_COMMAND,
                "exit_code": 2,
                "classification": classification,
                "category": category,
                "decision": decision,
                "scope_class": scope_class,
                "fix_hint": fix_hint,
                "annotations": {"baseline_expect": None},
                "stderr_head": _RG_REGEX_PARSE_ERROR_STDERR.splitlines(),
                "stdout_head": [],
            }
        ],
        "errors": [],
    }

    errors, aggregate_status = contract_readiness_check.map_preflight_result_to_errors(preflight_result)

    assert aggregate_status == "needs_fix", (
        f"Expected needs_fix but got {aggregate_status!r}; errors={errors!r}"
    )
    matching = [e for e in errors if e.get("category") == "vc_grep_syntax_error"]
    assert matching, f"Expected a vc_grep_syntax_error readiness error entry, got {errors!r}"


# ---------------------------------------------------------------------------
# AC5: table-driven coverage of the exactly 4 bounded patterns.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,stderr,expected_exit_code",
    [
        (_RG_REGEX_PARSE_ERROR_COMMAND, _RG_REGEX_PARSE_ERROR_STDERR, 2),
        (_RG_INVALID_OPTION_COMMAND, _RG_INVALID_OPTION_STDERR, 2),
        (_GREP_REGEX_ERROR_COMMAND, _GREP_REGEX_ERROR_STDERR, 2),
        (_GREP_INVALID_OPTION_COMMAND, _GREP_INVALID_OPTION_STDERR, 2),
    ],
    ids=[
        "rg_regex_parse_error",
        "rg_invalid_option",
        "grep_regex_error",
        "grep_invalid_option",
    ],
)
def test_syntax_error_patterns_table_driven(command, stderr, expected_exit_code):
    """AC5: each of the exactly 4 bounded patterns (rg regex parse error, rg
    invalid option/usage error, grep regex error, grep invalid option/usage
    error) is classified as `vc_grep_syntax_error` / `blocked`, using the
    REAL stderr text captured from local rg/grep subprocess invocations."""
    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=expected_exit_code,
        stdout="",
        stderr=stderr,
        command=command,
        allowed_paths=None,
        static_policy_passed=True,
    )
    assert classification == "blocked", (command, classification, category)
    assert category == baseline_vc_preflight.VC_GREP_SYNTAX_ERROR_CATEGORY, (command, category)
    assert decision == "blocked", (command, decision)
    assert fix_hint is not None


def test_syntax_error_patterns_table_driven_via_direct_detector():
    """AC5 (unit-level companion): `_detect_rg_grep_exit2_syntax_error()`
    itself returns the expected subtype label for each of the 4 patterns,
    confirming argv-basename-driven executable detection (not a raw
    substring search over the command string)."""
    cases = [
        (_RG_REGEX_PARSE_ERROR_COMMAND, _RG_REGEX_PARSE_ERROR_STDERR, "rg_regex_parse_error"),
        (_RG_INVALID_OPTION_COMMAND, _RG_INVALID_OPTION_STDERR, "rg_invalid_option"),
        (_GREP_REGEX_ERROR_COMMAND, _GREP_REGEX_ERROR_STDERR, "grep_regex_error"),
        (_GREP_INVALID_OPTION_COMMAND, _GREP_INVALID_OPTION_STDERR, "grep_invalid_option"),
    ]
    for command, stderr, expected_subtype in cases:
        subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(command, 2, stderr)
        assert subtype == expected_subtype, (command, subtype, expected_subtype)


# ---------------------------------------------------------------------------
# Boundary tests: this new category must NOT absorb exit_code==2 failures
# that are permission/config/env errors, or rg/grep's own missing-path
# special case (Issue #1328) -- Notes for Reviewer semantic finding
# (medium): `_RG_STDERR_ERROR_NOT_MISSING_PATH_PATTERNS` is a blacklist that
# ALSO matches permission/config errors and must not be reused as this
# category's positive matcher.
# ---------------------------------------------------------------------------


def test_rg_permission_denied_exit2_not_classified_as_syntax_error():
    """Boundary: rg exit 2 with a "Permission denied" stderr (a genuine
    environment/permission error, not a regex/usage mistake) must NOT be
    classified as `vc_grep_syntax_error`."""
    subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(
        "rg -n pattern /root/secret", 2, "rg: /root/secret: Permission denied (os error 13)\n"
    )
    assert subtype is None

    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: /root/secret: Permission denied (os error 13)\n",
        command="rg -n pattern /root/secret",
        allowed_paths=None,
        static_policy_passed=True,
    )
    assert category != "vc_grep_syntax_error", category


def test_non_rg_grep_exit2_not_classified_as_syntax_error():
    """Boundary: a non-rg/grep command that happens to exit 2 (e.g. a
    generic tool failure) must not be misdetected via executable-name
    checks that are scoped only to rg/grep/egrep/fgrep."""
    subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(
        "diff -u a.txt b.txt", 2, "diff: regex parse error: not really\n"
    )
    assert subtype is None


# ---------------------------------------------------------------------------
# PR #2643 review Finding 1 (P2): a grep invoked via an absolute path
# (`/usr/bin/grep`) must be recognized the same as bare `grep`, even though
# some grep builds echo the VERBATIM `argv[0]` (not just its basename) in
# their diagnostic prefix.
# ---------------------------------------------------------------------------


def test_grep_bare_and_absolute_path_argv0_regex_error_classified_identically():
    """Finding 1: `grep -E '[' file` and `/usr/bin/grep -E '[' file` must
    both be recognized as `grep_regex_error`, even though (verified live
    below) their stderr diagnostic prefixes literally differ (`grep:` vs
    `/usr/bin/grep:`)."""
    bare = _live_subprocess_result(["grep", "-E", "[", "/etc/passwd"])
    absolute = _live_subprocess_result(["/usr/bin/grep", "-E", "[", "/etc/passwd"])
    assert bare.returncode == 2, bare
    assert absolute.returncode == 2, absolute
    # Regression guard: confirm this environment's grep build actually
    # exhibits the argv[0]-verbatim prefix divergence this test defends
    # against (if some future grep build normalized to a fixed basename
    # prefix, this assertion documents that the reproduction premise
    # changed, instead of this test silently no-op-ing).
    assert bare.stderr != absolute.stderr, (bare.stderr, absolute.stderr)
    assert bare.stderr.startswith("grep:"), bare.stderr
    assert absolute.stderr.startswith("/usr/bin/grep:"), absolute.stderr

    bare_command = "grep -E '[' /etc/passwd"
    absolute_command = "/usr/bin/grep -E '[' /etc/passwd"

    bare_subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(
        bare_command, bare.returncode, bare.stderr
    )
    absolute_subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(
        absolute_command, absolute.returncode, absolute.stderr
    )
    assert bare_subtype == "grep_regex_error", bare_subtype
    assert absolute_subtype == "grep_regex_error", absolute_subtype

    for command, result in ((bare_command, bare), (absolute_command, absolute)):
        classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
            exit_code=result.returncode,
            stdout="",
            stderr=result.stderr,
            command=command,
            allowed_paths=None,
            static_policy_passed=True,
        )
        assert category == "vc_grep_syntax_error", (command, category)
        assert classification == "blocked", (command, classification)
        assert decision == "blocked", (command, decision)


def test_grep_bare_and_absolute_path_argv0_invalid_option_classified_identically():
    """Finding 1: the same argv[0]-prefix-divergence issue also affects the
    invalid-option pattern, not just the regex-error pattern."""
    bare = _live_subprocess_result(["grep", "--definitely-invalid-option", "foo", "/etc/passwd"])
    absolute = _live_subprocess_result(["/usr/bin/grep", "--definitely-invalid-option", "foo", "/etc/passwd"])
    assert bare.returncode == 2, bare
    assert absolute.returncode == 2, absolute
    assert bare.stderr.startswith("grep:"), bare.stderr
    assert absolute.stderr.startswith("/usr/bin/grep:"), absolute.stderr

    bare_command = "grep --definitely-invalid-option foo /etc/passwd"
    absolute_command = "/usr/bin/grep --definitely-invalid-option foo /etc/passwd"

    bare_subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(
        bare_command, bare.returncode, bare.stderr
    )
    absolute_subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(
        absolute_command, absolute.returncode, absolute.stderr
    )
    assert bare_subtype == "grep_invalid_option", bare_subtype
    assert absolute_subtype == "grep_invalid_option", absolute_subtype


def test_grep_absolute_path_argv0_preflight_cli_classified_as_needs_fix(tmp_path):
    """Finding 1, full CLI-level regression: `/usr/bin/grep -E '[' /etc/passwd`
    (absolute-path invocation) is classified as `vc_grep_syntax_error` end
    to end via the real CLI subprocess and maps to `needs_fix` -- exactly
    like the bare `grep` case already covered by
    `test_syntax_error_patterns_table_driven` above."""
    data = _run_preflight_cli(
        _vc_body(None, "/usr/bin/grep -E '[' /etc/passwd"), tmp_path / "abs_grep.md"
    )
    r = data["results"][0]
    assert r["category"] == "vc_grep_syntax_error", r
    assert r["classification"] == "blocked", r
    assert r["decision"] == "blocked", r
    mapped = contract_readiness_check._PREFLIGHT_CATEGORY_TO_READINESS.get(r["category"])
    assert mapped == "needs_fix", f"Expected needs_fix mapping but got {mapped!r}"


# ---------------------------------------------------------------------------
# PR #2643 review Finding 2 (P2): representative additional grep/rg
# syntax/usage-error diagnostics beyond the original single pattern per
# category, captured LIVE from a real subprocess invocation in this
# environment (not hardcoded strings), so each test self-validates against
# whatever rg/grep build/version/locale is actually installed.
# ---------------------------------------------------------------------------

_FINDING_B_LIVE_CASES = [
    pytest.param(
        ["grep", "-E", "(", "/etc/passwd"],
        "grep_regex_error",
        "Unmatched",
        id="grep_unmatched_paren",
    ),
    pytest.param(
        ["grep", "-E", "[z-a]", "/etc/passwd"],
        "grep_regex_error",
        "Invalid range end",
        id="grep_invalid_range_end",
    ),
    pytest.param(
        ["grep", "foo", "/etc/passwd", "-e"],
        "grep_invalid_option",
        "option requires an argument",
        id="grep_option_requires_argument",
    ),
    pytest.param(
        ["grep", "--max-count=no", "foo", "/etc/passwd"],
        "grep_invalid_option",
        "invalid max count",
        id="grep_invalid_max_count",
    ),
    pytest.param(
        ["rg", "foo", "/etc/passwd", "--max-count"],
        "rg_invalid_option",
        "missing value for flag",
        id="rg_missing_value_for_flag",
    ),
]


@pytest.mark.parametrize("argv,expected_subtype,expected_message_fragment", _FINDING_B_LIVE_CASES)
def test_finding_b_additional_syntax_error_patterns_live_capture(argv, expected_subtype, expected_message_fragment):
    """Finding 2: additional representative grep/rg syntax/usage-error
    diagnostics -- not just the single pattern per category the original PR
    covered -- are recognized as `vc_grep_syntax_error` / the correct
    bounded subtype, not silently dropped to `unknown` / `human_judgment`."""
    result = _live_subprocess_result(argv)
    assert result.returncode == 2, (argv, result.returncode, result.stdout, result.stderr)
    assert expected_message_fragment in result.stderr, (argv, result.stderr)

    command = " ".join(shlex.quote(a) for a in argv)
    subtype = baseline_vc_preflight._detect_rg_grep_exit2_syntax_error(command, result.returncode, result.stderr)
    assert subtype == expected_subtype, (argv, result.stderr, subtype)

    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=result.returncode,
        stdout="",
        stderr=result.stderr,
        command=command,
        allowed_paths=None,
        static_policy_passed=True,
    )
    assert category == "vc_grep_syntax_error", (argv, category)
    assert classification == "blocked", (argv, classification)
    assert decision == "blocked", (argv, decision)
    assert fix_hint is not None


def test_finding_b_grep_unmatched_paren_annotation_matrix(tmp_path):
    """Finding 2, representative pattern exercised through the full
    annotation matrix (none / baseline-expect: fail / baseline-expect:
    pass) via the real CLI subprocess -- confirms the newly-recognized
    `Unmatched ( or \\(` grep diagnostic survives
    `_finalize_and_store_job()`'s annotation post-processing the same way
    the pre-existing `Invalid regular expression` pattern already does."""
    command = "grep -E '(' /etc/passwd"

    data_none = _run_preflight_cli(_vc_body(None, command), tmp_path / "b_none.md")
    r_none = data_none["results"][0]
    assert r_none["category"] == "vc_grep_syntax_error", r_none
    assert r_none["classification"] == "blocked", r_none
    assert r_none["decision"] == "blocked", r_none
    mapped = contract_readiness_check._PREFLIGHT_CATEGORY_TO_READINESS.get(r_none["category"])
    assert mapped == "needs_fix", f"Expected needs_fix mapping but got {mapped!r}"

    data_fail = _run_preflight_cli(_vc_body("# baseline-expect: fail", command), tmp_path / "b_fail.md")
    r_fail = data_fail["results"][0]
    assert r_fail["category"] == "vc_grep_syntax_error", r_fail
    assert r_fail["decision"] != "go", r_fail

    data_pass = _run_preflight_cli(_vc_body("# baseline-expect: pass", command), tmp_path / "b_pass.md")
    r_pass = data_pass["results"][0]
    assert r_pass["category"] == "vc_grep_syntax_error", r_pass
    assert r_pass["category"] != "baseline_regression_failed", r_pass
    assert r_pass["classification"] != "human_judgment", r_pass


# ---------------------------------------------------------------------------
# PR #2643 review Finding 3 (P2) negative control: a
# `RIPGREP_CONFIG_PATH`-referenced config file containing an invalid option
# must NOT cause an otherwise-correct rg VC to be misclassified as
# `vc_grep_syntax_error`. Also confirms the isolation never mutates the
# user's own shell/global environment (`os.environ` itself).
# ---------------------------------------------------------------------------


def test_rg_config_path_invalid_option_env_delta_unsets_it_for_rg_only():
    """Finding 3, unit-level: `_fixed_env_delta_for_argv()` returns an
    `_ENV_UNSET_SENTINEL` for `RIPGREP_CONFIG_PATH` when `argv[0]` is `rg`,
    and returns an EMPTY delta for a non-rg command (e.g. `grep`) even when
    that non-rg argv is otherwise identical in shape -- the isolation is
    scoped to rg only, per Finding 3's requirement 5."""
    rg_delta = baseline_vc_preflight._fixed_env_delta_for_argv(["rg", "-F", "foo", "sample.txt"])
    assert rg_delta.get("RIPGREP_CONFIG_PATH") == baseline_vc_preflight._ENV_UNSET_SENTINEL, rg_delta

    grep_delta = baseline_vc_preflight._fixed_env_delta_for_argv(["grep", "-F", "foo", "sample.txt"])
    assert "RIPGREP_CONFIG_PATH" not in grep_delta, grep_delta


def test_ripgrep_config_path_false_positive_negative_control(tmp_path):
    """Finding 3, full CLI-level negative control: a `RIPGREP_CONFIG_PATH`
    pointed at a config file containing an invalid option must not cause
    the correct VC `rg -F foo sample.txt` to be misclassified as
    `vc_grep_syntax_error`. The PARENT preflight CLI subprocess itself is
    launched with `RIPGREP_CONFIG_PATH` set (reproducing a user's shell
    environment), confirming the isolation happens inside `run_command()`
    when it launches the VC's OWN rg subprocess -- not merely an artifact
    of this test's own environment already lacking the variable."""
    sample = tmp_path / "sample.txt"
    sample.write_text("foo bar baz\n", encoding="utf-8")
    config = tmp_path / "bad_ripgreprc"
    config.write_text("--definitely-invalid-option\n", encoding="utf-8")

    # Confirm (independently of the preflight script) that this config file
    # really does break rg the way Finding 3 describes, so this negative
    # control is exercising a genuine false-positive risk, not a no-op.
    poisoned_env = dict(os.environ)
    poisoned_env["RIPGREP_CONFIG_PATH"] = str(config)
    poisoned = subprocess.run(
        ["rg", "-F", "foo", str(sample)], capture_output=True, text=True, env=poisoned_env, timeout=10
    )
    assert poisoned.returncode == 2, poisoned
    assert "--definitely-invalid-option" in poisoned.stderr, poisoned.stderr

    command = f"rg -F foo {sample}"
    data = _run_preflight_cli_with_env(
        _vc_body("# baseline-expect: pass", command), tmp_path / "config_leak.md", poisoned_env
    )
    r = data["results"][0]
    assert r["exit_code"] == 0, r
    assert r["category"] != "vc_grep_syntax_error", r
    assert r["classification"] == "expected_pass", r
    assert r["decision"] == "go", r

    # The isolation must apply ONLY to the launched subprocess's env, never
    # to the caller-supplied env dict itself (which still has the poisoned
    # value set, exactly as a real user's shell environment would) -- i.e.
    # `run_command()` mutates a COPY, not `poisoned_env` in place.
    assert poisoned_env["RIPGREP_CONFIG_PATH"] == str(config), "caller's own env dict must be untouched"
