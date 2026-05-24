#!/usr/bin/env python3
"""
Product Spec Contract Checker

Issue contract を product spec 観点から検証する。以下の 6 ルールで判定:

  PS001: docs/product/** 更新時に spec delta Issue のリンクがあるか
  PS002: tasks.md が direct implementation source になっていないか
  PS003: .specify/ が canonical source になっていないか
  PS004: product spec 更新に diff_rationale / evidence があるか
  PS005: generated task が requirement_id / source_task_id を保持しているか
  PS006: generated task dependency が materialize されているか

出力: JSON with product_spec_check schema
"""

import argparse
import json
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple


def run_gh_api(issue_number: int, repo: str) -> Optional[Dict[str, Any]]:
    """GitHub API から Issue 情報を取得"""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "title,body,labels",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def extract_allowed_paths(issue_body: str) -> List[str]:
    """## Allowed Paths セクションから path を抽出"""
    paths = []
    match = re.search(
        r"^##\s+Allowed Paths\s*$(.+?)(?=^##|\Z)", issue_body, re.MULTILINE | re.DOTALL
    )
    if match:
        section = match.group(1)
        # - path または * path の形式で抽出
        for line in section.split("\n"):
            line = line.strip()
            if line.startswith("- ") or line.startswith("* "):
                paths.append(line[2:].strip())
    return paths


def extract_product_spec_context(issue_body: str) -> Optional[Dict[str, Any]]:
    """## Product Spec Context セクションから struct を抽出"""
    match = re.search(
        r"^##\s+Product Spec Context\s*$(.+?)(?=^##|\Z)",
        issue_body,
        re.MULTILINE | re.DOTALL,
    )
    if match:
        section = match.group(1)
        context = {}
        for line in section.split("\n"):
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                key, val = line.split(":", 1)
                context[key.strip()] = val.strip()
        return context if context else None
    return None


def extract_depends_on(issue_body: str) -> List[int]:
    """line-anchored `Depends on #N` を抽出（制御セクション）"""
    depends_on = []
    match = re.search(
        r"^##\s+Depends On\s*$(.+?)(?=^##|\Z)", issue_body, re.MULTILINE | re.DOTALL
    )
    if match:
        section = match.group(1)
        for line in section.split("\n"):
            line = line.strip()
            # ^- Depends on #<number> または ^Depends on #<number>
            m = re.match(r"^-?\s*Depends on #(\d+)", line)
            if m:
                depends_on.append(int(m.group(1)))
    return depends_on


def check_trigger_conditions(
    issue_body: str, allowed_paths: List[str]
) -> Dict[str, bool]:
    """trigger conditions を判定"""
    triggers = {
        "docs_product_allowed_paths": any("docs/product/" in p for p in allowed_paths),
        "tasks_md_mentioned": bool(
            re.search(r"\btasks\.md\b|tasks\.md", issue_body, re.IGNORECASE)
        ),
        "specify_artifact_mentioned": bool(
            re.search(r"\.specify|specify/", issue_body, re.IGNORECASE)
        ),
        "generated_task_mentioned": bool(
            re.search(
                r"generated[_-]?task|task_materialization|from[_-]?tasks\.md|generated from",
                issue_body,
                re.IGNORECASE,
            )
        ),
        "product_spec_context_present": extract_product_spec_context(issue_body)
        is not None,
    }
    return triggers


def check_ps001(
    issue_body: str, allowed_paths: List[str], triggers: Dict[str, bool]
) -> Tuple[str, List[Dict[str, str]]]:
    """
    PS001: docs/product/** 更新が spec delta Issue のリンクを含んでいるか
    - applicability: only if docs_product_allowed_paths is true
    """
    if not triggers["docs_product_allowed_paths"]:
        return "pass", []  # n/a: applicability check does not apply

    # Issue 自体が spec/update Issue かどうかを判定
    # Machine-Readable Contract に change_kind: product-spec-delta 等があるか
    if re.search(
        r"change_kind:\s*(spec-?delta|product-?spec|product[_-]?spec[_-]?delta|product[_-]?spec[_-]?update|spec[_-]?update)",
        issue_body,
        re.IGNORECASE,
    ):
        return "pass", [
            {
                "rule_id": "PS001",
                "source": "issue_body",
                "excerpt": "Issue 自体が product spec delta Issue として明示（change_kind）",
            }
        ]

    # code change も含むなら spec delta Issue のリンクが必須
    if not re.search(r"allowed_paths.*\.md\b", issue_body, re.MULTILINE | re.DOTALL):
        # docs-only の場合
        if re.search(r"issue_kind:\s*spec|issue_kind:\s*documentation", issue_body):
            return "pass", [
                {
                    "rule_id": "PS001",
                    "source": "issue_body",
                    "excerpt": "Issue 自体が docs-only spec issue として明示（issue_kind）",
                }
            ]

    # Product Spec Context がある場合、それを証跡として使える
    context = extract_product_spec_context(issue_body)
    if context:
        return "pass", [
            {
                "rule_id": "PS001",
                "source": "issue_body",
                "excerpt": "Product Spec Context セクション存在",
            }
        ]

    # spec delta Issue 参照を探す（`Relates to #N` / `Spec:` など）
    if re.search(r"(spec|spec-?delta|related|relates to).*#\d+", issue_body, re.IGNORECASE):
        return "pass", [
            {
                "rule_id": "PS001",
                "source": "issue_body",
                "excerpt": "spec delta Issue の参照有り（Spec / Relates to）",
            }
        ]

    return "fail", [
        {
            "rule_id": "PS001",
            "source": "allowed_paths",
            "excerpt": "docs/product/** 更新ありかつ spec delta Issue リンク / Product Spec Context 欠落",
        }
    ]


