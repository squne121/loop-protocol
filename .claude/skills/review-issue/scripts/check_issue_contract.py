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
import contextlib
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


class PreflightScope(str, Enum):
    """Valid values for # preflight-scope: annotation on VC commands."""
    PR_REVIEW_ONLY = "pr_review_only"
    RUNTIME_ONLY = "runtime_only"
    UNKNOWN = "unknown"  # fail-closed / human_judgment


@dataclass
class ParsedVcCommand:
    """A VC command parsed from the Verification Commands section, with optional annotation metadata.

    Fields:
        command: the raw command string (e.g. "$ rg -n foo bar.py")
        preflight_scope: PreflightScope value if # preflight-scope: annotation was directly above;
                         None if no annotation present.
        trivially_pass_reason: non-empty reason string if # trivially_pass: annotation was directly
                                above the command; None otherwise.
        classification: "executable" | "skipped"
        skip_reason_type: "preflight_scope" | "trivially_pass" | None (only set when skipped)
    """
    command: str
    preflight_scope: Optional[PreflightScope] = None
    trivially_pass_reason: Optional[str] = None
    classification: str = "executable"
    skip_reason_type: Optional[str] = None


def parse_vc_commands(vc_section: str) -> list[ParsedVcCommand]:
    """Parse VC commands from a Verification Commands section with annotation support.

    Rules:
    - Commands are lines starting with "$" inside ```bash code blocks.
    - A command directly preceded (no blank lines or non-annotation comments between) by:
        # preflight-scope: <value>  → sets preflight_scope, classification: skipped
        # trivially_pass: <reason>  → sets trivially_pass_reason, classification: skipped
    - Annotation comments themselves are NOT extracted as commands.
    - If a blank line or a non-annotation comment appears between annotation and command,
      the annotation is invalidated (annotation must be immediately above the command).
    - unknown preflight-scope values → PreflightScope.UNKNOWN, classification: skipped,
      skip_reason_type: "preflight_scope_human_judgment"

    Returns a list of ParsedVcCommand, one per extracted command.
    """
    results: list[ParsedVcCommand] = []

    # Extract all code blocks (bash or untyped)
    code_blocks = re.findall(r'```[^\n]*\n(.*?)```', vc_section, re.DOTALL)

    _preflight_scope_re = re.compile(r'^#\s*preflight-scope:\s*(.+)$')
    _trivially_pass_re = re.compile(r'^#\s*trivially_pass:\s*(.+)$')
    _annotation_re = re.compile(r'^#\s*(preflight-scope|trivially_pass):')

    for block in code_blocks:
        lines = block.splitlines()
        # State: pending annotation for the next command line
        pending_preflight_scope: Optional[str] = None
        pending_trivially_pass: Optional[str] = None
        # Track whether last non-blank line was an annotation (for invalidation)
        last_was_annotation = False

        for line in lines:
            stripped = line.strip()

            if not stripped:
                # Blank line: invalidate pending annotations
                pending_preflight_scope = None
                pending_trivially_pass = None
                last_was_annotation = False
                continue

            ps_match = _preflight_scope_re.match(stripped)
            tp_match = _trivially_pass_re.match(stripped)

            if ps_match:
                # This line is a # preflight-scope: annotation — do not emit as command
                # Invalidate any previously pending annotation (only last one counts)
                pending_preflight_scope = ps_match.group(1).strip()
                pending_trivially_pass = None  # reset other annotation
                last_was_annotation = True
                continue

            if tp_match:
                # This line is a # trivially_pass: annotation — do not emit as command
                pending_trivially_pass = tp_match.group(1).strip()
                pending_preflight_scope = None  # reset other annotation
                last_was_annotation = True
                continue

            # Non-annotation comment line: invalidate pending annotations (AC6)
            if stripped.startswith('#') and not _annotation_re.match(stripped):
                pending_preflight_scope = None
                pending_trivially_pass = None
                last_was_annotation = False
                continue

            # Command line: must start with "$" to be considered a VC command
            if stripped.startswith('$'):
                cmd = ParsedVcCommand(command=stripped)

                if pending_preflight_scope is not None:
                    scope_val = pending_preflight_scope
                    if scope_val == PreflightScope.PR_REVIEW_ONLY.value:
                        cmd.preflight_scope = PreflightScope.PR_REVIEW_ONLY
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "preflight_scope"
                    elif scope_val == PreflightScope.RUNTIME_ONLY.value:
                        cmd.preflight_scope = PreflightScope.RUNTIME_ONLY
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "preflight_scope"
                    else:
                        # unknown value: fail-closed as human_judgment
                        cmd.preflight_scope = PreflightScope.UNKNOWN
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "preflight_scope_human_judgment"

                elif pending_trivially_pass is not None:
                    reason = pending_trivially_pass
                    if reason:
                        cmd.trivially_pass_reason = reason
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "trivially_pass"

                results.append(cmd)
                # Reset pending annotations after consuming
                pending_preflight_scope = None
                pending_trivially_pass = None
                last_was_annotation = False
            else:
                # Non-command, non-annotation, non-blank line: invalidate annotations
                # (e.g. a comment like "# some other remark" — already handled above,
                # but also handles output lines etc.)
                pending_preflight_scope = None
                pending_trivially_pass = None
                last_was_annotation = False

    return results


