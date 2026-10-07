"""Issue #2973 AC7 / AC8: path 未列挙 role の bounded discovery を判定する pure evaluator。

#2963 の `issue2963_reachability_runtime_evaluator.py`（変更しない）の scanner / helper を unique module 名で
読み込んで再利用し、bound / root scope / role 単位の因果 / query 関連性の predicate だけを追加する。
構造化 field（tool_use の `name` / `input`、tool_result の `is_error` / `content`、lifecycle record）だけで判定し、
自由文は解釈せず、LLM judge を使わない。新しい汎用 parser / runner / analyzer は持たない。

判定規則（最初に該当した規則で確定する）:

1. stream-json が得られない（unavailable）。
2. reviewer 区間の必須 tool 呼び出し（root / HEAD / target source の Read / discovery の Grep・Glob）または
   親 Agent tool_use が permission 拒否された、または session の tool pool に discovery tool（Grep / Glob）が
   存在しない（unavailable。構造化 permission_denials と `system init` の `tools` だけで判定する）。
3. lifecycle / reviewer 区間 / terminal completion（fail）。#2963 の規則 3 と同一判定。
4. 必須 tool 観測と bound（fail）。root -> HEAD -> role 単位の（direct Read | discovery -> target Read）。
5. verdict（fail / pass）。fixture 種別ごと。
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Issue 本文「固定する bound の値」（実装側の都合で変更しない。contract / static test と同一値）
# ---------------------------------------------------------------------------

DISCOVERY_SEARCH_CALL_MAX = 8  # reviewer 区間の Grep + Glob tool_use の合計（成功・失敗を問わず数える）
DISCOVERY_SOURCE_READ_MAX = 8  # reviewer 区間の Read のうち bundle.json と body_file 以外の合計
SEARCH_SCOPE = "repository_root_only"
DISCOVERY_TOOLS = ("Grep", "Glob")

FIXTURE_KINDS = ("negative", "positive", "hybrid", "simple")
SOURCE_ROLES = ("producer", "parser", "evaluator", "consumer")
_GAP_KINDS = ("negative", "hybrid")  # dataflow gap を持つ fixture（high|blocker finding を期待する）

_EVAL2963_NAME = "issue2963_reachability_runtime_evaluator"
_EVAL2963_PATH = Path(__file__).resolve().parent / "issue2963_reachability_runtime_evaluator.py"


def _load_2963() -> Any:
    """#2963 の evaluator を変更せず、既存 test と同じ module 名で一度だけ読み込む。"""
    cached = sys.modules.get(_EVAL2963_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_EVAL2963_NAME, _EVAL2963_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_EVAL2963_NAME] = module
    spec.loader.exec_module(module)
    return module


E63 = _load_2963()
EXIT_CODES = E63.EXIT_CODES
REVIEWER_AGENT = E63.REVIEWER_AGENT
iter_stream_events = E63.iter_stream_events
scan_tool_records = E63.scan_tool_records
load_runner_module = E63.load_runner_module
parse_raw_result_object = E63.parse_raw_result_object
extract_reviewer_raw_result = E63.extract_reviewer_raw_result
sanitize_text = E63.sanitize_text
summarize_lifecycle_records = E63.summarize_lifecycle_records
_SHA_RE = E63._SHA_RE
_verdict = E63._verdict
_normalize_ref = E63._normalize_ref
_high_ref_union = E63._high_ref_union
_is_root_command = E63._is_root_command
_is_head_command = E63._is_head_command
_read_target = E63._read_target
_reviewer_invocation = E63._reviewer_invocation
_reviewer_agent_tool_use_ids = E63._reviewer_agent_tool_use_ids


# ---------------------------------------------------------------------------
# fixture ground truth（#2963 の `fixture_paths` を拡張しない、本 Issue 用の別 metadata）
# ---------------------------------------------------------------------------

FIXTURES_RELATIVE = ".claude/skills/issue-refinement-loop/tests/fixtures"
# fixture 種別 -> (fixture directory, source file / symbol の接頭辞)
FIXTURE_DIRS = {
    "negative": ("bounded_discovery_negative_case", "qneg"),
    "positive": ("bounded_discovery_positive_control_case", "qpos"),
    "hybrid": ("bounded_discovery_hybrid_case", "qhyb"),
    "simple": ("bounded_discovery_simple_docs_only_case", None),
}
_ROLE_NAMES = {
    "producer": ("collect_{p}_vc_rows", "{p}_producer"),
    "parser": ("split_{p}_vc_line", "{p}_parser"),
    "evaluator": ("judge_{p}_vc_requirement", "{p}_evaluator"),
    "consumer": ("route_{p}_vc_decision", "{p}_consumer"),
}
_HYBRID_LISTED = ("producer", "parser")


