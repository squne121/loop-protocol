"""scripts/agent-ops/tests/_claude_gpt_role_subagent_smoke.py

Issue #2925 AC4: Minimal Claude-GPT で、role alias 経由の 2 種類の SubAgent
(`model: haiku` の `codebase-investigator`、`model: sonnet` の `issue-design-reviewer`) が
spawn -> terminal completion -> parent hand-back まで実 runtime で完了することを判定する
helper。pytest は先頭 underscore のため collect しない。

判定は stream-json の構造化 event だけで行う（自己申告 text だけでは PASS にしない）:
  - 対象 `agent_type` の SubagentStart と、同じ agent_id の SubagentStop（Start より後）
  - parent の Agent tool_use（`subagent_type` 一致）に対応する tool_result が存在し is_error でないこと
  - hand-back text: Claude Code 2.1.289 以降の stream-json では Agent tool_result は「report は
    SubagentHandback で届いた」という定型文だけを持つため、SubAgent 側の `SubagentHandback` tool_use
    （`parent_tool_use_id` が当該 Agent tool_use）の input.message と、その成功 tool_result から
    hand-back を取る。harness が report を Agent tool_result へ直接載せる場合はそちらも許容する
  - hand-back が空でなく、`INSUFFICIENT_CONTEXT` 等の context 不足による停止を含まず、
    要求した調査結果（validator）を含むこと
dispatch-only / fixture-only / mock-only は PASS にしない。

`evaluate_role_subagent` は pure で、実 Claude Code process を起動しない（hermetic に unit-test
できる）。live 実行は `run_role_subagent_live` が `run_structured_claude`（claude-gpt adapter）
経由で行う。
"""

from __future__ import annotations

import json
import re
from typing import Callable

CONTEXT_STARVED_MARKERS = ("INSUFFICIENT_CONTEXT",)

HAIKU_AGENT = "codebase-investigator"
SONNET_AGENT = "issue-design-reviewer"


def _iter_events(stdout: str):
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _tool_result_text(block: dict) -> str:
    body = block.get("content")
    if isinstance(body, list):
        return "\n".join(str(item.get("text", "")) for item in body if isinstance(item, dict))
    return str(body or "")


HANDBACK_DELIVERY_NOTICE = "SubagentHandback call"


def _handback_result_succeeded(result: tuple[str, bool] | None) -> bool:
    if result is None or result[1]:
        return False
    try:
        parsed = json.loads(result[0])
    except ValueError:
        return True
    return not (isinstance(parsed, dict) and parsed.get("success") is False)


