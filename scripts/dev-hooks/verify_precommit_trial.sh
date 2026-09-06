#!/usr/bin/env bash
# verify_precommit_trial.sh — Issue #2552 / #1933
#
# prek による pre-commit staged TS/JS lint trial の動作確認スクリプト。
#
# 重要: 本スクリプトはメインリポジトリの実 `.git/hooks` を一切変更しない。
# `mktemp -d` で作成した isolated temporary git repository の中だけで
# `prek install` / `git commit` を実行し、確認後に破棄する。
#
# 確認項目:
#   (a) lint エラーを含む staged TS/JS ファイルで commit がブロックされること
#   (b) `git commit --no-verify` で bypass できること
#   (c) prek バイナリが解決できない環境をシミュレートした場合の挙動
#       （実機検証の結果、hook 未インストール時は fail-open、
#         hook インストール済みで prek が解決不能になった場合は
#         fail-closed という非対称な挙動になることを確認する。
#         詳細は docs/dev/local-hooks.md の「依存不足時の挙動」参照）
#
# exit code:
#   0   — 全チェック PASS
#   1   — いずれかのチェックが期待と異なる結果になった
#   2   — 前提となる prek / eslint バイナリが解決できない（環境不備）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PREK_BIN="$REPO_ROOT/node_modules/.bin/prek"
ESLINT_BIN="$REPO_ROOT/node_modules/.bin/eslint"

FAILURES=0

log() {
    printf '[verify_precommit_trial] %s\n' "$1"
}

fail() {
    printf '[verify_precommit_trial] FAIL: %s\n' "$1" >&2
    FAILURES=$((FAILURES + 1))
}

# ── 0. 前提チェック（prek / eslint バイナリの解決） ─────────────────────────
if [ ! -x "$PREK_BIN" ]; then
    echo "[verify_precommit_trial] ERROR: prek バイナリが見つかりません: $PREK_BIN" >&2
    echo "[verify_precommit_trial] 'pnpm install' を実行してから再試行してください。" >&2
    exit 2
fi
if [ ! -x "$ESLINT_BIN" ]; then
    echo "[verify_precommit_trial] ERROR: eslint バイナリが見つかりません: $ESLINT_BIN" >&2
    echo "[verify_precommit_trial] 'pnpm install' を実行してから再試行してください。" >&2
    exit 2
fi
log "preflight ok: prek=$("$PREK_BIN" --version) / eslint resolvable at $ESLINT_BIN"

# ── isolated temporary git repository の準備 ────────────────────────────────
TMP_REPO="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP_REPO"
}
trap cleanup EXIT

log "isolated fixture repo: $TMP_REPO"

(
    cd "$TMP_REPO"
    git init -q
    git config user.email "verify-precommit-trial@localhost"
    git config user.name "verify-precommit-trial"
)

# prek.toml を実プロジェクトからそのままコピーする
# （fixture 側の設定 drift を防ぎ、実際に使う設定で検証するため）
cp "$REPO_ROOT/prek.toml" "$TMP_REPO/prek.toml"

# self-contained な eslint flat config（外部 plugin に依存しない最小構成）
cat > "$TMP_REPO/eslint.config.mjs" <<'EOF'
export default [
  {
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
    },
    rules: {
      "no-unused-vars": "error",
    },
  },
];
EOF

(
    cd "$TMP_REPO"
    git add prek.toml eslint.config.mjs
    git commit -q -m "fixture: baseline"
)

FIXTURE_PATH="$REPO_ROOT/node_modules/.bin:$PATH"

# ── (c-1) prek 未インストール状態（hook 未設定）は fail-open であることの確認 ──
(
    cd "$TMP_REPO"
    printf 'const unusedInPreInstall = 1;\n' > uninstalled.js
    git add uninstalled.js
)
rc=0
PRE_INSTALL_LOG="$(cd "$TMP_REPO" && PATH="$FIXTURE_PATH" git commit -q -m "before prek install" 2>&1)" || rc=$?
if [ "$rc" -eq 0 ]; then
    log "PASS (c-1): prek install 前は hook が存在せず、lint エラーを含む staged ファイルでも commit が成立した（fail-open）"
else
    fail "(c-1): prek install 前にもかかわらず commit がブロックされた（想定外, rc=$rc）: $PRE_INSTALL_LOG"
fi

# ── hook のインストール ──────────────────────────────────────────────────────
(
    cd "$TMP_REPO"
    PATH="$FIXTURE_PATH" "$PREK_BIN" install >/dev/null
)
if [ ! -f "$TMP_REPO/.git/hooks/pre-commit" ]; then
    fail "prek install 後に .git/hooks/pre-commit が生成されていない"
