"""#1679: target-only executor boundary tests.

`implement-issue` / `open-pr` の production path から peer OPEN Issue の
overlap preflight（全件収集・semantic overlap route・overlap evidence
chain）を撤去し、target Issue、canonical repository、worktree、実 diff、
実 test、target PR、current-head CI、独立 review、human stop だけを
実行判断入力として残すことを検証する（#1860 Owner Decision）。

AC1・AC2・AC9 は Runtime Verification Applicability の `decision: immediate`
対象であり、fake `gh` executable を実際に PATH へ配置して subprocess として
起動される argv を記録・検証する（静的 grep ではなく実際の起動有無を確認）。
fake `gh` executable を PATH に配置できない実行環境では SKIP（exit 77）と
する。

証明範囲の精確化（#1679 Major 1）: `open-pr`（`open_pr.py`）は独立した
Python エントリポイントを持つため、本ファイルの AC1/AC2 テストは実際の
subprocess 起動を fake `gh` で観測する runtime behavior test として成立する。
一方 `implement-issue` は SKILL.md に記述された手順であり、独立した
決定論的 command execution boundary（薄い executor）を持たないため、
`implement-issue` 側の AC1/AC2 は checker（`check_implementation_overlap.py`）
の削除確認と `tests/agent_ops/test_implement_issue_overlap_policy.py` の
repository-wide static token inventory（forbidden overlap token の不在確認）
によって証明する。両者を合わせて AC1/AC2 の証明範囲とする。
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OPEN_PR_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "open-pr" / "scripts"
IMPLEMENT_ISSUE_DIR = REPO_ROOT / ".claude" / "skills" / "implement-issue"
IMPL_REVIEW_LOOP_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts"

if str(OPEN_PR_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(OPEN_PR_SCRIPTS_DIR))
if str(IMPL_REVIEW_LOOP_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(IMPL_REVIEW_LOOP_SCRIPTS_DIR))

import open_pr  # noqa: E402
from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402


# ---------------------------------------------------------------------------
# Fake `gh` executable harness (AC1 / AC2 Runtime Verification Applicability)
# ---------------------------------------------------------------------------

_FAKE_GH_SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import re
import sys

log_path = os.environ.get("FAKE_GH_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")

args = sys.argv[1:]


def _out(text):
    sys.stdout.write(text)


if len(args) >= 2 and args[0] == "issue" and args[1] == "view":
    fields = ""
    if "--json" in args:
        fields = args[args.index("--json") + 1]
    payload = {}
    if "state" in fields:
        payload["state"] = "OPEN"
    if "labels" in fields:
        payload["labels"] = []
    if "body" in fields:
        payload["body"] = ""
    if "url" in fields:
        payload["url"] = ""
    _out(json.dumps(payload))
elif len(args) >= 2 and args[0] == "api" and args[1] == "graphql":
    # #2815: target PR closing relation observation (0 closing issues =>
    # NO_LINK), so open_pr never starts the real Task Context `signal apply`.
    # 汎用 GraphQL evaluator ではない。critical binding
    # (repository(owner:$owner,name:$name) / pullRequest(number:$number)) が
    # 成立する query にだけ、-F 変数値から identity を返す。
    fields = {}
    for idx, arg in enumerate(args[:-1]):
        if arg in ("-f", "-F", "--field", "--raw-field"):
            key, _, value = args[idx + 1].partition("=")
            fields[key] = value
    query = fields.get("query", "")

    def _arg_map(field):
        bodies = re.findall(r"\b" + field + r"\s*\(([^()]*)\)", query)
        if len(bodies) != 1:
            return None
        return dict(re.findall(r'([A-Za-z_]\w*)\s*:\s*(\$?\w+|"[^"]*")', bodies[0]))

    bound = (
        _arg_map("repository") == {"owner": "$owner", "name": "$name"}
        and _arg_map("pullRequest") == {"number": "$number"}
        and fields.get("owner")
        and fields.get("name")
        and fields.get("number", "").isdigit()
    )
    if not bound:
        sys.stderr.write(
            "fake gh: graphql query does not bind repository(owner:$owner,name:$name) "
            "and pullRequest(number:$number) to -F variables\n"
        )
        sys.exit(1)
    _out(json.dumps({"data": {"repository": {
        "nameWithOwner": fields["owner"] + "/" + fields["name"],
        "pullRequest": {"number": int(fields["number"]), "closingIssuesReferences": {"nodes": []}},
    }}}))
elif len(args) >= 1 and args[0] == "api":
    target = args[1] if len(args) > 1 else ""
    full_name = target[len("repos/"):] if target.startswith("repos/") else "unknown/unknown"
    _out(json.dumps({"full_name": full_name}))
elif len(args) >= 2 and args[0] == "pr" and args[1] == "list":
    _out("[]")
elif len(args) >= 2 and args[0] == "pr" and args[1] == "create":
    _out("https://github.com/example/repo/pull/999\n")
else:
    _out("{}")

sys.exit(0)
'''


def _install_fake_gh(tmp_path: Path) -> Path:
    """Write an executable fake `gh` to tmp_path/bin and return the bin dir.

    Raises OSError if the platform cannot make the script executable (used
    by callers to SKIP with exit 77, per Runtime Verification Applicability
    skip_conditions).
    """
    bin_dir = tmp_path / "fake-gh-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh_path = bin_dir / "gh"
    gh_path.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    current_mode = gh_path.stat().st_mode
    gh_path.chmod(current_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    if not os.access(gh_path, os.X_OK):
        raise OSError(f"fake gh executable is not executable: {gh_path}")
    return bin_dir


def _fake_gh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    try:
        bin_dir = _install_fake_gh(tmp_path)
    except OSError:
        pytest.exit("SKIP: fake gh executable unavailable in this environment", returncode=77)
    log_path = tmp_path / "fake_gh_log.ndjson"
    log_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log_path


def _read_fake_gh_log(log_path: Path) -> list[list[str]]:
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _write_temp_body(tmp_path: Path) -> Path:
    body_path = tmp_path / "pr-body.md"
    body_path.write_text("# PR body\n\nplaceholder body for boundary tests.\n", encoding="utf-8")
    return body_path


def _run_open_pr_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked_issue: int = 9999) -> int:
    """Run open_pr.main() in-process with real subprocess calls to the fake
    `gh` executable (only body/japanese validators and git-remote resolution
    are monkeypatched to keep the test deterministic and offline)."""
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "example/repo")
    monkeypatch.setattr(open_pr, "resolve_branch", lambda: f"worktree-issue-{linked_issue}-test")
    monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
    monkeypatch.setattr(
        open_pr,
        "_run_pr_body_validator",
        lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
    )
    monkeypatch.setattr(
        open_pr,
        "_run_japanese_content_validator",
        lambda body_text, threshold=0.1: {
            "status": "pass",
            "failed_blocks": 0,
            "aggregate_ratio": 0.5,
            "threshold": 0.1,
            "body_sha256": "",
            "stderr": "",
        },
    )
    body_path = _write_temp_body(tmp_path)
    return open_pr.main(
        [
            "--pr-title", "feat: boundary test",
            "--linked-issue", str(linked_issue),
            "--publish", "yes",
            "--pr-body-file", str(body_path),
        ]
    )


# ---------------------------------------------------------------------------
# AC1: no peer inventory / search / GraphQL subprocess call
# ---------------------------------------------------------------------------


_FAKE_GH_PR_NUMBER = "999"  # fake `gh pr create` が返す PR 番号

# #2815: peer Issue inventory / search / pagination / peer readback を示す禁止 token。
# target PR 自身の bounded closing relation observation（GraphQL transport）は
# 許可し、transport 名ではなく query の意味で禁止対象を判定する。
_FORBIDDEN_PEER_TOKENS = (
    "issues(",
    "states:",
    "search",
    "issue list",
    "--paginate",
    "pageinfo",
    "after:",
    "endcursor",
    "dependenc",
    "comments",
    "labels",
    "body",
)

# closing relation query の selection set に現れてよい識別子（Issue number と
# repository{nameWithOwner} の relation identity に限る）。
_ALLOWED_RELATION_QUERY_IDENTIFIERS = frozenset(
    {
        "repository",
        "owner",
        "name",
        "nameWithOwner",
        "pullRequest",
        "number",
        "closingIssuesReferences",
        "first",
        "excludeUserLinked",
        "userLinkedOnly",
        "nodes",
        "false",
    }
)

_GH_FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field")
_GQL_ARGUMENT_RE = re.compile(r'([A-Za-z_]\w*)\s*:\s*(\$?\w+|"[^"]*")')
_GQL_OPERATION_HEADER_RE = re.compile(r"^\s*query\b(?:\s+[A-Za-z_]\w*)?\s*(?:\([^()]*\))?")


def _balanced_brace_body(text: str, open_brace_index: int) -> str:
    """`open_brace_index` の `{` から対応する `}` までの内側を返す。"""
    assert text[open_brace_index] == "{"
    depth = 0
    for pos in range(open_brace_index, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_index + 1 : pos]
    raise AssertionError(f"unbalanced braces from index {open_brace_index}: {text}")


def _gh_field_arguments(call: list[str]) -> dict[str, list[tuple[str, str]]]:
    """`-f/-F key=value` を `{key: [(flag, value), ...]}` に分解する。"""
    fields: dict[str, list[tuple[str, str]]] = {}
    for idx, arg in enumerate(call[:-1]):
        if arg in _GH_FIELD_FLAGS:
            key, _, value = call[idx + 1].partition("=")
            fields.setdefault(key, []).append((arg, value))
    return fields


def _graphql_field_arguments(query: str, field: str) -> dict[str, str]:
    """`field(...)` の引数本文を `{name: value}` に分解する（整形・引数順は問わない）。

    `field(` は query 内でちょうど 1 回だけ現れ、引数本文は `name: value` の並び
    （重複なし・解釈不能な残余なし）でなければならない。"""
    bodies = re.findall(rf"\b{field}\s*\(([^()]*)\)", query)
    assert len(bodies) == 1, f"exactly one {field}(...) call expected: {query}"
    body = bodies[0]
    leftover = _GQL_ARGUMENT_RE.sub("", body)
    assert not leftover.strip(" ,\t\r\n"), f"unparseable {field}(...) argument text {leftover!r}: {query}"
    pairs = _GQL_ARGUMENT_RE.findall(body)
    names = [name for name, _ in pairs]
    assert len(set(names)) == len(names), f"duplicate {field}(...) argument: {body}"
    return dict(pairs)


def _assert_bounded_target_pr_relation_call(
    call: list[str],
    *,
    expected_pr_number: str,
    expected_owner: str = "example",
    expected_name: str = "repo",
) -> None:
    """present lane の GraphQL 呼び出しが target PR 起点の bounded observation
    であり、peer Issue inventory / search / readback を含まないことを検証する。

    証明する意味（整形・引数順・named operation は固定しない）:
    - `repository(owner: $owner, name: $name)` と `pullRequest(number: $number)` に
      変数が束縛され、その変数へ canonical repository / actual PR number が `-F` で渡る
      （owner/name の入れ替えや repository・PR 番号の hardcode は拒否）
    - `closingIssuesReferences` は `first: 2` の bounded observation
    - relation nodes の取得 field は Issue `number` と `repository{nameWithOwner}` のみ"""
    fields = _gh_field_arguments(call)
    assert len(fields.get("query", [])) == 1, f"exactly one query= argument expected: {call}"
    query_flag, query = fields["query"][0]
    assert query_flag in ("-f", "--raw-field", "-F", "--field"), call
    joined = " ".join(call).lower()

    for token in _FORBIDDEN_PEER_TOKENS:
        assert token not in joined, f"forbidden peer-inventory token {token!r} in graphql call: {call}"

    # canonical target repository / actual target PR number は -F 変数として渡す
    expected_variables = {"owner": expected_owner, "name": expected_name, "number": expected_pr_number}
    for variable, expected_value in expected_variables.items():
        assert fields.get(variable) == [("-F", expected_value)], (
            f"variable {variable}={expected_value} must be passed exactly once with -F: {call}"
        )

    # critical binding edge: 変数が実際に引数位置で使われていること
    assert _graphql_field_arguments(query, "repository") == {"owner": "$owner", "name": "$name"}, (
        f"repository(owner:$owner,name:$name) binding expected: {query}"
    )
    assert _graphql_field_arguments(query, "pullRequest") == {"number": "$number"}, (
        f"pullRequest(number:$number) binding expected: {query}"
    )
    header = _GQL_OPERATION_HEADER_RE.match(query)
    assert header is not None, f"query operation expected: {query}"
    for variable in expected_variables:
        assert re.search(rf"\${variable}\s*:", header.group(0)), f"${variable} must be declared: {query}"

    # bounded closing relation observation（first: 2 のみ。cursor / last 等は不可）
    relation_args = _graphql_field_arguments(query, "closingIssuesReferences")
    assert relation_args.get("first") == "2", f"bounded first:2 expected: {query}"
    assert set(relation_args) <= {"first", "excludeUserLinked", "userLinkedOnly"}, (
        f"unexpected closingIssuesReferences argument(s): {sorted(relation_args)}"
    )

    # 取得 field は Issue `number` と `repository{nameWithOwner}` に限る
    nodes_markers = list(re.finditer(r"\bnodes\s*\{", query))
    assert len(nodes_markers) == 1, f"single relation nodes selection expected: {query}"
    nodes_body = _balanced_brace_body(query, nodes_markers[0].end() - 1)
    assert re.findall(r"[A-Za-z_]\w*", nodes_body) == ["number", "repository", "nameWithOwner"], (
        f"relation nodes must select only number and repository{{nameWithOwner}}: {nodes_body}"
    )
    selection = query[header.end() :]
    identifiers = set(re.findall(r"[A-Za-z_]\w*", selection))
    unexpected = identifiers - _ALLOWED_RELATION_QUERY_IDENTIFIERS
    assert not unexpected, f"unexpected field(s) selected in relation query: {sorted(unexpected)}"


# REST `gh api` の peer OPEN Issue inventory / search surface（collection・search）だけを
# 拒否する。target-local API（`repos/<owner>/<repo>` の resolve、target PR 系）は許可する。
_GH_API_VALUE_FLAGS = frozenset(
    {
        "-f", "-F", "--field", "--raw-field", "-H", "--header", "-X", "--method", "--jq", "-q",
        "-t", "--template", "--input", "--hostname", "--cache", "-p", "--preview",
    }
)
_PEER_REST_INVENTORY_RE = re.compile(
    r"^(?:"
    r"repos/[^/]+/[^/]+/issues(?:/(?:comments|events))?"  # repo Issue collection / repo-wide comments・events
    r"|(?:user|orgs/[^/]+)/issues"  # user / org Issue inventory
    r"|issues"  # authenticated user Issue inventory
    r"|search(?:/.*)?"  # search surface
    r")$"
)


def _gh_api_rest_endpoint(call: list[str]) -> str | None:
    """`gh api <endpoint> ...` の endpoint を正規化して返す（GraphQL / 非 api は None）。"""
    if call[:1] != ["api"]:
        return None
    endpoint = None
    skip_next = False
    for arg in call[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in _GH_API_VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        endpoint = arg
        break
    if endpoint is None or endpoint == "graphql":
        return None
    endpoint = re.sub(r"^https?://[^/]+/", "", endpoint)
    endpoint = endpoint.split("?", 1)[0].split("#", 1)[0].strip("/")
    return endpoint.lower()


def _assert_no_peer_rest_inventory_call(call: list[str]) -> None:
    endpoint = _gh_api_rest_endpoint(call)
    if endpoint is None:
        return
    assert not _PEER_REST_INVENTORY_RE.match(endpoint), (
        f"peer Issue inventory / search REST surface must not be called: {call}"
    )


def _assert_no_peer_inventory_call(call: list[str]) -> None:
    """両 lane の全 subprocess call に適用する peer inventory / search / pagination 禁止。"""
    joined = " ".join(call).lower()
    assert call[:2] != ["issue", "list"], f"peer Issue inventory call must not happen: {call}"
    assert "issue list" not in joined, f"peer Issue inventory call must not happen: {call}"
    assert call[:1] != ["search"], f"Issue search call must not happen: {call}"
    assert "--paginate" not in call, f"pagination must not happen: {call}"
    _assert_no_peer_rest_inventory_call(call)


@pytest.mark.parametrize("session_bound", [False, True])
def test_no_peer_inventory_or_search_from_target_only_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_bound: bool
) -> None:
    # lane は外側の shell 環境ではなくテスト自身が確定する（#2815）。
    if session_bound:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "boundary-test-session")
    else:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    log_path = _fake_gh_env(tmp_path, monkeypatch)
    rc = _run_open_pr_main(tmp_path, monkeypatch)
    assert rc == 0
    calls = _read_fake_gh_log(log_path)
    assert calls, "expected at least one gh subprocess call"

    # peer inventory / search / pagination は両 lane で禁止（transport 名ではなく意味で判定）。
    for call in calls:
        _assert_no_peer_inventory_call(call)

    graphql_calls = [call for call in calls if call[:2] == ["api", "graphql"]]
    if not session_bound:
        assert graphql_calls == [], f"unbound lane must not call api graphql: {graphql_calls}"
        return

    assert len(graphql_calls) == 1, f"exactly one target PR relation observation expected: {graphql_calls}"
    _assert_bounded_target_pr_relation_call(graphql_calls[0], expected_pr_number=_FAKE_GH_PR_NUMBER)


@pytest.mark.parametrize(
    "call",
    [
        ["api", "repos/example/repo/issues", "-f", "state=open"],
        ["api", "/repos/example/repo/issues"],
        ["api", "repos/example/repo/issues/"],
        ["api", "/repos/example/repo/issues/"],
        ["api", "repos/example/repo/issues?state=open&per_page=100"],
        ["api", "-X", "GET", "-H", "Accept: application/json", "repos/example/repo/issues"],
        ["api", "https://api.github.com/repos/example/repo/issues"],
        ["api", "repos/example/repo/issues/comments"],
        ["api", "repos/example/repo/issues/events"],
        ["api", "search/issues", "-f", "q=repo:example/repo is:issue is:open"],
        ["api", "/search/issues?q=repo:example/repo"],
        ["api", "search/code", "-f", "q=x"],
        ["api", "orgs/example/issues"],
        ["api", "user/issues"],
        ["api", "issues"],
    ],
)
def test_rest_peer_issue_inventory_call_is_rejected(call: list[str]) -> None:
    with pytest.raises(AssertionError):
        _assert_no_peer_inventory_call(call)


@pytest.mark.parametrize(
    "call",
    [
        ["api", "repos/example/repo"],  # canonical repository resolve
        ["api", "/repos/example/repo/"],
        ["api", "repos/example/repo/pulls/999"],  # target PR
        ["api", "repos/example/repo/pulls/999/reviews", "-H", "Accept: application/json"],
        ["api", "repos/example/repo/commits/abc/check-runs"],
        ["issue", "view", "9999", "--json", "state"],
        ["pr", "list", "--head", "branch", "--json", "url"],
        ["pr", "create", "--title", "search issues report"],  # 文字列としての "search" は許可
    ],
)
def test_target_local_rest_and_pr_calls_are_not_rejected(call: list[str]) -> None:
    _assert_no_peer_inventory_call(call)


@pytest.fixture
def production_relation_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """実 production（`open_pr.py`）が発行する present lane の GraphQL 呼び出しを取得する。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "boundary-test-session")
    log_path = _fake_gh_env(tmp_path, monkeypatch)
    assert _run_open_pr_main(tmp_path, monkeypatch) == 0
    graphql_calls = [call for call in _read_fake_gh_log(log_path) if call[:2] == ["api", "graphql"]]
    assert len(graphql_calls) == 1, graphql_calls
    return graphql_calls[0]


