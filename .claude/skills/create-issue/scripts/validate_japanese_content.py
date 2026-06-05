#!/usr/bin/env python3
"""
validate_japanese_content.py

Markdown の prose block から日本語文字比率を検査するスクリプト。
code fence / inline code / URL / CLI コマンドを除外して検査する。

Exit codes:
  0 = pass (日本語比率が閾値以上)
  1 = fail (日本語比率が閾値未満)
"""

import argparse
import re
import sys
from dataclasses import dataclass

import prose_boundary_policy as _pbp

# GraphQL body mutation keywords (B1: #594 blocker fix)
# gh api graphql --input payload.json の query にこれらが含まれる場合は body mutation として deny する
GRAPHQL_BODY_MUTATION_KEYWORDS = [
    "updateIssue", "updatePullRequest", "updateIssueComment",
    "addComment", "createIssue", "createPullRequest",
]


# 日本語文字の Unicode range
HIRAGANA_RE = re.compile(r'[぀-ゟ]')
KATAKANA_RE = re.compile(r'[゠-ヿ]')
CJK_RE = re.compile(r'[一-鿿]')

JAPANESE_RE = re.compile(r'[぀-ゟ゠-ヿ一-鿿]')

# コードフェンスパターン（``` または ~~~）
CODE_FENCE_RE = re.compile(r'^```.*?^```|^~~~.*?^~~~', re.MULTILINE | re.DOTALL)

# インラインコードパターン
INLINE_CODE_RE = re.compile(r'`[^`\n]+`')

# URL パターン
URL_RE = re.compile(r'https?://[^\s\)>\]"]+')

# Markdown リンクのURLパーツ
MD_LINK_URL_RE = re.compile(r'\[([^\]]*)\]\([^\)]+\)')

# CLI コマンド / シェルスクリプト行 ($ / # で始まる行、または bash/sh コマンド)
CLI_LINE_RE = re.compile(r'^\s*[$#]\s+\S.*$', re.MULTILINE)

# Markdown heading
HEADING_RE = re.compile(r'^#{1,6}\s+(.+)$', re.MULTILINE)

# 空白文字（スペース、タブ、改行）
WHITESPACE_RE = re.compile(r'\s+')

# 技術識別子パターン: _・.・- のいずれかを含む snake_case / kebab-case / file.ext 形式のみ対象
# 通常の英単語（特殊文字なし）は除外しない
IDENTIFIER_RE = re.compile(r'\b[a-zA-Z][a-zA-Z0-9]*(?:[_.\-][a-zA-Z0-9._\-]*)+\b')


@dataclass
class ValidationResult:
    """バリデーション結果"""
    passed: bool
    aggregate_ratio: float
    prose_blocks: list
    failed_blocks: list
    total_chars: int
    japanese_chars: int
    threshold: float


def extract_code_fences(text: str) -> tuple[str, list[str]]:
    """コードフェンスを取り除いてテキストと取り除いた部分を返す。

    prose_boundary_policy.iter_markdown_blocks() の GFM 準拠セグメンテーションに委譲し、
    独自 non-greedy fence regex（triple_backtick / tilde_fence）を撤去している（#659）。
    """
    removed = []
    prose_parts = []

    for block_text, block_kind in _pbp.iter_markdown_blocks(text):
        if block_kind == _pbp.BLOCK_KIND_CODE_FENCE:
            removed.append(block_text)
        else:
            prose_parts.append(block_text)

    result = ''.join(prose_parts)
    return result, removed


def clean_prose(text: str) -> str:
    """prose テキストから非日本語判定対象の要素を除去する"""
    # インラインコードを除去
    text = INLINE_CODE_RE.sub('', text)

    # URL を除去
    text = URL_RE.sub('', text)

    # Markdown リンクのURLパーツを除去（テキスト部分は残す）
    text = MD_LINK_URL_RE.sub(lambda m: m.group(1), text)

    # CLI コマンド行を除去
    text = CLI_LINE_RE.sub('', text)

    # 識別子・パッケージ名・CLI トークン・file path を除去 (AC13)
    text = IDENTIFIER_RE.sub('', text)

    return text


def count_japanese_chars(text: str) -> int:
    """テキスト内の日本語文字数をカウントする"""
    return len(JAPANESE_RE.findall(text))


def count_effective_chars(text: str) -> int:
    """判定に使う有効文字数（空白除く）をカウントする"""
    # 空白を除去した文字数
    cleaned = WHITESPACE_RE.sub('', text)
    return len(cleaned)


def split_into_prose_blocks(text: str) -> list[str]:
    """テキストを prose block（段落・見出し等）に分割する"""
    # 空行で分割
    blocks = re.split(r'\n\s*\n', text)

    # 空ブロックを除去
    blocks = [b.strip() for b in blocks if b.strip()]

    return blocks


