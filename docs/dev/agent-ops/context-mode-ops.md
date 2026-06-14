# context-mode 運用ドキュメント

関連 Issue: #828  
Parent: #813  
作成日: 2026-06-14  
context-mode バージョン: v1.0.162

このドキュメントは context-mode (Elastic License 2.0) の以下を記録する:
- Storage / persistence の解決順序と実態
- Purge コマンドと手動削除手順
- Secret 混入時の incident response 手順
- Elastic License 2.0 (ELv2) notice policy
- fetch policy と permission deny の用語分離
- repo commit 禁止方針

## 1. Effective Storage Root の解決順序

context-mode v1.0.162 は以下の優先順序で storage root を決定する。

| 優先度 | 環境変数 / ソース | 説明 |
|--------|------------------|------|
| 1 | `CONTEXT_MODE_DIR` | 最優先。明示指定した storage root |
| 2 | `CLAUDE_PLUGIN_DATA` | Claude Code plugin data ディレクトリ |
| 3 | adapter default | context-mode adapter が決定するデフォルト root |
| 4 | `<root>/sessions` | セッションデータ (SQLite DB) |
| 5 | `<root>/content` | コンテンツ / インデックスデータ (SQLite FTS5) |

### 実環境での確認方法

```bash
# ctx-doctor を実行して Storage paths を確認する
npx context-mode doctor
# または MCP tool として
mcp__context-mode__ctx_doctor
```

ctx-doctor-result.json に記録された実測値（v1.0.162）:
- sessions: `<HOME>/.claude/context-mode/sessions` (adapter default)
- content: `<HOME>/.claude/context-mode/content` (adapter default)
- stats: `<HOME>/.claude/context-mode/sessions` (adapter default)

`<HOME>` は実行環境の home ディレクトリ（マスク済み）。実際のパスは `ctx_doctor` で確認する。

証跡: `.claude/artifacts/context-mode/persistence-proof.json` (schema: `context_mode_persistence_proof_v1`)

## 2. Purge コマンドと手動削除手順

### v1.0.162 で実在確認済みの手順のみ記録

以下は `registered-tools.json` で registered_tools に存在確認済み（v1.0.162）の方法。

#### 2a. MCP tool: ctx_purge

```
# MCP tool として呼び出す（Claude Code セッション内）
mcp__context-mode__ctx_purge
```

`ctx_purge` は registered_tools に存在し、permission_policy は `registered`（deny なし）。

#### 2b. slash command: /context reset

セッション内でコンテキストをリセットする slash command。

```
/context reset
```

#### 2c. fallback: 手動削除（plugin 停止後）

plugin を無効化した後、storage root 配下のファイルを手動削除する。

```bash
# sessions DB の削除（<HOME> は実際の home path に置換する）
rm -rf "<HOME>/.claude/context-mode/sessions/"

# content / index の削除
rm -rf "<HOME>/.claude/context-mode/content/"
```

注意: 削除前に `ctx_doctor` で実際の storage path を確認すること。
`CONTEXT_MODE_DIR` を設定している場合はそのパスが優先される。

### 記録しない手順

v1.0.162 で実在未確認のコマンド（`context-mode purge --dry-run` 等のCLI直接呼び出し）は
このドキュメントに記録しない。旧 rollback.md に記載があった場合は legacy として読み替える。

## 3. Secret 混入時の Incident Response

secret が context-mode の index / cache に混入した場合は以下の順序で対応する。

| ステップ | アクション | 説明 |
|----------|-----------|------|
| 1. stop | セッション停止 | 現在のセッションを即座に停止し、context-mode を無効化する |
| 2. isolate | 隔離 | 影響範囲を特定し、他セッションへの伝播を防ぐ |
| 3. identify | 特定 | 混入した secret の種別（token / key / password 等）を特定する |
| 4. purge | 削除 | `ctx_purge` または手動削除で index / cache を完全削除する |
| 5. verify zero hit | ゼロ確認 | purge 後に secret が残存しないことを確認する |
| 6. rotate | ローテーション | 混入した secret を無効化し、新しい credential に差し替える |
| 7. redact evidence | 証跡の redaction | 証跡ファイルに secret が残存する場合は redaction 処理を行う |

### 詳細手順

**ステップ 1: stop**
```bash
# context-mode plugin を無効化する
claude plugins disable context-mode
# または設定ファイルの enabledPlugins を false にする
```

**ステップ 4: purge**
```bash
# MCP tool（context-mode が稼働中の場合）
mcp__context-mode__ctx_purge

# fallback: 手動削除
rm -rf "<CONTEXT_MODE_DIR_OR_DEFAULT>/"
```

**ステップ 5: verify zero hit**
```bash
# storage root が空になっていることを確認する
ls "<HOME>/.claude/context-mode/"
# または ctx_doctor で storage stats を確認する
```

**ステップ 7: redact evidence**
- `.claude/artifacts/context-mode/` 配下の証跡ファイルに secret が含まれる場合は
  `<REDACTED>` に置換してから commit する。
- home path は `<HOME>` に置換済みであること（`redaction.home_path_masked: true`）。

## 4. Fetch Policy と Permission Deny の用語分離

`ctx_fetch_and_index` の制御には 2 つの独立した概念がある。混在させないこと。

### CTX_FETCH_STRICT（環境変数）

upstream context-mode が提供する環境変数による追加安全策。

