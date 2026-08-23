"""
test_skill_runtime_exec_anchor.py

Real subprocess chain tests for `preflight.run.with_anchor` in
skill_runtime_exec.py (Issue #1498).

Covers AC4 (executor reaches real subprocess for Matrix #2, Matrix #4 exits 2)
and AC9 (real executor chain positive + negative smoke).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _install_skill_runtime_exec_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

    pin = _pinned_uv_version(source_root)
    _write_text(
        repo_root / "pyproject.toml",
        f'''[project]
name = "skill-runtime-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
    )

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations

class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    return None
""",
    )
    _write_text(
        # Issue #2311 AC1 fixture parity: bare `preflight.run` first-hops
        # into `workflow_start_entry.py` (a minimal fixture-local forwarder
        # to `run_refinement_preflight.py` below -- see that file) instead
        # of `run_refinement_preflight.py` directly. Sibling anchor-comment
        # entries (`.with_anchor` / `.with_human_context` /
        # `.with_agent_report`) below are unaffected and keep
        # `run_refinement_preflight.py` as their first hop.
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "workflow_start_entry.py",
        """from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    _inner = Path(__file__).resolve().parent / "run_refinement_preflight.py"
    _proc = subprocess.run([sys.executable, str(_inner), *sys.argv[1:]])
    raise SystemExit(_proc.returncode)
""",
    )
    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    "preflight.run.with_anchor": {
        "id": "preflight.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {
                "type": "github_issue_comment_url", "required": True,
            },
        },
    },
    "preflight.run.with_human_context": {
        "id": "preflight.run.with_human_context",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
            "--investigation-evidence-transport-path", "{investigation_evidence_transport_path}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {
                "type": "github_issue_comment_url", "required": True,
            },
            "investigation_evidence_transport_path": {
                "type": "path", "required": False, "optional_flag_pair": True,
            },
        },
    },
    "preflight.run.with_agent_report": {
        "id": "preflight.run.with_agent_report",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--agent-report-comment-url", "{anchor_comment_url}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {
                "type": "github_issue_comment_url", "required": True,
            },
        },
    },
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    # #2086 P0 fix_delta (Blocker 1/2): mirror production render_command()'s
    # optional_flag_pair mechanism -- an optional placeholder token whose
    # value was not supplied (and the flag literal token immediately before
    # it) is dropped entirely, instead of KeyError-ing.
    placeholders = REGISTRY[command_id].get("placeholders", {})
    argv = REGISTRY[command_id]["argv"]
    rendered: list[str] = []
    idx = 0
    while idx < len(argv):
        token = argv[idx]
        if token.startswith("{") and token.endswith("}"):
            name = token[1:-1]
            spec = placeholders.get(name, {})
            if name not in values and spec.get("optional_flag_pair"):
                if rendered and idx > 0 and not (argv[idx - 1].startswith("{")):
                    rendered.pop()
                idx += 1
                continue
            rendered.append(str(values[name]))
        else:
            rendered.append(token)
        idx += 1
    return rendered