def _replace_query(call: list[str], pattern: str, replacement: str, *, many: bool = False) -> list[str]:
    """`query=` 引数へ regex 置換を適用する（既定は 1 回だけ。適用されない場合は失敗）。"""
    result = []
    applied = 0
    for arg in call:
        if arg.startswith("query="):
            arg, count = re.subn(pattern, replacement, arg, flags=re.MULTILINE)
            applied += count
        result.append(arg)
    assert applied >= 1 and (many or applied == 1), (
        f"mutation pattern must apply {'at least' if many else 'exactly'} once (applied={applied}): {pattern}"
    )
    return result


_REPOSITORY_ARGS = r"(\brepository\s*\()[^()]*\)"
_NODES_OPEN = r"(\bnodes\s*\{\s*number)"

_REJECTED_MUTATIONS = {
    "first-100": (r"(closingIssuesReferences\s*\(\s*first\s*:\s*)2\b", r"\g<1>100"),
    "pageinfo": (r"(\bnodes\s*\{)", r"pageInfo{endCursor} \g<1>"),
    "body": (_NODES_OPEN, r"\g<1> body"),
    "labels": (_NODES_OPEN, r"\g<1> labels{nodes{name}}"),
    "comments": (_NODES_OPEN, r"\g<1> comments{totalCount}"),
    "search": (r"(\brepository\s*\([^()]*\))", r'\g<1> search(query:"x")'),
    "issues-states": (r"(\bpullRequest\s*\()", r"issues(states:OPEN){nodes{number}} \g<1>"),
    "after-cursor": (r"(closingIssuesReferences\s*\()", r"\g<1>after:$cursor,"),
    "unlisted-field": (_NODES_OPEN, r"\g<1> trackedIssues{totalCount}"),
    "swap-owner-name": (_REPOSITORY_ARGS, r"\g<1>owner:$name,name:$owner)"),
    "hardcoded-pr-number": (r"(\bpullRequest\s*\(\s*number\s*:\s*)\$number", r"\g<1>999"),
    "hardcoded-repository": (_REPOSITORY_ARGS, r'\g<1>owner:"example",name:"repo")'),
}