- `CTX_FETCH_STRICT=1` を設定すると loopback / RFC1918 / ULA 等の private address への
  fetch を upstream 側でブロックする。
- **この環境変数と permission deny は独立した制御層である。**
- permission deny が存在する場合、MCP tool 呼び出し自体が Claude Code 側でブロックされるため、
  `CTX_FETCH_STRICT` の有無に関わらず effective となる。

### project permission deny（settings.json）

`.claude/settings.json` の `permissions.deny` に `mcp__context-mode__ctx_fetch_and_index`
を登録することで、MCP tool 呼び出し自体を Claude Code 側でブロックする。

```json
{
  "permissions": {
    "deny": [
      "mcp__context-mode__ctx_fetch_and_index"
    ]
  }
}
```

### 現行プロジェクトポリシー（#883/PR #887 後の実効設定）

`.claude/settings.json` の `permissions.deny` に以下を登録済み（#883/PR #887 で確定）:
- `mcp__context-mode__ctx_execute`
- `mcp__context-mode__ctx_batch_execute`
- `mcp__context-mode__ctx_execute_file`
- `mcp__context-mode__ctx_fetch_and_index`

この deny は **project settings（main branch の .claude/settings.json）に適用済み**。
旧ドキュメントに記載があった "experiment-only" や "2 deny entries only" は
#883/PR #887 以前の状態であり、現在は legacy。

## 5. ELv2 (Elastic License 2.0) Notice Policy

context-mode は Elastic License 2.0 (ELv2) で提供されている。

### 利用条件 matrix

| 用途 | 可否 | 説明 |
|------|------|------|
| internal use | 許可 | 社内 / プロジェクト内での利用は自由 |
| no vendoring | 禁止 | source の fork / vendoring は禁止（Out of Scope） |
| no hosted managed service | 禁止 | 外部向け SaaS / managed service としての提供は禁止 |
| no notice removal | 禁止 | license notice を削除してはならない |
| modified copy notice | 必要 | fork / modify した場合は変更した旨の notice が必要 |

### notice 保持義務

- context-mode パッケージの ELv2 license notice は削除・改変しない。
- repo に context-mode のソースを vendoring しない（npm install -g のみ）。
- `LOOP_PROTOCOL` プロジェクトでの利用は internal use に該当し、ELv2 上許可される。

### 参照

- ELv2 原文: https://www.elastic.co/licensing/elastic-license
- context-mode npm: https://www.npmjs.com/package/context-mode

## 6. Repo Commit 禁止方針

context-mode が生成する以下のファイルは **repo に commit してはならない**。

| ファイル種別 | 説明 | 禁止理由 |
|------------|------|---------|
| DB files | `sessions/*.db`, `content/*.db` | SQLite raw data には index された内容が含まれる |
| index files | FTS5 index files | 取得・索引されたコンテンツが含まれる |
| cache files | TTL cache | 外部 URL のキャッシュが含まれる |
| raw fetched body | ctx_fetch_and_index の取得結果 | 著作権・secret 混入リスク |

### .gitignore の確認

context-mode の storage root（デフォルト: `~/.claude/context-mode/`）は repo 外にあるため、
通常は repo に入らない。ただし `CONTEXT_MODE_DIR` をリポジトリ内に設定した場合は
`.gitignore` で除外すること。

```gitignore
# context-mode storage (if CONTEXT_MODE_DIR points inside repo)
.context-mode/
```

## 7. Stale Claim の更新記録（#828 での修正）

以下の stale claim を本 Issue (#828) で修正または legacy 明示した。

### context-mode-rollback.md の stale claim

- `context-mode purge --dry-run` / `context-mode purge` CLI 直接呼び出し: 
  v1.0.162 での実在未確認。手順 2c の fallback（手動削除）を使用すること。

### registered-tools.json の stale claim

- `deny_basis` の `mcp__context-mode-experiment__ctx_execute` 記述:
  現在の実効設定は `mcp__context-mode__ctx_execute`（`-experiment` サフィックスなし）。
  #883/PR #887 で更新済み。

### version-provenance.json の stale claim

- `enabled_in` の `project settings (enabledPlugins.context-mode@context-mode: true)` 記述:
  #883/PR #887 で main branch の settings.json に適用済み（experiment-profile-only は古い状態）。

## 関連ドキュメント

- `docs/dev/agent-ops/context-mode-rollback.md` — ロールバック手順
- `docs/dev/agent-ops/context-mode-fetch-policy.md` — fetch policy 詳細
- `docs/dev/agent-ops/context-mode-cwd-quarantine.md` — CWD quarantine policy
- `.claude/artifacts/context-mode/persistence-proof.json` — storage 実測証跡
- `.claude/artifacts/context-mode/registered-tools.json` — tool 登録状態
- `.claude/artifacts/context-mode/permission-policy.json` — permission policy 証跡

## 関連 Issue / PR

- #813: parent（context-mode 段階的導入）
- #824: context-mode profile 設定（実験用 profile 導入）
- #825: deny rule negative test
- #826: execution-like tools quarantine
- #827: fetch strict 隔離
- #828: 本 Issue（persistence / purge / ELv2 docs）
- #856: ctx_fetch_and_index deny（PR）
- #866: profile-isolation artifact（PR）
- #880: quarantine matrix docs（PR）
- #883: execution-like tools quarantine fix（CI 接続含む）
- #887: PR #883 に対応する PR
- #892: CTX_FETCH_STRICT=1 network negative test
