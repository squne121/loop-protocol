# プロジェクト憲法

この文書は `CLAUDE.md` の詳細版であり、Issue を読んでから PR を作るまでの標準運用を定義する。

## 役割分担

- `CLAUDE.md`: repo 全体の短い入口、不変原則、非ゴール、読む順序
- この文書: 実装手順、docs の優先順位、検証規則
- `docs/product/requirements.md`: 全体要件と非ゴールの正本
- `docs/product/features/<feature>.md`: 個別機能の詳細仕様と受け入れ条件
- `docs/adr/*.md`: 設計判断の理由
- `docs/dev/current-focus.md`: 現在のフェーズと一時的な優先順位
- `docs/product/game-overview.md`: 体験の概要。正本ではない

## 作業前の読み順

1. 対象 Issue 本文を読む
2. 仕様判断に関わるコメントがあれば読む
3. `CLAUDE.md` を読む
4. この文書を読む
5. `docs/product/requirements.md` と `docs/dev/current-focus.md` を読む
6. 必要に応じて feature spec、ADR、構成メモを読む

## 実装ワークフロー

- `1 Issue = 1 PR` を原則とする。
- 複数ファイル変更、仕様判断、境界変更を含む場合は、実装前に Plan を出す。
- Plan 承認前に repo-tracked file を書き換えない。
- Issue コメントは永久仕様にしない。恒久判断は `requirements`、feature spec、ADR のいずれかへ反映する。
- Issue のスコープ外に広げる必要が出たら、同一 PR に押し込まず follow-up Issue を切る。
- Issue contract を作業計画の正本として扱う着手条件（4 点）は `docs/dev/workflow.md` の「Issue contract を作業計画の正本として扱う条件」セクションを参照。

## docs の優先順位

- workflow と guardrail は `CLAUDE.md` とこの文書を優先する。
- 全体要件と非ゴールは `docs/product/requirements.md` を優先する。
- 個別機能の挙動は `docs/product/features/<feature>.md` を優先する。
- `docs/product/game-overview.md` は概念説明であり、要件正本として扱わない。
- `docs/dev/current-focus.md` は一時的な実行順のメモであり、恒久仕様に昇格しない。

## feature spec の標準配置

- 個別機能 spec は `docs/product/features/<feature>.md` に置く。
- spec は YAML フロントマター付き Markdown を使う。
- spec には少なくとも以下を含める。
  - feature ID
  - status
  - related issue
  - acceptance
  - non-goals
  - related tests

## docs 更新ルール

- 全体の非ゴールや MVP 境界が変わる場合は `docs/product/requirements.md` を更新する。
- 個別機能の stable な挙動が固まる場合は feature spec を追加または更新する。
- 設計判断の理由を後で参照する必要がある場合は ADR を追加する。
- 現在フェーズや優先順位が変わった場合は `docs/dev/current-focus.md` を更新する。

## 必須検証

- 原則として以下を実行する。
  - `pnpm typecheck`
  - `pnpm lint`
  - `pnpm test`
  - `pnpm build`
  - `uv run pytest .claude/skills/gemini-cli-headless-delegation/tests/test_model_routing.py`
- VC でのテキスト検索は `rg`（ripgrep）を優先する。POSIX 互換性・CI イメージ制約・ツール未導入環境では `grep` を許容するが、その場合は理由を VC または PR 本文に明記する。
- docs-only 変更でも cheap で安定しているため、可能なら同じ品質ゲートを通す。
- 失敗した場合は握りつぶさず、未解決のまま報告する。
- Runtime Verification Applicability が `immediate` の Issue は `docs/dev/runtime-verification-policy.md` の SKIP 規約・証跡保存・Stop Condition 連動に従う（適用判定スキーマは policy.md の「Runtime Verification Applicability」を参照）。
- 既存 OPEN Issue（特に #26 等）の VC が `immediate` / `deferred` / `not_applicable` のどれに該当するかは、policy.md の適用判定スキーマを基準に確認する。

## スコープ管理

- `M1: Foundation Gate (v0.1.x)` の間は、まず基盤整備 Issue を閉じる。
- `movement + projectile` は `#2` の最小仕様と `#3` の docs 整備を前提に進める。
- hooks、permission 詳細、skill 実装はそれぞれ専用 Issue で扱う。
