#!/usr/bin/env python3
"""
Tests for Issue #1511: baseline_vc_preflight.py pnpm allowlist expansion.

AC9: pnpm typecheck:e2e / pnpm lint:docs are classified as allowed (not
     command_not_allowed) by the baseline VC preflight, so a VC using these
     script names is treated as an executable gate rather than blocked.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "baseline_vc_preflight.py"
)

sys.path.insert(0, str(SCRIPT_PATH.parent))


def run_preflight(body_content: str, issue_num: int = 1511) -> dict:
    """Run preflight on a string of body content via a temp file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body_content)
        fixture_file = f.name
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--body-file", fixture_file, "--issue", str(issue_num)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout, f"No output from preflight: stderr={result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(fixture_file)


def test_pnpm_typecheck_e2e_is_allowed():
    """AC9: pnpm typecheck:e2e must not be classified as command_not_allowed."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm typecheck:e2e
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["category"] not in ("unsafe_command", "command_not_allowed", "unsupported_shell_syntax"), (
        f"pnpm typecheck:e2e should be allowed, but got category={r['category']}"
    )


def test_pnpm_lint_docs_is_allowed():
    """AC9: pnpm lint:docs must not be classified as command_not_allowed."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm lint:docs
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["category"] not in ("unsafe_command", "command_not_allowed", "unsupported_shell_syntax"), (
        f"pnpm lint:docs should be allowed, but got category={r['category']}"
    )


def test_pnpm_typecheck_e2e_allowlist_membership_direct():
    """AC1/AC3 regression guard: ('pnpm', 'typecheck:e2e') is a member of the frozenset directly."""
    import baseline_vc_preflight as bvp  # noqa: E402

    assert ("pnpm", "typecheck:e2e") in bvp._ALLOWED_PNPM_SUBCOMMANDS
    assert bvp._FIXED_ENV_DELTA_BY_COMMAND[("pnpm", "typecheck:e2e")] == {"CI": "true"}


def test_pnpm_lint_docs_allowlist_membership_direct():
    """AC2/AC4 regression guard: ('pnpm', 'lint:docs') is a member of the frozenset directly."""
    import baseline_vc_preflight as bvp  # noqa: E402

    assert ("pnpm", "lint:docs") in bvp._ALLOWED_PNPM_SUBCOMMANDS
    assert bvp._FIXED_ENV_DELTA_BY_COMMAND[("pnpm", "lint:docs")] == {"CI": "true"}


def test_pnpm_typecheck_e2e_canonical_gate_resolution():
    """AC9: _canonical_pnpm_gate(['pnpm', 'typecheck:e2e']) resolves to the canonical tuple."""
    import baseline_vc_preflight as bvp  # noqa: E402

    assert bvp._canonical_pnpm_gate(["pnpm", "typecheck:e2e"]) == ("pnpm", "typecheck:e2e")


def test_pnpm_lint_docs_canonical_gate_resolution():
    """AC9: _canonical_pnpm_gate(['pnpm', 'lint:docs']) resolves to the canonical tuple."""
    import baseline_vc_preflight as bvp  # noqa: E402

    assert bvp._canonical_pnpm_gate(["pnpm", "lint:docs"]) == ("pnpm", "lint:docs")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
