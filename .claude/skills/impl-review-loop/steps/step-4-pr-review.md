# Step 4: PR Review

Step 2 の `VC_ADJUDICATION_RESULT_V1.blocking == false` で完了したら、`pr-reviewer` SubAgent に PR レビューを委譲する。Step 2 が `blocking == true`（FAIL 相当）の場合は本ステップをスキップして Step 5 に直行（REQUEST_CHANGES 確定）。

## current-head gate（Issue #88、Step 4 起動直前の再照合）

linked Issue に Verification Commands がある場合、`pr-reviewer` の `spawn_agent` を送信する **直前** に、Step 2 が保持している `VC_ADJUDICATION_RESULT_V1` を current-head binding tuple
（PR の現在の head SHA、linked Issue の現在の body SHA256、Verification Commands の
literal command SHA256 一覧）に対して再照合する。この再照合は
`.claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py` の
`evaluate_step4_vc_gate()` を使い、以下のいずれかに該当する場合は
`pr-reviewer` を起動しない（fail-closed）。

- `VC_ADJUDICATION_RESULT_V1` が欠落・破損（malformed）
- `blocking == true`（FAIL / SKIP / fallback 検出を含む）
- head SHA が current PR head と不一致（stale head）
- body SHA256 が現在の Issue 本文と不一致（stale body）
- literal command SHA256 の集合が現在の Verification Commands と不一致（stale command）

上記いずれにも該当しない場合のみ `pr-reviewer` を起動する。
同一 binding tuple（head/body/command が全て一致）の有効な adjudication が
`LOOP_STATE.vc_adjudication`（Step 2 が `step4_persist_vc_adjudication()` で
書き込む、plain YAML/JSON シリアライズ可能なマッピング）に既に存在する場合は
`step4_gate_from_loop_state()` がそれを再利用し、test-runner を再実行しない
（binding tuple の head/body/command のいずれかが変われば別 key となり、
旧 adjudication は自動的に stale として扱われ再実行される。旧 `Step4AdjudicationCache`
はプロセス内メモリのみに存在し CLI を複数回起動する構成では前回状態が残らなかったため、
Issue #88 fix_delta で LOOP_STATE ベースの永続化へ置き換えられた）。

TEST_VERDICT comment/artifact（存在する場合）は diagnostics-only であり、
この gate の判定入力にはならない。TEST_VERDICT だけを与えても Step 4 の gate は開かない。

### 実行コマンド例（current-head gate の具体的な呼び出し）

Step 2 が test-runner の read-only report（`TEST_VERDICT_MACHINE/v2` 相当）を
受け取ったら、まず canonical adapter で `adjudicate_vc_result.py --current-vc-result-file`
が受理する `baseline_vc_preflight/v1` 形へ変換し、通常の adjudicate 呼び出しで
`VC_ADJUDICATION_RESULT_V1` を得て `LOOP_STATE.vc_adjudication` へ永続化する
（`step4_persist_vc_adjudication()`、Python から呼ぶか、同等の永続化を
呼び出し元プロセスが行う）。

```bash
# 1) test-runner の read-only report(JSON) を current_vc_result schema へ変換
#    （adapter は adjudicate_vc_result.py の adapt subcommand として同居する）
uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py adapt \
  --test-verdict-file /tmp/test_runner_report.json \
  --adapt-out /tmp/current_vc_result.json

# 2) 通常の adjudicate 呼び出し（LOOP_STATE への永続化は呼び出し元が
#    step4_persist_vc_adjudication() で行う）
uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py \
  --contract-snapshot-file /tmp/contract_snapshot.json \
  --current-vc-result-file /tmp/current_vc_result.json \
  --diff-summary-file /tmp/diff_summary.json \
  --allowed-paths-file /tmp/allowed_paths.json

# 3) pr-reviewer を spawn_agent する直前に、live PR head / Issue body / literal
#    command SHA256 を再取得して current-head gate を評価する
uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py step4-gate \
  --loop-state-file /tmp/loop_state.json \
  --expected-head-sha "$(gh pr view <pr_number> --json headRefOid --jq .headRefOid)" \
  --expected-contract-body-sha256 "$(gh issue view <issue_number> --json body --jq .body | sha256sum | awk '"'"'{print "sha256:" $1}'"'"')" \
  --expected-command-hashes-file /tmp/expected_command_hashes.json
```

