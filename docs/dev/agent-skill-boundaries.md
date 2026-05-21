# Agent / Skill 責務境界

LOOP_PROTOCOL の Issue 駆動開発で使う各 SubAgent / Skill の責務境界を、開発者が運用上参照するためのドキュメント。
SKILL.md / SubAgent 定義に書くとコンテクスト汚染になるため、本ドキュメントを正本とする。

## SubAgent 役割分類と permissionMode 一覧

各 SubAgent を役割カテゴリ別に分類し、それぞれの `permissionMode` と主要ツール制約を示す。

| SubAgent | 役割カテゴリ | permissionMode | 主な tools | disallowedTools |
|---|---|---|---|---|
| `codebase-investigator` | read-only | `dontAsk` | Bash, Read | Edit, Write, MultiEdit, Grep, Glob |
| `pr-reviewer` | read-only | `dontAsk` | Bash, Read, Grep, Glob | Edit, Write, MultiEdit |
| `test-runner` | read-only | `dontAsk` | Read, Grep, Glob, Bash | Edit, Write, MultiEdit |
| `review-issue` | write | `acceptEdits` | Bash, Read, Grep, Glob, Write | Edit, MultiEdit |
| `issue-author` | write | `acceptEdits` | Bash, Read, Write | Agent, Edit, MultiEdit |
| `implementation-worker` | write | `acceptEdits` | Read, Grep, Glob, Bash, Edit, Write, MultiEdit | — |
| `post-merge-cleanup-worker` | cleanup | `default` | Bash, Read | Agent, Edit, Write, MultiEdit |

### 役割カテゴリの定義

| カテゴリ | 説明 | permissionMode 方針 |
|---|---|---|
| read-only | ファイル読み取り・gh 情報取得のみ。repo 変更なし | `dontAsk`（承認不要） |
| write | ファイル編集・Issue / PR 作成・コミットを行う | `acceptEdits`（編集系は自動、破壊的操作は ask） |
| cleanup | 破壊的 git/gh 操作（branch 削除・PR マージ等）を含む | `default`（破壊的操作は ask に残す） |

### cleanup 系の permissionMode 選択根拠

`post-merge-cleanup-worker` は `git branch -D` / `gh pr merge` / `git push` のような取り消し困難な操作を含む。
`permissionMode: default` を維持することで、これらの破壊的操作は Claude Code の通常の承認フローに残り、人間の確認を経る。
`dontAsk` にすると承認なしで branch 削除等が実行されるリスクがあるため非採用。

## 基本モデル

```
SubAgent（役割）── Skill（作業手順）
        │              │
        │              └─ references/（補助ドキュメント）
        │
        └─ 必要な複数の Skill を **使う**
```

- **SubAgent = 役割**: 「何を担当する人物か」。隔離されたコンテクストで動く実行者
- **Skill = 作業手順**: 「どう作業するか」の再現可能な手順書
- **関係**: SubAgent が Skill を使う。SubAgent と Skill は責務分離するものではなく、役割と手順の関係

### アンチパターン

- SubAgent 定義に詳細な作業手順を埋める（手順は Skill 側に書く）
- 複数 Skill が共有する説明・概念を独立 Skill にする（references/ または本ドキュメントに置く）
- 「Why this SubAgent exists」のような普遍的説明を SubAgent 定義に書く（普遍は本ドキュメントに集約）

## Issue 管理系

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `issue-author` | Issue を **起票・修正** する役割 | `create-issue`（新規起票）、`edit-issue`（既存修正）|

| Skill | 手順 | 呼び出し元の例 |
|---|---|---|
| `create-issue` | 新規 Issue 起票の手順（Template Guard / Outcome Quality Guard / scope 重複チェック / `gh issue create`） | `issue-author` SubAgent、main session、`issue-refinement-loop`、`post-merge-cleanup` |
| `edit-issue` | 既存 Issue 本文更新の手順（バックアップ / Guard / 差分閾値 / `gh issue edit --body-file`）| `issue-author` SubAgent、`issue-refinement-loop`、`post-merge-cleanup`、`review-issue`（needs-fix 適用時） |
| `review-issue` | Issue 本文の品質を決定論的にチェックして verdict と差分提案を返す | main session、`issue-refinement-loop` |
| `issue-contract-review` | 実装着手直前に作業計画・コンテクスト・開発フロー適合性を preflight | main session、`implement-issue` の手前 |
| `issue-refinement-loop` | Issue 改善 4 段ループのオーケストレーター | main session |

