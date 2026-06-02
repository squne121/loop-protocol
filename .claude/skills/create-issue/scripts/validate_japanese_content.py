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
    """コードフェンスを取り除いてテキストと取り除いた部分を返す"""
    removed = []
    result = text

    # トリプルバッククォートフェンス
    triple_backtick = re.compile(r'```[^\n]*\n.*?```', re.DOTALL)
    for m in triple_backtick.finditer(result):
        removed.append(m.group(0))

    result = triple_backtick.sub('', result)

    # チルダフェンス
    tilde_fence = re.compile(r'~~~[^\n]*\n.*?~~~', re.DOTALL)
    for m in tilde_fence.finditer(result):
        removed.append(m.group(0))

    result = tilde_fence.sub('', result)

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


def _classify_block(block: str) -> str:
    """
    ブロックの種別を分類する。

    Returns:
        'code_fence' | 'machine_yaml' | 'shell_command' | 'grep_pattern' |
        'url_or_identifier_only' | 'prose'
    """
    stripped = block.strip()

    # code fence
    if stripped.startswith('```') or stripped.startswith('~~~'):
        return 'code_fence'

    # YAML front matter or machine-readable YAML block (key: value パターンが支配的)
    yaml_line_re = re.compile(r'^\s*[a-zA-Z_][a-zA-Z0-9_.]*\s*:', re.MULTILINE)
    lines = stripped.splitlines()
    if lines:
        yaml_lines = sum(1 for l in lines if yaml_line_re.match(l))
        if yaml_lines >= max(1, len(lines) * 0.6):
            return 'machine_yaml'

    # shell command block ($ or # prefix lines が支配的)
    shell_line_re = re.compile(r'^\s*[$#]\s+\S', re.MULTILINE)
    shell_lines = len(shell_line_re.findall(stripped))
    non_empty_lines = sum(1 for l in lines if l.strip())
    if non_empty_lines > 0 and shell_lines >= non_empty_lines * 0.5:
        return 'shell_command'

    # grep pattern line (rg/grep コマンドが含まれる行)
    if re.search(r'\b(grep|rg|egrep|fgrep)\b', stripped):
        return 'grep_pattern'

    # URL or identifier only (有効文字がほぼ識別子/URLのみ)
    cleaned = clean_prose(stripped)
    effective = count_effective_chars(cleaned)
    if effective < 5:
        return 'url_or_identifier_only'

    return 'prose'


def split_markdown_blocks(text: str) -> list[dict]:
    """
    Markdown テキストをブロック単位に分割し、各ブロックの種別を返す。

    Returns:
        list of {'text': str, 'type': str}
        type は 'code_fence' | 'machine_yaml' | 'shell_command' |
               'grep_pattern' | 'url_or_identifier_only' | 'prose'
    """
    result = []

    # まず code fence を先に抽出（順序を保持するため手動分割）
    code_fence_re = re.compile(
        r'(```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~)', re.DOTALL
    )

    pos = 0
    for m in code_fence_re.finditer(text):
        # code fence 前の部分を段落分割
        before = text[pos:m.start()]
        if before.strip():
            for block in re.split(r'\n\s*\n', before):
                if block.strip():
                    btype = _classify_block(block)
                    result.append({'text': block.strip(), 'type': btype})
        # code fence 自体
        result.append({'text': m.group(0), 'type': 'code_fence'})
        pos = m.end()

    # 残り部分を段落分割
    remainder = text[pos:]
    if remainder.strip():
        for block in re.split(r'\n\s*\n', remainder):
            if block.strip():
                btype = _classify_block(block)
                result.append({'text': block.strip(), 'type': btype})

    return result


def changed_prose_blocks(old: str, new: str) -> list[dict]:
    """
    old/new の prose block を比較し、new 側で追加・実質変更された prose block を返す。

    変更が code_fence / machine_yaml / shell_command / grep_pattern /
    url_or_identifier_only だけであれば空リストを返す（pass）。

    Returns:
        list of prose block dict (split_markdown_blocks の 'prose' type のみ)
        新規または変更された prose block。
        変更が prose 以外のみ → 空リスト（pass）
    """
    import hashlib

    def prose_block_hash(block_text: str) -> str:
        return hashlib.sha256(block_text.encode('utf-8')).hexdigest()

    old_blocks = split_markdown_blocks(old)
    new_blocks = split_markdown_blocks(new)

    # old の prose block hash セット
    old_prose_hashes = {
        prose_block_hash(b['text'])
        for b in old_blocks
        if b['type'] == 'prose'
    }

    # new の prose block のうち、old に存在しないものを「変更・追加」とみなす
    changed = []
    for b in new_blocks:
        if b['type'] == 'prose':
            h = prose_block_hash(b['text'])
            if h not in old_prose_hashes:
                changed.append(b)

    return changed


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

    args = parser.parse_args()

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
