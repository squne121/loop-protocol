#!/usr/bin/env python3
"""Compatibility entrypoint for Issue #1241 VC AC1.

The canonical contract tests live under `.claude/hooks/tests/`, but this shim
keeps the issue Verification Command path stable.
"""

from __future__ import annotations

import runpy
from pathlib import Path


TARGET = (
    Path(__file__).resolve().parents[3]
    / ".claude"
    / "hooks"
    / "tests"
    / "test_local_main_branch_guard.py"
)

globals().update(runpy.run_path(str(TARGET)))