def test_production_relation_call_satisfies_assertion(production_relation_call: list[str]) -> None:
    _assert_bounded_target_pr_relation_call(production_relation_call, expected_pr_number=_FAKE_GH_PR_NUMBER)


@pytest.mark.parametrize("mutation_id", list(_REJECTED_MUTATIONS))
def test_relation_call_assertion_rejects_peer_inventory_mutations(
    production_relation_call: list[str], mutation_id: str
) -> None:
    """present lane の semantic assertion が、peer inventory 混入や binding 破壊を
    現 production query へ加えると実際に失敗する（vacuous でない）ことを示す。"""
    pattern, replacement = _REJECTED_MUTATIONS[mutation_id]
    mutated = _replace_query(production_relation_call, pattern, replacement)
    with pytest.raises(AssertionError):
        _assert_bounded_target_pr_relation_call(mutated, expected_pr_number=_FAKE_GH_PR_NUMBER)


# 意味が同一の整形差（false-negative にしてはならない）。
_EQUIVALENT_FORMATTING_VARIANTS = {
    "space-before-paren": [
        (r"\brepository\(", "repository ("),
        (r"\bpullRequest\(", "pullRequest ("),
        (r"\bclosingIssuesReferences\(", "closingIssuesReferences ("),
    ],
    "space-around-colon": [
        (r"\bowner:\$owner", "owner : $owner"),
        (r"\bname:\$name", "name : $name"),
        (r"\bnumber:\$number", "number : $number"),
        (r"\bfirst:2", "first : 2"),
    ],
    "named-operation": [(r"query=query\(", "query=query Foo(")],
    "argument-order-swap": [(_REPOSITORY_ARGS, r"\g<1>name:$name,owner:$owner)")],
    "multiline": [(r"([{},])", "\\g<1>\n  ", True)],
    "space-before-nodes-brace": [(r"\bnodes\{", "nodes {")],
}


