# 流用 Agent / Skill 群のトリアージ表（改訂版）

別プライベートリポジトリから持ち込んだ agent / skill 定義を、LOOP_PROTOCOL に適合させるための判定表。フェーズ A の成果物。

> **重要**: 本表は PR #12 への [人間レビュー指摘](https://github.com/squne121/loop-protocol/pull/12#issuecomment-4477986377) を受けて、当初版から **方針を全面改訂** している。
> 主な変更:
> - `.agents/rules/` は持ち込まない → **ディレクトリごとの CLAUDE.md** + 新規 **SSOT 探索エージェントスキル** で代替
> - `shared-agent-skills-governance/follow-up-issue-contract` は不要
> - `adversarial-review` skill は当面導入しない（Usage 消費が大きく、使い分けが困難）
> - `gemini-cli-headless-delegation` を Issue #14 で改善導入（ユーザー配置済み）
> - `create_issue_txn.py` は `.claude/skills/create-issue/scripts/` 配置版を流用採用
> - `.github/ISSUE_TEMPLATE/github-ops-*.md` 4 件はコメント経由で提供（別 PR で評価）

凡例：
- **適合**: そのまま使える（最終 PR まで触らない）
- **軽微修正**: ハードコード値の置換・他プロジェクト例の差し替え程度
- **大幅改修**: ロジック・依存・前提を LOOP_PROTOCOL 用に書き換える
- **依存欠落**: 参照する rule / script / contract がリポジトリに存在しないため動作不能 → CLAUDE.md / SSOT 探索 skill 参照に書き換える
- **不要**: LOOP_PROTOCOL では使わず、コミット対象から除外
- **保留**: 後日再判断（一旦コミット対象から除外）

ステージ表記：`A` トリアージ / `B` per-dir CLAUDE.md + SSOT 探索 skill / `B-2` ISSUE_TEMPLATE 評価 / `C-1..C-5` クラスター適合 / `D` 統合検証

---

## Agents (`.claude/agents/`)

| ファイル | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `codebase-investigator.md` | 適合 | C-5 | 言語非依存。最小修正のみ |
| `implementation-worker.md` | 大幅改修 | C-2 | `.agents/rules/` インライン注入機構を **per-dir CLAUDE.md + SSOT 探索 skill 経由** に置換。worktree 取扱を `.claude/worktrees/` に統一 |
| `issue-author.md` | 大幅改修 | C-1 | AC3 が Python AST 前提 → TypeScript / Markdown 静的解析に置換。`shared-agent-skills-governance/follow-up-issue-contract` 参照を除去 |
| `post-merge-cleanup-worker.md` | 軽微修正 | C-4 | `POST_MERGE_CLEANUP_REPORT_V1` の出力契約は KindleAudiobookMakeSystem #2146 を一次情報として参照（YAML スキーマ流用、LOOP_PROTOCOL 不要要素は削除） |
| `pr-reviewer.md` | 軽微修正 | C-3 | `.agents/rules/` インライン注入機構を per-dir CLAUDE.md / SSOT 探索 skill 経由に置換 |
| `review-issue.md` | 軽微修正 | C-1 | `tmp/` 配下 body-file 書き出し前提（LOOP_PROTOCOL では `tmp/` を gitignore 済みで利用可） |
| `test-runner.md` | 大幅改修 | C-2 | `just check` / `pytest --timeout=60` / Windows GUI（uiautomation 等）前提を pnpm + Vitest + ブラウザに置換 |
| `web-researcher.md` | **保留** | — | gemini-cli-headless-delegation / Python / uv 依存。本トリアージでコミット対象から除外 |

`web-researcher.md` は親リポジトリの untracked のまま据え置く。`gemini-cli-headless-delegation` の整備完了後に再判断。

---

## Skills (`.claude/skills/`)

### 言語/AI ツール非依存に近いもの

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `nlm-skill` | 適合（既存） | — | 既存導入済み。本トリアージでも触らない |
| `gemini-cli-headless-delegation` | 大幅改修 | Issue #14 | **新規導入**。Issue #14 で squne121 自作スキルを改善する方針。本 PR では import のみ。詳細改善は Issue #14 スコープ |

