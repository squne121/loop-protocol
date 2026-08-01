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

# #1869 fix_delta P1-1: scope-rollup-runner is a manual advisory diagnostic,
# not an automatic Step dispatch -- it is intentionally NOT required here.
# preparation.md must NOT contain an explicit automatic spawn note for it
# (see assert_no_scope_rollup_runner_auto_spawn_note below, which inverts
# the old "must contain" check into a "must not contain" check).
# The static contract intentionally validates documentation call shape only.  It
# is not evidence that a native runtime can spawn, authorize, or complete an
# agent task; Issue #1841 owns that runtime verification.
NATIVE_V2_DISPATCH_SITES = {
    ".claude/skills/impl-review-loop/steps/step-1-implementation.md": {
        "task_name": "implementation_i0",
        "agent_type": "implementation-worker",
    },
    ".claude/skills/impl-review-loop/steps/step-2-verification.md": {
        "task_name": "verification_i0",
        "agent_type": "test-runner",
    },
    ".claude/skills/impl-review-loop/steps/step-4-pr-review.md": {
        "task_name": "pr_review_i0",
        "agent_type": "pr-reviewer",
    },
    ".claude/skills/post-merge-cleanup/SKILL.md": {
        "task_name": "post_merge_cleanup_pr1900",
        "agent_type": "post-merge-cleanup-worker",
    },
}

PREPARATION_MD = ".claude/skills/impl-review-loop/steps/preparation.md"
NATIVE_V2_DISPATCH_BLOCK = re.compile(
    r"^```yaml\nspawn_agent:\n(?P<arguments>(?: {2,}[^\n]*\n)+?)^```$",
    re.MULTILINE,
)
TASK_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")


def read_toml(path: Path) -> tuple[dict | None, str | None]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), None
    except FileNotFoundError:
        return None, f"{path}: TOML file not found"
    except PermissionError:
        return None, f"{path}: TOML file is not readable"
    except OSError:
        return None, f"{path}: TOML I/O error"
    except tomllib.TOMLDecodeError:
        return None, f"{path}: malformed TOML"


def read_table(
    config: dict,
    key: str,
    table_name: str,
    config_path: Path,
) -> tuple[dict | None, str | None]:
    """Read an optional TOML table without treating scalar values as mappings."""
    value = config.get(key, {})
    if not isinstance(value, dict):
        return None, f"{config_path}: [{table_name}] must be a table"
    return value, None


def assert_project_declares_multi_agent_v2_enabled(config_path: Path) -> list[str]:
    """Validate the repository-pinned Multi-Agent V2 declaration."""
    config, error = read_toml(config_path)
    if error:
        return [error]

    assert config is not None
    features, error = read_table(config, "features", "features", config_path)
    if error:
        return [error]
    assert features is not None
    multi_agent_v2 = features.get("multi_agent_v2")
    if not isinstance(multi_agent_v2, dict):
        return [f"{config_path}: [features.multi_agent_v2] must be declared"]

    failures: list[str] = []
    if type(multi_agent_v2.get("enabled")) is not bool or multi_agent_v2["enabled"] is not True:
        failures.append(f"{config_path}: [features.multi_agent_v2].enabled must be strict boolean true")
    if (
        type(multi_agent_v2.get("max_concurrent_threads_per_session")) is not int
        or multi_agent_v2["max_concurrent_threads_per_session"] != 4
    ):
        failures.append(
            f"{config_path}: [features.multi_agent_v2].max_concurrent_threads_per_session "
            "must be strict integer 4"
        )
    return failures


def assert_no_max_depth_setting(config_path: Path) -> list[str]:
    """Reject every legacy [agents].max_depth value, including zero."""
    config, error = read_toml(config_path)
    if error:
        return [error]

    assert config is not None
    agents, table_error = read_table(config, "agents", "agents", config_path)
    if table_error:
        return [table_error]
    assert agents is not None
    if isinstance(agents, dict) and "max_depth" in agents:
        return [".codex/config.toml: [agents].max_depth must be absent"]
    return []


def assert_project_declares_multi_agent_v1_config(config_path: Path) -> list[str]:
    """Validate the documented positive V1 rollback configuration."""
    config, error = read_toml(config_path)
    if error:
        return [error]

    assert config is not None
    features, table_error = read_table(config, "features", "features", config_path)
    if table_error:
        return [table_error]
    assert features is not None
    multi_agent_v2 = features.get("multi_agent_v2")
    if not isinstance(multi_agent_v2, dict):
        return [f"{config_path}: [features.multi_agent_v2] must be declared"]

    agents, agents_error = read_table(config, "agents", "agents", config_path)
    if agents_error:
        return [agents_error]
    assert agents is not None

    failures: list[str] = []
    if type(multi_agent_v2.get("enabled")) is not bool or multi_agent_v2["enabled"] is not False:
        failures.append(f"{config_path}: [features.multi_agent_v2].enabled must be strict boolean false")
    if type(agents.get("max_depth")) is not int or agents["max_depth"] != 1:
        failures.append(f"{config_path}: [agents].max_depth must be strict integer 1")
    return failures


def assert_no_project_profile_routing(failures: list[str]) -> None:
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    if re.search(r"^\s*profile\s*=", raw, re.MULTILINE):
        failures.append(".codex/config.toml: project-local profile= must not be used for routing")
    if re.search(r"^\s*\[profiles(?:\.|\])", raw, re.MULTILINE):
        failures.append(".codex/config.toml: project-local [profiles] blocks must not be used for routing")
    required_phrase = "Project-local config is not evidence of profile routing"
    if required_phrase not in raw:
        failures.append(f".codex/config.toml: missing phrase '{required_phrase}'")


