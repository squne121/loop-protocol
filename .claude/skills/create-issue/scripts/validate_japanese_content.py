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

# 識別子・パッケージ名パターン (英数字 + アンダースコア + ハイフン + ドット)
IDENTIFIER_RE = re.compile(r'\b[a-zA-Z][a-zA-Z0-9_.\-/]*\b')


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

    args = parser.parse_args()

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
