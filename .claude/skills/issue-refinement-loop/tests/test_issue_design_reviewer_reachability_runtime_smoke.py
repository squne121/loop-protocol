"""Issue #2963 AC8: 実 `issue-design-reviewer` SubAgent の runtime smoke（`claude_live`）。

3 fixture（negative / positive control / simple docs-only）それぞれの pinned bundle に対して、実 reviewer を
fresh Claude Code session から delegation し、stream-json と raw result を pure evaluator
（`issue2963_reachability_runtime_evaluator.py`、hermetic test は
`test_issue_design_reviewer_reachability_evaluator.py`）の固定規則だけで判定する。

- repository root は test file の位置から `git -C <test file の directory> rev-parse --show-toplevel` で導出する
  （process cwd に依存しない）。その root を `run_structured_claude` の worktree（cwd）として渡す。
- reviewer へは pin した bundle の `invocation_dir`（repository 内の git-ignored artifact directory）だけを渡し、
  root と HEAD は reviewer 自身が `git -C` で導出する。
- 各 fixture の live 実行は 1 回。terminal completion に至らない spawn 失敗（stream-json が得られない）に限り
  同条件で 1 回だけ再実行する。verdict が規則に合わないことを理由とする再試行はしない。
- 結果 JSON は `artifacts/runtime-verification-2963-<head8>.json`（git-ignored、commit しない）。
- claude 実行ファイル / 認証が利用不能、または permission 拒否は exit 77（SKIP、PASS ではない）。

既存 helper（`run_structured_claude` / `extract_claude_hook_lifecycle_events` /
`extract_claude_permission_denials`）は変更せず、unique module 名で読み込んで再利用する。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

_THIS_FILE = Path(__file__).resolve()
_TESTS_DIR = _THIS_FILE.parent
_REFERENCE_RELATIVE = ".claude/skills/issue-refinement-loop/references/semantic-design-review.md"
_TRANSPORT_RELATIVE = ".claude/skills/issue-refinement-loop/scripts/semantic_review_transport.py"
_FIXTURES_RELATIVE = ".claude/skills/issue-refinement-loop/tests/fixtures"

_EVALUATOR_NAME = "issue2963_reachability_runtime_evaluator"
_TRANSPORT_NAME = "issue2963_semantic_review_transport_for_smoke"

_FIXTURES = {
    "negative": ("consumer_reachability_negative_case", "negative_case"),
    "positive": ("consumer_reachability_positive_control_case", "positive_control_case"),
    "simple": ("consumer_reachability_simple_docs_only_case", "simple_docs_only_case"),
}

_UNAVAILABLE_MARKERS = (
    "Please run /login",
    "Not authenticated",
    "invalid_grant",
    "command not found",
    "unrecognized_model",
    "WebSocket upgrade was rejected",
)
_ISSUE_NUMBER = 2963
_LIVE_COMMAND = (
    "uv run --locked pytest "
    ".claude/skills/issue-refinement-loop/tests/"
    "test_issue_design_reviewer_reachability_runtime_smoke.py -q -m claude_live"
)


def _load(name: str, path: Path) -> Any:
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


EVAL = _load(_EVALUATOR_NAME, _TESTS_DIR / "issue2963_reachability_runtime_evaluator.py")


def _git(directory: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=30, check=False)


def _resolve_repo_root() -> Path:
    """test file の位置から repository root を明示導出する（process cwd に依存しない）。"""
    completed = _git(_TESTS_DIR, "rev-parse", "--show-toplevel")
    assert completed.returncode == 0, "cannot resolve the repository root from the test file location"
    return Path(completed.stdout.strip())


def _resolve_head(root: Path) -> str:
    completed = _git(root, "rev-parse", "HEAD")
    assert completed.returncode == 0, "runtime evidence requires resolving the current git HEAD"
    head = completed.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", head), "runtime evidence requires a lowercase full git HEAD"
    return head


def _fixture_paths(kind: str) -> dict[str, str]:
    directory, prefix = _FIXTURES[kind]
    if kind == "simple":
        return {}
    return {role: f"{_FIXTURES_RELATIVE}/{directory}/{prefix}_{role}.py" for role in ("producer", "parser", "consumer")}


def _fixture_body(root: Path, kind: str) -> str:
    directory, prefix = _FIXTURES[kind]
    path = root / _FIXTURES_RELATIVE / directory / f"issue_body_{prefix}.md"
    return path.read_text(encoding="utf-8")


def launch_prompt_template(root: Path) -> str:
    """committed な reference の起動 prompt（blockquote）。live 実行は常にこの文言を使う。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in (root / _REFERENCE_RELATIVE).read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            current.append(line.lstrip("> ").rstrip())
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    prompts = ["\n".join(block) for block in blocks if any("bundle.json" in line for line in block)]
    assert len(prompts) == 1, "reference must contain exactly one launch prompt block"
    return prompts[0]