def check_ps002(issue_body: str, triggers: Dict[str, bool]) -> Tuple[str, List[Dict[str, str]]]:
    """
    PS002: tasks.md が direct implementation source になっていないか
    - Pass: staging artifact として言及
    - Fail: direct source / tracking SSOT として言及
    """
    if not triggers["tasks_md_mentioned"]:
        return "pass", []  # n/a: applicability check does not apply

    # allow patterns first — higher priority
    # These patterns relate to tasks.md as staging/artifact/convert/materialize
    allow = [
        r"tasks\.md.*staging",
        r"staging.*tasks\.md",
        r"tasks\.md.*artifact",
        r"artifact.*tasks\.md",
        r"convert.*tasks\.md.*issue|materialize.*tasks\.md",
    ]

    if any(re.search(p, issue_body, re.IGNORECASE) for p in allow):
        return "pass", [
            {
                "rule_id": "PS002",
                "source": "issue_body",
                "excerpt": "tasks.md を staging artifact として参照",
            }
        ]

    # prohibit patterns — checked after allow to prevent false positive
    prohibit = [
        r"implement\s+.*?tasks\.md(?!.*(?:from|materialize|staging|artifact))|tasks\.md\s+.*?implement",
        r"direct.*tasks\.md|tasks\.md.*direct",
        r"source.*tasks\.md(?!_task_id)|tasks\.md.*source\b(?!_task_id)",
        r"tracking.*tasks\.md|tasks\.md.*tracking",
        r"proceed.*tasks\.md|tasks\.md.*proceed",
    ]

    for pattern in prohibit:
        if re.search(pattern, issue_body, re.IGNORECASE | re.DOTALL):
            return "fail", [
                {
                    "rule_id": "PS002",
                    "source": "issue_body",
                    "excerpt": re.search(pattern, issue_body, re.IGNORECASE | re.DOTALL).group(0),
                }
            ]

    # neutral mention — pass but with note
    return "pass", [
        {
            "rule_id": "PS002",
            "source": "issue_body",
            "excerpt": "tasks.md への言及あるも、staging artifact / direct implementation source の区別がない",
        }
    ]


def check_ps003(issue_body: str, triggers: Dict[str, bool]) -> Tuple[str, List[Dict[str, str]]]:
    """
    PS003: .specify/ が canonical source になっていないか
    - Pass: derived workbench として言及、または docs/SSOT が優先と明記
    - Fail: canonical source / SSOT override として言及
    """
    if not triggers["specify_artifact_mentioned"]:
        return "pass", []  # n/a: applicability check does not apply

    # prohibit patterns (check FIRST, before allow)
    # Specifically match .specify as THE canonical/SSOT (not derived from docs)
    prohibit = [
        r"\.specify.*?\bcanonical\b(?!.*not)(?!.*derived)",
        r"\.specify.*?\bssot\b(?!.*docs)(?!.*wins)",
        r"\.specify.*?(?:override.*?docs|takes priority.*docs|comes.*?before.*docs)",
    ]

    for pattern in prohibit:
        if re.search(pattern, issue_body, re.IGNORECASE | re.DOTALL):
            return "fail", [
                {
                    "rule_id": "PS003",
                    "source": "issue_body",
                    "excerpt": re.search(pattern, issue_body, re.IGNORECASE | re.DOTALL).group(0)[:100],
                }
            ]

    # allow patterns (check AFTER prohibit)
    allow = [
        r"\.specify.*workbench|workbench.*\.specify",
        r"\.specify.*derived|derived.*\.specify",
        r"\.specify.*artifact",
        r"docs.*wins|docs.*priority|docs.*ssot.*wins|docs.*SSOT",
        r"\.specify.*not canonical",
    ]

    if any(re.search(p, issue_body, re.IGNORECASE) for p in allow):
        return "pass", [
            {
                "rule_id": "PS003",
                "source": "issue_body",
                "excerpt": ".specify/ を derived workbench として明記",
            }
        ]

    # neutral mention
    return "pass", [
        {
            "rule_id": "PS003",
            "source": "issue_body",
            "excerpt": ".specify/ への言及あるも、canonical / derived の区別がない",
        }
    ]


