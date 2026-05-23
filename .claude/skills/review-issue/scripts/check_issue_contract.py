#!/usr/bin/env python3
"""
check_issue_contract.py — C1〜C11 の決定論的チェッカー

Issue 本文（Markdown テキスト）を読み、C1〜C11 の判定を機械的に行い
JSON で結果を出力する。LLM は本スクリプトの JSON 出力を整形するだけでよい。

Usage:
    # ファイルから読み込む（テスト用）
    python check_issue_contract.py --file <path>

    # GitHub から取得する
    python check_issue_contract.py --issue <number> --repo <owner/repo>

    # JSON 出力
    python check_issue_contract.py --file <path> --json

Exit codes:
    0: すべてのチェックが pass（verdict: approve）
    1: 1 つ以上のチェックが fail（verdict: needs-fix）
    2: 入力エラー / 実行エラー
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    NA = "n/a"
    LEGACY_MISSING = "legacy_missing_applicability"


WORKFLOW_SKILLS = {
    "implement-issue",
    "pr-review-judge",
    "ssot-discovery",
    "issue-refinement-loop",
    "impl-review-loop",
    "issue-contract-review",
    "open-pr",
    "post-merge-cleanup",
    "edit-issue",
    "create-issue",
}

SUBJECTIVE_PATTERNS = [
    r"適切に動作",
    r"品質を改善",
    r"最適化",
    r"よりよい",
    r"より良い",
    r"適切な",
    r"良好な",
    r"効果的に",
    r"efficiently",
    r"appropriately",
    r"properly",
    r"optimized?",
    r"improved?",
]

VAGUE_OUTCOME_PATTERNS = [
    r"〜が決定される",
    r"〜を検討する",
    r"〜を改善する",
    r"が決定される",
    r"を検討する",
    r"を改善する",
    r"検討する",
    r"決定される",
]

IMPLEMENTATION_REQUIRED_SECTIONS = [
    "Outcome",
    "Acceptance Criteria",
    "Verification Commands",
    "Stop Conditions",
    "Runtime Verification Applicability",
    "Allowed Paths",
]


@dataclass
class DeterministicChecks:
    C1_required_sections: str = CheckResult.NA
    C2_stop_conditions_6: str = CheckResult.NA
    C3_ac_checkbox_format: str = CheckResult.NA
    C4_vc_commands_present: str = CheckResult.NA
    C5_ac_vc_number_alignment: str = CheckResult.NA
    C6_no_subjective_phrasing: str = CheckResult.NA
    C7_required_skills_semantics: str = CheckResult.NA
    C8_outcome_concreteness: str = CheckResult.NA
    C9_runtime_applicability_present: str = CheckResult.NA
    C10_deferred_destination_present: str = CheckResult.NA
    C11_decision_tag_consistency: str = CheckResult.NA


@dataclass
class CheckerResult:
    verdict: str = "approve"
    deterministic_checks: DeterministicChecks = field(default_factory=DeterministicChecks)
    blocking_issues: list[str] = field(default_factory=list)
    non_blocking_improvements: list[str] = field(default_factory=list)
    issue_kind: str = "implementation"


def extract_section(body: str, section_name: str) -> str:
    """Extract text under a ## section heading until the next ## heading."""
    pattern = rf"^## {re.escape(section_name)}\s*$(.*?)(?=^## |\Z)"
    match = re.search(pattern, body, re.MULTILINE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def detect_issue_kind(labels: str, title: str) -> str:
    """Detect issue kind from labels and title."""
    if "tracking" in labels or "parent" in labels:
        return "tracking"
    if "phase/research" in labels or title.startswith("調査:"):
        return "research"
    if "phase/implementation" in labels or title.startswith("実装:"):
        return "implementation"
    # Default to implementation for unknown
    return "implementation"


def check_c1_required_sections(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C1: 必須セクション存在チェック"""
    if issue_kind not in ("implementation",):
        return CheckResult.NA, []

    failing = []
    for section in IMPLEMENTATION_REQUIRED_SECTIONS:
        pattern = rf"^## {re.escape(section)}"
        if not re.search(pattern, body, re.MULTILINE):
            failing.append(f"必須セクション '## {section}' が存在しない")

    if failing:
        return CheckResult.FAIL, failing
    return CheckResult.PASS, []


def check_c2_stop_conditions(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C2: Stop Conditions 6 項目以上（implementation のみ）"""
    if issue_kind != "implementation":
        return CheckResult.NA, []

    section = extract_section(body, "Stop Conditions")
    if not section:
        return CheckResult.FAIL, ["## Stop Conditions セクションが存在しない"]

    bullet_count = len(re.findall(r"^- ", section, re.MULTILINE))
    if bullet_count < 6:
        return CheckResult.FAIL, [f"Stop Conditions の項目数が {bullet_count} 件（6 件以上必要）"]
    return CheckResult.PASS, []


def check_c3_ac_checkbox_format(body: str) -> tuple[str, list[str]]:
    """C3: AC が - [ ] 形式"""
    section = extract_section(body, "Acceptance Criteria")
    if not section:
        return CheckResult.FAIL, ["## Acceptance Criteria セクションが存在しないか空"]

    checkbox_lines = re.findall(r"^- \[[ xX]\]", section, re.MULTILINE)
    if not checkbox_lines:
        return CheckResult.FAIL, ["AC に `- [ ]` 形式のチェックボックス行が見つからない"]
    return CheckResult.PASS, []


def check_c4_vc_commands_present(body: str) -> tuple[str, list[str]]:
    """C4: VC コマンド存在チェック"""
    section = extract_section(body, "Verification Commands")
    if not section:
        return CheckResult.FAIL, ["## Verification Commands セクションが存在しないか空"]

    # コマンド行: $ で始まる行、または ``` コードブロック内の非空行
    has_dollar_prefix = bool(re.search(r"^\$\s+\S", section, re.MULTILINE))
    has_code_block = bool(re.search(r"```", section))

    if not has_dollar_prefix and not has_code_block:
        return CheckResult.FAIL, ["Verification Commands に `$` 始まりのコマンド行またはコードブロックが見つからない"]
    return CheckResult.PASS, []


def check_c5_ac_vc_alignment(body: str) -> tuple[str, list[str]]:
    """C5: AC と VC の番号一致チェック"""
    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")

    ac_numbers = set(re.findall(r"\bAC(\d+)\b", ac_section))
    vc_numbers = set(re.findall(r"\bAC(\d+)\b", vc_section))

    if not ac_numbers:
        return CheckResult.FAIL, ["AC セクションに AC[N] 番号が見つからない"]
    if not vc_numbers:
        return CheckResult.FAIL, ["VC セクションに AC[N] 番号が見つからない"]

    missing_in_vc = ac_numbers - vc_numbers
    extra_in_vc = vc_numbers - ac_numbers

    issues = []
    if missing_in_vc:
        issues.append(f"AC番号 {sorted(missing_in_vc)} が VC に対応コマンドなし")
    if extra_in_vc:
        issues.append(f"VC に AC番号 {sorted(extra_in_vc)} があるが AC セクションに存在しない")

    if issues:
        return CheckResult.FAIL, issues
    return CheckResult.PASS, []


def check_c6_no_subjective_phrasing(body: str) -> tuple[str, list[str]]:
    """C6: 主観表現の混入チェック（AC / VC のみ）"""
    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")
    check_text = ac_section + "\n" + vc_section

    found = []
    for pattern in SUBJECTIVE_PATTERNS:
        if re.search(pattern, check_text):
            found.append(f"主観表現パターン '{pattern}' が AC/VC に含まれる")

    if found:
        return CheckResult.FAIL, found
    return CheckResult.PASS, []


def check_c7_required_skills_semantics(body: str) -> tuple[str, list[str]]:
    """C7: Required Skills にワークフロースキル / ドキュメントパスを含まない"""
    section = extract_section(body, "Required Skills")
    if not section or section.strip() in ("なし", "none", "N/A", ""):
        return CheckResult.PASS, []

    issues = []
    lines = section.splitlines()
    for line in lines:
        line = line.strip().lstrip("- ").strip()
        if not line:
            continue
        # ワークフロースキルチェック
        if line in WORKFLOW_SKILLS:
            issues.append(f"Required Skills にワークフロースキル '{line}' が含まれている（禁止）")
        # ドキュメントパスチェック
        if re.search(r"docs/|\.md$|^/", line):
            issues.append(f"Required Skills にドキュメントパス '{line}' が含まれている（禁止）")

    if issues:
        return CheckResult.FAIL, issues
    return CheckResult.PASS, []


def check_c8_outcome_concreteness(body: str) -> tuple[str, list[str]]:
    """C8: Outcome に抽象的パターンが含まれない"""
    section = extract_section(body, "Outcome")
    if not section:
        return CheckResult.FAIL, ["## Outcome セクションが存在しないか空"]

    found = []
    for pattern in VAGUE_OUTCOME_PATTERNS:
        if re.search(pattern, section):
            found.append(f"Outcome に抽象的表現パターン '{pattern}' が含まれる")

    if found:
        return CheckResult.FAIL, found
    return CheckResult.PASS, []


def check_c9_runtime_applicability(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C9: Runtime Verification Applicability セクション存在チェック"""
    has_section = bool(re.search(r"^## Runtime Verification Applicability", body, re.MULTILINE))

    if issue_kind == "implementation":
        if not has_section:
            return CheckResult.FAIL, ["implementation Issue に ## Runtime Verification Applicability セクションが存在しない（blocker）"]
        return CheckResult.PASS, []
    elif issue_kind in ("research", "tracking"):
        if not has_section:
            return CheckResult.WARN, ["research/tracking Issue に ## Runtime Verification Applicability セクションが存在しない（warn、approve を妨げない）"]
        return CheckResult.PASS, []
    else:
        if not has_section:
            return CheckResult.FAIL, ["## Runtime Verification Applicability セクションが存在しない"]
        return CheckResult.PASS, []


def check_c10_deferred_destination(body: str) -> tuple[str, list[str]]:
    """C10: deferred の検証先不明チェック"""
    section = extract_section(body, "Runtime Verification Applicability")
    if not section:
        return CheckResult.NA, []

    if "decision: deferred" not in section:
        return CheckResult.PASS, []

    # deferred の場合は deferred_destination または deferred_verification_condition が必要
    has_destination_type = bool(re.search(r"destination_type:", section))
    has_destination_ref = bool(re.search(r"destination_ref:", section))
    has_verification_condition = bool(re.search(r"deferred_verification_condition:", section))

    if not (has_destination_type and has_destination_ref) and not has_verification_condition:
        return CheckResult.FAIL, [
            "decision: deferred なのに deferred_destination（destination_type + destination_ref）または "
            "deferred_verification_condition が欠けている"
        ]
    return CheckResult.PASS, []


def check_c11_decision_tag_consistency(body: str) -> tuple[str, list[str]]:
    """C11: decision と runtime-verification タグの整合チェック"""
    rva_section = extract_section(body, "Runtime Verification Applicability")
    if not rva_section:
        return CheckResult.NA, []

    ac_section = extract_section(body, "Acceptance Criteria")

    # decision を取得
    decision_match = re.search(r"decision:\s*(\S+)", rva_section)
    if not decision_match:
        return CheckResult.NA, []

    decision = decision_match.group(1).strip()
    has_rv_tag = bool(re.search(r"<!--\s*runtime-verification:\s*true\s*-->", ac_section))

    if decision == "immediate" and not has_rv_tag:
        return CheckResult.FAIL, [
            "decision: immediate なのに AC に <!-- runtime-verification: true --> タグが 1 つもない（blocker）"
        ]
    elif decision in ("not_applicable", "deferred") and has_rv_tag:
        return CheckResult.FAIL, [
            f"decision: {decision} なのに AC に <!-- runtime-verification: true --> タグが存在する（矛盾 blocker）"
        ]

    return CheckResult.PASS, []


def run_checks(body: str, labels: str = "", title: str = "") -> CheckerResult:
    """Run all C1-C11 checks and return structured result."""
    issue_kind = detect_issue_kind(labels, title)
    result = CheckerResult(issue_kind=issue_kind)
    checks = result.deterministic_checks

    # C1
    checks.C1_required_sections, issues = check_c1_required_sections(body, issue_kind)
    result.blocking_issues.extend(issues)

    # C2
    checks.C2_stop_conditions_6, issues = check_c2_stop_conditions(body, issue_kind)
    result.blocking_issues.extend(issues)

    # C3
    checks.C3_ac_checkbox_format, issues = check_c3_ac_checkbox_format(body)
    result.blocking_issues.extend(issues)

    # C4
    checks.C4_vc_commands_present, issues = check_c4_vc_commands_present(body)
    result.blocking_issues.extend(issues)

    # C5
    checks.C5_ac_vc_number_alignment, issues = check_c5_ac_vc_alignment(body)
    result.blocking_issues.extend(issues)

    # C6
    checks.C6_no_subjective_phrasing, issues = check_c6_no_subjective_phrasing(body)
    result.blocking_issues.extend(issues)

    # C7
    checks.C7_required_skills_semantics, issues = check_c7_required_skills_semantics(body)
    result.blocking_issues.extend(issues)

    # C8
    checks.C8_outcome_concreteness, issues = check_c8_outcome_concreteness(body)
    result.blocking_issues.extend(issues)

    # C9
    checks.C9_runtime_applicability_present, issues = check_c9_runtime_applicability(body, issue_kind)
    # warn は blocking_issues に追加しない
    if checks.C9_runtime_applicability_present == CheckResult.FAIL:
        result.blocking_issues.extend(issues)
    elif checks.C9_runtime_applicability_present == CheckResult.WARN:
        result.non_blocking_improvements.extend(issues)

    # C10
    checks.C10_deferred_destination_present, issues = check_c10_deferred_destination(body)
    result.blocking_issues.extend(issues)

    # C11
    checks.C11_decision_tag_consistency, issues = check_c11_decision_tag_consistency(body)
    result.blocking_issues.extend(issues)

    # Verdict
    blocking_check_results = {
        CheckResult.FAIL,
    }
    all_check_values = [
        checks.C1_required_sections,
        checks.C2_stop_conditions_6,
        checks.C3_ac_checkbox_format,
        checks.C4_vc_commands_present,
        checks.C5_ac_vc_number_alignment,
        checks.C6_no_subjective_phrasing,
        checks.C7_required_skills_semantics,
        checks.C8_outcome_concreteness,
        checks.C9_runtime_applicability_present,
        checks.C10_deferred_destination_present,
        checks.C11_decision_tag_consistency,
    ]
    has_fail = any(v == CheckResult.FAIL for v in all_check_values)
    result.verdict = "needs-fix" if has_fail else "approve"

    return result


def fetch_issue_body(issue_number: int, repo: str) -> tuple[str, str, str]:
    """Fetch issue body, labels, and title from GitHub."""
    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "title,body,labels",
        "--jq", '.title + "\n---LABELS---\n" + (.labels | map(.name) | join(",")) + "\n---BODY---\n" + .body',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: gh issue view failed: {result.stderr}", file=sys.stderr)
        sys.exit(2)

    output = result.stdout
    parts = output.split("\n---LABELS---\n", 1)
    title = parts[0].strip() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    label_parts = rest.split("\n---BODY---\n", 1)
    labels = label_parts[0].strip() if label_parts else ""
    body = label_parts[1].strip() if len(label_parts) > 1 else ""

    return body, labels, title


def load_fixture_file(path: str) -> tuple[str, str, str]:
    """Load a fixture file (Markdown with optional YAML-like header)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    labels = ""
    title = ""
    body = content

    # Parse simple header if present
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            header = content[4:end]
            body = content[end + 5:].strip()
            for line in header.splitlines():
                if line.startswith("LABELS:"):
                    labels = line[len("LABELS:"):].strip()
                elif line.startswith("TITLE:"):
                    title = line[len("TITLE:"):].strip()

    return body, labels, title


def result_to_dict(result: CheckerResult) -> dict:
    """Convert CheckerResult to a dict for JSON output."""
    return {
        "verdict": result.verdict,
        "issue_kind": result.issue_kind,
        "deterministic_checks": {
            "C1_required_sections": result.deterministic_checks.C1_required_sections,
            "C2_stop_conditions_6": result.deterministic_checks.C2_stop_conditions_6,
            "C3_ac_checkbox_format": result.deterministic_checks.C3_ac_checkbox_format,
            "C4_vc_commands_present": result.deterministic_checks.C4_vc_commands_present,
            "C5_ac_vc_number_alignment": result.deterministic_checks.C5_ac_vc_number_alignment,
            "C6_no_subjective_phrasing": result.deterministic_checks.C6_no_subjective_phrasing,
            "C7_required_skills_semantics": result.deterministic_checks.C7_required_skills_semantics,
            "C8_outcome_concreteness": result.deterministic_checks.C8_outcome_concreteness,
            "C9_runtime_applicability_present": result.deterministic_checks.C9_runtime_applicability_present,
            "C10_deferred_destination_present": result.deterministic_checks.C10_deferred_destination_present,
            "C11_decision_tag_consistency": result.deterministic_checks.C11_decision_tag_consistency,
        },
        "blocking_issues": result.blocking_issues,
        "non_blocking_improvements": result.non_blocking_improvements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="C1〜C11 決定論的チェッカー — Issue 本文を機械的に検査する"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "-f", help="フィクスチャファイルパス（テスト用）")
    group.add_argument("--issue", "-i", type=int, help="GitHub Issue 番号")
    parser.add_argument("--repo", "-r", help="GitHub repo (owner/repo)。--issue と共に使用")
    parser.add_argument("--json", action="store_true", help="JSON 出力モード")
    args = parser.parse_args()

    if args.issue and not args.repo:
        print("ERROR: --issue には --repo が必要です", file=sys.stderr)
        sys.exit(2)

    if args.file:
        body, labels, title = load_fixture_file(args.file)
    else:
        body, labels, title = fetch_issue_body(args.issue, args.repo)

    result = run_checks(body, labels, title)
    output = result_to_dict(result)

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"verdict: {result.verdict}")
        print(f"issue_kind: {result.issue_kind}")
        print()
        print("deterministic_checks:")
        for key, val in output["deterministic_checks"].items():
            print(f"  {key}: {val}")
        if result.blocking_issues:
            print()
            print("blocking_issues:")
            for issue in result.blocking_issues:
                print(f"  - {issue}")
        if result.non_blocking_improvements:
            print()
            print("non_blocking_improvements:")
            for improvement in result.non_blocking_improvements:
                print(f"  - {improvement}")

    # Exit code: 0 = approve, 1 = needs-fix
    sys.exit(0 if result.verdict == "approve" else 1)


if __name__ == "__main__":
    main()
