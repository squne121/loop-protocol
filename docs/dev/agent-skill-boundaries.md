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
| `review-issue`（standalone SubAgent） | write | `acceptEdits` | Bash, Read, Grep, Glob, Write | Edit, MultiEdit |
| `issue-reviewer`（loop worker SubAgent） | read-only | `dontAsk` | Bash, Read, Grep, Glob | Agent, Edit, Write, MultiEdit |
| `issue-author` | write | `acceptEdits` | Bash, Read, Write | Agent, Edit, MultiEdit |
| `implementation-worker` | write | `acceptEdits` | Read, Grep, Glob, Bash, Edit, Write, MultiEdit | — |
| `post-merge-cleanup-worker` | cleanup | `default` | Bash, Read | Agent, Edit, Write, MultiEdit |

### `review-issue` / `issue-reviewer` の使い分け

| エントリ | 種別 | 呼び出し元 | 役割 |
|---|---|---|---|
| `review-issue` Skill | Skill（手順書） | main session・各 SubAgent | Issue 本文の品質を決定論的チェックして `REVIEW_ISSUE_RESULT_V1` を返す手順 |
| `review-issue` SubAgent（`review-issue.md`） | write SubAgent | main session（standalone） | Issue 本文編集を伴う standalone レビュー。`gh issue edit` を human-in-the-loop で実行する |
| `issue-reviewer` SubAgent（`issue-reviewer.md`） | read-only SubAgent | `issue-refinement-loop`（loop worker） | `review-issue` skill を内部で実行し `REVIEW_ISSUE_RESULT_V1` を返すのみ。Issue の mutation を行わない |

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
| `issue-reviewer` | `issue-refinement-loop` の loop worker として Issue 品質を判定する役割（read-only） | `review-issue` |

| Skill | 手順 | 呼び出し元の例 |
|---|---|---|
| `create-issue` | 新規 Issue 起票の手順（Template Guard / Outcome Quality Guard / scope 重複チェック / `gh issue create`） | `issue-author` SubAgent、main session、`issue-refinement-loop`、`post-merge-cleanup` |
| `edit-issue` | 既存 Issue 本文更新の手順（バックアップ / Guard / 差分閾値 / `gh issue edit --body-file`）| `issue-author` SubAgent、`issue-refinement-loop`、`post-merge-cleanup`、`review-issue`（needs-fix 適用時） |
| `review-issue` | Issue 本文の品質を決定論的にチェックして verdict と差分提案を返す | main session、`issue-reviewer` SubAgent（`issue-refinement-loop` loop worker 経由） |
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

### ssot-discovery 発動タイミング

以下の操作・コンテキストで `ssot-discovery` を積極的にトリガーする:

| 発動タイミング | 例 |
|---|---|
| 実装 Issue 着手前 | Issue に変更対象パスが含まれる場合、関連 SSOT を事前確認 |
| 開発フロー・ワークフロー変更 | workflow / CI / hooks / worktree 操作に関するキーワードが含まれる場合 |
| SubAgent / Skill 設計変更 | agent, skill, subagent, 責務, control-plane に関する操作 |
| GitHub 操作（Issue / PR 共通） | gh, github, ops, label, comment に関する操作 |
| **GitHub metadata / Milestone 操作** | milestone, github-milestone, milestone 作成・割当・close・rollup に関する操作。正本は `docs/dev/milestone-ops.md` |
| アーキテクチャ境界変更 | src/state, src/render, src/systems 等のレイヤー変更 |
| 新規 SSOT 追加時 | docs/ への新規文書作成時に既存カタログとの整合確認 |

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

## ORCHESTRATOR_IO_BOUNDARY_V1

オーケストレーター（`impl-review-loop` / `issue-refinement-loop`）が保持してよいコンテキストと、保持・処理を禁止するコンテキストを定義する。

> **適用スコープ**: 本 PR（#238 / Issue #227）では `issue-refinement-loop` への適用を完了条件とする。
> `impl-review-loop` および他 orchestrator skill への適合確認は follow-up Issue の Remaining Parent Gap として扱う。

### 保持可能コンテキスト（Allowed Context）

オーケストレーターのメインスレッドが LOOP_STATE として保持・参照してよい情報:

```yaml
allowed_context:
  - issue_number          # Issue 番号（数値 ID のみ）
  - loop_id               # ループインスタンスの識別子
  - iteration             # 現在のイテレーション番号（0-indexed）
  - max_iterations        # 最大イテレーション数
  - last_verdict          # approve | needs-fix | null（verdict 値のみ）
  - termination_reason    # approved | max_iterations | human_escalation | superseded_by_decision | null
  - pr_url                # PR URL（routing 判断に使うメタデータのみ）
  - branch                # ブランチ名
  - worktree              # worktree パス
  - head_sha              # コミット SHA（routing 用）
  - blockers_history      # blockers の要約リスト（構造化データのみ）
  - improvements_applied  # 改善履歴（各 iteration の概要のみ）
  - subagent_result_refs  # SubAgent 結果の参照（GitHub comment URL / issue_url 等）
  - opaque_forwarding_payload  # SubAgent から後続 SubAgent へ転送する opaque payload
                               # （routing 判断には使わない。blocking_issues / diff_proposal 等）
```