@pytest.mark.parametrize("variant_id", list(_EQUIVALENT_FORMATTING_VARIANTS))
def test_relation_call_assertion_accepts_equivalent_formatting(
    production_relation_call: list[str], variant_id: str
) -> None:
    """binding / bound=2 / selected fields が同一なら整形差で落ちない（harness friction 除去）。"""
    call = production_relation_call
    for pattern, replacement, *rest in _EQUIVALENT_FORMATTING_VARIANTS[variant_id]:
        call = _replace_query(call, pattern, replacement, many=bool(rest))
    _assert_bounded_target_pr_relation_call(call, expected_pr_number=_FAKE_GH_PR_NUMBER)


def _run_fake_gh_graphql(call: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *call], capture_output=True, text=True, timeout=10)


def test_fake_gh_graphql_requires_critical_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production_relation_call: list[str]
) -> None:
    """fake `gh` は critical binding が成立する query にだけ、-F 変数値から
    identity を返す（query が間違った repository を指しても正しい identity を捏造しない）。"""
    ok = _run_fake_gh_graphql(production_relation_call)
    assert ok.returncode == 0, ok.stderr
    repository = json.loads(ok.stdout)["data"]["repository"]
    assert repository["nameWithOwner"] == "example/repo"
    assert repository["pullRequest"]["number"] == int(_FAKE_GH_PR_NUMBER)

    reformatted = _replace_query(production_relation_call, r"\bowner:\$owner", "owner : $owner")
    assert _run_fake_gh_graphql(reformatted).returncode == 0

    for mutation_id in ("swap-owner-name", "hardcoded-pr-number", "hardcoded-repository"):
        pattern, replacement = _REJECTED_MUTATIONS[mutation_id]
        rejected = _run_fake_gh_graphql(_replace_query(production_relation_call, pattern, replacement))
        assert rejected.returncode != 0, f"fake gh must not fabricate identity for {mutation_id}"
        assert "does not bind" in rejected.stderr
        assert rejected.stdout == ""


