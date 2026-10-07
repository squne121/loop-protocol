"""Issue #2973 AC7: bounded discovery pure evaluator の hermetic 単体 test。

実 Claude Code process は起動しない。stream-json の stdout 行そのもの（runtime と同じ行形式）を合成して、
`issue2973_bounded_discovery_runtime_evaluator.py` の各分類を検証する。各 negative control は同じ builder の
正常系とちょうど 1 点だけ異なり、期待する違反 code が出ることを確かめる（false-green 防止）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent


def _load(name: str, filename: str) -> Any:
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _TESTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OLD = _load("issue2963_reachability_runtime_evaluator", "issue2963_reachability_runtime_evaluator.py")
NEW = _load("issue2973_bounded_discovery_runtime_evaluator", "issue2973_bounded_discovery_runtime_evaluator.py")

ROOT = "/synthetic/repo-root"
INV = f"{ROOT}/artifacts/invocation-x"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
AGENT = "issue-design-reviewer"
PARENT = "toolu_parent_1"
SYN = "synthetic_fx"


def _roles(listed: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    for role in ("producer", "parser", "evaluator", "consumer"):
        roles[role] = {
            "path": f"{SYN}/zz_{role}.py",
            "symbol": f"fn_{role}_zz",
            "file_fragment": f"zz_{role}",
            "listed": role in listed,
            "decoy_path": f"{SYN}/decoy/zz_stale_{role}.py" if role == "evaluator" else None,
        }
    return roles


NEG_ROLES = _roles()
HYB_ROLES = _roles(("producer", "parser"))
DECOY = f"{SYN}/decoy/zz_stale_evaluator.py"
ALLOWED = [f"{SYN}/glossary.md"]
_CONSUMER_TEXT = "1\tfrom zz_evaluator import fn_evaluator_zz\n2\tfrom zz_producer import fn_producer_zz\n"
_PRODUCER_TEXT = "1\tfrom zz_parser import fn_parser_zz\n"
_SOURCE_TEXT = {"consumer": _CONSUMER_TEXT, "producer": _PRODUCER_TEXT}


class Stream:
    """stream-json 行を組み立てる hermetic builder（`extract_claude_*` helper が読む実形状）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.n = 0

    def add(self, obj: dict[str, Any]) -> "Stream":
        self.lines.append(json.dumps(obj))
        return self

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def init(self, tools: list[str] | None = None) -> "Stream":
        event: dict[str, Any] = {"type": "system", "subtype": "init", "session_id": "s"}
        if tools is not None:
            event["tools"] = tools
        return self.add(event)

    def agent_call(self, tool_use_id: str = PARENT, subagent_type: str = AGENT) -> "Stream":
        return self.add(
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Agent",
                            "input": {"subagent_type": subagent_type, "prompt": "p"},
                        }
                    ]
                },
            }
        )

    def agent_result(self, tool_use_id: str = PARENT, *, is_error: bool = False) -> "Stream":
        return self.add(
            {
                "type": "user",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "is_error": is_error,
                            "content": [{"type": "text", "text": "done"}],
                        }
                    ]
                },
            }
        )

    def _hook_line(self, subtype: str, hook_event: str, hook_name: str, hook_id: str, payload: str = "") -> "Stream":
        line: dict[str, Any] = {
            "type": "system",
            "subtype": subtype,
            "hook_id": hook_id,
            "hook_name": hook_name,
            "hook_event": hook_event,
        }
        if subtype == "hook_response":
            line.update({"output": payload, "stdout": payload, "stderr": "", "exit_code": 0, "outcome": "success"})
        return self.add(line)

    def _lifecycle(self, hook_event: str, agent_id: str, agent_type: str, hooks: int, name: str) -> "Stream":
        payload = json.dumps({"hook_event_name": hook_event, "agent_id": agent_id, "agent_type": agent_type})
        ids = [f"{hook_event}-{agent_id}-hook{n}" for n in range(hooks)]
        for hook_id in ids:
            self._hook_line("hook_started", hook_event, name, hook_id)
        for position, hook_id in enumerate(reversed(ids)):
            self._hook_line("hook_response", hook_event, name, hook_id, payload if position == 0 else "")
        return self

    def start(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        return self._lifecycle("SubagentStart", agent_id, agent_type, 2, f"SubagentStart:{agent_type}")

    def stop(self, agent_id: str = "agent-1", agent_type: str = AGENT) -> "Stream":
        return self._lifecycle("SubagentStop", agent_id, agent_type, 3, "SubagentStop")

    def tool(
        self,
        name: str,
        tool_input: dict[str, Any],
        output: str = "",
        *,
        tool_use_id: str | None = None,
        parent: str | None = PARENT,
        is_error: bool = False,
    ) -> "Stream":
        self.n += 1
        tool_use_id = tool_use_id or f"toolu_child_{self.n}"
        self.add(
            {
                "type": "assistant",
                "parent_tool_use_id": parent,
                "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}]},
            }
        )
        return self.add(
            {
                "type": "user",
                "parent_tool_use_id": parent,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "is_error": is_error,
                            "content": [{"type": "text", "text": output}],
                        }
                    ]
                },
            }
        )

    def root(self, **kw: Any) -> "Stream":
        return self.tool(
            "Bash", {"command": f"git -C {INV} rev-parse --show-toplevel"}, kw.pop("output", ROOT + "\n"), **kw
        )

    def head(self, **kw: Any) -> "Stream":
        return self.tool("Bash", {"command": f"git -C {ROOT} rev-parse HEAD"}, kw.pop("output", HEAD + "\n"), **kw)

    def bundle(self) -> "Stream":
        self.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")
        return self.tool("Read", {"file_path": f"{INV}/body.md"}, "body")

    def grep(self, pattern: str, output: str, path: str | None = ROOT, **kw: Any) -> "Stream":
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        return self.tool("Grep", tool_input, output, **kw)

    def glob(self, pattern: str, output: str, path: str | None = ROOT, **kw: Any) -> "Stream":
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        return self.tool("Glob", tool_input, output, **kw)

    def read_role(self, roles: dict[str, dict[str, Any]], role: str, **kw: Any) -> "Stream":
        output = kw.pop("output", _SOURCE_TEXT.get(role, "source"))
        return self.tool("Read", {"file_path": f"{ROOT}/{roles[role]['path']}"}, output, **kw)

    def read_path(self, rel: str, output: str = "source", **kw: Any) -> "Stream":
        return self.tool("Read", {"file_path": f"{ROOT}/{rel}"}, output, **kw)

    def handback(self, message: str) -> "Stream":
        return self.tool("SubagentHandback", {"message": message}, "ok", tool_use_id="toolu_hb")

    def final(self, denials: list[dict[str, Any]] | None = None, subtype: str = "success") -> "Stream":
        return self.add({"type": "result", "subtype": subtype, "permission_denials": denials or []})


