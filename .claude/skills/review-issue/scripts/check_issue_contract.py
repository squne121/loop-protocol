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


def get_required_sections(issue_kind: str, template_path: str = ".github/ISSUE_TEMPLATE/implementation.yml") -> list:
    """Issue template から必須セクションを動的取得。未存在時はハードコードにフォールバック。"""
    if issue_kind != "implementation":
        return []

    import os
    import yaml as _yaml
    if os.path.exists(template_path):
        try:
            with open(template_path) as f:
                tmpl = _yaml.safe_load(f)
            required = [
                item["attributes"]["label"]
                for item in tmpl.get("body", [])
                if item.get("type") != "markdown"
                   and item.get("validations", {}).get("required", False)
                   and "label" in item.get("attributes", {})
            ]
            if required:
                return required
        except Exception:
            pass

    # fallback
    return IMPLEMENTATION_REQUIRED_SECTIONS


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


def detect_issue_kind(body: str, labels: str = "", title: str = "") -> str:
    """Issue kind を検出する。Machine-Readable Contract を最優先で参照。"""
    # 最優先: Machine-Readable Contract の issue_kind フィールド
    # ```yaml ... contract_schema_version ... issue_kind: <value> ... ``` を探す
    contract_match = re.search(
        r'```yaml\s*\n.*?contract_schema_version.*?\n.*?issue_kind:\s*(\S+)',
        body,
        re.DOTALL
    )
    if contract_match:
        kind = contract_match.group(1).strip().rstrip('"\'')
        if kind in ("implementation", "research", "tracking", "parent"):
            return kind

    # fallback: labels
    if "tracking" in labels or "parent" in labels:
        return "tracking"
    if "phase/research" in labels or title.startswith("調査:"):
        return "research"
    if "phase/implementation" in labels or title.startswith("実装:"):
        return "implementation"

    # fallback: title prefix
    if title.startswith(("実装:", "implement:", "perf:", "fix:", "docs:")):
        return "implementation"

    # Default to implementation for unknown
    return "implementation"


def check_c1_required_sections(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C1: 必須セクション存在チェック"""
    if issue_kind not in ("implementation",):
        return CheckResult.NA, []

    required = get_required_sections(issue_kind)
    failing = []
    for section in required:
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

    # コードブロック内のコマンド行を確認（$ または - で始まる行）
    code_blocks = re.findall(r'```[^\n]*\n(.*?)```', section, re.DOTALL)
    command_lines = []
    for block in code_blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith('$') or (stripped.startswith('-') and '`' in stripped):
                command_lines.append(stripped)

    if command_lines:
        return CheckResult.PASS, []

    # コードブロックが存在しない場合: コードブロック外のインライン backtick を確認
    # コードブロックを除去してからインライン backtick を確認する
    section_without_code_blocks = re.sub(r'```[^\n]*\n.*?```', '', section, flags=re.DOTALL)
    inline = re.findall(r'`[^`]+`', section_without_code_blocks)

    if not inline:
        return CheckResult.FAIL, ["VC に実行可能コマンドが見当たらない（$ / - で始まる行、またはインライン backtick が必要）"]

    return CheckResult.PASS, []


def check_c5_ac_vc_alignment(body: str) -> tuple[str, list[str]]:
    """C5: AC と VC の番号一致チェック"""
    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")

    if not ac_section or not vc_section:
        return CheckResult.NA, []

    # AC 番号を収集
    ac_numbers = set(re.findall(r'AC(\d+)', ac_section))

    # VC 内の AC 参照を収集（# AC1 形式のコメント）
    vc_ac_refs = set(re.findall(r'#\s*AC(\d+)', vc_section))

    # AC 番号と VC 参照が全て一致するか
    if not ac_numbers:
        return CheckResult.FAIL, ["AC セクションに AC[N] 番号が見つからない"]

    missing_in_vc = ac_numbers - vc_ac_refs

    if missing_in_vc:
        missing_list = [f"AC{n}" for n in sorted(missing_in_vc)]
        return CheckResult.FAIL, [
            f"以下の AC が VC に '# AC<N>' 形式でコメント参照されていない: {', '.join(missing_list)}"
        ]

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
    section = extract_section(body, "Runtime Verification Applicability")
    has_section = bool(re.search(r"^## Runtime Verification Applicability", body, re.MULTILINE))

    if issue_kind == "implementation":
        if not has_section:
            # セクション自体がない
            return CheckResult.LEGACY_MISSING, ["## Runtime Verification Applicability セクションがない（レガシー Issue）"]

        # decision: フィールドの確認
        decision_match = re.search(r'decision:\s*(\S+)', section)
        if not decision_match:
            return CheckResult.LEGACY_MISSING, ["decision: フィールドがない（レガシー Issue）"]

        decision = decision_match.group(1).strip()
        valid_decisions = {"not_applicable", "deferred", "immediate"}
        if decision not in valid_decisions:
            return CheckResult.FAIL, [f"decision: '{decision}' が不正（not_applicable / deferred / immediate のいずれかであること）"]

        return CheckResult.PASS, []

    elif issue_kind in ("research", "tracking"):
        if not has_section:
            return CheckResult.WARN, ["research/tracking Issue に ## Runtime Verification Applicability セクションが存在しない（warn、approve を妨げない）"]
        return CheckResult.PASS, []

    else:
        if not has_section:
            return CheckResult.WARN, ["## Runtime Verification Applicability セクションが推奨（非実装 Issue）"]
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
    issue_kind = detect_issue_kind(body, labels, title)
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
    if checks.C9_runtime_applicability_present in (CheckResult.FAIL, CheckResult.LEGACY_MISSING):
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
    has_fail = any(v in (CheckResult.FAIL, CheckResult.LEGACY_MISSING) for v in all_check_values)
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
    import datetime
    return {
        "verdict": result.verdict,
        "issue_kind": result.issue_kind,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