""",
    )
    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--issue-number", required=True)
parser.add_argument("--repo", required=True)
parser.add_argument("--anchor-comment-url")
parser.add_argument("--human-context-comment-url", dest="human_context_comment_urls", action="append", default=[])
parser.add_argument("--agent-report-comment-url", dest="agent_report_comment_urls", action="append", default=[])
parser.add_argument("--consume-contract-patch-plan", action="store_true")
parser.add_argument("--investigation-evidence-transport-path", default=None)
args = parser.parse_args()
artifact = Path(".claude/artifacts/issue-refinement-loop") / args.issue_number
artifact.mkdir(parents=True, exist_ok=True)
payload = {
    "issue_number": args.issue_number,
    "repo": args.repo,
    "anchor_comment_url": args.anchor_comment_url,
    "human_context_comment_urls": args.human_context_comment_urls,
    "agent_report_comment_urls": args.agent_report_comment_urls,
}
(artifact / "preflight.json").write_text(json.dumps(payload))
print(json.dumps({"ok": True, **payload}))
""",
    )


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a production skill subtree into an isolated process fixture."""
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))


def _install_real_contract_update_fixture(repo_root: Path) -> Path:
    """Install the production wrapper and its direct consumers.

    Only GitHub and the controlled mutation executor are replaced.  The
    registry, policy, privileged executor, production preflight wrapper,
    planner, candidate readiness, review, and edit transaction helper all run
    as their production files in the temporary repository.
    """
    source_root = REPO_ROOT
    for skill in (
        "issue-refinement-loop",
        "edit-issue",
        "issue-contract-review",
        "review-issue",
    ):
        _copy_tree(
            source_root / ".claude" / "skills" / skill,
            repo_root / ".claude" / "skills" / skill,
        )
    _copy_tree(
        source_root / ".claude" / "skills" / "create-issue" / "scripts",
        repo_root / ".claude" / "skills" / "create-issue" / "scripts",
    )
    _copy_tree(source_root / ".github" / "ISSUE_TEMPLATE", repo_root / ".github" / "ISSUE_TEMPLATE")
    _copy_tree(source_root / "docs" / "dev", repo_root / "docs" / "dev")
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
    ):
        src = source_root / rel
        _write_text(repo_root / rel, src.read_text())

    for rel in ("pyproject.toml", "uv.lock"):
        _write_text(repo_root / rel, (source_root / rel).read_text())
    # Materialize the project environment before the executor snapshots the
    # fixture repository; the runtime command itself must not create an
    # unrelated `.venv/` write.
    subprocess.run(["uv", "sync", "--locked"], cwd=str(repo_root), check=True, capture_output=True, text=True)

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations

class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    return None
""",
    )

    # The transaction helper remains production code.  This fake is its
    # external controlled-executor boundary and mutates only the fixture's
    # artifact-root backed fake remote state.
    _write_text(
        repo_root / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py",
        """from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    artifact = Path(".claude/artifacts/issue-refinement-loop") / args.issue_number
    state_path = artifact / "fake_remote_issue.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["title"] = payload["new_title"]
    state["body"] = payload["new_body"]
    state["updatedAt"] = "2026-08-01T00:00:01Z"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (artifact / "controlled_transaction_request.json").write_text(
        json.dumps({"command_id": args.command_id, "payload": payload}), encoding="utf-8"
    )
    print(json.dumps({"new_body_sha256": payload["new_body_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )

    _write_text(
        next((repo_root / ".venv").glob("lib/python*/site-packages")) / "sitecustomize.py",
        """# Fixture-only external GitHub boundary for subprocess tests.
import json
import os
from pathlib import Path
import subprocess
import sys

_real_run = subprocess.run
_real_popen_init = subprocess.Popen.__init__


def _fake_gh(args, *positional, **kwargs):
    if not isinstance(args, (list, tuple)) or not args:
        return _real_run(args, *positional, **kwargs)
    executable = Path(str(args[0])).name
    if executable == "uv":
        child_env = dict(kwargs.get("env") or os.environ)
        child_env["PYTHONPATH"] = str(Path(__file__).parent)
        kwargs["env"] = child_env
        return _real_run(args, *positional, **kwargs)
    if executable != "gh":
        return _real_run(args, *positional, **kwargs)
    artifact = Path.cwd() / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    state = artifact / "fake_remote_issue.json"
    anchor = artifact / "fake_anchor.json"
    calls = artifact / "fake_gh_calls.jsonl"
    argv = [str(value) for value in args[1:]]
    calls.parent.mkdir(parents=True, exist_ok=True)
    with calls.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\\n")
    if argv[:2] == ["issue", "view"]:
        payload = json.loads(state.read_text(encoding="utf-8"))
    elif argv[:1] == ["api"] and argv[1].startswith(
        "repos/squne121/loop-protocol/issues/1498/comments?"
    ):
        payload = [[json.loads(anchor.read_text(encoding="utf-8"))]]
    elif argv[:2] == ["api", "repos/squne121/loop-protocol/issues/comments/1"]:
        payload = json.loads(anchor.read_text(encoding="utf-8"))
    else:
        return subprocess.CompletedProcess(args, 2, "", "unexpected fake gh argv")
    return subprocess.CompletedProcess(args, 0, json.dumps(payload) + "\\n", "")


def _fake_popen_init(self, args, *positional, **kwargs):
    # Issue #2075: skill_runtime_exec.py's outer-child supervisor launches
    # its child via subprocess.Popen (not subprocess.run). Mirror the same
    # PYTHONPATH propagation _fake_gh() applies to `uv` subprocess.run calls
    # so a nested `uv run python3 run_refinement_preflight.py` launched via
    # Popen still inherits this fixture's sitecustomize.py boundary.
    if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name == "uv":
        child_env = dict(kwargs.get("env") or os.environ)
        child_env["PYTHONPATH"] = str(Path(__file__).parent)
        kwargs["env"] = child_env
    return _real_popen_init(self, args, *positional, **kwargs)

subprocess.run = _fake_gh
subprocess.Popen.__init__ = _fake_popen_init
""",
    )
    return

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
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
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    "preflight.run.with_anchor": {
        "id": "preflight.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    "contract_update.run.with_anchor": {
        "id": "contract_update.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
            "--consume-contract-patch-plan",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_contract_update_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            "artifacts/{active_issue}/issue-metadata/",
        ],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    argv = REGISTRY[command_id]["argv"]
    rendered = []
    for token in argv:
        if token == "{issue_number}":
            rendered.append(str(values["issue_number"]))
        elif token == "{repo}":
            rendered.append(str(values["repo"]))
        elif token == "{anchor_comment_url}":
            rendered.append(str(values["anchor_comment_url"]))
        else:
            rendered.append(token)
    return rendered
""",
    )

    _write_text(
        repo_root / "scripts" / "agent-guards" / "fake_issue_edit_txn.py",
        """from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text())
    artifact_dir = Path(request["artifact_dir"])
    (artifact_dir / "transaction_invoked.json").write_text(json.dumps(request))
    metadata_dir = Path("artifacts") / request["issue_number"] / "issue-metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "transaction.json").write_text(json.dumps(request))
    (artifact_dir / "final_body.txt").write_text(request["candidate_body"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    parser.add_argument("--human-context-comment-url", required=False, default=None)
    parser.add_argument("--consume-contract-patch-plan", action="store_true")
    args = parser.parse_args()
    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue_number": args.issue_number,
        "repo": args.repo,
        "anchor_comment_url": args.anchor_comment_url,
    }
    if args.consume_contract_patch_plan:
        final_body = artifact_dir / "final_body.txt"
        if final_body.exists():
            handoff = {
                "status": "no_change", "writes": 0, "iterations": 0,
                "final_readback": "verified", "fresh_preflight": "pass",
                "fresh_review": "approve", "fresh_readiness": "go",
            }
        else:
            candidate_body = "## Outcome\\ntrusted desired state\\n"
            request = artifact_dir / "issue_edit_txn_input.json"
            request.write_text(json.dumps({
                "schema": "ISSUE_EDIT_TXN_INPUT_V1", "artifact_dir": str(artifact_dir),
                "issue_number": args.issue_number,
                "candidate_body": candidate_body,
            }))
            subprocess.run(
                [sys.executable, "scripts/agent-guards/fake_issue_edit_txn.py", str(request)],
                check=True, shell=False,
            )
            assert final_body.read_text() == candidate_body
            (artifact_dir / "fresh_check_input.json").write_text(json.dumps({"body": candidate_body}))
            handoff = {
                "status": "applied", "writes": 1, "iterations": 0,
                "final_readback": "verified", "fresh_preflight": "pass",
                "fresh_review": "approve", "fresh_readiness": "go",
            }
        payload["contract_update"] = handoff
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


_VALID_URL = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"


def _run_executor(
    repo: Path,
    command_id: str = "preflight.run.with_anchor",
    issue_number: str = "1498",
    repo_slug: str = "squne121/loop-protocol",
    anchor_comment_url: "str | None" = _VALID_URL,
    extra_args: "list[str] | None" = None,
    extra_env: "dict[str, str] | None" = None,
    use_fixture_runtime: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    if extra_env:
        env.update(extra_env)
    runtime = ["uv", "run", "--locked", "python3"] if use_fixture_runtime else [sys.executable]
    argv = [
        *runtime,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id",
        command_id,
        "--issue-number",
        issue_number,
        "--repo",
        repo_slug,
    ]
    if anchor_comment_url is not None:
        argv += ["--anchor-comment-url", anchor_comment_url]
    if extra_args:
        argv += extra_args
    return subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC4 / AC9: Matrix #2 reaches the real subprocess chain.
# ---------------------------------------------------------------------------


def test_executor_reaches_subprocess_and_rejects_anchor_on_preflight_run(tmp_path: Path) -> None:
    """AC4: Matrix #2 (valid anchor) reaches real subprocess execution;
    Matrix #4 (anchor on preflight.run) exits 2 without running a subprocess."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    # Matrix #2: positive — real subprocess chain executes end to end.
    positive = _run_executor(repo)
    assert positive.returncode == 0, positive.stderr
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    assert artifact.exists(), "expected preflight artifact to be created by the real subprocess"
    payload = json.loads(artifact.read_text())
    assert payload == {
        "issue_number": "1498",
        "repo": "squne121/loop-protocol",
        "anchor_comment_url": _VALID_URL,
        "human_context_comment_urls": [],
        "agent_report_comment_urls": [],
    }
    assert json.loads(positive.stdout)["anchor_comment_url"] == _VALID_URL

    # Matrix #4: negative — anchor flag rejected for preflight.run before any
    # subprocess is spawned.
    negative = _run_executor(repo, command_id="preflight.run", issue_number="1499")
    assert negative.returncode == 2, negative.stderr
    assert "anchor-comment-url" in negative.stderr
    no_artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1499" / "preflight.json"
    assert not no_artifact.exists()


def test_executor_real_subprocess_smoke_positive_and_negative(tmp_path: Path) -> None:
    """AC9: real executor chain positive and negative smoke via subprocess."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    positive = _run_executor(repo)
    assert positive.returncode == 0, positive.stderr

    # Negative: preflight.run.fixture must reject --anchor-comment-url too.
    fixture_negative = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run.fixture",
            "--issue-number",
            "1498",
            "--repo",
            "squne121/loop-protocol",
            "--fixture",
            "tmp/fixture.json",
            "--anchor-comment-url",
            _VALID_URL,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        check=False,
    )
    assert fixture_negative.returncode == 2, fixture_negative.stderr

    # Negative: missing anchor-comment-url for preflight.run.with_anchor.
    missing_anchor = _run_executor(repo, anchor_comment_url=None)
    assert missing_anchor.returncode == 2, missing_anchor.stderr
    assert "required" in missing_anchor.stderr


def test_executor_rejects_context_mismatched_anchor_url(tmp_path: Path) -> None:
    """Matrix #22: URL owner/repo/issue must bind to the CLI arguments."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    mismatched = _run_executor(
        repo,
        anchor_comment_url="https://github.com/other/repo/issues/1498#issuecomment-1",
    )
    assert mismatched.returncode == 2, mismatched.stderr