def evaluate_role_subagent(
    stdout: str,
    agent_type: str,
    validator: Callable[[str], bool],
    *,
    hook_events: list[dict],
) -> dict:
    """Judge one role-routed SubAgent from the already-captured stream-json ``stdout``.

    ``hook_events`` is ``extract_claude_hook_lifecycle_events(stdout)`` (injected so this module
    stays free of a runner import)."""
    starts = [e for e in hook_events if e.get("hook_event") == "SubagentStart" and e.get("agent_type") == agent_type]
    stops = [e for e in hook_events if e.get("hook_event") == "SubagentStop" and e.get("agent_type") == agent_type]
    start_ids = {e.get("agent_id") for e in starts if e.get("agent_id")}
    paired_stops = [
        s for s in stops
        if s.get("agent_id") in start_ids
        and any(
            st.get("agent_id") == s.get("agent_id") and st.get("stream_index", -1) < s.get("stream_index", -1)
            for st in starts
        )
    ]

    # parent 側の hand-back: subagent_type が一致する Agent/Task tool_use の tool_result（存在・非 error 必須）と、
    # 同 tool_use を parent_tool_use_id とする SubAgent 側 SubagentHandback tool_use（成功 tool_result 付き）。
    wanted_ids: set[str] = set()
    handbacks: list[str] = []
    results: dict[str, tuple[str, bool]] = {}
    sub_handbacks: list[tuple[str | None, str | None, str]] = []
    for event in _iter_events(stdout):
        content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                tool_input = block.get("input") or {}
                if isinstance(tool_input, dict) and tool_input.get("subagent_type") == agent_type and block.get("id"):
                    wanted_ids.add(block["id"])
            elif block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                tool_input = block.get("input")
                message = tool_input.get("message") if isinstance(tool_input, dict) else None
                sub_handbacks.append((event.get("parent_tool_use_id"), block.get("id"), str(message or "")))
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[block["tool_use_id"]] = (_tool_result_text(block), bool(block.get("is_error")))
    for tool_use_id in wanted_ids:
        result = results.get(tool_use_id)
        if result is None or result[1]:
            continue  # tool_result 欠落 / is_error は hand-back 完了ではない
        text = result[0]
        if text and HANDBACK_DELIVERY_NOTICE not in text:
            handbacks.append(text)  # report が Agent tool_result へ直接載る harness
        for parent_id, handback_id, message in sub_handbacks:
            if parent_id == tool_use_id and message and _handback_result_succeeded(results.get(handback_id)):
                handbacks.append(message)
    child_final = [s.get("last_assistant_message") or "" for s in paired_stops]

    texts = [t for t in (*handbacks, *child_final) if t]
    context_starved = any(marker in text for text in texts for marker in CONTEXT_STARVED_MARKERS)
    handback_text = "\n".join(handbacks)
    spawned = bool(starts)
    completed = bool(paired_stops)
    handed_back = bool(handbacks)
    requested_result_present = bool(handback_text) and bool(validator(handback_text))
    ok = spawned and completed and handed_back and not context_starved and requested_result_present
    return {
        "agent_type": agent_type,
        "spawned": spawned,
        "terminal_completion": completed,
        "parent_handback": handed_back,
        "context_starved": context_starved,
        "requested_result_present": requested_result_present,
        "ok": ok,
    }


def validate_haiku_handback(text: str) -> bool:
    """`codebase-investigator` に頼んだ調査結果（lib.sh の compaction window 値）を含むこと。"""
    return "272000" in text


def validate_sonnet_handback(text: str) -> bool:
    """`issue-design-reviewer` の raw semantic review 結果（assessment: clear|findings）を含むこと。"""
    return bool(re.search(r"assessment[\"']?\s*[:=]\s*[\"']?(clear|findings)\b", text))


def haiku_prompt(repo_root: str) -> str:
    return (
        "You are running inside an automated runtime smoke test. Use the Agent tool exactly once with "
        f"subagent_type \"{HAIKU_AGENT}\". Give the SubAgent exactly this task: "
        f"target_path: {repo_root}/scripts/claude-gpt/lib.sh ; purpose: report the numeric value assigned "
        "to CLAUDE_GPT_AUTO_COMPACT_WINDOW and the file/line where it is assigned ; "
        "agy_advisory_native_fallback_allowed: true. Wait for the SubAgent to finish, then reply with the "
        "SubAgent's report verbatim."
    )


def sonnet_prompt(invocation_dir: str) -> str:
    return (
        "You are running inside an automated runtime smoke test. Use the Agent tool exactly once with "
        f"subagent_type \"{SONNET_AGENT}\". Give the SubAgent exactly this task: Read "
        f"{invocation_dir}/bundle.json, read the body_file it points at, review only that pinned body, and "
        "return one raw semantic review result object (assessment and findings). Do not fetch any other "
        "Issue body. Wait for the SubAgent to finish, then reply with the SubAgent's result verbatim."
    )


SAMPLE_ISSUE_BODY = """## Outcome

launcher の default path を薄い wrapper へ縮退する。

## Acceptance Criteria

- [ ] AC1 — default path が isolation を持たない。

## Verification Commands

```bash
# AC1
$ uv run --locked pytest scripts/claude-gpt/tests/test_minimal_default_contract.py -q -k default_env_contract
```

## Allowed Paths

- `scripts/claude-gpt/launch.sh`
"""