# machine_yaml の value 側として許容されるパターン（自然文でないもの）
# boolean, number, identifier, path, URL, enum 等
_YAML_VALUE_NON_PROSE_RE = re.compile(
    r'^('
    r'true|false|yes|no|null|~'             # boolean/null
    r'|\d[\d._\-]*'                          # number / version
    r'|[a-zA-Z][a-zA-Z0-9_.\-/]*'           # identifier / path / enum (スペースなし)
    r'|https?://\S+'                         # URL
    r'|\[[^\]]*\]'                           # YAML inline list
    r'|\{[^}]*\}'                            # YAML inline map
    r'|)'                                    # 空値
    r'$'
)

# 自然文の特徴を持つ value かどうかを判定
# スペースを含みかつ 3 語以上、または句読点（.,;:!?）を含む場合は prose とみなす
_PROSE_VALUE_RE = re.compile(
    r'[,;!?]'                               # 句読点
    r'|(?:\S+\s+){2,}\S+'                  # スペース区切りで 3 語以上
)


def _is_yaml_machine_line(line: str) -> bool:
    """
    1 行が machine-readable な key: value 形式かどうかを判定する。
    自然文スタイルの value（語数が多い・句読点がある）は False を返す。
    """
    m = re.match(r'^\s*[a-zA-Z_][a-zA-Z0-9_.]*\s*:\s*(.*)', line)
    if not m:
        return False
    value = m.group(1).strip()
    # value が自然文的特徴を持つ場合は prose として扱う
    if _PROSE_VALUE_RE.search(value):
        return False
    # value が machine_yaml value パターンに一致するか確認
    return bool(_YAML_VALUE_NON_PROSE_RE.match(value))


def _classify_block(block: str) -> str:
    """
    ブロックの種別を分類する。

    prose_boundary_policy.classify_block_legacy() に委譲し、
    既存 consumer（changed_prose_blocks 等）向けの legacy 分類名を返す。

    Returns:
        'code_fence' | 'machine_yaml' | 'shell_command' | 'grep_pattern' |
        'url_or_identifier_only' | 'prose'
    """
    return _pbp.classify_block_legacy(block)


def _is_heading_block(block: str) -> bool:
    """
    ブロックが heading_policy（SSOT）に登録された canonical / bilingual heading かどうかを判定する。

    **この関数が prose 除外可否（guard 判定）の正本である。**
    ``classify_block()`` の戻り値（``BLOCK_KIND_CANONICAL_HEADING`` 等）は構文分類のみであり、
    prose 除外の根拠として使ってはならない。prose 除外が必要な場合は必ずこの関数を経由すること。

    heading_policy (#654 B1_B4):
    - classify_block() が canonical_heading / bilingual_heading を返しても、
      heading_policy に存在しない見出し（非 canonical）は False を返す。
    - HEADING_POLICY に登録された見出しのみ prose ratio 判定から除外される。
    - 非 canonical 英語見出し（例: ## Outcome Risks / ## This is a long English sentence）は
      False を返し、prose delta 対象に残る（AC7）。
    - heading_policy は validate_japanese_content.py / check_issue_contract.py の
      唯一の許可リスト（SSOT）として機能する（B1_B4）。

    Note: classify_block() 公開 API は AC1 により変更しない（旧動作維持）。
    heading_policy 参照はこの関数でのみ行う。
    """
    # B1 fix (#654): leading whitespace を strip しない。raw line（rstrip("\r\n") のみ）を
    # parse_atx_heading_line() に渡す。4-space indented code block を誤って heading 扱いしないため。
    raw_line = block.rstrip('\r\n')
    parsed = _pbp.parse_atx_heading_line(raw_line)
    if parsed is None:
        # parse_atx_heading_line() が None → GFM 上は heading ではない（code block 等）
        return False

    heading_text = parsed['text']

    # heading_policy SSOT で照合（B1_B4: inventory に存在しない見出しは False）
    return _pbp.lookup_heading_policy(heading_text) is not None


def _is_grep_pattern_block(text: str) -> bool:
    """
    ブロックが grep/rg コマンド行またはパターン行であるかを判定する。
    文中に grep が出てくる程度の英語説明文は False を返す。

    prose_boundary_policy._is_grep_pattern_block() に委譲する。
    """
    return _pbp._is_grep_pattern_block(text)