# --- scope_cvs_in_scope_mismatch tokenization constants (Issue #396) ---

PATH_TOKEN_EXTENSIONS = (".md", ".py", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".toml", ".sh")
PATH_TOKEN_PREFIXES = ("docs/", ".claude/", ".github/")
PATH_TOKEN_STRIP_CHARS = ".,:;)]}>"

# SSOT: PATH_TOKEN_RE auto-generated from PATH_TOKEN_EXTENSIONS and PATH_TOKEN_PREFIXES (Blocker 4 fix)
_EXT_RE = "|".join(re.escape(ext.lstrip(".")) for ext in PATH_TOKEN_EXTENSIONS)
_PREFIX_RE = "|".join(re.escape(prefix.rstrip("/")) for prefix in PATH_TOKEN_PREFIXES)

# ASCII-only path components, Unicode path matching is out of scope (Non-blocking C fix).
# Blocker 3: trailing sentence-final punctuation (.,;) is included in the match then stripped via
# rstrip(PATH_TOKEN_STRIP_CHARS). Both extension branch and prefix branch allow optional trailing
# punctuation chars so that "src/foo.py." and "config/settings.yaml." are captured and normalized.
# The suffix group [.,;]? must be kept outside PATH_TOKEN_STRIP_CHARS rstrip so we only need
# the lookahead to handle the character AFTER the optional trailing punct.
_SENT_PUNCT = r"[.,;]?"  # optional sentence-final punctuation included in match; rstripped later
_PATH_BOUNDARY_END = r"(?=$|[\s:)\]}>\"。．、])"  # must NOT include . , ; (already in _SENT_PUNCT)

PATH_TOKEN_RE = re.compile(
    r"(?<![/A-Za-z0-9_.~-])"
    + r"(?:"
    + r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:" + _EXT_RE + r")" + _SENT_PUNCT
    + r"|"
    + r"(?:" + _PREFIX_RE + r")/(?:[A-Za-z0-9_./-]+)" + _SENT_PUNCT
    + r")"
    + _PATH_BOUNDARY_END
)

# Bullet pattern: matches "- ", "* ", "+ " and indented variants (up to 3 spaces) (Blocker 2 fix)
BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")

SIGNIFICANT_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{4,}")

STOP_TOKENS: frozenset[str] = frozenset({
    "issue", "scope", "current", "validated", "warning",
    "token", "tokens", "checker", "review", "script",
    "scripts", "test", "tests", "fixture", "fixtures",
    "implementation", "function", "detect", "result",
    "output", "outcome", "verification", "commands",
    "acceptance", "criteria", "allowed", "paths",
    "this", "that", "with", "from", "will", "also",
    "have", "been", "more", "than", "when", "then",
})

JACCARD_THRESHOLD = 0.3

# --- end of scope_cvs tokenization constants ---

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
    C12_product_trace_fields_structure: str = CheckResult.NA
    C13_vc_preflight_decision_consistency: str = CheckResult.NA