def fixture_roles(kind: str) -> dict[str, dict[str, Any]]:
    """role -> `{path, symbol, file_fragment, listed, decoy_path}`。simple は role を持たない（空）。"""
    directory, prefix = FIXTURE_DIRS[kind]
    if prefix is None:
        return {}
    roles: dict[str, dict[str, Any]] = {}
    for role, (symbol, fragment) in _ROLE_NAMES.items():
        decoy = f"{FIXTURES_RELATIVE}/{directory}/decoy/qneg_stale_evaluator.py"
        roles[role] = {
            "path": f"{FIXTURES_RELATIVE}/{directory}/{fragment.format(p=prefix)}.py",
            "symbol": symbol.format(p=prefix),
            "file_fragment": fragment.format(p=prefix),
            "listed": kind == "hybrid" and role in _HYBRID_LISTED,
            "decoy_path": decoy if (kind == "negative" and role == "evaluator") else None,
        }
    return roles


def fixture_allowed_read_paths(kind: str) -> list[str]:
    """simple fixture が Read してよい repository file（bound の対象外の bundle.json / body_file は含まない）。"""
    if kind != "simple":
        return []
    directory, _prefix = FIXTURE_DIRS[kind]
    return [f"{FIXTURES_RELATIVE}/{directory}/qsimple_glossary.md"]


# ---------------------------------------------------------------------------
# 小さな predicate
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _under_root(path: str, root: str) -> bool:
    """正規化後の `path` が resolved root 配下（root 自身を含む）にあるか。`..` による脱出は偽になる。"""
    root_n = _norm(root)
    path_n = _norm(path)
    return path_n == root_n or path_n.startswith(root_n.rstrip("/") + "/")


def _exempt_read(tool_use: dict[str, Any], invocation_dir: str, body_file: str) -> bool:
    """bound の対象外にする Read: `<invocation_dir>` 直下の `bundle.json` と `body_file` の 2 file のみ。"""
    if tool_use["name"] != "Read":
        return False
    file_path = tool_use["input"].get("file_path")
    if not isinstance(file_path, str):
        return False
    base = invocation_dir.rstrip("/")
    return _norm(file_path) in (_norm(f"{base}/bundle.json"), _norm(f"{base}/{body_file}"))


def _read_path_is(tool_use: dict[str, Any], resolved_root: str, repo_relative: str) -> bool:
    return _read_target(tool_use, resolved_root, repo_relative)


def search_scope_violation(tool_use: dict[str, Any], resolved_root: str) -> str | None:
    """Grep / Glob の search scope 違反（`path` 省略・相対 path・root 外・`..` 脱出・root 外 pattern）の理由。"""
    path = tool_use["input"].get("path")
    if not isinstance(path, str) or not path.strip():
        return "search_path_omitted"
    if not os.path.isabs(path):
        return "search_path_not_absolute"
    if not _under_root(path, resolved_root):
        return "search_outside_root"
    if tool_use["name"] == "Glob":
        pattern = tool_use["input"].get("pattern")
        if isinstance(pattern, str) and pattern:
            candidate = pattern if os.path.isabs(pattern) else f"{path.rstrip('/')}/{pattern}"
            if not _under_root(candidate, resolved_root):
                return "search_outside_root"
    return None


def _query_text(tool_use: dict[str, Any]) -> str:
    pattern = tool_use["input"].get("pattern")
    return pattern if isinstance(pattern, str) else ""


def _query_matches_role(tool_use: dict[str, Any], role: dict[str, Any]) -> bool:
    """Grep は named symbol、Glob は file 名断片を `pattern` に含むこと（その role に関連する検索）。"""
    key = "symbol" if tool_use["name"] == "Grep" else "file_fragment"
    needle = role.get(key)
    return bool(needle) and needle in _query_text(tool_use)


