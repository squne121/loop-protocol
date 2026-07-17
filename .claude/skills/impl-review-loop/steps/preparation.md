# Preparation（事前準備）

ループ開始前に LOOP_STATE を初期化し、必要な前提を確認する。

## 0-a. Intake capsule-first（インテーク処理をカプセル生成優先で行う）

着手直後は **`IMPL_REVIEW_INTAKE_CAPSULE_V1` を最優先で生成して利用**する。

以下の順序を固定し、同一データの重複取得を禁止する。

1. `gh issue view` / snapshot / `git status` などの直接取得は、まず実行しない。
2. `build_intake_capsule.py` を実行して `IMPL_REVIEW_INTAKE_CAPSULE_V1` を取得。
3. `issue_ready_tuple` / `contract_snapshot` / `repo_state` / `source_integrity` を capsule 内で先に消費。
4. capsule が `fresh` かつ `valid`（`source_integrity.evidence_complete == true`）な場合、**同一 loop 内では `gh issue view` / comments fetch / main `git status` / snapshot 探索を再実行しない**。
5. `next_action.route` が `proceed_to_step_1` の場合のみ、capsule に含まれる準備済み情報を用いて Step 1 へ進む。
6. `next_action.route == ensure_contract_snapshot` の場合のみ `ensure_contract_snapshot` を実行する。
7. `next_action.route == run_contract_blocker_triage` の場合は `contract_snapshot.contract_blocker_triage` を優先し、raw evidence の再分類や preflight 再実行を行わない。
8. `next_action.route == refresh_contract_snapshot` の場合は stale 扱いとして停止し、fresh snapshot の再取得へ route する。

```bash
uv run python3 .claude/skills/impl-review-loop/scripts/build_intake_capsule.py \
  --issue-number <issue_number> \
  --repo <owner/repo> \
  --max-stdout-bytes 4096
```

実行後、`stdout` の JSON 以外の `issue` 本文や `.claude/skills` 全文は展開しない。

```yaml
IMPL_REVIEW_INTAKE_CAPSULE_V1:
  schema: IMPL_REVIEW_INTAKE_CAPSULE_V1
  issue_ready_tuple: <ready判定結果>
  contract_snapshot: <status/route 依存>
  source_integrity: <command digest / parse warnings / evidence_complete>
  repo_state: <dirty summary>
  worktree:
    path: .claude/worktrees/issue-<N>-<slug>
    branch: worktree-issue-<N>-<slug>
  agent_runtime:
    runner: wsl2-ubuntu
    collector: build_intake_capsule.py
  next_action:
    route: ensure_contract_snapshot | run_contract_blocker_triage | proceed_to_step_1 | request_readiness_check | refresh_contract_snapshot | human_review_required
```

### Capsule failure policy（カプセル取得失敗時の方針）

`build_intake_capsule.py` がエラーを返した場合:

- `issue_ready_tuple` / `contract_snapshot` は trust-less のまま扱い、既存 Step 0 判定を継続して再実行しない。
- まず `stdout` の `errors` を確認し、必要なら `intake_gate_failed` 相当として停止。
- `artifact` の `issue_metadata` を参照し、原因を fix できる範囲だけ再収集。

`contract_snapshot.source` は以下を想定し、上位 Step が raw の再取得で同一ロジックを再実行しない:

- `ensure_contract_snapshot_result`
- `live_parse`

`contract_snapshot.normalized_status` は最低限 `go | missing_go | latest_blocked | stale | human_judgment | runtime_error` を区別し、`upstream_status` を潰して 1 つの missing/blocked にまとめない。


## 0. Intake Gate — `CONTRACT_REVIEW_RESULT_V1 status: go` 必須検査

`impl-review-loop` preparation の最初のゲート。以下の 5 つのサブ理由を **優先順位の高い順**に評価し、いずれかに該当する場合は `intake_gate_failed` として停止する。後続ステップへは進まない。

```yaml
INTAKE_GATE_RESULT_V1:
  status: pass | intake_gate_failed
  subreason: null | metadata_not_ready | missing_contract_go | stale_contract_review | body_snapshot_mismatch | request_changes_after_go
  detail: "<人間向け説明>"
```

### サブ理由の優先順位と判定条件

評価は以下の順序で行い、最初に該当したサブ理由で停止する（複数該当しても最初の 1 つを返す）:

#### 1. `metadata_not_ready`（最高優先）

Issue の routing metadata が `impl-review-loop` の前提を満たさない場合。以下のいずれかが欠落:

- **title prefix** が `実装:` または `implement:` で始まっていない
- **`phase/implementation` label** が付与されていない

```bash
gh issue view <issue_number> --json title,labels \
  --jq '{title: .title, labels: [.labels[].name]}'
```

どちらか一方でも欠落していれば `intake_gate_failed: metadata_not_ready` で停止。

#### 2. `missing_contract_go`（契約go判定が未検出の状態）

`contract_snapshot_url` が提供されておらず、Issue コメントにも有効な `CONTRACT_REVIEW_RESULT_V1 status: go` が存在しない場合。

- `contract_snapshot_url` 未提供 → Issue コメントを自動検出しても `status: go` の valid block が見つからない
- この場合は `ensure_contract_snapshot` を呼び出して contract snapshot の自動 materialize を試みる