共通参照: [`create-issue/references/body-authoring.md`](../../.claude/skills/create-issue/references/body-authoring.md)
（VC 作成ガイダンス・Anchor Verification・Machine-Readable Contract block guidance 等。`edit-issue` / `issue-author` も参照する）

## 実装系

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `implementation-worker` | 実装作業の役割 | `implement-issue` |
| `test-runner` | Verification Commands 実行・AC 達成確認の役割 | （他 skill から委譲） |

| Skill | 手順 |
|---|---|
| `implement-issue` | 承認済み implementation issue を 1 PR で完了させる手順 |

## レビュー系

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `pr-reviewer` | PR レビューの役割 | `pr-review-judge` |

| Skill | 手順 |
|---|---|
| `pr-review-judge` | PR の review verdict（APPROVE / REQUEST_CHANGES）を決定する手順 |

## オーケストレーション系

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `post-merge-cleanup-worker` | PR マージ後 cleanup の役割 | `post-merge-cleanup` |

| Skill | 手順 |
|---|---|
| `impl-review-loop` | 実装→検証→PR レビュー の 4 段ループ手順 |
| `open-pr` | PR 起票手順 |
| `post-merge-cleanup` | PR マージ後の cleanup 手順 |

## 補助系

| SubAgent | 役割 |
|---|---|
| `codebase-investigator` | 大規模コードベース調査の役割（常に `gemini-cli-headless-delegation` skill に委譲して大規模文脈読み取りを行う） |

| Skill | 手順 |
|---|---|
| `ssot-discovery` | `docs/` 配下を SSOT として横断探索する手順 |
| `gemini-cli-headless-delegation` | Gemini CLI への headless 委譲手順 |
| `nlm-skill` | NotebookLM CLI / MCP 操作（既存導入） |

## Runtime Verification 責務分担

詳細なポリシーは `docs/dev/runtime-verification-policy.md` を SSOT とする。本セクションは各 Agent / Skill の役割分担のみを記載する。

| 役割 | Runtime Verification に関する責務 |
|---|---|
| `issue-author` | Issue に `## Runtime Verification Applicability` セクション（decision: not_applicable \| immediate \| deferred）を記載する。`deferred` の場合は後続 Issue / フェーズ / 条件を明記する |
| `review-issue` | 適用判定不在（C9 warning）、`deferred` の検証先不明（C10 blocker）を検出する |
| `issue-contract-review` | `immediate` の Issue で VC preflight を実施し、SKIP 規約・証跡保存・Stop Condition 連動が設計されているかを審査する |
| `implementation-worker` | `immediate` のときのみ VC スクリプトと artifacts/ 出力ロジックを実装する。`deferred` の場合は実装中に動作検証を捏造しない |
| `test-runner` | `immediate` の VC スクリプトを実行し、exit code と証跡を `TEST_VERDICT_MACHINE` に統合する。SKIP exit 77 を検知して `stop_condition_triggered: true` を返す |
| `pr-reviewer` | `immediate` で証跡なし / SKIP のみ / fallback PASS の場合は APPROVE しない。`deferred` は後続 Issue 参照の有無を確認する |

## 設計原則の補足

### review-issue と issue-contract-review の使い分け

| skill | 何を見るか | いつ呼ぶか |
|---|---|---|
| `review-issue` | Issue 本文の構造的品質（テンプレ準拠・AC 検証可能性・Outcome 具体性等） | Issue 起票後 / 改善ループ中 |
| `issue-contract-review` | 作業計画・コンテクストが指定通りで開発フローに沿って AI が安全着手できるか（VC preflight・AC 検証可能性・worktree/branch 命名） | 人間承認後・実装着手直前 |

責務が重なる項目（AC 検証可能性等）はあるが、**呼ぶタイミング**と **判定後の next action** が異なる。

