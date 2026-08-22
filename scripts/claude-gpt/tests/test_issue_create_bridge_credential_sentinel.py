"""Issue #2299 AC7: GitHub credential never leaks to stdout/stderr/artifact.

The credential-isolated bridge (Issue #2259 / PR #2286) that this test's filename
references was closed NOT_PLANNED and its transport machinery is not part of this
repository. This test survives that reversal because its concern -- GitHub
credential material must never appear in launcher stdout/stderr or evidence
artifacts -- is independent of any specific transport. Issue #2299 makes
`GH_TOKEN`/`GITHUB_TOKEN`/`GH_ENTERPRISE_TOKEN`/`GITHUB_ENTERPRISE_TOKEN`/`GH_HOST`/
`GH_REPO`/`GH_CONFIG_DIR` compatibility-first (no longer scrubbed from the isolated
Claude-GPT child process), which makes it *more* important, not less, to keep
verifying that the token *value* itself is never echoed anywhere observable.

Uses sentinel (fake, high-entropy, obviously-fake) token values -- never a real
credential -- and asserts they do not appear in `launch.sh --check-only` stdout/
stderr, nor in any `runtime_smoke_test.sh` evidence JSON file written during the
test run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LAUNCH_SH = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"
RUNTIME_SMOKE_SH = REPO_ROOT / "scripts" / "claude-gpt" / "runtime_smoke_test.sh"

# Obviously-fake, high-entropy sentinel values. Never real credentials.
SENTINEL_GH_TOKEN = "sentinel-ghp-DO-NOT-LEAK-0f9c7b21e4a6"
SENTINEL_GITHUB_TOKEN = "sentinel-ghs-DO-NOT-LEAK-7a13de56bc90"
SENTINEL_GH_ENTERPRISE_TOKEN = "sentinel-ghe-DO-NOT-LEAK-3c8891ffab12"
SENTINEL_VALUES = (
    SENTINEL_GH_TOKEN,
    SENTINEL_GITHUB_TOKEN,
    SENTINEL_GH_ENTERPRISE_TOKEN,
)


def _sentinel_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["GH_TOKEN"] = SENTINEL_GH_TOKEN
    env["GITHUB_TOKEN"] = SENTINEL_GITHUB_TOKEN
    env["GH_ENTERPRISE_TOKEN"] = SENTINEL_GH_ENTERPRISE_TOKEN
    # Isolate CLAUDE_GPT_HOME to a throwaway tmp dir so this test run's evidence/state
    # never mixes with a real developer's ~/.claude-gpt.
    env["CLAUDE_GPT_HOME"] = str(tmp_path / "claude-gpt-home")
    return env


def _assert_no_sentinel_leak(text: str, *, where: str) -> None:
    for sentinel in SENTINEL_VALUES:
        assert sentinel not in text, f"sentinel credential leaked into {where}: {sentinel!r} found"


def test_launch_sh_check_only_does_not_leak_sentinel_credential(tmp_path: Path) -> None:
    env = _sentinel_env(tmp_path)
    proc = subprocess.run(
        ["bash", str(LAUNCH_SH), "--check-only"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    _assert_no_sentinel_leak(proc.stdout or "", where="launch.sh --check-only stdout")
    _assert_no_sentinel_leak(proc.stderr or "", where="launch.sh --check-only stderr")


def test_runtime_smoke_test_evidence_and_output_do_not_leak_sentinel_credential(
    tmp_path: Path,
) -> None:
    env = _sentinel_env(tmp_path)
    proc = subprocess.run(
        ["bash", str(RUNTIME_SMOKE_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # PASS (0), FAIL (1), or SKIP (77) are all acceptable outcomes for this test --
    # only the absence of credential leakage is asserted here.
    assert proc.returncode in (0, 1, 77)
    _assert_no_sentinel_leak(proc.stdout or "", where="runtime_smoke_test.sh stdout")
    _assert_no_sentinel_leak(proc.stderr or "", where="runtime_smoke_test.sh stderr")

    evidence_dir = REPO_ROOT / "scripts" / "claude-gpt" / ".evidence"
    if not evidence_dir.is_dir():
        return
    for evidence_file in evidence_dir.glob("smoke-*.json"):
        text = evidence_file.read_text(encoding="utf-8")
        _assert_no_sentinel_leak(text, where=f"evidence artifact {evidence_file.name}")
        # Sanity: evidence files are JSON, not just non-empty text.
        json.loads(text)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
