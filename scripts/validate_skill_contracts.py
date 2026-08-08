"""Structural frontmatter validator for tracked .claude/skills/**/SKILL.md files.

Checks YAML frontmatter validity and required `name`/`description` fields
(structural_contract_coverage), and detects broken repo-relative path
references embedded in SKILL.md body text (backtick-quoted paths under
`.claude/`, `scripts/`, or `docs/`). Prose behavioral claims in the free-form
body are out of scope by design (see Issue #2017 Domain B findings).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
REFERENCE_RE = re.compile(
    r"`((?:\.claude|scripts|docs)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)`"
)


def list_tracked_skill_md(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", ".claude/skills/*/SKILL.md"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = [line for line in result.stdout.splitlines() if line.strip()]
    return sorted(paths)


def parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None, "no_frontmatter_block"
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, f"yaml_parse_error: {exc}"
    if not isinstance(data, dict):
        return None, "frontmatter_not_mapping"
    return data, None


def extract_references(text: str) -> list[str]:
    return sorted(set(REFERENCE_RE.findall(text)))


def audit(repo_root: Path) -> dict:
    tracked = list_tracked_skill_md(repo_root)
    files_report = []
    invalid_frontmatter_count = 0
    broken_reference_count = 0

    for rel_path in tracked:
        abs_path = repo_root / rel_path
        text = abs_path.read_text(encoding="utf-8")
        frontmatter, error = parse_frontmatter(text)

        missing_fields = []
        if frontmatter is not None:
            if not frontmatter.get("name"):
                missing_fields.append("name")
            if not frontmatter.get("description"):
                missing_fields.append("description")

        is_valid = frontmatter is not None and not missing_fields
        if not is_valid:
            invalid_frontmatter_count += 1

        references = extract_references(text)
        skill_dir = abs_path.parent
        broken_refs = [
            ref
            for ref in references
            if not (repo_root / ref).exists()
            and not (skill_dir / ref).exists()
        ]
        broken_reference_count += len(broken_refs)

        files_report.append(
            {
                "path": rel_path,
                "frontmatter_valid": is_valid,
                "frontmatter_error": error,
                "missing_fields": missing_fields,
                "reference_count": len(references),
                "broken_references": broken_refs,
            }
        )

    return {
        "schema": "skill_contract_audit/v1",
        "tracked_skill_md_count": len(tracked),
        "invalid_frontmatter_count": invalid_frontmatter_count,
        "broken_reference_count": broken_reference_count,
        "files": files_report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="repository root (default: inferred from script location)",
    )
    args = parser.parse_args()

    report = audit(Path(args.repo_root))

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            f"tracked_skill_md_count={report['tracked_skill_md_count']} "
            f"invalid_frontmatter_count={report['invalid_frontmatter_count']} "
            f"broken_reference_count={report['broken_reference_count']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
