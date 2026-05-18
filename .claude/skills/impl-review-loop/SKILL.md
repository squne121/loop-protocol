---
name: impl-review-loop
description: 実装→検証→PRレビュー→敵対的レビューの4ステップループを自律実行するオーケストレータースキル。Issue番号を受け取り、pr-reviewer が APPROVE かつ adversarial-reviewer の CRITICAL/HIGH が 0 件になるまでループする。
required_rules:
  - github-ops-workflow
  - issueops-common-guard
  - git-policy
  - file-edit-protocol
  - skill-sync-policy
  - issue-uncertainty-policy
  - skill-rule-boundary
  - orchestrator-skill-policy
  - subagent-design-policy
---

# Impl Review Loop

`/impl-review-loop <Issue番号>` で起動する実装→検証→PRレビュー→敵対的レビューのオーケストレーション skill。

このファイルは index と共通契約のみを保持し、各 Step の詳細は `steps/` 配下へ分割している。実行時は下記の順で読む。

## Use When

- implementation child issue を自律的に実装・検証・レビューまで完了させたい
- `impl-review-loop <N>` などで開始したい
- `issue-contract-review` が完了し、人間が Go を返した後に進めたい

## Do Not Use When

- Issue contract が未承認
- Allowed Paths が不明確
- 人間の承認なく本番環境へのデプロイ・破壊的操作を含む場合

## Inputs

- `Issue番号`（必須）: 実装対象の implementation child issue 番号
- `model_overrides`（任意、デフォルト: なし）: SubAgent ごとの LLM モデル指定。キーは SubAgent 名を使う。CodexCLI SubAgent 委譲では、各値を `{ "model": "<CodexCLI model>", "model_reasoning_effort": "<low|medium|high>" }` 形式で渡す。互換のため文字列値（例: `"gpt-5.5"`）も受け付けてよいが、CodexCLI へ委譲する場合は `model` と `model_reasoning_effort` を明示する。例:
  ```json
  {
    "implementation-worker": {
      "model": "gpt-5.4-mini",
      "model_reasoning_effort": "low"
    },
    "test-runner": {
      "model": "gpt-5.4-mini",
      "model_reasoning_effort": "low"
    },
    "pr-reviewer": {
      "model": "gpt-5.5",
      "model_reasoning_effort": "medium"
    },
    "adversarial-reviewer": {
      "model": "gpt-5.5",
      "model_reasoning_effort": "medium"
    },
    "spec-document-reviewer": {
      "model": "gpt-5.5",
      "model_reasoning_effort": "medium"
    }
  }
  ```
  `spec-document-reviewer` を使う Step 1.5 では、この設定をそのまま継承してよい。
  各 role で `model_overrides` が未指定の場合は `.codex/agents/*.toml` の role-level pin（`model` / `model_reasoning_effort`）を実行時既定として扱う。

  **コスト最適化ヒント**: `change_kind=docs-only` かつ以下のいずれかを満たす場合、`implementation-worker` は haiku-4-5 推奨。ドキュメント変更は推論負荷が低く、haiku で十分な品質が得られる。
  - Allowed Paths が 1 ファイル
  - Allowed Paths が ≤3 ファイル かつ 各ファイルの記述形式・内容パターンが既定済（追加・更新のみで新規設計不要）

## 調査トリアージ方針

外部仕様調査を伴う委譲は、毎 iteration で次の3段階判定を先に行う。

1. フル調査: 要件・仕様の更新可能性が高く、既存知見だけでは判断できない場合
2. 差分調査: 前 iteration からの変更点だけ再確認すれば十分な場合
3. スキップ: 純内部変更（internal-only）で外部仕様調査が不要な場合

初回 iteration（iteration 0 / first iteration）は `web-researcher` の実行可否を必ず判定する。純内部変更でない限り、初回は少なくとも差分調査以上（フル調査または差分調査）を行う。純内部変更で外部仕様調査が不要と判断した場合のみ、Web 調査をスキップしてよい。

`external_research: skipped` と判断した場合は、その根拠（research run-id・判断理由・スキップ条件）を LOOP_STATE YAML の `external_research_skip_basis` フィールドに記録する。例:
```yaml
# 例1: 純内部 docs-only 変更で外部依存なし
external_research_skip_basis: "pure internal docs-only change; no external spec dependency (iteration 0, run-id: none)"

# 例2: 親 Issue grounded_research 確定済（再 delegation 不要）
external_research_skip_basis: "parent Issue #<N> grounded_research already confirmed at <issuecomment-URL>; child issue adds docs-only guidance derived from that research; no new external spec to investigate (iteration 0, run-id: <run-id>)"
```
この記録により、後続の pr-reviewer や adversarial-reviewer がスキップ判断の妥当性を評価できる。

## 責務分離原則

