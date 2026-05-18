# 流用 Agent / Skill 群のトリアージ表

別プライベートリポジトリから持ち込んだ 7 agent + 11 skill（および付随ファイル）を、LOOP_PROTOCOL に適合させるための判定表。フェーズ A の成果物。

凡例：
- **適合**: そのまま使える（最終 PR まで触らない）
- **軽微修正**: ハードコード値の置換・他プロジェクト例の差し替え程度
- **大幅改修**: ロジック・依存・前提を LOOP_PROTOCOL 用に書き換える
- **依存欠落**: 参照する rule / script / contract がリポジトリに存在しないため動作不能
- **不要**: LOOP_PROTOCOL では使わず、コミット対象から除外
- **保留**: 後日再判断（一旦コミット対象から除外）

ステージ表記：`A` トリアージ / `B` 欠落依存解決 / `C-1..C-5` クラスター適合 / `D` 統合検証

---

## Agents (`.claude/agents/`)

| ファイル | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `codebase-investigator.md` | 適合 | C-5 | 言語非依存。最小修正のみ |
| `implementation-worker.md` | 大幅改修 / 依存欠落 | C-2 | `.agents/rules/` インライン注入前提、`worktree` 取り扱いを LOOP_PROTOCOL 配置に統一 |
| `issue-author.md` | 大幅改修 / 依存欠落 | C-1 | AC3 が Python AST 前提、`shared-agent-skills-governance/follow-up-issue-contract` 参照 |
| `post-merge-cleanup-worker.md` | 軽微修正 | C-4 | `POST_MERGE_CLEANUP_REPORT_V1` の参照先と CodexCLI 前提を見直し |
| `pr-reviewer.md` | 軽微修正 / 依存欠落 | C-3 | `.agents/rules/` インライン注入前提。本体は言語非依存 |
| `review-issue.md` | 軽微修正 | C-1 | `tmp/` 配下の body-file 書き出し前提。LOOP_PROTOCOL の `tmp/` 利用ポリシーと整合確認 |
| `test-runner.md` | 大幅改修 | C-2 | `just check` / `pytest --timeout=60` / Windows GUI（uiautomation 等）前提を pnpm + Vitest + ブラウザに置換 |
| `web-researcher.md` | **保留** | — | gemini-cli-headless-delegation / Python / uv 依存。本トリアージでコミット対象から除外 |

`web-researcher.md` は親リポジトリの untracked のまま据え置く。実利用が発生した時点で再判断。

---

## Skills (`.claude/skills/`)

### 言語/AI ツール非依存に近いもの

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `nlm-skill` | 適合（既存） | — | 既存導入済み。本トリアージでも触らない |
| `codebase-investigator`（agent） | 適合 | C-5 | 上記 |

### Issue 管理系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `create-issue` | 大幅改修 / 依存欠落 | C-1 | `scripts/github_ops/create_issue_txn.py` 必須、`shared-agent-skills-governance/follow-up-issue-contract` 参照、`.agents/rules/issue-uncertainty-policy` 参照 |
| `issue-body-authoring` | 軽微修正 / 依存欠落 | C-1 | `shared-agent-skills-governance/follow-up-issue-contract` 参照、KindleAudiobook 用の bootstrap_recipe.py 実例 |
| `issue-contract-review` | 軽微修正 / 依存欠落 | C-1 | `required_rules: issue-body-ssot-policy / issueops-mode-guard` 参照、Windows GUI 実例 |
| `issue-refinement-loop` | 大幅改修 / 依存欠落 | C-1 | `.agents/rules/index.md` 必須、`orchestrator-skill-policy` / `subagent-design-policy` 参照、CodexCLI / gpt-5.x モデル想定 |
| `review-issue`（skill） | 軽微修正 | C-1 | Windows GUI AC 実例、`.github/ISSUE_TEMPLATE/github-ops-*.md` 前提 |

### 実装系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `implement-issue` | 大幅改修 / 依存欠落 | C-2 | `.agents/rules/` 7 件必須、KindleAudiobook の Python 実例、worktree 配置を LOOP_PROTOCOL 規約に統一 |