`step4-gate` は単一 JSON 決定を stdout に出力し、exit code は
`0=invoke`（pr-reviewer 起動可）/ `1=rerun`（Step 2 再実行が必要）/
`2=malformed`（`--loop-state-file` / `--expected-command-hashes-file` が
壊れている、または LOOP_STATE が object でない）である。`exit 1` の場合は
`pr-reviewer` を起動せず Step 2 に戻る。

Codex CLI では `pr-reviewer` custom agent を起動し、root thread は file edit / test 実行 / commit / push / review judgment を直接行わない。

## Discovery-Failure Recovery Routing（agent 定義変更後の delegation 失敗時の回復手順）

SubAgent 定義（`.claude/agents/*.md`）変更後に、その agent への delegation が
`Agent(subagent_type: "<変更対象>")` の `not found` 等で失敗しても、それを
即座に停止条件とせず、回復可能な runtime discovery failure（YAML parse
error、一時的な watch 遅延等の個別具体的原因を含みうる）の可能性を疑う。

### Stage A（bounded live-reload retry、短時間の再試行）

対象の `.claude/agents/` directory が既に session 開始時から存在し、
`--add-dir` 経由でも `--disable-slash-commands` でもない通常ケースでは、
agent 定義の変更は数秒以内に自動検出され次の delegation から使われるのが
公式の正常系である。`not found` が発生した場合、新しい retry framework を
作らず、短い bounded grace（例: 数秒程度）の後に現在 session で1回だけ
再 dispatch を試みる。

### Stage B（candidate-head fresh direct invocation、通常の第一候補 recovery）

Stage A で解決しない場合、candidate worktree を cwd とした fresh Claude
runtime で、対象 agent を `--agent <name>` として直接起動する。この
「fresh Claude runtime」は、呼び出し元 session が使っている
runtime/adapter をそのまま継承する（例: 呼び出し元 session が
`scripts/claude-gpt/launch.sh` 経由の `claude-gpt` adapter で
`--claude-bin` / `--claude-adapter claude-gpt` を指定して動作している
場合、Stage B もその同じ adapter 指定を保ったまま実行し、ambient な
plain `claude` バイナリへ黙って fallback しない）。そうしないと、実運用で
使われているものと異なる runtime 下で recovery が見かけ上成功してしまい、
production 固有の real failure を覆い隠しうる。

Stage B の成功条件は、達成したい保証の強さに応じて次の二段（tier）に
分かれる。

#### Tier 1: canonical review recovery（既定、通常はこれで十分）

plain `--agent <name>` による project-discovery route（既存の
`worktree-agent-runtime-smoke` の structured lane・`--claude-agent-name
<name>` route を再利用してよい）で fresh runtime を起動し、以下を満たせば
canonical review を実行する能力そのものは回復したとみなす: (i)
process/runtime が正常終了する、(ii) parse 可能な canonical
verdict／出力契約が返る、(iii) `reviewed_head_sha` 等が candidate
HEADと一致する、(iv) blockers/warnings が canonical contract に従う。
**この Tier 1 の成功だけでは、candidate-head の agent 定義が実際に
使用されたことの証明にはならない**（`worktree-agent-runtime-smoke` の
project-discovery lane 自身の canonical documentation が
`agent_definition` 結果を独立検証不能として `status: unavailable` と
定義しており、Claude Code の agent 解決は managed settings → `--agents`
→ project → user → plugin の priority order で行われ、cwd に近い
`.claude/agents/` が優先されるため、共有ディレクトリツリー内に同名の
agent 定義が複数存在する場合は filesystem read order が実際に使用された
定義を隠しうる）。**ここでは spawned child 用の
`SubagentStart`/`SubagentStop` / `causal_evidence_source ==
hook_id_correlated` を成功条件にしない**（`--agent <name>` による
main-session persona binding では child SubAgent が spawn されないため、
これを要求すると正常な canonical review を harness の都合で FAIL させて
しまう）。