def _hit_text(*rels: str, extra: str = "") -> str:
    return "Found files\n" + "\n".join(rels) + extra


def _all_symbols_pattern(roles: dict[str, dict[str, Any]], only_unlisted: bool = True) -> str:
    return "|".join(r["symbol"] for r in roles.values() if not (only_unlisted and r["listed"]))


def _finding(severity: str, refs: list[str]) -> dict[str, Any]:
    return {
        "severity": severity,
        "summary": "s",
        "evidence_refs": refs,
        "recommended_fix": "f",
        "requires_owner_choice": False,
    }


def gap_result(roles: dict[str, dict[str, Any]], head: str = HEAD) -> dict[str, Any]:
    refs = [f"HEAD {head}", *(f"{ROOT}/{r['path']}:5" for r in roles.values())]
    return {"assessment": "findings", "findings": [_finding("high", refs)]}


CLEAR = {"assessment": "clear", "findings": []}


def default_result(kind: str) -> dict[str, Any]:
    if kind in ("negative", "hybrid"):
        return gap_result(NEG_ROLES if kind == "negative" else HYB_ROLES)
    return CLEAR


def discovery_steps(s: Stream, kind: str) -> Stream:
    """正常系の observation 部分（root -> HEAD -> bundle -> discovery -> Read）。"""
    s.root().head().bundle()
    if kind == "negative" or kind == "positive":
        roles = NEG_ROLES
        s.grep(
            _all_symbols_pattern(roles),
            _hit_text(
                *(r["path"] for r in roles.values()),
                DECOY,
                extra=f"\n{SYN}/test_self.py:3:  '{roles['evaluator']['path']}'",
            ),
        )
        s.read_role(roles, "consumer")
        if kind == "negative":
            s.read_path(DECOY, "decoy source")  # 同名 symbol の decoy（bound 内。target 確定とは別）
        for role in ("evaluator", "producer", "parser"):
            s.read_role(roles, role)
    elif kind == "hybrid":
        roles = HYB_ROLES
        s.read_role(roles, "producer")
        s.read_role(roles, "parser")
        s.grep(
            _all_symbols_pattern(roles),
            _hit_text(roles["evaluator"]["path"], roles["consumer"]["path"]),
        )
        s.read_role(roles, "consumer")
        s.read_role(roles, "evaluator")
    return s


def normal_stream(kind: str = "negative", result: Any = "__default__") -> Stream:
    s = Stream().init().agent_call().start()
    if kind == "simple":
        s.bundle().read_path(ALLOWED[0], "glossary")
    else:
        discovery_steps(s, kind)
    raw = default_result(kind) if result == "__default__" else result
    s.handback(json.dumps(raw))
    return s.stop().agent_result().final()


def evaluate(stream: Stream | str, kind: str = "negative", result: Any = "__default__", **kw: Any) -> dict[str, Any]:
    stdout = stream if isinstance(stream, str) else stream.text()
    raw = default_result(kind) if result == "__default__" else result
    roles = {"negative": NEG_ROLES, "positive": NEG_ROLES, "hybrid": HYB_ROLES, "simple": None}[kind]
    params: dict[str, Any] = {
        "stdout": stdout,
        "tested_head": HEAD,
        "resolved_root": ROOT,
        "invocation_dir": INV,
        "fixture_kind": kind,
        "fixture_roles": roles,
        "allowed_read_paths": ALLOWED if kind == "simple" else [],
        "raw_result": raw,
    }
    params.update(kw)
    return NEW.evaluate_bounded_discovery_runtime(**params)


def assert_outcome(outcome: dict[str, Any], verdict: str, rule: int, violation: str | None = None) -> None:
    assert (outcome["verdict"], outcome["rule"]) == (verdict, rule), outcome
    assert outcome["exit_code"] == NEW.EXIT_CODES[verdict]
    if violation is not None:
        violations = outcome["evidence"]["violations"]
        assert any(v.startswith(violation) for v in violations), (violation, violations)


def build(kind: str, mutate: Any) -> Stream:
    """正常系の observation を `mutate(stream)` で置き換えた stream（Start / Stop / handback は共通）。"""
    s = Stream().init().agent_call().start()
    mutate(s)
    s.handback(json.dumps(default_result(kind)))
    return s.stop().agent_result().final()


# --- 正常系（false-green でない: 各 negative control の対照） ---------------------------------------------------


@pytest.mark.parametrize("kind", ["negative", "positive", "hybrid", "simple"])
def test_normal_cases_pass(kind: str) -> None:
    outcome = evaluate(normal_stream(kind), kind)
    assert_outcome(outcome, "pass", 5)
    assert outcome["evidence"]["violations"] == []


def test_constants_are_the_fixed_issue_contract_values() -> None:
    assert NEW.DISCOVERY_SEARCH_CALL_MAX == 8
    assert NEW.DISCOVERY_SOURCE_READ_MAX == 8
    assert NEW.SEARCH_SCOPE == "repository_root_only"
    assert NEW.DISCOVERY_TOOLS == ("Grep", "Glob")