fi

# ── (a) lint エラーを含む staged TS/JS ファイルで commit がブロックされること ──
(
    cd "$TMP_REPO"
    printf 'const unusedVariableJs = 1;\n' > offending.js
    printf 'const unusedVariableTs = 1;\n' > offending.ts
    git add offending.js offending.ts
)
BEFORE_HEAD="$(cd "$TMP_REPO" && git rev-parse HEAD)"
rc=0
BLOCK_LOG="$(cd "$TMP_REPO" && PATH="$FIXTURE_PATH" git commit -q -m "should be blocked" 2>&1)" || rc=$?
AFTER_HEAD="$(cd "$TMP_REPO" && git rev-parse HEAD)"
if [ "$rc" -ne 0 ] && [ "$BEFORE_HEAD" = "$AFTER_HEAD" ]; then
    log "PASS (a): lint エラーを含む staged TS/JS ファイルで commit がブロックされた（rc=$rc）"
else
    fail "(a): lint エラーを含む commit がブロックされなかった（rc=$rc, HEAD changed=$([ "$BEFORE_HEAD" != "$AFTER_HEAD" ] && echo yes || echo no)）: $BLOCK_LOG"
fi
if ! printf '%s' "$BLOCK_LOG" | grep -q "no-unused-vars"; then
    fail "(a): ESLint の失敗理由（no-unused-vars）が出力に含まれていない: $BLOCK_LOG"
fi

# ── (b) --no-verify で bypass できること ────────────────────────────────────
rc=0
BYPASS_LOG="$(cd "$TMP_REPO" && PATH="$FIXTURE_PATH" git commit -q --no-verify -m "bypassed" 2>&1)" || rc=$?
AFTER_BYPASS_HEAD="$(cd "$TMP_REPO" && git rev-parse HEAD)"
if [ "$rc" -eq 0 ] && [ "$AFTER_BYPASS_HEAD" != "$BEFORE_HEAD" ]; then
    log "PASS (b): git commit --no-verify で lint エラーを含む staged ファイルの commit が成立した"
else
    fail "(b): --no-verify での bypass が成立しなかった（rc=$rc）: $BYPASS_LOG"
fi

# ── (c-2) hook インストール済みで prek が解決不能になった場合の挙動確認 ──────
# 実プロジェクトの node_modules を一切変更せず、isolated fixture 内の
# .git/hooks/pre-commit に埋め込まれた絶対パス参照のみを書き換えてシミュレートする。
python3 - "$TMP_REPO/.git/hooks/pre-commit" <<'PYEOF'
import re
import sys

hook_path = sys.argv[1]
with open(hook_path, "r", encoding="utf-8") as fh:
    content = fh.read()

updated = re.sub(
    r'PREK="[^"]+"\n',
    'PREK="/nonexistent/prek-binary-does-not-exist"\n',
    content,
    count=1,
)
if updated == content:
    raise SystemExit("failed to patch PREK path in fixture hook script")

with open(hook_path, "w", encoding="utf-8") as fh:
    fh.write(updated)
PYEOF

(
    cd "$TMP_REPO"
    printf 'const unusedVariableAfterBreak = 1;\n' > after-break.js
    git add after-break.js
)
BEFORE_BROKEN_HEAD="$(cd "$TMP_REPO" && git rev-parse HEAD)"
rc=0
# prek 実体を含まない最小 PATH で実行し、"prek バイナリが解決できない" 状態を再現する
BROKEN_LOG="$(cd "$TMP_REPO" && PATH="/usr/bin:/bin" HOME="$HOME" git commit -q -m "should fail due to missing prek" 2>&1)" || rc=$?
AFTER_BROKEN_HEAD="$(cd "$TMP_REPO" && git rev-parse HEAD)"
if [ "$rc" -ne 0 ] && [ "$BEFORE_BROKEN_HEAD" = "$AFTER_BROKEN_HEAD" ]; then
    log "PASS (c-2): hook インストール済みで prek が解決不能な場合、commit は lint 内容に関係なく fail-closed でブロックされた（rc=$rc）"
else
    fail "(c-2): prek 解決不能時に想定と異なる結果になった（rc=$rc）: $BROKEN_LOG"
fi

# ── 判定 ─────────────────────────────────────────────────────────────────────
if [ "$FAILURES" -eq 0 ]; then
    log "ALL CHECKS PASSED (a, b, c-1, c-2)"
    exit 0
else
    log "$FAILURES check(s) FAILED"
    exit 1
fi