### レビュー系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `adversarial-review` | 軽微修正 / 依存欠落 | C-3 | `.agents/rules/` / `.agents/skills/*/references/` 参照、Priority attack surface が doc-lint 等で LOOP_PROTOCOL に不適 |
| `pr-review-judge` | 大幅改修 / 依存欠落 | C-3 | **`squne121/KindleAudiobookMakeSystem` ハードコード** 、`required_rules` 3 件、`local-ci/just-check` 参照 |

### オーケストレーション系

| skill | 判定 | 着手フェーズ | 主な不適合 / メモ |
|---|---|---|---|
| `impl-review-loop` | 大幅改修 / 依存欠落 | C-4 | `required_rules` 9 件、`steps/*.md` 内に CodexCLI / gpt-5.x モデル前提、別プロジェクト Issue/PR 番号参照多数 |
| `post-merge-cleanup` | 大幅改修 / 依存欠落 | C-4 | SubAgent 委譲（`POST_MERGE_CLEANUP_REPORT_V1` 返却）の仕様確認、KindleAudiobook 固有 Issue/PR 番号 |
| `open-pr` | 大幅改修 / 依存欠落 | C-4 | `scripts/open_pr.py`（Python）の取扱、`.github/PULL_REQUEST_TEMPLATE.md` 整合、KindleAudiobook PR テンプレ実例 |

---

## 付随ファイル

| パス | 種別 | 判定 | メモ |
|---|---|---|---|
| `.claude/skills/adversarial-review/references/review-output-schema.json` | JSON Schema | 適合 | 言語非依存。フェーズ C-3 で軽微確認 |
| `.claude/skills/impl-review-loop/steps/*.md`（9 件） | step 詳細 | 大幅改修 | impl-review-loop 本体と一緒に C-4 で扱う |
| `.claude/skills/issue-refinement-loop/references/issue-ops-and-handoff-sidecar.md` | 参照 | 軽微修正 | 他プロジェクト前提の文言確認 |
| `.claude/skills/open-pr/scripts/open_pr.py` | Python | 大幅改修 | 流用判断。node 化検討も含めて C-4 |
| `.claude/skills/open-pr/tests/test_open_pr.py` | Python テスト | open_pr.py に追従 | C-4 |
| `.claude/skills/open-pr/tests/test_sync_pr_evidence_template.py` | Python テスト | 同上 | C-4 |
| `.claude/skills/implement-issue/agents/openai.yaml` | CodexCLI 用 agent config | 不要候補 | C-2 で削除 or 残存判断 |
| `.claude/skills/issue-contract-review/agents/openai.yaml` | 同上 | 不要候補 | C-1 で削除 or 残存判断 |
| `.claude/skills/pr-review-judge/agents/openai.yaml` | 同上 | 不要候補 | C-3 で削除 or 残存判断 |
| `.claude/skills/pr-review-judge/references/best-practices.md` | 参照 | 軽微修正 | C-3 |
| `.claude/skills/pr-review-judge/references/review-output-contract.md` | 参照 | 軽微修正 | C-3 |
| `.claude/skills/pr-review-judge/references/step-3x-domain-verification-template.md` | 参照 | 軽微修正 | C-3。Windows GUI / Python 例の置換 |

---

## 欠落依存 判定表（フェーズ B で確認）

### `.agents/rules/` グループ（14 件）