### Issue 管理系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `create-issue` | 大幅改修 | C-1 | `scripts/create_issue_txn.py` は流用採用（ユーザー配置済み）。`shared-agent-skills-governance/follow-up-issue-contract` 参照は除去（不要判定）。`.agents/rules/` 参照は CLAUDE.md / SSOT 探索 skill 参照へ書き換え |
| `issue-body-authoring` | 軽微修正 | C-1 | `shared-agent-skills-governance/follow-up-issue-contract` 参照は除去。KindleAudiobook 用の bootstrap_recipe.py 等の実例を LOOP_PROTOCOL 例に置換 |
| `issue-contract-review` | 軽微修正 | C-1 | `required_rules` を per-dir CLAUDE.md / SSOT 探索 skill 参照に書き換え。Windows GUI 実例除去 |
| `issue-refinement-loop` | 大幅改修 | C-1 | `.agents/rules/index.md` 参照を per-dir CLAUDE.md 探索ロジックに置換。CodexCLI / gpt-5.x モデル想定を Claude モデル (Opus/Sonnet/Haiku) に置換 |
| `review-issue`（skill） | 軽微修正 | C-1 | Windows GUI AC 実例除去。`.github/ISSUE_TEMPLATE/github-ops-*.md`（フェーズ B-2 で評価）との整合 |

### 実装系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `implement-issue` | 大幅改修 | C-2 | `.agents/rules/` 7 件参照を per-dir CLAUDE.md / SSOT 探索 skill 参照に置換。KindleAudiobook の Python 実例を LOOP_PROTOCOL（TypeScript）例に置換。worktree 配置を `.claude/worktrees/` 規約に統一 |

### レビュー系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| ~~`adversarial-review`~~ | **不要（当面導入しない）** | — | Usage 消費が大きく、全実装で有用ではない、正常系レビュアーとの使い分けが困難。本 PR で削除済み |
| `pr-review-judge` | 大幅改修 | C-3 | **`squne121/KindleAudiobookMakeSystem` ハードコード**を `squne121/loop-protocol` に置換。`required_rules` 参照を CLAUDE.md / SSOT 探索 skill 経由に書き換え。`local-ci/just-check` 参照は `pnpm` スクリプト群に置換 |

### オーケストレーション系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `impl-review-loop` | 大幅改修 | C-4 | `required_rules` 9 件を per-dir CLAUDE.md / SSOT 探索 skill 参照に置換。`steps/*.md` 内に CodexCLI / gpt-5.x モデル前提を Claude モデルへ置換。adversarial-review 連携部分（Step 3）の取り扱いを削除または保留に変更。別プロジェクト Issue/PR 番号参照を削除 |
| `post-merge-cleanup` | 大幅改修 | C-4 | SubAgent 委譲（`POST_MERGE_CLEANUP_REPORT_V1` 返却）の仕様は KindleAudiobookMakeSystem #2146 を一次情報として参照しつつ、LOOP_PROTOCOL 不要要素は除去 |
| `open-pr` | 大幅改修 | C-4 | `scripts/open_pr.py`（Python）の取扱を判断（流用維持 or 撤去）、`.github/PULL_REQUEST_TEMPLATE.md` 整合、KindleAudiobook PR テンプレ実例除去 |

---

## 付随ファイル

| パス | 種別 | 判定 | メモ |
|---|---|---|---|
| `.claude/skills/create-issue/scripts/create_issue_txn.py` | Python | 流用維持 | ユーザー配置版。`shared-agent-skills-governance` への依存があれば C-1 で除去 |
| `.claude/skills/gemini-cli-headless-delegation/**` | skill 一式 | Issue #14 スコープ | 本 PR では import のみ。改善は Issue #14 |
| `.claude/skills/impl-review-loop/steps/*.md`（9 件） | step 詳細 | 大幅改修 | impl-review-loop 本体と一緒に C-4 で扱う |
| `.claude/skills/issue-refinement-loop/references/issue-ops-and-handoff-sidecar.md` | 参照 | 軽微修正 | 他プロジェクト前提の文言確認 |
| `.claude/skills/open-pr/scripts/open_pr.py` | Python | 大幅改修 | 流用判断。C-4 |
| `.claude/skills/open-pr/tests/*.py` | Python テスト | open_pr.py に追従 | C-4 |
| `.claude/skills/implement-issue/agents/openai.yaml` | CodexCLI 用 agent config | 不要候補 | C-2 で削除 or 残存判断 |
| `.claude/skills/issue-contract-review/agents/openai.yaml` | 同上 | 不要候補 | C-1 で削除 or 残存判断 |
| `.claude/skills/pr-review-judge/agents/openai.yaml` | 同上 | 不要候補 | C-3 で削除 or 残存判断 |
| `.claude/skills/gemini-cli-headless-delegation/agents/openai.yaml` | 同上 | Issue #14 で判断 | Issue #14 スコープ |
| `.claude/skills/pr-review-judge/references/best-practices.md` | 参照 | 軽微修正 | C-3 |
| `.claude/skills/pr-review-judge/references/review-output-contract.md` | 参照 | 軽微修正 | C-3 |
| `.claude/skills/pr-review-judge/references/step-3x-domain-verification-template.md` | 参照 | 軽微修正 | C-3。Windows GUI / Python 例の置換 |

---

## 欠落依存への新方針（旧 .agents/rules/ 14 件の代替）