#### Tier 2: candidate-definition binding（実際に証明が必要な場合のみ）

candidate-head の agent 定義が実際に使用されていることを厳密に証明する
必要がある場合に限り、`verify_pr_reviewer_permission_boundary.py` が既に
使っている passthrough の仕組み（candidate branch の `.md` agent 定義
ファイルをディスクから fresh に読み、session-local `--agents <json>`
引数として fresh な Claude Code invocation に渡す）を再利用する。これは
その passthrough の仕組み／パターンのみを再利用するものであり、同スクリプト
の permission-canary test suite 一式の実行を要求するものではない。Tier 2
まで実施した場合、Tier 1 の (i)〜(iv) に加えて、candidate HEADと一致する
agent 定義ファイルが実際に fresh invocation へ渡されたことを確認できた
場合にのみ、candidate-definition binding が成功したとみなす。

#### Stage B のハンドオフ（通常の loop への合流）

Stage B で fresh runtime に渡すプロンプトは、通常の `spawn_agent` 呼び出し
が渡すのと同じ materialize 済みレビュー入力を持たせる（実際の
`pr_number` と現在の candidate `reviewed_head_sha` を値として渡し、
placeholder を渡さない）。fresh process の最終出力は canonical
`LOOP_VERDICT`（`verdict` / `reviewed_head_sha` / `blockers` /
`warnings`）として parse する。Stage B がこの形で有効な verdict を得た
場合、その verdict はその Step 4 iteration の `pr-reviewer` 結果を
**代替する**。orchestrator は同一 iteration に対して `spawn_agent` や
child 用 Common Completion Protocol を再実行してはならない（`--agent
<name>` による main-session persona binding は child task を spawn
せず、待機する対象が存在しないため）。`worktree-agent-runtime-smoke` の
runtime `exit_code == 0` だけでは APPROVE authority にならず、単独では
有効な parsed verdict を構成しない（実際に消費されるのは parse された
LOOP_VERDICT の中身である）。

### Stage C（delegation/discovery diagnostic、必要な場合のみ）

「Agent tool による child delegation 自体」または「candidate 定義が child
として discover/spawn できること」を検証する必要がある場合に限り、
`worktree-agent-runtime-smoke` の structured lane・`--require-min-subagents
1`・**child 自身が出力する語**を `--expect-marker` に指定し、
`subagent_causal_evidence.causal_evidence_source == hook_id_correlated`
かつ `exit_code == 0` を成功条件とする。canonical review 完遂の必須 gate
にはしない。

Stage B が成功すれば通常の `impl-review-loop` を継続する。Stage C まで
必要な場合でも自動的に実行可能なら人間を呼ばない。

candidate head が変わった場合、既存の runtime evidence／review は
**staleとして扱い再取得**する。

Stage A〜C の全てで genuinely recovery 不可能な場合（fresh direct
invocation でも runtime/parse 失敗が続く等）にのみ、human/operator
blocker として停止する。

#### 「not found」の二つの意味の区別

本節が扱う discovery failure は、あくまで (a) `Agent(subagent_type:
...)` による pre-dispatch / dispatch 時点での agent-type discovery 失敗
（recoverable、本節の Stage A〜C の対象）である。これに対し、(b) 既に
生成された canonical child task 自身が後になって `list_agents` で
`not_found` を報告するケースは lifecycle-integrity failure であり、
本節の recovery routing の対象ではなく、既存の Common Completion
Protocol（本ファイル「Common Completion Protocol」節）の
`errored`/`interrupted`/`shutdown`/`not_found` の扱いに従い fail-closed
のままとする。

#### `--add-dir` に関する補足