def split_markdown_blocks(text: str) -> list[dict]:
    """
    Markdown テキストをブロック単位に分割し、各ブロックの種別を返す。

    GFM 準拠のセグメンテーションを使用するため prose_boundary_policy.iter_markdown_blocks()
    に委譲し、独自 non-greedy fence regex を撤去している（#659）。

    Returns:
        list of {'text': str, 'type': str}
        type は 'code_fence' | 'machine_yaml' | 'shell_command' |
               'grep_pattern' | 'url_or_identifier_only' | 'prose'
    """
    result = []

    for block_text, _raw_kind in _pbp.iter_markdown_blocks(text):
        if _raw_kind == _pbp.BLOCK_KIND_CODE_FENCE:
            # code fence ブロック: 独自分割は行わず直接追加
            result.append({'text': block_text.strip(), 'type': 'code_fence'})
        else:
            # prose 領域: 空行で段落分割してから各ブロックを分類
            for sub_block in re.split(r'\n\s*\n', block_text):
                if sub_block.strip():
                    btype = _classify_block(sub_block)
                    # B1 fix (#654): raw_text は leading whitespace を保持（strip しない）。
                    # _is_heading_block() は raw_text を構文判定に使うことで
                    # 4-space indented code block を誤って heading 扱いしない。
                    # 'text' は後方互換のため strip 後を維持（hash / display 用）。
                    result.append({
                        'text': sub_block.strip(),
                        'raw_text': sub_block.rstrip('\r\n'),
                        'type': btype,
                    })

    return result


def changed_prose_blocks(old: str, new: str) -> list[dict]:
    """
    old/new の prose block を比較し、new 側で追加・実質変更された prose block を返す。

    変更が code_fence / machine_yaml / shell_command / grep_pattern /
    url_or_identifier_only だけであれば空リストを返す（pass）。

    multiplicity を考慮する: 旧側に 1 回ある block が新側で 2 回になったら
    余剰の 1 回を changed として検査対象にする。

    Returns:
        list of prose block dict (split_markdown_blocks の 'prose' type のみ)
        新規または変更された prose block。
        変更が prose 以外のみ → 空リスト（pass）
    """
    import hashlib
    from collections import Counter

    def prose_block_hash(block_text: str) -> str:
        return hashlib.sha256(block_text.encode('utf-8')).hexdigest()

    old_blocks = split_markdown_blocks(old)
    new_blocks = split_markdown_blocks(new)

    def _strip_leading_line_endings_only(text: str) -> str:
        """leading \r\n のみを除去する（space/tab は保持）。#672 fix."""
        return text.lstrip('\r\n')

    def _is_prose_delta_target(b: dict) -> bool:
        """prose delta 判定対象かどうか。
        type == 'prose' かつ heading でないブロック（#654 heading_policy）。
        canonical_heading / bilingual_heading は delta 対象外とする。
        """
        if b['type'] != 'prose':
            return False
        # heading_policy (#654 B1 fix): raw_text（leading whitespace 保持）で判定。
        # 'text' は strip 済みのため 4-space indented code block の leading spaces が
        # 失われ誤 heading 判定が生じる。raw_text があればそれを優先する。
        # #672 fix: code fence 直後の prose 領域は leading \n 付きで返るため、
        # heading 判定前に \r\n のみを除去する（space/tab は保持して B1 fix を維持）。
        heading_check_text = b.get('raw_text', b['text'])
        heading_probe = _strip_leading_line_endings_only(heading_check_text)
        if _is_heading_block(heading_probe):
            return False
        return True

    # old の prose block hash を Counter で管理（multiplicity を保持）
    old_prose_counts: Counter = Counter(
        prose_block_hash(b['text'])
        for b in old_blocks
        if _is_prose_delta_target(b)
    )

    # new の prose block のうち、old の multiplicity を超えるものを「変更・追加」とみなす
    remaining = Counter(old_prose_counts)
    changed = []
    for b in new_blocks:
        if _is_prose_delta_target(b):
            h = prose_block_hash(b['text'])
            if remaining[h] > 0:
                # 旧側の残余分を消費（同一 block は pass）
                remaining[h] -= 1
            else:
                # 旧側の残余がない = 新規または重複追加
                changed.append(b)

    return changed


def classify_borderline(text: str, threshold: float = 0.1, lower_threshold: float = 0.05) -> str:
    """
    テキストが borderline か否かを判定する。

    borderline の定義:
      - aggregate_ratio >= threshold: PASS（border 判定不要）
      - lower_threshold <= aggregate_ratio < threshold: BORDERLINE
      - aggregate_ratio < lower_threshold: CLEAR_FAIL

    prose block が存在しない場合は CLEAR_FAIL を返す。

    Returns:
        'PASS' | 'BORDERLINE' | 'CLEAR_FAIL'
    """
    # コードフェンスを除去
    cleaned_text, _ = extract_code_fences(text)

    # prose block に分割
    raw_blocks = split_into_prose_blocks(cleaned_text)

    total_japanese = 0
    total_chars = 0
    has_prose = False

    for block in raw_blocks:
        # heading_policy (#654 B1_B4): SSOT 参照で canonical heading のみ除外
        # _is_heading_block() は heading_policy を参照し、非 canonical 見出しは False を返す
        if _is_heading_block(block):
            continue
        clean_block = clean_prose(block)
        effective_chars = count_effective_chars(clean_block)
        if effective_chars < 5:
            continue
        has_prose = True
        total_japanese += count_japanese_chars(clean_block)
        total_chars += effective_chars

    if not has_prose or total_chars == 0:
        return 'CLEAR_FAIL'

    aggregate_ratio = total_japanese / total_chars

    if aggregate_ratio >= threshold:
        return 'PASS'
    elif aggregate_ratio >= lower_threshold:
        return 'BORDERLINE'
    else:
        return 'CLEAR_FAIL'