@dataclass
class CheckerResult:
    verdict: str = "approve"
    deterministic_checks: DeterministicChecks = field(default_factory=DeterministicChecks)
    blocking_issues: list[str] = field(default_factory=list)
    non_blocking_improvements: list = field(default_factory=list)  # list of dict {code, severity, evidence, suggested_action}
    diff_proposal: dict = field(default_factory=lambda: {"add": [], "remove": [], "rewrite": []})
    issue_kind: str = "implementation"


PLACEHOLDER_VALUES = {"", "tbd", "todo", "none", "n/a", "na", "<tbd>", "<todo>"}
PLACEHOLDER_PATTERN = re.compile(r"^\s*<[^>]+>\s*$")  # <...> 形式
REQUIREMENT_ID_PATTERN = re.compile(r"^REQ-\d{3,}$")
SOURCE_TASK_ID_PATTERN = re.compile(r"^(T|TASK-)\d{3,}$")


def _add_warning(result: "CheckerResult", code: str, severity: str, evidence: list, suggested_action: str) -> None:
    """Append a structured non_blocking_improvement entry."""
    result.non_blocking_improvements.append({
        "code": code,
        "severity": severity,
        "evidence": evidence,
        "suggested_action": suggested_action,
    })


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


def check_c13_vc_preflight_decision_consistency(
    vc_preflight_json_path: Optional[str] = None,
) -> tuple[str, list[str]]:
    """C13: VC preflight JSON (if provided) has consistent decision values.

    Applicability: only if --vc-preflight-json path is provided.
    If not provided, return NA (not PASS).

    Checks:
      - All entries in vc_preflight JSON results have valid decision field
      - decision values are in (go, blocked, human_judgment)
      - skipped results have verification_owner and runtime_verification_required fields

    戻り値: (CheckResult, list[failure_message])

    Note: category is regression_gate for both pass and fail outcomes.
    The pass/fail distinction is carried by classification (expected_pass vs blocked)
    and decision (go vs blocked). Downstream consumers MUST read classification
    for the routing-canonical pass/fail signal.
    """
    if not vc_preflight_json_path:
        return CheckResult.NA, []

    try:
        with open(vc_preflight_json_path) as f:
            preflight_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return CheckResult.FAIL, [f"Failed to load or parse VC preflight JSON: {vc_preflight_json_path}"]

    issues = []
    results = preflight_data.get("results", [])

    valid_decisions = {"go", "blocked", "human_judgment"}

    for result in results:
        decision = result.get("decision")
        if decision not in valid_decisions:
            issues.append(f"AC {result.get('ac', 'UNKNOWN')}: invalid decision '{decision}'")

        # Check skipped result metadata
        if result.get("classification") == "skipped":
            if "verification_owner" not in result:
                issues.append(f"AC {result.get('ac', 'UNKNOWN')}: skipped result missing verification_owner")
            if "runtime_verification_required" not in result:
                issues.append(f"AC {result.get('ac', 'UNKNOWN')}: skipped result missing runtime_verification_required")

    if issues:
        return CheckResult.FAIL, issues
    return CheckResult.PASS, []


