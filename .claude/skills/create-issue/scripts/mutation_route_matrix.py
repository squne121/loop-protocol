"""
mutation_route_matrix.py

公開 mutation route の SSOT（Single Source of Truth）。

各 route の public_side_effect / validation / action を定義し、
guard-japanese-prose.sh の thin wrapper がこのモジュールから route 分類を受け取る。

Body Source 7 種 enum:
- api_raw_field_body_literal:       -f body=<literal>
- api_field_body_literal:           -F body=<literal>
- api_field_body_file:              -F body=@<file>
- api_field_body_stdin:             -F body=@- (stdin fail-closed)
- api_input_json_file:              --input <file> (JSON)
- api_input_json_stdin:             --input - (stdin fail-closed)
- api_input_non_json_or_invalid:    --input <file> (non-JSON)

Deny Reason Codes:
- deny_invalid_json
- deny_missing_body_for_public_body_route
- deny_null_body_public_mutation
- deny_empty_body_public_mutation
- deny_unreadable_body_file
- deny_stdin_body_uninspectable
- deny_graphql_mutation_unsupported
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


# ============================================================
# Body Source enum constants
# ============================================================

BODY_SOURCE_RAW_FIELD_LITERAL = "api_raw_field_body_literal"
BODY_SOURCE_FIELD_LITERAL = "api_field_body_literal"
BODY_SOURCE_FIELD_FILE = "api_field_body_file"
BODY_SOURCE_FIELD_STDIN = "api_field_body_stdin"
BODY_SOURCE_INPUT_JSON_FILE = "api_input_json_file"
BODY_SOURCE_INPUT_JSON_STDIN = "api_input_json_stdin"
BODY_SOURCE_INPUT_NON_JSON = "api_input_non_json_or_invalid"

# ============================================================
# Deny Reason Codes
# ============================================================

DENY_INVALID_JSON = "deny_invalid_json"
DENY_MISSING_BODY = "deny_missing_body_for_public_body_route"
DENY_NULL_BODY = "deny_null_body_public_mutation"
DENY_EMPTY_BODY = "deny_empty_body_public_mutation"
DENY_UNREADABLE_FILE = "deny_unreadable_body_file"
DENY_STDIN_BODY = "deny_stdin_body_uninspectable"
DENY_GRAPHQL = "deny_graphql_mutation_unsupported"


# ============================================================
# MutationRoute dataclass
# ============================================================

@dataclass(frozen=True)
class MutationRoute:
    """公開 mutation route の定義。各フィールドは SSOT として機能する。"""
    route_id: str
    public_side_effect: bool
    validation: str          # 'body_inspect' | 'conservative_deny' | 'none'
    action: str              # 'inspect_body' | 'deny' | 'pass'
    method: str | None = None
    endpoint_pattern: str | None = None


# ============================================================
# Route Matrix（SSOT）
# ============================================================

ROUTE_MATRIX: list[MutationRoute] = [
    MutationRoute(
        route_id="gh_issue_create",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
    ),
    MutationRoute(
        route_id="gh_issue_edit",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
    ),
    MutationRoute(
        route_id="gh_issue_comment",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
    ),
    MutationRoute(
        route_id="gh_pr_create",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
    ),
    MutationRoute(
        route_id="gh_pr_edit",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
    ),
    MutationRoute(
        route_id="gh_pr_comment",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
    ),
    MutationRoute(
        route_id="gh_pr_review",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
    ),
    MutationRoute(
        route_id="rest_issue_body_patch",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="PATCH",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/issues/\d+$",
    ),
    MutationRoute(
        route_id="rest_issue_body_post",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="POST",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/issues$",
    ),
    MutationRoute(
        route_id="rest_pr_body_patch",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="PATCH",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/pulls/\d+$",
    ),
    MutationRoute(
        route_id="rest_pr_body_post",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="POST",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/pulls$",
    ),
    MutationRoute(
        route_id="rest_issue_comment_post",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="POST",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/issues/\d+/comments$",
    ),
    MutationRoute(
        route_id="rest_issue_comment_patch",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="PATCH",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/issues/comments/\d+$",
    ),
    MutationRoute(
        route_id="rest_pr_review_comment_patch",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="PATCH",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/pulls/comments/\d+$",
    ),
    MutationRoute(
        route_id="rest_pr_review_comment_post",
        public_side_effect=True,
        validation="body_inspect",
        action="inspect_body",
        method="POST",
        endpoint_pattern=r"^repos/[^/]+/[^/]+/pulls/\d+/comments$",
    ),
    MutationRoute(
        route_id="graphql_mutation_phase1",
        public_side_effect=True,
        validation="conservative_deny",
        action="deny",
    ),
    MutationRoute(
        route_id="tmp_draft_write_edit",
        public_side_effect=False,
        validation="none",
        action="pass",
    ),
]


# ============================================================
# Route Lookup
# ============================================================

def get_route(route_id: str) -> MutationRoute | None:
    """route_id で route を取得する。"""
    for route in ROUTE_MATRIX:
        if route.route_id == route_id:
            return route
    return None


def classify_rest_endpoint(endpoint: str, method: str) -> MutationRoute | None:
    """
    REST endpoint と HTTP method から一致する route を返す。

    endpoint の leading slash は除去して照合する。
    一致する route がない場合は None（guard 対象外）。
    """
    ep = endpoint.lstrip("/")
    method_upper = method.upper() if method else ""

    for route in ROUTE_MATRIX:
        if route.endpoint_pattern is None:
            continue
        if route.method and route.method.upper() != method_upper:
            continue
        if re.match(route.endpoint_pattern, ep):
            return route
    return None


# ============================================================
# Body Source Resolution
# ============================================================

@dataclass
class BodySourceResult:
    """body source 解決結果。"""
    source_kind: str
    body_text: str | None
    deny_reason: str | None
    file_path: str | None


def resolve_body_source(command: str) -> BodySourceResult:
    """
    gh api コマンドから body source を解決する。

    優先順位:
    1. --input 指定 → input file を body source（-f/-F は query param 扱い）
    2. -F body=@<file> → ファイル内容を読んで検査
    3. -F body=@- → stdin fail-closed
    4. -F body=<literal> → literal として検査
    5. -f body=<literal> → literal として検査（@ dereference なし）
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_NON_JSON,
            body_text=None,
            deny_reason=DENY_INVALID_JSON,
            file_path=None,
        )

    input_file = _extract_input_flag(tokens)
    if input_file == "-":
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_JSON_STDIN,
            body_text=None,
            deny_reason=DENY_STDIN_BODY,
            file_path=None,
        )
    if input_file is not None:
        return _resolve_input_file_source(input_file)

    field_result = _extract_field_body(tokens, capitalize=True)
    if field_result is not None:
        flag_value, is_file_ref = field_result
        if is_file_ref and flag_value == "-":
            return BodySourceResult(
                source_kind=BODY_SOURCE_FIELD_STDIN,
                body_text=None,
                deny_reason=DENY_STDIN_BODY,
                file_path=None,
            )
        if is_file_ref:
            return _resolve_field_file_source(flag_value)
        return BodySourceResult(
            source_kind=BODY_SOURCE_FIELD_LITERAL,
            body_text=flag_value,
            deny_reason=None,
            file_path=None,
        )

    raw_result = _extract_field_body(tokens, capitalize=False)
    if raw_result is not None:
        flag_value, _ = raw_result
        return BodySourceResult(
            source_kind=BODY_SOURCE_RAW_FIELD_LITERAL,
            body_text=flag_value,
            deny_reason=None,
            file_path=None,
        )

    return BodySourceResult(
        source_kind=BODY_SOURCE_RAW_FIELD_LITERAL,
        body_text=None,
        deny_reason=None,
        file_path=None,
    )