`--add-dir` で追加したディレクトリ内の `.claude/agents/` は discovery
対象としてロードされるが、**watch されない**ため、追加・編集後は session
restart が必要である（Stage A の「通常ケース」から除外される具体的理由）。
本 repository が `--add-dir` を recovery route の第一候補にしない理由は、
discovery が不可能だからではなく、`${CLAUDE_PROJECT_DIR}` resolution が
hooks の `${CLAUDE_PROJECT_DIR}` interpolation を壊すためである
（`scripts/agent-ops/verify_pr_reviewer_permission_boundary.py` 11-38行
参照）。

## 委譲呼び出し

```yaml
spawn_agent:
  task_name: pr_review_i{iteration}
  agent_type: pr-reviewer
  fork_turns: none
  message: |
    Objective: review the actual implementation PR against its live Issue contract.
    Live reference: bind the actual PR number, linked Issue number, and reviewed head SHA.
    Bounded scope: bind the actual PR diff, AC, Allowed Paths, Verification evidence, and required checks.
    Expected result: LOOP_VERDICT with reviewed_head_sha, blockers, and warnings.
```

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の非負 iteration で `pr_review_i{iteration}` から materialize し、stale-head の再レビューでは次の未使用 iteration を使う。同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の PR number、linked Issue number、reviewed head SHA、PR diff、AC、Allowed Paths、Verification evidence、required checks を値として埋め込む。`Step 1 PR number`、`current reviewed head SHA`、変数名、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

SubAgent 側は `.claude/skills/pr-review-judge/SKILL.md` の手順を実行し、verdict 本文と最小 convention フィールドを呼び出し元へ返す（pr-reviewer は Write/Edit を持たないため、実際の PR コメント投稿は control-plane が行う。詳細は「期待する出力」参照）。

## Common Completion Protocol（共通完了プロトコル）

この規範は Step 1 implementation、Step 2 verification、Step 4 PR review、post-merge cleanup の4 dispatch site に共通である。

1. root は `spawn_agent` の戻り値から canonical `task_name` を保存する。
2. `wait_agent` は mailbox activity を待つためだけに使う。timeout、steer、途中 mailbox update は成功ではない。
3. root は対象 task の final result を消費してから `list_agents` で canonical task name の terminal `completed` を確認する。
4. `errored`、`interrupted`、`shutdown`、`not_found` は成功にせず fail-closed とする。
5. root は terminal `completed` と result の両方を確認した後にだけ最終 routing を決定する。

## PR レビュー前の CI 待機ルート

Step 4 では verdict 判定前に `wait_ci_checks.sh` を使って required checks の head-scoped 完了を待つ。

- `--required` は必須
- expected head SHA は Step 4 入力の `reviewed_head_sha`
- helper は全終了経路で `CI_WAIT_RESULT_V1_JSON=...` を 1 行だけ出力する
- exit code は `0=passed` / `1=CI negative or incomplete` / `2=auth, gh, malformed, invalid args`

```bash
.claude/skills/impl-review-loop/scripts/wait_ci_checks.sh \
  --repo "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
  --pr <pr_number> \
  --head-sha <reviewed_head_sha> \
  --required \
  --interval 15 \
  --timeout-seconds 1800
```

### CI_WAIT_RESULT_V1 status routing（ステータス別ルーティング）

| status | routing |
|---|---|
| `passed` | PR review を継続 |
| `failed` | `get_ci_failed_log.sh` を呼び出して failed log summary を取得 |
| `cancelled` | `get_ci_failed_log.sh` を呼び出して cancelled / interrupted context を取得 |
| `pending_timeout` | fail-closed。`CI_PENDING_TIMEOUT` blocker で REQUEST_CHANGES |
| `no_checks` | fail-closed。required checks 未解決として REQUEST_CHANGES |
| `skipped_only` | fail-closed。required checks が skipped のみとして REQUEST_CHANGES |
| `head_sha_changed` | stale review。最新 head に対して Step 4 を再実行 |
| `auth_error` | fail-closed。認証/権限問題として REQUEST_CHANGES |
| `gh_error` | fail-closed。CLI/runtime 問題として REQUEST_CHANGES |
| `malformed_gh_response` | fail-closed。machine-readable parse 不能として REQUEST_CHANGES |

