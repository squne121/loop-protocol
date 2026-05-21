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

## Issue / PR を主インターフェースとする原則

AI エージェントと人間のコミュニケーションは **Issue / PR を主インターフェース（primary interface）** として行う。

- エージェントのアクション（起票・コメント・ラベル更新）は Issue / PR 上に記録され、人間が追跡・取消可能。
- Skill / SubAgent が生成した観察・提案は、最終的に **Issue として具体化（materialize）** することで人間可視な形で管理する。
- 不要な Issue は **triage モデル** に従って処理する（後述）。

### triage モデル（不要なら close）

自動起票した follow-up Issue はすべて `triage-required` ラベルを付与して起票する。
人間または AI エージェントが triage した後、不要と判断した Issue は `not planned` で close する。
triage せずに積まれた Issue は定期 triage セッション（または `state/needs-human` エスカレーション）で処理する。

| triage 結果 | アクション |
|---|---|
| 有効な改善 | `triage-required` を外し、適切な `phase/` ラベルを付与して `state/queued` に移行 |
| 重複 | 既存 Issue にコメントして close（`duplicate` ラベル） |
| 不要 | `not planned` で close（理由をコメントに記録） |
| 判断保留 | `state/needs-human` を付与して人間判断を仰ぐ |

## Follow-up Materialization Policy

### FOLLOW_UP_ISSUE_REQUEST_V1

Skill / SubAgent が「後で Issue にすべき観察」を main thread に返す際に使う構造化スキーマ。
main thread（impl-review-loop Step 5 / post-merge-cleanup 等）が受け取り、`issue-author` / `create-issue` 経由で起票責務を担う。

```yaml
FOLLOW_UP_ISSUE_REQUEST_V1:
  title: "<起票する Issue のタイトル候補>"
  issue_kind: implementation | research | parent
  severity: mandatory_follow_up | optional_follow_up | note_only
  source:
    kind: pr_body | pr_review | issue_comment | post_merge_cleanup | refinement
    url: "<観察元の PR / コメント / Issue URL>"
    note_id: "<観察元ドキュメント内の通し番号（1-indexed）>"
  dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
  desired_destination: "<この Issue を解決したあとの状態（Outcome 1文）>"
  validated_scope_delta: "<create-issue に渡す In Scope の概要>"
  origin_skill: impl-review-loop | post-merge-cleanup | issue-refinement-loop | pr-review-judge
  labels:
    - triage-required  # 必須
    # 追加ラベル（docs, chore 等はここに入れる）
  initial_label_profile: triage_only | standard  # デフォルト: triage_only
  materialization:
    required_before_approve: true | false  # severity: mandatory_follow_up の場合 true
    existing_issue_url: null | "https://github.com/..."
    status: already_materialized | missing
```

**フィールド定義**:

| フィールド | 説明 |
|---|---|
| `title` | 起票候補タイトル（main thread が調整してよい） |
| `issue_kind` | `implementation`（実装）/ `research`（調査）/ `parent`（サブ Issue 親）。`docs`/`chore` 等は `labels` に入れる |
| `severity` | `mandatory_follow_up`（必ず起票）/ `optional_follow_up`（重複なければ起票）/ `note_only`（起票せず終了報告コメントに記録のみ） |
| `source.kind` | 観察元の種別（`pr_body` / `pr_review` / `issue_comment` / `post_merge_cleanup` / `refinement`） |
| `source.url` | 観察元の URL（PR URL、コメント URL 等） |
| `source.note_id` | 観察元ドキュメント内での通し番号（dedupe_key 生成に使用） |
| `dedupe_key` | 重複起票防止キー。形式: `follow-up:<repo>:<source-url-or-pr>:<note-id-or-hash>` |
| `desired_destination` | create-issue skill の handoff で必須。Outcome 1 文で書く |
| `validated_scope_delta` | create-issue の handoff で必須。変更範囲の概要 |
| `origin_skill` | どの skill が生成したかを追跡するためのフィールド |
| `labels` | `triage-required` を必ず含める。`docs`/`chore` 等はここに追加 |
| `initial_label_profile` | `triage_only`（デフォルト）: `triage-required` のみ付与し `state/queued` / `phase/implementation` / `agent/implementer` は付けない。`gh issue create` を直接使う。`standard`: create-issue skill の標準フローを使う（implementation Issue の通常起票） |
| `materialization.required_before_approve` | `severity: mandatory_follow_up` の場合 `true`。APPROVE 確定前に Issue を create または reuse する必要があることを示す |
| `materialization.existing_issue_url` | 既に materialize 済みの Issue URL。`null` は未 materialize |
| `materialization.status` | `already_materialized`（起票済み）/ `missing`（未起票・APPROVE 不可） |