### shared reference の置き場所

複数 skill が共通参照するガイドライン（VC 作成・Anchor Verification 等）は以下の順で配置を検討する:

1. 主体的に使う 1 つの Skill の `references/` 配下に置き、他 Skill から相対パスで参照
2. プロジェクト全体に関わる方針なら `docs/dev/` に置く
3. 独立 Skill にはしない（Skill は「何かを実行する手順」であり、共有参照は手順ではないため）

例: VC 作成ガイダンスは `create-issue/references/body-authoring.md` に置き、`edit-issue` / `issue-author` SubAgent から参照する。

## オーケストレーター設計原則（impl-review-loop / issue-refinement-loop）

### control-plane / data-plane の分離

- **オーケストレーター** は **control-plane**（state tracking + routing）のみを担当する
- **data-plane 操作**（push / `gh pr edit` / マージ / Issue 本文編集 等）は対応する **SubAgent に委譲** する
- オーケストレーターが直接 `git push` / `gh pr create` / `gh issue edit` を呼ばない

| 操作 | 担当 |
|---|---|
| state tracking（LOOP_STATE 更新） | オーケストレーター（control-plane） |
| routing（次の Step / SubAgent 決定） | オーケストレーター（control-plane） |
| 実装 / conflict resolve / push | `implementation-worker` SubAgent（data-plane） |
| Verification Commands 実行 | `test-runner` SubAgent（data-plane） |
| PR レビュー verdict 投稿 | `pr-reviewer` SubAgent（data-plane） |
| Issue 本文編集 | `issue-author` SubAgent + `edit-issue` skill（data-plane） |

### ループ内の人間承認原則

**ユーザーがループを起動した時点でループ全体の実行が承認されている。** ループ内の以下の決定では追加の人間承認を求めない:

- イテレーションの継続判断（REQUEST_CHANGES → 次イテレーション）
- Issue 本文の修正適用（refinement loop 中の改善ライト書き戻し）
- 各 Step 間の SubAgent 委譲

**例外**（人間判断を仰ぐ）:
- `max_iterations` 超過 → fail-close
- SubAgent から `human_review_required: true` を受けた場合（CONFLICT / blocked / 連続失敗 等）
- 想定外のエラー（DIRTY / BLOCKED の永続、verdict YAML 不正 等）

ループ内の routine な書き込み・コメント投稿は SubAgent 経由で自動進行する。

### LOOP_STATE による状態管理

ループの全状態は YAML 構造の LOOP_STATE で表現し、各 Step 完了直後に会話履歴へ明示記録する。

- LOOP_STATE は **次イテレーション開始時に最新値を読み戻す前提**
- 口頭サマリで上書きしない（Context Rot 防止）
- 全 SubAgent の出力（IMPLEMENT_RESULT_V1 / TEST_VERDICT / LOOP_VERDICT / REVIEW_ISSUE_RESULT_V1 / ISSUE_EDIT_RESULT_V1 等）を構造化フォーマットで受け取り、LOOP_STATE に反映する

### Context 効率（既存コンテクスト最大活用）

- **既存の GitHub state（Issue / PR / コメント）を最大限活用** し、メインセッションでの新規出力を最小限に抑える
- 各 SubAgent への inputs は既存 GitHub state の **参照**（Issue 番号 / PR 番号 / comment URL）で渡し、本文を main session に展開しない
- SubAgent の詳細な実行ログをメインに引き上げない（要約 + 構造化結果のみ）
- 「過去 iteration で言及済み」「Issue 本文に書いてある」を再展開しない

### 無限ループ防止と冪等性

- `max_iterations` 超過時は必ず fail-close（デフォルト: impl-review-loop 5 / issue-refinement-loop 3）
- 連続 conflict は 2 回までで自動 escalation
- SubAgent から `human_review_required: true` を受けたら即停止
- 同一 PR / Issue に対して同じ Step を複数回呼んでも壊れないこと（冪等性）
- LOOP_STATE.iteration を厳密に追跡し、退行しない
- pr-reviewer は `reviewed_head_sha` で stale review を検出する

## Loop Sequencing & Preconditions

