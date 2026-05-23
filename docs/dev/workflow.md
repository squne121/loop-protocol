# LOOP_PROTOCOL 開発運用ワークフロー（SSOT）

LOOP_PROTOCOL における Issue 駆動開発の **単一の真実の情報源（SSOT）**。
個別 skill / agent / docs はこの文書を運用ルールの正本として参照する。

## 全体像（3 階層構造）

```
[SSOT]                  ← 開発運用ドキュメント（docs/dev/, docs/adr/, docs/product/）
   ↓
[確率論的プロンプト]    ← CLAUDE.md / Skill / Subagent 定義（AI に振る舞いを伝える）
   ↓
[決定論的ガードレール]  ← Claude Hooks / Git Hooks / GitHub Actions CI（物理強制）
```

| 階層 | 役割 | 実体 |
|---|---|---|
| SSOT | プロジェクトルールの正本（人間可読） | `docs/dev/workflow.md`（本ドキュメント）, `docs/dev/agent-skill-boundaries.md`, `docs/dev/github-ops.md`, `docs/adr/`, `docs/product/` |
| 確率論的プロンプト | AI 向け実行コンテキスト | ルート / per-directory `CLAUDE.md`, `.claude/skills/`, `.claude/agents/` |
| 決定論的ガードレール | AI 逸脱時の物理強制 | Claude Hooks（Issue #9）、Git Hooks（Issue #10）、`.github/workflows/ci.yml` |

SSOT を編集したら、対応する確率論的プロンプト層・決定論的ガードレール層を必ず同 PR で更新する。

## Issue 駆動開発フロー

```
[1] Issue 起票
       ↓ create-issue (issue-author SubAgent)
[2] Issue refinement (任意)
       ↓ issue-refinement-loop オーケストレーター
[3] 着手前 preflight
       ↓ issue-contract-review
[4] 実装 → 検証 → PR レビュー
       ↓ impl-review-loop オーケストレーター
[5] 人間レビュー → マージ
[6] post-merge cleanup
       ↓ post-merge-cleanup (post-merge-cleanup-worker SubAgent)
```

各フェーズで使う Skill / SubAgent の詳細は `docs/dev/agent-skill-boundaries.md` を参照。

### Phase 別の入口

| Phase | 起動方法 | 主要 Skill / SubAgent |
|---|---|---|
| Issue 起票 | 「Issue 起票して」「create issue」 | `create-issue` (via `issue-author`) |
| Issue 改善ループ | 「Issue ◯◯ を磨いて」「refinement loop」 | `issue-refinement-loop` |
| 着手前 preflight | 「Issue ◯◯ 実装の前確認」「contract review」 | `issue-contract-review` |
| 実装ループ | 「Issue ◯◯ をループで実装して」「`/impl-review-loop <N>`」 | `impl-review-loop` |
| 個別実装（loop なし） | 「Issue ◯◯ を実装して」「implement issue」 | `implement-issue` (via `implementation-worker`) |
| PR レビュー | 「PR ◯◯ レビューして」「review PR」 | `pr-review-judge` (via `pr-reviewer`) |
| マージ後 cleanup | 「クリーンアップして」「post merge」 | `post-merge-cleanup` (via `post-merge-cleanup-worker`) |

## テスト戦略（3 層責務分離 — Defense in Depth）

| レイヤー | 実行手段 | 実行内容 | 目的 |
|---|---|---|---|
| 1. AI 自己修復 | Claude Hooks (`PostToolUse`) | 編集ファイルの lint / typecheck | AI への即時フィードバック。CI 消費前のローカル fail-fast |
| 2. 履歴の保護 | Git Hooks (`pre-commit` / `pre-push`) | 高速検証 (typecheck / lint / unit test) | 壊れたコードが Git 履歴に刻まれるのを物理防止。E2E など重いテストは含めない |
| 3. 最終品質保証 | GitHub Actions CI | typecheck + lint + unit + E2E + build | クリーン環境での再現可能な最終確定。PR マージをシステムブロック |

同じテストを複数レイヤーで実行するのは **Defense in Depth（多層防御）**。

### テストスタイル

- **TDD（テスト駆動開発）**: 実装前に Vitest テストを書く
- **BDD（振る舞い駆動開発 = Behavior-Driven Development）**: テスト名・記述は GIVEN/WHEN/THEN 命名規則
- 実装詳細でなく入出力の振る舞いをアサーションする

## 1 Issue = 1 PR ルール

- 1 つの Issue に対して必ず 1 つの PR を作る
- 実装中に別の問題を発見した場合は新規 Issue を起票し、現 Issue のスコープを保つ
- 複数 Issue を 1 PR にまとめることは原則禁止
- skill 内・サブエージェント内でこのルールを物理強制する

良い PR スコープの判定基準（`create-issue` Scope 判定で使う）:

