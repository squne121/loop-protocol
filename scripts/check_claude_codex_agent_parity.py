#!/usr/bin/env python3
"""Validate machine-readable parity between Codex agent TOML and Claude agent docs."""

from __future__ import annotations

import json
import re
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


def extract_frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---\n"):
        return {}
    _, _, remainder = text.partition("---\n")
    frontmatter, _, _ = remainder.partition("\n---\n")
    result: dict[str, object] = {}
    current_key: str | None = None
    for raw_line in frontmatter.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("  - ") and current_key:
            result.setdefault(current_key, [])
            cast_list = result[current_key]
            if isinstance(cast_list, list):
                cast_list.append(raw_line[4:].strip())
            continue
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        current_key = key.strip()
        parsed = value.strip()
        if not parsed:
            result[current_key] = []
        else:
            result[current_key] = parsed
    return result


def extract_runtime_field(instructions: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}:\s*([a-zA-Z0-9._|-]+)", instructions)
    return match.group(1) if match else None


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
        codex_instructions = str(codex_doc.get("developer_instructions", ""))

        if codex_doc.get("name") != agent_name:
            failures.append(f"{expected['path']}: name must be {agent_name}")
        if claude_frontmatter.get("name") != agent_name:
            failures.append(f"{expected['claude_agent_path']}: frontmatter name must be {agent_name}")
        if claude_frontmatter.get("model") != expected["claude_model"]:
            failures.append(
                f"{expected['claude_agent_path']}: model expected {expected['claude_model']!r} got {claude_frontmatter.get('model')!r}"
            )
        if claude_frontmatter.get("permissionMode") != expected["claude_permission_mode"]:
            failures.append(
                f"{expected['claude_agent_path']}: permissionMode expected {expected['claude_permission_mode']!r} got {claude_frontmatter.get('permissionMode')!r}"
            )

        tools = claude_frontmatter.get("tools", [])
        if not isinstance(tools, list) or not tools:
            failures.append(f"{expected['claude_agent_path']}: tools frontmatter list is required")

        runtime_status = extract_runtime_field(codex_instructions, "runtime_dependency_status")
        runtime_route = extract_runtime_field(codex_instructions, "runtime_followup_route")
        if runtime_status != expected["runtime_dependency_status"]:
            failures.append(
                f"{expected['path']}: runtime_dependency_status expected {expected['runtime_dependency_status']!r} got {runtime_status!r}"
            )
        if runtime_route != expected["runtime_followup_route"]:
            failures.append(
                f"{expected['path']}: runtime_followup_route expected {expected['runtime_followup_route']!r} got {runtime_route!r}"
            )

        if expected["runtime_followup_route"] != "none" and expected["runtime_followup_route"].split("|")[0] not in claude_text:
            failures.append(
                f"{expected['claude_agent_path']}: expected route token {expected['runtime_followup_route']!r} not found"
            )

        permission_expected = "acceptEdits" if expected["default_permissions"] == "loop-protocol-rtk" else "dontAsk"
        if agent_name == "post-merge-cleanup-worker":
            permission_expected = "default"
        if claude_frontmatter.get("permissionMode") != permission_expected:
            failures.append(
                f"{expected['claude_agent_path']}: permissionMode must match Codex permission profile {expected['default_permissions']}"
            )

    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1

    print("OK: Claude/Codex agent parity passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