def check_c12_product_trace_fields_structure(body: str) -> tuple[str, list[str]]:
    """C12: Product Spec / task-lineage Issue で trace fields の構造を検査する。

    Applicable 条件: body に `## Product Spec Context`、`## Machine-Readable Contract` 内の
    product trace fields、または `product_spec_id` / `requirement_id` / `source_task_id`
    の言及があるとき。

    Applicable な場合、以下を要求:
      - product_spec_id / requirement_id / source_task_id の 3 fields が全て存在し non-placeholder
      - requirement_id は REQ-\\d{3,} 形式
      - source_task_id は (T|TASK-)\\d{3,} 形式
      - product_spec_id は non-empty / non-placeholder のみ確認（canonical format は固定しない）
    """
    # Applicability detection (PR #390 REQUEST_CHANGES blocker 1 対応):
    # 本文全体に trace field 語を含むだけで applicable にすると、Out of Scope や
    # spec 説明文に出現する言及まで誤って C12 対象にしてしまう。Machine-Readable
    # Contract の YAML / Product Spec Context セクションに `<field>:` 形式で
    # **構造化された** trace field が存在する場合に限って applicable とする。
    #
    # PR #390 review-2 blocker 3 対応: MRC YAML は yaml.safe_load で parse し、
    # inline comment / quote / null / folded scalar を YAML semantics で正しく扱う。
    # parse 失敗時は regex fallback に降りる（既存挙動を破壊しない）。
    mrc_yaml_text = ""
    mrc_parsed: dict = {}
    mrc_yaml_parse_failed = False
    mrc_match = re.search(
        r"```yaml\s*\n(.*?contract_schema_version.*?)\n```",
        body,
        re.DOTALL,
    )
    if mrc_match:
        mrc_yaml_text = mrc_match.group(1)
        try:
            import yaml as _yaml  # noqa: PLC0415
            loaded = _yaml.safe_load(mrc_yaml_text)
            if isinstance(loaded, dict):
                mrc_parsed = loaded
        except Exception:
            mrc_yaml_parse_failed = True
    psc_section = extract_section(body, "Product Spec Context")
    structured_trace_text = mrc_yaml_text + "\n" + psc_section

    has_product_spec_context = bool(psc_section)
    has_trace_field_mention = bool(re.search(
        r"\b(product_spec_id|requirement_id|source_task_id)\s*:",
        structured_trace_text,
    ))
    # task-lineage marker: structured field か、tasks.md / generated task 由来の宣言文
    has_task_lineage_marker = bool(re.search(
        r"^\s*-?\s*(generated_from_task|task_lineage|source_task)\s*:",
        body,
        re.MULTILINE,
    )) or bool(re.search(
        r"\b(generated\s+from\s+tasks?\.md|from\s+tasks?\.md|task_materialization|generated\s+task)\b",
        body,
        re.IGNORECASE,
    ))

    applicable = has_product_spec_context or has_trace_field_mention or has_task_lineage_marker
    if not applicable:
        return CheckResult.NA, []

    # Extract trace fields:
    # 1) MRC YAML が parse 成功した場合: mrc_parsed[field] を優先（inline comment 等を YAML semantics で除去）
    # 2) それ以外（PSC セクション / parse 失敗時）: regex fallback で structured_trace_text を走査
    # (PR #390 REQUEST_CHANGES blocker 1 + review-2 blocker 3 対応)
    def _extract_field(field_name: str) -> Optional[str]:
        if field_name in mrc_parsed:
            v = mrc_parsed.get(field_name)
            if v is None:
                return None
            return str(v).strip()
        m = re.search(
            rf'^\s*-?\s*{re.escape(field_name)}\s*:\s*["\']?([^"\'\n#]*?)["\']?\s*(?:#.*)?$',
            structured_trace_text,
            re.MULTILINE,
        )
        if m:
            return m.group(1).strip()
        return None

    def _is_placeholder(v: Optional[str]) -> bool:
        if v is None:
            return True
        norm = v.strip().lower()
        if norm in PLACEHOLDER_VALUES:
            return True
        if PLACEHOLDER_PATTERN.match(v):
            return True
        return False

    product_spec_id = _extract_field("product_spec_id")
    requirement_id = _extract_field("requirement_id")
    source_task_id = _extract_field("source_task_id")

    failures = []
    if _is_placeholder(product_spec_id):
        failures.append("product_spec_id が欠落または placeholder")
    if _is_placeholder(requirement_id):
        failures.append("requirement_id が欠落または placeholder")
    elif not REQUIREMENT_ID_PATTERN.match(requirement_id):
        failures.append(f"requirement_id '{requirement_id}' が REQ-\\d{{3,}} 形式に一致しない")
    if _is_placeholder(source_task_id):
        failures.append("source_task_id が欠落または placeholder")
    elif not SOURCE_TASK_ID_PATTERN.match(source_task_id):
        failures.append(f"source_task_id '{source_task_id}' が (T|TASK-)\\d{{3,}} 形式に一致しない")

    if failures:
        return CheckResult.FAIL, failures
    return CheckResult.PASS, []


