"""Issue #2799: reusable five-path VC compatibility matrix.

Rows compare shared parser, static-only, canonical plan, normal execution,
and validate_issue_body.py observations independently. Deliberately different
statuses are not reconciled: static-only rejects every malformed family,
while normal execution rejects the whole body ONLY for non-$ lines.
Guard-issue-body.py and check_issue_contract.py parity is covered by the
existing test_vc_grammar_parity.py suite and open Issue #1719, not this matrix.
Security-policy coverage and deduplicated history estimates are excluded;
distinct-text candidate population is checked, but history-store I/O is not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_REPO / ".claude/skills/create-issue/scripts"))

import baseline_vc_preflight as preflight  # noqa: E402
import vc_contract_syntax as syntax  # noqa: E402
import validate_issue_body as validator  # noqa: E402


@dataclass(frozen=True)
class CompatCase:
    name: str
    section: str  # section content, INCLUDING initial blank line(s)
    definitions: tuple[str, ...]
    parser_errors: tuple[tuple[str, str | None], ...]  # kind, LP rule
    commands: tuple[tuple[str, frozenset[str]], ...]  # source order, AC association
    normal_status: str
    normal_results: tuple[tuple[str, str, str, str], ...]  # command, scalar AC, category, decision
    normal_errors: tuple[tuple[str, str], ...]  # kind, rule
    validator_rules: tuple[tuple[str, str], ...]  # rule, severity (LP001 excluded by fixture)
    # plan_count is independent of shared parser count: non-$ rejects all candidates.
    plan_count: int

    @property
    def body(self) -> str:
        ac_lines = "\n".join(f"- [ ] {label}: verify {self.name}" for label in self.definitions)
        return (
            "## Acceptance Criteria\n\n" + ac_lines + "\n\n"
            # extract_level2_section drops the heading's own newline; this
            # extra separator preserves the first blank line of `section`.
            "## Verification Commands\n" + self.section +
            "## Allowed Paths\n\n- `.claude/skills/issue-contract-review/scripts/tests/**`\n"
        )


# Every row is consumed by parser/static/plan/normal/validator assertions
# below; no consumer infers its expected result from another consumer's
# output. The bash command `true` is local and side-effect-free. Its
# baseline annotation makes a successful launch an observable go result.
CASES = (
    CompatCase(
        "non_dollar_command",
        "\n\n```bash\n# AC1\nExplanatory non-dollar line.\n$ true\n```\n\n",
        ("AC1",), (("non_dollar_command", None),),
        (("true", frozenset({"AC1"})),),
        "blocked", (), (("extraction_error", "VC004_NON_DOLLAR_COMMAND"),),
        (("LP_VC_PARSER", "warning"),), 0,
    ),
    CompatCase(
        "compound_shell",
        "\n\n```bash\n# AC1\n# baseline-expect: pass\n$ true 2>&1\n```\n\n",
        ("AC1",), (("compound_shell", None),),
        (("true 2>&1", frozenset({"AC1"})),),
        "blocked", (("true 2>&1", "AC1", "compound_command_disallowed", "blocked"),),
        (), (("LP_VC_PARSER", "error"),), 1,
    ),
    CompatCase(
        "colon_marker",
        "\n\n```bash\n# AC1: explanation\n# baseline-expect: pass\n$ true\n```\n\n",
        ("AC1",), (("colon_marker", "LP016"),),
        (("true", frozenset()),),
        "pass", (("true", "AC_UNKNOWN", "baseline_expect_pass", "go"),),
        (), (("LP010", "error"), ("LP016", "error")), 1,
    ),
    CompatCase(
        "suffixed_marker",
        "\n\n```bash\n# AC1 - explanation\n# baseline-expect: pass\n$ true\n```\n\n",
        ("AC1",), (("suffixed_marker", "LP016"),),
        (("true", frozenset()),),
        "pass", (("true", "AC_UNKNOWN", "baseline_expect_pass", "go"),),
        (), (("LP010", "error"), ("LP016", "error")), 1,
    ),
    CompatCase(
        "inline_backtick",
        "\n\n```bash\n# AC1\n# baseline-expect: pass\n$ true\n```\n\n- `$ true` (not a fence)\n\n",
        ("AC1",), (("inline_backtick", None),),
        (("true", frozenset({"AC1"})),),
        "pass", (("true", "AC1", "baseline_expect_pass", "go"),),
        (), (("LP_VC_PARSER", "error"),), 1,
    ),
    CompatCase(
        "preceding_marker_with_inline_suffix",
        "\n\n```bash\n# AC1\n# baseline-expect: pass\n$ true # AC2\n```\n\n",
        ("AC1",), (("preceding_marker_with_inline_suffix", None),),
        (("true", frozenset({"AC2"})),),
        "pass", (("true", "AC2", "baseline_expect_pass", "go"),),
        (), (("LP010", "error"), ("LP_VC_PARSER", "warning")), 1,
    ),
    CompatCase(
        "unlabeled_fence",
        "\n\n```bash\n# AC1\n# baseline-expect: pass\n$ true\n```\n\n```\n$ false\n```\n\n",
        ("AC1",), (("unlabeled_fence", None),),
        (("true", frozenset({"AC1"})),),
        "pass", (("true", "AC1", "baseline_expect_pass", "go"),),
        (), (), 1,  # LP011 is NOT required when a bash fence also exists.
    ),
)


def _run_cli(case: CompatCase, tmp_path: Path, *flags: str) -> dict:
    source = tmp_path / "issue-body.md"
    source.write_text(case.body, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(_SCRIPTS / "baseline_vc_preflight.py"),
         "--body-file", str(source), "--issue", "999", "--no-history-estimator", *flags],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.stdout, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_case_by_consumer_observable_matrix(case: CompatCase, tmp_path: Path) -> None:
    """AC1: one source row drives five independent observable contracts."""
    section = preflight.extract_verification_commands_section(case.body)
    assert section == case.section  # preserve leading blank lines for provenance
    parsed = syntax.parse_verification_commands_section(section)
    assert [(err.kind, err.rule_id) for err in parsed.errors] == list(case.parser_errors)
    assert [(entry.command, entry.ac_refs) for entry in parsed.commands] == list(case.commands)
    assert parsed.canonical_ac_refs == set().union(*(refs for _, refs in case.commands))

    static = _run_cli(case, tmp_path, "--static-only")
    assert static["status"] == "blocked"
    assert [(r["category"], r["decision"], r["errors"][0]["rule"])
            for r in static["results"]] == [
        (kind, "blocked", rule or f"VC_STATIC_{kind.upper()}")
        for kind, rule in case.parser_errors
    ]

    plan = preflight.compute_canonical_vc_plan(case.body)
    expected_population = [cmd for cmd, _ in case.commands] if case.plan_count else []
    assert plan["command_occurrence_count"] == case.plan_count
    assert len(plan["command_occurrences"]) == case.plan_count
    expected_hashes = [preflight.compute_command_hash(cmd) for cmd in expected_population]
    assert [item["command_hash"] for item in plan["command_occurrences"]] == expected_hashes
    assert len(plan["command_budgets"]) == len(set(expected_population))
    assert {item["command_hash"] for item in plan["command_budgets"]} == set(expected_hashes)
    assert preflight._distinct_command_texts_from_body(case.body) == list(dict.fromkeys(expected_population))

    normal = _run_cli(case, tmp_path, "--strict")
    assert normal["status"] == case.normal_status
    assert [(r["raw_command"], r["ac"], r["category"], r["decision"])
            for r in normal["results"]] == list(case.normal_results)
    assert [(err["kind"], err["rule"]) for err in normal["errors"]] == list(case.normal_errors)
    # A malformed non-$ line rejects the whole body before subprocess selection;
    # other errors are per-command or outside the executable candidate set.
    assert (normal["results"] == []) == (case.name == "non_dollar_command")

    lint = validator.validate_issue_body(case.body)
    relevant = {"LP010", "LP011", "LP016", "LP_VC_PARSER"}
    assert [(err.rule_id, err.severity) for err in lint.errors if err.rule_id in relevant] == list(case.validator_rules)
    assert lint.status == ("fail" if any(sev == "error" for _, sev in case.validator_rules) else "pass")
    assert validator._extract_vc_ac_numbers(case.body) == parsed.ac_refs


def test_unlabeled_fence_without_bash_has_distinct_normal_and_validator_errors(tmp_path: Path) -> None:
    """AC1: contrast the mixed row: LP011/VC003 apply only with no bash candidates."""
    case = CompatCase(
        "unlabeled_only", "\n\n```\n$ true\n```\n\n", ("AC1",),
        (("unlabeled_fence", None),), (), "blocked", (),
        (("unsupported_vc_format", "VC003_UNLABELED_FENCE_BLOCK"),),
        (("LP010", "error"), ("LP011", "error")), 0,
    )
    assert syntax.parse_verification_commands_section(case.section).commands == []
    assert preflight.compute_canonical_vc_plan(case.body)["command_occurrence_count"] == 0
    assert _run_cli(case, tmp_path, "--strict")["errors"][0]["rule"] == case.normal_errors[0][1]
    lint = validator.validate_issue_body(case.body)
    assert [(e.rule_id, e.severity) for e in lint.errors
            if e.rule_id in {"LP010", "LP011"}] == list(case.validator_rules)


# The exact punctuation classifier is reused by the parser, static-only
# reporting and normal per-command classification; no new Bash parser.
PUNCTUATION = (
    "true 2>&1", "true &>out", "true |& tee out", "true >| out", "true <> file",
)


@pytest.mark.parametrize("command", PUNCTUATION)
def test_punctuation_quote_aware_consumer_classification(command: str, tmp_path: Path) -> None:
    """AC2: every punctuation run is blocked before execution (not token equality)."""
    case = CompatCase(
        command, f"\n\n```bash\n# AC1\n# baseline-expect: pass\n$ {command}\n```\n\n",
        ("AC1",), (("compound_shell", None),),
        ((command, frozenset({"AC1"})),), "blocked",
        ((command, "AC1", "compound_command_disallowed", "blocked"),),
        (), (("LP_VC_PARSER", "error"),), 1,
    )
    assert syntax.detect_compound_command(command) is True
    assert [e.kind for e in syntax.parse_verification_commands_section(case.section).errors] == ["compound_shell"]
    static = _run_cli(case, tmp_path, "--static-only")
    assert [(r["category"], r["decision"]) for r in static["results"]] == [("compound_shell", "blocked")]
    normal = _run_cli(case, tmp_path, "--strict")
    assert [(r["raw_command"], r["category"], r["exit_code"]) for r in normal["results"]] == [
        (command, "compound_command_disallowed", None)
    ]
    assert [(e.rule_id, e.severity) for e in validator.validate_issue_body(case.body).errors
            if e.rule_id == "LP_VC_PARSER"] == [("LP_VC_PARSER", "error")]


def test_quoted_regex_and_malformed_quote_classifier_controls(tmp_path: Path) -> None:
    """AC2: quoted alternation is not shell punctuation; malformed quote fails closed."""
    quoted = 'rg -n "foo|bar" PATH'
    assert syntax.detect_compound_command(quoted) is False
    quoted_case = CompatCase("quoted_regex", f"\n\n```bash\n# AC1\n$ {quoted}\n```\n\n",
                             ("AC1",), (), ((quoted, frozenset({"AC1"})),),
                             "pass", (), (), (), 1)
    assert syntax.parse_verification_commands_section(quoted_case.section).errors == []
    assert preflight.compute_canonical_vc_plan(quoted_case.body)["command_occurrence_count"] == 1
    assert _run_cli(quoted_case, tmp_path, "--static-only")["status"] == "ok"
    quoted_normal = _run_cli(quoted_case, tmp_path, "--strict", "--cwd", str(tmp_path))
    # PATH is intentionally an unbounded search target: normal blocks it
    # for that *different* reason, not a fabricated compound-shell verdict.
    assert [(r["category"], r["decision"]) for r in quoted_normal["results"]] == [
        ("broad_search_path_unbounded", "blocked")
    ]
    assert not any(e.rule_id == "LP_VC_PARSER" for e in validator.validate_issue_body(quoted_case.body).errors)
    malformed = 'rg -n "unterminated PATH'
    assert syntax.detect_compound_command(malformed) is True
    malformed_case = CompatCase("malformed_quote", f"\n\n```bash\n# AC1\n$ {malformed}\n```\n\n",
                                ("AC1",), (("compound_shell", None),),
                                ((malformed, frozenset({"AC1"})),), "blocked", (), (), (), 1)
    assert [e.kind for e in syntax.parse_verification_commands_section(
        malformed_case.section
    ).errors] == ["compound_shell"]
    assert _run_cli(malformed_case, tmp_path, "--static-only")["results"][0]["category"] == "compound_shell"
    assert [(r["category"], r["exit_code"]) for r in
            _run_cli(malformed_case, tmp_path, "--strict")["results"]] == [
        ("unsupported_shell_syntax", None)
    ]


# These special-case rows are reusable by future adapter migrations too: one
# body feeds both the shared command entry oracle and the normal JSON oracle.
PROVENANCE_SECTION = (
    "\n\nProse before the first fence.\n\n"
    "```bash\n# AC1\n# baseline-expect: pass\n$ true\n```\n\n"
    "Prose between fences.\n\n"
    "```bash\n# AC2\n# baseline-expect: pass\n$ true\n```\n\n"
)
PROVENANCE = CompatCase("provenance", PROVENANCE_SECTION, ("AC1", "AC2"), (),
                        (("true", frozenset({"AC1"})), ("true", frozenset({"AC2"}))),
                        "pass", (), (), (), 2)
GROUPED = CompatCase("grouped", "\n\n```bash\n# AC2, AC3\n# baseline-expect: pass\n$ true\n```\n\n",
                     ("AC2", "AC3"), (), (("true", frozenset({"AC2", "AC3"})),),
                     "pass", (("true", "AC2,AC3", "baseline_expect_pass", "go"),), (), (), 1)


def test_exact_provenance_coordinates_across_two_fences(tmp_path: Path) -> None:
    """AC3: section lines 8/16 differ from both block lines 3/3 and annotation lines 2/2."""
    section = preflight.extract_verification_commands_section(PROVENANCE.body)
    assert section == PROVENANCE.section
    entries = syntax.parse_verification_commands_section(section).commands
    assert [(e.command, e.ac_refs, e.line_number, e.block_line_number,
             e.annotation_source_line, e.annotation_source_raw)
            for e in entries] == [
        ("true", {"AC1"}, 8, 3, 2, "# baseline-expect: pass"),
        ("true", {"AC2"}, 16, 3, 2, "# baseline-expect: pass"),
    ]
    normal = _run_cli(PROVENANCE, tmp_path, "--strict")
    assert normal["status"] == "pass"
    assert [(r["ac"], r["raw_command"], r["line"], r["annotation_source"])
            for r in normal["results"]] == [
        ("AC1", "true", 3, {"line": 2, "raw": "# baseline-expect: pass"}),
        ("AC2", "true", 3, {"line": 2, "raw": "# baseline-expect: pass"}),
    ]
    assert preflight.compute_canonical_vc_plan(PROVENANCE.body)["command_occurrence_count"] == 2


def test_grouped_ac_one_source_one_occurrence_one_result_one_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """AC4: intercept the real normal execution launch boundary, not only result count."""
    parsed = syntax.parse_verification_commands_section(GROUPED.section)
    assert len(parsed.commands) == 1
    assert parsed.commands[0].ac_refs == {"AC2", "AC3"}
    plan = preflight.compute_canonical_vc_plan(GROUPED.body)
    assert plan["command_occurrence_count"] == len(plan["command_occurrences"]) == 1
    assert len(plan["command_budgets"]) == 1
    grouped_hash = preflight.compute_command_hash(GROUPED.commands[0][0])
    assert [item["command_hash"] for item in plan["command_occurrences"]] == [grouped_hash]
    assert {item["command_hash"] for item in plan["command_budgets"]} == {grouped_hash}
    launches: list[str] = []

    def record_launch(command: str, timeout_seconds: int, cwd: str) -> tuple[int, str, str, int, dict]:
        launches.append(command)
        return 0, "", "", 1, {}

    source = tmp_path / "grouped.md"
    source.write_text(GROUPED.body, encoding="utf-8")
    monkeypatch.setattr(preflight, "run_command", record_launch)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPTS / "baseline_vc_preflight.py"),
                                       "--body-file", str(source), "--issue", "999",
                                       "--strict", "--no-history-estimator"])
    assert preflight.main() == 0
    normal = json.loads(capsys.readouterr().out)
    assert normal["status"] == "pass"
    assert [(r["ac"], r["raw_command"], r["exit_code"]) for r in normal["results"]] == [
        ("AC2,AC3", "true", 0)
    ]
    assert launches == ["true"]
    assert validator._extract_vc_ac_numbers(GROUPED.body) == {"AC2", "AC3"}
    # Exclude validator grouped-marker LP016 from compatibility expectations:
    # its known false rejection is owned by open Issue #1719, not Issue #2799.
    # Keep the independent validator AC association observation above.


def test_annotation_source_docstring_semantics() -> None:
    """AC5: document the actual block-relative value without changing extraction."""
    doc = syntax.VcCommandEntry.__doc__ or ""
    annotation = doc.split("annotation_source_line:", 1)[1].split("annotation_source_raw:", 1)[0]
    assert "block-relative" in annotation
    assert "section-relative" in doc.split("line_number:", 1)[1].split("preflight_scope:", 1)[0] or (
        "within the VC section content" in doc.split("line_number:", 1)[1].split("preflight_scope:", 1)[0]
    )
    assert "block_line_number:" in doc


def test_scope_and_classifier_contract() -> None:
    """AC7: scope is tests plus provenance docstring; classifier is the existing primitive.

    The reviewer's actual git diff remains the authority on Allowed Paths and
    absence of production behavior changes (not an import-identity assertion).
    """
    assert Path(__file__).resolve().is_relative_to(_SCRIPTS / "tests")
    assert all(syntax.detect_compound_command(cmd) for cmd in PUNCTUATION)
    assert not syntax.detect_compound_command('rg -n "foo|bar" PATH')
    assert syntax.detect_compound_command('rg "unterminated')
    assert {case.name for case in CASES} == {
        "non_dollar_command", "compound_shell", "colon_marker", "suffixed_marker",
        "inline_backtick", "preceding_marker_with_inline_suffix", "unlabeled_fence",
    }
