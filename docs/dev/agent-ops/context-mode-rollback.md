# context-mode ロールバック手順

関連 Issue: #824, #828  
作成日: 2026-06-13  
更新日: 2026-06-14 (#828: stale claim 修正)

このドキュメントは Claude Code で導入した context-mode MCP server を
安全にロールバックするための手順を記述します。

## 前提

- context-mode は **project settings（.claude/settings.json）** で導入済み（#883/PR #887 後）
- #883/PR #887 以前は「実験用 profile のみ」での導入だったが、
  現在は main branch の `.claude/settings.json` に `enabledPlugins` と `permissions.deny` が適用済み
- ロールバック後に `.claude/artifacts/context-mode/` は保持する（証跡として残す）

> **Legacy 注記**: 旧ドキュメントに「context-mode は実験用 profile / scope にのみ導入済み」
> とあった記述は #883/PR #887 以前の状態。現在は main branch に適用済み。

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

### 2a. `.claude/settings.json` から enabledPlugins を無効化

`enabledPlugins["context-mode@context-mode"]` を `false` に変更するか削除します。

変更前:
```json
{
  "enabledPlugins": {
    "context-mode@context-mode": true
  }
}
```

変更後:
```json
{
  "enabledPlugins": {
    "context-mode@context-mode": false
  }
}
```

### 2b. permissions.deny から context-mode エントリを削除（optional）

ロールバック時にも deny entries を残すことで、誤再有効化時の安全網を維持できます。
削除する場合は以下の entries を `permissions.deny` から削除します:
- `"mcp__context-mode__ctx_execute"`
- `"mcp__context-mode__ctx_batch_execute"`
- `"mcp__context-mode__ctx_execute_file"`
- `"mcp__context-mode__ctx_fetch_and_index"`

> **Legacy 注記**: 旧ドキュメントに「2 deny entries のみ削除」と記載があったが、
> #883/PR #887 で deny entries が 4 件に拡張済み。

## 手順 3: Settings Diff の確認と Revert

```bash
# 変更差分を確認する
git diff .claude/settings.json

# 必要であればロールバック前の状態に戻す
git checkout main -- .claude/settings.json
```

## 手順 4: context-mode データの Purge

context-mode が DB / index を作成している場合は purge します。

### v1.0.162 で実在確認済みの手順

#### 4a. ctx_purge MCP tool（推奨）

context-mode が稼働中のセッション内で MCP tool を呼び出します:
```
mcp__context-mode__ctx_purge
```

#### 4b. slash command（セッションリセット）

```
/context reset
```

#### 4c. fallback: 手動削除（plugin 停止後）

plugin 停止後に storage root 配下のファイルを手動削除します。
storage root は `ctx_doctor` で確認してください（デフォルト: `~/.claude/context-mode/`）。

```bash
# storage root の確認（ctx-doctor-result.json を参照）
cat .claude/artifacts/context-mode/ctx-doctor-result.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for c in d.get('checks', []):
    if 'Storage' in c.get('message', ''):
        print(c['message'])
"

# sessions DB の削除（実際のパスは上記で確認すること）
rm -rf "$HOME/.claude/context-mode/sessions/"

# content / index の削除
rm -rf "$HOME/.claude/context-mode/content/"
```

> **Legacy 注記**: 旧ドキュメントに `context-mode purge --dry-run` / `context-mode purge` の
> CLI 直接呼び出し手順があったが、v1.0.162 での実在が未確認のため削除した（#828）。
> 代わりに上記 4a/4b/4c を使用すること。

`CONTEXT_MODE_DIR` が設定されている場合:
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

Claude Code セッション内で以下を実行して context-mode が表示されないことを確認します:

```
/mcp
```

期待結果:
- `context-mode` がリストに表示されない
- またはステータスが `disconnected` / `disabled` になっている

## ロールバック完了の確認

ロールバック完了後に以下を確認します:

- [ ] `npm list -g context-mode` が `(empty)` を返す
- [ ] `.claude/settings.json` に `enabledPlugins.context-mode@context-mode: true` が存在しない
- [ ] Claude Code セッションの `/mcp` で `context-mode` が表示されない
- [ ] context-mode storage root（`~/.claude/context-mode/`）が空またはなし

## 証跡の保持

ロールバック後も以下のファイルは削除しないでください（#824〜#828 の証跡として保持）:

- `.claude/artifacts/context-mode/version-provenance.json`
- `.claude/artifacts/context-mode/ctx-doctor-result.json`
- `.claude/artifacts/context-mode/registered-tools.json`
- `.claude/artifacts/context-mode/profile-isolation.json`
- `.claude/artifacts/context-mode/config-diff.json`
- `.claude/artifacts/context-mode/persistence-proof.json`

## 関連ドキュメント

- [#824 Issue](https://github.com/squne121/loop-protocol/issues/824)
- [#825 deny rule negative test](https://github.com/squne121/loop-protocol/issues/825)
- [#828 persistence / purge / ELv2 notice policy](https://github.com/squne121/loop-protocol/issues/828)
- [#883 execution-like tools quarantine fix](https://github.com/squne121/loop-protocol/issues/883)
- `docs/dev/agent-ops/context-mode-ops.md` — 詳細 ops ドキュメント