def _bullet_tokens(section: str) -> set[str]:
    """Extract tokens from bullet lines in a section using 3-pass tokenization.

    Pass 1: backtick-quoted tokens (e.g. `foo.py`)
    Pass 2: bare path tokens matching PATH_TOKEN_RE (with extension or prefix)
    Pass 3: ASCII significant tokens matching SIGNIFICANT_TOKEN_RE (lowercased, STOP_TOKENS excluded)

    Bullet markers supported: "- ", "* ", "+ " and indented variants (up to 3 spaces).
    Scope: ASCII / English natural-language tokens only.
    Japanese text without path/backtick tokens yields 0 tokens (known limitation).
    Tokens are lowercased for normalization.
    """
    tokens: set[str] = set()
    for line in section.splitlines():
        m = BULLET_RE.match(line)
        if not m:
            continue

        content = m.group(1)  # content after bullet marker

        # Pass 1: backtick-quoted tokens
        for tok in re.findall(r"`([^`]+)`", content):
            cleaned = tok.strip().rstrip(PATH_TOKEN_STRIP_CHARS)
            if cleaned:
                # normalize ./prefix
                if cleaned.startswith("./"):
                    cleaned = cleaned[2:]
                tokens.add(cleaned.lower())

        # Pass 2: bare path tokens
        for match in PATH_TOKEN_RE.finditer(content):
            tok = match.group(0).rstrip(PATH_TOKEN_STRIP_CHARS)
            if tok:
                if tok.startswith("./"):
                    tok = tok[2:]
                tokens.add(tok.lower())

        # Pass 3: ASCII significant tokens
        for match in SIGNIFICANT_TOKEN_RE.finditer(content):
            tok = match.group(0).lower()
            if tok not in STOP_TOKENS:
                tokens.add(tok)

    return tokens


def detect_warning_scope_cvs_in_scope_mismatch(body: str, result: CheckerResult) -> None:
    """non_blocking: Current Validated Scope と In Scope の bullet 集合の乖離検出。"""
    cvs = extract_section(body, "Current Validated Scope")
    in_scope = extract_section(body, "In Scope")
    if not cvs or not in_scope:
        return

    cvs_tokens = _bullet_tokens(cvs)
    in_scope_tokens = _bullet_tokens(in_scope)
    if not cvs_tokens or not in_scope_tokens:
        return

    overlap = cvs_tokens & in_scope_tokens
    union = cvs_tokens | in_scope_tokens
    jaccard = len(overlap) / len(union) if union else 1.0
    # Substantial divergence: less than JACCARD_THRESHOLD overlap
    if union and jaccard < JACCARD_THRESHOLD:
        missing_from_cvs = sorted(in_scope_tokens - cvs_tokens)[:10]
        missing_from_in_scope = sorted(cvs_tokens - in_scope_tokens)[:10]
        # evidence: list[str] — machine-readable shape (Blocker 1: keep as list[str])
        evidence = [
            f"scope token jaccard {jaccard:.3f} < {JACCARD_THRESHOLD}",
            f"Current Validated Scope tokens: {sorted(cvs_tokens)[:10]}",
            f"In Scope tokens: {sorted(in_scope_tokens)[:10]}",
        ]
        # details: dict — structured info separated from evidence (Blocker 1: new field)
        details = {
            "jaccard": round(jaccard, 4),
            "overlap": sorted(overlap)[:10],
            "missing_from_cvs": missing_from_cvs,
            "missing_from_in_scope": missing_from_in_scope,
        }
        result.non_blocking_improvements.append({
            "code": "scope_cvs_in_scope_mismatch",
            "severity": "warning",
            "evidence": evidence,
            "details": details,
            "suggested_action": "Current Validated Scope と In Scope の対象ファイル/対象範囲を揃えるか、乖離理由を Background / Scope Delta に記載する",
        })


def detect_warning_vc_untracked_false_negative(body: str, result: CheckerResult) -> None:
    """non_blocking: `git status --porcelain | grep -v "^?"` 型の偽陰性パターン検出。"""
    vc = extract_section(body, "Verification Commands")
    if not vc:
        return

    matches = re.findall(
        r"git\s+status\s+--porcelain[^\n]*\|\s*grep\s+-v\s+[\"']?\^[?][\"']?",
        vc,
    )
    if matches:
        _add_warning(
            result,
            code="vc_untracked_false_negative_pattern",
            severity="warning",
            evidence=[m[:120] for m in matches[:3]],
            suggested_action="`grep -v \"^?\"` は untracked 行を除外するため、unstaged 変更を見逃す。検証対象を literal で列挙する形式に変更する",
        )