impl-review-loop が期待する実行フェーズの順序・必須入出力・停止条件・引き継ぎ契約を示す。
各フェーズの **実際の実行証跡** は SubAgent Execution Ledger（後述）で記録する。

| phase | required_subagent_or_skill | required_input | required_output | stop_condition | handoff_contract | ledger_key |
|---|---|---|---|---|---|---|
| `issue_contract_preflight` | `issue-contract-review` skill | Issue 番号・contract_snapshot_url | `CONTRACT_REVIEW_RESULT_V1`（status: go） | status が `go` 以外 / Allowed Paths 不明 / VC preflight fail | `status: go` + worktree/branch 確定 | `contract_preflight` |
| `runtime_preflight` | `issue-contract-review` skill（runtime_verification_applicability: immediate のとき） | Issue の Runtime Verification Applicability セクション | SKIP 規約・証跡保存・Stop Condition 連動の設計確認 | `decision: immediate` で未設計 | 設計確認済み注釈 または deferred 宣言 | `runtime_preflight` |
| `implementation` | `implementation-worker` SubAgent | `CONTRACT_REVIEW_RESULT_V1`・Allowed Paths・worktree/branch | `IMPLEMENT_RESULT_V1`（status: ok） | Allowed Paths 外の変更が必要 / Stop Conditions に該当 | `IMPLEMENT_RESULT_V1` を次フェーズへ渡す | `implementation` |
| `post_commit_verification` | `test-runner` SubAgent | コミット済み HEAD sha・Verification Commands | `TEST_VERDICT_MACHINE/v1`（pass または partial） | test-runner 未実行 / SKIP-only / fallback PASS / stale head_sha | `TEST_VERDICT_MACHINE/v1` + head_sha を ledger に記録 | `post_commit_verification` |
| `pr_body_update` | `implementation-worker` SubAgent（`open-pr` skill 経由） | `IMPLEMENT_RESULT_V1`・`TEST_VERDICT_MACHINE/v1`・ledger summary | PR 本文（Closes/Refs・検証結果・ledger summary 含む） | PR 本文必須セクションの欠落 | PR URL を次フェーズへ渡す | `pr_body_update` |
| `semantic_review` | `pr-reviewer` SubAgent（`pr-review-judge` skill） | PR URL・head_sha・`TEST_VERDICT_MACHINE/v1` | `LOOP_VERDICT`（APPROVE または REQUEST_CHANGES + blockers） | 証跡なし / SKIP-only / fallback PASS で APPROVE 禁止 / stale head_sha | `LOOP_VERDICT` を loop オーケストレーターへ返す | `semantic_review` |
| `pre_merge_judgment` | `pr-reviewer` SubAgent（`pr-review-judge` skill） + ledger completeness gate | `LOOP_VERDICT`・ledger entries（required phases 完了確認） | APPROVE（全必須フェーズ完了かつ ledger 整合） | APPROVE 禁止条件のいずれかに該当（次セクション参照） | マージ可能状態を loop に通知 | `pre_merge_judgment` |

### フェーズ間の前提依存まとめ

```
issue_contract_preflight
        ↓ status: go
runtime_preflight（immediate のとき）
        ↓ 設計確認
implementation
        ↓ IMPLEMENT_RESULT_V1
post_commit_verification（material delta 後）
        ↓ TEST_VERDICT_MACHINE/v1
pr_body_update
        ↓ PR URL
semantic_review
        ↓ LOOP_VERDICT
pre_merge_judgment（ledger completeness gate）
        ↓ APPROVE
```

material delta とは: ソースコード変更 / 検証スクリプト変更 / schema・contract 変更 / runtime 動作変更 / 依存・設定・権限境界変更。
docs-only・merge-only・PR-body-only コミットは test-runner をスキップできるが、ledger に `skip_reason` を記録する。

## Mandatory SubAgent Contract

各フェーズで必須の SubAgent または skill と、それらが満たすべき契約を定義する。

### 必須 SubAgent / skill 一覧