# ---------------------------------------------------------------------------
# AC2: no peer Issue body/comments/native dependency read
# ---------------------------------------------------------------------------


def test_no_peer_issue_body_comments_or_native_dependency_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = _fake_gh_env(tmp_path, monkeypatch)
    rc = _run_open_pr_main(tmp_path, monkeypatch)
    assert rc == 0
    calls = _read_fake_gh_log(log_path)
    assert calls, "expected at least one gh subprocess call"
    for call in calls:
        joined = " ".join(call)
        assert "dependencies" not in joined, f"native dependency endpoint must not be read: {call}"
        assert "--json" not in call or "body,url" not in call, (
            f"peer/self contract-snapshot body,url readback must not happen: {call}"
        )
        assert "--json" not in call or "labels" not in call, (
            f"label-forced overlap gate readback must not happen: {call}"
        )
        # `gh issue comment` (posting overlap evidence/warning to Issues)
        # must not happen from open_pr.py's production path.
        assert not (len(call) >= 2 and call[0] == "issue" and call[1] == "comment"), (
            f"overlap warning comment posting must not happen: {call}"
        )


# ---------------------------------------------------------------------------
# AC3: check_implementation_overlap.py not invoked from production path
# ---------------------------------------------------------------------------


def test_check_implementation_overlap_not_invoked_from_production_path() -> None:
    script = (
        IMPLEMENT_ISSUE_DIR / "scripts" / "check_implementation_overlap.py"
    )
    assert not script.exists(), f"checker script must be deleted: {script}"

    open_pr_source = (OPEN_PR_SCRIPTS_DIR / "open_pr.py").read_text(encoding="utf-8")
    assert "check_implementation_overlap" not in open_pr_source

    skill_source = (IMPLEMENT_ISSUE_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "check_implementation_overlap" not in skill_source


# ---------------------------------------------------------------------------
# AC6: overlap evidence/route/digest/collection-contract/waiver/warning
# producer/consumer removed from production
# ---------------------------------------------------------------------------


def test_overlap_evidence_route_digest_waiver_warning_removed_from_production() -> None:
    open_pr_source = (OPEN_PR_SCRIPTS_DIR / "open_pr.py").read_text(encoding="utf-8")
    forbidden_tokens = (
        "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1",
        "overlap_preflight",
        "E_OVERLAP_PREFLIGHT_",
        "OVERLAP_PREFLIGHT_WARNING",
        "overlap_readback_waiver",
        "run_overlap_preflight_gate",
        "post_overlap_warning_comment",
        "FORCE_OVERLAP_PREFLIGHT_LABEL",
    )
    for token in forbidden_tokens:
        assert token not in open_pr_source, f"forbidden overlap token still present in open_pr.py: {token}"

    assert not hasattr(open_pr, "run_overlap_preflight_gate")
    assert not hasattr(open_pr, "post_overlap_warning_comment")
    assert not hasattr(open_pr, "fetch_current_linked_issue_labels")

    parser = open_pr.parse_args(
        [
            "--pr-title", "t",
            "--linked-issue", "1",
            "--publish", "yes",
            "--pr-body-file", "/tmp/does-not-matter.md",
        ]
    )
    assert not hasattr(parser, "overlap_preflight_required")
    assert not hasattr(parser, "overlap_preflight_evidence_file")


# ---------------------------------------------------------------------------
# AC9: only mergeable == CONFLICTING or merge_state_status == DIRTY are
# treated as an actual GitHub conflict.
# ---------------------------------------------------------------------------


def _reviewer_verdict(head_sha: str = "deadbeef") -> dict:
    return {
        "verdict": "APPROVE",
        "reviewed_head_sha": head_sha,
        "blockers": [],
        "warnings": [],
    }


def test_only_conflicting_or_dirty_mergestatus_treated_as_actual_conflict() -> None:
    conflicting = route_loop_verdict_v2(
        _reviewer_verdict(),
        {"head_sha": "deadbeef", "mergeable": "CONFLICTING", "merge_state_status": "CLEAN"},
    )
    assert conflicting.route == "conflict_hard_stop"

    dirty = route_loop_verdict_v2(
        _reviewer_verdict(),
        {"head_sha": "deadbeef", "mergeable": "MERGEABLE", "merge_state_status": "DIRTY"},
    )
    assert dirty.route == "conflict_hard_stop"

    non_conflict_statuses = ("UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE")
    for status in non_conflict_statuses:
        decision = route_loop_verdict_v2(
            _reviewer_verdict(),
            {"head_sha": "deadbeef", "mergeable": "MERGEABLE", "merge_state_status": status},
        )
        assert decision.route != "conflict_hard_stop", (
            f"merge_state_status={status} must not be misclassified as a conflict: {decision}"
        )

    unknown_mergeable = route_loop_verdict_v2(
        _reviewer_verdict(),
        {"head_sha": "deadbeef", "mergeable": "UNKNOWN", "merge_state_status": "CLEAN"},
    )
    assert unknown_mergeable.route != "conflict_hard_stop"


# ---------------------------------------------------------------------------
# AC10: checker (check_implementation_overlap.py) is completely removed and
# #1652 is superseded/not planned by this Issue.
# ---------------------------------------------------------------------------


def test_check_implementation_overlap_script_and_tests_are_removed() -> None:
    scripts_dir = IMPLEMENT_ISSUE_DIR / "scripts"
    tests_dir = IMPLEMENT_ISSUE_DIR / "tests"

    deleted_scripts = (
        "check_implementation_overlap.py",
        "verify_overlap_pagination_runtime.py",
    )
    for name in deleted_scripts:
        assert not (scripts_dir / name).exists(), f"must be deleted: {scripts_dir / name}"

    deleted_tests = (
        "test_check_implementation_overlap.py",
        "test_check_implementation_overlap_false_positive_signals.py",
        "test_check_implementation_overlap_native_dependencies.py",
        "test_check_implementation_overlap_pagination.py",
        "test_check_implementation_overlap_repository_binding.py",
        "test_check_implementation_overlap_successor_dependency.py",
    )
    for name in deleted_tests:
        assert not (tests_dir / name).exists(), f"must be deleted: {tests_dir / name}"

    open_pr_tests_dir = OPEN_PR_SCRIPTS_DIR / "tests"
    deleted_open_pr_tests = (
        "test_open_pr_overlap_gate.py",
        "test_open_pr_overlap_collection_contract.py",
        "test_open_pr_overlap_warning_persistence.py",
        "test_open_pr_contract_bound_waiver.py",
    )
    for name in deleted_open_pr_tests:
        assert not (open_pr_tests_dir / name).exists(), f"must be deleted: {open_pr_tests_dir / name}"

    issue_1679_body = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    # This is a repo-local structural check only; #1652 disposition itself
    # (superseded/not planned close) is a GitHub-side action performed by
    # the human/orchestrator after this PR merges, not something this
    # repository-local test can observe. We assert only the in-repo
    # artifact removal here.
    assert issue_1679_body.returncode == 0


# ---------------------------------------------------------------------------
# AC11: legacy overlap token inventory is zero outside the explicit
# allowlist (create-issue's ISSUE_OVERLAP_PREFLIGHT_RESULT_V1 asset).
# ---------------------------------------------------------------------------

_LEGACY_OVERLAP_TOKENS = (
    "check_implementation_overlap",
    "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1",
    "overlap_preflight",
    "E_OVERLAP_PREFLIGHT_",
    "OVERLAP_PREFLIGHT_WARNING",
    "overlap_readback_waiver",
)

# The create-issue skill's ISSUE_OVERLAP_PREFLIGHT_RESULT_V1 pure classifier
# is an explicitly out-of-scope, separate asset (per Issue #1679 "#1652 との
# 関係" section) and is not part of this token inventory. It is allowlisted
# both by path (create-issue skill directory) and by literal schema-name
# substring (references to it from other docs/skills, e.g. workflow.md).
_ALLOWLISTED_PATHS = (
    REPO_ROOT / ".claude" / "skills" / "create-issue",
)
_ALLOWLISTED_SUBSTRINGS = ("ISSUE_OVERLAP_PREFLIGHT_RESULT_V1",)

# This enforcement test file, and the sibling policy test file, necessarily
# reference the forbidden tokens as literal strings in order to assert their
# absence elsewhere. They are enforcement tooling, not production code or
# documentation, and are excluded from the scan of themselves.
_SELF_EXCLUDED_FILES = (
    Path(__file__).resolve(),
    Path(__file__).resolve().parent / "test_implement_issue_overlap_policy.py",
    REPO_ROOT
    / ".claude"
    / "skills"
    / "open-pr"
    / "scripts"
    / "tests"
    / "test_open_pr_overlap_removal.py",
)

_SCAN_ROOTS = (
    REPO_ROOT / ".claude" / "skills" / "implement-issue",
    REPO_ROOT / ".claude" / "skills" / "open-pr",
    REPO_ROOT / "tests" / "agent_ops",
    REPO_ROOT / "docs" / "dev" / "workflow.md",
)


def _iter_scan_files():
    for root in _SCAN_ROOTS:
        if root.is_file():
            yield root
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(str(path).startswith(str(allowlisted)) for allowlisted in _ALLOWLISTED_PATHS):
                continue
            if path.resolve() in _SELF_EXCLUDED_FILES:
                continue
            if ".git" in path.parts:
                continue
            yield path


def test_legacy_overlap_token_inventory_is_zero_outside_manual_only_allowlist() -> None:
    offenders: list[str] = []
    for path in _iter_scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for allowlisted_substring in _ALLOWLISTED_SUBSTRINGS:
            text = text.replace(allowlisted_substring, "")
        for token in _LEGACY_OVERLAP_TOKENS:
            if token in text:
                offenders.append(f"{path}: {token}")
    assert not offenders, "legacy overlap tokens found outside allowlist:\n" + "\n".join(offenders)