def detect_warning_vc_negative_grep_without_literal_inventory(body: str, result: CheckerResult) -> None:
    """non_blocking: 削除確認 VC が削除対象 literal を列挙せず否定 grep のみで完了確認している形を検出。"""
    vc = extract_section(body, "Verification Commands")
    ac = extract_section(body, "Acceptance Criteria")
    if not vc:
        return

    # Triggers: deletion intent in AC/VC + negation grep pattern in VC
    deletion_intent = bool(re.search(r"(削除|deletion|removed?\b|delete\b)", ac + "\n" + vc, re.IGNORECASE))
    negation_patterns = re.findall(
        r"(?:^|\n)\s*\$?\s*!\s*(?:rg|grep)\b[^\n]+",
        vc,
    )
    if not deletion_intent or not negation_patterns:
        return

    # Heuristic: literal inventory absent if there's no `test -f <path>` or rg listing deletion targets.
    # Look for explicit removal-target enumeration: `removed:` section, or `- [ ] AC.*削除.*<literal>`
    has_literal_inventory = bool(re.search(r"(removed_paths|削除対象|deleted_files|literal_list)\s*:", vc + "\n" + body))
    if not has_literal_inventory:
        _add_warning(
            result,
            code="vc_negative_grep_without_literal_inventory",
            severity="warning",
            evidence=[m.strip()[:120] for m in negation_patterns[:3]],
            suggested_action="削除確認 VC では削除対象 literal を明示的に列挙し、`test ! -f <path>` 形式または `removed_paths:` リストで個別確認する",
        )


def get_required_section_placeholders(
    issue_kind: str,
    template_path: str = ".github/ISSUE_TEMPLATE/implementation.yml",
) -> dict[str, str]:
    """ISSUE_TEMPLATE の attributes.label → attributes.placeholder map を返す。

    PR #390 REQUEST_CHANGES blocker 3 対応:
    C1 skeleton を `## <label>` + 固定 TODO ではなく、template の placeholder 由来にする。
    """
    if issue_kind != "implementation":
        return {}

    import os
    import yaml as _yaml
    if not os.path.exists(template_path):
        return {}
    try:
        with open(template_path) as f:
            tmpl = _yaml.safe_load(f)
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    for item in tmpl.get("body", []):
        if item.get("type") == "markdown":
            continue
        attrs = item.get("attributes", {})
        label = attrs.get("label")
        if not label:
            continue
        placeholder = attrs.get("placeholder") or attrs.get("description") or ""
        mapping[label] = placeholder
    return mapping


def generate_c1_missing_section_skeleton(
    missing_sections: list[str],
    placeholders: Optional[dict[str, str]] = None,
) -> list[dict]:
    """C1 fail 時に missing section ごとの diff_proposal.add エントリを生成する。

    各エントリは sentinel marker `missing_section_skeleton` を含み、impl-review-loop / issue-author
    が機械的に skeleton を挿入できる形式にする。skeleton 本文は ISSUE_TEMPLATE の
    `attributes.placeholder` を優先し、無い場合のみ fallback TODO 文字列を使う
    (PR #390 REQUEST_CHANGES blocker 3 対応)。
    """
    placeholders = placeholders or {}
    entries = []
    for section in missing_sections:
        placeholder_text = placeholders.get(section, "").strip()
        if placeholder_text:
            body_block = placeholder_text
        else:
            body_block = (
                f"<!-- TODO: {section} を記述する。"
                "詳細は .github/ISSUE_TEMPLATE/implementation.yml 参照 -->"
            )
        entries.append({
            "kind": "missing_section_skeleton",
            "section": section,
            "placeholder_source": "template" if placeholder_text else "fallback_todo",
            "skeleton": f"## {section}\n\n{body_block}\n",
        })
    return entries