def validate_text(text: str, threshold: float = 0.1) -> ValidationResult:
    """
    テキストの日本語比率を検査する

    Args:
        text: 検査対象のテキスト
        threshold: 日本語比率の閾値（デフォルト: 0.1）

    Returns:
        ValidationResult
    """
    # コードフェンスを除去
    cleaned_text, _ = extract_code_fences(text)

    # prose block に分割
    raw_blocks = split_into_prose_blocks(cleaned_text)

    prose_blocks = []
    failed_blocks = []
    total_japanese = 0
    total_chars = 0

    for block in raw_blocks:
        # heading_policy (#654 B1_B4): SSOT 参照で canonical heading のみ除外
        # _is_heading_block() は heading_policy を参照し、非 canonical 見出しは False を返す。
        # 非 canonical 見出し（例: ## Outcome Risks）は prose block として残る（AC7）。
        if _is_heading_block(block):
            continue

        # 各ブロックをクリーン化
        clean_block = clean_prose(block)

        # 有効文字数
        effective_chars = count_effective_chars(clean_block)

        # 有効文字が少なすぎるブロックはスキップ（heading のみ等）
        if effective_chars < 5:
            continue

        japanese_count = count_japanese_chars(clean_block)
        ratio = japanese_count / effective_chars if effective_chars > 0 else 0.0

        block_info = {
            'original': block[:100] + '...' if len(block) > 100 else block,
            'effective_chars': effective_chars,
            'japanese_chars': japanese_count,
            'ratio': ratio,
            'passed': ratio >= threshold,
        }
        prose_blocks.append(block_info)

        total_japanese += japanese_count
        total_chars += effective_chars

        if ratio < threshold:
            failed_blocks.append(block_info)

    # aggregate 比率
    aggregate_ratio = total_japanese / total_chars if total_chars > 0 else 0.0

    # prose block が全く存在しない場合はfail
    if not prose_blocks:
        return ValidationResult(
            passed=False,
            aggregate_ratio=0.0,
            prose_blocks=[],
            failed_blocks=[],
            total_chars=0,
            japanese_chars=0,
            threshold=threshold,
        )

    # prose block 単位で全て pass する必要がある
    all_blocks_pass = len(failed_blocks) == 0

    return ValidationResult(
        passed=all_blocks_pass,
        aggregate_ratio=aggregate_ratio,
        prose_blocks=prose_blocks,
        failed_blocks=failed_blocks,
        total_chars=total_chars,
        japanese_chars=total_japanese,
        threshold=threshold,
    )


