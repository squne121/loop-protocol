"""
test_guard_graphql_mutation.py

guard-japanese-prose.sh の GraphQL mutation conservative deny テスト (#655)。

AC4: gh api graphql の body/comment mutation は conservative deny される
AC14: gh api graphql への mutation は Phase 1 として全て deny_graphql_mutation_unsupported で deny
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent
HOOK_SCRIPT = HOOKS_DIR / "guard-japanese-prose.sh"
PROJECT_DIR = HOOKS_DIR.parent.parent
MATRIX = PROJECT_DIR / ".claude/skills/create-issue/scripts/mutation_route_matrix.py"


def run_hook(hook_input, mock_gh=None):
    env = os.environ.copy()
    env["PROJECT_DIR"] = str(PROJECT_DIR)
    env["GUARD_JAPANESE_PROSE_MODE"] = "enforce"
    with tempfile.TemporaryDirectory() as d:
        if mock_gh is not None:
            fp = os.path.join(d, "gh")
            with open(fp, "w") as f:
                f.write("#!/usr/bin/env bash\necho \"fake_gh\" >&2\nexit 0\n")
            os.chmod(fp, 0o755)
            env["PATH"] = d + ":" + env.get("PATH", "")
        return subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=json.dumps(hook_input),
            capture_output=True, text=True, env=env,
        )


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


# ============================================================
# AC4 / AC14: GraphQL conservative deny
# ============================================================

def test_graphql_conservative_deny_body_mutation(tmp_path):
    """AC4/AC14: gh api graphql --input mutation -> deny_graphql_mutation_unsupported"""
    payload = tmp_path / "mutation.json"
    payload.write_text(json.dumps({
        "query": "mutation UpdateIssue($id: ID!, $body: String!) { updateIssue(input: {id: $id, body: $body}) { issue { id } } }",
        "variables": {"id": "I_123", "body": "日本語のテキスト"}
    }))
    r = run_hook(bash(f"gh api graphql --input {payload}"), {})
    assert r.returncode == 2, f"Expected deny, got {r.returncode}\n{r.stderr}"
    assert "deny_graphql_mutation_unsupported" in r.stderr, f"Expected reason code\n{r.stderr}"


def test_graphql_all_deny_any_mutation(tmp_path):
    """AC14: Phase 1 - any GraphQL mutation is denied, regardless of body/comment keyword"""
    payload = tmp_path / "mutation.json"
    payload.write_text(json.dumps({
        "query": "mutation AddLabel($id: ID!, $labelIds: [ID!]!) { addLabelsToLabelable(input: {labelableId: $id, labelIds: $labelIds}) { labelable { id } } }",
        "variables": {"id": "I_123", "labelIds": ["L_1"]}
    }))
    r = run_hook(bash(f"gh api graphql --input {payload}"), {})
    assert r.returncode == 2, f"Expected deny for any mutation, got {r.returncode}\n{r.stderr}"
    assert "deny_graphql_mutation_unsupported" in r.stderr


def test_graphql_not_mutation_passes(tmp_path):
    """AC4: GraphQL query (not mutation) -> exit 0"""
    payload = tmp_path / "query.json"
    payload.write_text(json.dumps({
        "query": "query GetIssue($id: ID!) { node(id: $id) { ... on Issue { title } } }",
        "variables": {"id": "I_123"}
    }))
    r = run_hook(bash(f"gh api graphql --input {payload}"), {})
    assert r.returncode == 0, f"Expected pass for query, got {r.returncode}\n{r.stderr}"


def test_graphql_stdin_fail_closed():
    """AC4: gh api graphql --input - (stdin) -> fail-closed"""
    r = run_hook(bash("gh api graphql --input -"), {})
    assert r.returncode == 2, f"Expected fail-closed for stdin graphql, got {r.returncode}\n{r.stderr}"


def test_graphql_invalid_json_fail_closed(tmp_path):
    """AC4: gh api graphql --input invalid JSON -> fail-closed"""
    payload = tmp_path / "bad.json"
    payload.write_text("not valid json")
    r = run_hook(bash(f"gh api graphql --input {payload}"), {})
    assert r.returncode == 2, f"Expected fail-closed for invalid JSON graphql, got {r.returncode}\n{r.stderr}"


# ============================================================
# B1: -f / --raw-field / -F / --field の inline query 形式
# ============================================================

def test_graphql_conservative_deny_inline_f():
    """B1: gh api graphql -f query='mutation {...}' -> deny_graphql_mutation_unsupported"""
    r = run_hook(bash("gh api graphql -f query='mutation UpdateIssue($id: ID!) { updateIssue(input: {id: $id}) { issue { id } } }'"), {})
    assert r.returncode == 2, f"Expected deny, got {r.returncode}\n{r.stderr}"
    assert "deny_graphql_mutation_unsupported" in r.stderr, f"Expected reason code\n{r.stderr}"


def test_graphql_conservative_deny_inline_raw_field():
    """B1: gh api graphql --raw-field query='mutation {...}' -> deny_graphql_mutation_unsupported"""
    r = run_hook(bash("gh api graphql --raw-field query='mutation AddLabel($id: ID!) { addLabelsToLabelable(input: {labelableId: $id, labelIds: []}) { labelable { id } } }'"), {})
    assert r.returncode == 2, f"Expected deny, got {r.returncode}\n{r.stderr}"
    assert "deny_graphql_mutation_unsupported" in r.stderr, f"Expected reason code\n{r.stderr}"


def test_graphql_all_deny_inline_F():
    """B1: gh api graphql -F query='mutation {...}' -> deny_graphql_mutation_unsupported"""
    r = run_hook(bash("gh api graphql -F query='mutation CreateComment($id: ID!) { addComment(input: {subjectId: $id, body: \"test\"}) { commentEdge { node { id } } } }'"), {})
    assert r.returncode == 2, f"Expected deny, got {r.returncode}\n{r.stderr}"
    assert "deny_graphql_mutation_unsupported" in r.stderr


def test_graphql_all_deny_inline_field():
    """B1: gh api graphql --field query='mutation {...}' -> deny_graphql_mutation_unsupported"""
    r = run_hook(bash("gh api graphql --field query='mutation RemoveLabel($id: ID!) { removeLabelsFromLabelable(input: {labelableId: $id, labelIds: []}) { labelable { id } } }'"), {})
    assert r.returncode == 2, f"Expected deny, got {r.returncode}\n{r.stderr}"
    assert "deny_graphql_mutation_unsupported" in r.stderr
