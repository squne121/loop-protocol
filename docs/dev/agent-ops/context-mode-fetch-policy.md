# context-mode fetch policy

`ctx_fetch_and_index` に関する利用条件、禁止条件、セキュリティ上の注意事項を記録する。

## 概要

`context-mode` plugin が提供する `ctx_fetch_and_index` は、外部 URL のコンテンツを取得して
context window に取り込む MCP ツールである。URL fetch、HTML/Markdown 変換、chunk/index、TTL
キャッシュ保存という複数の処理を内包するため、以下のリスクが同時に発生し得る。

- SSRF（Server-Side Request Forgery）— private network への意図しない接続
- metadata endpoint 接触 — クラウドインスタンスの IAM credential 漏洩
- DNS rebinding / redirect 経由の private network 到達
- 外部コンテンツ由来の prompt injection
- index 汚染（悪意ある外部コンテンツが context に混入）

## 通常利用条件

`ctx_fetch_and_index` を利用する場合は、以下の条件を全て満たすこと。

1. 対象 URL が public internet 上の信頼できるホストである
2. URL が private / link-local / loopback / metadata endpoint でないことを事前に確認する
3. 取得コンテンツが LLM context に直接渡されることを前提に、prompt injection リスクを評価する
4. CONTEXT_MODE_DIR を isolated temp directory に設定し、TTL cache 汚染を防ぐ (`ttl: 0` または `force: true`)
5. 不要になったキャッシュは `ctx_purge` で速やかに削除する

## 禁止条件（Stop Condition 該当）

以下のいずれかに該当する場合は実行を停止し、人間判断を仰ぐ。

- `mcp__context-mode__ctx_fetch_and_index` が `permissions.deny` から除去される
- loopback / localhost / RFC1918 / ULA / link-local / metadata endpoint を fetch 対象とする
- redirect 先が private range に含まれる URL を fetch する
- DNS rebinding 攻撃の可能性がある untrusted hostname を fetch する
- 取得した外部コンテンツをサニタイズせずに LLM context に渡す

## 現行プロジェクトポリシー（#827 / PR #856）

`LOOP_PROTOCOL` では `ctx_fetch_and_index` を `.claude/settings.json` の `permissions.deny` に
登録し、**committed deny** として維持する。

```json
"deny": [
  "mcp__context-mode__ctx_fetch_and_index"
]
```

この deny は以下の設計判断に基づく。

- 外部 URL fetch は SSRF / SSRF 類似のリスクを内包するため、デフォルト deny が安全側
- `CTX_FETCH_STRICT=1` は upstream context-mode での追加安全策だが、deny が存在する間は
  MCP ツール呼び出し自体がブロックされるため `CTX_FETCH_STRICT` の有無に関わらず effective である
- context-mode upstream は loopback / RFC1918 / ULA を既定では許可する設計（local dev/internal fetch 用）
  であり、`CTX_FETCH_STRICT=1` で追加ブロックされる。このためプロジェクトポリシーとして deny を維持する

### ctx_fetch_strict_effective の定義

- `ctx_fetch_strict_configured: true` — `mcp__context-mode__ctx_fetch_and_index` が deny に存在する
- `ctx_fetch_strict_effective: true` — deny により MCP ツール呼び出しが実際にブロックされる
  （環境変数 `CTX_FETCH_STRICT=1` または deny entry のいずれかが存在する場合に true）

## prompt injection 扱い

外部 URL から取得したコンテンツには prompt injection payload が含まれる可能性がある。

- 外部コンテンツを LLM context に渡す前にサニタイズする（HTML タグ除去、script 除去等）
- 取得コンテンツをそのまま `ctx_index` で保存しない（search 経由で context に混入するため）
- prompt injection が疑われる場合は `ctx_purge` で該当 URL の index を即座に削除する

Claude Code の公式ドキュメントは、外部コンテンツを取得する MCP server の prompt injection
リスクを警告している。本プロジェクトでは `ctx_fetch_and_index` の deny によりこのリスクを
根本的に排除する。

## cache / purge

`ctx_fetch_and_index` は TTL 24h のキャッシュ（`CONTEXT_MODE_DIR` 内の SQLite DB）を使用する。

- キャッシュが存在すると fetch 経路を通らず、strict block の検証が機能しない可能性がある
- テスト・検証時は `CONTEXT_MODE_DIR` を isolated temp directory に設定し、テスト後に削除する
- 本番利用時は `ttl: 0` または `force: true` を指定してキャッシュをバイパスする
- 不要な index は `ctx_purge` で削除する

```bash
# 特定 URL の purge
ctx_purge --url <URL>

# 全キャッシュの purge
ctx_purge --all
```

## #828 への依存関係

`ctx_fetch_and_index` のキャッシュ persistent policy および purge 手順の詳細は #828
（context-mode index persistence / purge 運用）に委譲する。

- #827: fetch strict 隔離、URL policy matrix、adversarial cases、network negative test suite
- #828: index persistence policy、purge 自動化、cleanup proof の詳細運用

## URL policy matrix

以下の URL カテゴリは block 対象（`_is_private_url` classifier による検証）。

| カテゴリ | 例 | ブロック理由 |
|---|---|---|
| loopback IPv4 | `127.0.0.1`, `127.x.x.x` | ローカルサービスへの SSRF |
| localhost | `localhost` | ローカルサービスへの SSRF |
| loopback IPv6 | `::1` | ローカルサービスへの SSRF |
| RFC1918 | `10.x`, `172.16-31.x`, `192.168.x` | private network への接続 |
| link-local | `169.254.x.x` | AWS/GCP/Azure IMDS metadata |
| metadata endpoint | `169.254.169.254`, `metadata.google.internal` | IAM credential 漏洩リスク |
| ULA IPv6 | `fc00::/7`, `fd00::/7` | private IPv6 network |
| link-local IPv6 | `fe80::/10` | link-local IPv6 |
| IPv6-mapped IPv4 | `::ffff:192.168.x.x` | IPv4 private を IPv6 で迂回 |
| numeric IPv4 | `2130706433` (= 127.0.0.1) | 数値エンコードによる迂回 |
| URL credential | `http://user:pass@...` | credential 付き URL |
| non-http(s) scheme | `file://`, `ftp://`, `data:` | 意図しないローカルアクセス |

## 関連 Issue・PR

- #827: 本ポリシー・fetch strict 隔離実装
- #828: index persistence / purge 運用（依存先）
- PR #856: `ctx_fetch_and_index` を `permissions.deny` に追加（先行 PR）
