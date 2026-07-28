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

SubAgent 側は `.claude/agents/test-runner.md` の手順を実行し、Verification Commands を実行して結果を **read-only report として呼び出し元へ返す**。test-runner は PR へのコメント投稿を行わない（Issue #1648）。

## 読み取り専用レポート -> materializer -> 専用publisherの3段構成（Issue #1648）

`pr_review_only` を含む VC adjudication で current-head PASS を主張するには、以下の3段を経由する。raw comment（自己申告 JSON をそのまま PR へ貼るだけの経路）は正規経路ではない。

1. **test-runner（read-only report）**: Verification Commands を実行し、AC ごとの PASS/FAIL/SKIP と mergeable 状態を read-only report として返す（PR への書き込み・投稿は行わない）。
2. **materializer（`.claude/skills/impl-review-loop/scripts/materialize_test_verdict_artifact.py`）**: Child B（#1647）の `test_verdict.publish` request 相当の入力（Child A（#1646）の producer receipt （`schemas/test-verdict-producer-receipt.schema.json`）を含む）を受け取り、Child B の live readback 関数 （`_verify_producer_run_and_job` / `_verify_execution_artifact_metadata` / `_download_and_verify_artifact_archive`、いずれも `scripts/agent-guards/controlled_skill_mutation_exec.py`）を自ら呼び出して、receipt が主張する workflow run/job/check run と artifact を GitHub から実際に readback し、artifact archive を実際にダウンロードして digest を再計算する（execution record はこの live readback で実際にダウンロードされたものだけを使い、呼び出し元が渡す任意の execution record は信用しない）。current Issue/PR/HEAD/body SHA/artifact digest binding を独立に検証し、binding を満たす場合のみ `TEST_VERDICT_MACHINE/v2` input bundle と private/audit bundle を生成する（満たさない場合は fail-closed で何も生成せず、既存の古い成功済み bundle があれば無効化する）。
3. **dedicated publisher（`scripts/agent-guards/controlled_skill_mutation_exec.py` の `test_verdict.publish` コマンド、Child B）**: materializer が生成した input bundle 由来の publish request のみを受け付け、PR へ実際にコメントを投稿する。

`adjudicate_vc_result.py` は、materializer が生成した bundle を評価する際は `--require-producer-receipt` を指定して呼び出す。これにより producer receipt・artifact bytes digest・repository identity を検証しない handwritten TEST_VERDICT は fail-closed で拒否される（`--require-producer-receipt` を指定しない legacy 呼び出しは、既存の self-attested TEST_VERDICT 経路の後方互換のため従来通り動作する）。

## 受け取り結果の期待値

materializer が生成し dedicated publisher が投稿する `TEST_VERDICT` YAML（producer_receipt / receipt_sha256 を含む）:

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
  producer_receipt: <Child A TEST_VERDICT_PRODUCER_RECEIPT_V1、materializer が埋め込む>
  receipt_sha256: "sha256:<canonical producer_receipt SHA256>"
```

`pr_review_only` を baseline comparison から除外する adjudication では、`adjudicate_vc_result.py --test-verdict-file <TEST_VERDICT_MACHINE/v2 JSON> --require-producer-receipt` に materializer が生成した bundle を渡す。adjudicator は producer/repository、Issue/PR、current/reviewed/diff HEAD、contract body SHA、run ID/URL、workflow/check run、artifact digest と payload binding、全対象ACの command hash と PASS/exit 0/no fallback/no skip に加え、`producer_receipt` のフルスキーマ検証・`receipt_sha256` 一致・`pass_eligible: true`・artifact digest の receipt 側との一致を検証し、欠落または不一致なら fail-closed とする。PASS は正規VCと `pr_review_only` 除外VCを含む非空の `per_ac` coverage を必須とする。

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

## #88 との関係（Issue #1648 AC5）

`#88`（Step 2/4 の docs-only 責務記述）は、本ドキュメントが定める read-only report -> materializer -> dedicated publisher の実装経路によって実現された。`#88` 自体のクローズは本 Issue（#1648）のマージ後、merged readback を前提とした supersede close の対象として別途判断する（本ドキュメント更新自体は `#88` を自動でクローズしない）。