def test_normal_hybrid_does_not_search_for_listed_roles_and_negative_resolves_all_four() -> None:
    hybrid = evaluate(normal_stream("hybrid"), "hybrid")
    roles = hybrid["evidence"]["discovery"]["roles"]
    assert roles["producer"]["via"] == "direct_read" and roles["parser"]["via"] == "direct_read"
    assert roles["evaluator"]["via"] == "discovery" and roles["consumer"]["via"] == "discovery"
    negative = evaluate(normal_stream("negative"))
    assert all(r["satisfied"] and not r["listed"] for r in negative["evidence"]["discovery"]["roles"].values())


# --- 規則 1-3 -----------------------------------------------------------------------------------------------


def test_rule1_no_stream_is_unavailable() -> None:
    assert_outcome(evaluate(""), "unavailable", 1)
    assert_outcome(evaluate(normal_stream(), claude_unavailable_reason="claude not found"), "unavailable", 1)


def test_rule2_permission_denial_of_discovery_or_required_read_is_unavailable_not_fail() -> None:
    s = Stream().init().agent_call().start().root().head().bundle()
    s.grep(_all_symbols_pattern(NEG_ROLES), "denied", tool_use_id="toolu_grep", is_error=True)
    s.stop().agent_result().final([{"tool_name": "Grep", "tool_use_id": "toolu_grep", "tool_input": {}}])
    assert_outcome(evaluate(s), "unavailable", 2)

    def denied_read(s2: Stream) -> None:
        discovery_steps(s2, "negative")
        s2.read_role(NEG_ROLES, "producer", tool_use_id="toolu_read", is_error=True)

    s3 = Stream().init().agent_call().start()
    denied_read(s3)
    s3.stop().agent_result().final([{"tool_name": "Read", "tool_use_id": "toolu_read", "tool_input": {}}])
    assert_outcome(evaluate(s3), "unavailable", 2)


def test_rule2_session_without_grep_glob_tools_is_unavailable_not_fail_and_never_pass() -> None:
    """native build の既定 tool pool は Grep / Glob を持たない。reviewer は呼べないため検証不能（unavailable）。"""

    def stream(tools: list[str] | None) -> Stream:
        s = Stream().init(tools).agent_call().start().root().head().bundle()
        s.grep(
            _all_symbols_pattern(NEG_ROLES),
            "<tool_use_error>No such tool available: Grep</tool_use_error>",
            is_error=True,
        )
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)
        s.handback(json.dumps(default_result("negative")))
        return s.stop().agent_result().final()

    base = ["Read", "Bash", "Agent"]
    for tools in (base, [*base, "Grep"], [*base, "Glob"]):
        outcome = evaluate(stream(tools))
        assert_outcome(outcome, "unavailable", 2)
        assert outcome["evidence"]["missing_discovery_tools"] == [t for t in ("Grep", "Glob") if t not in tools]
    # Grep / Glob が tool pool にあるのに discovery が失敗した場合は、通常どおり規則 4 の FAIL（unavailable にしない）。
    assert_outcome(evaluate(stream([*base, "Grep", "Glob"])), "fail", 4, "no_successful_discovery")
    # `init` に tools が無い stream は capability を判定しない（従来どおり規則 4 へ進む）。
    assert_outcome(evaluate(stream(None)), "fail", 4, "no_successful_discovery")
    # simple は discovery を要求しないため tool pool に依存しない。
    simple = Stream().init(base).agent_call().start().bundle().handback(json.dumps(CLEAR)).stop().agent_result().final()
    assert_outcome(evaluate(simple, "simple"), "pass", 5)
    # `missing_discovery_tools` は `system init` の `tools` だけを読む（構造化 field）。
    init_events = [json.loads(line) for line in Stream().init(base).lines]
    assert NEW.missing_discovery_tools(init_events) == ["Grep", "Glob"]
    assert (
        NEW.missing_discovery_tools([json.loads(line) for line in Stream().init([*base, "Grep", "Glob"]).lines]) == []
    )


def test_rule3_terminal_incomplete_is_fail_not_pass() -> None:
    complete = normal_stream("negative")
    assert NEW.terminal_incomplete_reason(complete.text(), timed_out=False) is None
    assert NEW.terminal_incomplete_reason(complete.text(), timed_out=True)
    reason = NEW.terminal_incomplete_reason(complete.text(), timed_out=True)
    assert_outcome(evaluate(complete, terminal_incomplete=reason), "fail", 3)
    max_turns = Stream().init().agent_call().start()
    discovery_steps(max_turns, "negative")
    max_turns.handback(json.dumps(default_result("negative"))).stop().agent_result().final(subtype="error_max_turns")
    assert NEW.terminal_incomplete_reason(max_turns.text(), timed_out=False)
    no_result = Stream().init().agent_call()
    assert NEW.terminal_incomplete_reason(no_result.text(), timed_out=False) == "no terminal result event"


def test_rule3_lifecycle_failures_are_fail() -> None:
    no_start = Stream().init().agent_call()
    discovery_steps(no_start, "negative")
    no_start.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    assert_outcome(evaluate(no_start), "fail", 3)
    two_agents = Stream().init().agent_call().start("agent-1").start("agent-2")
    discovery_steps(two_agents, "negative")
    two_agents.stop("agent-1").stop("agent-2").agent_result().final()
    assert_outcome(evaluate(two_agents), "fail", 3)


# --- parity: 規則 1-3 の outcome が #2963 の evaluator と一致する ---------------------------------------------------