- **オーケストレータ（本 skill）**: **control-plane**（state tracking + routing）のみ。検知・判断・実装ロジックを持たない。publish / mutation などの **data-plane** 操作は bounded writer か fail-close に一意化し、main thread から直接実行しない。
- **test-runner**: 検知（mergeable 状態 / baseline failure 等）を `TEST_VERDICT` に統合する。
- **pr-reviewer**: 判断（mergeable、blockers、`LOOP_VERDICT`）を担当する。
- **implementation-worker**: 修正実装、conflict resolve、force-push を担当する（data-plane 操作の単一委譲先）。
- **adversarial-reviewer**: 信頼性リスク観点と project convention 適合観点を担当する。

## Shared Handoff Contract

`impl-review-loop` の共通 contract は `.agents/skills/shared-agent-skills-governance/references/handoff-contract.md` を
基本正本とする。

Machine-readable handoff payload は
`.agents/skills/shared-agent-skills-governance/references/machine-readable-handoff-payload.md`
を参照し、`Current Objective` / `Bounded Current Context` / `Normative References` / `Next Action` を更新対象として引き継ぐ。

- SubAgent 再利用 ledger (`agent_thread_reuse`, `previous_agent_id_or_task`, `reuse_method`, `previous_findings`, `fix_delta`, `handoff_artifact`)
- result metadata (`requested_model`, `requested_reasoning_effort`, `actual_execution_surface`, `actual_model_or_unknown`, `analytics_verification`)
- canonical PR state (`canonical_pr_url`, `canonical_pr_source`, `superseded_prs`, `repair_context`)
- workflow evidence capture bundle (reflog / attached-tree inspection / branch status、current/expected head、detached verify head switch decision — コマンド詳細は step docs を参照)

この skill では step docs が各 phase の手順正本であり、上記 shared fields を LOOP_STATE / issue comment / PR 本文へどう記録するかだけを本 section から参照する。`#1948 / PR #1977` の profile routing と `#1978` の pre-push helper は既存 destination boundary として扱い、この skill へ混ぜ戻さない。`#695` を implementation delegation fail-close の canonical destination とし、`#698` / `#1163` は conflict / reference-only として扱う（`#2076` routing-map 参照）。

## Procedure

1. [事前準備（Step 0: Rules Loading Preflight を含む）](.agents/skills/impl-review-loop/steps/preparation.md)
   - **Step 0: Rules Loading Preflight**: orchestrator は SubAgent 委譲前に rules を context に inline 注入し、冪等マーカー `<active_rules ...>` で重複読込を防止する。**role manifest 抽出 + iteration cache** を適用し、各 SubAgent role に必要な rule subset のみを注入することで prompt 長を削減する（詳細は preparation.md の「Step 0」セクション参照）。
2. [Step 1: 実装](.agents/skills/impl-review-loop/steps/step-1-implementation.md)
3. [Step 1.5: spec ドキュメントレビュー（オプション）](.agents/skills/impl-review-loop/steps/step-1-5-spec-document-review.md)
4. [Step 2: 検証](.agents/skills/impl-review-loop/steps/step-2-verification.md)
5. [Step 3 / 3.5: 敵対的レビューと正規化](.agents/skills/impl-review-loop/steps/step-3-adversarial-review.md)
6. [Step 4: PRレビュー](.agents/skills/impl-review-loop/steps/step-4-pr-review.md)
7. [Step 5: 判定・終了・フィードバック循環](.agents/skills/impl-review-loop/steps/step-5-feedback-and-termination.md)
8. [Context Protocol / Guardrails / Related](.agents/skills/impl-review-loop/steps/context-protocol-and-guardrails.md)
9. [Step 5: LOOP_VERDICT 自動読み取り](.agents/skills/impl-review-loop/steps/step-5-mergeability-handling.md)
10. [CONFLICTING PR Escalation Runbook](.agents/skills/impl-review-loop/steps/conflicting-pr-escalation-runbook.md)

## Maintenance

- `impl-review-loop` は `.agents/skills/.sync-exclude` に含まれるため、このスキルの変更を `.claude/skills/impl-review-loop/**` へ同期しない。
- `.agents/skills/.sync-exclude` に `impl-review-loop` が残っていることを維持し、CodexCLI 専用変更の mirror 同期例外を壊さない。
- `.agents/skills/impl-review-loop/steps/*.md` と `SKILL.md` の記述更新時は、`skill-creator` 相当の観点（progressive disclosure / validation integrity / bundled resources）で過剰説明を避ける。
- `scripts/sync-agent-skills.sh` は `git rev-parse --show-toplevel` でリポジトリ root を解決するため、実行コンテキストによらず決定論的に動作する（Issue #1859）。

## Related

- skill: `.agents/skills/implement-issue/SKILL.md`
- skill: `.agents/skills/pr-review-judge/SKILL.md`
- skill: `.agents/skills/adversarial-review/SKILL.md`
- skill: `.agents/skills/issue-refinement-loop/SKILL.md` の `## Body File Guidance`（`gh issue edit --body-file` / `tmp/` / HEREDOC escape guard の共通参照。PR cache / temp guidance は #1513 文脈の step docs、Issue 本文更新 guard は #1079 文脈の本ガイドを正本にする）
- rule: `.agents/rules/github-ops-workflow.md`
- rule: `.agents/rules/issueops-common-guard.md`
- template: `templates/github-ops/pr-evidence.md`