def result_lists_path(text: str, resolved_root: str, repo_relative: str) -> bool:
    """検索結果 text に `repo_relative` が「hit した file の path」として現れるか。

    path で始まる行（Grep の files_with_matches / `path:line:content` / count、Glob の path 一覧。root からの
    相対でも絶対でもよい）だけを数える。他 file の本文中に literal として現れる（body・test・evaluator の自己参照）
    だけの行は数えない。"""
    root_prefix = resolved_root.rstrip("/") + "/"
    pattern = re.compile(re.escape(repo_relative) + r"(?:$|[:\-\s])")
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(root_prefix):
            line = line[len(root_prefix) :]
        while line.startswith("./"):
            line = line[2:]
        if pattern.match(line):
            return True
    return False


def _module_name(repo_relative: str) -> str:
    return Path(repo_relative).stem


def text_mentions_module(text: str, repo_relative: str) -> bool:
    """既に Read した source の結果 text に `repo_relative` の module 名が語として現れるか（import / call-site）。"""
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(_module_name(repo_relative))}(?![A-Za-z0-9_])", text or "") is not None
    )


# ---------------------------------------------------------------------------
# 規則 1-3（#2963 の規則 1-3 と同一判定。drift は parity test が検出する）
# ---------------------------------------------------------------------------


def _required_read_paths(fixture_roles: dict[str, dict[str, Any]] | None) -> list[str]:
    return [role["path"] for role in (fixture_roles or {}).values() if role.get("path")]


def missing_discovery_tools(events: list[dict[str, Any]]) -> list[str]:
    """session の `system init` event の `tools`（構造化 field）に存在しない discovery tool。

    `init` に `tools` が無い stream では判定できないため空（= 判定しない）を返す。Claude Code の native build は
    Grep / Glob を既定の tool pool から外し、embedded な bfs / ugrep を Bash 経由で提供する場合がある。その
    runtime では reviewer が Grep / Glob を呼べず、discovery は検証不能（unavailable であり PASS ではない）。"""
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init" and isinstance(event.get("tools"), list):
            return [tool for tool in DISCOVERY_TOOLS if tool not in event["tools"]]
    return []