def _parity_streams() -> list[tuple[str, Stream | str, dict[str, Any]]]:
    streams: list[tuple[str, Stream | str, dict[str, Any]]] = []
    streams.append(("normal", normal_stream("negative"), {}))
    streams.append(("no_stream", "", {}))
    streams.append(("unavailable_reason", normal_stream("negative"), {"unavailable": "claude not found"}))
    no_start = Stream().init().agent_call()
    discovery_steps(no_start, "negative")
    streams.append(("no_start", no_start.stop().agent_result().final(), {}))
    no_stop = Stream().init().agent_call().start()
    discovery_steps(no_stop, "negative")
    streams.append(("no_stop", no_stop.agent_result().final(), {}))
    stop_first = Stream().init().agent_call().stop()
    discovery_steps(stop_first, "negative")
    streams.append(("stop_before_start", stop_first.start().agent_result().final(), {}))
    different_ids = Stream().init().agent_call().start("agent-1")
    discovery_steps(different_ids, "negative")
    streams.append(("different_ids", different_ids.stop("agent-2").agent_result().final(), {}))
    two_ids = Stream().init().agent_call().start("agent-1").start("agent-2")
    discovery_steps(two_ids, "negative")
    streams.append(("two_ids", two_ids.stop("agent-1").stop("agent-2").agent_result().final(), {}))
    wrong_type = Stream().init().agent_call().start(agent_type="other-agent")
    discovery_steps(wrong_type, "negative")
    streams.append(("wrong_type", wrong_type.stop(agent_type="other-agent").agent_result().final(), {}))
    no_parent = Stream().init().start()
    discovery_steps(no_parent, "negative")
    streams.append(("no_parent_agent", no_parent.stop().final(), {}))
    two_parents = Stream().init().agent_call().agent_call("toolu_parent_2").start()
    discovery_steps(two_parents, "negative")
    streams.append(("two_parent_agents", two_parents.stop().agent_result().final(), {}))
    parent_error = Stream().init().agent_call().start()
    discovery_steps(parent_error, "negative")
    streams.append(("parent_result_error", parent_error.stop().agent_result(is_error=True).final(), {}))
    empty_interval = Stream().init().agent_call().start().stop().agent_result().final()
    streams.append(("empty_interval", empty_interval, {}))
    outside = Stream().init().agent_call()
    discovery_steps(outside, "negative")
    streams.append(("tools_outside_interval", outside.start().stop().agent_result().final(), {}))
    other_parent = Stream().init().agent_call().start()
    other_parent.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}", parent="toolu_other")
    streams.append(("only_other_parent_tools", other_parent.stop().agent_result().final(), {}))
    denied_root = Stream().init().agent_call().start()
    denied_root.root(tool_use_id="toolu_root", is_error=True, output="denied")
    streams.append(
        (
            "denied_root",
            denied_root.stop().agent_result().final([{"tool_name": "Bash", "tool_use_id": "toolu_root"}]),
            {},
        )
    )
    denied_parent = Stream().init().agent_call().start()
    discovery_steps(denied_parent, "negative")
    streams.append(
        (
            "denied_parent_agent",
            denied_parent.stop().agent_result().final([{"tool_name": "Agent", "tool_use_id": PARENT}]),
            {},
        )
    )
    denied_read = Stream().init().agent_call().start().root().head()
    denied_read.read_role(NEG_ROLES, "producer", tool_use_id="toolu_target_read", is_error=True)
    streams.append(
        (
            "denied_target_read",
            denied_read.stop().agent_result().final([{"tool_name": "Read", "tool_use_id": "toolu_target_read"}]),
            {},
        )
    )
    return streams


def _rules_1_3(outcome: dict[str, Any]) -> tuple[str, int] | str:
    return (outcome["verdict"], outcome["rule"]) if outcome["rule"] <= 3 else "continue"


def parity_mismatches() -> list[str]:
    """同一 stream に対する #2963 の evaluator と新 evaluator の規則 1-3 の outcome 不一致の一覧。

    比較は両 evaluator が定義する domain（lifecycle / 親 Agent / root・HEAD の Bash / target Read）に限る。
    #2973 固有の discovery（Grep / Glob）の観測は #2963 側と比較しない。hybrid は #2963 contract では
    negative と同等に扱う（4 役の target path を全て必須とする）。"""
    paths = {role: NEG_ROLES[role]["path"] for role in NEG_ROLES}
    mismatches: list[str] = []
    for name, stream, options in _parity_streams():
        stdout = stream if isinstance(stream, str) else stream.text()
        old = OLD.evaluate_reachability_runtime(
            stdout=stdout,
            tested_head=HEAD,
            resolved_root=ROOT,
            invocation_dir=INV,
            fixture_kind="negative",
            fixture_paths=paths,
            raw_result=default_result("negative"),
            claude_unavailable_reason=options.get("unavailable"),
        )
        for kind, roles in (("negative", NEG_ROLES), ("hybrid", HYB_ROLES)):
            new = NEW.evaluate_bounded_discovery_runtime(
                stdout=stdout,
                tested_head=HEAD,
                resolved_root=ROOT,
                invocation_dir=INV,
                fixture_kind=kind,
                fixture_roles=roles,
                raw_result=default_result(kind),
                claude_unavailable_reason=options.get("unavailable"),
            )
            if _rules_1_3(old) != _rules_1_3(new):
                mismatches.append(f"{name}/{kind}: old={_rules_1_3(old)} new={_rules_1_3(new)}")
    return mismatches


def test_rules_1_3_outcomes_match_the_2963_evaluator_on_the_same_stream() -> None:
    streams = _parity_streams()
    assert len(streams) >= 15
    assert parity_mismatches() == []
    seen = set()
    for _name, stream, options in streams:
        stdout = stream if isinstance(stream, str) else stream.text()
        outcome = OLD.evaluate_reachability_runtime(
            stdout=stdout,
            tested_head=HEAD,
            resolved_root=ROOT,
            invocation_dir=INV,
            fixture_kind="negative",
            fixture_paths={role: NEG_ROLES[role]["path"] for role in NEG_ROLES},
            raw_result=default_result("negative"),
            claude_unavailable_reason=options.get("unavailable"),
        )
        seen.add(_rules_1_3(outcome))
    # 非 vacuous: unavailable / fail の別と、規則 3 を通過する経路の全てを parity が実際に区別している。
    assert {("unavailable", 1), ("unavailable", 2), ("fail", 3), "continue"} <= seen