def run_checks(body: str, labels: str = "", title: str = "", vc_preflight_json_path: Optional[str] = None) -> CheckerResult:
    """Run all C1-C13 checks and return structured result."""
    issue_kind = detect_issue_kind(body, labels, title)
    result = CheckerResult(issue_kind=issue_kind)
    checks = result.deterministic_checks

    # C1
    checks.C1_required_sections, issues = check_c1_required_sections(body, issue_kind)
    result.blocking_issues.extend(issues)
    # C1 fail 時: missing section ごとの skeleton を diff_proposal.add に同梱する
    if checks.C1_required_sections == CheckResult.FAIL and issues:
        missing_sections = []
        for msg in issues:
            m = re.search(r"必須セクション '## ([^']+)' が存在しない", msg)
            if m:
                missing_sections.append(m.group(1))
        if missing_sections:
            placeholders = get_required_section_placeholders(issue_kind)
            result.diff_proposal["add"].extend(
                generate_c1_missing_section_skeleton(missing_sections, placeholders)
            )

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
        for msg in issues:
            _add_warning(
                result,
                code="c9_runtime_applicability_missing",
                severity="warning",
                evidence=[msg],
                suggested_action="implementation 以外の Issue でも `## Runtime Verification Applicability` セクションの追加を推奨する",
            )

    # C10
    checks.C10_deferred_destination_present, issues = check_c10_deferred_destination(body)
    result.blocking_issues.extend(issues)

    # C11
    checks.C11_decision_tag_consistency, issues = check_c11_decision_tag_consistency(body)
    result.blocking_issues.extend(issues)

    # C12: Product trace fields structure
    checks.C12_product_trace_fields_structure, issues = check_c12_product_trace_fields_structure(body)
    result.blocking_issues.extend(issues)

    # C13: VC preflight decision consistency (if JSON provided)
    checks.C13_vc_preflight_decision_consistency, issues = check_c13_vc_preflight_decision_consistency(vc_preflight_json_path)
    result.blocking_issues.extend(issues)

    # Non-blocking warnings（structured: code/severity/evidence/suggested_action）
    detect_warning_scope_cvs_in_scope_mismatch(body, result)
    detect_warning_vc_untracked_false_negative(body, result)
    detect_warning_vc_negative_grep_without_literal_inventory(body, result)

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
        checks.C12_product_trace_fields_structure,
        checks.C13_vc_preflight_decision_consistency,
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
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            "C12_product_trace_fields_structure": result.deterministic_checks.C12_product_trace_fields_structure,
            "C13_vc_preflight_decision_consistency": result.deterministic_checks.C13_vc_preflight_decision_consistency,
        },
        "blocking_issues": result.blocking_issues,
        "non_blocking_improvements": result.non_blocking_improvements,
        "diff_proposal": result.diff_proposal,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="C1〜C13 決定論的チェッカー — Issue 本文を機械的に検査する"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "-f", help="フィクスチャファイルパス（テスト用）")
    group.add_argument("--issue", "-i", type=int, help="GitHub Issue 番号")
    parser.add_argument("--repo", "-r", help="GitHub repo (owner/repo)。--issue と共に使用")
    parser.add_argument("--json", action="store_true", help="JSON 出力モード")
    parser.add_argument("--vc-preflight-json", help="VC preflight JSON path for C13 check")
    args = parser.parse_args()

    if args.issue and not args.repo:
        print("ERROR: --issue には --repo が必要です", file=sys.stderr)
        sys.exit(2)

    if args.json:
        # In --json mode: redirect stdout to stderr during fetch/check so that any
        # incidental stdout (e.g. from sub-libraries) does not pollute the JSON output.
        with contextlib.redirect_stdout(sys.stderr):
            if args.file:
                body, labels, title = load_fixture_file(args.file)
            else:
                body, labels, title = fetch_issue_body(args.issue, args.repo)
            result = run_checks(body, labels, title, args.vc_preflight_json)
            output = result_to_dict(result)
        # Emit JSON exclusively to stdout (after redirect is restored)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        if args.file:
            body, labels, title = load_fixture_file(args.file)
        else:
            body, labels, title = fetch_issue_body(args.issue, args.repo)
        result = run_checks(body, labels, title, args.vc_preflight_json)
        output = result_to_dict(result)
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
