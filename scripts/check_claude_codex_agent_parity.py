#!/usr/bin/env python3
"""Validate parity between repo-local Codex agent TOML and Claude subagent docs."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"


def load_expectations() -> dict:
    return json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))


def read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def extract_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    _, _, remainder = text.partition("---\n")
    frontmatter, _, _ = remainder.partition("\n---\n")
    return frontmatter


def main() -> int:
    expectations = load_expectations()
    failures: list[str] = []

    for agent_name, expected in expectations["required_agents"].items():
        codex_path = REPO_ROOT / expected["path"]
        claude_path = REPO_ROOT / expected["claude_agent_path"]

        if not codex_path.exists():
            failures.append(f"missing codex agent file: {expected['path']}")
            continue
        if not claude_path.exists():
            failures.append(f"missing claude agent file: {expected['claude_agent_path']}")
            continue

        codex_doc = read_toml(codex_path)
        claude_text = claude_path.read_text(encoding="utf-8")
        claude_frontmatter = extract_frontmatter(claude_text)

        if codex_doc.get("name") != agent_name:
            failures.append(f"{expected['path']}: name must be {agent_name}")
        if f"name: {agent_name}" not in claude_frontmatter:
            failures.append(f"{expected['claude_agent_path']}: frontmatter must declare name {agent_name}")

        role_token = agent_name.split("-")[0]
        if role_token not in claude_text:
            failures.append(
                f"{expected['claude_agent_path']}: expected role token '{role_token}' not found"
            )

    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1

    print("OK: Claude/Codex agent parity passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