| フェーズ | 必須 SubAgent / skill | 役割 |
|---|---|---|
| 実装着手直前 | `issue-contract-review` | VC preflight・Allowed Paths 確認・worktree 命名・status: go 判定 |
| runtime あり の preflight | `issue-contract-review`（runtime 審査） | SKIP 規約・証跡保存・Stop Condition 連動の設計審査 |
| 実装 | `implementation-worker` | Allowed Paths 内の実装・コミット・push |
| material delta 後の検証 | `test-runner` | Verification Commands 実行・exit code 記録・`TEST_VERDICT_MACHINE/v1` 返却 |
| PR 本文更新 | `implementation-worker`（`open-pr` skill） | ledger summary・検証結果・Closes/Refs の PR 本文組み込み |
| semantic レビュー | `pr-reviewer`（`pr-review-judge` skill） | AC coverage・Allowed Paths 遵守・証跡確認・APPROVE / REQUEST_CHANGES 判定 |
| merge 直前最終判定 | `pr-reviewer` + ledger completeness gate | 全必須フェーズが ledger に記録済みか確認してから APPROVE |

### APPROVE 禁止条件

以下のいずれかに該当する場合、`pr-review-judge` は APPROVE を返してはならない:

1. **test-runner 未実行**: PR の head_sha に対応する `post_commit_verification` ledger エントリが存在しない
2. **SKIP-only**: test-runner の全 VC が SKIP（exit 77）であり、明示的な `deferred` 契約が存在しない
3. **fallback PASS**: `_acp_fallback: true` 等のフォールバックフラグが立った状態で exit 0 を返した証跡がある
4. **stale head_sha**: `TEST_VERDICT_MACHINE/v1` または ledger の `head_sha` が、レビュー対象 PR の最新 head_sha と一致しない
5. **ledger 不完全**: `required: true` の ledger フェーズ（`issue_contract_preflight`・`post_commit_verification`）のエントリが存在しない
6. **PR 本文必須セクション欠落**: PR 本文から `## Verification Commands 結果` または `## SubAgent Execution Ledger` セクションが消失している

## SubAgent Execution Ledger

### 設計目的

Loop Sequencing が **設計上の期待順序** を示すのに対し、SubAgent Execution Ledger は **実際に実行された証跡** を記録する。
PR 本文への自己申告だけでなく、hook / transcript / GitHub comment / artifact から再構成できる形にする。

### YAML Schema 定義

```yaml
schema: subagent_execution_ledger/v1
pr: <PR番号>
head_sha: "<reviewed_head_sha>"
entries:
  - phase: issue_contract_preflight
    required: true
    agent: issue-contract-review
    status: pass          # pass | partial | skip | fail | blocked
    evidence:
      comment_url: "<contract snapshot comment URL>"
      transcript_ref: "<optional>"
  - phase: runtime_preflight
    required: false       # decision: not_applicable のとき false
    agent: issue-contract-review
    status: skip
    skip_reason: "decision: not_applicable"
  - phase: implementation
    required: true
    agent: implementation-worker
    status: pass
    commit_sha: "<sha>"
  - phase: post_commit_verification
    required: true
    agent: test-runner
    status: pass          # pass | partial（SKIP あり）
    commit_sha: "<sha>"
    verdict_ref: "TEST_VERDICT_MACHINE/v1"
    runtime_verification:
      skipped_count: 0
      fallback_detected: false
    human_review_required: false
  - phase: pr_body_update
    required: true
    agent: implementation-worker
    status: pass
    pr_url: "<PR URL>"
  - phase: semantic_review
    required: true
    agent: pr-reviewer
    status: pass
    loop_verdict: APPROVE
    reviewed_head_sha: "<sha>"
  - phase: pre_merge_judgment
    required: true
    agent: pr-reviewer
    status: pass
    ledger_complete: true
```

### status 値の定義

| status | 意味 |
|---|---|
| `pass` | 正常完了 |
| `partial` | 一部スキップ（SKIP exit 77）または一部 fail あり。`human_review_required: true` を伴う |
| `skip` | phase 全体をスキップ（`skip_reason` 必須） |
| `fail` | 明確な失敗（exit 1 / blocker あり） |
| `blocked` | 外部依存・権限不足等でそもそも実行できなかった |

### PR 本文への summary 方針

