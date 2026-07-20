"""conftest.py for tests/agent_guards/ (Issue #1657 AC8).

`pyproject.toml` runs pytest with `--import-mode=importlib`, which does NOT
add each test module's own directory to `sys.path`. This conftest inserts
this directory onto `sys.path` (mirrors the existing pattern in
`.claude/hooks/tests/conftest.py`) so that test modules collected here can do
a plain `from worktree_scope_guard_testkit import ...` of the shared,
non-test helper module — without a fragile test-to-test bare import.
"""

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