def test_parity_test_can_fail_when_the_new_lifecycle_rules_diverge(monkeypatch: pytest.MonkeyPatch) -> None:
    """mutation: 新 evaluator の reviewer 判定を壊すと parity が不一致を検出する（false-green でない）。"""
    assert parity_mismatches() == []
    monkeypatch.setattr(NEW, "_reviewer_agent_tool_use_ids", lambda tool_uses: set())
    assert parity_mismatches() != []
    monkeypatch.undo()
    assert parity_mismatches() == []

    real = NEW._reviewer_invocation

    def lenient(lifecycle: list[dict[str, Any]]) -> dict[str, Any]:
        data = real(lifecycle)
        data["stop_ids"] = set(data["start_ids"])  # Start / Stop の agent_id 不一致を見逃す
        return data

    monkeypatch.setattr(NEW, "_reviewer_invocation", lenient)
    assert any(item.startswith("different_ids") for item in parity_mismatches())


# --- 規則 4: discovery / bound / scope / 因果 -----------------------------------------------------------------


def test_rule4_no_discovery_is_fail() -> None:
    def no_discovery(s: Stream) -> None:
        s.root().head().bundle()
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    outcome = evaluate(build("negative", no_discovery))
    assert_outcome(outcome, "fail", 4, "no_successful_discovery")
    assert any(v.startswith("read_before_discovery:consumer") for v in outcome["evidence"]["violations"])


def test_rule4_discovery_outside_the_reviewer_interval_is_not_counted() -> None:
    s = Stream().init().agent_call()
    s.root().head()  # Start より前（区間外）
    s.grep(_all_symbols_pattern(NEG_ROLES), _hit_text(*(r["path"] for r in NEG_ROLES.values())))
    s.start().root().head().bundle()
    for role in ("consumer", "evaluator", "producer", "parser"):
        s.read_role(NEG_ROLES, role)
    s.handback(json.dumps(default_result("negative"))).stop().agent_result().final()
    assert_outcome(evaluate(s), "fail", 4, "no_successful_discovery")
    other_parent = Stream().init().agent_call().start().root().head().bundle()
    other_parent.grep(
        _all_symbols_pattern(NEG_ROLES), _hit_text(*(r["path"] for r in NEG_ROLES.values())), parent="toolu_x"
    )
    for role in ("consumer", "evaluator", "producer", "parser"):
        other_parent.read_role(NEG_ROLES, role)
    assert_outcome(evaluate(other_parent.stop().agent_result().final()), "fail", 4, "no_successful_discovery")


def test_rule4_non_permission_search_failure_is_fail_not_unavailable() -> None:
    def failed_search(s: Stream) -> None:
        s.root().head().bundle()
        s.grep(_all_symbols_pattern(NEG_ROLES), "grep: error", is_error=True)
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", failed_search)), "fail", 4, "no_successful_discovery")


def _negative_with_extra_searches(count: int) -> Stream:
    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(count):
            s.grep(NEG_ROLES["evaluator"]["symbol"], _hit_text(NEG_ROLES["evaluator"]["path"]))

    return build("negative", mutate)