def main():
    parser = argparse.ArgumentParser(
        description='Markdown prose の日本語比率を検査する'
    )
    parser.add_argument(
        '--file', '-f',
        type=str,
        default=None,
        help='検査対象のファイルパス（省略時は stdin）'
    )
    parser.add_argument(
        '--threshold', '-t',
        type=float,
        default=0.1,
        help='日本語比率の閾値（デフォルト: 0.1）'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='詳細な結果を出力する'
    )
    parser.add_argument(
        '--parse-body',
        type=str,
        default=None,
        help=(
            'Parse gh command to extract --body value. '
            'Returns body text on stdout, or empty string if not found.'
        ),
    )
    parser.add_argument(
        '--parse-body-file',
        type=str,
        default=None,
        help=(
            'Parse gh command to extract --body-file path. '
            'Returns file path, STDIN_FAIL_CLOSED if "-", or empty if not found.'
        ),
    )
    parser.add_argument(
        '--parse-api-input',
        type=str,
        default=None,
        help=(
            'Parse gh api command to extract --input file path. '
            'Returns: '
            'API_INPUT_STDIN if "--input -", '
            'API_INPUT_FILE:<path> if file path, '
            'API_INPUT_NONE if no --input flag found, '
            'API_INPUT_ERROR on parse failure.'
        ),
    )
    parser.add_argument(
        '--classify-api-mutation',
        type=str,
        default=None,
        help=(
            'Given a JSON file path, classify if it is a body mutation. '
            'Combined with --api-endpoint. '
            'Returns: '
            'BODY_MUTATION_ISSUE:<number> if issue body mutation, '
            'BODY_MUTATION_PR:<number> if PR body mutation, '
            'NOT_BODY_MUTATION if not a body mutation, '
            'PAYLOAD_PARSE_FAILED if JSON parse fails, '
            'ENDPOINT_PARSE_FAILED if endpoint parse fails.'
        ),
    )
    parser.add_argument(
        '--api-endpoint',
        type=str,
        default=None,
        help='gh api endpoint for --classify-api-mutation (e.g. repos/owner/repo/issues/123)',
    )
    parser.add_argument(
        '--api-method',
        type=str,
        default=None,
        help=(
            'HTTP method for --classify-api-mutation. '
            'When omitted, classification is method-agnostic for backward compatibility. '
            'Comment PATCH routes should pass PATCH explicitly.'
        ),
    )
    parser.add_argument(
        '--classify-graphql-mutation',
        type=str,
        default=None,
        help=(
            'Given a GraphQL payload JSON file path, classify if it is a body mutation. '
            'Returns: '
            'GRAPHQL_BODY_MUTATION_BLOCKED if query contains mutation + body mutation keywords + body variable, '
            'GRAPHQL_MUTATION_DENIED if query contains mutation but no specific body mutation keyword, '
            'GRAPHQL_NOT_MUTATION if query does not contain mutation keyword, '
            'GRAPHQL_PARSE_FAILED if JSON parse or query field missing. '
            '(B1: #594 blocker fix)'
        ),
    )
    parser.add_argument(
        '--extract-api-command-endpoint',
        type=str,
        default=None,
        help=(
            'Parse full gh api command line to extract the endpoint. '
            'Returns the endpoint string, or ENDPOINT_PARSE_FAILED.'
        ),
    )
    parser.add_argument(
        '--extract-api-command-method',
        type=str,
        default=None,
        help=(
            'Parse full gh api command line to extract the HTTP method. '
            'Returns: PATCH, POST, GET, DELETE, or METHOD_UNKNOWN. '
            'Unspecified + --input present defaults to POST. '
            'GET is non-mutation. '
            '(B3: #594 blocker fix)'
        ),
    )
    parser.add_argument(
        '--parse-edit-target',
        type=str,
        default=None,
        help=(
            'Parse gh issue/pr edit command to extract target number. '
            'Returns NUMBER:<n>, AMBIGUOUS, or RESOLVE_ERROR on stdout.'
        ),
    )
    parser.add_argument(
        '--parse-edit-type',
        type=str,
        default='issue',
        choices=['issue', 'pr'],
        help='Edit type for --parse-edit-target (issue or pr)',
    )
    parser.add_argument(
        '--delta-check',
        action='store_true',
        help=(
            'Delta mode: --old-file と --new-file を比較して changed prose blocks のみを検査する。'
            'DELTA_PASS / DELTA_FAIL:<changed>:<failed> を stdout に出力し、'
            'exit 0 = pass, exit 2 = fail を返す。'
        ),
    )
    parser.add_argument(
        '--old-file',
        type=str,
        default=None,
        help='delta-check: 比較元ファイルパス',
    )
    parser.add_argument(
        '--new-file',
        type=str,
        default=None,
        help='delta-check: 比較先ファイルパス',
    )
    parser.add_argument(
        '--borderline-check',
        action='store_true',
        help=(
            'Borderline check mode: stdin から prose を読んで borderline か否かを判定する。'
            'stdout に PASS / BORDERLINE / CLEAR_FAIL を出力し exit 0 で終了する。'
            '--threshold と組み合わせて閾値を指定できる。'
            '--lower-threshold で borderline 下限を指定できる（デフォルト: threshold * 0.5）。'
        ),
    )
    parser.add_argument(
        '--lower-threshold',
        type=float,
        default=None,
        help=(
            'Borderline check の下限閾値（デフォルト: threshold * 0.5）。'
            'lower_threshold <= ratio < threshold の場合 BORDERLINE を返す。'
        ),
    )

    args = parser.parse_args()

    # ============================================================
    # Borderline check mode (--borderline-check)
    # ============================================================
    if args.borderline_check:
        text = sys.stdin.read()
        threshold = args.threshold
        lower = args.lower_threshold if args.lower_threshold is not None else threshold * 0.5
        result = classify_borderline(text, threshold=threshold, lower_threshold=lower)
        print(result)
        sys.exit(0)

    # ============================================================
    # Parse body mode (--parse-body)
    # ============================================================
    if args.parse_body is not None:
        import shlex as _shlex

        command = args.parse_body
        try:
            tokens = _shlex.split(command)
        except ValueError:
            tokens = []

        body_value = None
        i = 0
        while i < len(tokens):
            if tokens[i] in ('--body', '-b') and i + 1 < len(tokens):
                body_value = tokens[i + 1]
                break
            if tokens[i].startswith('--body='):
                body_value = tokens[i][len('--body='):]
                break
            if tokens[i] in ('--field', '--raw-field', '-f') and i + 1 < len(tokens):
                if tokens[i + 1].startswith('body='):
                    body_value = tokens[i + 1][5:]
                    break
            i += 1

        if body_value and body_value != '-':
            print(body_value)
        sys.exit(0)

    # ============================================================
    # Parse body-file mode (--parse-body-file)
    # ============================================================
    if args.parse_body_file is not None:
        import shlex as _shlex

        command = args.parse_body_file
        try:
            tokens = _shlex.split(command)
        except ValueError:
            tokens = []

        result_path = None
        is_stdin = False
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == '--body-file' and i + 1 < len(tokens):
                fp = tokens[i + 1]
                if fp == '-':
                    is_stdin = True
                else:
                    result_path = fp
                break
            if tok.startswith('--body-file='):
                fp = tok[len('--body-file='):]
                if fp == '-':
                    is_stdin = True
                else:
                    result_path = fp
                break
            if tok == '-F' and i + 1 < len(tokens):
                fp = tokens[i + 1]
                if fp == '-':
                    is_stdin = True
                else:
                    result_path = fp
                break
            if tok.startswith('-F='):
                fp = tok[len('-F='):]
                if fp == '-':
                    is_stdin = True
                else:
                    result_path = fp
                break
            i += 1

        if is_stdin:
            print('STDIN_FAIL_CLOSED')
        elif result_path:
            print(result_path)
        sys.exit(0)

    # ============================================================
    # Parse api input mode (--parse-api-input)
    # ============================================================
    if args.parse_api_input is not None:
        import shlex as _shlex

        command = args.parse_api_input
        try:
            tokens = _shlex.split(command)
        except ValueError:
            print('API_INPUT_ERROR')
            sys.exit(0)

        result_path = None
        is_stdin = False
        found = False
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == '--input' and i + 1 < len(tokens):
                found = True
                fp = tokens[i + 1]
                if fp == '-':
                    is_stdin = True
                else:
                    result_path = fp
                break
            if tok.startswith('--input='):
                found = True
                fp = tok[len('--input='):]
                if fp == '-':
                    is_stdin = True
                else:
                    result_path = fp
                break
            i += 1

        if is_stdin:
            print('API_INPUT_STDIN')
        elif result_path:
            print(f'API_INPUT_FILE:{result_path}')
        else:
            print('API_INPUT_NONE')
        sys.exit(0)

    # ============================================================
    # Classify GraphQL mutation (--classify-graphql-mutation) [B1: #594]
    # ============================================================
    if args.classify_graphql_mutation is not None:
        import json as _json

        payload_file = args.classify_graphql_mutation
        try:
            with open(payload_file, 'r', encoding='utf-8') as f:
                payload = _json.load(f)
        except (FileNotFoundError, IOError, _json.JSONDecodeError):
            print('GRAPHQL_PARSE_FAILED')
            sys.exit(0)

        if not isinstance(payload, dict):
            print('GRAPHQL_PARSE_FAILED')
            sys.exit(0)

        query = payload.get('query', '')
        if not isinstance(query, str):
            print('GRAPHQL_PARSE_FAILED')
            sys.exit(0)

        # Check if it's a mutation query
        if 'mutation' not in query.lower():
            print('GRAPHQL_NOT_MUTATION')
            sys.exit(0)

        # Check for body mutation keywords in query
        has_body_mutation_keyword = any(kw in query for kw in GRAPHQL_BODY_MUTATION_KEYWORDS)

        # Check if variables contain a 'body' field
        variables = payload.get('variables', {})
        has_body_variable = isinstance(variables, dict) and 'body' in variables

        if has_body_mutation_keyword and has_body_variable:
            # Body mutation via GraphQL: blocked (B1)
            print('GRAPHQL_BODY_MUTATION_BLOCKED')
        elif has_body_mutation_keyword:
            # Mutation keyword present but no body variable: conservative deny
            print('GRAPHQL_MUTATION_DENIED')
        else:
            # mutation keyword present but no specific body mutation keyword: conservative deny
            print('GRAPHQL_MUTATION_DENIED')
        sys.exit(0)

    # ============================================================
    # Extract api command endpoint (--extract-api-command-endpoint)
    # ============================================================
    if args.extract_api_command_endpoint is not None:
        import shlex as _shlex

        command = args.extract_api_command_endpoint
        try:
            tokens = _shlex.split(command)
        except ValueError:
            print('ENDPOINT_PARSE_FAILED')
            sys.exit(0)

        # gh api [flags] <endpoint>
        # endpoint is the first non-flag positional argument after 'api'
        api_idx = None
        for idx, tok in enumerate(tokens):
            if tok == 'api':
                api_idx = idx
                break

        if api_idx is None:
            print('ENDPOINT_PARSE_FAILED')
            sys.exit(0)

        # Flags that consume the next token
        consuming_flags = {
            '--hostname', '-H', '--header', '-f', '--raw-field', '-F', '--field',
            '-X', '--method', '--input', '--jq', '-q', '--template', '-t',
            '--cache', '--paginate', '--preview', '-p',
        }

        endpoint = None
        i = api_idx + 1
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith('-'):
                if tok in consuming_flags:
                    i += 2  # skip flag + value
                    continue
                # single char flag that may take value immediately
                i += 1
                continue
            # First non-flag token is the endpoint
            endpoint = tok
            break

        if endpoint:
            print(endpoint)
        else:
            print('ENDPOINT_PARSE_FAILED')
        sys.exit(0)

    # ============================================================
    # Classify api mutation (--classify-api-mutation)
    # ============================================================
    if args.classify_api_mutation is not None:
        import json as _json

        payload_file = args.classify_api_mutation
        endpoint = args.api_endpoint or ''

        # Parse JSON payload
        try:
            with open(payload_file, 'r', encoding='utf-8') as f:
                payload = _json.load(f)
        except (FileNotFoundError, IOError, _json.JSONDecodeError):
            print('PAYLOAD_PARSE_FAILED')
            sys.exit(0)

        # Check if payload contains 'body' key (body mutation indicator)
        if not isinstance(payload, dict) or 'body' not in payload:
            print('NOT_BODY_MUTATION')
            sys.exit(0)

        body_value = payload.get('body')
        if not isinstance(body_value, str):
            print('INVALID_BODY_TYPE')
            sys.exit(0)

        method = (args.api_method or '').strip().upper()
        if method in {'GET', 'DELETE'}:
            print('NOT_BODY_MUTATION')
            sys.exit(0)

        # Parse endpoint to detect issue/PR body mutation
        # Patterns:
        #   repos/{owner}/{repo}/issues/{number}          -> ISSUE PATCH
        #   repos/{owner}/{repo}/pulls/{number}           -> PR PATCH
        #   /repos/{owner}/{repo}/issues/{number}         -> ISSUE PATCH (leading slash)
        #   /repos/{owner}/{repo}/pulls/{number}          -> PR PATCH
        import re as _re
        # Strip leading slash
        ep = endpoint.lstrip('/')

        issue_m = _re.match(
            r'^repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)$', ep
        )
        pr_m = _re.match(
            r'^repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)$', ep
        )
        issue_comment_m = _re.match(
            r'^repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/comments/(?P<comment_id>\d+)$', ep
        )
        pr_review_comment_m = _re.match(
            r'^repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/comments/(?P<comment_id>\d+)$', ep
        )

        if issue_m:
            print(f"BODY_MUTATION_ISSUE:{issue_m.group('number')}")
        elif pr_m:
            print(f"BODY_MUTATION_PR:{pr_m.group('number')}")
        elif issue_comment_m:
            if method and method != 'PATCH':
                print('NOT_BODY_MUTATION')
            else:
                print(
                    "BODY_MUTATION_ISSUE_COMMENT:"
                    f"{issue_comment_m.group('owner')}:"
                    f"{issue_comment_m.group('repo')}:"
                    f"{issue_comment_m.group('comment_id')}"
                )
        elif pr_review_comment_m:
            if method and method != 'PATCH':
                print('NOT_BODY_MUTATION')
            else:
                print(
                    "BODY_MUTATION_PR_REVIEW_COMMENT:"
                    f"{pr_review_comment_m.group('owner')}:"
                    f"{pr_review_comment_m.group('repo')}:"
                    f"{pr_review_comment_m.group('comment_id')}"
                )
        else:
            # endpoint not recognized as issue/PR/comment PATCH mutation
            print('NOT_BODY_MUTATION')
        sys.exit(0)

    # ============================================================
    # Extract api command method (--extract-api-command-method) [B3: #594]
    # ============================================================
    if args.extract_api_command_method is not None:
        import shlex as _shlex

        command = args.extract_api_command_method
        try:
            tokens = _shlex.split(command)
        except ValueError:
            print('METHOD_UNKNOWN')
            sys.exit(0)

        method = None
        has_input = False
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ('--method', '-X') and i + 1 < len(tokens):
                method = tokens[i + 1].upper()
                i += 2
                continue
            if tok.startswith('--method='):
                method = tok[len('--method='):].upper()
                i += 1
                continue
            if tok.startswith('-X') and len(tok) > 2:
                method = tok[2:].upper()
                i += 1
                continue
            if tok == '--input' or tok.startswith('--input='):
                has_input = True
                i += 1
                continue
            i += 1

        if method is not None:
            # Explicit method specified
            print(method)
        elif has_input:
            # gh api with --input but no explicit method: GitHub CLI defaults to POST
            print('POST')
        else:
            # No method, no --input: treat as GET (non-mutation)
            print('GET')
        sys.exit(0)

    # ============================================================
    # Parse edit target mode (--parse-edit-target)
    # ============================================================
    if args.parse_edit_target is not None:
        import shlex as _shlex

        command = args.parse_edit_target
        edit_type = args.parse_edit_type

        try:
            tokens = _shlex.split(command)
        except ValueError:
            print('RESOLVE_ERROR')
            sys.exit(0)

        # gh issue edit / gh pr edit の positional target を探す
        flag_args = {
            '--body', '-b', '--body-file', '-F', '--title', '-t',
            '--add-assignee', '--remove-assignee', '--add-label', '--remove-label',
            '--add-project', '--remove-project', '--milestone', '--repo', '-R',
        }

        targets = []
        start_idx = 0
        for idx, tok in enumerate(tokens):
            if tok == 'edit' and idx > 0:
                start_idx = idx + 1
                break

        skip_next = False
        i = start_idx
        while i < len(tokens):
            tok = tokens[i]
            if skip_next:
                skip_next = False
                i += 1
                continue
            if tok.startswith('-'):
                if tok in flag_args:
                    skip_next = True
                i += 1
                continue
            targets.append(tok)
            i += 1

        if len(targets) == 0:
            print('RESOLVE_ERROR')
        elif len(targets) > 1:
            print('AMBIGUOUS')
        else:
            target = targets[0]
            url_m = re.search(r'/(issues|pulls)/(\d+)', target)
            if url_m:
                print(f'NUMBER:{url_m.group(2)}')
            elif re.match(r'^\d+$', target):
                print(f'NUMBER:{target}')
            else:
                print('RESOLVE_ERROR')
        sys.exit(0)

    # ============================================================
    # Delta check mode (--delta-check)
    # ============================================================
    if args.delta_check:
        if not args.old_file or not args.new_file:
            print('ERROR: --delta-check には --old-file と --new-file が必要です', file=sys.stderr)
            sys.exit(1)

        try:
            with open(args.old_file, 'r', encoding='utf-8') as f:
                old_text = f.read()
        except (FileNotFoundError, IOError) as e:
            print(f'ERROR: old-file 読み込みエラー: {e}', file=sys.stderr)
            sys.exit(1)

        try:
            with open(args.new_file, 'r', encoding='utf-8') as f:
                new_text = f.read()
        except (FileNotFoundError, IOError) as e:
            print(f'ERROR: new-file 読み込みエラー: {e}', file=sys.stderr)
            sys.exit(1)

        changed = changed_prose_blocks(old_text, new_text)

        if not changed:
            print('DELTA_PASS')
            sys.exit(0)

        # 変更された prose block を検証
        failed = []
        for block in changed:
            r = validate_text(block['text'], threshold=args.threshold)
            if not r.passed:
                failed.append(block)

        if not failed:
            print('DELTA_PASS')
            sys.exit(0)
        else:
            print(f'DELTA_FAIL:{len(changed)}:{len(failed)}')
            sys.exit(2)

    # ============================================================
    # 通常モード
    # ============================================================

    # テキストの読み込み
    if args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            print(f'ERROR: ファイルが見つかりません: {args.file}', file=sys.stderr)
            sys.exit(1)
        except IOError as e:
            print(f'ERROR: ファイル読み込みエラー: {e}', file=sys.stderr)
            sys.exit(1)
    else:
        text = sys.stdin.read()

    # バリデーション実行
    result = validate_text(text, threshold=args.threshold)

    # 結果の出力
    if args.verbose or not result.passed:
        print(f'aggregate_ratio: {result.aggregate_ratio:.3f}', file=sys.stderr)
        print(f'threshold: {result.threshold}', file=sys.stderr)
        print(f'prose_blocks: {len(result.prose_blocks)}', file=sys.stderr)
        print(f'failed_blocks: {len(result.failed_blocks)}', file=sys.stderr)

        if result.failed_blocks:
            print('\n--- 日本語比率不足の prose block ---', file=sys.stderr)
            for i, block in enumerate(result.failed_blocks, 1):
                print(
                    f'  [{i}] ratio={block["ratio"]:.3f} '
                    f'(jp={block["japanese_chars"]}, total={block["effective_chars"]})',
                    file=sys.stderr
                )

    if result.passed:
        if args.verbose:
            print('PASS', file=sys.stderr)
        sys.exit(0)
    else:
        if not args.verbose:
            print(
                f'FAIL: 日本語比率不足 '
                f'(aggregate={result.aggregate_ratio:.3f}, '
                f'threshold={result.threshold}, '
                f'failed_blocks={len(result.failed_blocks)})',
                file=sys.stderr
            )
        sys.exit(1)


if __name__ == '__main__':
    main()
