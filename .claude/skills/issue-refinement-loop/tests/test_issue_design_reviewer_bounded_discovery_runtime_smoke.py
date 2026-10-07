"""Issue #2973 AC8: 実 `issue-design-reviewer` SubAgent の bounded discovery runtime smoke（`claude_live`）。

4 fixture（negative / positive control / hybrid / simple docs-only control）それぞれの pinned bundle に対して、
実 reviewer を fresh Claude Code session から delegation し、stream-json と raw result を pure evaluator
（`issue2973_bounded_discovery_runtime_evaluator.py`、hermetic test は
`test_issue_design_reviewer_bounded_discovery_evaluator.py`）の固定規則だけで判定する。

#2963 の runtime smoke（変更しない）と同じ harness 方式で、並列の runner / parser は新設しない。

- repository root は test file の位置から `git -C <test file の directory> rev-parse --show-toplevel` で導出する。
  その root を `run_structured_claude` の worktree（cwd）として渡す。
- reviewer へは pin した bundle の `invocation_dir`（repository 内の git-ignored artifact directory）だけを渡し、
  root と HEAD は reviewer 自身が `git -C` で導出する。
- bounded discovery は #2963 より多くの tool 呼び出しを要するため、この smoke だけ turn / timeout の予算を引き上げる
  （`_MAX_TURNS` / `_TIMEOUT_SECONDS`）。turn 上限・timeout で terminal completion に至らなかった実行は PASS にしない
  （evaluator が規則 3 の FAIL とする）。
- 各 fixture の live 実行は 1 回。terminal completion に至らない spawn 失敗（stream-json が得られない）に限り
  同条件で 1 回だけ再実行する。verdict が規則に合わないことを理由とする再試行はしない。
- 結果 JSON は `artifacts/runtime-verification-2973-<head8>.json`（git-ignored、commit しない）。
- claude 実行ファイル / 認証が利用不能、permission 拒否、または session の tool pool（`system init` の `tools`）に
  supported discovery lane（Bash、または専用 Grep / Glob）が一つも存在しない場合は exit 77（SKIP、PASS ではない）。
  Claude Code の native build は専用 Grep / Glob を既定の tool pool から外す（discovery は Bash の find / grep）が、
  専用 Grep / Glob が無いことだけでは unavailable にしない（reviewer は Bash lane で続行し、その結果で判定する）。
  harness 側で `--tools` を足して tool を後付けすることはしない（production-default runtime を検証するため）。
- artifact には fixture ごとに、session init の tools と、reviewer 区間の各 tool_use が dedicated lane の discovery /
  Bash lane の discovery / 非 discovery のどれに認定されたか（認定した規則つき）を構造的に残す。
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
_ISSUE_NUMBER = 2973
_TRANSPORT_NAME = "issue2973_semantic_review_transport_for_smoke"

# #2963 の smoke は 900 秒 / 24 turn。bounded discovery（Grep / Glob + 最大 8 件の Read）を含む reviewer 区間は
# それより重いため、この smoke だけ予算を引き上げる（#2963 の smoke は変更しない）。
_TIMEOUT_SECONDS = 1500.0
_MAX_TURNS = 48

_UNAVAILABLE_MARKERS = (
    "Please run /login",
    "Not authenticated",
    "invalid_grant",
    "command not found",
    "unrecognized_model",
    "WebSocket upgrade was rejected",
)
_FIXTURE_KINDS = ("negative", "positive", "hybrid", "simple")
_LIVE_COMMAND = (
    "uv run --locked pytest "
    ".claude/skills/issue-refinement-loop/tests/"
    "test_issue_design_reviewer_bounded_discovery_runtime_smoke.py -q -m claude_live"
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


EVAL = _load(
    "issue2973_bounded_discovery_runtime_evaluator", _TESTS_DIR / "issue2973_bounded_discovery_runtime_evaluator.py"
)


def _git(directory: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=30, check=False)


def _resolve_repo_root() -> Path:
    completed = _git(_TESTS_DIR, "rev-parse", "--show-toplevel")
    assert completed.returncode == 0, "cannot resolve the repository root from the test file location"
    return Path(completed.stdout.strip())


def _resolve_head(root: Path) -> str:
    completed = _git(root, "rev-parse", "HEAD")
    assert completed.returncode == 0, "runtime evidence requires resolving the current git HEAD"
    head = completed.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", head), "runtime evidence requires a lowercase full git HEAD"
    return head


def _fixture_body(root: Path, kind: str) -> str:
    directory = EVAL.FIXTURE_DIRS[kind][0]
    path = root / EVAL.FIXTURES_RELATIVE / directory / f"issue_body_{directory}.md"
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


def classify_unavailable(
    *, stdout: str, stderr: str, returncode: int | None, timed_out: bool, has_stream: bool
) -> str | None:
    """claude runtime が利用不能（SKIP）かどうか。判定規則に合わない verdict（FAIL）とは混同しない。

    利用不能は「認証 / 実行ファイル由来の marker かつ非 0 終了」または「timeout で stream が全く得られない」だけ。
    stream が得られた上での timeout / turn 上限は terminal completion 未到達（evaluator の規則 3 の FAIL）である。"""
    combined = f"{stdout or ''}\n{stderr or ''}"
    marker = next((m for m in _UNAVAILABLE_MARKERS if m in combined), None)
    if marker is not None and returncode != 0:
        return f"claude runtime unavailable ({marker})"
    if timed_out and not has_stream:
        return "timed out before any stream-json was captured"
    return None


def overall_verdict(verdicts: dict[str, str]) -> str:
    """fail が 1 件でもあれば fail、次に unavailable、全て pass の場合だけ pass（unavailable を pass に昇格しない）。"""
    values = list(verdicts.values())
    if not values or set(values) - {"pass", "fail", "unavailable"}:
        return "fail"
    if "fail" in values:
        return "fail"
    if "unavailable" in values:
        return "unavailable"
    return "pass"


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
    path = directory / f"runtime-verification-{_ISSUE_NUMBER}-{tested_head[:8]}.json"
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
# offline（default selection）: smoke の組み立てが committed 文言・bundle・予算・分類規則に束縛されていること
# ---------------------------------------------------------------------------


def test_main_session_prompt_embeds_committed_launch_prompt_with_discovery_clause() -> None:
    root = _resolve_repo_root()
    template = launch_prompt_template(root)
    prompt = main_session_prompt(template, "/x/inv")
    assert "/x/inv/bundle.json" in prompt and "<invocation_dir>" not in prompt
    assert "git -C /x/inv rev-parse --show-toplevel" in prompt
    assert 'subagent_type "issue-design-reviewer"' in prompt
    assert "DISCOVERY_SEARCH_CALL_MAX: 8" in prompt and "DISCOVERY_SOURCE_READ_MAX: 8" in prompt
    flat = prompt.replace("\n", "")
    assert "named symbol を query にした bounded discovery" in flat
    # 2 lane（専用 Grep / Glob と root 束縛の Bash find / grep）と Bash allowlist、`-l` の推奨が launch prompt に載る。
    squashed = re.sub(r"\s+", "", prompt)  # 折り返し位置に依存させない
    for needle in (
        "Bash の `find` / `grep` で discovery する",
        "eligible な find / grep の 3 種類だけである",
        "はすべて契約違反である",
        "`-l` と `--include=` / `--exclude-dir=` を併用することを推奨する",
    ):
        assert re.sub(r"\s+", "", needle) in squashed, needle
    assert "--tools" not in prompt


@pytest.mark.parametrize("kind", _FIXTURE_KINDS)
def test_pin_fixture_bundle_binds_body_sha256_and_body_file(kind: str, tmp_path: Path) -> None:
    root = _resolve_repo_root()
    bundle = pin_fixture_bundle(root, kind, tmp_path)
    inv = Path(bundle["invocation_dir"])
    stored = json.loads((inv / "bundle.json").read_text(encoding="utf-8"))
    body = (inv / stored["body_file"]).read_text(encoding="utf-8")
    assert body == _fixture_body(root, kind)
    assert stored["body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert stored["body_file"] == "body.md"


def test_smoke_budget_is_raised_only_for_this_smoke_and_is_not_a_pass_criterion() -> None:
    # #2963 の smoke は 900 秒 / 24 turn（変更しない）。本 smoke は discovery の分だけ引き上げる。
    assert _TIMEOUT_SECONDS > 900.0 and _MAX_TURNS > 24
    old = (_TESTS_DIR / "test_issue_design_reviewer_reachability_runtime_smoke.py").read_text(encoding="utf-8")
    assert "900.0, 24" in old, "the #2963 smoke budget must stay untouched"


def test_unavailable_classification_never_absorbs_a_genuine_fail_or_incomplete_run() -> None:
    kw: dict[str, Any] = {"stdout": "", "stderr": "", "returncode": 0, "timed_out": False, "has_stream": True}
    assert classify_unavailable(**kw) is None
    assert classify_unavailable(**{**kw, "stderr": "Please run /login", "returncode": 1}) is not None
    assert classify_unavailable(**{**kw, "stderr": "Please run /login", "returncode": 0}) is None
    assert classify_unavailable(**{**kw, "timed_out": True, "has_stream": False}) is not None
    # stream が得られた上での timeout は unavailable ではない（terminal completion 未到達の FAIL）。
    assert classify_unavailable(**{**kw, "timed_out": True, "has_stream": True}) is None


def test_overall_verdict_never_promotes_unavailable_or_empty_to_pass() -> None:
    assert overall_verdict({"a": "pass", "b": "pass"}) == "pass"
    assert overall_verdict({"a": "pass", "b": "unavailable"}) == "unavailable"
    assert overall_verdict({"a": "unavailable", "b": "fail"}) == "fail"
    assert overall_verdict({}) == "fail"
    assert overall_verdict({"a": "skip"}) == "fail"


def test_incomplete_terminal_run_is_never_a_pass() -> None:
    complete = (
        json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps({"type": "result", "subtype": "success"})
    )
    assert EVAL.terminal_incomplete_reason(complete, timed_out=False) is None
    assert EVAL.terminal_incomplete_reason(complete, timed_out=True) is not None
    turn_limit = complete.replace('"success"', '"error_max_turns"')
    assert EVAL.terminal_incomplete_reason(turn_limit, timed_out=False) is not None


def test_artifact_redacts_the_absolute_root_and_writes_nothing_tracked(tmp_path: Path) -> None:
    root = tmp_path / "home" / "user" / "repo"
    root.mkdir(parents=True)
    path = _write_artifact(root, "c" * 40, {"evidence_refs": [f"{root}/a/b.py:3"], "nested": {"p": str(root)}})
    text = path.read_text(encoding="utf-8")
    assert path.name == "runtime-verification-2973-cccccccc.json"
    assert str(root) not in text and "<root>/a/b.py:3" in text


# ---------------------------------------------------------------------------
# live（claude_live）: 4 fixture 各 1 試行
# ---------------------------------------------------------------------------


@pytest.mark.claude_live
def test_real_issue_design_reviewer_bounded_discovery_four_fixture_runtime_smoke() -> None:
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
    bundles_root = root / "artifacts" / "runtime-2973-bundles"
    results: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    permission_modes: dict[str, Any] = {}

    for kind in _FIXTURE_KINDS:
        bundle = pin_fixture_bundle(root, kind, bundles_root)
        invocation_dir = Path(bundle["invocation_dir"])
        identity = _root_identity(root, invocation_dir)
        prompt = main_session_prompt(template, str(invocation_dir))
        attempts = 0
        while True:
            attempts += 1
            returncode, stdout, stderr, timed_out = runner.run_structured_claude(
                str(root), prompt, _TIMEOUT_SECONDS, _MAX_TURNS, claude_bin=claude_bin
            )
            has_stream = bool(EVAL.iter_stream_events(stdout))
            if has_stream or attempts >= 2:  # spawn 失敗（stream なし）に限り同条件で 1 回だけ再実行
                break
        unavailable = classify_unavailable(
            stdout=stdout, stderr=stderr, returncode=returncode, timed_out=timed_out, has_stream=has_stream
        )
        raw = EVAL.extract_reviewer_raw_result(stdout)
        incomplete = EVAL.terminal_incomplete_reason(stdout, timed_out)
        outcome = EVAL.evaluate_bounded_discovery_runtime(
            stdout=stdout,
            tested_head=tested_head,
            resolved_root=str(root),
            invocation_dir=str(invocation_dir),
            fixture_kind=kind,
            fixture_roles=EVAL.fixture_roles(kind) or None,
            allowed_read_paths=EVAL.fixture_allowed_read_paths(kind),
            raw_result=raw,
            claude_unavailable_reason=unavailable,
            terminal_incomplete=incomplete,
        )
        permission_modes[kind] = runner.extract_claude_subagentstop_permission_mode(stdout)
        classification = [
            record["classification"] for record in (outcome.get("tool_use_records") or []) if "classification" in record
        ]
        results[kind] = {
            "bundle_invocation_dir": str(invocation_dir.relative_to(root)),
            "body_sha256": bundle["body_sha256"],
            "attempts": attempts,
            "claude_exit_code": returncode,
            "timed_out": timed_out,
            "terminal_incomplete": incomplete,
            "session_tools": outcome.get("session_tools"),
            "reviewer_tool_use_classification": classification,
            "verdict": outcome["verdict"],
            "rule": outcome["rule"],
            "reason": outcome["reason"],
            "evidence": outcome["evidence"],
            # 将来の FAIL を診断できるよう sanitized な lifecycle / tool_use 要約（Grep / Glob / Read を含む）を残す。
            "lifecycle_records": outcome.get("lifecycle_records"),
            "tool_use_records": outcome.get("tool_use_records"),
            "raw_result": raw,
        }

    verdicts = {kind: item["verdict"] for kind, item in results.items()}
    overall = overall_verdict(verdicts)
    payload = {
        "tested_head": tested_head,
        "claude_code_version": version,
        "permission_mode_frontmatter": "dontAsk",
        "permission_mode_observed": permission_modes,
        "root_identity": identity,
        "bounds": {
            "DISCOVERY_SEARCH_CALL_MAX": EVAL.DISCOVERY_SEARCH_CALL_MAX,
            "DISCOVERY_SOURCE_READ_MAX": EVAL.DISCOVERY_SOURCE_READ_MAX,
            "SEARCH_SCOPE": EVAL.SEARCH_SCOPE,
        },
        "budget": {"timeout_seconds": _TIMEOUT_SECONDS, "max_turns": _MAX_TURNS},
        "overall": overall,
        "fixtures": results,
        "command": _LIVE_COMMAND,
    }
    artifact = _write_artifact(root, tested_head, payload)
    print(f"runtime evidence: {artifact.relative_to(root)} overall={overall} verdicts={verdicts}")
    if overall == "unavailable":
        pytest.exit("runtime verification unavailable (SKIP, not PASS)", returncode=77)
    assert overall == "pass", f"AC8 runtime verdicts: {verdicts}; see {artifact.relative_to(root)}"