PR 本文に以下の形式で ledger summary を置く。正本は hook / transcript / GitHub comment / artifact から再構成可能にする。
self-reported ledger のみで完結した PR に対して APPROVE してはならない。

```yaml
## SubAgent Execution Ledger
schema: subagent_execution_ledger/v1
pr: <番号>
head_sha: "<sha>"
summary:
  total_phases: 7
  required_complete: 6
  skipped: 1
  human_review_required: false
entries:
  - { phase: issue_contract_preflight, status: pass, evidence: "<comment URL>" }
  - { phase: runtime_preflight, status: skip, skip_reason: "not_applicable" }
  - { phase: implementation, status: pass, commit_sha: "<sha>" }
  - { phase: post_commit_verification, status: pass, verdict_ref: "TEST_VERDICT_MACHINE/v1" }
  - { phase: pr_body_update, status: pass }
  - { phase: semantic_review, status: pass, loop_verdict: APPROVE }
  - { phase: pre_merge_judgment, status: pass, ledger_complete: true }
```

## Hook-based Ledger Optional Design

Claude Code の hooks を使って SubAgent の開始・終了・結果を自動記録する設計概要。
**実装は別 Issue で行う**（本 Issue スコープ外）。

### 対象 hook と役割

| hook | タイミング | 役割 | 実装先 |
|---|---|---|---|
| `SubagentStart` | SubAgent 起動直後 | agent_id・agent_type を ledger に記録開始 | `.claude/hooks/subagent-ledger.sh start` |
| `SubagentStop` | SubAgent 終了直後 | agent_transcript_path・last_assistant_message を記録 | `.claude/hooks/subagent-ledger.sh stop` |
| `PostToolUse(Agent)` | 親セッションが Agent ツール結果を受け取った後 | SubAgent の最終結果を PR ledger 用 JSONL に追記 | `.claude/hooks/subagent-ledger.sh parent-agent-result` |
| `Stop`（ledger completeness gate） | セッション終了前 | required phases の ledger エントリが存在するか検査し、不完全ならブロック | `.claude/hooks/subagent-ledger.sh check-completeness` |

### 推奨 hook 設定例

```json
{
  "hooks": {
    "SubagentStart": [
      { "matcher": "", "hooks": [{ "type": "command", "command": ".claude/hooks/subagent-ledger.sh start" }] }
    ],
    "SubagentStop": [
      { "matcher": "", "hooks": [{ "type": "command", "command": ".claude/hooks/subagent-ledger.sh stop" }] }
    ],
    "PostToolUse": [
      { "matcher": "Agent", "hooks": [{ "type": "command", "command": ".claude/hooks/subagent-ledger.sh parent-agent-result" }] }
    ]
  }
}
```

### 出力先

```text
.claude/worktrees/issue-<番号>-*/.agent-ledger/subagents.jsonl
.claude/worktrees/issue-<番号>-*/.agent-ledger/ledger.yaml
```

transcript path・prompt 断片・ローカルパス等の機微情報が含まれるため、repo 本体には常時コミットしない。
PR 本文には **ledger summary + head_sha + artifact path** までに留める。

### hook の用途範囲

| 用途 | hooks で行うべきか | 理由 |
|---|---|---|
| SubAgent 開始・終了の記録 | Yes | `SubagentStart` / `SubagentStop` がある |
| Agent tool 実行結果の PR ledger 化 | Yes | `PostToolUse` on `Agent` で拾える |
| Bash / gh / git の危険操作ブロック | Yes | `PreToolUse` は permissionMode より前に走り deny できる |
| test-runner 未実行時の Stop ブロック | 条件付き Yes | Stop hook で ledger completeness を検査可能 |
| 実装→検証→レビューの本体オーケストレーション | No | hook は slash command / tool call を直接起動できず、agent hook は experimental |
| 複数 SubAgent の厳密なワークフロー制御 | Agent SDK 寄り | 状態管理・再試行・権限・セッションをコードで扱うべき |

### Agent SDK との境界

hook は「決定論的なシェルコマンド実行・記録・ブロック」に向く。
`impl-review-loop` を外部の決定論的オーケストレーターに寄せたい場合は、Agent SDK 化を別 Issue で検討する。
