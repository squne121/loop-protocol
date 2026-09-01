#!/usr/bin/env python3
"""Emit one secret-free runtime classification for a fixed Claude-GPT probe ID."""

from __future__ import annotations

import os
import sys


PROBE_IDS = frozenset(
    {
        "claude-gpt-root-state",
        "claude-gpt-home-class",
        "claude-gpt-root-relation",
    }
)


def _is_valid_absolute(value: str | None) -> bool:
    """Return whether a raw value has the contract's lexical absolute form."""
    return bool(value) and value.startswith("/")


def _classify(probe_id: str, claude_gpt_home: str | None, home: str | None) -> str:
    """Classify two raw environment values without resolving or exposing either."""
    both_absolute = _is_valid_absolute(claude_gpt_home) and _is_valid_absolute(home)

    if probe_id == "claude-gpt-root-state":
        return "runtime_root=present" if claude_gpt_home else "runtime_root=absent"

    if probe_id == "claude-gpt-home-class":
        if both_absolute and home == f"{claude_gpt_home}/claude-home":
            return "home_class=isolated"
        if both_absolute and claude_gpt_home == f"{home}/.claude-gpt":
            return "home_class=nested"
        return "home_class=unexpected"

    if both_absolute and claude_gpt_home == home:
        return "root_relation=same"
    if both_absolute and claude_gpt_home == f"{home}/.claude-gpt":
        return "root_relation=nested"
    return "root_relation=other"


def main(argv: list[str]) -> int:
    """Validate the fixed positional interface and write exactly one fixed result."""
    if len(argv) != 1 or argv[0] not in PROBE_IDS:
        print("error=invalid_arguments")
        return 2

    claude_gpt_home = os.environ.get("CLAUDE_GPT_HOME")
    home = os.environ.get("HOME")
    print(_classify(argv[0], claude_gpt_home, home))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