def test_rule4_search_call_bound_is_exactly_eight_counting_grep_and_glob_failures_too() -> None:
    # normal は Grep 1 回。追加 7 回で合計 8（上限ちょうど）は PASS、追加 8 回で 9 は FAIL。
    assert_outcome(evaluate(_negative_with_extra_searches(7)), "pass", 5)
    assert_outcome(evaluate(_negative_with_extra_searches(8)), "fail", 4, "search_bound_exceeded:9>8")

    def mixed(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(4):
            s.glob(f"**/{NEG_ROLES['evaluator']['file_fragment']}*", _hit_text(NEG_ROLES["evaluator"]["path"]))
        for _ in range(3):  # 失敗した検索も数える
            s.grep(NEG_ROLES["evaluator"]["symbol"], "error", is_error=True)

    assert_outcome(evaluate(build("negative", mixed)), "pass", 5)

    def mixed_over(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(4):
            s.glob(f"**/{NEG_ROLES['evaluator']['file_fragment']}*", _hit_text(NEG_ROLES["evaluator"]["path"]))
        for _ in range(4):
            s.grep(NEG_ROLES["evaluator"]["symbol"], "error", is_error=True)

    assert_outcome(evaluate(build("negative", mixed_over)), "fail", 4, "search_bound_exceeded")


def test_rule4_source_read_bound_is_exactly_eight_and_excludes_bundle_and_body() -> None:
    def reads(extra: int) -> Stream:
        def mutate(s: Stream) -> None:
            discovery_steps(s, "negative")  # target 4 + decoy 1 = 5 Read（bundle / body は数えない）
            for n in range(extra):
                s.read_path(f"{SYN}/other_{n}.py")

        return build("negative", mutate)

    assert_outcome(evaluate(reads(3)), "pass", 5)  # 合計 8
    assert_outcome(evaluate(reads(4)), "fail", 4, "read_bound_exceeded:9>8")

    def many_bundle_reads(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(12):
            s.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")
            s.tool("Read", {"file_path": f"{INV}/body.md"}, "body")

    assert_outcome(evaluate(build("negative", many_bundle_reads)), "pass", 5)

    def lookalike(s: Stream) -> None:
        discovery_steps(s, "negative")
        for _ in range(4):
            s.tool("Read", {"file_path": f"{INV}/sub/bundle.json"}, "{}")  # 直下ではない file は対象

    assert_outcome(evaluate(build("negative", lookalike)), "fail", 4, "read_bound_exceeded")


@pytest.mark.parametrize(
    ("tool", "path", "expected"),
    [
        ("Grep", "/synthetic/elsewhere", "search_outside_root:Grep"),
        ("Grep", f"{ROOT}/../outside", "search_outside_root:Grep"),
        ("Grep", f"{ROOT}-sibling", "search_outside_root:Grep"),
        ("Grep", None, "search_path_omitted:Grep"),
        ("Grep", "", "search_path_omitted:Grep"),
        ("Grep", "relative/dir", "search_path_not_absolute:Grep"),
        ("Glob", "/synthetic/elsewhere", "search_outside_root:Glob"),
        ("Glob", None, "search_path_omitted:Glob"),
    ],
)
def test_rule4_search_scope_violations_are_fail(tool: str, path: str | None, expected: str) -> None:
    def mutate(s: Stream) -> None:
        discovery_steps(s, "negative")
        query = NEG_ROLES["evaluator"]["symbol"] if tool == "Grep" else f"**/{NEG_ROLES['evaluator']['file_fragment']}*"
        (s.grep if tool == "Grep" else s.glob)(query, _hit_text(NEG_ROLES["evaluator"]["path"]), path=path)

    assert_outcome(evaluate(build("negative", mutate)), "fail", 4, expected)


def test_rule4_glob_pattern_escaping_or_pointing_outside_root_is_fail() -> None:
    fragment = NEG_ROLES["evaluator"]["file_fragment"]
    for pattern in (f"/synthetic/elsewhere/**/{fragment}*", f"../../{fragment}*"):

        def mutate(s: Stream, pattern: str = pattern) -> None:
            discovery_steps(s, "negative")
            s.glob(pattern, _hit_text(NEG_ROLES["evaluator"]["path"]))

        assert_outcome(evaluate(build("negative", mutate)), "fail", 4, "search_outside_root:Glob")
    inside = f"**/{fragment}*"

    def ok(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.glob(inside, _hit_text(NEG_ROLES["evaluator"]["path"]), path=f"{ROOT}/{SYN}")

    assert_outcome(evaluate(build("negative", ok)), "pass", 5)


def test_rule4_unrelated_queries_are_fail_with_grep_and_glob_checked_separately() -> None:
    def grep_unrelated(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.grep("totally_unrelated_symbol", _hit_text(NEG_ROLES["evaluator"]["path"]))

    assert_outcome(evaluate(build("negative", grep_unrelated)), "fail", 4, "irrelevant_query:Grep")

    def glob_unrelated(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.glob("**/*.py", _hit_text(NEG_ROLES["evaluator"]["path"]))

    assert_outcome(evaluate(build("negative", glob_unrelated)), "fail", 4, "irrelevant_query:Glob")

    def grep_with_fragment_only(s: Stream) -> None:  # Grep は symbol、Glob は file 名断片が関連性（取り違えは違反）
        discovery_steps(s, "negative")
        s.grep(NEG_ROLES["evaluator"]["file_fragment"], _hit_text(NEG_ROLES["evaluator"]["path"]))

    assert_outcome(evaluate(build("negative", grep_with_fragment_only)), "fail", 4, "irrelevant_query:Grep")

    def glob_with_symbol_only(s: Stream) -> None:
        discovery_steps(s, "negative")
        s.glob(f"**/{NEG_ROLES['evaluator']['symbol']}*", _hit_text(NEG_ROLES["evaluator"]["path"]))

    assert_outcome(evaluate(build("negative", glob_with_symbol_only)), "fail", 4, "irrelevant_query:Glob")


def test_rule4_result_without_the_target_path_does_not_justify_the_target_read() -> None:
    def no_target_in_result(s: Stream) -> None:
        s.root().head().bundle()
        s.grep(_all_symbols_pattern(NEG_ROLES), _hit_text(DECOY, f"{SYN}/test_self.py"))  # target path が無い
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role, output="no import hints")

    outcome = evaluate(build("negative", no_target_in_result))
    assert_outcome(outcome, "fail", 4, "read_before_discovery")
    assert "no_successful_discovery" in outcome["evidence"]["violations"]


def test_rule4_self_referential_literal_hit_is_not_a_discovery_success() -> None:
    """body・test・evaluator 内に target path が literal として現れるだけの hit（他 file の本文行）は数えない。"""
    literal = "\n".join(f"{SYN}/test_self.py:{n}:  '{r['path']}'" for n, r in enumerate(NEG_ROLES.values(), 1))

    def self_ref(s: Stream) -> None:
        s.root().head().bundle()
        s.grep(_all_symbols_pattern(NEG_ROLES), literal)
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role, output="no import hints")

    assert_outcome(evaluate(build("negative", self_ref)), "fail", 4, "read_before_discovery")
    assert not NEW.result_lists_path(literal, ROOT, NEG_ROLES["producer"]["path"])
    assert NEW.result_lists_path(_hit_text(NEG_ROLES["producer"]["path"]), ROOT, NEG_ROLES["producer"]["path"])
    assert NEW.result_lists_path(f"{ROOT}/{NEG_ROLES['producer']['path']}:3:x", ROOT, NEG_ROLES["producer"]["path"])
    assert NEW.result_lists_path(f"./{NEG_ROLES['producer']['path']}", ROOT, NEG_ROLES["producer"]["path"])
    assert not NEW.result_lists_path(f"{NEG_ROLES['producer']['path']}.bak", ROOT, NEG_ROLES["producer"]["path"])


def test_rule4_read_before_discovery_is_fail_even_if_a_dummy_search_follows() -> None:
    def guess_then_search(s: Stream) -> None:
        s.root().head().bundle()
        s.read_role(NEG_ROLES, "consumer")  # 推測 Read（discovery より前）
        s.grep(_all_symbols_pattern(NEG_ROLES), _hit_text(*(r["path"] for r in NEG_ROLES.values())))
        for role in ("evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", guess_then_search)), "fail", 4, "read_before_discovery:consumer")


def test_rule4_decoy_only_read_without_the_target_is_fail() -> None:
    def decoy_only(s: Stream) -> None:
        s.root().head().bundle()
        s.grep(_all_symbols_pattern(NEG_ROLES), _hit_text(*(r["path"] for r in NEG_ROLES.values()), DECOY))
        for role in ("consumer", "producer", "parser"):
            s.read_role(NEG_ROLES, role)
        s.read_path(DECOY, "decoy source")  # evaluator の target を Read しない

    outcome = evaluate(build("negative", decoy_only))
    assert_outcome(outcome, "fail", 4, "missing_target_read:evaluator")
    assert outcome["evidence"]["violations"] == ["missing_target_read:evaluator"]


def test_rule4_hybrid_re_search_of_an_explicit_path_role_is_fail() -> None:
    def re_search(s: Stream) -> None:
        discovery_steps(s, "hybrid")
        s.grep(HYB_ROLES["producer"]["symbol"], _hit_text(HYB_ROLES["producer"]["path"]))  # listed role の再探索

    assert_outcome(evaluate(build("hybrid", re_search), "hybrid"), "fail", 4, "irrelevant_query:Grep")

    def re_glob(s: Stream) -> None:
        discovery_steps(s, "hybrid")
        s.glob(f"**/{HYB_ROLES['parser']['file_fragment']}*", _hit_text(HYB_ROLES["parser"]["path"]))

    assert_outcome(evaluate(build("hybrid", re_glob), "hybrid"), "fail", 4, "irrelevant_query:Glob")


def test_rule4_hybrid_unresolved_role_without_discovery_is_fail() -> None:
    def no_discovery(s: Stream) -> None:
        s.root().head().bundle()
        for role in ("producer", "parser", "consumer", "evaluator"):
            s.read_role(HYB_ROLES, role)

    assert_outcome(evaluate(build("hybrid", no_discovery), "hybrid"), "fail", 4, "no_successful_discovery")


def test_rule4_target_derived_from_the_consumer_import_is_not_a_violation() -> None:
    """帰属規則 (b): 既に Read した consumer の結果 text に module 名が現れる target の Read は discovery 不要。"""

    def via_import(s: Stream) -> None:
        s.root().head().bundle()
        s.grep(NEG_ROLES["consumer"]["symbol"], _hit_text(NEG_ROLES["consumer"]["path"], DECOY))
        s.read_role(NEG_ROLES, "consumer")  # import: zz_evaluator / zz_producer
        s.read_role(NEG_ROLES, "evaluator")
        s.read_role(NEG_ROLES, "producer")  # producer の text は zz_parser を含む
        s.read_role(NEG_ROLES, "parser")

    outcome = evaluate(build("negative", via_import))
    assert_outcome(outcome, "pass", 5)
    roles = outcome["evidence"]["discovery"]["roles"]
    assert roles["consumer"]["via"] == "discovery"
    assert roles["evaluator"]["via"] == "import" and roles["producer"]["via"] == "import"
    assert roles["parser"]["via"] == "import"

    def import_without_hint(s: Stream) -> None:  # consumer の text に evaluator の module 名が無ければ導出できない
        s.root().head().bundle()
        s.grep(NEG_ROLES["consumer"]["symbol"], _hit_text(NEG_ROLES["consumer"]["path"]))
        s.read_role(NEG_ROLES, "consumer", output="1\tno imports here")
        for role in ("evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", import_without_hint)), "fail", 4, "read_before_discovery:evaluator")