def test_executor_preflight_run_unaffected_without_anchor(tmp_path: Path) -> None:
    """AC1: preflight.run (no anchor) continues to work exactly as before."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, command_id="preflight.run", anchor_comment_url=None)
    assert result.returncode == 0, result.stderr
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    payload = json.loads(artifact.read_text())
    assert payload["anchor_comment_url"] is None


def test_contract_update_phase_reaches_fake_transaction_and_fresh_handoff(tmp_path: Path) -> None:
    """#1877 AC3/AC6/AC10: production process path and fake external boundary.

    This runs the real registry, policy, privileged executor,
    ``run_refinement_preflight.py``, planner, candidate readiness, review,
    and ``edit_issue_txn.py`` in a temporary git repository. Only GitHub
    and the controlled mutation executable are faked; no fixture replaces
    the phase wrapper or changes the executor's production PATH trust.
    """
    repo = _make_repo(tmp_path)
    _install_real_contract_update_fixture(repo)
    # Canonical repositories provision this approved transaction-local
    # workspace.  Create it before the executor's before-snapshot; the real
    # consumer removes its candidate/input files before the child returns.
    (repo / "tmp").mkdir()
    artifact_dir = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    first = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        use_fixture_runtime=True,
    )
    # The controlled write reached final readback, but the post-update
    # contract review detects the deliberately incomplete new AC.  That is a
    # terminal fail-closed result, never a successful implementation route.
    assert first.returncode == 2, first.stderr
    request = json.loads((artifact_dir / "controlled_transaction_request.json").read_text())
    assert request["command_id"] == "issue_content.update"
    assert request["payload"]["schema"] == "ISSUE_CONTENT_UPDATE_INPUT_V1"
    assert "AC2: trusted fixture directive" in json.loads(
        (artifact_dir / "fake_remote_issue.json").read_text()
    )["body"]
    calls = [json.loads(line) for line in (artifact_dir / "fake_gh_calls.jsonl").read_text().splitlines()]
    assert calls.count(["api", "repos/squne121/loop-protocol/issues/comments/1"]) >= 2
    assert sum(call[:2] == ["issue", "view"] for call in calls) >= 4
    result = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert result["contract_update"] == {
        "status": "failed",
        "disposition": "patch",
        "writes": 1,
        "iterations": 0,
        "final_readback": "verified",
        "fresh_preflight": "pass",
        # The synthetic directive intentionally introduces a new AC without
        # its matching verification-command marker.  The failed review must
        # block the phase rather than remain telemetry on a successful exit.
        "fresh_review": "needs_fix",
        "fresh_readiness": "go",
    }
    provenance = json.loads((artifact_dir / "refinement_preflight_provenance_v1.json").read_text())
    assert provenance["runtime_evidence"]["source"]["source_kind"] == "issue_comment"

    replay = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        use_fixture_runtime=True,
    )
    assert replay.returncode == 2, replay.stderr
    replay_result = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert replay_result["contract_update"]["status"] == "failed"
    assert replay_result["contract_update"]["writes"] == 0
    generic_replay = _run_executor(
        repo,
        command_id="contract_update.run.with_anchor",
        use_fixture_runtime=True,
    )
    assert generic_replay.returncode == 2, generic_replay.stderr
    generic_replay_result = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert generic_replay_result["contract_update"]["status"] == "failed"
    assert generic_replay_result["contract_update"]["writes"] == 0
    assert not (repo / "artifacts" / "1498" / "issue-metadata").exists() or len(
        list((repo / "artifacts" / "1498" / "issue-metadata").rglob("*.input.json"))
    ) == 1


def test_contract_update_phase_cannot_be_reached_through_preflight_command(tmp_path: Path) -> None:
    """The read-only preflight command never receives the consumer flag."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, command_id="preflight.run.with_anchor")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "contract_update" not in payload
    artifact_dir = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    assert not (artifact_dir / "transaction_invoked.json").exists()


