#!/usr/bin/env python3
"""
guard-issue-body.py

Issue 本文ファイルに対して以下のガードを適用し、YAML/JSON で結果を出力する。

Guards:
  1. Template Guard         — 必須セクションが存在するか
  2. Outcome Quality Guard  — Outcome が成果物形式・完了条件を含むか
  3. Diff Threshold         — 削減率が 50% 以下か（--orig-file 指定時）
  4. AC-VC Alignment        — AC 番号と VC の # AC<N> コメントの件数が一致するか

Usage:
    python3 guard-issue-body.py <body_file> [--orig-file <original_file>] [--format yaml|json]

Exit codes:
    0 — all guards pass
    2 — at least one guard failed
    1 — unexpected error
"""

import argparse
import json
import re
import sys
from pathlib import Path

# allowlist: ファイルパスは安全な文字のみ許可
_PATH_RE = re.compile(r'^[A-Za-z0-9._/\-]+$')

REQUIRED_SECTIONS = [
    "## Outcome",
    "## In Scope",
    "## Out of Scope",
    "## Acceptance Criteria",
    "## Verification Commands",
    "## Allowed Paths",
    "## Stop Conditions",
]

# Outcome 不適合パターン（動作状態のみ・成果物形式欠落）
_OUTCOME_NG_RE = re.compile(
    r'(決定される|整理される|完了する|検討する|改善する)\s*$',
    re.MULTILINE
)


def validate_path(value: str) -> Path:
    if not _PATH_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"Path must match ^[A-Za-z0-9._/-]+$, got: {value!r}"
        )
    p = Path(value)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"File not found: {value!r}")
    return p


def extract_outcome_block(text: str) -> str:
    lines = text.splitlines()
    in_block = False
    block_lines = []
    for line in lines:
        if line.strip() == "## Outcome":
            in_block = True
            continue
        if in_block:
            if line.startswith("## "):
                break
            block_lines.append(line)
    return "\n".join(block_lines)


def guard_template(body: str) -> dict:
    missing = [s for s in REQUIRED_SECTIONS if s not in body]
    return {
        "name": "template_guard",
        "passed": len(missing) == 0,
        "missing_sections": missing,
    }


def guard_outcome_quality(body: str) -> dict:
    outcome_block = extract_outcome_block(body)
    ng_match = _OUTCOME_NG_RE.search(outcome_block)
    return {
        "name": "outcome_quality_guard",
        "passed": ng_match is None,
        "detail": f"NG pattern found: {ng_match.group(0)!r}" if ng_match else None,
    }


def guard_diff_threshold(orig_text: str, new_text: str) -> dict:
    orig_lines = len(orig_text.splitlines())
    new_lines = len(new_text.splitlines())
    diff_lines = orig_lines - new_lines
    threshold = orig_lines // 2
    passed = diff_lines <= threshold
    return {
        "name": "diff_threshold",
        "passed": passed,
        "orig_lines": orig_lines,
        "new_lines": new_lines,
        "diff_lines": diff_lines,
        "threshold": threshold,
    }


def guard_ac_vc_alignment(body: str) -> dict:
    ac_count = len(re.findall(r'^- \[.\] AC\d+', body, re.MULTILINE))
    vc_ac_count = len(re.findall(r'# AC\d+', body))
    passed = (ac_count == 0) or (ac_count == vc_ac_count)
    return {
        "name": "ac_vc_alignment",
        "passed": passed,
        "ac_count": ac_count,
        "vc_ac_count": vc_ac_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Guard checks for GitHub Issue body files"
    )
    parser.add_argument(
        "body_file",
        type=validate_path,
        help="Path to new body file (safe chars only)"
    )
    parser.add_argument(
        "--orig-file",
        dest="orig_file",
        type=validate_path,
        default=None,
        help="Path to original body file for diff threshold check"
    )
    parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Output format (default: yaml)"
    )
    args = parser.parse_args()

    body = args.body_file.read_text(encoding="utf-8")

    results = []
    results.append(guard_template(body))
    results.append(guard_outcome_quality(body))

    if args.orig_file is not None:
        orig_body = args.orig_file.read_text(encoding="utf-8")
        results.append(guard_diff_threshold(orig_body, body))

    results.append(guard_ac_vc_alignment(body))

    all_passed = all(r["passed"] for r in results)
    output = {
        "all_passed": all_passed,
        "guards": results,
    }

    if args.format == "json":
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        # Simple YAML-like output (no external dependency)
        print(f"all_passed: {str(all_passed).lower()}")
        print("guards:")
        for r in results:
            print(f"  - name: {r['name']}")
            print(f"    passed: {str(r['passed']).lower()}")
            for k, v in r.items():
                if k in ("name", "passed"):
                    continue
                if v is None:
                    print(f"    {k}: null")
                elif isinstance(v, list):
                    if v:
                        print(f"    {k}:")
                        for item in v:
                            print(f"      - {item!r}")
                    else:
                        print(f"    {k}: []")
                else:
                    print(f"    {k}: {v}")

    sys.exit(0 if all_passed else 2)


if __name__ == "__main__":
    main()