def main_session_prompt(template: str, invocation_dir: str) -> str:
    task = template.replace("<invocation_dir>", invocation_dir)
    return (
        "You are running inside an automated runtime smoke test. Use the Agent tool exactly once with "
        'subagent_type "issue-design-reviewer". Give the SubAgent exactly the following task prompt, '
        "with no additions or omissions:\n\n<<<TASK_PROMPT\n"
        f"{task}\nTASK_PROMPT>>>\n\n"
        "Wait for the SubAgent to finish, then reply with the SubAgent's result verbatim."
    )


def pin_fixture_bundle(root: Path, kind: str, bundles_root: Path) -> dict[str, Any]:
    transport = _load(_TRANSPORT_NAME, root / _TRANSPORT_RELATIVE)
    return transport.pin_bundle(
        issue_number=_ISSUE_NUMBER,
        body_text=_fixture_body(root, kind),
        prompt_version="v1",
        requested_model="sonnet",
        artifacts_root=bundles_root / kind,
    )


def redact(value: Any, root: Path) -> Any:
    """artifact へ書く前に、絶対 root を `<root>` へ置換する（HOME を含む絶対 path を残さない）。"""
    if isinstance(value, str):
        return value.replace(str(root), "<root>")
    if isinstance(value, list):
        return [redact(item, root) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, root) for key, item in value.items()}
    return value


def _tracked_status(root: Path) -> str:
    completed = _git(root, "status", "--porcelain")
    assert completed.returncode == 0
    return completed.stdout


def _root_identity(root: Path, invocation_dir: Path) -> dict[str, Any]:
    git_dir = _git(root, "rev-parse", "--git-dir").stdout.strip()
    common = _git(root, "rev-parse", "--git-common-dir").stdout.strip()
    inv_common = _git(invocation_dir, "rev-parse", "--git-common-dir").stdout.strip()

    def real(base: Path, rel: str) -> str:
        return str((base / rel).resolve()) if rel else ""

    return {
        "linked_worktree": real(root, git_dir) != real(root, common),
        "invocation_dir_git_common_dir_matches_root": real(invocation_dir, inv_common) == real(root, common),
    }


