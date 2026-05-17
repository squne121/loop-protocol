# 開発ワークフロー標準手順（LOOP PROTOCOL）

> このドキュメントは人間（開発者）向けのリファレンスです。  
> AI エージェント向けの実行指示は `.claude/skills/issue-driven-dev/SKILL.md` に定義されています。

## 現在のフェーズ

**開発基盤整備中（M1: Foundation Gate / v0.1.x）**

このフェーズでは、Combat 実装等のゲーム機能追加よりも、
AI 駆動開発を安全に継続するための仕組み整備を優先している。

---

## Issue から PR までの標準手順

### 1. Issue の準備（Human Decision）

AI に Issue を渡す前に、以下が揃っていることを確認する。

**ready 条件：**
- [ ] タイトルと目的が明確に書かれている
- [ ] 受け入れ条件（Acceptance Criteria）が箇条書きで列挙されている
- [ ] 非ゴール（やらないこと）が明示されている
- [ ] 関連 Issue がリンクされている（あれば）

**Human Decision が必要な条件：**
- `src/state` や `src/render` の境界を変更する場合
- 新しい外部依存（パッケージ）を追加する場合
- `assets/` や `LICENSES/` 配下に変更が必要な場合
- 複数 Issue にまたがる仕様変更が発生した場合

### 2. AI への依頼

Claude Code に対して Issue 番号を伝える。  
例：「`#1` を実装してほしい」

AI は `.claude/skills/issue-driven-dev/SKILL.md` の EPIC フローに従い、
コードを書く前に実装 Plan を提示する。

### 3. Plan のレビューと承認（Human Decision）

AI が提示した Plan を確認し、以下をチェックする：

- [ ] 変更対象ファイルが Issue のスコープと一致している
- [ ] CLAUDE.md の制約（分離・固定タイムステップ等）に違反していない
- [ ] 非ゴールのファイルが含まれていない
- [ ] `assets/` が変更対象に含まれていない

問題がなければ「OK」「進めて」等で承認する。  
不明点があれば質問してから承認する。**Plan を承認するまで AI はコードを変更しない。**

### 4. 実装・検証（AI が実行）

AI が以下を順番に実行する：

1. Plan に従って実装
2. `pnpm typecheck` — 型エラーのチェック
3. `pnpm lint` — Lint エラーのチェック
4. `pnpm test` — テストの通過確認
5. `pnpm build` — ビルドの通過確認
6. PR の作成（受け入れ条件の達成状況を PR 本文に記載）

### 5. PR のレビュー（Human Decision）

AI が作成した PR を確認する：

- [ ] 受け入れ条件が全て達成されているか
- [ ] 変更範囲が Plan と一致しているか
- [ ] 検証コマンドが全て通過しているか
- [ ] スコープ外の変更が含まれていないか

---

## docs 更新が必要な条件

以下の変更を行う場合、対応する docs も同じ PR で更新する。

| 変更内容 | 更新が必要なドキュメント |
|---|---|
| アーキテクチャの境界変更 | `docs/adr/` に ADR を追加 |
| 新機能の仕様追加 | `docs/product/` の仕様書を更新 |
| 開発フロー自体の変更 | このファイル（`docs/dev/workflow.md`）を更新 |
| ディレクトリ構造の変更 | `docs/dev/directory-structure.md` を更新 |

---

## 1 Issue = 1 PR ルール

- 1 つの Issue に対して必ず 1 つの PR を作る
- 実装中に別の問題を発見した場合は新しい Issue を立てて別 PR で対応する
- 複数 Issue をまとめて 1 PR にすることは原則禁止

---

## 関連ファイル

- `CLAUDE.md` — AI に対するプロジェクト全体の制約（憲法）
- `.claude/rules/project-constitution.md` — 憲法の補足
- `.claude/skills/issue-driven-dev/SKILL.md` — AI 向け実行手順
