# context-mode ロールバック手順

関連 Issue: #824
作成日: 2026-06-13

このドキュメントは Claude Code 実験用 profile で導入した context-mode MCP server を
安全にロールバックするための手順を記述します。

## 前提

- context-mode は実験用 profile / scope (`experiment`) にのみ導入済み
- main profile / main settings は変更していない（`profile-isolation.json` で確認可能）
- ロールバック後に `.claude/artifacts/context-mode/` は保持する（証跡として残す）

## 手順 1: Plugin の無効化 / アンインストール

### 1a. Claude Code plugin 経由でインストールした場合

```bash
# plugin 一覧確認
claude plugins list

# context-mode plugin を無効化
claude plugins disable context-mode

# plugin を完全削除（オプション）
claude plugins uninstall context-mode
```

### 1b. npm グローバルインストールした場合

```bash
# インストール済みか確認
npm list -g context-mode

# アンインストール
npm uninstall -g context-mode
```

## 手順 2: MCP Server の登録解除

### 2a. `.claude/settings.json` から mcpServers エントリを削除

`mcpServers.context-mode-experiment` ブロック全体を削除します。

変更前（ロールバック対象）:
```json
{
  "mcpServers": {
    "context-mode-experiment": { ... }
  }
}
```

変更後（ロールバック後）:
```json
{
  // mcpServers キー自体を削除するか空オブジェクトにする
}
```

### 2b. permissions.deny から context-mode エントリを削除

以下の 2 エントリを `permissions.deny` から削除します:
- `"mcp__context-mode-experiment__ctx_execute"`
- `"mcp__context-mode-experiment__ctx_fetch_and_index"`

## 手順 3: Settings Diff の確認と Revert

```bash
# 変更差分を確認する
git diff .claude/settings.json

# 必要であればロールバック前の状態に戻す
git checkout main -- .claude/settings.json
```

## 手順 4: `CONTEXT_MODE_DIR` 配下の Purge（オプション）

context-mode が DB / index を作成している場合は purge します。

```bash
# context-mode が管理する DB のデフォルト場所を確認する
# （インストール済みの場合）
context-mode config show

# purge コマンドを実行する（context-mode がインストールされている場合）
# MCP tool ctx_purge を使用するか、以下のコマンドで実行する
context-mode purge --dry-run   # 削除対象を確認してから実行する
context-mode purge             # 実際に purge する
```

CONTEXT_MODE_DIR が設定されている場合:
```bash
# 環境変数で確認する（context-mode 専用 session で実行すること）
echo $CONTEXT_MODE_DIR

# ディレクトリを削除する（必要な場合のみ）
rm -rf "$CONTEXT_MODE_DIR"
```

注意: artifact ディレクトリ `.claude/artifacts/context-mode/` は証跡として **削除しない** こと。

## 手順 5: `/reload-plugins` で動作確認

Claude Code セッション内で以下を実行して plugins が再読み込みされていることを確認します:

```
/reload-plugins
```

## 手順 6: `/mcp` で MCP server 状態を確認

Claude Code セッション内で以下を実行して context-mode-experiment が表示されないことを確認します:

```
/mcp
```

期待結果:
- `context-mode-experiment` がリストに表示されない
- またはステータスが `disconnected` / `disabled` になっている

## ロールバック完了の確認

ロールバック完了後に以下を確認します:

- [ ] `npm list -g context-mode` が `(empty)` を返す
- [ ] `.claude/settings.json` に `mcpServers.context-mode-experiment` が存在しない
- [ ] `.claude/settings.json` の `permissions.deny` に `ctx_execute` / `ctx_fetch_and_index` エントリが存在しない
- [ ] Claude Code セッションの `/mcp` で `context-mode-experiment` が表示されない
- [ ] main profile / main settings が変更されていない（`git diff ~/.claude/settings.json` が空）

## 証跡の保持

ロールバック後も以下のファイルは削除しないでください（#824 の証跡として保持）:

- `.claude/artifacts/context-mode/version-provenance.json`
- `.claude/artifacts/context-mode/ctx-doctor-result.json`
- `.claude/artifacts/context-mode/registered-tools.json`
- `.claude/artifacts/context-mode/profile-isolation.json`
- `.claude/artifacts/context-mode/config-diff.json`

## 関連ドキュメント

- [#824 Issue](https://github.com/squne121/loop-protocol/issues/824)
- [#825 deny rule negative test](https://github.com/squne121/loop-protocol/issues/825)
- [#828 persistence / purge / ELv2 notice policy](https://github.com/squne121/loop-protocol/issues/828)
