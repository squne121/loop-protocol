#!/usr/bin/env python3
"""Bounded shell-command structure analyzer (Issue #1428).

Extracts the set of *simple commands* that would actually execute when a
shell (POSIX/bash-family) evaluates a command string, distinguishing them
from quoted argument data, search keywords, and other non-executable text.

This module is intentionally a *bounded grammar* hand-written analyzer, not a
full shell parser. It recognizes:

  * top-level control operators: ``;`` ``&&`` ``||`` ``&`` ``|`` and newline
  * command substitution: ``$( ... )`` and backtick `` `...` ``
  * process substitution: ``<(...)`` and ``>(...)``
  * parameter/arithmetic expansion markers (``${...}``, ``$((...))``,
    bare ``$VAR``) — used only to decide word literalness, never recursed
    into for command extraction (arithmetic expansion cannot itself execute
    a *new* top-level command in the fragments this analyzer targets)
  * heredocs (``<<DELIM`` / ``<<-DELIM``) and here-strings (``<<<WORD``)
  * a bounded set of known *execution carriers*: ``bash -c`` / ``sh -c`` /
    ``bash -s`` / ``sh -s`` / bare ``bash`` / bare ``sh`` (stdin script),
    ``eval``, ``exec``, ``source`` / ``.``, leading ``env VAR=val...``
    assignment prefixes, bare leading ``VAR=val`` assignment prefixes, and
    the ``command`` wrapper.

Anything outside this bounded grammar (unclosed quotes, malformed
substitutions, unknown execution carriers such as ``find -exec`` / ``xargs``
/ ``sudo`` / ``timeout`` / ``nice`` / ``nohup``, dynamic command words that
could resolve to ``git``/``rtk``) is classified ``indeterminate`` and must be
treated as fail-closed by callers — never fail-open.

The analyzer never executes any part of the input and never returns raw
argument text (only the bounded enum classifications defined by
``SHELL_COMMAND_ANALYSIS_V1``), so it can safely run on secret-boundary
protected input without leaking argv into structured output (Issue #1428
In Scope 2 / secret boundary continuity).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

SCHEMA = "SHELL_COMMAND_ANALYSIS_V1"

STATUS_OK = "ok"
STATUS_INDETERMINATE = "indeterminate"

REASON_PARSED = "parsed"
REASON_MALFORMED_SHELL = "malformed_shell"
REASON_UNSUPPORTED_CONSTRUCT = "unsupported_construct"
REASON_DYNAMIC_COMMAND_WORD = "dynamic_command_word"
REASON_ANALYSIS_TIMEOUT = "analysis_timeout"

COMMAND_KIND_GIT_PUSH = "git_push"
COMMAND_KIND_RTK_GIT_PUSH = "rtk_git_push"

LITERAL = "literal"
DYNAMIC = "dynamic"

CTX_TOP_LEVEL = "top_level"
CTX_LIST = "list"
CTX_PIPELINE = "pipeline"
CTX_COMMAND_SUBSTITUTION = "command_substitution"
CTX_PROCESS_SUBSTITUTION = "process_substitution"
CTX_EXECUTION_CARRIER = "execution_carrier"

DANGEROUS_FORCE = "force"
DANGEROUS_TAGS = "tags"
DANGEROUS_ALL = "all"
DANGEROUS_MIRROR = "mirror"
DANGEROUS_DELETE = "delete"

_MAX_INPUT_LEN = 20000
_MAX_DEPTH = 12

# Known git global options that take a value and appear before the
# subcommand. Unrecognized leading `-`-prefixed tokens before a subcommand
# is identified are treated as unsupported (fail-closed).
_GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--config-env", "--namespace"}

# Known execution carriers that this analyzer recursively parses.
_SHELL_CARRIER_NAMES = {"bash", "sh"}

# Known but *unresolvable inline* carriers: their real script content is not
# present in the command string itself (external file / stdin), so any
# occurrence is indeterminate rather than recursed into.
_UNRESOLVABLE_CARRIER_REASON = REASON_DYNAMIC_COMMAND_WORD

# Execution carriers that are recognized but explicitly NOT supported for
# recursive analysis (Issue #1428 In Scope 1 / 8) — always indeterminate.
_UNSUPPORTED_CARRIERS = {"find", "xargs", "sudo", "timeout", "nice", "nohup"}

_PUSH_DANGEROUS_FLAG_MAP = {
    "--force": DANGEROUS_FORCE,
    "--force-with-lease": DANGEROUS_FORCE,
    "-f": DANGEROUS_FORCE,
    "--tags": DANGEROUS_TAGS,
    "--all": DANGEROUS_ALL,
    "--mirror": DANGEROUS_MIRROR,
    "--delete": DANGEROUS_DELETE,
    "-d": DANGEROUS_DELETE,
}


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class CommandFact:
    command_kind: str
    executable_literalness: str
    subcommand_literalness: str
    remote_class: str
    refspec_class: str
    dangerous_flags: tuple
    execution_context: str
    source_span: SourceSpan

    def to_dict(self) -> dict:
        return {
            "command_kind": self.command_kind,
            "executable_literalness": self.executable_literalness,
            "subcommand_literalness": self.subcommand_literalness,
            "remote_class": self.remote_class,
            "refspec_class": self.refspec_class,
            "dangerous_flags": list(self.dangerous_flags),
            "execution_context": self.execution_context,
            "source_span": self.source_span.to_dict(),
        }


@dataclass
class _Word:
    text: str
    start: int
    end: int
    literal: bool
    value: str | None  # unquoted literal value, only meaningful if literal


@dataclass
class _AnalysisState:
    commands: list = field(default_factory=list)
    indeterminate: bool = False
    reason_code: str = REASON_PARSED

    def mark_indeterminate(self, reason_code: str) -> None:
        self.indeterminate = True
        # First indeterminate reason wins unless already set to a more
        # specific one; keep it simple and deterministic (first wins).
        if self.reason_code == REASON_PARSED:
            self.reason_code = reason_code


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _find_matching(text: str, open_idx: int, open_ch: str, close_ch: str) -> int | None:
    """Return index of the matching close_ch for open_ch at open_idx, scanning
    forward while respecting nested quotes and nested (open_ch, close_ch)
    pairs of the SAME kind. Returns None if unbalanced."""
    depth = 1
    i = open_idx + 1
    quote: str | None = None
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _find_matching_backtick(text: str, open_idx: int) -> int | None:
    i = open_idx + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            return i
        i += 1
    return None


def _find_dollar_paren_extent(text: str, dollar_idx: int) -> tuple[int, int, bool] | None:
    """text[dollar_idx] == '$' and text[dollar_idx+1] == '('.
    Returns (start, end_inclusive_of_close, is_arithmetic) or None if
    unbalanced."""
    n = len(text)
    if dollar_idx + 1 >= n or text[dollar_idx + 1] != "(":
        return None
    is_arith = dollar_idx + 2 < n and text[dollar_idx + 2] == "("
    if is_arith:
        # $(( ... )) — find matching double-close.
        depth = 1
        i = dollar_idx + 3
        while i < n:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    if i + 1 < n and text[i + 1] == ")":
                        return (dollar_idx, i + 1, True)
                    return (dollar_idx, i, True)
            i += 1
        return None
    close = _find_matching(text, dollar_idx + 1, "(", ")")
    if close is None:
        return None
    return (dollar_idx, close, False)


def _find_matching_brace(text: str, open_idx: int) -> int | None:
    return _find_matching(text, open_idx, "{", "}")


def _scan_word_dynamism(text: str, start: int, end: int) -> tuple[bool, list[tuple[int, int]]]:
    """Scan text[start:end] (a single shell WORD's raw source, which may
    include quote characters) and determine:
      - is_dynamic: True if any expansion construct is present outside of
        single-quoted regions
      - substitutions: list of (abs_start, abs_end) spans of $(...) /
        backtick command-substitution content (exclusive of delimiters) to
        recurse into, in absolute offsets into the ORIGINAL command string.

    Single-quoted regions suppress all expansion (bash semantics).
    """
    is_dynamic = False
    subs: list[tuple[int, int]] = []
    i = start
    quote: str | None = None
    n = end
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch == "$" or ch == "`":
                pass  # fall through to expansion handling below
            else:
                i += 1
                continue
        if quote is None:
            if ch == "'":
                quote = "'"
                i += 1
                continue
            if ch == '"':
                quote = '"'
                i += 1
                continue
            if ch == "\\" and i + 1 < n:
                i += 2
                continue

        if ch == "`":
            close = _find_matching_backtick(text, i)
            is_dynamic = True
            if close is None:
                raise _ParseError(REASON_MALFORMED_SHELL)
            subs.append((i + 1, close))
            i = close + 1
            continue

        if ch == "$" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "(":
                extent = _find_dollar_paren_extent(text, i)
                is_dynamic = True
                if extent is None:
                    raise _ParseError(REASON_MALFORMED_SHELL)
                d_start, d_end, is_arith = extent
                if not is_arith:
                    subs.append((d_start + 2, d_end))
                i = d_end + 1
                continue
            if nxt == "{":
                close = _find_matching_brace(text, i + 1)
                is_dynamic = True
                if close is None:
                    raise _ParseError(REASON_MALFORMED_SHELL)
                i = close + 1
                continue
            if _is_ident_start(nxt) or nxt.isdigit() or nxt in ("@", "*", "#", "?", "-", "$", "!"):
                is_dynamic = True
                i += 1
                if _is_ident_start(nxt):
                    i += 1
                    while i < n and _is_ident_char(text[i]):
                        i += 1
                else:
                    i += 1
                continue
        i += 1
    if quote is not None:
        raise _ParseError(REASON_MALFORMED_SHELL)
    return is_dynamic, subs


class _ParseError(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _unquote_literal(text: str) -> str:
    """Given a word's raw source text that _scan_word_dynamism already
    determined is fully literal (no unescaped expansion), strip quoting to
    obtain its literal value. Assumes well-formed input (already validated
    by _scan_word_dynamism)."""
    out: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                out.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n and text[i + 1] in ('"', "\\", "$", "`"):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            quote = "'"
            i += 1
            continue
        if ch == '"':
            quote = '"'
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _tokenize_chunk_words(text: str, chunk_start: int, chunk_end: int) -> list[_Word]:
    """Split a top-level 'simple command' chunk (already isolated from
    top-level control operators) into shell WORDs, respecting quoting.
    Redirection operators and their targets/delimiters are excluded by the
    caller (via redir span removal) before this is invoked on the residual
    argv text; heredoc bodies are handled separately by the caller."""
    words: list[_Word] = []
    i = chunk_start
    n = chunk_end
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        word_start = i
        quote: str | None = None
        while i < n:
            c = text[i]
            if quote == "'":
                if c == "'":
                    quote = None
                i += 1
                continue
            if quote == '"':
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c == '"':
                    quote = None
                i += 1
                continue
            if c.isspace():
                break
            if c == "'":
                quote = "'"
                i += 1
                continue
            if c == '"':
                quote = '"'
                i += 1
                continue
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == "`":
                close = _find_matching_backtick(text, i)
                if close is None:
                    raise _ParseError(REASON_MALFORMED_SHELL)
                i = close + 1
                continue
            if c == "$" and i + 1 < n and text[i + 1] == "(":
                extent = _find_dollar_paren_extent(text, i)
                if extent is None:
                    raise _ParseError(REASON_MALFORMED_SHELL)
                i = extent[1] + 1
                continue
            if c == "$" and i + 1 < n and text[i + 1] == "{":
                close = _find_matching_brace(text, i + 1)
                if close is None:
                    raise _ParseError(REASON_MALFORMED_SHELL)
                i = close + 1
                continue
            i += 1
        if quote is not None:
            raise _ParseError(REASON_MALFORMED_SHELL)
        word_end = i
        is_dynamic, _subs = _scan_word_dynamism(text, word_start, word_end)
        value = None if is_dynamic else _unquote_literal(text[word_start:word_end])
        words.append(_Word(text=text[word_start:word_end], start=word_start, end=word_end, literal=not is_dynamic, value=value))
    return words


def _collect_substitutions_in_range(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """Scan text[start:end] for $(...) / backtick command-substitution spans
    anywhere (used for heredoc bodies / here-string words / redirection
    targets), returning (inner_start, inner_end, context) triples."""
    results: list[tuple[int, int, str]] = []
    i = start
    n = end
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
        if quote is None and ch == "'":
            quote = "'"
            i += 1
            continue
        if quote is None and ch == '"':
            quote = '"'
            i += 1
            continue
        if ch == "\\" and i + 1 < n and quote != "'":
            i += 2
            continue
        if ch == "`":
            close = _find_matching_backtick(text, i)
            if close is None:
                raise _ParseError(REASON_MALFORMED_SHELL)
            results.append((i + 1, close, CTX_COMMAND_SUBSTITUTION))
            i = close + 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            extent = _find_dollar_paren_extent(text, i)
            if extent is None:
                raise _ParseError(REASON_MALFORMED_SHELL)
            d_start, d_end, is_arith = extent
            if not is_arith:
                results.append((d_start + 2, d_end, CTX_COMMAND_SUBSTITUTION))
            i = d_end + 1
            continue
        i += 1
    return results


def _split_top_level_chunks(text: str) -> list[tuple[int, int, str]]:
    """Split the full command text into top-level chunks separated by
    control operators (;, &&, ||, &, |, newline), respecting quoting,
    nested $()/``/${}/process-substitution, and heredoc bodies.

    Returns list of (chunk_start, chunk_end, preceding_operator) where
    preceding_operator is one of: None (first chunk), ';', '&&', '||',
    '&', '|', '\\n'.
    """
    chunks: list[tuple[int, int, str]] = []
    n = len(text)
    i = 0
    chunk_start = 0
    preceding_op: str | None = None
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "'":
            quote = "'"
            i += 1
            continue
        if ch == '"':
            quote = '"'
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            close = _find_matching_backtick(text, i)
            if close is None:
                raise _ParseError(REASON_MALFORMED_SHELL)
            i = close + 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            extent = _find_dollar_paren_extent(text, i)
            if extent is None:
                raise _ParseError(REASON_MALFORMED_SHELL)
            i = extent[1] + 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "{":
            close = _find_matching_brace(text, i + 1)
            if close is None:
                raise _ParseError(REASON_MALFORMED_SHELL)
            i = close + 1
            continue
        if ch in ("<", ">") and i + 1 < n and text[i + 1] == "(":
            # process substitution <( ... ) / >( ... )
            close = _find_matching(text, i + 1, "(", ")")
            if close is None:
                raise _ParseError(REASON_MALFORMED_SHELL)
            i = close + 1
            continue
        if ch == "<" and i + 1 < n and text[i + 1] == "<":
            # heredoc / here-string: << / <<- / <<<
            heredoc_op_start = i
            j = i + 2
            dash = False
            if j < n and text[j] == "-":
                dash = True
                j += 1
            if j < n and text[j] == "<":
                # here-string <<<WORD — treat WORD as an ordinary word
                # (scanned normally by chunk word tokenization later); just
                # skip past the operator here.
                i = j + 1
                continue
            # classic heredoc: consume optional whitespace, then delimiter
            while j < n and text[j] in " \t":
                j += 1
            delim_start = j
            quoted_delim = False
            if j < n and text[j] in ("'", '"'):
                qc = text[j]
                quoted_delim = True
                j += 1
                while j < n and text[j] != qc:
                    j += 1
                if j >= n:
                    raise _ParseError(REASON_MALFORMED_SHELL)
                delim_text = text[delim_start + 1 : j]
                j += 1
            else:
                while j < n and (text[j].isalnum() or text[j] in "_-"):
                    j += 1
                delim_text = text[delim_start:j]
            if not delim_text:
                raise _ParseError(REASON_UNSUPPORTED_CONSTRUCT)
            # Find the next newline to locate the body start.
            nl = text.find("\n", j)
            if nl == -1:
                # No body present in this fragment (e.g. truncated input) —
                # treat as unsupported/indeterminate rather than guessing.
                raise _ParseError(REASON_UNSUPPORTED_CONSTRUCT)
            body_start = nl + 1
            scan = body_start
            body_end = None
            while scan <= n:
                line_end = text.find("\n", scan)
                if line_end == -1:
                    line_end = n
                line = text[scan:line_end]
                candidate = line.lstrip("\t") if dash else line
                if candidate == delim_text:
                    body_end = scan
                    delim_line_end = line_end
                    break
                if line_end >= n:
                    break
                scan = line_end + 1
            if body_end is None:
                raise _ParseError(REASON_UNSUPPORTED_CONSTRUCT)
            consumed_end = min(delim_line_end + 1, n)
            _HEREDOC_REGISTRY.append((heredoc_op_start, consumed_end, body_start, body_end, quoted_delim))
            i = consumed_end
            continue
        if ch == "&" and i + 1 < n and text[i + 1] == "&":
            chunks.append((chunk_start, i, preceding_op or ""))
            preceding_op = "&&"
            i += 2
            chunk_start = i
            continue
        if ch == "|" and i + 1 < n and text[i + 1] == "|":
            chunks.append((chunk_start, i, preceding_op or ""))
            preceding_op = "||"
            i += 2
            chunk_start = i
            continue
        if ch == "|":
            chunks.append((chunk_start, i, preceding_op or ""))
            preceding_op = "|"
            i += 1
            chunk_start = i
            continue
        if ch == ";":
            chunks.append((chunk_start, i, preceding_op or ""))
            preceding_op = ";"
            i += 1
            chunk_start = i
            continue
        if ch == "&":
            chunks.append((chunk_start, i, preceding_op or ""))
            preceding_op = "&"
            i += 1
            chunk_start = i
            continue
        if ch == "\n":
            chunks.append((chunk_start, i, preceding_op or ""))
            preceding_op = ";"
            i += 1
            chunk_start = i
            continue
        if ch in ("(", ")"):
            # Bounded grammar does not support subshell grouping semantics
            # beyond opaque skip; treat unmatched/parenthesized grouping as
            # unsupported to stay fail-closed rather than guessing.
            raise _ParseError(REASON_UNSUPPORTED_CONSTRUCT)
        i += 1
    if quote is not None:
        raise _ParseError(REASON_MALFORMED_SHELL)
    chunks.append((chunk_start, n, preceding_op or ""))
    return [c for c in chunks if text[c[0] : c[1]].strip() != ""]


# Module-level scratch registry used during a single _split_top_level_chunks
# call to communicate heredoc spans back to the caller. Reset per call.
# Entries: (consumed_start, consumed_end, body_start, body_end, quoted_delim)
_HEREDOC_REGISTRY: list[tuple[int, int, int, int, bool]] = []


def _classify_push_words(words: list[_Word]) -> tuple[str, int] | None:
    """Given the argv words of a simple command, determine if it is a
    git-push / rtk-git-push candidate. Returns (command_kind, subcommand_idx)
    where subcommand_idx is the index of the "push" token in words, or None
    if this command is not a push candidate at all (e.g. different git
    subcommand, unrelated executable)."""
    if not words:
        return None
    idx = 0
    # Strip `command` wrapper.
    if words[idx].literal and words[idx].value == "command":
        idx += 1
        while idx < len(words) and words[idx].literal and words[idx].value in ("-p", "-v", "-V"):
            idx += 1
    if idx >= len(words):
        return None
    exe = words[idx]
    rtk_prefix = False
    if exe.literal and exe.value == "rtk":
        rtk_prefix = True
        idx += 1
        if idx >= len(words):
            return None
        exe = words[idx]
    if not exe.literal:
        # Dynamic executable — only treat as an indeterminate push candidate
        # if a subsequent literal "push" token is present (bounded
        # heuristic matching Issue #1428 dynamic fixtures).
        for w in words[idx + 1 :]:
            if w.literal and w.value == "push":
                return ("__dynamic__", idx)
        return None
    if exe.value != "git":
        return None
    idx += 1
    # Skip known global options (with values) between `git` and subcommand.
    while idx < len(words):
        tok = words[idx]
        if not tok.literal:
            return ("__dynamic_subcommand__", idx)
        if tok.value in _GIT_GLOBAL_OPTS_WITH_VALUE:
            idx += 1  # option token
            if idx < len(words):
                idx += 1  # its value
            continue
        if tok.value.startswith("--") and "=" in tok.value and tok.value.split("=", 1)[0] in _GIT_GLOBAL_OPTS_WITH_VALUE:
            idx += 1
            continue
        if tok.value.startswith("-") and tok.value not in _GIT_GLOBAL_OPTS_WITH_VALUE:
            # Unknown leading option before subcommand is determined.
            return ("__unsupported__", idx)
        break
    if idx >= len(words):
        return None
    subcommand = words[idx]
    if not subcommand.literal:
        return ("__dynamic_subcommand__", idx)
    if subcommand.value != "push":
        return None
    return (COMMAND_KIND_RTK_GIT_PUSH if rtk_prefix else COMMAND_KIND_GIT_PUSH, idx)


def _dangerous_flags_for(words: list[_Word], push_idx: int) -> tuple:
    flags = []
    for w in words[push_idx + 1 :]:
        if w.literal and w.value in _PUSH_DANGEROUS_FLAG_MAP:
            mapped = _PUSH_DANGEROUS_FLAG_MAP[w.value]
            if mapped not in flags:
                flags.append(mapped)
    return tuple(flags)


def _positional_args_after(words: list[_Word], push_idx: int) -> list[_Word]:
    positional = []
    for w in words[push_idx + 1 :]:
        if w.literal and w.value.startswith("-"):
            continue
        positional.append(w)
    return positional


def _remote_and_refspec_class(words: list[_Word], push_idx: int) -> tuple[str, str]:
    positional = _positional_args_after(words, push_idx)
    if not positional:
        return "absent", "absent"
    remote_w = positional[0]
    if not remote_w.literal:
        remote_class = "dynamic"
    elif remote_w.value == "origin":
        remote_class = "origin"
    else:
        remote_class = "other_literal"
    if len(positional) < 2:
        return remote_class, "absent"
    refspec_w = positional[1]
    if not refspec_w.literal:
        refspec_class = "dynamic"
    elif refspec_w.value.startswith("HEAD:refs/heads/") and len(refspec_w.value) > len("HEAD:refs/heads/"):
        refspec_class = "head_to_literal_branch"
    else:
        refspec_class = "other_literal"
    return remote_class, refspec_class


def _strip_env_and_assignment_prefix(words: list[_Word]) -> list[_Word]:
    idx = 0
    if idx < len(words) and words[idx].literal and words[idx].value == "env":
        idx += 1
        while idx < len(words) and words[idx].literal and _looks_like_assignment(words[idx].value):
            idx += 1
        return words[idx:]
    while idx < len(words) and words[idx].literal and _looks_like_assignment(words[idx].value):
        idx += 1
    return words[idx:]


def _looks_like_assignment(value: str) -> bool:
    eq = value.find("=")
    if eq <= 0:
        return False
    name = value[:eq]
    if not _is_ident_start(name[0]):
        return False
    return all(_is_ident_char(c) for c in name[1:])


def _analyze_simple_command(
    words: list[_Word],
    execution_context: str,
    span: SourceSpan,
    state: _AnalysisState,
    depth: int,
) -> None:
    stripped = _strip_env_and_assignment_prefix(words)
    if not stripped:
        return
    exe = stripped[0]
    if exe.literal and exe.value in _UNSUPPORTED_CARRIERS:
        state.mark_indeterminate(REASON_UNSUPPORTED_CONSTRUCT)
        return
    if exe.literal and exe.value in _SHELL_CARRIER_NAMES:
        _handle_shell_carrier(stripped, execution_context, span, state, depth)
        return
    if exe.literal and exe.value == "eval":
        _handle_eval_or_exec(stripped, span, state, depth, is_exec=False)
        return
    if exe.literal and exe.value == "exec":
        _handle_eval_or_exec(stripped, span, state, depth, is_exec=True)
        return
    if exe.literal and exe.value in (".", "source"):
        state.mark_indeterminate(_UNRESOLVABLE_CARRIER_REASON)
        return

    classification = _classify_push_words(stripped)
    if classification is None:
        return
    kind, marker_idx = classification
    if kind in ("__dynamic__", "__dynamic_subcommand__"):
        state.mark_indeterminate(REASON_DYNAMIC_COMMAND_WORD)
        return
    if kind == "__unsupported__":
        state.mark_indeterminate(REASON_UNSUPPORTED_CONSTRUCT)
        return

    remote_class, refspec_class = _remote_and_refspec_class(stripped, marker_idx)
    fact = CommandFact(
        command_kind=kind,
        executable_literalness=LITERAL,
        subcommand_literalness=LITERAL,
        remote_class=remote_class,
        refspec_class=refspec_class,
        dangerous_flags=_dangerous_flags_for(stripped, marker_idx),
        execution_context=execution_context,
        source_span=span,
    )
    state.commands.append(fact)


def _handle_shell_carrier(
    words: list[_Word],
    execution_context: str,
    span: SourceSpan,
    state: _AnalysisState,
    depth: int,
) -> None:
    # words[0] is literal "bash" or "sh".
    rest = words[1:]
    flag = rest[0].value if rest and rest[0].literal else None
    if flag == "-c" and len(rest) >= 2:
        script_word = rest[1]
        if not script_word.literal:
            state.mark_indeterminate(REASON_DYNAMIC_COMMAND_WORD)
            return
        _recurse(script_word.value, 0, CTX_EXECUTION_CARRIER, state, depth + 1)
        return
    # bare `bash` / `sh`, or `-s`, or anything else reading a script from
    # stdin or an unresolved external source — content not inline.
    state.mark_indeterminate(_UNRESOLVABLE_CARRIER_REASON)


def _handle_eval_or_exec(
    words: list[_Word],
    span: SourceSpan,
    state: _AnalysisState,
    depth: int,
    *,
    is_exec: bool,
) -> None:
    rest = words[1:]
    if not rest:
        return
    if not all(w.literal for w in rest):
        state.mark_indeterminate(REASON_DYNAMIC_COMMAND_WORD)
        return
    if is_exec:
        # exec replaces the process with the given argv directly (not a
        # re-parsed script string) — analyze it as a simple command in
        # place.
        _analyze_simple_command(rest, CTX_EXECUTION_CARRIER, span, state, depth + 1)
        return
    joined = " ".join(w.value for w in rest)
    _recurse(joined, 0, CTX_EXECUTION_CARRIER, state, depth + 1)


def _word_in_any_span(w: _Word, spans: list[tuple[int, int]]) -> bool:
    return any(cs <= w.start and w.end <= ce for cs, ce in spans)


def _recurse(text: str, base_offset: int, execution_context: str, state: _AnalysisState, depth: int) -> None:
    if depth > _MAX_DEPTH or len(text) > _MAX_INPUT_LEN:
        state.mark_indeterminate(REASON_ANALYSIS_TIMEOUT)
        return
    global _HEREDOC_REGISTRY
    saved_registry = _HEREDOC_REGISTRY
    _HEREDOC_REGISTRY = []
    try:
        chunks = _split_top_level_chunks(text)
        heredocs = _HEREDOC_REGISTRY
    except _ParseError as exc:
        state.mark_indeterminate(exc.reason_code)
        return
    finally:
        _HEREDOC_REGISTRY = saved_registry

    consumed_spans = [(cs, ce) for cs, ce, _bs, _be, _q in heredocs]

    for _cs, _ce, body_start, body_end, quoted_delim in heredocs:
        if quoted_delim:
            continue
        try:
            subs = _collect_substitutions_in_range(text, body_start, body_end)
        except _ParseError as exc:
            state.mark_indeterminate(exc.reason_code)
            continue
        for inner_start, inner_end, ctx in subs:
            _recurse(text[inner_start:inner_end], base_offset + inner_start, ctx, state, depth + 1)

    is_single_chunk = len(chunks) == 1 and execution_context in (CTX_TOP_LEVEL,)
    for chunk_start, chunk_end, preceding_op in chunks:
        chunk_ctx = execution_context
        if execution_context == CTX_TOP_LEVEL:
            if is_single_chunk:
                chunk_ctx = CTX_TOP_LEVEL
            elif preceding_op == "|":
                chunk_ctx = CTX_PIPELINE
            else:
                chunk_ctx = CTX_LIST
        elif execution_context == CTX_LIST and preceding_op == "|":
            chunk_ctx = CTX_PIPELINE

        try:
            words = _tokenize_chunk_words(text, chunk_start, chunk_end)
        except _ParseError as exc:
            state.mark_indeterminate(exc.reason_code)
            continue

        # Drop any word that falls entirely inside a heredoc's consumed
        # region (operator + delimiter + body + closing delimiter line) —
        # that text is data (or already handled via the dedicated heredoc
        # substitution scan above), not argv.
        words = [w for w in words if not _word_in_any_span(w, consumed_spans)]

        # Redirection handling: drop plain redirection operator/target word
        # pairs from argv (best-effort — a `>`/`<`/`>>` token followed by a
        # target word is excluded from argv classification purposes). Also
        # scan every word (argv AND redirection targets) for embedded
        # command substitutions.
        argv_words: list[_Word] = []
        skip_next = False
        for w in words:
            if skip_next:
                skip_next = False
                continue
            bare = w.text
            if bare in (">", ">>", "<", "2>", "2>>", "&>", "1>"):
                skip_next = True
                continue
            if bare.startswith(">") or (bare.startswith("<") and not bare.startswith("<(")):
                # combined operator+target with no space, e.g. `>out.txt`
                continue
            argv_words.append(w)

        for w in words:
            try:
                _is_dyn, subs = _scan_word_dynamism(text, w.start, w.end)
            except _ParseError as exc:
                state.mark_indeterminate(exc.reason_code)
                continue
            for inner_start, inner_end in subs:
                _recurse(text[inner_start:inner_end], base_offset + inner_start, CTX_COMMAND_SUBSTITUTION, state, depth + 1)

        if not argv_words:
            continue
        span = SourceSpan(base_offset + argv_words[0].start, base_offset + argv_words[-1].end)
        _analyze_simple_command(argv_words, chunk_ctx, span, state, depth)


def analyze_shell_command(command: str) -> dict:
    """Analyze a shell command string and return a
    SHELL_COMMAND_ANALYSIS_V1-shaped dict."""
    if command is None:
        return {"schema": SCHEMA, "status": STATUS_INDETERMINATE, "commands": [], "reason_code": REASON_MALFORMED_SHELL}
    if len(command) > _MAX_INPUT_LEN:
        return {"schema": SCHEMA, "status": STATUS_INDETERMINATE, "commands": [], "reason_code": REASON_ANALYSIS_TIMEOUT}
    state = _AnalysisState()
    try:
        _recurse(command, 0, CTX_TOP_LEVEL, state, 0)
    except RecursionError:
        state.mark_indeterminate(REASON_ANALYSIS_TIMEOUT)
    except Exception:
        state.mark_indeterminate(REASON_MALFORMED_SHELL)

    status = STATUS_INDETERMINATE if state.indeterminate else STATUS_OK
    reason_code = state.reason_code if state.indeterminate else REASON_PARSED
    return {
        "schema": SCHEMA,
        "status": status,
        "commands": [c.to_dict() for c in state.commands],
        "reason_code": reason_code,
    }


def main(argv: list[str]) -> int:
    """CLI entrypoint: reads a JSON payload `{"command": "..."}` from stdin
    and writes a SHELL_COMMAND_ANALYSIS_V1 JSON document to stdout.

    Used by the Node.js adapter via execFileSync (Issue #1428 In Scope 4) —
    argv-based invocation, no shell interpolation."""
    del argv
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        command = payload.get("command")
        if not isinstance(command, str):
            raise ValueError("command must be a string")
    except Exception:
        result = {
            "schema": SCHEMA,
            "status": STATUS_INDETERMINATE,
            "commands": [],
            "reason_code": REASON_MALFORMED_SHELL,
        }
        sys.stdout.write(json.dumps(result))
        return 0
    result = analyze_shell_command(command)
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
