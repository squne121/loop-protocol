#!/usr/bin/env python3
"""fake_gh.py -- thin forwarder to the canonical fixture implementation.

Issue #2299 AC5 mentions both `scripts/claude-gpt/tests/fixtures/fake_gh.py`
and `.claude/skills/create-issue/tests/fixtures/fake_gh.py` as the fake `gh`
provider used by the `issue_create` runtime smoke scenario. To avoid
duplicating the fake `gh` CLI surface in two places (DRY), this file is a
thin forwarder that execs the canonical implementation at
`.claude/skills/create-issue/tests/fixtures/fake_gh.py`. Both paths remain
independently executable (both are chmod +x and both can be put at the front
of `PATH` as `gh`), but there is only one implementation to maintain.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANONICAL = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "tests" / "fixtures" / "fake_gh.py"


def main() -> int:
    if not _CANONICAL.is_file():
        sys.stderr.write(f"fake_gh.py (forwarder): canonical fixture not found at {_CANONICAL}\n")
        return 2
    os.execv(sys.executable, [sys.executable, str(_CANONICAL), *sys.argv[1:]])
    return 1  # unreachable: os.execv replaces this process on success


if __name__ == "__main__":
    raise SystemExit(main())
