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

Caller contract for --json mode:
    - stdout carries the contract-check JSON ONLY (a single final JSON object).
    - stderr carries diagnostics (warnings, deprecation notices, progress output).
    - Callers MUST parse JSON from stdout only and MUST NOT merge stderr into
      stdout (e.g. do not use stderr=subprocess.STDOUT). Treating stderr
      diagnostics as part of the JSON payload will break parsing.

Exit codes:
    0: すべてのチェックが pass（verdict: approve）
    1: 1 つ以上のチェックが fail（verdict: needs-fix）
    2: 入力エラー / 実行エラー
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# prose_boundary_policy の heading_policy を import（#654）
# ---------------------------------------------------------------------------
# check_issue_contract.py の scripts/ は review-issue/scripts/ にあるが、
# prose_boundary_policy.py は create-issue/scripts/ にある。
# sys.path に create-issue/scripts/ を追加してから import する。
_CREATE_ISSUE_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "create-issue" / "scripts"
)
if str(_CREATE_ISSUE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS))

# #1135: shared, section-bound MRC parser + repo path-policy SSOT
from mrc_contract_parser import parse_machine_readable_contract  # noqa: E402
from path_classification import (  # noqa: E402
    extract_allowed_paths as pc_extract_allowed_paths,
    has_code_or_runtime_scope,
    is_docs_only_allowed_paths,
)

# ---------------------------------------------------------------------------
# vc_contract_syntax を import（#993: shared VC grammar parser）
# ---------------------------------------------------------------------------
_VC_SYNTAX_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "issue-contract-review" / "scripts"
)
if str(_VC_SYNTAX_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_VC_SYNTAX_SCRIPTS))
_IMPL_REVIEW_LOOP_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "impl-review-loop" / "scripts"
)
if str(_IMPL_REVIEW_LOOP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_IMPL_REVIEW_LOOP_SCRIPTS))

try:
    from vc_contract_syntax import parse_verification_commands_section as _parse_vc_section
    _VC_SECTION_PARSER_AVAILABLE = True
except ImportError:
    _VC_SECTION_PARSER_AVAILABLE = False

    def _parse_vc_section(vc_section: str):  # type: ignore[misc]
        return None

try:
    from evaluate_product_spec_gate import evaluate_product_spec_payload  # noqa: E402
except ImportError:
    evaluate_product_spec_payload = None  # type: ignore[assignment]

try:
    from prose_boundary_policy import (
        lookup_heading_policy as _lookup_heading_policy,
        parse_atx_heading_line as _parse_atx_heading_line,
        iter_markdown_blocks as _iter_markdown_blocks,
        BLOCK_KIND_CODE_FENCE as _BLOCK_KIND_CODE_FENCE,
    )
    _HEADING_POLICY_AVAILABLE = True
except ImportError:
    _HEADING_POLICY_AVAILABLE = False

    def _lookup_heading_policy(heading_text: str):  # type: ignore[misc]
        return None

    def _parse_atx_heading_line(line: str):  # type: ignore[misc]
        return None

    def _iter_markdown_blocks(text: str):  # type: ignore[misc]
        """Fallback: yield entire text as a single prose block."""
        if text:
            yield text, "human_prose"

    _BLOCK_KIND_CODE_FENCE = "code_fence"


# ---------------------------------------------------------------------------
# ISSUE_KIND_POLICY_V1 SSOT loader
# ---------------------------------------------------------------------------
# Canonical source: docs/dev/github-ops.md ## ISSUE_KIND_POLICY_V1
# Local allowlist definitions are prohibited (SSOT single-source rule).

_ISSUE_KIND_POLICY_CACHE: "dict | None" = None