> **設計決定 (#817)**: `missing_contract_go` 判定時は `ensure_contract_snapshot.py` へ routing する。
> `ensure_contract_snapshot` の結果に応じて以下の分岐をたどる:
>
> ```bash
> uv run python3 .claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py \
>   --issue-number <issue_number> \
>   --repo <owner/repo> \
>   --mode auto \
>   --post
> ```
>
> `termination_reason` 有効値（`null | approved | max_iterations | human_escalation | intake_gate_failed`）は routing 結果に基づいて LOOP_STATE へ記録する。
>
> | ensure_contract_snapshot 結果 | exit code | routing |
> |---|---|---|
> | `status: ok` (source: existing_go \| materialized_go) | 0 | contract_snapshot_url を LOOP_STATE に記録して Step 1 へ |
> | `status: blocked_needs_refinement` | 10 | `intake_gate_failed: missing_contract_go` で停止。`contract_review_once_result.vc_preflight_classifications[]` がある場合のみ `triage_contract_blockers.py` で短い triage summary を生成し、人間に refinement を依頼 |
> | `status: human_judgment` | 20 | 即停止。`termination_reason: human_escalation` を記録して人間判断へ |
> | `status: stale_or_conflicting_snapshot` (exit 50) | 50 | 即停止。Issue body が materialization 中に更新された。人間判断へ |
> | `status: runtime_error` | 40 | 即停止。環境エラーを記録して人間判断へ |
>
> **旧設計との差分**: #564 以前の旧設計（fail-only gate）では `missing_contract_go` で無条件停止していた。
> #817 以降は `ensure_contract_snapshot` への自動 routing を経由する。
> `ensure_contract_snapshot` が ok を返した場合のみ、`LOOP_STATE.contract_snapshot_source: materialized_by_issue_contract_review` を記録してループを継続する。

#### `blocked_needs_refinement` の triage normalizer（分類結果の要約処理・#959）

`ensure_contract_snapshot` が `status: blocked_needs_refinement` を返した場合、preparation は **preflight の再実行や GitHub mutation を行わず**、既存の blocked evidence を `triage_contract_blockers.py` へ渡して short summary に正規化してよい。

- 本 normalizer は **new classifier ではなく `normalizer_router`** として扱う。`vc_preflight.classifications[]` / `vc_preflight_classifications[]` / `baseline_vc_preflight/v1.results[]` を消費するだけで、raw command result の再分類はしない。
- Section 6 の `vc_preflight.status: blocked` routing とは責務が異なる。本節は **Step 0 / `missing_contract_go`** で止まったとき専用、Section 6 は **`status: go` 受信後の Step 1-c** 専用。
- accepted input:
  - `CONTRACT_SNAPSHOT_ENSURE_RESULT_V1.contract_review_once_result.vc_preflight_classifications[]`
  - `CONTRACT_REVIEW_ONCE_RESULT_V1.vc_preflight_classifications[]`
  - `CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight.classifications[]`（scalar は unsupported）
  - `baseline_vc_preflight/v1.results[]`
- unsupported input:
  - `source: latest_blocked` で `contract_snapshot_url` しかない snapshot-only payload
  - scalar-only `CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight`
- preparation が LOOP_STATE に記録するのは `CONTRACT_BLOCKER_TRIAGE_V1` の route key と **短い triage summary のみ**。raw stdout/stderr は埋め込まない（raw stdout / stderr を埋め込まない）。
- `CONTRACT_BLOCKER_TRIAGE_V1` の route key には `aggregate_reason`, `step1_allowed`, `termination_reason`, `intake_gate_subreason`, `issue_refinement_recommended`, `environment_retry_recommended`, `body_author_fixable`, `suggested_actions`, `per_ac`, `source_integrity`, `mutation_free` を含める。
- `aggregate_reason: mixed` は `step1_allowed: false` のまま停止し、human review を required route とする。

minimum CLI invocation（最小限のコマンド実行例）:

```bash
python3 .claude/skills/impl-review-loop/scripts/triage_contract_blockers.py \
  --input-file "$CONTRACT_SNAPSHOT_ENSURE_RESULT_FILE" \
  > "$CONTRACT_BLOCKER_TRIAGE_FILE"
```

normalizer 実行後は次を機械的に検査する:

```text
schema == CONTRACT_BLOCKER_TRIAGE_V1
status == ok
source_integrity.evidence_complete == true
step1_allowed == false
```

`unsupported_input` / `incomplete_evidence` / `invalid_input` / invalid JSON / non-zero exit はすべて fail-closed で `human_escalation` に route する。

推奨する埋め込み形は次のとおり:

```yaml
intake_gate:
  status: intake_gate_failed
  subreason: missing_contract_go
  contract_blocker_triage:
    schema: CONTRACT_BLOCKER_TRIAGE_V1
    aggregate_reason: mixed
    step1_allowed: false
    summary: "AC1 pytest exit 5 requires VC refinement; AC2 pnpm no-TTY is an environment artifact."
    suggested_next_action: human_review
```

#### 3. `stale_contract_review`（陳腐化した契約レビュー）

`CONTRACT_REVIEW_RESULT_V1.status == "go"` のコメントが存在するが、freshness チェックに失敗した場合:

- `body_sha256` フィールドが go コメントに存在し、かつ現在の Issue body の sha256 と一致しない
- `body_sha256` フィールドが存在しない場合のフォールバック: `CONTRACT_REVIEW_RESULT_V1.generated_at` < Issue の `updated_at`（go コメント生成後に Issue 本文が更新された）

いずれかの条件が真の場合は `intake_gate_failed: stale_contract_review` で停止し、`issue-contract-review` の再実行を人間に依頼する。

#### 4. `body_snapshot_mismatch`（本文スナップショット不一致）

`contract_snapshot_url` が明示的に提供され、かつ上記 freshness チェックで body_sha256 または generated_at の不一致が検出された場合。  
（ステップ 3 の freshness チェックがコメント自動検出時に対応し、本サブ理由は明示提供 URL が stale な場合に使用）

#### 5. `request_changes_after_go`（最低優先）

`status: go` のコメントより新しい `CONTRACT_REVIEW_RESULT_V1.status: blocked` または明示的な go 無効化 marker が存在する場合。

**go 無効化 marker の定義**（machine-readable policy block）:

```yaml
GO_INVALIDATION_POLICY_V1:
  source: issue_comment
  accepted_marker: fenced_yaml REVIEW_RESULT_V1.status == request_changes
  target_issue_url_must_match: true
  ordering_key: created_at
  precedence: latest_valid_marker_after_latest_valid_go
```

上記ポリシーに従い、Issue コメントに `REVIEW_RESULT_V1.status == request_changes` を含む fenced yaml block が存在し、かつ対象 Issue URL が一致し、`created_at` が直近の `status: go` より新しい場合は `intake_gate_failed: request_changes_after_go` で停止。

### Stop 時の必須処理（`on_intake_gate_failed`）

`intake_gate_failed` に該当した場合、以下の処理を行ってから停止する:

```yaml
on_intake_gate_failed:
  set LOOP_STATE.termination_reason: intake_gate_failed
  set LOOP_STATE.intake_gate.status: intake_gate_failed
  do_not_continue_to_step_1: true
```

`LOOP_STATE.termination_reason` の有効値: `null | approved | max_iterations | human_escalation | intake_gate_failed`

### 全サブ理由なし → `status: pass`

全ての条件を通過した場合のみ `INTAKE_GATE_RESULT_V1.status: pass` を返し、Step 1 へ進む。

`LOOP_STATE.intake_gate` に結果を記録する:

```yaml
intake_gate:
  status: pass | intake_gate_failed
  subreason: null | metadata_not_ready | missing_contract_go | stale_contract_review | body_snapshot_mismatch | request_changes_after_go
  evaluated_at: "<ISO8601>"
```

## 1. Inputs の確認

```yaml
issue_number: <int, 必須>
contract_snapshot_url: <URL, 任意>  # 省略時は以下の自動検出フローで取得
max_iterations: 3  # 任意
```

### 1-a. `contract_snapshot_url` が提供された場合

`gh api` でコメントを取得し、`CONTRACT_REVIEW_RESULT_V1`・`generated_by: issue-contract-review`・`status: go` の組み合わせが記録されていることを確認する。
確認できない場合は停止し、人間判断を仰ぐ。

`LOOP_STATE.contract_snapshot_source` に `provided` を記録する。

### 1-b. `contract_snapshot_url` が未提供の場合（自動検出フロー）

Issue コメント一覧から `CONTRACT_REVIEW_RESULT_V1` marker を持つ YAML block を検出し、以下の流れで contract_snapshot_url を決定する。

**前提: valid CONTRACT_REVIEW_RESULT_V1 の定義**

YAML block は `CONTRACT_REVIEW_RESULT_V1` marker を含むコメント本文内の fenced code block（``` yaml ... ```）に限定して抽出する。以下の fields がすべて存在し、値が妥当であることを必須とする:

- `status`: `go | blocked` のいずれか
- `generated_by`: `issue-contract-review`
- `issue_url`: 現在の Issue URL と完全一致（例: https://github.com/<owner>/<repo>/issues/<current_issue_number>）
- `generated_at`: ISO8601 形式の timestamp

本文に `CONTRACT_REVIEW_RESULT_V1` / `generated_by: issue-contract-review` / `status: go` が含まれるだけでは採用しない。review comments や example code blocks の引用を誤採用しないこと。

**Issue コメント取得の手順**

```bash
REPO_FULL_NAME=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
ISSUE_NUMBER=<issue_number>

gh api --paginate \
  "repos/${REPO_FULL_NAME}/issues/${ISSUE_NUMBER}/comments?per_page=100" \
  --jq '.[] | select(.body | contains("CONTRACT_REVIEW_RESULT_V1")) |
        {id, html_url, created_at, updated_at, body}'
```

出力は comment `id` 昇順（ascending）で返される。最新判定は `created_at desc, id desc` の precedence で行う（newer が先）。

**ステップ 1: 最新 `status: blocked` チェック**

上記コメント一覧から、valid `CONTRACT_REVIEW_RESULT_V1` YAML block を持つ最新コメント（`created_at desc, id desc`）を特定する。その `status` が `blocked` である場合は、古い `status: go` コメントが存在していても採用せず停止する。

```
最新の valid CONTRACT_REVIEW_RESULT_V1 が status: blocked → 停止（人間判断）
```

**ステップ 2: 既存 `status: go` の検出（idempotency 保証）**

最新の valid `CONTRACT_REVIEW_RESULT_V1` が `status: go` である場合、そのコメント URL を `contract_snapshot_url` として採用する。
既存 `status: go` が存在する場合は、以降の `issue-contract-review` 先行実行をスキップする（idempotency 保証）。

`LOOP_STATE.contract_snapshot_source` に `detected_existing` を記録する。

**ステップ 3: 既存 `status: go` が存在しない場合**

Step 0 の intake gate で `missing_contract_go` が検出された場合は、`ensure_contract_snapshot` を呼び出す（Section 2 の `missing_contract_go` 分岐を参照）。
Step 0 を通過して Step 1-b に到達するのは `contract_snapshot_url` が提供済みの場合のみのため、ここには通常到達しない。

> **スコープ境界（#245 との関係）**: #245 のプリフライトで `contract_snapshot_url` 未提供問題が再現したため、本 Issue（#149）は contract snapshot materialization の canonical fix として扱う。一方で、#245 で観察された環境固有の ready tuple / 関連調整（#245 は session-recording docs Issue）は本 Issue の対象外であり、別 Issue または #245 側の refinement で扱う。本ステップは contract snapshot の取得（materialization）のみを担う。

### 1-c. contract snapshot 内 VC preflight の参照（#329・事前検証結果の確認）

contract snapshot コメント内には `CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight` セクションが含まれており、以下の情報を保持:

- 各 VC の実行結果（exit code / stdout / stderr）
- root-cause 分類（category / decision / confidence）：`baseline_vc_preflight.py` による自動分類
- `status: pass | blocked | human_judgment`

**Step 1 (implementation) で実施すべき確認**:

- contract snapshot の `vc_preflight.status` を確認する:
  - `pass` → 続行（Step 1 implementation へ）
  - `blocked` → **Section 6（contract blocked 3 分類ルーティング）へ委譲する**。終了判断は Section 6 の `contract_blocked_reason` による
  - `human_judgment` → 即停止。`termination_reason: human_escalation` を記録して人間判断へ送る
- `vc_preflight.classifications` 配列を参照し、各 VC の `decision` を把握
  - `decision: go` → baseline で失敗することが予期される
  - `decision: blocked` → 実行不可能（コマンド不在等） → preflight status: blocked → 停止
  - `decision: human_judgment` → 分類不能 → preflight status: human_judgment → 停止して人間判断へ
- implementation-worker は `pass` 時のみ起動され、preflight 結果を context として活用する

preflight では `baseline_vc_preflight.py` により **script 化された自動分類** が行われるため、本ステップでは重複実行を避ける（idempotency）。

## 1-d. Product Spec Check の評価（#333・仕様適合判定）

contract snapshot から `CONTRACT_REVIEW_RESULT_V1.checks.product_spec_check` を読み取り、Step 1 delegation 前に決定論的に評価する。以下のスクリプトを実行:

```bash
PRODUCT_SPEC_GATE_DECISION=$(
  python3 .claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py \
    --snapshot-json - \
    --contract-snapshot-url "$CONTRACT_SNAPSHOT_URL" \
    < "$CONTRACT_SNAPSHOT_FILE"
)
```

where:
- `$CONTRACT_SNAPSHOT_FILE`: contract snapshot JSON を持つ一時ファイルのパス
- `$CONTRACT_SNAPSHOT_URL`: contract snapshot comment の GitHub URL（preparation step 1-a / 1-b で検出）

**評価ルール**:

- `routing_action: continue` → Step 1 へ進む
- `routing_action: stop_human` → 停止。`LOOP_STATE.termination_reason: human_escalation` を記録して人間判断へ送る
- `routing_action: refresh_contract_snapshot` → stale / incomplete snapshot として停止。`issue-contract-review` 再実行へ route

**LOOP_STATE への記録**:

評価結果を `LOOP_STATE.product_spec_preflight` に正規化して格納する。Step 1 には raw `product_spec_check` ではなく summary のみを渡す:

```yaml
product_spec_preflight:
  source: contract_snapshot.checks.product_spec_check
  applicability: applicable | not_applicable | missing
  decision: pass | fail | human_judgment | missing
  blocked_rule_ids: []
  contract_snapshot_url: "<url>"
  body_sha256: "<sha256>"
  routing_action: continue | stop_human | refresh_contract_snapshot
```

**注**: `impl-review-loop` は PS001〜PS006 の意味論判定を行わない。evaluation は contract snapshot に既に存在する `product_spec_check` 結果を読むだけ（mutation-free gate）。意味論判定は `issue-contract-review` / `check_product_spec_contract.py` に集約。

## 2. ready tuple の再確認

```bash
gh issue view <issue_number> --json title,labels --jq '.title + " | " + (.labels | map(.name) | join(","))'
```

期待する canonical ready tuple:
- title prefix: `実装:` または `implement:`
- labels: `phase/implementation`
- blocker / dependency: GitHub native dependency（`depends on` リンク）がすべて close 済み、または `Depends on #N` テキスト表現がすべて close 済み（primary signal）

不一致なら停止し、人間判断を仰ぐ。blocker / dependency の close 状態が primary signal であり、state labels の有無は ready 判定に影響しない。ただし `phase/implementation` は issue kind / workflow routing の前提として維持する（`docs/dev/github-ops.md` 参照）。

## 2.5. scope rollup preflight（`scope-rollup-runner` への委譲による事前確認）

worktree 作成前に scope rollup preflight を実行し、同一 Allowed Paths / 同一 skill family / 同一 parent_issue / 同一 dedupe_key を持つ OPEN Issue / PR の統合候補を確認する。
preflight は mutation-free（Issue 作成・編集・クローズ禁止）。

### 委譲手順

main conversation は raw `gh issue/pr list` output を直接展開せず、`scope-rollup-runner` SubAgent に委譲する。

**1. invocation_id を生成する**（重複排除用）:

```bash
INVOCATION_ID=$(date -u +%Y%m%dT%H%M%SZ)_$$
REPO_FULL_NAME=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
SCRIPT_SHA=$(sha256sum .claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py | cut -d' ' -f1)
REQUESTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

**2. `scope-rollup-runner` を起動する**（`.claude/agents/scope-rollup-runner.md` 定義に従う）:

以下の入力を渡して起動する:

```yaml
issue_number: <issue_number>
repo: <REPO_FULL_NAME>
invocation_id: <INVOCATION_ID>
```

runner は `ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1` marker を stdout に返す。

**内部実装（Issue #1547）**: `scope-rollup-runner` は内部で `scope_rollup.run` exact executor（`uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py --issue-number <issue_number> --repo <repo>`）を単一 transaction として呼び出す。GitHub read-only inventory 取得・pagination 完走判定・SHA256/count 計算・planner 呼び出し・result finalize はすべてこの exact executor 内で shell redirect なしに完結し、`local_main_branch_guard.py` はこの exact invocation のみを canonical root context で allow する（raw `gh ... > /tmp/...` は引き続き block）。この Step の外部契約（`ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1` marker schema・`invocation_id` 生成手順・Step 順序）自体は変更しない。

### ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1 仕様

runner が返す marker のスキーマ（ref-based 設計）:

```yaml
ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1:
  status: ok | failed | runner_unavailable
  schema_version: 1
  repo: "<owner/repo>"
  current_issue: <issue_number>
  invocation_id: "<invocation_id>"
  requested_at: "<ISO8601>"
  generated_at: "<ISO8601>"
  git_head_sha: "<sha>"
  script_path: ".claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py"
  script_blob_sha256: "<sha256>"
  inputs:
    current_issue_sha256: "<sha256>"
    issues_all_sha256: "<sha256>"
    prs_all_sha256: "<sha256>"
    issue_count: <int>
    pr_count: <int>
  result:
    plan_schema: "ISSUE_SCOPE_ROLLUP_PLAN_V2"
    raw_plan_location: null  # Issue #1547 以降固定。executor-owned private invocation directory は全経路で cleanup され、永続 artifact は存在しない
    result_sha256: "<sha256>"
    verify_status: "verified|not_verified"
    suggested_actions_summary: "<1-3行の候補サマリ>"
    candidate_count: <int>
    high_confidence_count: <int>
```

`result_sha256` は `raw_plan_location` のファイルバイト列の sha256。

`raw_plan_location` は Issue #1547 以降 `null` 固定である。`scope_rollup.run` exact executor は executor-owned private invocation directory を success/failure/timeout の全経路で cleanup するため、`runner` が別経路でファイルを保存する運用はしない。

### main conversation の marker 検証

runner から受け取った raw output を `parse_scope_rollup_run_result.py` へ渡し、parser の決定のみを採用する。marker の採点に失敗した場合は **raw `result.raw_plan_location` の直接読み取りは禁止**。

```bash
uv run python3 .claude/skills/impl-review-loop/scripts/parse_scope_rollup_run_result.py \
  --assistant-output-file /tmp/scope_rollup_<invocation_id>.txt \
  --capture-sidecar-file /tmp/scope_rollup_<invocation_id>.capture.yaml \
  --repo "${REPO_FULL_NAME}" \
  --issue-number <issue_number> \
  --invocation-id "${INVOCATION_ID}" \
  --expected-script-sha "${SCRIPT_SHA}" \
  --requested-at "${REQUESTED_AT}"
```

`parse_scope_rollup_run_result.py` は以下のどれかを返す:

- `status: ok` → `routing_action: continue`
- `status: runner_unavailable` → `routing_action: deferred`
- `status: failed` → `routing_action: stop_human`
- `status: marker_missing | marker_malformed | marker_ambiguous | rejected` → `routing_action: stop_human`
- sidecar missing / `capture_mode != subagent_stop_hook` / `capture_status != captured` / `capture_sha256` mismatch / `capture_path` mismatch / `agent_type` mismatch / `invocation_id` mismatch / `capture_source != last_assistant_message` → `routing_action: stop_human`
- `routing_action: stop_human` は `human_escalation` として扱う（`LOOP_STATE.termination_reason: human_escalation`）
- `routing_action: deferred` は `LOOP_STATE.scope_rollup_decision.decision: deferred` を許容する（必要に応じて追加で `human_escalation`）
- `raw_plan_location_allowed: true` の場合のみ `result.raw_plan_location` を読み取る

### marker 検証・採用（ref-based フロー）

`parse_scope_rollup_run_result.py` が `ok` を返した場合のみ marker を採用する:

1. `repo` が現在の `REPO_FULL_NAME` と一致する
2. `current_issue` が対象 Issue 番号と一致する
3. `invocation_id` が step1 で生成した値と一致する
4. `generated_at > requested_at`
5. capture sidecar が存在し、`capture_mode == subagent_stop_hook`、`capture_status == captured`、`capture_source == last_assistant_message`
6. sidecar の `agent_type` / `invocation_id` / `capture_path` / `capture_sha256` が期待値と一致する
7. `script_blob_sha256` が現在の `plan_issue_scope_rollup.py` の sha と一致する
8. `result.raw_plan_location` が存在し、`result.result_sha256` と一致し、`result.verify_status == verified`
9. `verify_scope_rollup_result.py`（issue-refinement-loop 版）検証が pass

これらが全て通過した場合:
- `result.suggested_actions_summary` を `LOOP_STATE.scope_rollup_plan.summary` に採用する
- `result.raw_plan_location` は debug 専用とし、default では main context に展開しない
- `ISSUE_SCOPE_ROLLUP_DECISION_V2` を記録して次ステップへ進む

### runner_unavailable / marker 違反の扱い

- marker 検証で `marker_missing / marker_malformed / marker_ambiguous / rejected` が返る場合:
  - `LOOP_STATE.termination_reason: human_escalation`（`termination_reason` 有効値: `null | approved | max_iterations | human_escalation | intake_gate_failed`）
  - `LOOP_STATE.scope_rollup_decision.decision: human_review_required`
  - `LOOP_STATE.scope_rollup_decision.runner_result.status` に `marker_missing | marker_malformed | marker_ambiguous` を記録し、`reject_reason` に `marker_missing | marker_malformed | marker_ambiguous | ...` を記録する
  - `LOOP_STATE.scope_rollup_decision.termination_cause` を `scope_rollup_marker_missing` / `scope_rollup_marker_malformed` に保存する
  - Step 3 へ進めず停止する

- runner が `status: runner_unavailable` を返し、marker 検証が構文上有効な場合は、従来どおり `decision: deferred` を許容する（必要に応じて `human_escalation`）
- runner が `status: failed` を返す場合は、`decision: human_review_required` として停止する

### orchestrator の判断ルール（marker 採用後）

marker 採用後、`result.suggested_actions_summary` および必要に応じて `result.raw_plan_location` のファイル内容（debug 時のみ）に基づいて以下の判断を行う:

- `confidence: high` の候補が存在する場合: orchestrator は各候補の `suggested_action` を確認してから次ステップへ進む。
- `suggested_action: human_review_required` の候補: 即時停止して human review（`termination_reason: human_escalation`）。
- `suggested_action: proceed_with_coordination` の候補: 関連 Issue 番号を `LOOP_STATE.scope_rollup_decision.related_coordination[]` に構造化保存して次ステップへ継続する。
- `confidence: medium` の候補: LOOP_STATE に記録し、推奨アクションを提示する。
- `confidence: low` または候補なし: 記録して次ステップへ進む。

**`ISSUE_SCOPE_ROLLUP_DECISION_V2` の記録**（統合実施・未実施にかかわらず常時記録）:

```yaml
ISSUE_SCOPE_ROLLUP_DECISION_V2:
  schema_version: 2
  recorded_at: "<ISO8601>"
  rollup_plan_ref:
    body_sha256: "<ISSUE_SCOPE_ROLLUP_PLAN_V2.body_sha256>"
    generated_at: "<ISSUE_SCOPE_ROLLUP_PLAN_V2.generated_at>"
  runner_result:
    invocation_id: "<INVOCATION_ID>"
    status: ok | failed | runner_unavailable | rejected | marker_missing | marker_malformed | marker_ambiguous
    reject_reason: null | failed | marker_missing | marker_ambiguous | marker_malformed | repo_mismatch | issue_mismatch | invocation_id_mismatch | requested_at_mismatch | stale | script_sha_mismatch | result_missing | raw_plan_location_invalid | verify_status_not_verified
  decision: executed | skipped | deferred | human_review_required
  termination_cause: null | scope_rollup_marker_missing | scope_rollup_marker_malformed
  skipped_reason: null
  related_coordination: []
  candidates_reviewed:
    - kind: "issue|pr"
      number: <int>
      confidence: "high|medium|low"
      suggested_action: "<action>"
      final_decision: "accepted|rejected|deferred|human_review_required"
      rejection_reason: null
```

`LOOP_STATE.scope_rollup_decision` に記録した後、Step 3（worktree/branch preflight）に進む。
詳細は `.claude/skills/issue-refinement-loop/references/scope-rollup-policy.md` を参照。

## 3. worktree / branch の事前確認（preflight・既存衝突の有無を確認する）

```bash
SLUG=$(echo "<title>" | sed 's/.*: //; s/[^a-zA-Z0-9]/-/g; s/--*/-/g; s/^-//; s/-$//' | tr A-Z a-z | cut -c1-40)
WORKTREE=".claude/worktrees/issue-${issue_number}-${SLUG}"
BRANCH="worktree-issue-${issue_number}-${SLUG}"

# 既存衝突確認
git worktree list | grep "$WORKTREE" && echo "[WARN] worktree 既存" || echo "[OK] worktree 未作成"
git branch --list "$BRANCH" && echo "[WARN] branch 既存" || echo "[OK] branch 未作成"
```

既存衝突あり → 過去のイテレーションの残骸の可能性。人間判断を仰ぐ。



## 4. Product Spec Gate 評価完了後の LOOP_STATE 初期化

Product Spec Check 評価（1-d）を完了後、iteration = 0 で以下の状態で開始:

```yaml
LOOP_STATE:
  issue_number: <int>
  contract_snapshot_url: <URL>
  contract_snapshot_source: provided | detected_existing | materialized_by_issue_contract_review
  iteration: 0
  max_iterations: 3
  worktree: .claude/worktrees/issue-<番号>-<slug>
  branch: worktree-issue-<番号>-<slug>
  last_step: null
  last_loop_verdict: null
  blockers_history: []
  external_research_skip_basis: null
  termination_reason: null
  intake_gate:
    status: pass | intake_gate_failed
    subreason: null | metadata_not_ready | missing_contract_go | stale_contract_review | body_snapshot_mismatch | request_changes_after_go
    evaluated_at: "<ISO8601>"
  product_spec_preflight:
    source: contract_snapshot.checks.product_spec_check
    applicability: applicable | not_applicable | missing
    decision: pass | fail | human_judgment | missing
    blocked_rule_ids: []
    contract_snapshot_url: "<url>"
    body_sha256: "<sha256>"
    routing_action: continue | stop_human | refresh_contract_snapshot
```

routing_action が `stop_human` または `refresh_contract_snapshot` の場合は、ループを開始せず人間判断へ escalate する。

## 5. 外部仕様調査スキップ判断（任意）

internal-only 変更（`src/state` / `src/systems` 内の純粋ロジック変更等）なら `external_research_skip_basis` に理由を記録してスキップ。
外部仕様が絡む場合は `gemini-cli-headless-delegation` を先行起動して情報を集める。

## 6. contract blocked 受信時の 3 分類ルーティング（vc_preflight_contract_blocked）

`vc_preflight.status: blocked` を受信したとき、orchestrator は `CONTRACT_BLOCKED_ROUTING_V1` スキーマに従い `contract_blocked_reason` を 3 種に分類し、各クラスの手順に従う。

**重要制約（#569）**:

- classifier logic を新設・改修することは禁止。既存の `vc_preflight.classifications[]` を消費するだけ。routing のみを行う。
- Issue 本文変更は issue-refinement-loop 経由のみ。require_body_sha256_after 付きで呼び出す。
- 本セクションは Step 0 の intake gate を通過済みの状態（`status: go` 受信後）で、Step 1-c の `vc_preflight.status: blocked` 分岐にのみ適用される。
- **LOOP_STATE 記録**: 本セクションの routing 結果は `intake_gate.vc_preflight.contract_blocked_routing` として LOOP_STATE に記録される（#564 の intake_gate 語彙との整合）。Section 6.4 参照。

### 6.1 decision table（分類判定のための一覧表 / matrix）

| contract_blocked_reason class | 定義 | autonomous_fix_allowed | required_route |
|---|---|---|---|
| `simple_contract_hygiene_fix` | AC の受入意味を変えず、決定論的に修正可能なケース。以下のいずれかを満たす: (a) `vc_preflight.classifications[]` の全分類が `category: format_error` または `category: command_typo`（typo・書式エラー）、(b) `category: trivially_pass` または `category: unexpected_pass` であり、VC の探索範囲を対象 AC のセクションに限定するスコープ修正のみ（`accepted`/`deferred` 値の注入なし、`tested_commit` 由来の値の注入なし）、(c) `preflight-scope: pr_review_only` の付与（VC 意味論変更なし、scope class のみ明示）。Issue body の意味論変更を要しない | true | issue-refinement-loop（本文変更なし、VC 文字列のみ修正）→ issue-contract-review 再実行 |
| `contract_refinement_required` | VC ロジックの修正や Allowed Paths の追加など、Issue contract の構造変更が必要。`vc_preflight.classifications[]` に `category: scope_gap` / `category: missing_path` / `category: ambiguous_ac` 等が含まれる | false | issue-refinement-loop 経由でスコープ確認 → issue-contract-review 再実行 → 人間承認が必要 |
| `human_decision_required` | 分類不能、または `vc_preflight.classifications[]` に `category: unknown` / `category: security_related` / `category: auth_required` 等が含まれる。autonomous fix 禁止、停止して人間判断 | false（autonomous fix 禁止） | 即停止。`termination_reason: human_escalation` を記録して人間判断を仰ぐ |

### 6.2 CONTRACT_BLOCKED_ROUTING_V1 スキーマ

```yaml
CONTRACT_BLOCKED_ROUTING_V1:
  source_contract_review_comment_url: "<blocked status の issue-contract-review コメント URL>"
  body_sha256_before: "<blocked 受信時点の Issue body sha256>"
  contract_blocked_reason: simple_contract_hygiene_fix | contract_refinement_required | human_decision_required
  autonomous_fix_allowed: true | false
  required_route: "<routing 先の説明>"
  rerun_required: true | false  # issue-contract-review の再実行が必要かどうか
  classification_basis:
    - ac: "<AC ID>"
      category: "<vc_preflight.classifications[].category>"
      decision: "<vc_preflight.classifications[].decision>"
      rationale: "<分類根拠>"
```

### 6.3 各クラスのエスカレーション手順

#### simple_contract_hygiene_fix（自律修正フロー）

1. `vc_preflight.classifications[]` から修正対象の VC コマンド文字列を特定する
2. `issue-refinement-loop` を `require_body_sha256_after: true` 付きで呼び出し、VC 文字列のみを修正する（Issue body の意味論は変更しない）
3. 修正完了後に `issue-contract-review` を再実行する
4. 再実行結果が `status: go` であれば実装フローへ戻る（Step 1 の `vc_preflight.status: pass` 確認）
5. 再実行後も `status: blocked` であれば `contract_refinement_required` または `human_decision_required` に昇格して対応する

**正例（positive example・許容される具体例）**:

- `rg "foo" path/to/file` が `path/to/file` のスペルミス（正しくは `path/to/fle`）で blocked → typo fix のみで再実行可
- AC の VC コマンドに余分な引用符やエスケープが含まれ、exit_code: 1 となっている → format_error として修正可
- `preflight-scope: pr_review_only` フラグを VC に付与する → VC 意味論は変更せず scope class のみ明示するため `simple_contract_hygiene_fix` 対象
- `rg "tested_commit:" docs/playtest/m2-combat-mvp.md` が baseline pass（frontmatter に既存フィールドが存在）→ `rg -U "^tested_commit:" docs/playtest/m2-combat-mvp.md --multiline-dotall` など Human Playtest Evidence セクション限定の VC に絞り込む → section-scoped VC rewrite として `simple_contract_hygiene_fix` 対象

**負例（negative example・許容されない具体例）**:

- VC が参照する Allowed Path 自体が存在しない → `scope_gap` であり `contract_refinement_required` が必要
- VC の期待値（grep 対象文字列）が実装内容と乖離している → 意味論変更であり `simple_contract_hygiene_fix` 対象外
- 動画証跡 URL を VC コマンドに含める変更 → 証跡保存先の指定は意味論変更であり autonomous fix 禁止
- 環境情報（OS、node version 等）の記述を VC に追加する変更 → VC 意味論変更であり `simple_contract_hygiene_fix` 対象外
- `tested_commit:` 由来の値を VC コマンドに組み込む変更 → 実行時依存の注入は意味論変更であり autonomous fix 禁止
- `accepted` / `deferred` 判定を VC の結果評価に追加する変更 → VC 合否の意味論変更であり `human_decision_required` に該当

#### contract_refinement_required（人間承認が必要な修正フロー）

1. `CONTRACT_BLOCKED_ROUTING_V1` を `LOOP_STATE.contract_blocked_routing` に記録する
2. `issue-refinement-loop` を経由して Issue contract の修正を提案する（自律実行は禁止。人間の承認を待つ）
3. 人間承認後に `issue-contract-review` を再実行する
4. 再実行結果が `status: go` であれば実装フローへ戻る

#### human_decision_required（停止・人間判断）

autonomous fix 禁止。即停止して人間判断を仰ぐ。`LOOP_STATE.termination_reason: human_escalation` を記録する。

- `human_decision_required` → 停止。人間が明示的に再開指示を出すまで実装を再開しない
- `category: security_related` / `category: auth_required` の分類が含まれる場合、autonomous fix は絶対に禁止

### 6.4 LOOP_STATE への記録

本セクションは `intake_gate.vc_preflight.contract_blocked_routing` として LOOP_STATE に記録される（#564 の intake_gate 語彙との整合）。

```yaml
LOOP_STATE:
  termination_reason: human_escalation | null  # human_decision_required の場合のみ human_escalation
  intake_gate:
    status: intake_gate_failed  # human_decision_required の場合のみ
    vc_preflight:
      status: blocked
      contract_blocked_routing:
        schema_version: CONTRACT_BLOCKED_ROUTING_V1
        evaluated_at: "<ISO8601>"
        source_contract_review_comment_url: "<blocked status の issue-contract-review コメント URL>"
        body_sha256_before: "<blocked 受信時点の Issue body sha256>"
        contract_blocked_reason: simple_contract_hygiene_fix | contract_refinement_required | human_decision_required
        autonomous_fix_allowed: true | false
        required_route: "<routing 先>"
        rerun_required: true | false
        classification_basis:
          - ac: "<AC ID>"
            category: "<category>"
            decision: "<decision>"
            rationale: "<分類根拠>"
```

## 出力

LOOP_STATE 初期値を会話履歴に明示記録し、Step 1（Implementation）へ進む。