def _extract_input_flag(tokens: list[str]) -> str | None:
    """--input <file> または --input=<file> の値を抽出する。"""
    for i, tok in enumerate(tokens):
        if tok == "--input" and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--input="):
            return tok[len("--input="):]
    return None


def _extract_field_body(tokens: list[str], capitalize: bool) -> tuple[str, bool] | None:
    """
    -F body=<value> (capitalize=True) または -f body=<value> (capitalize=False) を抽出する。

    Returns (value, is_file_ref) または None。
    is_file_ref は capitalize=True かつ value が '@' で始まる場合 True。
    """
    flag = "-F" if capitalize else "-f"
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            next_tok = tokens[i + 1]
            if next_tok.startswith("body="):
                raw_value = next_tok[5:]
                if capitalize and raw_value.startswith("@"):
                    return raw_value[1:], True
                return raw_value, False
    return None


def _resolve_input_file_source(input_file: str) -> BodySourceResult:
    """--input <file> の JSON を解析して body source を返す。"""
    try:
        with open(input_file, encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, IOError, json.JSONDecodeError):
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_NON_JSON,
            body_text=None,
            deny_reason=DENY_INVALID_JSON,
            file_path=input_file,
        )

    if not isinstance(payload, dict):
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_NON_JSON,
            body_text=None,
            deny_reason=DENY_INVALID_JSON,
            file_path=input_file,
        )

    if "body" not in payload:
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_JSON_FILE,
            body_text=None,
            deny_reason=DENY_MISSING_BODY,
            file_path=input_file,
        )

    body = payload["body"]
    if body is None:
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_JSON_FILE,
            body_text=None,
            deny_reason=DENY_NULL_BODY,
            file_path=input_file,
        )
    if not isinstance(body, str):
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_NON_JSON,
            body_text=None,
            deny_reason=DENY_INVALID_JSON,
            file_path=input_file,
        )
    if body == "":
        return BodySourceResult(
            source_kind=BODY_SOURCE_INPUT_JSON_FILE,
            body_text=None,
            deny_reason=DENY_EMPTY_BODY,
            file_path=input_file,
        )

    return BodySourceResult(
        source_kind=BODY_SOURCE_INPUT_JSON_FILE,
        body_text=body,
        deny_reason=None,
        file_path=input_file,
    )


