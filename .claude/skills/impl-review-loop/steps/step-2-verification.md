# Step 2: Verification

Step 1 で PR が起票されたら、`test-runner` SubAgent に検証を委譲する。

Codex CLI では `test-runner` custom agent を起動し、root thread は file edit / test 実行 / commit / push / review judgment を直接行わない。

## 委譲呼び出し

Agent ツールで以下を呼ぶ:

```
subagent_type: test-runner
inputs:
  issue_number: <LOOP_STATE.issue_number>
  pr_number: <Step 1 で取得した PR 番号>
  ac_list: <linked issue の Acceptance Criteria 一覧>
  verification_commands: <linked issue の Verification Commands>
  contract_body_sha256: <live Issue body SHA>
  diff_head_sha: <diff summaryのhead_sha>
```

SubAgent 側は `.claude/agents/test-runner.md` の手順を実行し、Verification Commands を実行して `TEST_VERDICT_MACHINE v2` マーカー付きコメントを PR に投稿する。

## 受け取り結果の期待値

test-runner が PR コメントに投稿する `TEST_VERDICT` YAML:

```yaml
TEST_VERDICT:
  schema: TEST_VERDICT_MACHINE/v2
  producer_kind: test-runner
  repository: "<owner/repo>"
  issue_number: <int>
  pr_number: <int>
  head_sha: "<PR current head_sha>"
  reviewed_head_sha: "<reviewed head_sha>"
  diff_head_sha: "<diff summary head_sha>"
  contract_body_sha256: "sha256:<live Issue body SHA>"
  run_id: "<run ID>"
  run_url: "https://<run URL>"
  workflow_run_id: <GitHub Actions workflow run ID>
  workflow_run_attempt: <workflow run attempt>
  check_run_id: <GitHub check run ID>
  artifact:
    name: "<artifact name>"
    sha256: "sha256:<artifact content SHA256>"
    url: "https://github.com/<owner>/<repo>/actions/runs/<run>/artifacts/<id>"
  artifact_payload:
    issue_number: <int>
    pr_number: <int>
    head_sha: "<PR current head_sha>"
    reviewed_head_sha: "<reviewed head_sha>"
    diff_head_sha: "<diff summary head_sha>"
    contract_body_sha256: "sha256:<live Issue body SHA>"
    command_hashes: ["sha256:<command hash>"]
  result: PASS | PARTIAL | FAIL
  mergeable: MERGEABLE | CONFLICTING | UNKNOWN
  merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
  baseline_only: true | false
  verification_commands_pass: <int>
  verification_commands_fail: <int>
```

`pr_review_only` を baseline comparison から除外する adjudication では、`adjudicate_vc_result.py --test-verdict-file <TEST_VERDICT_MACHINE/v2 JSON>` に GitHub API readback 済みの artifact とこの実行済み証跡を渡す。adjudicator は producer/repository、Issue/PR、current/reviewed/diff HEAD、contract body SHA、run ID/URL、workflow/check run、artifact digest と payload binding、全対象ACの command hash と PASS/exit 0/no fallback/no skip を検証し、欠落または不一致なら fail-closed とする。PASS は正規VCと `pr_review_only` 除外VCを含む非空の `per_ac` coverage を必須とする。

`TEST_VERDICT` は Step 2 の実行結果を示すみにし、`baseline_only` は**routing の正本ではない**。
`baseline_only` は `adjudicate_vc_result.py` の evidence input としてのみ扱い、`VC_ADJUDICATION_RESULT_V1` の評価に渡す。

## 判定ルーティング

`VC_ADJUDICATION_RESULT_V1.overall_status` と `blocking` を contract snapshot + current VC + diff summary + allowed paths から生成し、Step 2 routing の正本にする。

`VC_ADJUDICATION_RESULT_V1` の `overall_status` / `blocking` が欠落、破損、期限切れである場合は fail-closed とし、Step 2 の判定は blocking とする。

判定表:

| 手順 | 条件 | 次アクション |
|---|---|---|
| 1 | `TEST_VERDICT.head_sha != PR current head_sha` | stale evidence として fail-closed。`VC_ADJUDICATION_RESULT_V1.blocking = true` 扱いで再検証へ |
| 2 | `VC_ADJUDICATION_RESULT_V1` 欠落・破損・期限切れ | fail-closed。Step 2 エビデンス不足/再実行扱いとして再判定へ |
| 3 | `VC_ADJUDICATION_RESULT_V1.blocking == false` | Step 3（pr-reviewer）へ |
| 4 | `VC_ADJUDICATION_RESULT_V1.blocking == true` | Step 5 へ。rerun / REQUEST_CHANGES / human escalation を判定 |

## 追加注意: baseline_only

- `TEST_VERDICT.result` は adjudicator input であり、routing 正本ではない。
- `baseline_only: true` のみで Step 2 を PASS と見なさない。
- `baseline_only` は、VC 判定結果の `evidence_refs`/`source_integrity` を整えるための参照情報とし、`VC_ADJUDICATION_RESULT_V1` の routing 正本を上書きしない。
- `VC_ADJUDICATION_RESULT_V1` の生成に必要な証跡（`baseline`, `current`, `diff`, `allowed_paths`）が欠損している場合は fail-closed で blocking。

## BEHIND 状態の取り扱い

`merge_state_status: BEHIND` は「head ref が base branch より古い（base が先行している）」状態を意味し、`mergeable: MERGEABLE` と両立する。
`BEHIND` は `CONFLICTING / DIRTY / BLOCKED` と同一視しない。`CONFLICTING PR Escalation Runbook` の発動条件に該当しない。

`BEHIND` の場合、Step 2 では `update-branch` / `rebase` を実行しない。
branch の更新（`gh pr update-branch` 等）は Step 5 および `#67` の責務として分離されており、Step 2 はその実行を担わない。

## 出力

LOOP_STATE.last_step = "verification" に更新し、`VC_ADJUDICATION_RESULT_V1` を会話履歴に保持して次ステップへ。
