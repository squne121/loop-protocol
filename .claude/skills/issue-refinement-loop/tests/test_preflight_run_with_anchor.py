"""
test_preflight_run_with_anchor.py

Tests for the `preflight.run.with_anchor` sibling exact profile added to
command_registry.py (Issue #1498).

Covers AC1, AC2, and Positive/Negative Test Matrix items #1, #9-#14.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as reg  # noqa: E402


# ---------------------------------------------------------------------------
# AC1: preflight.run.with_anchor is a sibling exact profile; preflight.run
# itself is byte-for-byte unmodified.
# ---------------------------------------------------------------------------

# Snapshot of the exact `preflight.run` entry as it existed prior to Issue
# #1498. If this entry ever changes, this test must fail loudly (AC1) rather
# than silently pass, since the Issue's core invariant is that `preflight.run`
# is untouched by the sibling-profile addition.
_EXPECTED_PREFLIGHT_RUN_ENTRY = {
    "id": "preflight.run",
    "argv": [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "{issue_number}",
        "--repo", "{repo}",
    ],
    "shell": False,
    "cwd_policy": "repo_root",
    "execution_class": "exact_skill_runtime",
    "required_cwd": "canonical_main_root",
    "required_branch": "default_branch",
    "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
    "network_effect": "github_read_only",
    "stdin_contract": "none",
    "stdout_contract": "refinement_preflight_result/v1",
    "timeout_seconds": 120,
    "mutation": False,
    "placeholders": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
    },
}


def test_registry_sibling_profile_preserves_preflight_run():
    """AC1: `preflight.run.with_anchor` exists as a sibling entry and
    `preflight.run` itself is unchanged (argv/placeholders/execution_class)."""
    assert "preflight.run.with_anchor" in reg.REGISTRY
    assert reg.REGISTRY["preflight.run"] == _EXPECTED_PREFLIGHT_RUN_ENTRY

    anchor_entry = reg.REGISTRY["preflight.run.with_anchor"]
    assert anchor_entry["execution_class"] == "exact_skill_runtime_anchor"
    assert anchor_entry["required_cwd"] == "canonical_main_root"
    assert anchor_entry["required_branch"] == "default_branch"
    assert anchor_entry["network_effect"] == "github_read_only"
    assert anchor_entry["mutation"] is False
    assert anchor_entry["allowed_write_roots"] == [
        ".claude/artifacts/issue-refinement-loop/{active_issue}/"
    ]
    assert anchor_entry["argv"] == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "{issue_number}",
        "--repo", "{repo}",
        "--anchor-comment-url", "{anchor_comment_url}",
    ]
    assert anchor_entry["placeholders"]["anchor_comment_url"] == {
        "type": "github_issue_comment_url",
        "required": True,
    }


def test_registry_sibling_profile_renders_argv():
    """render_command() produces the expected 10-token argv for
    preflight.run.with_anchor."""
    url = "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503"
    argv = reg.render_command(
        "preflight.run.with_anchor",
        {"issue_number": 1492, "repo": "squne121/loop-protocol", "anchor_comment_url": url},
    )
    assert argv == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "1492",
        "--repo", "squne121/loop-protocol",
        "--anchor-comment-url", url,
    ]


def test_registry_sibling_profile_missing_anchor_raises():
    """render_command() fails closed when the required anchor_comment_url
    placeholder is missing."""
    with pytest.raises(ValueError):
        reg.render_command(
            "preflight.run.with_anchor",
            {"issue_number": 1492, "repo": "squne121/loop-protocol"},
        )


# ---------------------------------------------------------------------------
# AC2: github_issue_comment_url placeholder type — Positive/Negative Test
# Matrix #9-#14.
# ---------------------------------------------------------------------------

_VALID_URL = "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503"

_NEGATIVE_URLS = {
    # Matrix #9: pull request review comment URL, not an issue comment URL.
    "pull_request_review_comment": (
        "https://github.com/squne121/loop-protocol/pull/1492/files#r1234567"
    ),
    # Matrix #10: discussion_r fragment form (PR review comment fragment).
    "discussion_r_fragment": (
        "https://github.com/squne121/loop-protocol/issues/1492#discussion_r1234567"
    ),
    # Matrix #11: query string present.
    "query_string": (
        "https://github.com/squne121/loop-protocol/issues/1492?tab=timeline"
        "#issuecomment-4959671503"
    ),
    # Matrix #12: extra fragment / suffix / trailing slash.
    "trailing_slash": (
        "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503/"
    ),
    "extra_suffix": (
        "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503-extra"
    ),
    # Matrix #13: userinfo, port, non-GitHub host, HTTP scheme.
    "userinfo": (
        "https://user:pass@github.com/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "port": (
        "https://github.com:8443/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "non_github_host": (
        "https://evil.example.com/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "subdomain_host": (
        "https://gist.github.com/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "http_scheme": (
        "http://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503"
    ),
    # Matrix #14: percent-encoding disguise of canonical shape.
    "percent_encoded_hash": (
        "https://github.com/squne121/loop-protocol/issues/1492%23issuecomment-4959671503"
    ),
    "percent_encoded_dotdot": (
        "https://github.com/squne121/loop-protocol/%2e%2e/issues/1492"
        "#issuecomment-4959671503"
    ),
    # Not a URL at all / empty
    "empty": "",
    "not_a_url": "not-a-url",
}


class TestGithubIssueCommentUrlPlaceholderType:
    def test_valid_url_accepted(self):
        argv = reg.render_command(
            "preflight.run.with_anchor",
            {
                "issue_number": 1492,
                "repo": "squne121/loop-protocol",
                "anchor_comment_url": _VALID_URL,
            },
        )
        assert _VALID_URL in argv

    @pytest.mark.parametrize("name", sorted(_NEGATIVE_URLS.keys()))
    def test_negative_matrix_rejected(self, name: str):
        url = _NEGATIVE_URLS[name]
        with pytest.raises(ValueError):
            reg.render_command(
                "preflight.run.with_anchor",
                {
                    "issue_number": 1492,
                    "repo": "squne121/loop-protocol",
                    "anchor_comment_url": url,
                },
            )


def test_github_issue_comment_url_type_rejects_negative_matrix():
    """AC2 entrypoint test referenced by the Issue's Verification Commands."""
    for url in _NEGATIVE_URLS.values():
        with pytest.raises(ValueError):
            reg.render_command(
                "preflight.run.with_anchor",
                {
                    "issue_number": 1492,
                    "repo": "squne121/loop-protocol",
                    "anchor_comment_url": url,
                },
            )


def test_registry_export_is_json_serializable_with_anchor_entry():
    """export_registry() (used by --list) does not choke on the new entry."""
    import json

    data = reg.export_registry()
    assert "preflight.run.with_anchor" in data["commands"]
    json.dumps(data)  # must not raise


def test_registry_entry_is_a_deep_copy_safe_snapshot():
    """Sanity: mutating a returned export dict must not corrupt REGISTRY."""
    data = reg.export_registry()
    mutated = copy.deepcopy(data)
    mutated["commands"]["preflight.run.with_anchor"]["argv"] = ["tampered"]
    assert reg.REGISTRY["preflight.run.with_anchor"]["argv"] != ["tampered"]
