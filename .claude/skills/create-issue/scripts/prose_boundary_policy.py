"""
prose_boundary_policy.py

Markdown ブロック種別分類の SSOT（Single Source of Truth）。
block_kind enum と classify API を提供する。

block_kind 一覧（AC1 固定契約・変更禁止）:
  human_prose           人間が書いた自然文 prose
  canonical_heading     ## Outcome 等の標準見出し
  bilingual_heading     日英混在の見出し
  machine_contract      Machine-Readable Contract ブロック（YAML fence 等）
  yaml_machine_line     機械可読 YAML 行が支配的なブロック
  vc_command            Verification Commands 内のコマンドブロック
  shell_command         シェルコマンドブロック（$ / # prefix）
  code_fence            ``` / ~~~ フェンスで囲まれたコードブロック
  url_or_identifier     URL・識別子のみの行

Out of scope:
  body source / route 分類（child-2 以降で扱う）はこのモジュールの block_kind に含めない。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# block_kind 定数（AC1 固定契約）
# ---------------------------------------------------------------------------

BLOCK_KIND_HUMAN_PROSE = "human_prose"
BLOCK_KIND_CANONICAL_HEADING = "canonical_heading"
BLOCK_KIND_BILINGUAL_HEADING = "bilingual_heading"
BLOCK_KIND_MACHINE_CONTRACT = "machine_contract"
BLOCK_KIND_YAML_MACHINE_LINE = "yaml_machine_line"
BLOCK_KIND_VC_COMMAND = "vc_command"
BLOCK_KIND_SHELL_COMMAND = "shell_command"
BLOCK_KIND_CODE_FENCE = "code_fence"
BLOCK_KIND_URL_OR_IDENTIFIER = "url_or_identifier"

# すべての block_kind の集合（型検証・テスト用）
ALL_BLOCK_KINDS: frozenset[str] = frozenset({
    BLOCK_KIND_HUMAN_PROSE,
    BLOCK_KIND_CANONICAL_HEADING,
    BLOCK_KIND_BILINGUAL_HEADING,
    BLOCK_KIND_MACHINE_CONTRACT,
    BLOCK_KIND_YAML_MACHINE_LINE,
    BLOCK_KIND_VC_COMMAND,
    BLOCK_KIND_SHELL_COMMAND,
    BLOCK_KIND_CODE_FENCE,
    BLOCK_KIND_URL_OR_IDENTIFIER,
})

# ---------------------------------------------------------------------------
# 内部正規表現
# ---------------------------------------------------------------------------

# Markdown 見出し
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')

# 日本語文字（ひらがな・カタカナ・CJK）
_JAPANESE_RE = re.compile(r'[぀-ゟ゠-ヿ一-鿿]')

# 技術識別子（snake_case / kebab-case / file.ext）
_IDENTIFIER_RE = re.compile(r'\b[a-zA-Z][a-zA-Z0-9]*(?:[_.\-][a-zA-Z0-9._\-]*)+\b')

# URL
_URL_RE = re.compile(r'https?://\S+')

# インラインコード
_INLINE_CODE_RE = re.compile(r'`[^`\n]+`')

# Markdown リンク
_MD_LINK_URL_RE = re.compile(r'\[([^\]]*)\]\([^\)]+\)')

# 空白
_WHITESPACE_RE = re.compile(r'\s+')

# CLI コマンド行（$ / # prefix）
_CLI_LINE_RE = re.compile(r'^\s*[$#]\s+\S.*$', re.MULTILINE)

# Issue/PR/SHA/パス参照のみ行パターン
# 例: #123, GH-123, abc1234567890123456789012345678901234567890, path/to/file
_REF_ONLY_LINE_RE = re.compile(
    r'^(?:'
    r'#\d+'                              # #123 Issue/PR
    r'|GH-\d+'                           # GH-123
    r'|[0-9a-f]{7,40}'                   # SHA (7-40 hex)
    r'|[a-zA-Z0-9._\-]+(?:/[a-zA-Z0-9._\-]+)+'  # path/to/file
    r')\s*$'
)

# machine_yaml の value 側として許容されるパターン（自然文でないもの）
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

# prose 的 value（3語以上 / 句読点）
_PROSE_VALUE_RE = re.compile(
    r'[,;!?]'
    r'|(?:\S+\s+){2,}\S+'
)

# grep/rg コマンド行
_GREP_CMD_LINE_RE = re.compile(
    r'^'
    r'(?:'
    r'\|?\s*(?:grep|rg|egrep|fgrep)\s+'
    r'|\$\s+(?:grep|rg|egrep|fgrep)\s+'
    r'|(?:grep|rg|egrep|fgrep)\s+-'
    r')'
)

# code fence の開始行パターン（3個以上のバッククォート / チルダ）
_CODE_FENCE_OPEN_RE = re.compile(r'^(`{3,}|~{3,})[^\n]*$')

# GFM 準拠 fence opening 行パターン（0-3 spaces indent + 3以上の backtick or tilde）
# 0-3 spaces indent + fence chars (backtick or tilde) + optional info string
_GFM_FENCE_OPEN_LINE_RE = re.compile(
    r'^( {0,3})((`{3,})|(~{3,}))(.*?)$'
)

# GFM 準拠 fence closing 行パターン
# closing fence は 0-3 spaces + fence chars のみ（trailing non-space は無効）
_GFM_FENCE_CLOSE_LINE_RE = re.compile(
    r'^( {0,3})((`{3,})|(~{3,}))\s*$'
)

# Machine-Readable Contract ブロック（```yaml で始まりかつ contract_schema_version を含む、
# または ``` の後が yaml/YAML のもの）
# ただしここでは code_fence 内の特別な判定として扱う

# ---------------------------------------------------------------------------
# GFM 準拠 block セグメンテーション API（#659 追加）
# ---------------------------------------------------------------------------


def iter_markdown_blocks(text: str):
    """
    GFM 準拠の block セグメンテーション API。

    テキストを行単位で走査し、GFM fenced code block の境界を正確に判定して
    (text, block_kind) のタプルを yield する。

    GFM 仕様準拠の fence 判定:
    - opening fence: 0-3 spaces indent + 3以上の backtick or tilde + optional info string
    - closing fence: opening と同種（backtick/tilde）かつ同長以上、0-3 spaces indent、
      trailing non-space なし
    - 未閉 fence: EOF まで code block として扱う（prose ではない）
    - 4 spaces indent: fence として認識しない
    - backtick/tilde mismatch: closing として認識しない（異なる文字種）

    Yields:
        tuple[str, str]: (block_text, block_kind)
            block_kind は BLOCK_KIND_CODE_FENCE または BLOCK_KIND_HUMAN_PROSE
            （分類の精緻化は classify_block() が行う）
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)
    i = 0
    prose_lines: list[str] = []

    while i < n:
        line = lines[i]
        # rstrip newlines for pattern matching, but keep original line for output
        stripped = line.rstrip('\n').rstrip('\r')

        # GFM fence opening line を検出
        m = _GFM_FENCE_OPEN_LINE_RE.match(stripped)
        if m:
            fence_chars = m.group(2)  # e.g. "```" or "~~~~"
            fence_char = fence_chars[0]  # '`' or '~'
            fence_len = len(fence_chars)

            # flush any accumulated prose
            if prose_lines:
                yield ''.join(prose_lines), BLOCK_KIND_HUMAN_PROSE
                prose_lines = []

            # collect the code fence block
            fence_line_buf: list[str] = [line]
            i += 1

            closed = False
            while i < n:
                inner_line = lines[i]
                inner_stripped = inner_line.rstrip('\n').rstrip('\r')

                # closing fence: same char type, length >= opening length,
                # 0-3 spaces indent, no trailing non-space
                cm = _GFM_FENCE_CLOSE_LINE_RE.match(inner_stripped)
                if cm:
                    close_chars = cm.group(2)
                    close_char = close_chars[0]
                    close_len = len(close_chars)
                    if close_char == fence_char and close_len >= fence_len:
                        # valid closing fence
                        fence_line_buf.append(inner_line)
                        i += 1
                        closed = True
                        break
                # not a valid closing fence — part of code block content
                fence_line_buf.append(inner_line)
                i += 1

            # yield code fence (closed or unclosed / EOF)
            yield ''.join(fence_line_buf), BLOCK_KIND_CODE_FENCE
        else:
            # regular (non-fence) line — accumulate into prose buffer
            prose_lines.append(line)
            i += 1

    # flush remaining prose
    if prose_lines:
        yield ''.join(prose_lines), BLOCK_KIND_HUMAN_PROSE