def _resolve_field_file_source(file_path: str) -> BodySourceResult:
    """-F body=@<file> のファイル内容を読んで返す。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8")
    except (FileNotFoundError, IOError, PermissionError):
        return BodySourceResult(
            source_kind=BODY_SOURCE_FIELD_FILE,
            body_text=None,
            deny_reason=DENY_UNREADABLE_FILE,
            file_path=file_path,
        )

    if content == "":
        return BodySourceResult(
            source_kind=BODY_SOURCE_FIELD_FILE,
            body_text=None,
            deny_reason=DENY_EMPTY_BODY,
            file_path=file_path,
        )

    return BodySourceResult(
        source_kind=BODY_SOURCE_FIELD_FILE,
        body_text=content,
        deny_reason=None,
        file_path=file_path,
    )


# ============================================================
# GraphQL Classification
# ============================================================

def classify_graphql_command(command: str) -> str:
    """
    gh api graphql コマンドを Phase 1 conservative deny で分類する。

    Returns:
        DENY_GRAPHQL:           mutation キーワードを含む
        'graphql_not_mutation': query / subscription
        'graphql_no_input':    --input なし
        DENY_STDIN_BODY:        --input -
        DENY_INVALID_JSON:      JSON parse 失敗
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return DENY_INVALID_JSON

    input_file = _extract_input_flag(tokens)
    if input_file is None:
        return "graphql_no_input"
    if input_file == "-":
        return DENY_STDIN_BODY

    try:
        with open(input_file, encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, IOError, json.JSONDecodeError):
        return DENY_INVALID_JSON

    if not isinstance(payload, dict):
        return DENY_INVALID_JSON

    query = payload.get("query", "")
    if not isinstance(query, str):
        return DENY_INVALID_JSON

    if "mutation" in query.lower():
        return DENY_GRAPHQL

    return "graphql_not_mutation"


# ============================================================
# Tmp Draft Path Classification
# ============================================================

_TMP_RELATIVE_RE = re.compile(r'^tmp/.*\.md$')
_TMP_NESTED_RE = re.compile(r'^(.+/)?tmp/.*\.md$')
_DRAFT_SUFFIX_RE = re.compile(r'.*_draft\.md$')
_TMP_ABS_ISSUE_PR_RE = re.compile(
    r'^/tmp/.*(?:issue.*body|pr.*body|comment|draft).*\.md$',
    re.IGNORECASE,
)


def is_tmp_draft_path(file_path: str) -> bool:
    """
    file_path が tmp 下書きパスかどうかを判定する（公開副作用なし → pass）。
    """
    if _TMP_RELATIVE_RE.match(file_path):
        return True
    if _TMP_NESTED_RE.match(file_path):
        return True
    if _DRAFT_SUFFIX_RE.match(file_path):
        return True
    if _TMP_ABS_ISSUE_PR_RE.match(file_path):
        return True
    return False


# ============================================================
# CLI
# ============================================================

def main() -> None:
    import sys as _sys
    import argparse

    parser = argparse.ArgumentParser(description="mutation_route_matrix.py CLI")
    sub = parser.add_subparsers(dest="cmd")

    rp = sub.add_parser("classify-rest")
    rp.add_argument("endpoint")
    rp.add_argument("--method", default="")

    bp = sub.add_parser("resolve-body-source")
    bp.add_argument("command")

    gp = sub.add_parser("classify-graphql")
    gp.add_argument("command")

    args = parser.parse_args()

    if args.cmd == "classify-rest":
        route = classify_rest_endpoint(args.endpoint, args.method)
        print("no_match" if route is None else route.route_id)

    elif args.cmd == "resolve-body-source":
        result = resolve_body_source(args.command)
        print(json.dumps({
            "source_kind": result.source_kind,
            "body_text": result.body_text,
            "deny_reason": result.deny_reason,
            "file_path": result.file_path,
        }))

    elif args.cmd == "classify-graphql":
        print(classify_graphql_command(args.command))

    else:
        parser.print_help()
        _sys.exit(1)


if __name__ == "__main__":
    main()
