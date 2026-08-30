"""Strict fake-GitHub coverage for the Issue #2433 runtime smoke."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_gh.py"
REPO = "squne121/loop-protocol"
ISSUE_NUMBER = "9100"
GET_JQ = '{title, body, updatedAt: .updated_at, isPullRequest: has("pull_request")}'


def _seed_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "next_number": 9101,
                "issues": {
                    ISSUE_NUMBER: {
                        "repo": REPO,
                        "title": "before title",
                        "body": "before body",
                        "state": "open",
                        "updatedAt": "2026-08-30T00:00:00Z",
                        "url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
                    }
                },
                "calls": [],
            }
        ),
        encoding="utf-8",
    )


def _run(state_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["FAKE_GH_STATE"] = str(state_path)
    return subprocess.run(
        [sys.executable, str(FIXTURE), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_issue_content_get_and_patch_are_exact_and_readable(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _seed_state(state_path)
    endpoint = f"repos/{REPO}/issues/{ISSUE_NUMBER}"

    get = _run(state_path, "api", "--hostname", "github.com", endpoint, "--jq", GET_JQ)
    assert get.returncode == 0, get.stderr
    assert json.loads(get.stdout) == {
        "title": "before title",
        "body": "before body",
        "updatedAt": "2026-08-30T00:00:00Z",
        "isPullRequest": False,
    }

    input_path = tmp_path / "content.json"
    input_path.write_text(json.dumps({"title": "after title", "body": "after body"}), encoding="utf-8")
    patch = _run(
        state_path,
        "api",
        "--hostname",
        "github.com",
        "--method",
        "PATCH",
        endpoint,
        "--input",
        str(input_path),
    )
    assert patch.returncode == 0, patch.stderr

    final_get = _run(state_path, "api", "--hostname", "github.com", endpoint, "--jq", GET_JQ)
    assert final_get.returncode == 0, final_get.stderr
    assert json.loads(final_get.stdout) == {
        "title": "after title",
        "body": "after body",
        "updatedAt": "2026-08-30T00:00:01Z",
        "isPullRequest": False,
    }

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["issue_content_patch_count"] == 1
    assert [call["operation"] for call in state["calls"]].count("issue_content_get") == 2
    assert [call["operation"] for call in state["calls"]].count("issue_content_patch") == 1


def test_issue_content_api_near_miss_is_rejected_without_patch(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _seed_state(state_path)
    endpoint = f"repos/{REPO}/issues/{ISSUE_NUMBER}"
    input_path = tmp_path / "content.json"
    input_path.write_text(json.dumps({"title": "after title", "body": "after body"}), encoding="utf-8")

    result = _run(
        state_path,
        "api",
        "--hostname",
        "github.com",
        "--method",
        "PATCH",
        endpoint,
        "--input",
        str(input_path),
        "--silent",
    )
    assert result.returncode != 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("issue_content_patch_count", 0) == 0
    assert state["issues"][ISSUE_NUMBER]["body"] == "before body"
