#!/usr/bin/env python3
"""Validate Codex-specific impl-review-loop dispatch constraints."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

import yaml

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
        "task_name_template": "implementation_i{iteration}",
        "agent_type": "implementation-worker",
        "message_binding_phrases": (
            "actual Issue number",
            "full Issue URL",
            "contract snapshot URL",
            "actual live Allowed Paths",
            "serialized fix_delta",
        ),
    },
    ".claude/skills/impl-review-loop/steps/step-2-verification.md": {
        "task_name_template": "verification_i{iteration}",
        "agent_type": "test-runner",
        "message_binding_phrases": (
            "actual Issue number",
            "PR number",
            "contract body SHA",
            "diff head SHA",
            "literal AC list",
            "literal Verification Commands",
        ),
    },
    ".claude/skills/impl-review-loop/steps/step-4-pr-review.md": {
        "task_name_template": "pr_review_i{iteration}",
        "agent_type": "pr-reviewer",
        "message_binding_phrases": (
            "actual PR number",
            "linked Issue number",
            "reviewed head SHA",
            "actual PR diff",
            "Allowed Paths",
            "Verification evidence",
            "required checks",
        ),
    },
    ".claude/skills/post-merge-cleanup/SKILL.md": {
        "task_name_template": "post_merge_cleanup_pr{merged_pr_number}_i{attempt}",
        "agent_type": "post-merge-cleanup-worker",
        "message_binding_phrases": (
            "actual merged PR number",
            "linked Issue number",
            "actual worktree",
            "actual branch",
            "canonical cleanup scripts",
            "follow-up candidates",
        ),
    },
}

PREPARATION_MD = ".claude/skills/impl-review-loop/steps/preparation.md"
NATIVE_V2_DISPATCH_BLOCK = re.compile(
    r"^```yaml\n(?P<document>spawn_agent:\n(?:.*\n)*?)^```$",
    re.MULTILINE,
)
TASK_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
DISPATCH_ARGUMENT_KEYS = frozenset({"task_name", "agent_type", "fork_turns", "message"})
DISPATCH_ROOT_KEYS = frozenset({"spawn_agent"})
MESSAGE_REQUIRED_ELEMENTS = (
    "Objective",
    "Live reference",
    "Bounded scope",
    "Expected result",
)
COMPLETION_PROTOCOL_REFERENCE = "Common Completion Protocol"
UNRESOLVED_MESSAGE_REFERENCES = (
    "LOOP_STATE",
    "Step 1 PR number",
    "current contract body SHA",
    "current reviewed head SHA",
)


class DuplicateDispatchKeyError(yaml.YAMLError):
    """Raised when a fenced dispatch document repeats a mapping key."""


class StrictDispatchLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves the runtime's duplicate-key rejection."""


def _construct_unique_mapping(
    loader: StrictDispatchLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateDispatchKeyError(f"duplicate YAML key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictDispatchLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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


def parse_native_v2_dispatch_blocks(text: str) -> tuple[list[dict[str, object]], list[str]]:
    """Strictly parse fenced ``spawn_agent`` documents without YAML relaxation."""
    blocks: list[dict[str, object]] = []
    failures: list[str] = []
    for block_number, match in enumerate(NATIVE_V2_DISPATCH_BLOCK.finditer(text), start=1):
        try:
            document = yaml.load(match.group("document"), Loader=StrictDispatchLoader)
        except yaml.YAMLError as error:
            failures.append(f"V2 dispatch block {block_number}: malformed YAML ({error})")
            continue
        if type(document) is not dict:
            failures.append(f"V2 dispatch block {block_number}: document must be a mapping")
            continue
        if set(document) != DISPATCH_ROOT_KEYS:
            failures.append(
                f"V2 dispatch block {block_number}: root keys must be exactly {sorted(DISPATCH_ROOT_KEYS)!r}"
            )
            continue
        arguments = document["spawn_agent"]
        if type(arguments) is not dict:
            failures.append(f"V2 dispatch block {block_number}: spawn_agent must be a mapping")
            continue
        if set(arguments) != DISPATCH_ARGUMENT_KEYS:
            failures.append(
                f"V2 dispatch block {block_number}: spawn_agent keys must be exactly "
                f"{sorted(DISPATCH_ARGUMENT_KEYS)!r}"
            )
            continue
        blocks.append(arguments)
    return blocks, failures


def materialize_task_name(template: str, **bindings: int) -> str:
    """Materialize one documented task-name rule with concrete scalar bindings."""
    try:
        task_name = template.format(**bindings)
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid task-name materialization: {error}") from error
    if TASK_NAME_PATTERN.fullmatch(task_name) is None:
        raise ValueError("materialized task_name must use lowercase letters, digits, and underscores")
    return task_name


def assert_unique_canonical_task_name(task_name: str, used_task_names: set[str]) -> None:
    """Reject a canonical task path before a retry attempts to reserve it again."""
    if task_name in used_task_names:
        raise ValueError(f"canonical task name already used: {task_name}")
    used_task_names.add(task_name)


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
            blocks, parse_failures = parse_native_v2_dispatch_blocks(path.read_text(encoding="utf-8"))
        except OSError as error:
            failures.append(f"{relative_path}: cannot read dispatch site ({error.__class__.__name__})")
            continue

        failures.extend(f"{relative_path}: {failure}" for failure in parse_failures)

        if len(blocks) != 1:
            failures.append(
                f"{relative_path}: expected exactly one V2 dispatch block, found {len(blocks)}"
            )
            continue

        block = blocks[0]
        if block["task_name"] != expected["task_name_template"]:
            failures.append(
                f"{relative_path}: task_name must use materialization rule "
                f"{expected['task_name_template']!r}"
            )
        elif not isinstance(block["task_name"], str):
            failures.append(f"{relative_path}: task_name must be a string")
        if block.get("agent_type") != expected["agent_type"]:
            failures.append(
                f"{relative_path}: agent_type must be {expected['agent_type']!r}"
            )
        if type(block["agent_type"]) is not str:
            failures.append(f"{relative_path}: agent_type must be a string")
        if block.get("fork_turns") != "none" or type(block["fork_turns"]) is not str:
            failures.append(f"{relative_path}: fork_turns must be 'none'")
        message = block.get("message", "")
        if type(message) is not str or not message.strip():
            failures.append(f"{relative_path}: message must be non-empty and self-contained")
            continue
        for element in MESSAGE_REQUIRED_ELEMENTS:
            if re.search(rf"(?m)^{re.escape(element)}:\s*\S", message) is None:
                failures.append(
                    f"{relative_path}: message must include non-empty '{element}:'"
                )
        for phrase in expected["message_binding_phrases"]:
            if phrase not in message:
                failures.append(
                    f"{relative_path}: message must require concrete binding for '{phrase}'"
                )
        for unresolved_reference in UNRESOLVED_MESSAGE_REFERENCES:
            if unresolved_reference in message:
                failures.append(
                    f"{relative_path}: message must not contain unresolved reference "
                    f"'{unresolved_reference}'"
                )
        if COMPLETION_PROTOCOL_REFERENCE not in path.read_text(encoding="utf-8"):
            failures.append(
                f"{relative_path}: must reference the common normative completion protocol"
            )


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