`bucket=skipping` は成功扱いにしてはならない。required-only 集合に skipped entry が残る場合は incomplete とみなし、少なくとも `skipped_only` は fail-closed とする。

## 期待する出力

pr-reviewer は判定結果（verdict 本文 + `verdict` / `reviewed_head_sha` / `blockers` / `warnings` の最小 convention、Issue #1873）を呼び出し元（control-plane）へ返す。`merge_ready` / `mergeability` / `required_auto_actions` / `allowed_paths_gate` は pr-reviewer の自己申告として受け取らない。mergeability（`mergeable` / `merge_state_status`）は control-plane が `gh pr view --json headRefOid,mergeable,mergeStateStatus` で都度直接取得し、`route_loop_verdict_v2()` の `live_mergeability` 引数として渡す（`step-5-mergeability-handling.md` 参照）。

pr-reviewer は Write/Edit を持たないため、監査用の verdict コメント投稿は control-plane が通常の `gh pr comment --body-file` で行う（専用 semantic publisher は使用しない）。投稿する verdict コメント本文には人間可読の判定根拠（Mergeability / Evidence Check / Blockers / Non-blockers）を書き、以下の最小 YAML ブロックを併記する:

```yaml
verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED
reviewed_head_sha: <SHA>
blockers: []
warnings: []
```

投稿前後に `gh pr view --json headRefOid` で head をリードバックし、投稿後に head が変化していた場合は stale note として扱い fresh review を実行する（Safety Invariants）。pr-reviewer 自身は生の `gh pr review` を呼ばない。

`route_loop_verdict_v2()` によるルーティングの詳細は `step-5-mergeability-handling.md` を canonical とする。

## reviewed_head_sha 整合チェック

```bash
CURRENT_HEAD=$(gh pr view <pr_number> --json headRefOid --jq .headRefOid)
```

`reviewed_head_sha != CURRENT_HEAD` の場合は stale review とみなし、Step 4 を現在 head で再実行する。

## CI 失敗ログの取得（get_ci_failed_log helper）

`wait_ci_checks.sh` が `failed` または `cancelled` を返した場合のみ呼び出す。pending 中は呼び出さない。

```bash
REVIEWED_HEAD_SHA=$(gh pr view <pr_number> --repo <repo> --json headRefOid --jq .headRefOid)

.claude/skills/impl-review-loop/scripts/get_ci_failed_log.sh \
  --repo <owner/repo> \
  --pr <pr_number> \
  --head-sha "$REVIEWED_HEAD_SHA" \
  --max-bytes 60000
```

`reviewed_head_sha` には branch 名ではなく現在の PR head SHA を渡す。

### CI_FAILED_LOG_RESULT_V1_JSON の解釈

helper は出力末尾に `CI_FAILED_LOG_RESULT_V1_JSON: {...}` を出す。主要フィールドは以下。

```yaml
CI_FAILED_LOG_RESULT_V1:
  status: ci_failed | ci_passed | ci_pending | no_matching_run | log_unavailable
  run_id: <int>
  attempt: <int>
  head_sha: <sha>
  workflow_name: <str>
  failed_jobs: ["job-name", ...]
  retrieval_method: gh_log_failed | rest_job_logs | none
  redaction_applied: true | false
  truncated: true | false
```

| status | routing |
|---|---|
| `ci_failed` | log summary を `reviewer_verdict.blockers[]` に反映 |
| `ci_passed` | CI pass とみなしログ取得をスキップ |
| `ci_pending` | wait helper を再実行、または `CI_PENDING` blocker |
| `no_matching_run` | `CI_LOG_UNAVAILABLE` blocker |
| `log_unavailable` | `CI_LOG_UNAVAILABLE` blocker |