**initial_label_profile に応じた起票フロー**:

```
initial_label_profile: triage_only（デフォルト）の場合:
  - create_issue_txn.py は使わず gh issue create を直接実行
  - 付与ラベル: triage-required + labels フィールドの内容のみ
  - state/queued / phase/implementation / agent/implementer は付与しない

initial_label_profile: standard の場合:
  - create-issue skill を通常フローで実行
  - create_issue_txn.py を通じた標準ラベル付与を行う
```

> NOTE: `triage_only` が自動起票の標準プロファイルである。`create_issue_txn.py` は現時点で `--label-profile triage-only` オプションを持たないため、`triage_only` の場合は `gh issue create` を直接使う。将来的に `create_issue_txn.py` に `--label-profile triage-only` を追加することが mandatory_follow_up Issue として起票予定である。

**severity に応じた action**:

```yaml
severity_actions:
  mandatory_follow_up:
    action: create_or_reuse_issue_before_approve
    note: "APPROVE 確定前に Issue を create または reuse する。未 materialize の場合は APPROVE しない"
  optional_follow_up:
    action: create_or_reuse_issue_at_loop_termination
    note: "ループ終了時（APPROVE 後）に dedupe チェックして起票"
  note_only:
    action: record_only_no_issue
    note: "起票せず終了コメントの note_only_observations に記録"
```

**follow-up Issue 本文の `## Source` セクション（自動起票 Issue 必須）**:

```markdown
## Source（自動起票 Issue 必須セクション）

- origin_skill: <origin_skill>
- source_url: <source.url>
- source_note_id: <source.note_id>
- dedupe_key: <dedupe_key>
```

**dedupe フロー**:

```
for each request:
  1. dedupe チェック: dedupe_key で既存 Issue を検索（open / closed すべて対象）
     gh issue list --repo <owner>/<repo> --state all \
       --search '"<dedupe_key>"' --json number,title,url,state,stateReason,labels
  2. 重複なし → issue-author SubAgent に委譲して create-issue 経由で起票
     ※ Issue 本文に ## Source セクション（dedupe_key を含む）を必須で付与
  3. 重複あり（open）→ スキップ（既存 Issue 番号をレポートに記録、status: reused_open）
  4. 重複あり（closed / not_planned）→ 起票せずスキップ（status: skipped_closed_not_planned）
  5. 重複あり（closed / completed）→ 起票せずスキップ（status: skipped_closed_completed）
  6. 重複あり（closed / duplicate）→ 起票せずスキップ（status: skipped_closed_duplicate）
  ※ closed Issue を open に差し戻して再利用する場合は human escalation が必要（自動起票不可）
```

**FOLLOW_UP_MATERIALIZATION_RESULT_V1**（各 skill の終了コメントで共通参照するスキーマ）:

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  follow_up_issues:
    - request_dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
      issue_number: 123 | null
      issue_url: "https://github.com/..." | null
      status: created | reused_open | skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed
  note_only_observations:
    - dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
      source_url: "<観察元の URL>"
      source_note_id: "<note_id>"
      summary: "<観察内容の要約>"
```

各 skill（`impl-review-loop` Step 5 / `post-merge-cleanup` / `issue-refinement-loop`）の終了コメントは本スキーマを参照して `follow_up_issues` と `note_only_observations` を報告する。

**責務境界**:

- `pr-review-judge`: non-blocker observations を `FOLLOW_UP_ISSUE_REQUEST_V1` として `LOOP_VERDICT.follow_up_issue_requests` に出力する。**Issue 起票は行わない**。
- `post-merge-cleanup-worker`: `follow_up_issue_requests` を `FOLLOW_UP_ISSUE_REQUEST_V1[]` として列挙して返す。**Issue 起票は行わない**。
- main thread（impl-review-loop Step 5 / post-merge-cleanup Delegation）: リクエストを受け取り、dedupe_key で dedupe チェック後に `issue-author` / `create-issue` 経由で起票する。

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