def evaluate_rules_1_3(
    *,
    stdout: str,
    resolved_root: str,
    invocation_dir: str,
    fixture_kind: str,
    fixture_roles: dict[str, dict[str, Any]] | None,
    claude_unavailable_reason: str | None = None,
    terminal_incomplete_reason: str | None = None,
    require_discovery_tools: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """規則 1-3 を評価する。(確定した outcome | None, 規則 4 以降が使う context) を返す。"""
    events = iter_stream_events(stdout)
    if claude_unavailable_reason or not events:
        return _verdict("unavailable", 1, claude_unavailable_reason or "no stream-json events captured"), {}

    if require_discovery_tools:
        missing_tools = missing_discovery_tools(events)
        if missing_tools:
            return (
                _verdict(
                    "unavailable",
                    2,
                    f"discovery tool(s) {missing_tools} are absent from the session tool pool (system init `tools`)",
                    missing_discovery_tools=missing_tools,
                ),
                {},
            )

    runner = load_runner_module()
    tool_uses, results = scan_tool_records(events)
    denials = runner.extract_claude_permission_denials(stdout)
    lifecycle = runner.extract_claude_hook_lifecycle_events(stdout)
    agent_tool_ids = _reviewer_agent_tool_use_ids(tool_uses)

    invocation = _reviewer_invocation(lifecycle)
    starts = invocation["start_indexes"]
    stops = invocation["stop_indexes"]
    interval: list[dict[str, Any]] = []
    lifecycle_ok = (
        len(invocation["start_ids"]) == 1
        and invocation["start_ids"] == invocation["stop_ids"]
        and not any(e.get("contradictory") for e in lifecycle)
        and max(starts) < min(stops)
        and len(agent_tool_ids) == 1
    )
    if lifecycle_ok:
        parent_id = next(iter(agent_tool_ids))
        interval = [
            tu
            for tu in tool_uses
            if tu["parent_tool_use_id"] == parent_id and min(starts) < tu["stream_index"] < max(stops)
        ]

    required_reads = _required_read_paths(fixture_roles) if fixture_kind != "simple" else []

    def _required_observation(tool_use: dict[str, Any]) -> bool:
        if fixture_kind == "simple":
            return False
        if _is_root_command(tool_use, invocation_dir) or _is_head_command(tool_use, resolved_root):
            return True
        if tool_use["name"] in DISCOVERY_TOOLS:
            return True
        return any(_read_path_is(tool_use, resolved_root, rel) for rel in required_reads)

    def _denied(tool_use: dict[str, Any]) -> bool:
        for denial in denials:
            if not isinstance(denial, dict):
                continue
            if denial.get("tool_use_id"):
                if denial["tool_use_id"] == tool_use["id"]:
                    return True
            elif denial.get("tool_name") == tool_use["name"] and denial.get("tool_input") == tool_use["input"]:
                return True
        return False

    denied_targets = [tu for tu in tool_uses if tu["id"] in agent_tool_ids and _denied(tu)]
    denied_targets += [tu for tu in interval if _required_observation(tu) and _denied(tu)]
    if denied_targets:
        return (
            _verdict(
                "unavailable",
                2,
                "permission denial recorded for a parent Agent or required observation tool_use",
                denied_tool_use_ids=[tu["id"] for tu in denied_targets],
            ),
            {},
        )

    if len(agent_tool_ids) != 1:
        return (
            _verdict(
                "fail", 3, f"expected exactly 1 parent Agent tool_use for {REVIEWER_AGENT}, got {len(agent_tool_ids)}"
            ),
            {},
        )
    parent_result = results.get(next(iter(agent_tool_ids)))
    if parent_result is None or parent_result["is_error"]:
        return _verdict("fail", 3, "parent Agent tool_result is missing or is_error"), {}
    if not lifecycle_ok:
        return (
            _verdict(
                "fail",
                3,
                "reviewer SubagentStart/SubagentStop must be exactly one non-contradictory pair "
                "with the same agent_id (Start before Stop)",
                reviewer_start_agent_ids=sorted(invocation["start_ids"]),
                reviewer_stop_agent_ids=sorted(invocation["stop_ids"]),
            ),
            {},
        )
    if not interval:
        return _verdict("fail", 3, "no reviewer-attributed tool_use inside the SubagentStart/SubagentStop interval"), {}
    if terminal_incomplete_reason:
        return _verdict("fail", 3, f"terminal completion not reached: {terminal_incomplete_reason}"), {}

    context = {
        "tool_uses": tool_uses,
        "results": results,
        "interval": interval,
        "lifecycle": {
            "agent_id": next(iter(invocation["start_ids"])),
            "start_stream_index": min(starts),
            "stop_stream_index": max(stops),
        },
    }
    return None, context


def terminal_incomplete_reason(stdout: str, timed_out: bool) -> str | None:
    """terminal completion に至らなかった理由（timeout / turn limit / error result）。完了していれば None。

    最後の `result` event が `subtype: success` かつ非 error であることだけを構造化 field で判定する。"""
    if timed_out:
        return "timed out before terminal completion"
    last: dict[str, Any] | None = None
    for event in iter_stream_events(stdout):
        if event.get("type") == "result":
            last = event
    if last is None:
        return "no terminal result event"
    if last.get("subtype") != "success" or last.get("is_error"):
        return f"terminal result subtype={last.get('subtype')!r} is_error={bool(last.get('is_error'))}"
    return None


# ---------------------------------------------------------------------------
# 規則 4: 必須 tool 観測 / bound / scope / 因果
# ---------------------------------------------------------------------------


def _unresolved_roles(fixture_roles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {name: role for name, role in fixture_roles.items() if not role.get("listed")}


def _evaluate_simple_observations(
    *,
    interval: list[dict[str, Any]],
    resolved_root: str,
    invocation_dir: str,
    body_file: str,
    allowed_read_paths: list[str],
) -> list[str]:
    violations: list[str] = []
    for tool_use in interval:
        if tool_use["name"] in DISCOVERY_TOOLS:
            violations.append(f"simple_discovery:{tool_use['name']}")
        elif tool_use["name"] == "Read" and not _exempt_read(tool_use, invocation_dir, body_file):
            if not any(_read_path_is(tool_use, resolved_root, rel) for rel in allowed_read_paths):
                violations.append("simple_source_read")
    return violations


def _evaluate_discovery_observations(
    *,
    interval: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    tested_head: str,
    resolved_root: str,
    invocation_dir: str,
    body_file: str,
    fixture_roles: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    """root -> HEAD -> role 単位の（direct Read | discovery -> target Read）と bound を検証する。"""
    violations: list[str] = []
    by_index = sorted(interval, key=lambda tu: tu["stream_index"])

    def _ok_results(predicate: Any) -> list[dict[str, Any]]:
        found = []
        for tool_use in by_index:
            result = results.get(tool_use["id"])
            if predicate(tool_use) and result is not None and not result["is_error"]:
                found.append({"tool_use": tool_use, "result": result})
        return found

    roots = [
        item
        for item in _ok_results(lambda tu: _is_root_command(tu, invocation_dir))
        if item["result"]["text"].strip() and E63._same_path(item["result"]["text"].strip(), resolved_root)
    ]
    heads = [
        item
        for item in _ok_results(lambda tu: _is_head_command(tu, resolved_root))
        if item["result"]["text"].strip() == tested_head
    ]
    if not roots:
        violations.append("root_resolution_missing")
    if not heads:
        violations.append("head_resolution_missing")
    head_use_index: int | None = None
    if roots and heads:
        root_result_index = min(r["result"]["stream_index"] for r in roots)
        later = [h for h in heads if h["tool_use"]["stream_index"] > root_result_index]
        if later:
            head_use_index = min(h["tool_use"]["stream_index"] for h in later)
        else:
            violations.append("head_before_root")

    # --- bound / scope / query 関連性 -------------------------------------------------------------------------
    searches = [tu for tu in by_index if tu["name"] in DISCOVERY_TOOLS]
    source_reads = [tu for tu in by_index if tu["name"] == "Read" and not _exempt_read(tu, invocation_dir, body_file)]
    if len(searches) > DISCOVERY_SEARCH_CALL_MAX:
        violations.append(f"search_bound_exceeded:{len(searches)}>{DISCOVERY_SEARCH_CALL_MAX}")
    if len(source_reads) > DISCOVERY_SOURCE_READ_MAX:
        violations.append(f"read_bound_exceeded:{len(source_reads)}>{DISCOVERY_SOURCE_READ_MAX}")
    unresolved = _unresolved_roles(fixture_roles)
    for tool_use in searches:
        scope = search_scope_violation(tool_use, resolved_root)
        if scope:
            violations.append(f"{scope}:{tool_use['name']}")
        if not any(_query_matches_role(tool_use, role) for role in unresolved.values()):
            violations.append(f"irrelevant_query:{tool_use['name']}")
    if head_use_index is not None:
        for tool_use in [*searches, *source_reads]:
            if tool_use["stream_index"] <= head_use_index:
                violations.append(f"before_head:{tool_use['name']}")
                break

    # --- role 単位の帰属（direct Read | (a) 成功した discovery の hit | (b) import 由来） -----------------------
    successful_searches = _ok_results(lambda tu: tu["name"] in DISCOVERY_TOOLS)
    role_state: dict[str, dict[str, Any]] = {}
    read_texts: list[tuple[int, str]] = []  # (result stream_index, text) of successfully Read target sources
    discovered: dict[str, bool] = {}
    for name, role in fixture_roles.items():
        reads = [tu for tu in by_index if _read_path_is(tu, resolved_root, role["path"])]
        state = {"listed": bool(role.get("listed")), "read_attempts": len(reads), "satisfied": False, "via": None}
        role_state[name] = state
        discovered[name] = any(
            _query_matches_role(item["tool_use"], role)
            and result_lists_path(item["result"]["text"], resolved_root, role["path"])
            for item in successful_searches
        )
    # Read を stream 順に処理し、(b) 用の text を積み上げる。
    all_target_reads = sorted(
        (
            (tu["stream_index"], name, tu)
            for name, role in fixture_roles.items()
            for tu in by_index
            if _read_path_is(tu, resolved_root, role["path"])
        ),
        key=lambda item: item[0],
    )
    for read_index, name, tool_use in all_target_reads:
        role = fixture_roles[name]
        result = results.get(tool_use["id"])
        succeeded = result is not None and not result["is_error"]
        if role.get("listed"):
            justified, via = True, "direct_read"
        else:
            via = None
            for item in successful_searches:
                if (
                    item["result"]["stream_index"] < read_index
                    and _query_matches_role(item["tool_use"], role)
                    and result_lists_path(item["result"]["text"], resolved_root, role["path"])
                ):
                    via = "discovery"
                    break
            if via is None:
                for text_index, text in read_texts:
                    if text_index < read_index and text_mentions_module(text, role["path"]):
                        via = "import"
                        break
            justified = via is not None
            if not justified:
                violations.append(f"read_before_discovery:{name}")
        if succeeded:
            if justified and not role_state[name]["satisfied"]:
                role_state[name]["satisfied"] = True
                role_state[name]["via"] = via
            read_texts.append((result["stream_index"], result["text"]))
    for name, state in role_state.items():
        if not state["satisfied"]:
            violations.append(f"missing_target_read:{name}")

    # 少なくとも 1 件、未解決 role の target path を含む成功した discovery が必要（import 導出だけで済ませない）。
    if unresolved and not any(discovered[name] for name in unresolved):
        violations.append("no_successful_discovery")

    evidence = {
        "search_calls": len(searches),
        "source_reads": len(source_reads),
        "search_call_max": DISCOVERY_SEARCH_CALL_MAX,
        "source_read_max": DISCOVERY_SOURCE_READ_MAX,
        "roles": {
            name: {"listed": s["listed"], "satisfied": s["satisfied"], "via": s["via"]}
            for name, s in role_state.items()
        },
    }
    return sorted(set(violations)), evidence


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def evaluate_bounded_discovery(
    *,
    stdout: str,
    tested_head: str,
    resolved_root: str,
    invocation_dir: str,
    fixture_kind: str,
    fixture_roles: dict[str, dict[str, Any]] | None,
    raw_result: Any,
    allowed_read_paths: list[str] | None = None,
    body_file: str = "body.md",
    claude_unavailable_reason: str | None = None,
    terminal_incomplete: str | None = None,
) -> dict[str, Any]:
    """AC7 判定規則 1-5 をこの順で評価する（最初に該当した規則で確定する）。"""
    if fixture_kind not in FIXTURE_KINDS:
        return _verdict("fail", 5, f"unknown fixture_kind: {fixture_kind!r}")
    if fixture_kind != "simple" and not fixture_roles:
        return _verdict("fail", 5, f"fixture_roles is required for fixture_kind {fixture_kind!r}")

    outcome, context = evaluate_rules_1_3(
        stdout=stdout,
        resolved_root=resolved_root,
        invocation_dir=invocation_dir,
        fixture_kind=fixture_kind,
        fixture_roles=fixture_roles,
        claude_unavailable_reason=claude_unavailable_reason,
        terminal_incomplete_reason=terminal_incomplete,
        require_discovery_tools=fixture_kind != "simple",
    )
    if outcome is not None:
        return outcome

    interval = context["interval"]
    results = context["results"]
    roles = fixture_roles or {}
    if fixture_kind == "simple":
        violations = _evaluate_simple_observations(
            interval=interval,
            resolved_root=resolved_root,
            invocation_dir=invocation_dir,
            body_file=body_file,
            allowed_read_paths=list(allowed_read_paths or []),
        )
        observation: dict[str, Any] = {
            "search_calls": sum(1 for tu in interval if tu["name"] in DISCOVERY_TOOLS),
        }
    else:
        violations, observation = _evaluate_discovery_observations(
            interval=interval,
            results=results,
            tested_head=tested_head,
            resolved_root=resolved_root,
            invocation_dir=invocation_dir,
            body_file=body_file,
            fixture_roles=roles,
        )
    evidence = {
        "lifecycle": context["lifecycle"],
        "interval_tool_uses": [
            {"id": tu["id"], "name": tu["name"], "stream_index": tu["stream_index"]} for tu in interval
        ],
        "discovery": observation,
        "violations": violations,
    }
    if violations:
        return _verdict("fail", 4, f"bounded discovery observation violated: {violations}", **evidence)

    # 規則 5: verdict（構造化 field のみ。曖昧さは FAIL）。
    parsed = parse_raw_result_object(raw_result)
    if (
        parsed is None
        or parsed.get("assessment") not in ("clear", "findings")
        or not isinstance(parsed.get("findings"), list)
    ):
        return _verdict("fail", 5, "raw result is not a structured assessment/findings object", **evidence)
    assessment = parsed["assessment"]
    findings = parsed["findings"]
    union, high_count = _high_ref_union(findings)
    if union is None:
        return _verdict("fail", 5, "high|blocker finding has malformed evidence_refs", **evidence)
    normalized = [_normalize_ref(ref, resolved_root) for ref in union]

    if fixture_kind in _GAP_KINDS:
        if assessment != "findings" or high_count < 1:
            return _verdict(
                "fail",
                5,
                f"{fixture_kind}: expected assessment findings with at least one high|blocker finding",
                **evidence,
            )
        missing = [name for name, role in roles.items() if not any(role["path"] in ref for ref in normalized)]
        if missing:
            return _verdict(
                "fail", 5, f"{fixture_kind}: high|blocker evidence_refs union lacks target paths {missing}", **evidence
            )
        if tested_head not in {token for ref in union for token in _SHA_RE.findall(ref)}:
            return _verdict(
                "fail", 5, f"{fixture_kind}: high|blocker evidence_refs union lacks tested_head", **evidence
            )
        return _verdict(
            "pass",
            5,
            f"{fixture_kind}: dataflow gap reported with HEAD and the discovered/direct target source paths",
            **evidence,
        )

    if fixture_kind == "positive":
        if high_count:
            return _verdict("fail", 5, "positive: high|blocker finding present (must be 0)", **evidence)
        return _verdict("pass", 5, "positive: no high|blocker finding (observation verified by rule 4)", **evidence)

    if high_count:
        return _verdict("fail", 5, "simple: unexpected high|blocker finding for a docs-only Issue", **evidence)
    return _verdict("pass", 5, "simple: no discovery, no source Read, no high|blocker finding", **evidence)


# ---------------------------------------------------------------------------
# artifact 用の sanitized な要約（診断用。判定規則には使わない）
# ---------------------------------------------------------------------------


def summarize_tool_uses(stdout: str, resolved_root: str = "", invocation_dir: str = "") -> list[dict[str, Any]]:
    """tool_use の sanitized な要約。reviewer 帰属の Bash / Read / Grep / Glob は input を sanitized で添える。"""
    tool_uses, results = scan_tool_records(iter_stream_events(stdout))
    reviewer_agent_ids = _reviewer_agent_tool_use_ids(tool_uses)
    summary: list[dict[str, Any]] = []
    for tu in tool_uses:
        record: dict[str, Any] = {
            "id": tu["id"],
            "name": tu["name"],
            "parent_tool_use_id": tu["parent_tool_use_id"],
            "stream_index": tu["stream_index"],
        }
        result = results.get(tu["id"])
        record["result_is_error"] = None if result is None else result["is_error"]
        if tu["parent_tool_use_id"] in reviewer_agent_ids:
            tool_input = tu["input"]
            if tu["name"] == "Bash" and isinstance(tool_input.get("command"), str):
                record["input_summary"] = sanitize_text(tool_input["command"], resolved_root, invocation_dir)
                if result is not None:
                    record["stdout_head"] = sanitize_text(result["text"], resolved_root, invocation_dir)[:200]
            elif tu["name"] == "Read" and isinstance(tool_input.get("file_path"), str):
                record["input_summary"] = sanitize_text(tool_input["file_path"], resolved_root, invocation_dir)
            elif tu["name"] in DISCOVERY_TOOLS:
                record["input_summary"] = {
                    "pattern": sanitize_text(str(tool_input.get("pattern", "")), resolved_root, invocation_dir),
                    "path": sanitize_text(str(tool_input.get("path", "<omitted>")), resolved_root, invocation_dir),
                }
                if result is not None:
                    record["result_head"] = sanitize_text(result["text"], resolved_root, invocation_dir)[:300]
        summary.append(record)
    return summary


def evaluate_bounded_discovery_runtime(**kwargs: Any) -> dict[str, Any]:
    """判定に、診断用の sanitized な lifecycle / tool_use 要約を添えて返す。"""
    outcome = evaluate_bounded_discovery(**kwargs)
    stdout = kwargs.get("stdout") or ""
    if iter_stream_events(stdout):
        outcome["lifecycle_records"] = summarize_lifecycle_records(stdout)
        outcome["tool_use_records"] = summarize_tool_uses(
            stdout, kwargs.get("resolved_root") or "", kwargs.get("invocation_dir") or ""
        )
    return outcome