| 基準 | 判定方法 |
|---|---|
| 単一意図 | 変更ファイル群が 1 つの Outcome のためだけに必要 |
| アーキ層のまとまり | Allowed Paths が 1 つの層（`src/state` / `src/render` / `src/systems` / `src/data` 等）に閉じている。複数層をまたぐ場合は層境界の変更そのものが Outcome |
| ロールバック単位 | 1 PR を revert すれば Outcome が完全に元に戻る |
| AC の独立性 | 各 AC が他の AC に依存せず、相互に独立に検証可能 |

## Worktree 配置規約

- 配置先: `.claude/worktrees/issue-<番号>-<slug>/` または `.claude/worktrees/<task-name>/`（リポジトリ内）
- `.gitignore` で除外済み
- `git worktree add` CLI を直接利用（特定エージェント専用機能には依存しない）
- リポジトリ外配置は禁止（Claude Code の workspace trust prompt が再発し承認マシーン化）

### マージ後クリーンアップ

PR マージ後は `post-merge-cleanup` skill 経由で自動的に:

```bash
git worktree remove .claude/worktrees/<slug>
git branch -d worktree-<slug>
```

## Issue / PR 種別とテンプレート

### Issue テンプレート（`.github/ISSUE_TEMPLATE/`）

| テンプレ | 用途 | 自動付与ラベル |
|---|---|---|
| `implementation.yml` | 実装作業 | `phase/implementation`, `state/queued`, `agent/implementer` |
| `research.yml` | 仕様調査・比較検討 | `phase/research`, `state/queued`, `agent/research` |
| `parent.yml` | parent tracker（複数 child を束ねる） | `tracking`, `state/in-progress` |
| `bug-report.yml` | エンドユーザーバグ報告 | `bug` |
| `feature-request.yml` | エンドユーザー機能要望 | `enhancement` |

`human-confirm.yml` は不採用（PR #16）。人間判断は元 Issue 内でブロッカー扱い + 本文修正の運用とする。

### PR テンプレート

`implement-issue` が生成する PR 本文の必須セクション（`open-pr` の Template Guard で強制）:
- `## Summary`
- `## 受け入れ条件の達成状況`
- `## 検証コマンド結果`
- `## Allowed Paths 遵守`

## Issue contract を作業計画の正本として扱う条件

`impl-review-loop` が GitHub Issue contract を作業計画の正本として扱い、追加の実装計画承認を要求しないための着手条件。以下をすべて満たした場合のみ着手する。

- `issue-contract-review` が `status: go` を返していること
  - この判定には DoR 準拠・VC preflight・GitHub native dependency または `Depends on #N` で表現された blocker / dependency の全 close・human escalation 非該当の確認を含む（詳細は `issue-contract-review` skill 参照）
- Allowed Paths が現在 OPEN な他 Implementation Issue の Allowed Paths と重複しないこと（マージコンフリクトリスクなし、`issue-contract-review` では現時点で未判定のため独立条件として明示）

いずれか 1 つでも欠けた場合は着手を停止し、人間判断を仰ぐ。

> Claude Code の plan permission mode（`--permission-mode plan` / `Shift+Tab` / `/plan` で人間が選ぶセッション制御）は人間がセッション単位で選択する UI 制御であり、本ルールの対象外である。plan permission mode の有無は上記着手条件判定に影響しない。

## Human Decision が必要な条件

以下に該当する場合、AI に丸投げせず人間が判断する:

- `src/state` ↔ `src/render` の境界変更
- 新しい外部依存（パッケージ）追加
- `assets/` / `LICENSES/` への変更（AI 編集禁止領域）
- 複数 Issue にまたがる仕様変更
- `CLAUDE.md` の制約変更
- 本ドキュメント（SSOT）の変更

ループ内では「ユーザーがループ起動した時点で routine 操作は承認済み」。詳細は `docs/dev/agent-skill-boundaries.md` の「ループ内の人間承認原則」を参照。

## docs 更新が必要な条件

| 変更内容 | 更新が必要なドキュメント |
|---|---|
| 開発フロー自体の変更 | 本ドキュメント（`docs/dev/workflow.md`） |
| アーキテクチャ境界の変更 | `docs/adr/` に ADR を追加 |
| 新機能の仕様追加 | `docs/product/` の仕様書を更新 |
| ディレクトリ構造の変更 | `docs/dev/directory-structure.md` |
| AI 向け実行手順の変更 | `.claude/skills/` / `.claude/agents/` |
| SubAgent / Skill 責務境界の変更 | `docs/dev/agent-skill-boundaries.md` |
| GitHub 運用ルールの変更 | `docs/dev/github-ops.md` |
| 物理強制ルールの追加 | `.claude/settings.json` のフック定義 + 該当スクリプト |
| GitHub Milestone 操作 | `docs/dev/milestone-ops.md` |

## SSOT Routing Table

SSOT 追加時の参照先を集約した索引。AI エージェントは実装着手前に対象トピックの SSOT を本表から確認する。
カタログの正本は `docs/dev/ssot-registry.md` を参照すること。

