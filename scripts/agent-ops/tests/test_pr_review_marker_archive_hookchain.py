"""test_pr_review_marker_archive_hookchain.py -- Issue #1636 AC3-AC7.

Real PreToolUse hook chain verification for the post-merge-cleanup-worker
canonical executor invocation (`pr_review_marker_archive_exec.py`), driven
through `.claude/hooks/tests/hookchain_harness.py` against the REAL,
currently-registered `.claude/settings.json` PreToolUse hook chain (real
subprocesses, real stdin JSON) -- not a hand-picked subset of hooks and not
an in-process function call.

The canonical invocation string is derived from
`.claude/skills/post-merge-cleanup/SKILL.md` (not reconstructed by hand in
this test), and the worker's tool/permission posture is derived independently
from `.claude/agents/post-merge-cleanup-worker.md` frontmatter, and the
`permissions.allow` wiring is asserted structurally against the real,
parsed `.claude/settings.json` JSON -- per Issue #1636's Scope Delta, none of
these three sources is duplicated/hardcoded as a parallel spec.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / ".claude" / "hooks" / "tests"))
import hookchain_harness  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "SKILL.md"
WORKER_MD = REPO_ROOT / ".claude" / "agents" / "post-merge-cleanup-worker.md"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"

TEST_PR_NUMBER = 1636


def _extract_canonical_invocation_template() -> str:
    """Read the canonical `pr_review_marker_archive_exec.py` invocation
    documented in the post-merge-cleanup SKILL.md's own fenced bash block
    (read-only reference; this test does not modify the skill), collapsing
    the shell line-continuation into a single command string."""
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(
        r"```bash\n(uv run --locked python3 scripts/agent-ops/"
        r"pr_review_marker_archive_exec\.py.*?)\n```",
        text,
        re.S,
    )
    assert match, (
        "post-merge-cleanup SKILL.md must document the canonical "
        "pr_review_marker_archive_exec.py invocation in a fenced bash block"
    )
    raw = match.group(1)
    collapsed = re.sub(r"\\\s*\n\s*", " ", raw).strip()
    collapsed = re.sub(r"\s+", " ", collapsed)
    return collapsed


def _canonical_command_prefix(template: str) -> str:
    """Return the invocation up to and including `.py`, independent of
    argument values -- this is what the `permissions.allow` glob rule and
    the hook chain payload both key off of."""
    idx = template.index(".py") + len(".py")
    return template[:idx]


def _resolved_canonical_command(template: str, pr_number: int) -> str:
    return template.replace("<merged_pr_number>", str(pr_number))


def _worker_frontmatter() -> dict:
    text = WORKER_MD.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "post-merge-cleanup-worker.md must start with a YAML frontmatter block"
    return yaml.safe_load(match.group(1))


@pytest.fixture
def tmp_git_repo():
    tmpdir = tempfile.mkdtemp(prefix="pr_review_marker_archive_hookchain_")
    try:
        subprocess.run(["git", "init", "-b", "main", tmpdir], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "T"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", tmpdir, "remote", "add", "origin",
             "https://github.com/squne121/loop-protocol.git"],
            check=True, capture_output=True,
        )
        (Path(tmpdir) / "README.md").write_text("test")
        subprocess.run(["git", "-C", tmpdir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "init"], check=True, capture_output=True)

        executor = Path(tmpdir) / "scripts" / "agent-ops" / "pr_review_marker_archive_exec.py"
        executor.parent.mkdir(parents=True, exist_ok=True)
        executor.write_text("# stub\n")

        yield Path(tmpdir)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _pretool_payload(command: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def test_full_chain_aggregate_allow_for_pr_review_marker_archive_command(tmp_git_repo):
    """Issue #1636 AC3."""
    template = _extract_canonical_invocation_template()
    command = _resolved_canonical_command(template, TEST_PR_NUMBER)
    payload = _pretool_payload(command, str(tmp_git_repo))

    results = hookchain_harness.run_pretool_hook_chain(payload, tmp_git_repo)
    assert results, "hook chain must actually execute at least one hook"

    aggregate = hookchain_harness.aggregate_decision(results)
    assert aggregate == "allow", (
        "Aggregate PreToolUse decision across the full real hook chain "
        f"must be allow for the canonical pr_review_marker_archive_exec.py "
        f"invocation, not deny/ask. Per-hook results: {results}"
    )
    for r in results:
        assert r["decision"] == "allow", (
            f"{r['hook_name']} returned decision={r['decision']} "
            f"(exit={r['returncode']}); stderr={r['stderr']}"
        )


def test_worker_frontmatter_bash_and_permission_mode():
    """Issue #1636 AC4."""
    frontmatter = _worker_frontmatter()
    tools = frontmatter.get("tools") or []
    assert "Bash" in tools, (
        f"post-merge-cleanup-worker.md frontmatter tools must include Bash; got {tools}"
    )
    assert frontmatter.get("permissionMode") == "default", (
        "post-merge-cleanup-worker.md frontmatter permissionMode must be "
        f"'default'; got {frontmatter.get('permissionMode')!r}"
    )


def test_settings_permissions_allow_rule_structural():
    """Issue #1636 AC5."""
    template = _extract_canonical_invocation_template()
    prefix = _canonical_command_prefix(template)
    expected_rule = f"Bash({prefix} *)"

    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    permissions = data.get("permissions", {})
    allow_rules = permissions.get("allow", [])
    ask_rules = permissions.get("ask", [])
    deny_rules = permissions.get("deny", [])

    assert expected_rule in allow_rules, (
        f"expected canonical rule {expected_rule!r} must be present in "
        f"permissions.allow; allow_rules={allow_rules}"
    )
    assert expected_rule not in ask_rules, (
        f"canonical rule {expected_rule!r} must not also appear in "
        "permissions.ask"
    )
    assert expected_rule not in deny_rules, (
        f"canonical rule {expected_rule!r} must not also appear in "
        "permissions.deny"
    )


def test_negative_control_denies_unauthorized_command(tmp_git_repo):
    """Negative control (Issue #1636 AC6): proves the harness genuinely
    detects a real block, not a fail-open false negative. A known
    unauthorized raw command (`gh pr review --approve`, issued directly
    from local root rather than through a controlled executor) must be
    denied by the real hook chain."""
    raw_cmd = f"gh pr review {TEST_PR_NUMBER} --approve --body x"
    payload = _pretool_payload(raw_cmd, str(tmp_git_repo))
    results = hookchain_harness.run_pretool_hook_chain(payload, tmp_git_repo)
    aggregate = hookchain_harness.aggregate_decision(results)
    assert aggregate == "block", (
        "raw `gh pr review --approve` must be denied (deny/block) by the "
        f"real hook chain; results={results}"
    )
    assert any(r["decision"] == "deny" for r in results), (
        f"at least one hook in the chain must classify this command as "
        f"deny; results={results}"
    )


def test_python_test_plan_includes_agent_ops_tests():
    """Issue #1636 AC7."""
    plan_path = REPO_ROOT / ".github" / "ci" / "python-test-plan.json"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    targets = json.dumps(data)
    assert "scripts/agent-ops/tests/" in targets, (
        "scripts/agent-ops/tests/ must be a CI python-test target so "
        "this file's tests run in CI; python-test-plan.json="
        f"{data}"
    )
