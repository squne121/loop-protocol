#!/usr/bin/env python3
"""Validate Codex-specific impl-review-loop dispatch constraints."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".codex/config.toml"

SPAWN_NOTE_EXPECTATIONS = {
    ".claude/skills/impl-review-loop/steps/preparation.md": "scope-rollup-runner",
    ".claude/skills/impl-review-loop/steps/step-1-implementation.md": "implementation-worker",
    ".claude/skills/impl-review-loop/steps/step-2-verification.md": "test-runner",
    ".claude/skills/impl-review-loop/steps/step-4-pr-review.md": "pr-reviewer",
    ".claude/skills/post-merge-cleanup/SKILL.md": "post-merge-cleanup-worker",
}


def read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def assert_max_depth(failures: list[str]) -> None:
    config = read_toml(CONFIG_PATH)
    if config.get("agents", {}).get("max_depth") != 1:
        failures.append(".codex/config.toml: [agents].max_depth must be 1")


def assert_no_project_profile_routing(failures: list[str]) -> None:
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    if re.search(r"^\s*profile\s*=", raw, re.MULTILINE):
        failures.append(".codex/config.toml: project-local profile= must not be used for routing")
    if re.search(r"^\s*\[profiles(?:\.|\])", raw, re.MULTILINE):
        failures.append(".codex/config.toml: project-local [profiles] blocks must not be used for routing")
    required_phrase = "Project-local config is not evidence of profile routing"
    if required_phrase not in raw:
        failures.append(f".codex/config.toml: missing phrase '{required_phrase}'")


def assert_explicit_spawn_notes(failures: list[str]) -> None:
    for relative_path, agent_name in SPAWN_NOTE_EXPECTATIONS.items():
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        expected_phrase = (
            f"Codex CLI: spawn the custom agent named {agent_name} for this step; "
            "the root thread must not"
        )
        if expected_phrase not in text:
            failures.append(f"{relative_path}: missing explicit spawn note for {agent_name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assert-max-depth", action="store_true")
    parser.add_argument("--assert-no-project-profile-routing", action="store_true")
    parser.add_argument("--assert-explicit-spawn-notes", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not any(vars(args).values()):
        parser.error("specify at least one assertion flag")

    failures: list[str] = []
    if args.assert_max_depth:
        assert_max_depth(failures)
    if args.assert_no_project_profile_routing:
        assert_no_project_profile_routing(failures)
    if args.assert_explicit_spawn_notes:
        assert_explicit_spawn_notes(failures)

    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1

    print("OK: Codex dispatch contract validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