def _write_artifact(root: Path, tested_head: str, payload: dict[str, Any]) -> Path:
    directory = root / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"runtime-verification-2963-{tested_head[:8]}.json"
    path.write_text(json.dumps(redact(payload, root), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _exit_runtime_skip(*, root: Path, tested_head: str, reason: str, results: dict[str, Any]) -> NoReturn:
    artifact = _write_artifact(
        root,
        tested_head,
        {
            "tested_head": tested_head,
            "overall": "unavailable",
            "reason": reason,
            "fixtures": results,
            "command": _LIVE_COMMAND,
        },
    )
    print(f"SKIP (unavailable, NOT a PASS): {reason}; evidence: {artifact.relative_to(root)}")
    pytest.exit("runtime verification unavailable", returncode=77)


# ---------------------------------------------------------------------------
# offline（default selection）: smoke の組み立てが committed 文言と bundle に束縛されていること
# ---------------------------------------------------------------------------


def test_main_session_prompt_embeds_committed_launch_prompt_and_invocation_dir() -> None:
    root = _resolve_repo_root()
    template = launch_prompt_template(root)
    prompt = main_session_prompt(template, "/x/inv")
    assert "/x/inv/bundle.json" in prompt and "<invocation_dir>" not in prompt
    assert "git -C /x/inv rev-parse --show-toplevel" in prompt
    assert 'subagent_type "issue-design-reviewer"' in prompt


@pytest.mark.parametrize("kind", sorted(_FIXTURES))
def test_pin_fixture_bundle_binds_body_sha256_and_body_file(kind: str, tmp_path: Path) -> None:
    root = _resolve_repo_root()
    bundle = pin_fixture_bundle(root, kind, tmp_path)
    inv = Path(bundle["invocation_dir"])
    stored = json.loads((inv / "bundle.json").read_text(encoding="utf-8"))
    body = (inv / stored["body_file"]).read_text(encoding="utf-8")
    assert body == _fixture_body(root, kind)
    assert stored["body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_artifact_redacts_the_absolute_root_and_writes_nothing_tracked(tmp_path: Path) -> None:
    root = tmp_path / "home" / "user" / "repo"
    root.mkdir(parents=True)
    path = _write_artifact(root, "c" * 40, {"evidence_refs": [f"{root}/a/b.py:3"], "nested": {"p": str(root)}})
    text = path.read_text(encoding="utf-8")
    assert path.name == "runtime-verification-2963-cccccccc.json"
    assert str(root) not in text and "<root>/a/b.py:3" in text


# ---------------------------------------------------------------------------
# live（claude_live）: 3 fixture 各 1 試行
# ---------------------------------------------------------------------------


@pytest.mark.claude_live
def test_real_issue_design_reviewer_reachability_three_fixture_runtime_smoke() -> None:
    runner = EVAL.load_runner_module()
    root = _resolve_repo_root()
    tested_head = _resolve_head(root)
    assert _tracked_status(root) == "", (
        "AC8 runtime smoke requires a clean committed worktree (acceptance is on committed HEAD)"
    )

    claude_bin, skip_reason = runner.preflight_claude_available()
    if claude_bin is None:
        _exit_runtime_skip(root=root, tested_head=tested_head, reason=skip_reason or "claude unavailable", results={})
    version = runner.capture_runtime_version(claude_bin)

    template = launch_prompt_template(root)
    bundles_root = root / "artifacts" / "runtime-2963-bundles"
    results: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    permission_modes: dict[str, Any] = {}

    for kind in ("negative", "positive", "simple"):
        bundle = pin_fixture_bundle(root, kind, bundles_root)
        invocation_dir = Path(bundle["invocation_dir"])
        identity = _root_identity(root, invocation_dir)
        prompt = main_session_prompt(template, str(invocation_dir))
        attempts = 0
        while True:
            attempts += 1
            returncode, stdout, stderr, timed_out = runner.run_structured_claude(
                str(root), prompt, 900.0, 24, claude_bin=claude_bin
            )
            has_stream = bool(EVAL.iter_stream_events(stdout))
            if has_stream or attempts >= 2:  # spawn 失敗（stream なし）に限り同条件で 1 回だけ再実行
                break
        combined = f"{stdout or ''}\n{stderr or ''}"
        marker = next((m for m in _UNAVAILABLE_MARKERS if m in combined), None)
        unavailable = (
            f"claude runtime unavailable ({marker})"
            if marker is not None and returncode != 0
            else ("timed out before terminal completion" if timed_out and not has_stream else None)
        )
        raw = EVAL.extract_reviewer_raw_result(stdout)
        outcome = EVAL.evaluate_reachability_runtime(
            stdout=stdout,
            tested_head=tested_head,
            resolved_root=str(root),
            invocation_dir=str(invocation_dir),
            fixture_kind=kind,
            fixture_paths=_fixture_paths(kind) or None,
            raw_result=raw,
            claude_unavailable_reason=unavailable,
        )
        permission_modes[kind] = runner.extract_claude_subagentstop_permission_mode(stdout)
        results[kind] = {
            "bundle_invocation_dir": str(invocation_dir.relative_to(root)),
            "body_sha256": bundle["body_sha256"],
            "attempts": attempts,
            "claude_exit_code": returncode,
            "verdict": outcome["verdict"],
            "rule": outcome["rule"],
            "reason": outcome["reason"],
            "evidence": outcome["evidence"],
            "raw_result": raw,
        }

    verdicts = {kind: item["verdict"] for kind, item in results.items()}
    overall = (
        "fail" if "fail" in verdicts.values() else ("unavailable" if "unavailable" in verdicts.values() else "pass")
    )
    payload = {
        "tested_head": tested_head,
        "claude_code_version": version,
        "permission_mode_frontmatter": "dontAsk",
        "permission_mode_observed": permission_modes,
        "root_identity": identity,
        "overall": overall,
        "fixtures": results,
        "command": _LIVE_COMMAND,
    }
    artifact = _write_artifact(root, tested_head, payload)
    print(f"runtime evidence: {artifact.relative_to(root)} overall={overall} verdicts={verdicts}")
    if overall == "unavailable":
        pytest.exit("runtime verification unavailable (SKIP, not PASS)", returncode=77)
    assert overall == "pass", f"AC8 runtime verdicts: {verdicts}; see {artifact.relative_to(root)}"
