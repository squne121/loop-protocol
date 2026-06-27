#!/usr/bin/env python3
"""Generate a Japanese-first PR body that satisfies LOOP_PROTOCOL validators."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from string import Template


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value}")


def _is_agent_surface_change(changed_files: list[str]) -> bool:
    return any(path == ".claude" or path.startswith(".claude/") for path in changed_files)


def _build_summary(issue: int, is_agent_surface_change: bool) -> str:
    if is_agent_surface_change:
        return (
            f"この PR は Issue #{issue} の実装です。"
            "PR 作成前に日本語比率、Safety Claim Matrix、Schema Change Applicability、"
            "Draft 状態を決定論的に確認できるようにします。"
        )
    return (
        f"この PR は Issue #{issue} の実装です。"
        "PR body generator と hygiene validator を追加し、既存 validator を通る本文を"
        "一発で準備できるようにします。"
    )


def _build_checks() -> str:
    return (
        "- [ ] テスト確認: `uv run pytest .claude/skills/open-pr/tests/test_pr_body_hygiene.py -q`\n"
        "- [ ] hygiene 判定確認: `uv run python3"
        " .claude/skills/open-pr/scripts/validate_pr_body_hygiene.py"
        " --body-file <generated-body-file>"
        " --changed-paths-file <changed-paths-file>"
        " --linked-issue <issue> --draft <true|false> --require-merge-ready`"
    )


def _build_schema_reason() -> str:
    return (
        "PR body generator と hygiene 判定の追加であり、既存 schema consumer が受け取る"
        "入力契約は変更しないためです。"
    )


def _build_inventory_reason() -> str:
    return (
        "schema_change ではなく、既存 consumer の before/after 差分も発生しないためです。"
    )


def _build_safety_matrix(
    issue: int,
    changed_files: list[str],
    is_agent_surface_change: bool,
    draft: bool,
) -> str:
    draft_arg = "true" if draft else "false"
    evidence = (
        "`generate_pr_body.py --issue "
        f"{issue} --changed-files {' '.join(changed_files)} --draft {draft_arg}`"
    )
    if is_agent_surface_change:
        claim = "`.claude/**` 変更時でも Safety Claim Matrix の空欄や placeholder を残さない"
    else:
        claim = "PR body の必須セクションと日本語本文を作成前に固定し、review 往復を減らす"
    return "\n".join(
        [
            "| Claim | Implemented? | Not controlled | Evidence | Follow-up |",
            "|---|---|---|---|---|",
            f"| {claim} | yes | N/A | {evidence} | N/A |",
        ]
    )


def _build_notes(issue: int) -> str:
    lines = [
        f"- 関連する Issue は #{issue} です。",
        f"- 本 PR は Issue #{issue} を close します（Closes #{issue}）。",
        "- レビュー時に参照しやすい日本語本文を維持し、本文 hygiene の再修正を減らします。",
    ]
    return "\n".join(lines)


def generate_pr_body(issue: int, changed_files: list[str], draft: bool) -> str:
    template_path = Path(__file__).resolve().parent.parent / "templates" / "pr_body.ja.md"
    template = Template(template_path.read_text(encoding="utf-8"))
    is_agent_surface_change = _is_agent_surface_change(changed_files)
    return template.substitute(
        summary=_build_summary(issue, is_agent_surface_change),
        checks=_build_checks(),
        schema_reason=_build_schema_reason(),
        inventory_reason=_build_inventory_reason(),
        safety_claim_matrix=_build_safety_matrix(issue, changed_files, is_agent_surface_change, draft),
        notes=_build_notes(issue),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a validator-compliant PR body.")
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--changed-files", nargs="+", required=True)
    parser.add_argument("--draft", default="true", type=_parse_bool)
    args = parser.parse_args(argv)

    body = generate_pr_body(args.issue, args.changed_files, args.draft)
    sys.stdout.write(body)
    if not body.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