def test_rule4_a_decoy_module_name_does_not_justify_the_target() -> None:
    def decoy_import_only(s: Stream) -> None:
        s.root().head().bundle()
        s.grep(NEG_ROLES["consumer"]["symbol"], _hit_text(NEG_ROLES["consumer"]["path"]))
        s.read_role(NEG_ROLES, "consumer", output="1\tfrom zz_stale_evaluator import x\n")
        for role in ("evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", decoy_import_only)), "fail", 4, "read_before_discovery:evaluator")


def test_rule4_one_search_may_serve_several_roles() -> None:
    outcome = evaluate(normal_stream("negative"))
    assert outcome["evidence"]["discovery"]["search_calls"] == 1
    assert all(r["via"] in ("discovery", "import") for r in outcome["evidence"]["discovery"]["roles"].values())


def test_rule4_root_and_head_must_precede_discovery_and_match() -> None:
    def head_after_search(s: Stream) -> None:
        s.root()
        s.grep(_all_symbols_pattern(NEG_ROLES), _hit_text(*(r["path"] for r in NEG_ROLES.values())))
        s.head().bundle()
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", head_after_search)), "fail", 4, "before_head")

    def wrong_head(s: Stream) -> None:
        s.root().head(output=OTHER_HEAD + "\n").bundle()
        for role in ("consumer", "evaluator", "producer", "parser"):
            s.read_role(NEG_ROLES, role)

    assert_outcome(evaluate(build("negative", wrong_head)), "fail", 4, "head_resolution_missing")

    def no_root(s: Stream) -> None:
        s.head().bundle()
        discovery_steps_no_prefix(s)

    assert_outcome(evaluate(build("negative", no_root)), "fail", 4, "root_resolution_missing")


def discovery_steps_no_prefix(s: Stream) -> None:
    s.grep(_all_symbols_pattern(NEG_ROLES), _hit_text(*(r["path"] for r in NEG_ROLES.values())))
    for role in ("consumer", "evaluator", "producer", "parser"):
        s.read_role(NEG_ROLES, role)


def test_rule4_marker_string_only_and_bash_cat_are_not_observations() -> None:
    def cat_instead(s: Stream) -> None:
        s.root().head().bundle()
        s.tool(
            "Bash",
            {"command": f"rg -n {NEG_ROLES['evaluator']['symbol']} {ROOT}"},
            _hit_text(NEG_ROLES["evaluator"]["path"]),
        )
        s.tool("Bash", {"command": f"cat {ROOT}/{NEG_ROLES['evaluator']['path']}"}, "source")

    outcome = evaluate(build("negative", cat_instead))
    assert_outcome(outcome, "fail", 4, "no_successful_discovery")
    assert "missing_target_read:evaluator" in outcome["evidence"]["violations"]


# --- 規則 5: verdict ----------------------------------------------------------------------------------------


def test_rule5_negative_and_hybrid_require_all_target_paths_and_tested_head_in_the_high_union() -> None:
    for kind, roles in (("negative", NEG_ROLES), ("hybrid", HYB_ROLES)):
        for drop in roles:
            refs = [f"HEAD {HEAD}", *(r["path"] for name, r in roles.items() if name != drop)]
            result = {"assessment": "findings", "findings": [_finding("high", refs)]}
            assert_outcome(evaluate(normal_stream(kind, result), kind, result=result), "fail", 5)
        no_head = {"assessment": "findings", "findings": [_finding("blocker", [r["path"] for r in roles.values()])]}
        assert_outcome(evaluate(normal_stream(kind, no_head), kind, result=no_head), "fail", 5)
        medium = {
            "assessment": "findings",
            "findings": [_finding("medium", [HEAD, *(r["path"] for r in roles.values())])],
        }
        assert_outcome(evaluate(normal_stream(kind, medium), kind, result=medium), "fail", 5)
        assert_outcome(evaluate(normal_stream(kind, CLEAR), kind, result=CLEAR), "fail", 5)
        split = {
            "assessment": "findings",
            "findings": [
                _finding("high", [HEAD, roles["evaluator"]["path"]]),
                _finding("blocker", [r["path"] for name, r in roles.items() if name != "evaluator"]),
            ],
        }
        assert_outcome(evaluate(normal_stream(kind, split), kind, result=split), "pass", 5)