def check_ps004(
    issue_body: str, allowed_paths: List[str], triggers: Dict[str, bool]
) -> Tuple[str, List[Dict[str, str]]]:
    """
    PS004: product spec 更新に diff_rationale / changed_requirement_id / affected_sections があるか
    - Applicability: only if docs_product_allowed_paths AND not pure spec issue
    """
    if not triggers["docs_product_allowed_paths"]:
        return "pass", []  # n/a: applicability check does not apply

    # If Issue 自体が spec delta/update Issue, then evidence is required
    if re.search(
        r"change_kind:\s*(spec-?delta|product-?spec|product[_-]?spec[_-]?delta|product[_-]?spec[_-]?update|spec[_-]?update)",
        issue_body,
        re.IGNORECASE,
    ):
        # Spec delta issues must have evidence in Machine-Readable Contract or Product Spec Context
        contract_match = re.search(
            r"^##\s+Machine-Readable Contract\s*$(.+?)(?=^##|\Z)",
            issue_body,
            re.MULTILINE | re.DOTALL,
        )

        context_match = re.search(
            r"^##\s+Product Spec Context\s*$(.+?)(?=^##|\Z)",
            issue_body,
            re.MULTILINE | re.DOTALL,
        )

        evidence_patterns = [
            r"diff[_-]?rationale\s*:",
            r"changed[_-]?requirement\s*:",
            r"affected[_-]?sections\s*:",
        ]

        found = []
        search_text = ""
        if contract_match:
            search_text += contract_match.group(1)
        if context_match:
            search_text += context_match.group(1)

        for pattern in evidence_patterns:
            if re.search(pattern, search_text, re.IGNORECASE):
                found.append(pattern)

        if found:
            return "pass", [
                {
                    "rule_id": "PS004",
                    "source": "issue_body",
                    "excerpt": f"spec delta issue with rationale: {found[0]}",
                }
            ]

        # Spec delta issues without explicit evidence: fail
        return "fail", [
            {
                "rule_id": "PS004",
                "source": "issue_body",
                "excerpt": "spec delta issue without diff_rationale / changed_requirement_id / affected_sections",
            }
        ]

    # Non-spec issues with docs/product updates need diff evidence
    contract_match = re.search(
        r"^##\s+Machine-Readable Contract\s*$(.+?)(?=^##|\Z)",
        issue_body,
        re.MULTILINE | re.DOTALL,
    )

    context_match = re.search(
        r"^##\s+Product Spec Context\s*$(.+?)(?=^##|\Z)",
        issue_body,
        re.MULTILINE | re.DOTALL,
    )

    evidence_patterns = [
        r"diff[_-]?rationale\s*:",
        r"changed[_-]?requirement\s*:",
        r"affected[_-]?sections\s*:",
        r"change[_-]?summary\s*:",
    ]

    found = []
    search_text = ""
    if contract_match:
        search_text += contract_match.group(1)
    if context_match:
        search_text += context_match.group(1)

    for pattern in evidence_patterns:
        if re.search(pattern, search_text, re.IGNORECASE):
            found.append(pattern)

    if found:
        return "pass", [
            {
                "rule_id": "PS004",
                "source": "issue_body",
                "excerpt": f"evidence found: {found[0]}",
            }
        ]

    # context check — Product Spec Context の存在が evidence になる
    context = extract_product_spec_context(issue_body)
    if context:
        return "pass", [
            {
                "rule_id": "PS004",
                "source": "issue_body",
                "excerpt": "Product Spec Context セクション（implicit evidence）",
            }
        ]

    return "fail", [
        {
            "rule_id": "PS004",
            "source": "issue_body",
            "excerpt": "docs/product/** 更新あり、diff_rationale / changed_requirement_id / affected_sections 欠落",
        }
    ]