def split_blocks(text: str) -> list[tuple[str, str]]:
    """
    iter_markdown_blocks のリスト版。

    Returns:
        list of (block_text, block_kind) tuples
    """
    return list(iter_markdown_blocks(text))


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------


def _is_yaml_machine_line(line: str) -> bool:
    """
    1 行が machine-readable な key: value 形式かどうかを判定する。
    自然文スタイルの value（語数が多い・句読点がある）は False を返す。
    """
    m = re.match(r'^\s*[a-zA-Z_][a-zA-Z0-9_.]*\s*:\s*(.*)', line)
    if not m:
        return False
    value = m.group(1).strip()
    if _PROSE_VALUE_RE.search(value):
        return False
    return bool(_YAML_VALUE_NON_PROSE_RE.match(value))


def _is_grep_pattern_block(text: str) -> bool:
    """
    ブロックが grep/rg コマンド行またはパターン行であるかを判定する。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    cmd_lines = sum(1 for line in lines if _GREP_CMD_LINE_RE.match(line))
    return cmd_lines >= max(1, len(lines) * 0.5)


def _clean_for_effective_char_count(text: str) -> str:
    """有効文字数カウント用のクリーニング（識別子・URL・インラインコード除去）"""
    text = _INLINE_CODE_RE.sub('', text)
    text = _URL_RE.sub('', text)
    text = _MD_LINK_URL_RE.sub(lambda m: m.group(1), text)
    text = _CLI_LINE_RE.sub('', text)
    text = _IDENTIFIER_RE.sub('', text)
    return text


def _count_effective_chars(text: str) -> int:
    """空白除き有効文字数"""
    cleaned = _WHITESPACE_RE.sub('', text)
    return len(cleaned)


def _is_machine_contract_fence(fence_text: str) -> bool:
    """
    code_fence ブロックが YAML Machine-Readable Contract かどうかを判定する。
    条件: fence prefix が yaml / yml であり、かつ contract_schema_version を行頭の key として含む。

    注意: 以前の実装は `'contract_schema_version' in content or (...)` という形で
    or のため、fence の言語指定を無視して任意のフェンス内に
    `contract_schema_version` 文字列があれば machine_contract に分類していた。
    この実装では yaml/yml prefix を必須条件とすることで過剰一致を排除する。
    """
    first_line = fence_text.splitlines()[0] if fence_text.splitlines() else ''
    if not _CODE_FENCE_OPEN_RE.match(first_line.strip()):
        return False
    content = fence_text
    return (
        first_line.strip().lstrip('`~').strip().lower() in {'yaml', 'yml'}
        and bool(re.search(r'(?m)^\s*contract_schema_version\s*:', content))
    )


def _is_url_or_identifier_line(line: str) -> bool:
    """行が URL・識別子・Issue/PR/SHA/パス参照のみで構成されているかを判定する"""
    stripped = line.strip()
    if not stripped:
        return False
    # URL のみ
    if _URL_RE.fullmatch(stripped):
        return True
    # #123 / GH-123 / SHA / パス参照
    if _REF_ONLY_LINE_RE.match(stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# メイン分類 API
# ---------------------------------------------------------------------------


def classify_block(block: str) -> str:
    """
    Markdown ブロックの block_kind を返す。

    Args:
        block: 分類対象の Markdown ブロック文字列（前後空白はあってもよい）

    Returns:
        block_kind 文字列（ALL_BLOCK_KINDS のいずれか）
    """
    stripped = block.strip()
    if not stripped:
        return BLOCK_KIND_HUMAN_PROSE

    lines = stripped.splitlines()

    # -----------------------------------------------------------------------
    # code_fence チェック（3個以上のバッククォート / チルダ）
    # -----------------------------------------------------------------------
    first_line = lines[0].strip()
    if _CODE_FENCE_OPEN_RE.match(first_line):
        # machine_contract かどうか（```yaml + contract_schema_version）
        if _is_machine_contract_fence(stripped):
            return BLOCK_KIND_MACHINE_CONTRACT
        return BLOCK_KIND_CODE_FENCE

    # -----------------------------------------------------------------------
    # 見出しチェック
    # -----------------------------------------------------------------------
    if len(lines) == 1 or (len(lines) >= 1 and _HEADING_RE.match(lines[0])):
        # 見出し行のみ、または先頭行が見出し
        heading_line = lines[0]
        hm = _HEADING_RE.match(heading_line)
        if hm:
            heading_text = hm.group(2)
            has_japanese = bool(_JAPANESE_RE.search(heading_text))
            # 英語 ASCII のみ → canonical_heading
            # 日本語含む → bilingual_heading
            if has_japanese:
                return BLOCK_KIND_BILINGUAL_HEADING
            else:
                return BLOCK_KIND_CANONICAL_HEADING

    # -----------------------------------------------------------------------
    # shell_command / vc_command チェック
    # -----------------------------------------------------------------------
    shell_line_re = re.compile(r'^\s*[$#]\s+\S', re.MULTILINE)
    shell_lines = len(shell_line_re.findall(stripped))
    non_empty_lines = sum(1 for line in lines if line.strip())

    if non_empty_lines > 0 and shell_lines >= non_empty_lines * 0.5:
        # VC コマンド（uv run pytest / pnpm / rg などの検証コマンド）
        vc_cmd_re = re.compile(
            r'^\s*[$#]\s+(?:uv\s+run|pnpm|rg|pytest|python3?|bash|sh)\s',
            re.MULTILINE
        )
        vc_lines = len(vc_cmd_re.findall(stripped))
        if vc_lines >= max(1, shell_lines * 0.5):
            return BLOCK_KIND_VC_COMMAND
        return BLOCK_KIND_SHELL_COMMAND

    # -----------------------------------------------------------------------
    # grep_pattern → shell_command として扱う
    # -----------------------------------------------------------------------
    if _is_grep_pattern_block(stripped):
        return BLOCK_KIND_SHELL_COMMAND

    # -----------------------------------------------------------------------
    # yaml_machine_line チェック（machine-readable YAML block）
    # -----------------------------------------------------------------------
    if lines:
        machine_yaml_count = sum(1 for line in lines if _is_yaml_machine_line(line))
        if non_empty_lines > 0 and machine_yaml_count >= max(1, non_empty_lines * 0.6):
            return BLOCK_KIND_YAML_MACHINE_LINE

    # -----------------------------------------------------------------------
    # url_or_identifier チェック
    # -----------------------------------------------------------------------
    # すべての非空行が URL / 識別子 / 参照のみの場合
    non_empty = [line.strip() for line in lines if line.strip()]
    if non_empty and all(_is_url_or_identifier_line(line) for line in non_empty):
        return BLOCK_KIND_URL_OR_IDENTIFIER

    # 有効文字数が極端に少ない場合
    cleaned = _clean_for_effective_char_count(stripped)
    effective = _count_effective_chars(cleaned)
    if effective < 5:
        return BLOCK_KIND_URL_OR_IDENTIFIER

    # -----------------------------------------------------------------------
    # デフォルト: human_prose
    # -----------------------------------------------------------------------
    return BLOCK_KIND_HUMAN_PROSE


def classify_block_legacy(block: str) -> str:
    """
    validate_japanese_content.py の既存 consumer 向けの legacy 分類名を返す。

    legacy → block_kind マッピング:
      prose                 ← human_prose / canonical_heading / bilingual_heading
      code_fence            ← code_fence / machine_contract
      machine_yaml          ← yaml_machine_line
      shell_command         ← shell_command / vc_command
      grep_pattern          ← (shell_command に統合; 後方互換用に shell_command → grep_pattern 変換は _classify_block で実施)
      url_or_identifier_only ← url_or_identifier

    Returns:
        'prose' | 'code_fence' | 'machine_yaml' | 'shell_command' |
        'grep_pattern' | 'url_or_identifier_only'
    """
    kind = classify_block(block)

    if kind in (BLOCK_KIND_HUMAN_PROSE, BLOCK_KIND_CANONICAL_HEADING, BLOCK_KIND_BILINGUAL_HEADING):
        return 'prose'
    elif kind in (BLOCK_KIND_CODE_FENCE, BLOCK_KIND_MACHINE_CONTRACT):
        return 'code_fence'
    elif kind == BLOCK_KIND_YAML_MACHINE_LINE:
        return 'machine_yaml'
    elif kind in (BLOCK_KIND_SHELL_COMMAND, BLOCK_KIND_VC_COMMAND):
        # legacy の grep_pattern は grep/rg コマンド行が支配的なブロック
        # _is_grep_pattern_block を再チェックして区別する
        if _is_grep_pattern_block(block.strip()):
            return 'grep_pattern'
        return 'shell_command'
    elif kind == BLOCK_KIND_URL_OR_IDENTIFIER:
        return 'url_or_identifier_only'
    else:
        return 'prose'