### 禁止コンテキスト（Forbidden Context）

オーケストレーターが直接保持・解釈・routing 判断に使用してはならない情報:

```yaml
forbidden_context:
  - raw_issue_body          # Issue 本文の raw テキスト全体
  - raw_pr_diff             # PR の raw diff テキスト
  - review_details          # review-issue / pr-review-judge の詳細な domain judgment 内容（routing 判断への使用禁止）
  - blocking_issue_details  # blocking_issues の個別テキスト（routing 判断に使用禁止。opaque forwarding は allowed_context 参照）
  - code_content            # 実装ファイルのコード内容
  - test_output_raw         # テスト実行の生出力
```

> **Note**: `diff_proposal` / `blocking_issues` の内容テキストは routing 判断に使用してはならないが、
> 後続 SubAgent（`issue-author` 等）への **opaque forwarding payload** として LOOP_STATE に保持・転送することは許可する。
> orchestrator はこれらの内容を再解釈せず、受け取ったまま転送する（`detail_payload_policy: opaque_ref_only`）。

**禁止の理由**: raw コンテンツをオーケストレーターが直接保持すると以下の問題が生じる。

- Context Rot: raw テキストがメインスレッドに蓄積し、イテレーションを経るごとに context window を圧迫する
- 責務汚染: routing 判断（control-plane）に domain judgment（data-plane）が混入する
- 冪等性破壊: SubAgent が独立して判断できるはずの情報をオーケストレーターが先読みすることで、SubAgent の出力と競合する

### オーケストレーターの worker step Skill 直接呼び出し禁止原則

**オーケストレーターは worker step で `Skill` tool を直接呼ばない。** すべての worker step は対応する SubAgent 境界を通す。

```
WRONG（禁止パターン）:
  orchestrator → Skill tool（review-issue skill）直接呼び出し
  orchestrator → Skill tool（implement-issue skill）直接呼び出し

CORRECT（正しいパターン）:
  orchestrator → issue-reviewer SubAgent → review-issue skill
  orchestrator → implementation-worker SubAgent → implement-issue skill
  orchestrator → issue-author SubAgent → edit-issue skill
```

#### 違反パターンの例示

以下のような呼び出し形式は ORCHESTRATOR_IO_BOUNDARY_V1 の違反:

```yaml
# 違反例 1: issue-refinement-loop Step 2 での Skill 直接呼び出し
step: 2
action: |
  skill: review-issue
  inputs:
    issue_number: <LOOP_STATE.issue_number>
    invoked_as_loop: true
# → SubAgent 境界なしで Skill を直接実行している

# 違反例 2: impl-review-loop Step での Skill 直接呼び出し
step: verification
action: |
  skill: pr-review-judge
  inputs:
    pr_url: <PR URL>
# → SubAgent 境界なしで Skill を直接実行している
```

#### SubAgent 境界を通す理由

1. **コンテキスト隔離**: SubAgent は隔離されたコンテキストで実行されるため、オーケストレーターの蓄積コンテキストに影響されない
2. **結果の構造化**: SubAgent は構造化された出力スキーマ（`REVIEW_ISSUE_RESULT_V1` 等）を返すため、オーケストレーターは verdict / status のみを参照して routing できる
3. **再試行・タイムアウト耐性**: SubAgent 境界があることで、個別 step の再試行が可能
4. **permissionMode 分離**: read-only worker は `dontAsk` permissionMode で動作し、write worker と明確に分離される

#### routing 判断の制約

オーケストレーターが SubAgent から結果を受け取った後の routing 判断では、以下のフィールドのみを参照する:

```yaml
routing_allowed_fields:
  REVIEW_ISSUE_RESULT_V1:
    - verdict        # approve | needs-fix
    - status         # ok | failed
    - failure_class  # gh_auth | permission_denied | issue_not_found | schema_invalid | unknown（status: failed 時のみ）
  TEST_VERDICT_MACHINE/v1:
    - status     # pass | partial | fail
    - summary    # 統計のみ（raw 出力は参照しない）
  LOOP_VERDICT:
    - verdict    # APPROVE | REQUEST_CHANGES
    - status     # ok | failed
  IMPLEMENT_RESULT_V1:
    - status     # ok | failed | blocked
```

詳細な domain judgment（blocking_issues のテキスト / diff_proposal の内容 / test failure の詳細 等）は、後続 SubAgent へ参照（GitHub comment URL / issue_url）として渡す。オーケストレーターが詳細を再解釈しない。

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
docs-only・merge-only・PR-body-only コミットは原則 test-runner をスキップできるが、ledger に `skip_reason` を記録する。
ただし以下の normative docs は material delta とみなし、skip 不可または明示的な human_review_required を伴う:
- `.claude/skills/**/SKILL.md`
- `.claude/agents/*.md`
- `CLAUDE.md`
- `.claude/rules/**`
- `docs/dev/*policy*.md`
- schema / contract / gate / permission / verification を定義する docs

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
5. **ledger 不完全**: `required: true` の全 phase
   (issue_contract_preflight, implementation, post_commit_verification,
   pr_body_update, semantic_review, pre_merge_judgment)
   が `status: pass` または明示的に許可された `status: skip` / `partial` になっていない場合。
   `runtime_preflight` は Runtime Verification Applicability が `immediate` のときのみ required とする。
