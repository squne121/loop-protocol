"""
test_validate_api_field_body.py

mutation_route_matrix.py の body source 解決テスト (#655)。

AC3: -f body=literal / -F body=literal / -F body=@file が検出される
AC9: -F body=@file はファイル内容を読んで検査、-f body=@file は literal、-F title=@file は body source でない
AC10: -F body=@- (stdin) は fail-closed
AC13: body:null / body:"" / body key 欠落は reason code 付きで fail-closed
"""

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent
sys.path.insert(0, str(SRC))

from mutation_route_matrix import (
    resolve_body_source,
    BODY_SOURCE_RAW_FIELD_LITERAL,
    BODY_SOURCE_FIELD_LITERAL,
    BODY_SOURCE_FIELD_FILE,
    BODY_SOURCE_FIELD_STDIN,
    BODY_SOURCE_INPUT_JSON_FILE,
    BODY_SOURCE_INPUT_JSON_STDIN,
    BODY_SOURCE_INPUT_NON_JSON,
    DENY_STDIN_BODY,
    DENY_UNREADABLE_FILE,
    DENY_NULL_BODY,
    DENY_EMPTY_BODY,
    DENY_MISSING_BODY,
    DENY_INVALID_JSON,
)


# ============================================================
# AC3: -f/-F body= raw field detection
# ============================================================

def test_raw_field_body_literal_detected():
    """AC3: -f body=literal -> api_raw_field_body_literal"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH -f 'body=test body'")
    assert r.source_kind == BODY_SOURCE_RAW_FIELD_LITERAL
    assert r.body_text == "test body"
    assert r.deny_reason is None


def test_field_body_literal_detected():
    """AC3: -F body=literal -> api_field_body_literal"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH -F 'body=test body'")
    assert r.source_kind == BODY_SOURCE_FIELD_LITERAL
    assert r.body_text == "test body"
    assert r.deny_reason is None


def test_field_file_expand(tmp_path):
    """AC9: -F body=@file -> api_field_body_file, reads file content"""
    f = tmp_path / "body.md"
    f.write_text("日本語のテキスト")
    r = resolve_body_source(f"gh api repos/o/r/issues/1 -X PATCH -F body=@{f}")
    assert r.source_kind == BODY_SOURCE_FIELD_FILE
    assert r.body_text == "日本語のテキスト"
    assert r.deny_reason is None
    assert r.file_path == str(f)


def test_raw_field_no_expand():
    """AC9: -f body=@file -> literal '@file' (no file dereference)"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH -f body=@some_file.md")
    assert r.source_kind == BODY_SOURCE_RAW_FIELD_LITERAL
    assert r.body_text == "@some_file.md"
    assert r.deny_reason is None


def test_title_not_body():
    """AC9: -F title=@file -> body source として扱わない"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH -F title=@title.md")
    # body source は解決されない (body_text=None, deny_reason=None)
    assert r.body_text is None
    assert r.deny_reason is None


# ============================================================
# AC10: -F body=@- stdin fail-closed
# ============================================================

def test_field_stdin_fail_closed():
    """AC10: -F body=@- -> deny_stdin_body_uninspectable"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH -F body=@-")
    assert r.source_kind == BODY_SOURCE_FIELD_STDIN
    assert r.deny_reason == DENY_STDIN_BODY
    assert r.body_text is None


# ============================================================
# AC13: body:null / body:"" / body key missing -> reason code
# ============================================================

def test_null_body_fail_closed(tmp_path):
    """AC13: body:null -> deny_null_body_public_mutation"""
    f = tmp_path / "p.json"
    f.write_text('{"body": null}')
    r = resolve_body_source(f"gh api repos/o/r/issues/1 --method PATCH --input {f}")
    assert r.deny_reason == DENY_NULL_BODY
    assert r.body_text is None


def test_empty_body_fail_closed(tmp_path):
    """AC13: body:"" -> deny_empty_body_public_mutation"""
    f = tmp_path / "p.json"
    f.write_text('{"body": ""}')
    r = resolve_body_source(f"gh api repos/o/r/issues/1 --method PATCH --input {f}")
    assert r.deny_reason == DENY_EMPTY_BODY
    assert r.body_text is None


def test_missing_body_fail_closed(tmp_path):
    """AC13: body key missing -> deny_missing_body_for_public_body_route"""
    f = tmp_path / "p.json"
    f.write_text('{"title": "No body key"}')
    r = resolve_body_source(f"gh api repos/o/r/issues/1 --method PATCH --input {f}")
    assert r.deny_reason == DENY_MISSING_BODY
    assert r.body_text is None


def test_unreadable_field_file(tmp_path):
    """AC5: -F body=@missing_file -> deny_unreadable_body_file"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH -F body=@/no/such/file.md")
    assert r.source_kind == BODY_SOURCE_FIELD_FILE
    assert r.deny_reason == DENY_UNREADABLE_FILE
    assert r.body_text is None


# ============================================================
# B2: --raw-field / --field long option alias
# ============================================================

def test_raw_field_long_option_body_literal():
    """B2: --raw-field body=TEXT -> api_raw_field_body_literal"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH --raw-field 'body=English text'")
    assert r.source_kind == BODY_SOURCE_RAW_FIELD_LITERAL
    assert r.body_text == "English text"
    assert r.deny_reason is None


def test_field_long_option_body_literal():
    """B2: --field body=TEXT -> api_field_body_literal"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH --field 'body=English text'")
    assert r.source_kind == BODY_SOURCE_FIELD_LITERAL
    assert r.body_text == "English text"
    assert r.deny_reason is None


def test_field_long_option_file_expand(tmp_path):
    """B2: --field body=@file -> api_field_body_file (dereferences file like -F)"""
    f = tmp_path / "body.md"
    f.write_text("日本語のテキスト")
    r = resolve_body_source(f"gh api repos/o/r/issues/1 -X PATCH --field body=@{f}")
    assert r.source_kind == BODY_SOURCE_FIELD_FILE
    assert r.body_text == "日本語のテキスト"
    assert r.deny_reason is None


def test_raw_field_long_option_no_expand():
    """B2: --raw-field body=@file -> literal '@file' (no dereference like -f)"""
    r = resolve_body_source("gh api repos/o/r/issues/1 -X PATCH --raw-field body=@some_file.md")
    assert r.source_kind == BODY_SOURCE_RAW_FIELD_LITERAL
    assert r.body_text == "@some_file.md"
    assert r.deny_reason is None