| rule-id | 参照元 skill / agent | デフォルト判断（暫定） | 備考 |
|---|---|---|---|
| `file-edit-protocol` | implement-issue / impl-review-loop | 新規作成（最小） | LOOP_PROTOCOL では `CLAUDE.md` の保護領域 + Edit/Write プロトコルとして簡潔に再定義 |
| `git-policy` | implement-issue / impl-review-loop / issue-refinement-loop / pr-review-judge 等 | 新規作成（最小） | `1 Issue = 1 PR`、worktree 配置、`--no-verify` 禁止等を簡潔に集約 |
| `github-ops-workflow` | 多数 | 新規作成（最小） | gh CLI 利用パターンと `tmp/` の body-file 経由 issue/PR 更新方針 |
| `index` | issue-refinement-loop | 新規作成 | `.agents/rules/` 全 rule の目次。SubAgent への inline 注入時の正本 |
| `issue-body-ssot-policy` | issue-contract-review | 新規作成（最小） | Issue 本文を SSOT として扱う原則の集約 |
| `issue-uncertainty-policy` | create-issue / impl-review-loop / implement-issue / issue-refinement-loop | 新規作成（最小） | `phase/research` `state/needs-human` ラベル運用 |
| `issueops-common-guard` | 多数 | 新規作成（最小） | Issue/PR への書き込みガード共通条件 |
| `issueops-mode-guard` | issue-contract-review | 新規作成（最小） | research vs implementation の Issue 種別ガード |
| `orchestrator-skill-policy` | impl-review-loop / issue-refinement-loop | 新規作成（最小） | オーケストレーター skill 共通契約 |
| `skill-rule-boundary` | implement-issue / impl-review-loop | 新規作成（最小） | skill 本文 vs rule の責務境界 |
| `skill-sync-policy` | implement-issue / impl-review-loop | 新規作成（最小） | skill 更新時の整合性確認手順 |
| `subagent-design-policy` | impl-review-loop / issue-refinement-loop | 新規作成（最小） | SubAgent 設計共通指針 |
| `wsl-dev-environment` | 一部 | 削除 | LOOP_PROTOCOL は WSL 固有依存なし、必要なら CLAUDE.md に統合 |
| `xxx` | 一部 | 削除 | 明らかにプレースホルダ |

**フェーズ B での既定方針**: 「新規作成（最小）」とした項目は、LOOP_PROTOCOL に必要十分な短い rule として作る（ボイラープレートを引きずらない）。配置は `.agents/rules/` 配下（流用 skill の参照パスに合わせる）。

### `shared-agent-skills-governance/references/`（3 件）

| ファイル | 参照元 | デフォルト判断（暫定） | 備考 |
|---|---|---|---|
| `follow-up-issue-contract.md` | create-issue / issue-body-authoring / post-merge-cleanup 等 | 新規作成（最小） | follow-up Issue の canonical contract |
| `handoff-contract.md` | issue-refinement-loop 等 | 新規作成（最小） | SubAgent ハンドオフ契約 |
| `machine-readable-handoff-payload.md` | 一部 | 新規作成（最小） | YAML ペイロード仕様。本当に必要か skill 側で再評価 |

配置は `.agents/skills/shared-agent-skills-governance/references/`（参照パスに合わせる）。

### スクリプト

| パス | 参照元 | デフォルト判断（暫定） | 備考 |
|---|---|---|---|
| `scripts/github_ops/create_issue_txn.py` | create-issue | 流用持ち込み or node 化 | `gh issue create` ラッパー。LOOP_PROTOCOL の運用都合で node 化が望ましいかは C-1 で判断 |

---

## PR #11 由来のアセット再利用判断

| アセット | フェーズ | 判断 |
|---|---|---|
| `docs/dev/workflow.md`（PR #11 版） | D | 流用 skill ベースに全面書き換え |
| `scripts/check-issue-contract.sh` | C-1 / D | 撤去（`issue-contract-review` skill と機能重複） |
| `.github/ISSUE_TEMPLATE/implementation.yml`（PR #11 版） | C-1 | `github-ops-implementation` 形式に置換 |
| `.claude/agents/architecture-reviewer.md` | C-3 | 撤去（`pr-reviewer` agent に置換） |
| `.claude/skills/issue-driven-dev/SKILL.md` | C-2 | 撤去（`implement-issue` skill に置換） |
| `.gitignore` の `.claude/worktrees/` `.claude/plans/` 追加 | D | 再採用 |

---

## 次のアクション

1. 本 PR を **import-as-is + 本トリアージ表** だけでマージ可能な最小構成にする
2. フェーズ B で「新規作成（最小）」とした rule / contract をまとめて作る PR を起票
3. フェーズ C-1〜C-5 でクラスター単位の適合作業を進める
4. フェーズ D で `workflow.md` 再書き換えと統合検証
