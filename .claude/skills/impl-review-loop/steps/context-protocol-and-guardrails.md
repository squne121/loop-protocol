## Context Protocol

### ループ状態の記録（LOOP_STATE）

各フェーズ移行時に Issue にコメントして状態を記録する。

| フィールド | 説明 |
|---|---|
| `iteration` | 現在のイテレーション番号（0 = init、1 = 最初の実装ループ、以降 +1） |
| `phase` | `init` / `implemented` / `tested` / `reviewed` / `completed` / `feedback` / `escalation` |
| `status` | `running` / `success` / `blocked` |
| `pr_url` | 実装 PR の URL（Step 1 以降） |
| `last_verdict` | 最後の pr-reviewer verdict |
| `handoff_artifact` | 当該 iteration で投稿した Feedback コメント URL。次反復の SubAgent が参照すべき唯一の成果物（`feedback` phase のみ） |
| `supersedes` | 前回 iteration の `handoff_artifact` を置き換える旧 artifact URL。初回は `none`（`feedback` phase のみ） |
| `agent_thread_reuse` | 同一 SubAgent thread を再利用したか（`true`/`false`） |
| `previous_agent_id_or_task` | 再利用対象の前回 agent id または task 名 |
| `reuse_method` | `send_input` / `resume_agent + send_input` / `new_agent` |
| `previous_findings` | 前回 iteration の主要指摘（次反復の修正入力） |
| `fix_delta` | 今回 iteration で適用する修正方針 |
| `contradiction_findings` | 前回指摘と逆方向の修正要求として自動除外した adversarial 所見 |
| `out_of_scope_findings` | オーケストラレータが Out of Scope と確定した所見の累積リスト |
| `repeated_out_of_scope_findings` | 今回再掲された既知 Out of Scope 所見 |
| `wip_scope_downgraded_findings` | `wip/` 単一スレッド前提により MEDIUM 以下へ減衰した所見 |
| `normalized_critical_count` | 矛盾 / Out of Scope / WIP 減衰を除外した後の CRITICAL 件数 |
| `normalized_high_count` | 矛盾 / Out of Scope / WIP 減衰を除外した後の HIGH 件数 |

### SubAgent からの結果記録（Context Protocol）

各 SubAgent の結果は以下のコマンドで GitHub surface に記録する:

- **test-runner**: `gh pr comment <PR番号> --body "...TEST_VERDICT: PASS/PARTIAL/FAIL"`
- **adversarial-reviewer**: `gh pr comment <PR番号> --body "...ADV_VERDICT: APPROVED/NEEDS_FIX"`
- **pr-reviewer**: `gh pr review <PR番号> --comment --body "...LOOP_VERDICT: yaml block..."`
- **オーケストラレータ**: `gh issue comment <Issue番号> --body "...LOOP_STATE: yaml block..."`

これらの structured コメントを primary surface として扱い、ループ判定の根拠とする。

---

## Guardrails

- オーケストラレータは `git push` および PR 作成を事前承認済みとして SubAgent に明示的に伝達すること（CLAUDE.md §4 による一時停止を防ぐため）。
- Windows ネイティブ検証など、ユーザー環境に影響がある検証操作を行う前に、test-runner は実行前同意を取得すること。
- PR ブランチ上の実装が正しければ、main との差分は HIGH として扱わないこと（PR 未マージ状態は正常）。adversarial-reviewer はこの誤検出を避けること。
- `context: fork` は frontmatter に含めない（nested delegation 違反）。
- Allowed Paths 外の変更が必要と判明した場合は即停止し、Issue comment に scope delta を記録して人間の判断を待つ。
- max_iterations: 5 を超えた場合は自律継続せず、エスカレーションコメントを残して停止する。
- 検証未実施のまま APPROVE 扱いにしない。
- LOOP_VERDICT 構造化ブロックが存在しない pr-reviewer コメントは APPROVE と見なさない。
- foreground 実行（`run_in_background: false`）では connector 経由の GitHub 自動投稿が機能する場合がある。ただし自動投稿の成否は実行環境・タイミングに依存するため、LOOP_VERDICT の記録は `gh pr view <PR番号> --json reviews,comments` で必ず確認すること。
- SubAgent は必ず expected_worktree_path 内で作業を開始すること。main worktree（リポジトリルート）での作業を検知した場合は即時停止し、オーケストラレータに報告する（サイレント続行禁止）。
- SubAgent 停止・失敗・worktree mismatch 時は、handoff 修復・worktree 修復・`send_input`/`resume_agent`/新規起動による再委任を優先し、オーケストラレータが本作業を直接代行しないこと。
- **SubAgent への Windows GUI 操作委任は禁止**。`windows-gui-dev` を Required Skills に含む Issue の GUI 操作（`powershell.exe -Command` による実行を含む）は、オーケストラレータが直接実行すること。オーケストラレータは GUI 操作前に AskUserQuestion でユーザー確認を取り、承認後に Bash ツールで実行する（理由: SubAgent 継続不可問題・確認タイミングずれの防止。Issue #912）。
- **フォローアップ Issue 起票の確認禁止**: オーケストラレータはフォローアップ Issue の起票をユーザーに確認しないこと。impl-review-loop の責務は PR 本文の Follow-ups Intentionally Deferred セクションへの記載のみ。Issue 起票は post-merge-cleanup スキルの責務であり、PR マージ後に実施する。
- **test-runner が live 検証を保留（pending）した場合の AskUserQuestion 義務**: test-runner SubAgent が windows-gui-dev 対象の live 検証（実機操作が必要な検証、または `TEST_VERDICT: PARTIAL` として保留報告されたもの）を「保留」と報告した場合、オーケストラレータは即座に AskUserQuestion を発行してユーザー確認を取ること。AskUserQuestion の省略・自動続行（保留のまま次 iteration に進む、または APPROVE 扱いにする）は禁止。ユーザーが「手動確認済み」「スキップ承認」いずれかを返答するまで iteration を進めない（背景: PR #1253 impl-review-loop 実行中に live 検証保留が無断で記録されオーケストラレータが省略進行したプロセス違反。Issue #1255）。

---

## Related

- skill: `.agents/skills/implement-issue/SKILL.md`
- skill: `.agents/skills/pr-review-judge/SKILL.md`
- skill: `.agents/skills/adversarial-review/SKILL.md`
- rule: `.agents/rules/github-ops-workflow.md`
- rule: `.agents/rules/issueops-common-guard.md`
- template: `templates/github-ops/pr-evidence.md`