def test_anchor_profiles_materialize_only_the_explicit_origin_lane(tmp_path: Path) -> None:
    """P0: executor preserves the profile-selected lane into the child argv."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    generic = _run_executor(repo, command_id="preflight.run.with_anchor")
    assert generic.returncode == 0, generic.stderr
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    generic_payload = json.loads(artifact.read_text())
    assert generic_payload["human_context_comment_urls"] == []
    assert generic_payload["agent_report_comment_urls"] == []

    human = _run_executor(repo, command_id="preflight.run.with_human_context")
    assert human.returncode == 0, human.stderr
    human_payload = json.loads(artifact.read_text())
    assert human_payload["human_context_comment_urls"] == [_VALID_URL]
    assert human_payload["agent_report_comment_urls"] == []

    agent = _run_executor(repo, command_id="preflight.run.with_agent_report")
    assert agent.returncode == 0, agent.stderr
    agent_payload = json.loads(artifact.read_text())
    assert agent_payload["human_context_comment_urls"] == []
    assert agent_payload["agent_report_comment_urls"] == [_VALID_URL]


# ---------------------------------------------------------------------------
# PR #2057 OWNER REQUEST_CHANGES (P0-3): true process-boundary E2E for the
# #2048 approved-scope-reframe / empty-operations `full_rewrite_required`
# disposition, reusing `_install_real_contract_update_fixture()` (the SAME
# harness `test_contract_update_phase_reaches_fake_transaction_and_fresh_
# handoff` above already established for PR #1884/#1877). This exercises:
#
#   registry (command_registry.REGISTRY["contract_update.run.with_anchor"])
#   -> skill_runtime_command_policy.py (privileged command parsing)
#   -> skill_runtime_exec.py (real subprocess boundary)
#   -> run_refinement_preflight.py (REAL production wrapper, not a fixture
#      stub -- run_preflight() -> consume_trusted_anchor_contract_patch_plan()
#      -> scope_signal_delta.run_trusted_anchor_iteration_zero() ->
#      decide_rewrite_route.classify_scope_reframe_disposition())
#   -> fake `gh` (subprocess.run monkeypatch; only GitHub I/O is faked)
#   -> refinement_preflight_result_v1.json artifact + NEXT_ACTION stdout
#
# unlike the in-process dynamic-import tests in
# test_preflight_run_with_anchor.py, this crosses a real subprocess boundary
# (skill_runtime_exec.py spawns `uv run --locked python3 run_refinement_
# preflight.py` as a genuinely separate process).
# ---------------------------------------------------------------------------


def test_contract_update_phase_full_rewrite_required_reaches_next_action_via_real_subprocess(
    tmp_path: Path,
) -> None:
    """AC1/AC6/P0-3: an approved trusted-anchor ANCHOR_SCOPE_REFRAME_V1 scope
    reframe (not yet reflected in the Issue's Allowed Paths section) reaches
    `contract_update.status == "handoff_required"` /
    `disposition == "full_rewrite_required"` through the REAL registry ->
    policy -> privileged executor subprocess -> production
    `run_refinement_preflight.py` -> production `scope_signal_delta.py`
    classifier chain, with ZERO writes and ZERO mutation-executor
    invocations (full-rewrite handoff, not a mutation attempt)."""
    repo = _make_repo(tmp_path)
    _install_real_contract_update_fixture(repo)
    (repo / "tmp").mkdir()
    artifact_dir = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor_body = (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        "target:\n  repo: squne121/loop-protocol\n  issue_number: 1498\n"
        "decision: approve_scope_delta\n"
        'allowed_path_deltas: ["docs/product/features/scope-only-reframe.md"]\n'
        'rationale: "process-boundary E2E fixture (PR #2057 OWNER review P0-3)"\n'
        'required_rerun: ["contract_review"]\n'
        "```\n"
    )
    anchor = {
        "id": 1,
        "body": anchor_body,
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-09T00:00:00Z",
        "updated_at": "2026-08-09T00:00:00Z",
    }
    pre_body = (
        "## Machine-Readable Contract\n\n"
        "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n"
        "parent_issue: none\ngoal_ref: test\nchange_kind: workflow\n```\n\n"
        "## Parent Issue\n\nnone\n\n## Parent Goal Ref\n\ntest\n\n"
        "## Current Validated Scope\n\n- test\n\n## Remaining Parent Gaps\n\nnone\n\n"
        "## Outcome\n\ntest\n\n## In Scope\n\n- test\n\n## Out of Scope\n\n- none\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: test\n\n"
        "## Verification Commands\n\n```bash\n$ true\n```\n\n"
        "## Allowed Paths\n\n- docs/product/features/existing.md\n\n"
        "## Stop Conditions\n\n- none\n\n## Required Skills\n\n- none\n"
    )
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-09T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    result = _run_executor(
        repo,
        command_id="contract_update.run.with_anchor",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
    )

    assert "NEXT_ACTION: issue_editor_required" in result.stdout, result.stdout + result.stderr
    payload = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert payload["next_action"] == "issue_editor_required"
    assert payload["contract_update"]["status"] == "handoff_required"
    assert payload["contract_update"]["disposition"] == "full_rewrite_required"
    assert payload["contract_update"]["writes"] == 0
    assert payload["contract_update"]["final_readback"] == "not_applicable"
    # No mutation executor invocation of any kind: the write-path helper
    # (controlled_skill_mutation_exec.py, this fixture's fake external
    # controlled-executor boundary) is never reached for a disposition that
    # attempted zero writes.
    assert not (artifact_dir / "controlled_transaction_request.json").exists()

    # Replaying the identical transition through a second real subprocess
    # invocation is still side-effect free and deterministic (regression #2
    # / #8 "side-effect 0" + "restart replay").
    replay = _run_executor(
        repo,
        command_id="contract_update.run.with_anchor",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
    )
    assert "NEXT_ACTION: issue_editor_required" in replay.stdout, replay.stdout + replay.stderr
    replay_payload = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert replay_payload["contract_update"]["status"] == "handoff_required"
    assert replay_payload["contract_update"]["writes"] == 0
    assert not (artifact_dir / "controlled_transaction_request.json").exists()