def check_ps005(issue_body: str, triggers: Dict[str, bool]) -> Tuple[str, List[Dict[str, str]]]:
    """
    PS005: generated task が requirement_id / source_task_id を保持しているか
    - Applicability: only if generated_task_mentioned
    """
    if not triggers["generated_task_mentioned"]:
        return "pass", []  # n/a: applicability check does not apply

    # context check (most reliable source)
    context = extract_product_spec_context(issue_body)

    has_requirement_id = False
    has_source_task_id = False

    # Check in Machine-Readable Contract section
    contract_match = re.search(
        r"^##\s+Machine-Readable Contract\s*$(.+?)(?=^##|\Z)",
        issue_body,
        re.MULTILINE | re.DOTALL,
    )
    if contract_match:
        contract_section = contract_match.group(1)
        has_requirement_id = bool(re.search(r"requirement[_-]?id:", contract_section, re.IGNORECASE))
        has_source_task_id = bool(re.search(r"source[_-]?task[_-]?id:", contract_section, re.IGNORECASE))

    # Fallback to context
    if context:
        has_requirement_id = has_requirement_id or ("requirement_id" in context)
        has_source_task_id = has_source_task_id or ("source_task_id" in context)

    if has_requirement_id and has_source_task_id:
        return "pass", [
            {
                "rule_id": "PS005",
                "source": "issue_body",
                "excerpt": "requirement_id と source_task_id 両方存在",
            }
        ]

    missing = []
    if not has_requirement_id:
        missing.append("requirement_id")
    if not has_source_task_id:
        missing.append("source_task_id")

    return "fail", [
        {
            "rule_id": "PS005",
            "source": "issue_body",
            "excerpt": f"generated task tracing: {', '.join(missing)} 欠落",
        }
    ]


def check_ps006(issue_body: str, triggers: Dict[str, bool]) -> Tuple[str, List[Dict[str, str]]]:
    """
    PS006: generated task dependency が materialize されているか
    - Applicability: only if generated_task_mentioned AND mentions dependencies
    - Line-anchored Depends on #N を使用（GitHub native dependency 対応は follow-up）
    """
    if not triggers["generated_task_mentioned"]:
        return "pass", []  # n/a: applicability check does not apply

    # Check if dependencies are mentioned
    has_dependency_mention = bool(
        re.search(
            r"depend|depend on|prerequisite|requires|prior.*task|blocked.*by|blocks",
            issue_body,
            re.IGNORECASE,
        )
    )

    if not has_dependency_mention:
        return "pass", []  # n/a: applicability check does not apply

    # Check for line-anchored Depends on #N
    depends_on = extract_depends_on(issue_body)
    has_depends_on = len(depends_on) > 0

    if has_depends_on:
        return "pass", [
            {
                "rule_id": "PS006",
                "source": "dependencies",
                "excerpt": "Depends on #N 宣言あり（line-anchored）",
            }
        ]

    # If generated task mentions dependencies but none materialized, flag as human_judgment
    return "human_judgment", [
        {
            "rule_id": "PS006",
            "source": "issue_body",
            "excerpt": "generated task が dependencies を言及するもそれが materialize されていない（確認必要）",
        }
    ]


def main():
    parser = argparse.ArgumentParser(description="Product Spec Contract Checker")
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--repo", required=True, default="squne121/loop-protocol")
    parser.add_argument("--body-file", type=str, help="Read issue body from file (for testing)")
    args = parser.parse_args()

    # Issue 情報を取得
    if args.body_file:
        # Test mode: read from file
        with open(args.body_file, "r", encoding="utf-8") as f:
            issue_body = f.read()
    else:
        issue_data = run_gh_api(args.issue_number, args.repo)
        if not issue_data:
            print(
                json.dumps(
                    {
                        "applicability": "not_applicable",
                        "decision": "human_judgment",
                        "error": "Failed to fetch issue",
                    }
                )
            )
            sys.exit(1)
        issue_body = issue_data.get("body", "")

    allowed_paths = extract_allowed_paths(issue_body)

    # trigger conditions を判定
    triggers = check_trigger_conditions(issue_body, allowed_paths)

    # applicability を決定
    applicability = "applicable" if any(triggers.values()) else "not_applicable"

    # 各ルールを実行
    checks = {
        "docs_product_requires_spec_evidence": check_ps001(
            issue_body, allowed_paths, triggers
        ),
        "tasks_md_not_direct_source": check_ps002(issue_body, triggers),
        "specify_not_canonical": check_ps003(issue_body, triggers),
        "diff_first_rationale_present": check_ps004(issue_body, allowed_paths, triggers),
        "generated_task_trace_present": check_ps005(issue_body, triggers),
        "task_dependencies_materialized": check_ps006(issue_body, triggers),
    }

    # 結果を集約
    conditions = {}
    all_blocked_reasons = []

    for check_name, (status, evidence) in checks.items():
        conditions[check_name] = {"status": status, "evidence": evidence}
        if status in ("fail", "human_judgment"):
            all_blocked_reasons.extend(evidence)

    # decision を決定
    if any(s == "fail" for s, _ in checks.values()):
        decision = "fail"
    elif any(s == "human_judgment" for s, _ in checks.values()):
        decision = "human_judgment"
    else:
        decision = "pass"

    result = {
        "applicability": applicability,
        "decision": decision,
        "triggers": triggers,
        "conditions": conditions,
        "blocked_reasons": all_blocked_reasons,
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if decision in ("pass",) else 1)


if __name__ == "__main__":
    main()
