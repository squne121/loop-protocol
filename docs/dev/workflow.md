# LOOP PROTOCOL 開発運用設計（SSOT）

本ドキュメントは LOOP PROTOCOL における Issue 駆動開発の **単一の真実の情報源（SSOT, Single Source of Truth）** である。
AI エージェント向けの skill / subagent / hook はこの SSOT の実装層であり、本ドキュメントと矛盾した場合は本ドキュメントが優先する。

---

## 現在のフェーズ

**M1: Foundation Gate (v0.1.x) — 開発基盤整備中**

Combat 等のゲーム機能追加よりも、AI 駆動開発を安全に継続するためのハーネス整備を優先する。

---

## 1. 階層構造（Docs as Code & Guarded SDD）

| 階層 | 役割 | 実体 |
|---|---|---|
| **SSOT** | プロジェクトルールの正本（人間可読） | 本ドキュメント、`docs/adr/`、`docs/product/` |
| **確率論的プロンプト** | SSOT を AI 向け実行コンテキストに変換 | `CLAUDE.md` / `.claude/skills/` / `.claude/agents/` |
| **決定論的ガードレール** | AI 逸脱時の物理強制 | Claude Hooks / Git Hooks / GitHub Actions CI |

SSOT を編集したら、対応する確率論的プロンプト層・決定論的ガードレール層を必ず同 PR で更新する。

---

## 2. テスト戦略 — 3 層責務分離（Defense in Depth）

| レイヤー | 実行手段 | 実行内容 | 目的 |
|---|---|---|---|
| 1. AI 自己修復 | Claude Hooks (`PostToolUse`) | 編集ファイルの lint / typecheck | AI への即時フィードバック。CI を消費する前にローカルで自律デバッグ |
| 2. 履歴の保護 | Git Hooks (`pre-commit` / `pre-push`) | 高速検証 (typecheck / lint / unit test) | 壊れたコードが Git 履歴に刻まれるのを物理防止。E2E など重いテストは含めない |
| 3. 最終品質保証 | GitHub Actions (CI) | typecheck + lint + unit + E2E + build | クリーン環境での再現可能な最終確定。PR マージをシステムブロック |

同じテストを複数レイヤーで実行するのは **Defense in Depth（多層防御）** であり、無意味ではない。各層は目的が異なる。

### テストスタイル

- **TDD（テスト駆動開発）**: 実装前に Vitest テストを書く
- **BDD（振る舞い駆動開発 = Behavior-Driven Development）**: テスト名・記述は GIVEN/WHEN/THEN 命名規則を使う
- 実装詳細でなく入出力の振る舞いをアサーションする

---

## 3. Issue 駆動開発フロー

### 3-1. Issue 起票（人間 or 起票担当者）

`.github/ISSUE_TEMPLATE/implementation.yml`（Issue Forms）に従い、以下の必須項目を埋める：

- 背景 / 目的 / 受け入れ条件 / 非ゴール / テスト観点 / 変更許可領域

必須項目を埋めないと `scripts/check-issue-contract.sh` でガードレール検知され、AI エージェントは実装に着手できない。

### 3-2. AI への依頼（人間 → AI）

`/.claude/skills/issue-driven-dev` の起動キーワード（「#番号 を実装」「Issue を PR 化」等）で依頼する。  
**Plan モードは使わない**（廃棄済み）。Issue が実装契約である以上、AI に自然言語で計画を再立案させない。

### 3-3. AI 実装フロー（skill が決定論的に実行）

1. **ガードレール検知** — `scripts/check-issue-contract.sh <番号>`
2. **環境隔離** — `git worktree add .claude/worktrees/issue-<番号>-<slug>` で隔離
3. **実装ディスパッチ** — Issue の変更許可領域に従い、TDD + BDD で直接 Edit
4. **サブエージェント委譲** — `architecture-reviewer` へハンドオフ（メインセッションは PR を起票しない）

### 3-4. サブエージェント監査（architecture-reviewer）

- 権限: Read / Grep / Glob / Bash（読み取り中心）
- 監査対象: git diff × CLAUDE.md 分離原則 × Issue 受け入れ条件 × 変更許可領域
- 合格時のみ `gh pr create` で PR 起票
- 不合格時はメインセッションへ不適合返却

### 3-5. PR レビュー（人間）

PR の確認観点：

- 受け入れ条件チェックリストが全て埋まっているか
- 変更ファイルが「変更許可領域」内か
- `architecture-reviewer` の監査結果に違反がないか
- CI が通過しているか

### 3-6. マージ後のクリーンアップ

```bash
git worktree remove .claude/worktrees/issue-<番号>-<slug>
git branch -d worktree-issue-<番号>-<slug>
```

---

## 4. Worktree 配置の決定事項

- **配置先**: `.claude/worktrees/issue-<番号>-<slug>/`（リポジトリ内）
- **理由**:
  - `permissions.additionalDirectories` は workspace trust prompt をスキップしないため、外部配置は承認マシーン化する
  - 公式推奨は `.claude/worktrees/` + `.gitignore` 除外
  - Context Bloat（ripgrep / LSP の二重読み込み）懸念は sandbox 機能（WSL2 bubblewrap）で対処
- **配置先の `.gitignore`**: `.claude/worktrees/` を除外済み

---

## 5. Human Decision が必要な条件

以下に該当する場合、AI に丸投げせず人間が判断する：

- `src/state` ↔ `src/render` の境界変更
- 新しい外部依存（パッケージ）追加
- `assets/` `LICENSES/` への変更
- 複数 Issue にまたがる仕様変更
- CLAUDE.md の制約変更
- 本ドキュメント（SSOT）の変更

---

## 6. 1 Issue = 1 PR ルール

- 1 つの Issue に対して必ず 1 つの PR を作る
- 実装中に別の問題を発見した場合は新しい Issue を立てる
- 複数 Issue を 1 PR にまとめることは原則禁止
- skill 内・サブエージェント内でこのルールを物理強制する

---

## 7. docs 更新が必要な条件

| 変更内容 | 更新が必要なドキュメント |
|---|---|
| 開発フロー自体の変更 | 本ドキュメント（`docs/dev/workflow.md`） |
| アーキテクチャ境界の変更 | `docs/adr/` に ADR を追加 |
| 新機能の仕様追加 | `docs/product/` の仕様書を更新 |
| ディレクトリ構造の変更 | `docs/dev/directory-structure.md` |
| AI 向け実行手順の変更 | `.claude/skills/` `.claude/agents/` |
| 物理強制ルールの追加 | `.claude/settings.json` のフック定義 + 該当スクリプト |

---

## 8. 関連リソース

### SSOT 層
- `docs/dev/workflow.md`（本ドキュメント）
- `docs/dev/directory-structure.md`
- `docs/adr/`
- `docs/product/`

### 確率論的プロンプト層
- `CLAUDE.md` — プロジェクト憲法（自動ロード）
- `.claude/rules/project-constitution.md`
- `.claude/skills/issue-driven-dev/SKILL.md` — Issue 駆動実装の skill
- `.claude/agents/architecture-reviewer.md` — 監査 + PR 起票のサブエージェント

### 決定論的ガードレール層
- `scripts/check-issue-contract.sh` — Issue 必須項目欠落の検知
- `.github/ISSUE_TEMPLATE/implementation.yml` — Issue Forms
- `.claude/settings.json` — Claude Hooks（Issue #9 で整備）
- Git Hooks 設定（Issue #10 で整備）
- `.github/workflows/` — GitHub Actions CI（別 Issue 想定）
