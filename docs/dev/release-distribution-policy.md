---
status: draft
authority: "Issue #286"
last_updated_issue: "#286"
scope: "M1 Foundation Gate (v0.1.x)"
---

# リリース・配布方針（Release Distribution Policy）

## 目的 / Purpose

本文書は、LOOP_PROTOCOL における Vite ビルド成果物の取扱方針、M1 Foundation Gate 時点での配布候補評価、および RC（Release Candidate）判断基準の **唯一の正本（SSOT）** である。

本文書は配布判断の評価基準・方針を定める。実際の配布インフラ構築・CI/CD 自動化・収益化・ストア提出は対象外。

## スコープ / Scope

本文書の適用範囲は **M1 Foundation Gate（v0.1.x）** における配布戦略評価と RC 基準策定に限定する。

**In Scope:**
- Vite ビルド成果物（`dist/`）の取扱方針
- 配布候補プラットフォームの評価（ローカル / GitHub Pages / itch.io / その他）
- M1 RC（Release Candidate）checklist の定義
- public hosting における risk gate の定義

**Out of Scope:**
- 配布インフラの実際の構築
- アセットのライセンス監査（SPDX/REUSE 仕様に基づく監査）
- M2 以降の長期的な配布戦略
- CI/CD パイプラインでの配布自動化の実装
- 収益化・ストアへの正式提出

## スコープガード / Scope Guard

`docs/product/mvp-scope.md` の lifecycle status が `draft` の間は、本 RC checklist は **M1 evidence-gathering / internal RC** に限定する。

- public release 判断 / external distribution は、人間承認または `docs/product/mvp-scope.md` が `accepted` になるまで **blocked**
- playtest protocol の結果は release 判定材料として利用可能だが、`draft` ステータスの文書を normative release gate として扱わない
- #405（ssot-discovery に lifecycle status を含める）の解決前は、AI エージェントが draft spec を normative と誤認するリスクに注意

## M1 への適用 / M1 Applicability

本文書は M1 Foundation Gate での内部 RC 判断・品質ゲート確認に使用する。M1 完了後のリリース判断基準として参照可能であるが、public distribution の実行は人間承認を必要とする。

---

## Vite Build Artifact Policy

| 項目 | 方針 |
|---|---|
| `dist/` の位置づけ | Vite が生成する **再生成可能なビルド成果物（generated output）** |
| `dist/` の main commit | **禁止** — `dist/` は `main` ブランチに commit しない |
| RC artifact の紐づけ | commit SHA / build command / quality gate 結果と紐づける（ビルドの再現可能性を保証） |
| ローカル検証 | `pnpm build` でビルド後、`pnpm preview` でローカル確認 |
| `vite preview` の位置づけ | **ローカル production build 確認ツール**。本番サーバーとして使用しない |

### Vite build コマンド

```bash
# ビルド
pnpm build    # → dist/ を生成

# ローカル確認（production build の動作確認のみ）
pnpm preview  # → localhost で dist/ をサーブ（production server ではない）
```

---

## Distribution Candidate Matrix

| Candidate | M1 での利用 | 強み | リスク / 制約 | Decision |
|---|---|---|---|---|
| Local preview | Yes | 最速・安全な動作確認。外部依存なし | 外部への直接共有不可 | **Approved** |
| GitHub Pages | Provisional | GitHub Actions との連携が容易。静的ホスティング | `vite.config` で `base: '/<REPO>/'` 設定が必要。public 公開される点に注意 | **Provisional** |
| itch.io HTML5 ZIP | No（M1 では deferred） | HTML5 ゲームの配布に最適。`butler` での差分デプロイ可 | ZIP 制約あり（詳細は Target-Specific Constraints 参照） | **Deferred** |
| Other / future channels | No（M1 では deferred） | プラットフォーム非依存の将来拡張 | 現段階では運用・構築コストが大きい | **Deferred** |

---

## Target-Specific Constraints

### GitHub Pages

GitHub Pages を使う場合は以下の制約を必ず守る。

| 制約 | 詳細 |
|---|---|
| base path | `vite.config.ts` で `base: '/<REPO>/'`（例: `/loop-protocol/`）を設定する。設定漏れでアセットの 404 が発生する |
| Actions artifact path | GitHub Actions から Pages にデプロイする際は `./dist` を artifact として upload する（`actions/upload-pages-artifact` の `path: ./dist`） |
| 公開範囲 | GitHub Pages は private リポジトリでも **public サイト**になる。Public Hosting Risk Gate を必ず通過すること |
| deploy 手順 | `actions/deploy-pages` を使う公式手順を参照。`vite preview` を deploy 先に使用しない |

