# Step 2: Verification（検証ステップ）

Step 1 で PR が起票されたら、`test-runner` SubAgent に検証を委譲する。

Codex CLI では `test-runner` custom agent を起動し、root thread は file edit / test 実行 / commit / push / review judgment を直接行わない。

## 委譲呼び出し

Agent ツールで以下の static call shape を使って起動する:

```yaml
spawn_agent:
  task_name: verification_i{iteration}
  agent_type: test-runner
  fork_turns: none
  message: |
    Objective: execute the actual linked Issue Verification Commands as an independent read-only report.
    Live reference: bind the actual Issue number, PR number, contract body SHA, and diff head SHA.
    Bounded scope: bind the literal AC list and literal Verification Commands for that exact head only.
    Expected result: a head-bound test-runner report with per-AC PASS, FAIL, or SKIP facts.
```

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の非負 iteration で `verification_i{iteration}` から materialize し、同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の Issue number、PR number、AC 全文、literal Verification Commands 全文、contract body SHA、diff head SHA を値として埋め込む。`LOOP_STATE`、`Step 1 PR number`、`current contract body SHA`、変数名、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

完了の扱いは4 site 共通の [Common Completion Protocol](step-4-pr-review.md#common-completion-protocol) に従う。Step 4 起動前に、root は `list_agents` で test-runner task の terminal `completed` と、その task の final result（read-only report）の両方を確認する（Issue #88 Required Design #1）。terminal `completed` のみ、または final result のみでは Step 2 完了とみなさない。

SubAgent 側は `.claude/agents/test-runner.md` の手順を実行し、Verification Commands を実行して結果を **read-only report として呼び出し元へ返す**。test-runner は PR へのコメント投稿を行わない（Issue #1648, #88）。

## 独立検証（証拠権限の切替、Issue #1856 Phase 1）

Step 2 の routing 正本は、TEST_VERDICT comment/artifact の有無に依存しない。orchestrator（呼び出し元）が以下をその場で照合するだけで判定する。materializer・dedicated publisher・producer側の署名情報・PR 上の TEST_VERDICT コメントは、この照合の routing input として要求しない（それらの実装ファイル自体は Phase 3 の別 Issue まで物理削除しないが、Step 2 の判定ロジックはこれらに依存しない）。

1. **current head SHA の一致**: test-runner の read-only report が主張する `head_sha` が、orchestrator が独立に取得した PR の current head SHA（`gh pr view --json headRefOid` 等）と一致すること。不一致は stale evidence として fail-closed（`VC_ADJUDICATION_RESULT_V1.blocking = true`）。
2. **literal command SHA256 の一致**: report に含まれる各 Verification Command の実行文字列が、linked Issue の Verification Commands ブロックに記載された literal command と一致すること（command 文字列そのものの SHA256 一致、または orchestrator による文字列比較のいずれかで確認する）。commands の改変・省略・置換は fail-closed。
3. **AC ごとの PASS/FAIL/SKIP と exit code**: report の per-AC 結果を、上記 2 点の照合が成立した場合にのみ evidence として採用する。

TEST_VERDICT（materializer/publisher 経由で PR に投稿される YAML、存在する場合）は、上記の独立検証を代替しない。TEST_VERDICT の有無に関わらず、Step 2 は同じ独立検証手順から同じ判定を返す。新規 artifact schema は追加しない。

## 判定ルーティング

`VC_ADJUDICATION_RESULT_V1.overall_status` と `blocking` を、上記の独立検証結果 + contract snapshot + diff summary + allowed paths から生成し、Step 2 routing の正本にする。

`VC_ADJUDICATION_RESULT_V1` の `overall_status` / `blocking` が欠落、破損、期限切れである場合は fail-closed とし、Step 2 の判定は blocking とする。

判定表:

| 手順 | 条件 | 次アクション |
|---|---|---|
| 1 | test-runner report の `head_sha` != PR current head SHA | stale evidence として fail-closed。`VC_ADJUDICATION_RESULT_V1.blocking = true` 扱いで再検証へ |
| 2 | 実行された Verification Command の文字列が linked Issue の記載と不一致 | fail-closed。`VC_ADJUDICATION_RESULT_V1.blocking = true` 扱いで再検証へ |
| 3 | `VC_ADJUDICATION_RESULT_V1` 欠落・破損・期限切れ | fail-closed。Step 2 エビデンス不足/再実行扱いとして再判定へ |
| 4 | `VC_ADJUDICATION_RESULT_V1.blocking == false` | Step 3（pr-reviewer）へ |
| 5 | `VC_ADJUDICATION_RESULT_V1.blocking == true` | Step 5 へ。rerun / REQUEST_CHANGES / human escalation を判定 |

## BEHIND 状態の取り扱い

`merge_state_status: BEHIND` は「head ref が base branch より古い（base が先行している）」状態を意味し、`mergeable: MERGEABLE` と両立する。
`BEHIND` は `CONFLICTING / DIRTY / BLOCKED` と同一視しない。`CONFLICTING PR Escalation Runbook` の発動条件に該当しない。

`BEHIND` の場合、Step 2 では `update-branch` / `rebase` を実行しない。
branch の更新（`gh pr update-branch` 等）は Step 5 および `#67` の責務として分離されており、Step 2 はその実行を担わない。

## 出力

LOOP_STATE.last_step = "verification" に更新し、`VC_ADJUDICATION_RESULT_V1` を会話履歴に保持して次ステップへ。

## #88 との関係（Issue #1648 AC5 / Issue #1856 Phase 1 / Issue #88 reframe で更新）

`#88`（Step 2/4 の docs-only 責務記述）は、当初は read-only report -> materializer -> dedicated publisher の実装経路によって実現されていた。Issue #1856（evidence authority cutover, Phase 1）により、Step 2 の routing 正本は TEST_VERDICT comment/artifact の有無に依存しない独立検証（current head SHA + literal command SHA256 の照合）へ切り替わった。materializer/publisher/producer側の実装自体は Phase 3 の別 Issue まで物理削除しないが、Step 2 の判定契約はそれらを要求しない。

その後 PR #2310 のレビューで、独立 Issue VC が Step 4 の初回 reviewer 起動前に渡されず、同一 head に対して不要な二重 review（REQUEST_CHANGES → APPROVE）が発生した。これを受けて `#88` は「独立VCが current-head で完了・束縛・adjudicate されるまで Step 4 の pr-reviewer を起動しない」contract へ reframe された。この gate 自体は `step-4-pr-review.md` の「current-head gate」節、および `.claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py` の `evaluate_step4_vc_gate()` / `step4_gate_from_loop_state()` として実装される。`#88` 自体のクローズは、merged readback を前提とした supersede close の対象として別途判断する（本ドキュメント更新自体は `#88` を自動でクローズしない）。

test-runner が返す `TEST_VERDICT_MACHINE/v2` の read-only report は、`adjudicate_vc_result.py` が受理する `baseline_vc_preflight/v1` 形とスキーマが異なる。Step 2 はこの report を `adjudicate_vc_result.py` の `adapt` subcommand（`adapt_test_verdict_to_current_vc_result()`、canonical adapter）で変換してから通常の adjudicate 呼び出しへ渡し、得られた `VC_ADJUDICATION_RESULT_V1` が `evaluate_step4_vc_gate()` を満たす場合のみ `step4_persist_vc_adjudication()` で `LOOP_STATE.vc_adjudication`（binding tuple キー付きの plain mapping）へ永続化する。Step 4 は test-runner を再起動せず、`step4_gate_from_loop_state()`（`adjudicate_vc_result.py step4-gate` CLI、`step-4-pr-review.md` 参照）で live binding へ再照合するのみとし、stale/invalid なら Step 2 へ戻して新規 test-runner task を起動する。