6. **PR 本文必須セクション欠落**: PR 本文から `## Verification Commands 結果` または `## SubAgent Execution Ledger` セクションが消失している
7. **evidence 不足**: Required phase の ledger エントリに `evidence.source_kind` が `github_comment` / `ci_check` / `hook_jsonl` / `transcript` / `artifact` のいずれか（PR 本文以外）の証拠源が少なくとも 1 つ存在しない

> **APPROVE requires at least one non-PR-body evidence source for every required phase.**
> PR body ledger is a summary, not the authoritative record.

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
      source_kind: github_comment  # github_comment | ci_check | hook_jsonl | transcript | artifact
      source_ref: "<contract snapshot comment URL>"
      observed_head_sha: "<sha>"
      produced_at: "<ISO8601>"
      source_sha256: "<optional-for-local-artifact>"
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
    evidence:
      source_kind: hook_jsonl      # github_comment | ci_check | hook_jsonl | transcript | artifact
      source_ref: "<path-or-url>"
      observed_head_sha: "<sha>"
      produced_at: "<ISO8601>"
  - phase: post_commit_verification
    required: true
    agent: test-runner | ci
    verification_source: subagent | ci_check | manual
    status: pass          # pass | partial | skip | fail | blocked
    commit_sha: "<sha>"
    verdict_ref: "TEST_VERDICT_MACHINE/v1"
    ci_check_ref: "<optional GitHub check URL>"
    evidence:
      source_kind: ci_check        # github_comment | ci_check | hook_jsonl | transcript | artifact
      source_ref: "<GitHub check URL or artifact path>"
      observed_head_sha: "<sha>"
      produced_at: "<ISO8601>"
    runtime_verification:
      applicability: not_applicable | immediate | deferred
      verification_skipped_count: 0
      fallback_detected: false
      runtime_ac_results:
        - ac: AC7
          verdict: pass | skip | fail
          exit_code: 0
          artifact_ref: "<path-or-url>"
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
    evidence:
      source_kind: github_comment  # github_comment | ci_check | hook_jsonl | transcript | artifact
      source_ref: "<PR review comment URL>"
      observed_head_sha: "<sha>"
      produced_at: "<ISO8601>"
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

### waiver schema（skip / partial 時の免除申告）

`required: true` の phase で `status: skip` または `status: partial` の場合、`waiver` フィールドが必須。
waiver なしの skip / partial は APPROVE 禁止条件（条件 5 の延長）として扱う。

```yaml
waiver:
  required_when: "status in [skip, partial] and required == true"
  decision: allow_merge | defer_to_followup | block
  approver: human | pr-review-judge | ci_policy
  reason: "<why this is safe>"
  evidence_ref: "<url-or-artifact>"
  followup_issue: "<required if decision: defer_to_followup>"
  expires_at: "<optional ISO8601>"
```

APPROVE 条件（waiver 追加分）:
> Required phase の `skip` / `partial` は、`waiver.decision: allow_merge` または
> `waiver.decision: defer_to_followup` かつ `followup_issue` が存在する場合のみ merge 許容とする。
> `human_review_required: true` が残っている場合、`pre_merge_judgment` は `pass` にしてはならない。

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
  required_total: 6
  required_pass: <int>
  required_pending: <int>
  required_blocked: 0
  required_skipped_with_waiver: 0
  human_review_required: true  # pending > 0 の場合は必ず true
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
| Agent hook による検査用 SubAgent 起動 | 原則 No / experimental | `type: "agent"` hook は SubAgent を spawn できるが experimental。production workflow では command hook による記録・検査・deny と、CI / pr-review-judge / Agent SDK による hard gate を優先する |
| 複数 SubAgent の厳密なワークフロー制御 | Agent SDK 寄り | 状態管理・再試行・権限・セッションをコードで扱うべき |

### Stop hook の位置づけ

Stop hook は in-session soft gate であり、GitHub 上の APPROVE / merge を単独では保証しない。
**Stop hook のブロックは Claude Code セッション内の継続制御であり、GitHub 上の APPROVE / merge を防ぐ hard gate ではない。**
hard gate は pr-review-judge、CI check、または pre-merge script / GitHub branch protection に置く。
Stop hook は「不足を検出して作業継続を促す」ための補助層とする。

### Agent SDK との境界

hook は「決定論的なシェルコマンド実行・記録・ブロック」に向く。
`impl-review-loop` を外部の決定論的オーケストレーターに寄せたい場合は、Agent SDK 化を別 Issue で検討する。