[人間レビュー指摘](https://github.com/squne121/loop-protocol/pull/12#issuecomment-4477986377) に従い、`.agents/rules/` の取り込みは中止する。代替アプローチは以下：

### 1. ディレクトリごとの CLAUDE.md（新方針 — フェーズ B で実装）

- LOOP_PROTOCOL では各ディレクトリに `CLAUDE.md` を配置する運用を採る
- 例: `src/state/CLAUDE.md`、`src/render/CLAUDE.md`、`src/systems/CLAUDE.md` 等
- 各 `CLAUDE.md` はそのディレクトリ配下を編集する際の不変条件・規約を簡潔に記述
- Claude Code が自動で読み込む（プロジェクトルート CLAUDE.md と階層的に作用）

### 2. SSOT 探索エージェントスキル（新方針 — フェーズ B で実装）

- 新規 skill: `docs/` を SSOT として扱う場合の探索手順を提供
- 実装・レビュー agent が「該当 Issue / PR に関連する `docs/` 配下の SSOT ドキュメントを列挙→読む」フローを再現可能にする
- 例: `docs/dev/workflow.md`、`docs/adr/`、`docs/product/`、`docs/dev/directory-structure.md`

### 3. 旧 `.agents/rules/` 各項目の取扱

| 旧 rule-id | 新方針 |
|---|---|
| `file-edit-protocol` | プロジェクトルート `CLAUDE.md` の保護領域記述で代替 |
| `git-policy` | プロジェクトルート `CLAUDE.md` または `docs/dev/workflow.md` に集約 |
| `github-ops-workflow` | 新 skill 群（implement-issue 等）の SKILL.md 内に最小記述 |
| `index` | 不要（per-dir CLAUDE.md は自動ロード） |
| `issue-body-ssot-policy` | `docs/dev/workflow.md`（SSOT）に集約 |
| `issue-uncertainty-policy` | `docs/dev/workflow.md` または create-issue / implement-issue SKILL.md に集約 |
| `issueops-common-guard` | 各 issue 系 skill の SKILL.md に最小記述 |
| `issueops-mode-guard` | `.github/ISSUE_TEMPLATE/github-ops-*.md` のテンプレ自体で表現 |
| `orchestrator-skill-policy` | impl-review-loop / issue-refinement-loop の SKILL.md 内に最小記述 |
| `skill-rule-boundary` | 不要（rule 自体を持たない方針） |
| `skill-sync-policy` | 検証 PR（フェーズ D）の手順として実施 |
| `subagent-design-policy` | 各 agent ファイル frontmatter で個別に定義（disallowedTools 等） |
| `wsl-dev-environment` | 不要 |
| `xxx` | 不要 |

### 4. 旧 `shared-agent-skills-governance/references/` の取扱

| ファイル | 新方針 |
|---|---|
| `follow-up-issue-contract.md` | 不要（ユーザー指示） |
| `handoff-contract.md` | 必要なら C-4 で各 skill 本文内に簡潔記述 |
| `machine-readable-handoff-payload.md` | 必要なら C-4 で各 skill 本文内に簡潔記述 |

---

## PR #11 由来のアセット再利用判断（変更なし）

| アセット | フェーズ | 判断 |
|---|---|---|
| `docs/dev/workflow.md`（PR #11 版） | D | 流用 skill ベース + 新方針（per-dir CLAUDE.md + SSOT 探索 skill）に全面書き換え |
| `scripts/check-issue-contract.sh` | C-1 / D | 撤去（`issue-contract-review` skill と機能重複） |
| `.github/ISSUE_TEMPLATE/implementation.yml`（PR #11 版） | B-2 | `github-ops-implementation.md` 形式に置換（B-2 で評価） |
| `.claude/agents/architecture-reviewer.md` | C-3 | 撤去（`pr-reviewer` agent に置換） |
| `.claude/skills/issue-driven-dev/SKILL.md` | C-2 | 撤去（`implement-issue` skill に置換） |
| `.gitignore` の `.claude/worktrees/` `.claude/plans/` 追加 | D | 再採用 |

---

## 次のアクション

1. 本 PR を再 push（adversarial-review 除去 + gemini-cli-headless-delegation 取込 + create-issue/scripts 取込 + トリアージ表書き換え）
2. **新フェーズ B（PR）**: per-dir CLAUDE.md + SSOT 探索エージェントスキル新設
3. **フェーズ B-2（PR）**: `.github/ISSUE_TEMPLATE/github-ops-*.md` 4 件評価・採用
4. **フェーズ C-1 〜 C-5**: クラスター単位の適合作業（`required_rules` 参照を per-dir CLAUDE.md / SSOT 探索 skill 経由に書き換える点が当初計画から変わる）
5. **フェーズ D**: workflow.md 再書き換え + スモークテスト + 不要アセット撤去
