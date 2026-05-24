from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "SKILL.md"
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

EXPECTED_DESCRIPTION = (
    "implementation child issue を **実装→検証→PR レビュー** の 3 ステップループで自律完了させる"
    "オーケストレーター。 Issue 番号を受け取り、pr-reviewer の LOOP_VERDICT が APPROVE に"
    "なるまで反復する。 `/impl-review-loop <N>` または「Issue ◯◯ をループで実装して」の"
    "トリガーで使う。 着手前に `docs/dev/workflow.md` の「Issue contract を作業計画の正本"
    "として扱う条件」と `issue-contract-review` の `status: go` を確認する。"
)


def _frontmatter(text: str) -> str:
    parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
    assert len(parts) >= 3, "frontmatter が見つからない"
    return parts[1]


def test_impl_review_loop_frontmatter_description_is_stable():
    data = yaml.safe_load(_frontmatter(SKILL_MD.read_text(encoding="utf-8")))

    desc = data["description"]
    assert isinstance(desc, str)
    assert desc == EXPECTED_DESCRIPTION
    assert "\n" not in desc
    assert desc.startswith("implementation child issue を")
    assert "Issue 番号を受け取り" in desc
    assert "issue-contract-review" in desc
    assert "`status: go`" in desc


def test_all_skill_frontmatters_are_yaml_parseable():
    bad: list[tuple[str, str]] = []

    for skill in SKILLS_DIR.glob("*/SKILL.md"):
        try:
            yaml.safe_load(_frontmatter(skill.read_text(encoding="utf-8")))
        except Exception as exc:  # pragma: no cover - failure path only
            bad.append((str(skill.relative_to(REPO_ROOT)), repr(exc)))

    assert not bad, bad