def test_rule5_positive_with_any_high_or_blocker_finding_is_fail() -> None:
    for severity in ("high", "blocker"):
        over = {"assessment": "findings", "findings": [_finding(severity, ["symbol_name_only"])]}
        assert_outcome(evaluate(normal_stream("positive", over), "positive", result=over), "fail", 5)
    medium = {"assessment": "findings", "findings": [_finding("medium", ["x"]), _finding("low", ["y"])]}
    assert_outcome(evaluate(normal_stream("positive", medium), "positive", result=medium), "pass", 5)
    assert_outcome(evaluate(normal_stream("positive", CLEAR), "positive", result=CLEAR), "pass", 5)


def test_rule5_unstructured_or_ambiguous_raw_result_is_fail() -> None:
    for raw in (None, "", "looks fine", {"assessment": "maybe", "findings": []}, {"assessment": "clear"}):
        assert_outcome(evaluate(normal_stream("negative"), result=raw), "fail", 5)
    malformed = {"assessment": "findings", "findings": [{"severity": "high", "evidence_refs": "not-a-list"}]}
    assert_outcome(evaluate(normal_stream("negative"), result=malformed), "fail", 5)


def test_unknown_fixture_kind_and_missing_roles_are_fail_closed() -> None:
    assert_outcome(evaluate(normal_stream(), kind="negative", fixture_kind="bogus"), "fail", 5)
    assert_outcome(evaluate(normal_stream(), fixture_roles=None), "fail", 5)


# --- simple ---------------------------------------------------------------------------------------------------


def test_simple_requires_no_discovery_and_no_source_read_beyond_allowed_paths() -> None:
    def with_grep(s: Stream) -> None:
        s.bundle()
        s.grep("anything", "x")

    assert_outcome(evaluate(build("simple", with_grep), "simple"), "fail", 4, "simple_discovery:Grep")

    def with_glob(s: Stream) -> None:
        s.bundle()
        s.glob("**/*.md", "x")

    assert_outcome(evaluate(build("simple", with_glob), "simple"), "fail", 4, "simple_discovery:Glob")

    def with_source_read(s: Stream) -> None:
        s.bundle()
        s.read_path(NEG_ROLES["evaluator"]["path"])

    assert_outcome(evaluate(build("simple", with_source_read), "simple"), "fail", 4, "simple_source_read")

    def only_bundle(s: Stream) -> None:
        s.bundle()
        s.tool("Read", {"file_path": f"{INV}/bundle.json"}, "{}")

    assert_outcome(evaluate(build("simple", only_bundle), "simple"), "pass", 5)

    def allowed(s: Stream) -> None:
        s.bundle()
        s.read_path(ALLOWED[0], "glossary")

    assert_outcome(evaluate(build("simple", allowed), "simple"), "pass", 5)
    # allowed_read_paths を空にすると同じ Read は違反になる（allow-list が効いている）。
    assert_outcome(evaluate(build("simple", allowed), "simple", allowed_read_paths=[]), "fail", 4, "simple_source_read")


def test_simple_with_high_or_blocker_is_fail() -> None:
    for severity in ("high", "blocker"):
        over = {"assessment": "findings", "findings": [_finding(severity, ["x.md"])]}
        assert_outcome(evaluate(normal_stream("simple", over), "simple", result=over), "fail", 5)
    medium = {"assessment": "findings", "findings": [_finding("medium", ["x.md"])]}
    assert_outcome(evaluate(normal_stream("simple", medium), "simple", result=medium), "pass", 5)


def test_simple_denied_discovery_attempt_is_not_a_required_observation() -> None:
    """simple には必須観測が無いため、Grep の拒否は unavailable ではなく discovery 実行（fail）として扱う。"""
    s = Stream().init().agent_call().start().bundle()
    s.grep("anything", "denied", tool_use_id="toolu_g", is_error=True)
    s.stop().agent_result().final([{"tool_name": "Grep", "tool_use_id": "toolu_g"}])
    assert_outcome(evaluate(s, "simple"), "fail", 4, "simple_discovery:Grep")


# --- raw result 抽出と artifact 要約 ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["negative", "positive", "hybrid", "simple"])
def test_pipeline_extracted_raw_result_feeds_the_evaluator(kind: str) -> None:
    stream = normal_stream(kind)
    raw = NEW.extract_reviewer_raw_result(stream.text())
    assert raw is not None
    assert_outcome(evaluate(stream, kind, result=raw), "pass", 5)


def test_artifact_tool_use_summary_includes_grep_glob_and_is_sanitized() -> None:
    outcome = evaluate(normal_stream("negative"))
    records = [r for r in outcome["tool_use_records"] if r["name"] in ("Grep", "Glob", "Read")]
    grep = next(r for r in records if r["name"] == "Grep")
    assert set(grep["input_summary"]) == {"pattern", "path"} and grep["input_summary"]["path"] == "<ROOT>"
    dumped = json.dumps(outcome["tool_use_records"])
    assert ROOT not in dumped and "/home/" not in dumped
    home = Stream().init().agent_call().start()
    home.grep("fn_evaluator_zz", "/home/someone/secret/path.py", path=f"{ROOT}/x")
    summary = NEW.summarize_tool_uses(home.stop().agent_result().final().text(), ROOT, INV)
    assert "/home/" not in json.dumps(summary) and "<HOME>" in json.dumps(summary)