def _find_repo_root_for_contract() -> Path:
    """Find repository root by walking up to find .git directory."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume .claude/skills/review-issue/scripts/
    return Path(__file__).resolve().parent.parent.parent.parent.parent


class IssueKindPolicyLoadError(RuntimeError):
    """Raised when ISSUE_KIND_POLICY_V1 cannot be loaded from SSOT.

    Fail-closed: callers must not silently substitute a hardcoded fallback.
    If this exception escapes to detect_issue_kind, it returns UNKNOWN_ISSUE_KIND_SENTINEL.
    """


def _load_issue_kind_policy(repo_root: "Path | None" = None) -> dict:
    """Load ISSUE_KIND_POLICY_V1 from docs/dev/github-ops.md.

    Returns a dict with keys:
      - canonical_kinds: frozenset[str]
      - aliases: dict[str, str]
      - unknown_kind_policy: str  ("block")
      - unknown_kind_reason_code: str

    Raises IssueKindPolicyLoadError if the SSOT file is missing, the
    ISSUE_KIND_POLICY_V1 block cannot be found/parsed, or the yaml library
    is unavailable.  No silent fallback — callers must handle the error.
    """
    global _ISSUE_KIND_POLICY_CACHE
    if _ISSUE_KIND_POLICY_CACHE is not None:
        return _ISSUE_KIND_POLICY_CACHE

    if repo_root is None:
        repo_root = _find_repo_root_for_contract()

    ssot_path = repo_root / "docs" / "dev" / "github-ops.md"
    if not ssot_path.exists():
        raise IssueKindPolicyLoadError(
            f"SSOT file not found: {ssot_path}. "
            "Cannot load ISSUE_KIND_POLICY_V1 — fail-closed."
        )

    try:
        import yaml as _yaml
    except ImportError as exc:
        raise IssueKindPolicyLoadError(
            "PyYAML is not available; cannot parse ISSUE_KIND_POLICY_V1."
        ) from exc

    try:
        text = ssot_path.read_text(encoding="utf-8")
        match = re.search(r"```yaml\s*\nISSUE_KIND_POLICY_V1:(.*?)```", text, re.DOTALL)
        if not match:
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 fenced YAML block not found in {ssot_path}. "
                "Ensure the block starts with ```yaml on a line followed by 'ISSUE_KIND_POLICY_V1:'."
            )

        yaml_content = "ISSUE_KIND_POLICY_V1:" + match.group(1)
        parsed = _yaml.safe_load(yaml_content)
        if not isinstance(parsed, dict) or "ISSUE_KIND_POLICY_V1" not in parsed:
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 YAML parse produced unexpected structure in {ssot_path}."
            )

        policy = parsed["ISSUE_KIND_POLICY_V1"]
        if not isinstance(policy, dict):
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 value is not a mapping in {ssot_path}."
            )

        canonical_kinds = frozenset(policy.get("canonical_kinds") or [])
        aliases_raw = policy.get("aliases") or {}
        aliases = {str(k): str(v) for k, v in aliases_raw.items()} if isinstance(aliases_raw, dict) else {}
        unknown_kind_policy = str(policy.get("unknown_kind_policy", "block"))
        unknown_kind_reason_code = str(policy.get("unknown_kind_reason_code", "unknown_issue_kind"))

        result: dict = {
            "canonical_kinds": canonical_kinds,
            "aliases": aliases,
            "unknown_kind_policy": unknown_kind_policy,
            "unknown_kind_reason_code": unknown_kind_reason_code,
        }
        _ISSUE_KIND_POLICY_CACHE = result
        return result
    except IssueKindPolicyLoadError:
        raise
    except Exception as exc:
        raise IssueKindPolicyLoadError(
            f"Unexpected error while loading ISSUE_KIND_POLICY_V1 from {ssot_path}: {exc}"
        ) from exc


def _clear_issue_kind_policy_cache() -> None:
    """Clear the SSOT cache (for testing only)."""
    global _ISSUE_KIND_POLICY_CACHE
    _ISSUE_KIND_POLICY_CACHE = None


# Sentinel value returned by detect_issue_kind when kind is not in SSOT allowlist/aliases.
UNKNOWN_ISSUE_KIND_SENTINEL = "unknown_issue_kind"


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


REVIEW_ISSUE_RESULT_SCHEMA_VERSION = "review_issue_result/v1"
REVIEW_ISSUE_RESULT_SCHEMA = "REVIEW_ISSUE_RESULT_V1"
REVIEW_ISSUE_RESULT_SCHEMA_FILE = (
    Path(__file__).resolve().parent.parent / "schemas" / "review_issue_result_v1.json"
)
REVIEW_ISSUE_CHECKER_ARTIFACT_SCHEMAS = frozenset(
    {
        REVIEW_ISSUE_RESULT_SCHEMA,
        "CHECK_ISSUE_CONTRACT_V1",
        "product_spec_check/v1",
    }
)
REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER = "deterministic_domain_blocker"
REVIEW_ISSUE_FINDING_KIND_CHECKER_GAP = "checker_gap"
REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN = "heuristic_concern"
VALID_REVIEW_FINDING_KINDS = {
    REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
    REVIEW_ISSUE_FINDING_KIND_CHECKER_GAP,
    REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
}
VALID_REVIEW_ISSUE_STATUS = ("ok", "failed")


def _sha256_prefixed(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _append_findings(
    result: "CheckerResult",
    issues: list[str],
    deterministic_domain_key: str,
    *,
    finding_kind: str,
    blocking: bool,
    checker_evidence: Optional[list[dict]] = None,
    reviewer_blocker_code: Optional[str] = None,
) -> None:
    evidence_entries = list(checker_evidence) if checker_evidence is not None else []

    finding_kind = _validate_finding_kind(finding_kind)
    if finding_kind == REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER and blocking:
        evidence_entries = [
            entry
            for entry in evidence_entries
            if _is_valid_deterministic_evidence(result, entry)
        ]
        if not evidence_entries:
            # Deterministic blocker は current body に結びつく実 evidence が無ければ fail-closed。
            finding_kind = REVIEW_ISSUE_FINDING_KIND_CHECKER_GAP
            blocking = False

    for issue in issues:
        finding = {
            "finding_kind": finding_kind,
            "deterministic_domain_key": deterministic_domain_key,
            "blocking": bool(blocking),
            "checker_evidence": evidence_entries,
            "message": issue,
        }
        result.findings.append(finding)
        if (
            reviewer_blocker_code
            and finding_kind == REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER
            and blocking
        ):
            result.structured_blockers.append(
                {
                    "code": reviewer_blocker_code,
                    "message": issue,
                    "finding_kind": finding_kind,
                    "deterministic_domain_key": deterministic_domain_key,
                    "blocking": True,
                    "checker_evidence": evidence_entries,
                }
            )


def _validate_finding_kind(kind: str) -> str:
    if kind in VALID_REVIEW_FINDING_KINDS:
        return kind
    raise ValueError(f"Unsupported finding_kind: {kind!r}")


def _validate_status(status: str) -> str:
    if status in VALID_REVIEW_ISSUE_STATUS:
        return status
    raise ValueError(f"Unsupported REVIEW_ISSUE_RESULT status: {status!r}")


def _is_valid_deterministic_evidence(result: "CheckerResult", entry: dict) -> bool:
    required_fields = (
        "source_check",
        "rule_id",
        "category",
        "artifact_path",
        "artifact_schema",
        "body_sha256",
        "iteration_id",
    )
    for field_name in required_fields:
        value = entry.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return False
    if entry.get("artifact_schema") not in REVIEW_ISSUE_CHECKER_ARTIFACT_SCHEMAS:
        return False
    if entry.get("body_sha256") != result.body_sha256:
        return False
    return True


def _load_review_issue_result_schema() -> dict:
    return json.loads(REVIEW_ISSUE_RESULT_SCHEMA_FILE.read_text(encoding="utf-8"))


def _validate_review_issue_result_payload(payload: dict) -> None:
    import jsonschema

    jsonschema.validate(instance=payload, schema=_load_review_issue_result_schema())


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
        _last_was_annotation = False

        for line in lines:
            stripped = line.strip()

            if not stripped:
                # Blank line: invalidate pending annotations
                pending_preflight_scope = None
                pending_trivially_pass = None
                _last_was_annotation = False
                continue

            ps_match = _preflight_scope_re.match(stripped)
            tp_match = _trivially_pass_re.match(stripped)

            if ps_match:
                # This line is a # preflight-scope: annotation — do not emit as command
                # Invalidate any previously pending annotation (only last one counts)
                pending_preflight_scope = ps_match.group(1).strip()
                pending_trivially_pass = None  # reset other annotation
                _last_was_annotation = True
                continue

            if tp_match:
                # This line is a # trivially_pass: annotation — do not emit as command
                pending_trivially_pass = tp_match.group(1).strip()
                pending_preflight_scope = None  # reset other annotation
                _last_was_annotation = True
                continue

            # Non-annotation comment line: invalidate pending annotations (AC6)
            if stripped.startswith('#') and not _annotation_re.match(stripped):
                pending_preflight_scope = None
                pending_trivially_pass = None
                _last_was_annotation = False
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
                _last_was_annotation = False
            else:
                # Non-command, non-annotation, non-blank line: invalidate annotations
                # (e.g. a comment like "# some other remark" — already handled above,
                # but also handles output lines etc.)
                pending_preflight_scope = None
                pending_trivially_pass = None
                _last_was_annotation = False

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
    schema: str = REVIEW_ISSUE_RESULT_SCHEMA
    schema_version: str = REVIEW_ISSUE_RESULT_SCHEMA_VERSION
    status: str = "ok"
    body_sha256: str = ""
    verdict: str = "approve"
    deterministic_checks: DeterministicChecks = field(default_factory=DeterministicChecks)
    blocking_issues: list[str] = field(default_factory=list)
    structured_blockers: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    # list of dict {code, severity, evidence, suggested_action}
    non_blocking_improvements: list = field(default_factory=list)
    diff_proposal: dict = field(default_factory=lambda: {"add": [], "remove": [], "rewrite": []})
    issue_kind: str = "implementation"
    parsed_vc_commands: list = field(default_factory=list)  # list[ParsedVcCommand] (serialized dicts)


PLACEHOLDER_VALUES = {"", "tbd", "todo", "none", "n/a", "na", "<tbd>", "<todo>"}
PLACEHOLDER_PATTERN = re.compile(r"^\s*<[^>]+>\s*$")  # <...> 形式
REQUIREMENT_ID_PATTERN = re.compile(r"^REQ-\d{3,}$")
SOURCE_TASK_ID_PATTERN = re.compile(r"^(T|TASK-)\d{3,}$")


def _add_warning(
    result: "CheckerResult",
    code: str,
    severity: str,
    evidence: list,
    suggested_action: str,
    *,
    emit_finding: bool = True,
) -> None:
    """Append a structured non_blocking_improvement entry."""
    result.non_blocking_improvements.append({
        "code": code,
        "severity": severity,
        "evidence": evidence,
        "suggested_action": suggested_action,
    })
    if emit_finding:
        result.findings.append(
            {
                "finding_kind": REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
                "deterministic_domain_key": code,
                "blocking": False,
                "checker_evidence": [],
                "message": suggested_action,
            }
        )


def extract_section(body: str, section_name: str) -> str:
    """Extract text under a ## section heading until the next ## heading.

    heading_policy (#654 B2): bilingual heading（例: ## 成果物 (Outcome)）も
    canonical_en（"Outcome"）として認識する。
    GFM ATX heading 仕様（0-3 spaces indent / 任意 closing #）に対応した
    parse_atx_heading_line() を使って各行を解析し、heading_policy の
    lookup_heading_policy() で canonical_en を照合する（SSOT 共用）。

    fence 対応 (#654 iter3): iter_markdown_blocks() で fence ブロックを識別し、
    code fence 内の行を見出し境界判定から除外する（SSOT: prose_boundary_policy）。
    境界は旧実装同様 level-2（##）見出しに限定する（level-1 等を境界扱いしない）。
    """
    lines = body.splitlines(keepends=True)
    n = len(lines)

    # fence 内行の行番号セットを構築（iter_markdown_blocks SSOT 利用）
    fence_line_indices: set[int] = set()
    current_line_idx = 0
    for block_text, block_kind in _iter_markdown_blocks(body):
        block_lines = block_text.splitlines(keepends=True)
        if block_kind == _BLOCK_KIND_CODE_FENCE:
            for k in range(len(block_lines)):
                fence_line_indices.add(current_line_idx + k)
        current_line_idx += len(block_lines)

    # 各行を走査して section_name に対応する level-2 見出しを探す
    for i, line in enumerate(lines):
        # fence 内行はスキップ（見出し境界判定から除外）
        if i in fence_line_indices:
            continue

        # GFM ATX heading として解析（B2: leading spaces / closing # 対応）
        parsed = _parse_atx_heading_line(line.rstrip('\n'))
        if parsed is None:
            continue

        # 境界は level-2（##）のみ（旧実装の ^## セマンティクスを維持）
        if parsed['level'] != 2:
            continue

        heading_text = parsed['text']

        # セクション名と照合: exact match (英語正規見出し) + heading_policy（bilingual）
        matched = False
        if heading_text == section_name:
            matched = True
        elif _HEADING_POLICY_AVAILABLE:
            policy = _lookup_heading_policy(heading_text)
            if policy and policy.get("canonical_en") == section_name:
                matched = True

        if not matched:
            continue

        # 見出しの次の行から次の level-2 heading（または EOF）までを収集
        # fence 内行も収集対象（セクション本文の一部）だが、境界判定には使わない
        start = i + 1
        end = n
        for j in range(start, n):
            # fence 内行は境界判定をスキップ
            if j in fence_line_indices:
                continue
            # 次の level-2 heading を GFM ATX parser で検出
            nxt = _parse_atx_heading_line(lines[j].rstrip('\n'))
            if nxt is not None and nxt['level'] == 2:
                end = j
                break

        section_body = ''.join(lines[start:end])
        return section_body.strip()

    return ""


def detect_issue_kind(body: str, labels: str = "", title: str = "") -> str:
    """Issue kind を検出する。Machine-Readable Contract を最優先で参照。

    SSOT: docs/dev/github-ops.md ## ISSUE_KIND_POLICY_V1

    - canonical_kinds（implementation / research / parent）はそのまま返す。
    - aliases（design → research, tracking → parent）は正規化して返す。
    - allowlist にも aliases にも存在しない未知の kind は UNKNOWN_ISSUE_KIND_SENTINEL を返す
      （silent "implementation" fallback は禁止）。
    - SSOT が読み込めない場合（IssueKindPolicyLoadError）は UNKNOWN_ISSUE_KIND_SENTINEL を返す
      （SSOT 不在を "implementation" に誤解させない）。
    """
    try:
        policy = _load_issue_kind_policy()
    except IssueKindPolicyLoadError:
        return UNKNOWN_ISSUE_KIND_SENTINEL

    canonical_kinds = policy["canonical_kinds"]
    aliases = policy["aliases"]

    def _normalize(kind: str) -> str:
        """Normalize kind: apply alias or return UNKNOWN_ISSUE_KIND_SENTINEL."""
        if kind in canonical_kinds:
            return kind
        if kind in aliases:
            return aliases[kind]
        return UNKNOWN_ISSUE_KIND_SENTINEL

    # 最優先: Machine-Readable Contract の issue_kind フィールド
    # ```yaml ... contract_schema_version ... issue_kind: <value> ... ``` を探す
    contract_match = re.search(
        r'```yaml\s*\n.*?contract_schema_version.*?\n.*?issue_kind:\s*(\S+)',
        body,
        re.DOTALL
    )
    if contract_match:
        kind = contract_match.group(1).strip().rstrip('"\'')
        return _normalize(kind)

    # fallback: labels
    if "tracking" in labels or "parent" in labels:
        return _normalize("tracking")
    if "phase/research" in labels or title.startswith("調査:"):
        return "research"
    if "phase/implementation" in labels or title.startswith("実装:"):
        return "implementation"

    # fallback: title prefix
    if title.startswith(("実装:", "implement:", "perf:", "fix:", "docs:")):
        return "implementation"

    # Unknown: do NOT silently return "implementation"
    return UNKNOWN_ISSUE_KIND_SENTINEL


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
    """C4: VC コマンド存在チェック（#993: shared parser ベースに統一）

    canonical format: ```bash fenced block 内の $ コマンド行のみを認識。
    inline backtick VC や list-style VC (- `cmd`) は pass にしない。
    """
    section = extract_section(body, "Verification Commands")
    if not section:
        return CheckResult.FAIL, ["## Verification Commands セクションが存在しないか空"]

    if _VC_SECTION_PARSER_AVAILABLE:
        parse_result = _parse_vc_section(section)
        if parse_result.commands:
            return CheckResult.PASS, []
        # No canonical commands — check if bash fence exists with non-$ content
        if not parse_result.has_bash_fence:
            return CheckResult.FAIL, ["VC に ```bash fenced block が見当たらない（canonical format が必要）"]
        return CheckResult.FAIL, ["VC に実行可能コマンドが見当たらない（$ <command> 形式が必要）"]

    # Fallback (shared parser unavailable): $ lines in bash fences only
    bash_blocks = re.findall(r'```bash[^\n]*\n(.*?)```', section, re.DOTALL)
    for block in bash_blocks:
        for line in block.splitlines():
            if line.strip().startswith('$'):
                return CheckResult.PASS, []
    return CheckResult.FAIL, ["VC に実行可能コマンドが見当たらない（$ で始まる行が必要）"]


_VC_AC_COMMENT_RE = re.compile(
    r"^\s*#\s*(AC\d+(?:\s*,\s*AC\d+)*)\s*$",
    re.MULTILINE,
)


def _extract_vc_ac_refs(vc_section: str) -> set[str]:
    """
    Extract all AC numbers referenced in VC comment lines.

    Only pure comment lines are recognised — a line whose content (after
    stripping leading whitespace) is exactly ``# AC<N>`` or a comma-separated
    list ``# AC<N>, AC<M>, ...``.  Lines that merely contain ``#AC`` as part
    of prose, a URL fragment, a filename, or a shell command are intentionally
    excluded.

    Supports two forms:
    - Single:  # AC1
    - Grouped: # AC2, AC3, AC4  (comma-separated list on a single comment line)

    Range notation (e.g. # AC2-AC4) is NOT a recognised form; the whole line
    fails to match and is treated as unrecognised (no refs extracted).

    Returns a set of digit strings (e.g. {"1", "2", "3", "4"}).
    """
    refs: set[str] = set()
    for m in _VC_AC_COMMENT_RE.finditer(vc_section):
        refs.update(re.findall(r"AC(\d+)", m.group(1)))
    return refs


def check_c5_ac_vc_alignment(body: str) -> tuple[str, list[str]]:
    """C5: AC と VC の番号一致チェック（#993: shared parser ベースに統一）

    VC 内の AC 参照として以下の形式を認識する:
    - Single:  # AC1
    - Grouped: # AC2, AC3, AC4  (カンマ区切りの grouped 表記 — #814)
    - Inline suffix: $ command  # AC1

    Range 表記 (# AC2-AC4) は本 Issue のスコープ外であり、サポートしない (AC3b)。
    """
    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")

    if not ac_section or not vc_section:
        return CheckResult.NA, []

    # AC 番号を収集
    ac_numbers = set(re.findall(r'AC(\d+)', ac_section))

    # VC 内の AC 参照を収集
    # #993: shared parser を優先使用（inline suffix + grouped 両対応）。
    # fallback として _extract_vc_ac_refs() を使用（comment-only 形式）。
    if _VC_SECTION_PARSER_AVAILABLE:
        parse_result = _parse_vc_section(vc_section)
        # VcParseResult.ac_refs uses "AC1" format; extract digit-only for comparison
        # with ac_numbers (which are digit-only from re.findall(r'AC(\d+)', ac_section)).
        vc_ac_refs = {re.sub(r"^AC", "", ref) for ref in parse_result.ac_refs}
    else:
        vc_ac_refs = _extract_vc_ac_refs(vc_section)

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
            return CheckResult.LEGACY_MISSING, [(
                "## Runtime Verification Applicability セクションがない（レガシー Issue）"
            )]

        # decision: フィールドの確認
        decision_match = re.search(r'decision:\s*(\S+)', section)
        if not decision_match:
            return CheckResult.LEGACY_MISSING, ["decision: フィールドがない（レガシー Issue）"]

        decision = decision_match.group(1).strip()
        valid_decisions = {"not_applicable", "deferred", "immediate"}
        if decision not in valid_decisions:
            return (
                CheckResult.FAIL,
                [f"decision: '{decision}' が不正（not_applicable / deferred / immediate のいずれかであること）"]
            )

        return CheckResult.PASS, []

    elif issue_kind in ("research", "tracking"):
        if not has_section:
            return (
                CheckResult.WARN,
                [
                    "research/tracking Issue に ## Runtime Verification Applicability"
                    " セクションが存在しない（warn、approve を妨げない）"
                ]
            )
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
    # #1135 P0: MRC は本文全体ではなく `## Machine-Readable Contract` セクションに
    # 束縛して strict に parse する（共通 parser）。decoy YAML / 重複 key / fence 複数 /
    # mapping でない root は fail-closed（mrc_result.ok == False）となり、docs 免除や
    # trace field 取得に利用されない。LP002 と同一 parser を共有する。
    mrc_result = parse_machine_readable_contract(body)
    mrc_data: dict = mrc_result.data if (mrc_result.ok and isinstance(mrc_result.data, dict)) else {}

    mrc_change_kind: Optional[str] = None
    if mrc_result.ok:
        raw_change_kind = mrc_data.get("change_kind")
        if raw_change_kind is not None:
            mrc_change_kind = str(raw_change_kind).strip().lower()

    psc_section = extract_section(body, "Product Spec Context")
    has_product_spec_context = bool(psc_section)

    _trace_keys = ("product_spec_id", "requirement_id", "source_task_id")
    mrc_has_trace = mrc_result.ok and any(k in mrc_data for k in _trace_keys)
    psc_has_trace = bool(re.search(
        r"\b(product_spec_id|requirement_id|source_task_id)\s*:",
        psc_section,
    ))
    has_trace_field_mention = mrc_has_trace or psc_has_trace

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

    # #1135 P1a: docs-only 免除は自己申告 change_kind: docs だけでは不十分。
    # Allowed Paths が全て documentation 分類のときに限り docs_only とする。
    allowed_paths = pc_extract_allowed_paths(body)
    docs_only = (mrc_change_kind == "docs") and is_docs_only_allowed_paths(allowed_paths)

    # #1135 P1a: change_kind: docs を自己申告しながら Allowed Paths に code/runtime
    # path を含む矛盾 issue は docs 免除を適用せず、product-spec signal があるとき
    # blocking fail にする（silent な n/a にしない）。
    if (
        mrc_change_kind == "docs"
        and has_code_or_runtime_scope(allowed_paths)
        and (has_product_spec_context or has_trace_field_mention or has_task_lineage_marker)
    ):
        return CheckResult.FAIL, [
            "change_kind: docs だが Allowed Paths に code/runtime path が含まれる"
            "（docs-only 免除は不適用）。product trace fields を宣言するか change_kind を見直す",
        ]

    psc_alone_applies = has_product_spec_context and not docs_only
    applicable = psc_alone_applies or has_trace_field_mention or has_task_lineage_marker
    if not applicable:
        return CheckResult.NA, []

    # Extract trace fields:
    # 1) MRC YAML が parse 成功した場合: mrc_parsed[field] を優先（inline comment 等を YAML semantics で除去）
    # 2) それ以外（PSC セクション / parse 失敗時）: regex fallback で structured_trace_text を走査
    # (PR #390 REQUEST_CHANGES blocker 1 + review-2 blocker 3 対応)
    def _extract_field(field_name: str) -> Optional[str]:
        if mrc_result.ok and field_name in mrc_data:
            v = mrc_data.get(field_name)
            if v is None:
                return None
            return str(v).strip()
        m = re.search(
            rf'^\s*-?\s*{re.escape(field_name)}\s*:\s*["\']?([^"\'\n#]*?)["\']?\s*(?:#.*)?$',
            psc_section,
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
            "suggested_action": (
                "Current Validated Scope と In Scope の対象ファイル/対象範囲を揃えるか、"
                "乖離理由を Background / Scope Delta に記載する"
            ),
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
            suggested_action=(
                "`grep -v \"^?\"` は untracked 行を除外するため、unstaged 変更を見逃す。検証対象を literal"
                " で列挙する形式に変更する"
            ),
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
    has_literal_inventory = bool(re.search(
        r"(removed_paths|削除対象|deleted_files|literal_list)\s*:",
        vc + "\n" + body,
    ))
    if not has_literal_inventory:
        _add_warning(
            result,
            code="vc_negative_grep_without_literal_inventory",
            severity="warning",
            evidence=[m.strip()[:120] for m in negation_patterns[:3]],
            suggested_action=(
                "削除確認 VC では削除対象 literal を明示的に列挙し、`test ! -f <path>` 形式または `removed_paths:`"
                " リストで個別確認する"
            ),
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


def _make_self_checker_evidence(
    result: "CheckerResult",
    *,
    rule_id: str,
    category: str,
) -> list[dict]:
    return [
        {
            "source_check": "check_issue_contract",
            "rule_id": rule_id,
            "category": category,
            "artifact_path": "check_issue_contract.py",
            "artifact_schema": "CHECK_ISSUE_CONTRACT_V1",
            "body_sha256": result.body_sha256,
            "iteration_id": "check_issue_contract_current",
            "line_start": None,
            "line_end": None,
        }
    ]


def _make_product_spec_checker_evidence(
    result: "CheckerResult",
    *,
    rule_id: str,
    body_sha256: str,
) -> list[dict]:
    return [
        {
            "source_check": "check_product_spec_contract",
            "rule_id": rule_id,
            "category": "product_spec_contract",
            "artifact_path": "check_product_spec_contract.py",
            "artifact_schema": "product_spec_check/v1",
            "body_sha256": body_sha256,
            "iteration_id": "check_product_spec_contract_current",
            "line_start": None,
            "line_end": None,
        }
    ]


def _run_product_spec_check(
    body: str,
    *,
    body_file_path: str | None,
) -> tuple[dict | None, int, str | None]:
    script_path = (
        Path(__file__).resolve().parent.parent.parent
        / "issue-contract-review"
        / "scripts"
        / "check_product_spec_contract.py"
    )

    temp_path: str | None = None
    if body_file_path is not None:
        try:
            existing_body = Path(body_file_path).read_text(encoding="utf-8")
        except OSError:
            existing_body = None
        if existing_body != body:
            body_file_path = None

    if body_file_path is None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".md") as tmp:
            tmp.write(body)
            temp_path = tmp.name
        body_file_path = temp_path

    cmd = [
        sys.executable,
        str(script_path),
        "--issue-number",
        "0",
        "--repo",
        "local/review-issue",
        "--body-file",
        body_file_path,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, -1, "timeout"
    finally:
        if temp_path:
            with contextlib.suppress(OSError):
                Path(temp_path).unlink()

    try:
        return json.loads(completed.stdout), completed.returncode, None
    except json.JSONDecodeError:
        return None, completed.returncode, "malformed_json"


def _apply_product_spec_check(
    result: "CheckerResult",
    *,
    body: str,
    body_file_path: str | None,
) -> None:
    if evaluate_product_spec_payload is None:
        issue = "product spec checker evaluator import failed"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    payload, rc, error = _run_product_spec_check(body, body_file_path=body_file_path)
    if error:
        issue = f"product spec checker failed closed: {error}"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    if rc not in (0, 1):
        issue = f"product spec checker failed closed: nonzero_exit:{rc}"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    if not isinstance(payload, dict):
        issue = "product spec checker failed closed: payload_not_object"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    payload_body_sha = payload.get("body_sha256")
    if payload_body_sha != result.body_sha256:
        issue = "product spec checker body_sha256 mismatch"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    gate = evaluate_product_spec_payload(
        payload,
        issue_url="https://github.com/local/review-issue/issues/0",
        body_sha256=result.body_sha256,
        exit_code=rc,
    )
    if gate.get("routing_action") == "refresh_contract_snapshot":
        issue = gate.get("reason", "product spec checker invariant violation")
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    if (
        gate.get("applicability") == "applicable"
        and gate.get("decision") in {"fail", "human_judgment"}
    ):
        blocked_reasons = payload.get("blocked_reasons", [])
        issues = [
            reason.get("excerpt", "product spec checker blocked")
            for reason in blocked_reasons
            if isinstance(reason, dict)
        ] or ["product spec checker blocked"]
        rule_id = (
            blocked_reasons[0].get("rule_id")
            if blocked_reasons and isinstance(blocked_reasons[0], dict)
            else "PS001"
        )
        _append_findings(
            result,
            issues,
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id=rule_id,
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.extend(issues)


def run_checks(
    body: str,
    labels: str = "",
    title: str = "",
    vc_preflight_json_path: Optional[str] = None,
    body_file_path: Optional[str] = None,
) -> CheckerResult:
    """Run all C1-C13 checks and return structured result."""
    issue_kind = detect_issue_kind(body, labels, title)
    result = CheckerResult(issue_kind=issue_kind)
    result.body_sha256 = _sha256_prefixed(body)

    checks = result.deterministic_checks

    # C1
    checks.C1_required_sections, issues = check_c1_required_sections(body, issue_kind)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="required_sections",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C1_required_sections in (CheckResult.FAIL, CheckResult.LEGACY_MISSING),
    )
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
    _append_findings(
        result,
        issues,
        deterministic_domain_key="stop_conditions",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C2_stop_conditions_6 == CheckResult.FAIL,
    )

    # C3
    checks.C3_ac_checkbox_format, issues = check_c3_ac_checkbox_format(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="ac_checkbox_format",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C3_ac_checkbox_format == CheckResult.FAIL,
    )

    # C4
    checks.C4_vc_commands_present, issues = check_c4_vc_commands_present(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="vc_command_format",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
        blocking=checks.C4_vc_commands_present == CheckResult.FAIL,
        checker_evidence=(
            _make_self_checker_evidence(
                result,
                rule_id="C4_vc_commands_present",
                category="vc_command_format",
            )
            if checks.C4_vc_commands_present == CheckResult.FAIL and issues
            else []
        ),
        reviewer_blocker_code="C4",
    )

    # annotation-aware VC parse (Issue #599)
    vc_section = extract_section(body, "Verification Commands")
    if vc_section:
        result.parsed_vc_commands = parse_vc_commands(vc_section)

    # C5
    checks.C5_ac_vc_number_alignment, issues = check_c5_ac_vc_alignment(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="vc_number_alignment",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
        blocking=checks.C5_ac_vc_number_alignment == CheckResult.FAIL,
        checker_evidence=(
            _make_self_checker_evidence(
                result,
                rule_id="C5_ac_vc_number_alignment",
                category="vc_number_alignment",
            )
            if checks.C5_ac_vc_number_alignment == CheckResult.FAIL and issues
            else []
        ),
        reviewer_blocker_code="C5",
    )

    # C6
    checks.C6_no_subjective_phrasing, issues = check_c6_no_subjective_phrasing(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="subjective_phrasing",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C6_no_subjective_phrasing == CheckResult.FAIL,
    )

    # C7
    checks.C7_required_skills_semantics, issues = check_c7_required_skills_semantics(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="required_skills_semantics",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C7_required_skills_semantics == CheckResult.FAIL,
    )

    # C8
    checks.C8_outcome_concreteness, issues = check_c8_outcome_concreteness(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="outcome_concreteness",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C8_outcome_concreteness == CheckResult.FAIL,
    )

    # C9
    checks.C9_runtime_applicability_present, issues = check_c9_runtime_applicability(body, issue_kind)
    # warn は blocking_issues に追加しない
    if checks.C9_runtime_applicability_present in (CheckResult.FAIL, CheckResult.LEGACY_MISSING):
        result.blocking_issues.extend(issues)
        _append_findings(
            result,
            issues,
            deterministic_domain_key="runtime_applicability",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=(
                _make_self_checker_evidence(
                    result,
                    rule_id="C9_runtime_applicability_present",
                    category="runtime_applicability",
                )
                if issues
                else []
            ),
            reviewer_blocker_code="C9",
        )
    elif checks.C9_runtime_applicability_present == CheckResult.WARN:
        for msg in issues:
            _add_warning(
                result,
                code="c9_runtime_applicability_missing",
                severity="warning",
                evidence=[msg],
                suggested_action=(
                    "implementation 以外の Issue でも `## Runtime Verification Applicability`"
                    " セクションの追加を推奨する"
                ),
                emit_finding=False,
            )
        _append_findings(
            result,
            issues,
            deterministic_domain_key="runtime_applicability",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
            blocking=False,
        )

    # C10
    checks.C10_deferred_destination_present, issues = check_c10_deferred_destination(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="deferred_destination",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C10_deferred_destination_present == CheckResult.FAIL,
    )

    # C11
    checks.C11_decision_tag_consistency, issues = check_c11_decision_tag_consistency(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="decision_tag_consistency",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C11_decision_tag_consistency == CheckResult.FAIL,
    )

    # C12: Product trace fields structure
    checks.C12_product_trace_fields_structure, issues = check_c12_product_trace_fields_structure(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="product_trace_fields_structure",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C12_product_trace_fields_structure != CheckResult.PASS,
    )

    # C13: VC preflight decision consistency (if JSON provided)
    (
        checks.C13_vc_preflight_decision_consistency,
        issues,
    ) = check_c13_vc_preflight_decision_consistency(vc_preflight_json_path)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="vc_preflight_decision_consistency",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C13_vc_preflight_decision_consistency != CheckResult.PASS,
    )

    # Non-blocking warnings（structured: code/severity/evidence/suggested_action）
    detect_warning_scope_cvs_in_scope_mismatch(body, result)
    detect_warning_vc_untracked_false_negative(body, result)
    detect_warning_vc_negative_grep_without_literal_inventory(body, result)
    _apply_product_spec_check(result, body=body, body_file_path=body_file_path)

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
    has_structured_blockers = bool(result.structured_blockers)
    result.status = _validate_status("failed" if has_fail or has_structured_blockers else "ok")
    result.verdict = "needs-fix" if has_fail or has_structured_blockers else "approve"

    return result


def fetch_issue_body(issue_number: int, repo: str) -> tuple[str, str, str]:
    """Fetch issue body, labels, and title from GitHub.

    Uses plain --json output (parsed as JSON) rather than --jq string
    concatenation: gh's --jq raw-string rendering appends its own trailing
    newline to the output, which previously landed inside the extracted body
    unless stripped -- and stripping silently diverged body_sha256 from the
    canonical raw-body hash used by contract_readiness_check.py /
    run_refinement_preflight.py, causing spurious body_sha_mismatch in
    reviewer_claim_replay.
    """
    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "title,body,labels",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: gh issue view failed: {result.stderr}", file=sys.stderr)
        sys.exit(2)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: gh issue view returned invalid JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    title = payload.get("title") or ""
    body = payload.get("body") or ""
    labels = ",".join(label.get("name", "") for label in payload.get("labels") or [])

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

    def _serialize_parsed_vc_cmd(cmd: ParsedVcCommand) -> dict:
        return {
            "command": cmd.command,
            "preflight_scope": cmd.preflight_scope.value if cmd.preflight_scope is not None else None,
            "trivially_pass_reason": cmd.trivially_pass_reason,
            "classification": cmd.classification,
            "skip_reason_type": cmd.skip_reason_type,
        }

    payload = {
        "schema": result.schema,
        "schema_version": result.schema_version,
        "verdict": result.verdict,
        "status": result.status,
        "body_sha256": result.body_sha256,
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
        "structured_blockers": result.structured_blockers,
        "non_blocking_improvements": result.non_blocking_improvements,
        "findings": result.findings,
        "diff_proposal": result.diff_proposal,
        "parsed_vc_commands": [_serialize_parsed_vc_cmd(c) for c in result.parsed_vc_commands],
    }
    _validate_review_issue_result_payload(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="C1〜C13 決定論的チェッカー — Issue 本文を機械的に検査する"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "-f", help="フィクスチャファイルパス（テスト用）")
    group.add_argument("--issue", "-i", type=int, help="GitHub Issue 番号")
    parser.add_argument("--repo", "-r", help="GitHub repo (owner/repo)。--issue と共に使用")
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "JSON 出力モード（stdout = JSON only / stderr = diagnostics）。"
            "caller は stdout のみを parse し stderr を stdout に merge しないこと。"
        ),
    )
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
            result = run_checks(
                body,
                labels,
                title,
                args.vc_preflight_json,
                body_file_path=args.file,
            )
            output = result_to_dict(result)
        # Emit JSON exclusively to stdout (after redirect is restored)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        if args.file:
            body, labels, title = load_fixture_file(args.file)
        else:
            body, labels, title = fetch_issue_body(args.issue, args.repo)
        result = run_checks(
            body,
            labels,
            title,
            args.vc_preflight_json,
            body_file_path=args.file,
        )
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