| トピック | 参照先 SSOT |
|---|---|
| Issue 駆動開発フロー・1 Issue 1 PR | `docs/dev/workflow.md`（本ドキュメント） |
| SubAgent / Skill 責務境界 | `docs/dev/agent-skill-boundaries.md` |
| `gh` CLI 利用規約・ラベル運用 | `docs/dev/github-ops.md` |
| GitHub Milestone 作成・割当・close・rollup | `docs/dev/milestone-ops.md` |
| アーキテクチャ分離原則・60Hz タイムステップ | `docs/adr/0001-architecture-baseline.md` |
| SDD ツール採否・正本境界・token 対策・playtest 補正 | `docs/adr/0002-sdd-tool-adoption.md` |
| 全体要件・非ゴール | `docs/product/requirements.md` |
| 現在のフェーズ・優先項目 | `docs/dev/current-focus.md` |
| SSOT カタログ全体 | `docs/dev/ssot-registry.md` |

### 新規 SSOT 追加時の必須更新セット

新しい SSOT 文書（`docs/` 配下）を追加する場合は、**同一 PR で以下をすべて更新する**こと:

1. **本表（SSOT Routing Table）** にエントリ追加
2. **`docs/dev/ssot-registry.md`**（SSOT カタログ正本）にエントリ追加
3. **`.claude/skills/ssot-discovery/SKILL.md`** の説明・例を必要に応じて更新

> 注意: `ssot-catalog.md` は PR #302 で削除済み（`ssot-registry.md` に統合）。以前の手順にあった「ssot-catalog.md にエントリ追加」は不要。

上記を同一 PR で更新しない場合、AI エージェントが新 SSOT を見落として古い情報で誤実行するリスクが生じる。

## Delivery-rollup Parent / Parent-mode Handoff 手順

`parent_mode: delivery-rollup` / `closure_mode: child-complete` の親 Issue を持つ child PR がマージされたとき、残り child を確実に起票・管理するための標準フロー。

### フロー概要

```
child PR マージ
  ↓
post-merge-cleanup Section 6a:
  plan_child_materialization.py --repo ... --issue <parent>
  → CHILD_MATERIALIZATION_PLAN_V2
    → missing children → follow_up_issue_requests (optional_follow_up)
    → stale_body_only → edit-issue (delivery-rollup-parent-update mode)
    → human_escalation → human_review_required: true
  ↓
main thread: dedupe チェック → issue-author / create-issue で起票
```

### 使用するスクリプト

```bash
# read-only plan 生成（GitHub から取得）
uv run python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
  --repo <owner>/<repo> \
  --issue <parent_issue_number>
```

スキーマ正本: `docs/dev/agent-skill-boundaries.md#CHILD_MATERIALIZATION_PLAN_V2`

### skill 別の責務

| skill / SubAgent | delivery-rollup 特有の責務 |
|---|---|
| `create-issue` | `CHILD_MATERIALIZATION_PLAN_V2` の `action=create_issue` を `create_issue_txn.py` 経由で materialize する |
| `edit-issue` | `parent_body_updates` を backup / guard / rollback 付きで適用する（`delivery-rollup-parent-update` mode） |
| `issue-refinement-loop` | delivery-rollup parent approve 前に child materialization gate を実行する（Step 4.5） |
| `impl-review-loop` Step 5 | APPROVE 前に delivery-rollup parent の残り child を `mandatory_follow_up` として処理する |
| `open-pr` | PR 本文に `## Parent Child Materialization` セクションを追加する |
| `post-merge-cleanup` | Section 6a で delivery-rollup parent の残り child を検出し `follow_up_issue_requests` に追加する |

## 関連ドキュメント

- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界、オーケストレーター設計原則、ループ内人間承認原則
- `docs/dev/github-ops.md` — `gh` CLI 利用規約、Parent Mode、コメント記録テンプレ
- `docs/dev/directory-structure.md` — リポジトリ構造
- `docs/dev/current-focus.md` — 現在のフェーズ・優先項目
- `docs/adr/` — アーキテクチャ決定記録
- `docs/product/` — プロダクト仕様
- ルート `CLAUDE.md` — プロジェクト憲法（自動ロード）
- per-directory `CLAUDE.md` — 各層の不変条件

## 関連 Skill / SubAgent インデックス

詳細は `docs/dev/agent-skill-boundaries.md` を参照。

- Issue 管理系: `create-issue`, `edit-issue`, `review-issue`, `issue-contract-review`, `issue-refinement-loop`, `issue-author` (SubAgent)
- 実装系: `implement-issue`, `implementation-worker` (SubAgent), `test-runner` (SubAgent)
- レビュー系: `pr-review-judge`, `pr-reviewer` (SubAgent)
- オーケストレーション系: `impl-review-loop`, `open-pr`, `post-merge-cleanup`, `post-merge-cleanup-worker` (SubAgent)
- 補助系: `ssot-discovery`, `gemini-cli-headless-delegation`, `nlm-skill`, `codebase-investigator` (SubAgent)