def parse_native_v2_dispatch_blocks(text: str) -> list[dict[str, str]]:
    """Extract fenced static ``spawn_agent`` call-shape blocks from a document."""
    blocks: list[dict[str, str]] = []
    for match in NATIVE_V2_DISPATCH_BLOCK.finditer(text):
        lines = match.group("arguments").splitlines()
        fields: dict[str, str] = {}
        for index, line in enumerate(lines):
            field_match = re.fullmatch(r"  (task_name|agent_type|fork_turns|message):(?: (.*))?", line)
            if field_match is None:
                continue
            field, value = field_match.groups()
            if field == "message" and value == "|":
                message_lines: list[str] = []
                for message_line in lines[index + 1 :]:
                    if not message_line.startswith("    "):
                        break
                    message_lines.append(message_line[4:])
                fields[field] = "\n".join(message_lines).strip()
            else:
                fields[field] = (value or "").strip()
        blocks.append(fields)
    return blocks


def assert_native_v2_dispatch_contract(
    failures: list[str],
    *,
    repo_root: Path = REPO_ROOT,
    dispatch_sites: dict[str, dict[str, str]] = NATIVE_V2_DISPATCH_SITES,
) -> None:
    """Validate exactly one self-contained V2 static call shape per canonical site."""
    for relative_path, expected in dispatch_sites.items():
        path = repo_root / relative_path
        try:
            blocks = parse_native_v2_dispatch_blocks(path.read_text(encoding="utf-8"))
        except OSError as error:
            failures.append(f"{relative_path}: cannot read dispatch site ({error.__class__.__name__})")
            continue

        if len(blocks) != 1:
            failures.append(
                f"{relative_path}: expected exactly one V2 dispatch block, found {len(blocks)}"
            )
            continue

        block = blocks[0]
        if block.get("task_name") != expected["task_name"]:
            failures.append(
                f"{relative_path}: task_name must be {expected['task_name']!r}"
            )
        elif TASK_NAME_PATTERN.fullmatch(block["task_name"]) is None:
            failures.append(f"{relative_path}: task_name must use lowercase letters, digits, and underscores")
        if block.get("agent_type") != expected["agent_type"]:
            failures.append(
                f"{relative_path}: agent_type must be {expected['agent_type']!r}"
            )
        if block.get("fork_turns") != "none":
            failures.append(f"{relative_path}: fork_turns must be 'none'")
        if not block.get("message"):
            failures.append(f"{relative_path}: message must be non-empty and self-contained")


def assert_no_scope_rollup_runner_auto_spawn_note(failures: list[str]) -> None:
    """#1869 fix_delta P1-1 (inverted check): preparation.md must NOT contain
    an explicit automatic-spawn note for scope-rollup-runner. It is a manual
    advisory diagnostic, not an automatic Step dispatch."""
    text = (REPO_ROOT / PREPARATION_MD).read_text(encoding="utf-8")
    forbidden_phrase = (
        "Codex CLI: spawn the custom agent named scope-rollup-runner for this step; "
        "the root thread must not"
    )
    if forbidden_phrase in text:
        failures.append(
            f"{PREPARATION_MD}: must NOT contain an automatic spawn note for "
            "scope-rollup-runner (manual advisory diagnostic only, #1869 fix_delta P1-1)"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-path", type=Path, default=CONFIG_PATH)
    config_assertions = parser.add_mutually_exclusive_group()
    config_assertions.add_argument("--assert-project-multi-agent-v2-config", action="store_true")
    config_assertions.add_argument("--assert-project-multi-agent-v1-config", action="store_true")
    parser.add_argument("--assert-no-max-depth-setting", action="store_true")
    parser.add_argument("--assert-no-project-profile-routing", action="store_true")
    parser.add_argument("--assert-native-v2-dispatch-contract", action="store_true")
    parser.add_argument("--assert-explicit-spawn-notes", action="store_true")
    parser.add_argument("--assert-no-scope-rollup-runner-auto-spawn-note", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    assertion_selected = any(
        (
            args.assert_project_multi_agent_v2_config,
            args.assert_project_multi_agent_v1_config,
            args.assert_no_max_depth_setting,
            args.assert_no_project_profile_routing,
            args.assert_native_v2_dispatch_contract,
            args.assert_explicit_spawn_notes,
            args.assert_no_scope_rollup_runner_auto_spawn_note,
        )
    )
    if not assertion_selected:
        parser.error("specify at least one assertion flag")

    failures: list[str] = []
    if args.assert_project_multi_agent_v2_config:
        failures.extend(assert_project_declares_multi_agent_v2_enabled(args.config_path))
        failures.extend(assert_no_max_depth_setting(args.config_path))
    elif args.assert_project_multi_agent_v1_config:
        failures.extend(assert_project_declares_multi_agent_v1_config(args.config_path))
    elif args.assert_no_max_depth_setting:
        failures.extend(assert_no_max_depth_setting(args.config_path))
    if args.assert_no_project_profile_routing:
        assert_no_project_profile_routing(failures)
    if args.assert_native_v2_dispatch_contract or args.assert_explicit_spawn_notes:
        assert_native_v2_dispatch_contract(failures)
    if args.assert_no_scope_rollup_runner_auto_spawn_note:
        assert_no_scope_rollup_runner_auto_spawn_note(failures)

    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1

    print("OK: Codex dispatch contract validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