### itch.io HTML5 ZIP

itch.io への HTML5 配布（M1 では Deferred）には以下の制約がある。

| 制約 | 詳細 |
|---|---|
| ZIP ルート | ZIP 内のルートに `index.html` が必要。サブディレクトリへの配置不可 |
| パス | 相対パスを使用する。絶対パスはパス解決に失敗する可能性がある |
| ファイル名 | case-sensitive（大文字小文字区別あり）。OS ごとの挙動差異に注意 |
| ファイル数制限 | 展開後 **1,000 ファイル以下** |
| 容量制限（総計） | 展開後 **500 MB 以下** |
| 容量制限（単一ファイル） | **200 MB 以下** |
| ゲーム設定 | iframe / fullscreen / mobile 設定をダッシュボードで確認する |

### Public Hosting Risk Gate

GitHub Pages・itch.io を含む public 配布に進む前に、以下をすべて確認する。

- **secrets の不在**: API キー・認証情報・内部エンドポイントが `dist/` に含まれていない
- **PII の不在**: 個人情報・開発者情報が `dist/` に含まれていない
- **source map の扱い**: 意図しない source map が公開されていない（`vite.config.ts` の `build.sourcemap` 設定を確認）
- **アセットライセンス**: 未ライセンスアセット・ライセンス要件を満たしていないアセットが含まれていない
- **開発者限定 artifact の不在**: セッション録画・デバッグ証跡・internal metadata が `dist/` に含まれていない

---

## Release Candidate Checklist

`docs/product/mvp-scope.md` が `draft` の間は、本 checklist は M1 evidence-gathering / internal RC 用途に限定する。

| criterion | evidence | command / manual check | blocker? | owner |
|---|---|---|---|---|
| pnpm typecheck PASS | CI log / ローカル実行結果 | `pnpm typecheck` | Yes | AI / human |
| pnpm lint PASS | CI log / ローカル実行結果 | `pnpm lint` | Yes | AI / human |
| pnpm test PASS | CI log / ローカル実行結果 | `pnpm test` | Yes | AI / human |
| pnpm build 成功 | CI log / ローカル実行結果 | `pnpm build` | Yes | AI / human |
| pnpm preview ローカルスモーク | ブラウザで `localhost:4173` を目視確認 | `pnpm preview` → ブラウザ確認 | Yes | human |
| `dist/` が main に commit されていない | `git status` / `.gitignore` 確認 | `git ls-files dist/` → 空であること | Yes | AI / human |
| Vite base path 確認 | `vite.config.ts` の `base` 設定 | `vite.config.ts` を確認 | Target-dependent | human |
| source map ポリシー確認 | `vite.config.ts` の `build.sourcemap` | `grep sourcemap vite.config.ts` | Yes（public deploy 前） | human |
| secret / PII 不在 | `dist/` の静的確認 | `dist/` を grep / レビュー | Yes（public deploy 前） | human |
| アセットライセンス準備状況 | `LICENSES/` 確認・アセット棚卸し | `LICENSES/` ディレクトリ確認 | Yes（public release 前） | human |
| GitHub Pages 適用可能性 | base path 設定確認・Actions workflow 確認 | `vite.config.ts` + `.github/workflows/` 確認 | Target-dependent | human |
| itch.io ZIP ルート `index.html` 確認 | ZIP 展開後の `index.html` 存在確認 | ZIP を展開して確認 | Yes（itch.io deploy 前） | human |
| M1 scope / playtest evidence 整合 | `docs/product/mvp-scope.md` 確認 | MVP criteria と実装の照合 | Yes | human |

---

## 非ゴール / Non-Goals

- 配布インフラ（GitHub Pages / itch.io Butler 等）の実際のセットアップ・構築
- アセットのライセンス監査（SPDX/REUSE 準拠確認）
- M2 以降の長期的な配布・収益化戦略
- CI/CD パイプラインでの自動デプロイ実装
- ストア（Steam / App Store 等）への提出手順

---

## 関連ドキュメント / Related Documents

- `docs/product/mvp-scope.md` — MVP 仮説・scope 境界の正本（lifecycle status が `accepted` になるまで normative release gate として扱わない）
- `docs/product/playtest-protocol.md` — プレイテスト手順・フィードバック分類
- `docs/dev/ssot-registry.md` — SSOT カタログ正本
- `docs/dev/workflow.md` — Issue 駆動開発フロー・release/distribution routing
- `docs/dev/secret-policy.md` — Secret Inventory・no-secret 運用境界
